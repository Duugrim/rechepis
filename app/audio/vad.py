"""Energy based speech detector with an adaptive noise floor.

Deliberately simple: a dictated monologue into a close microphone has a clear gap between the level of
speech and the level of the room, and that gap is all we need to find pauses.

Two thresholds come out of it:

* the **speech threshold** (noise floor + sensitivity): below it there is no speech at all. It decides what
  may be thrown away and where a segment really starts and ends;
* the **dip threshold** (typical speech level - pause depth): a phrase break is usually not silence but
  a dip, because of breathing, room tails and the microphone's own gain control. It decides where a
  segment may be *cut*.

Frame states returned by :meth:`SpeechDetector.step`:
``SILENCE``, ``SPEECH`` and ``ONSET`` (the first frame where speech is *confirmed*; the speech really
started ``ONSET_FRAMES - 1`` frames earlier).
"""

from __future__ import annotations

import numpy as np
from scipy.signal import butter, sosfilt, sosfilt_zi

from . import SAMPLE_RATE

FRAME_MS = 20
FRAME_LEN = SAMPLE_RATE * FRAME_MS // 1000  # 320 samples
FRAME_S = FRAME_MS / 1000.0

SILENCE, SPEECH, ONSET = 0, 1, 2

ONSET_FRAMES = 3  # 60 ms above threshold before speech is confirmed (rejects clicks)
HANGOVER_FRAMES = 4  # 80 ms kept after the level drops (bridges stops and plosives)
HYSTERESIS_DB = 4.0
ABS_MIN_THRESHOLD_DB = -60.0
DIGITAL_SILENCE_DB = -90.0  # exact zeros (a gated or muted source) say nothing about the room
FLOOR_MIN_DB, FLOOR_MAX_DB = -80.0, -35.0
FLOOR_WINDOW_FRAMES = 750  # 15 s
FLOOR_PERCENTILE = 15
FLOOR_UPDATE_EVERY = 25
DEFAULT_FLOOR_DB = -55.0
WARMUP_FRAMES = 50  # 1 s: while the room is still unknown the floor may jump freely
FLOOR_RISE_DB_PER_S = 0.5
FLOOR_MAX_DRIFT_DB = 12.0  # the room may get this much noisier than its quietest moment, no more
SPEECH_REF_FRAMES = 1000  # 20 s of loud frames
SPEECH_REF_MIN = 50
SPEECH_REF_PERCENTILE = 80

_BAND_SOS = butter(2, [120, 3800], btype="bandpass", fs=SAMPLE_RATE, output="sos")


def frame_db(frames: np.ndarray) -> np.ndarray:
    """RMS level in dBFS of each row of ``frames`` (shape ``(n, FRAME_LEN)``)."""
    power = np.mean(np.square(frames, dtype=np.float32), axis=1)
    return (10.0 * np.log10(power + 1e-10)).astype(np.float32)


class BandPass:
    """Streaming speech band filter (120-3800 Hz): keeps rumble and hiss out of the level measurement."""

    def __init__(self) -> None:
        self._zi = sosfilt_zi(_BAND_SOS) * 0.0

    def process(self, x: np.ndarray) -> np.ndarray:
        y, self._zi = sosfilt(_BAND_SOS, x.astype(np.float32), zi=self._zi)
        return y.astype(np.float32)


