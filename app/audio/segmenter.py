"""Cuts a stream of speech into segments at pauses.

Only frame levels are looked at, the audio itself is never held here: ``feed`` returns the sample ranges
of finished segments and the caller slices them out of the session recording. The same code serves the
live recording (a segment is released as soon as its closing pause is long enough, so transcription can
start while the user is still talking) and the offline re-cut of a stored session.

Rules, in order of priority:

1. a stretch without any speech of ``hard_pause_ms`` always ends the segment, and so does a *dip* that
   long once the segment has reached ``min_len_s`` (the thought is finished);
2. once the segment is at least ``target_len_s`` long, a dip of ``pause_ms`` ends it;
3. at ``max_len_s`` the segment is split at the longest earlier dip, and if there was none (continuous
   speech) at the quietest point.

"Dip" is a stretch quieter than ``pause_depth_db`` below the typical speech level (see ``vad``).
Silence between segments is dropped: a segment is the speech plus ``pad_ms`` on both sides. That keeps
long thinking pauses away from the recognisers, which tend to hallucinate text on silence.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from . import SAMPLE_RATE
from .vad import (
    FRAME_LEN,
    FRAME_S,
    ONSET,
    ONSET_FRAMES,
    SILENCE,
    BandPass,
    SpeechDetector,
    frame_db,
    primed_detector,
)

MIN_SPEECH_S = 0.25  # shorter bursts (coughs, clicks) are not worth a segment
MIN_CANDIDATE_PAUSE_S = 0.2  # shortest dip that may serve as a split point


@dataclass
class Cut:
    start: int  # first sample (SAMPLE_RATE) of the segment
    end: int  # one past the last sample
    reason: str  # long_pause | pause | max_len | forced | end | single
    speech_s: float
    gap_before_s: float = 0.0  # dropped audio between the previous segment and this one
    forced: bool = False

    @property
    def duration_s(self) -> float:
        return (self.end - self.start) / SAMPLE_RATE

    def to_dict(self) -> dict:
        return {
            "start": self.start,
            "end": self.end,
            "reason": self.reason,
            "speech_s": round(self.speech_s, 3),
            "gap_before_s": round(self.gap_before_s, 3),
        }


@dataclass
class _Open:
    start_f: int
    last_speech_f: int
    speech_frames: int = 0
    silence_start_f: int | None = None  # start of the current run without speech
    pauses: list[tuple[int, int]] = field(default_factory=list)  # closed dips inside the segment


class Segmenter:
    def __init__(self, settings: dict, detector: SpeechDetector | None = None) -> None:
        self.settings = settings
        self._vad = detector or SpeechDetector(float(settings["sensitivity_db"]), float(settings["pause_depth_db"]))
        self._band = BandPass()
        self._tail = np.zeros(0, dtype=np.float32)
        self._levels: list[float] = []
        self._open: _Open | None = None
        self._dip_start: int | None = None
        self._await_dip_end = False
        self._emitted_end_f = 0
        self._prev_end_sample = 0

        s = settings
        self._pad_f = round(s["pad_ms"] / 1000 / FRAME_S)
        self._min_f = round(s["min_len_s"] / FRAME_S)
        self._target_f = round(s["target_len_s"] / FRAME_S)
        self._max_f = round(s["max_len_s"] / FRAME_S)
        self._pause_f = max(1, round(s["pause_ms"] / 1000 / FRAME_S))
        self._hard_f = max(self._pause_f, round(s["hard_pause_ms"] / 1000 / FRAME_S))
        self._cand_f = max(1, round(MIN_CANDIDATE_PAUSE_S / FRAME_S))

    # -- live readings for the UI --------------------------------------------------------------------

    @property
    def frames_seen(self) -> int:
        return len(self._levels)

    @property
    def noise_floor_db(self) -> float:
        return self._vad.floor.value

    @property
    def threshold_db(self) -> float:
        return self._vad.threshold_db

    @property
    def dip_threshold_db(self) -> float:
        return self._vad.dip_threshold_db

    @property
    def level_db(self) -> float:
        return self._levels[-1] if self._levels else -100.0

    # -- feeding -------------------------------------------------------------------------------------

    def feed(self, x: np.ndarray) -> list[Cut]:
        """Consume audio; return the segments that became final."""
        if len(x) == 0:
            return []
        data = np.concatenate([self._tail, self._band.process(np.asarray(x, dtype=np.float32))])
        n = len(data) // FRAME_LEN
        self._tail = data[n * FRAME_LEN :]
        if n == 0:
            return []
        levels = frame_db(data[: n * FRAME_LEN].reshape(n, FRAME_LEN))
        cuts: list[Cut] = []
        for level in levels:
            f = len(self._levels)
            self._levels.append(float(level))
            state = self._vad.step(float(level))
            cuts.extend(self._on_frame(f, state, float(level) < self._vad.dip_threshold_db))
        return cuts

    def flush(self) -> list[Cut]:
        """End of the stream: whatever is open becomes the last segment."""
        cuts: list[Cut] = []
        if self._open is not None:
            cut = self._close(min(self._open.last_speech_f + 1 + self._pad_f, len(self._levels)), "end")
            if cut:
                cuts.append(cut)
        return cuts

    # -- state machine -------------------------------------------------------------------------------

    def _on_frame(self, f: int, state: int, quiet: bool) -> list[Cut]:
        seg = self._open

        # dips: runs of frames well below the speech level
        if quiet:
            if self._dip_start is None:
                self._dip_start = f
        elif self._dip_start is not None:
            if seg is not None:
                start = max(self._dip_start, seg.start_f)
                if f - start >= self._cand_f:
                    seg.pauses.append((start, f))
            self._dip_start = None

        if state != SILENCE:
            onset_f = f - (ONSET_FRAMES - 1) if state == ONSET else f
            if seg is None:
                if not (self._await_dip_end and quiet):
                    self._await_dip_end = False
                    self._open = _Open(
                        start_f=max(onset_f - self._pad_f, self._emitted_end_f), last_speech_f=f, speech_frames=1
                    )
            else:
                seg.silence_start_f = None
                seg.last_speech_f = f
                seg.speech_frames += 1
        elif seg is not None and seg.silence_start_f is None:
            seg.silence_start_f = seg.last_speech_f + 1

        seg = self._open
        if seg is None:
            return []
        cuts: list[Cut] = []

        silent_for = f + 1 - seg.silence_start_f if seg.silence_start_f is not None else 0
        dip_start = self._dip_start
        dip_for = f + 1 - max(dip_start, seg.start_f) if dip_start is not None else 0
        content_to_dip = (dip_start - seg.start_f) if dip_start is not None else 0
        end_after_speech = min(seg.last_speech_f + 1 + self._pad_f, f + 1)

        cut: Cut | None = None
        if silent_for >= self._hard_f:
            cut = self._close(end_after_speech, "long_pause")
        elif dip_for >= self._hard_f and content_to_dip >= self._min_f:
            cut = self._close(min(dip_start + self._pad_f, f + 1), "long_pause", await_dip_end=True)
        elif dip_for >= self._pause_f and content_to_dip >= self._target_f:
            cut = self._close(min(dip_start + self._pad_f, f + 1), "pause", await_dip_end=True)
        else:
            while self._open is not None and f + 1 - self._open.start_f >= self._max_f:
                piece = self._split_at_max(f)
                if piece:
                    cuts.append(piece)
            return cuts
        if cut:
            cuts.append(cut)
        return cuts

    def _split_at_max(self, f: int) -> Cut | None:
        seg = self._open
        assert seg is not None
        candidates = list(seg.pauses)
        if self._dip_start is not None:
            start = max(self._dip_start, seg.start_f)
            if f + 1 - start >= self._cand_f:
                candidates.append((start, f + 1))
        roomy = [p for p in candidates if p[0] - seg.start_f >= self._min_f]
        pool = roomy or [p for p in candidates if p[0] - seg.start_f >= self._cand_f]
        if pool:
            p0, p1 = max(pool, key=lambda p: (p[1] - p[0], p[0]))
            end = min(p0 + self._pad_f, p1)
            return self._close(end, "max_len", reopen_at=max(p1 - self._pad_f, end), keep_after=p1)
        # No pause to split at: take the quietest moment in the last part of the window.
        lo = seg.start_f + max(self._min_f, int(0.6 * self._max_f))
        window = np.asarray(self._levels[lo : f + 1], dtype=np.float32)
        if len(window) >= 5:
            smooth = np.convolve(window, np.ones(5, dtype=np.float32) / 5, mode="same")
            at = lo + int(np.argmin(smooth[2:-2])) + 2
        else:
            at = f + 1
        return self._close(at, "forced", reopen_at=at, keep_after=at, forced=True)

    def _close(
        self,
        end_f: int,
        reason: str,
        reopen_at: int | None = None,
        keep_after: int = 0,
        forced: bool = False,
        await_dip_end: bool = False,
    ) -> Cut | None:
        seg = self._open
        assert seg is not None
        start_f = seg.start_f
        end_f = max(end_f, start_f + 1)
        speech_in_cut = seg.speech_frames
        if reopen_at is None:
            self._open = None
            self._await_dip_end = await_dip_end
        else:
            # The tail of the segment goes on as a new one; its speech counter restarts from what is left.
            kept = sum(1 for lv in self._levels[reopen_at : seg.last_speech_f + 1] if lv >= self._vad.threshold_db - 4)
            speech_in_cut = max(0, seg.speech_frames - kept)
            self._open = _Open(
                start_f=reopen_at,
                last_speech_f=seg.last_speech_f,
                speech_frames=kept,
                silence_start_f=seg.silence_start_f if seg.silence_start_f is not None and seg.silence_start_f >= reopen_at else None,
                pauses=[p for p in seg.pauses if p[0] >= keep_after],
            )
        self._emitted_end_f = end_f
        speech_s = speech_in_cut * FRAME_S
        start_sample, end_sample = start_f * FRAME_LEN, end_f * FRAME_LEN
        gap = max(0.0, (start_sample - self._prev_end_sample) / SAMPLE_RATE)
        self._prev_end_sample = end_sample
        if speech_s < MIN_SPEECH_S:
            return None
        return Cut(start_sample, end_sample, reason, speech_s, gap, forced)


def cut_audio(x: np.ndarray, settings: dict) -> list[Cut]:
    """Cut a whole recording (offline). Same rules as the live path, but the room is known upfront."""
    usable = len(x) // FRAME_LEN * FRAME_LEN
    if usable == 0:
        return []
    x = np.asarray(x[:usable], dtype=np.float32)
    levels = frame_db(BandPass().process(x).reshape(-1, FRAME_LEN))
    detector = primed_detector(levels, float(settings["sensitivity_db"]), float(settings["pause_depth_db"]))
    segmenter = Segmenter(settings, detector)
    cuts = segmenter.feed(x)
    cuts.extend(segmenter.flush())
    return cuts


def single_cut(n_samples: int) -> list[Cut]:
    """The whole session as one segment (slicing switched off)."""
    if n_samples <= 0:
        return []
    return [Cut(0, n_samples, "single", n_samples / SAMPLE_RATE, 0.0)]