class NoiseFloor:
    """Low percentile of the recent frame levels, clamped to a sane range.

    It may fall at once but rises slowly, and never beyond ``FLOOR_MAX_DRIFT_DB`` above the quietest value
    seen, so a long stretch of uninterrupted speech cannot drag the floor up to the level of the speech
    itself and make the rest of the monologue look like silence.
    """

    def __init__(self, initial_db: float = DEFAULT_FLOOR_DB) -> None:
        self._ring = np.zeros(FLOOR_WINDOW_FRAMES, dtype=np.float32)
        self._fed = 0
        self.value = float(np.clip(initial_db, FLOOR_MIN_DB, FLOOR_MAX_DB))
        self._lowest = self.value

    def prime(self, levels_db: np.ndarray) -> None:
        """Start from the quiet part of known audio (used when a whole session is analysed at once).

        Looks for the quietest *seconds*, not the quietest 15% of frames: a dense monologue may have less
        than 15% pauses, but it almost always has a second or two of room tone somewhere.
        """
        live = levels_db[levels_db > DIGITAL_SILENCE_DB]
        if len(live) == 0:
            return
        blocks = len(live) // WARMUP_FRAMES
        if blocks >= 2:
            lows = np.percentile(live[: blocks * WARMUP_FRAMES].reshape(blocks, WARMUP_FRAMES), 10, axis=1)
            value = np.sort(lows)[1]  # second quietest second: one odd dropout cannot set the floor
        else:
            value = np.percentile(live, FLOOR_PERCENTILE)
        self.value = float(np.clip(value, FLOOR_MIN_DB, FLOOR_MAX_DB))
        self._ring[:] = self.value
        self._fed = FLOOR_WINDOW_FRAMES
        self._lowest = self.value

    def feed(self, level_db: float) -> None:
        if level_db <= DIGITAL_SILENCE_DB:
            return
        self._ring[self._fed % FLOOR_WINDOW_FRAMES] = level_db
        self._fed += 1
        warming = self._fed < WARMUP_FRAMES * 2
        every = 10 if warming else FLOOR_UPDATE_EVERY
        if self._fed < 10 or self._fed % every:
            return
        window = self._ring[: min(self._fed, FLOOR_WINDOW_FRAMES)]
        target = float(np.clip(np.percentile(window, FLOOR_PERCENTILE), FLOOR_MIN_DB, FLOOR_MAX_DB))
        if warming or target <= self.value:
            self.value = target
        else:
            self.value = min(target, self.value + FLOOR_RISE_DB_PER_S * every * FRAME_S)
        self._lowest = min(self._lowest, self.value)
        if not warming:
            self.value = min(self.value, self._lowest + FLOOR_MAX_DRIFT_DB)


class SpeechDetector:
    def __init__(self, sensitivity_db: float = 10.0, pause_depth_db: float = 20.0, floor: NoiseFloor | None = None) -> None:
        self.sensitivity_db = float(sensitivity_db)
        self.pause_depth_db = float(pause_depth_db)
        self.floor = floor or NoiseFloor()
        self._active = False
        self._run = 0
        self._hang = 0
        self._loud = np.zeros(SPEECH_REF_FRAMES, dtype=np.float32)
        self._loud_n = 0
        self._speech_ref: float | None = None

    @property
    def threshold_db(self) -> float:
        return max(self.floor.value + self.sensitivity_db, ABS_MIN_THRESHOLD_DB)

    @property
    def speech_level_db(self) -> float | None:
        """Typical level of the loud frames of the last seconds (None until there are enough)."""
        return self._speech_ref

    @property
    def dip_threshold_db(self) -> float:
        if self._speech_ref is None:
            return self.threshold_db
        return max(self.threshold_db, self._speech_ref - self.pause_depth_db)

    def prime_speech_level(self, levels_db: np.ndarray) -> None:
        loud = levels_db[levels_db >= self.threshold_db]
        if len(loud) >= SPEECH_REF_MIN:
            self._speech_ref = float(np.percentile(loud, SPEECH_REF_PERCENTILE))

    def step(self, level_db: float) -> int:
        on = self.threshold_db
        off = on - HYSTERESIS_DB
        state = SILENCE
        if not self._active:
            if level_db >= on:
                self._run += 1
                if self._run >= ONSET_FRAMES:
                    self._active = True
                    self._hang = HANGOVER_FRAMES
                    state = ONSET
            else:
                self._run = 0
        else:
            if level_db >= off:
                self._hang = HANGOVER_FRAMES
                state = SPEECH
            else:
                self._hang -= 1
                if self._hang > 0:
                    state = SPEECH
                else:
                    self._active = False
                    self._run = 0
        self.floor.feed(level_db)
        if level_db >= on:
            self._loud[self._loud_n % SPEECH_REF_FRAMES] = level_db
            self._loud_n += 1
            if self._loud_n >= SPEECH_REF_MIN and self._loud_n % FLOOR_UPDATE_EVERY == 0:
                window = self._loud[: min(self._loud_n, SPEECH_REF_FRAMES)]
                self._speech_ref = float(np.percentile(window, SPEECH_REF_PERCENTILE))
        return state


def frames_of(x: np.ndarray) -> np.ndarray:
    n = len(x) // FRAME_LEN
    return x[: n * FRAME_LEN].reshape(n, FRAME_LEN)


def primed_detector(levels: np.ndarray, sensitivity_db: float, pause_depth_db: float = 20.0) -> SpeechDetector:
    """A detector that already knows the room and the speech level of a whole recording."""
    floor = NoiseFloor()
    floor.prime(levels)
    detector = SpeechDetector(sensitivity_db, pause_depth_db, floor)
    detector.prime_speech_level(levels)
    return detector
