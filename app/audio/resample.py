from __future__ import annotations

import math

import numpy as np
from scipy.signal import resample_poly

from . import SAMPLE_RATE


def resample(x: np.ndarray, in_rate: int, out_rate: int = SAMPLE_RATE) -> np.ndarray:
    """Band-limited (polyphase FIR) sample rate conversion of a whole signal."""
    if in_rate == out_rate or len(x) == 0:
        return x.astype(np.float32, copy=False)
    g = math.gcd(int(in_rate), int(out_rate))
    return resample_poly(x, out_rate // g, in_rate // g).astype(np.float32)


class StreamResampler:
    """Resamples a stream chunk by chunk with the same filter as :func:`resample`.

    The browser delivers audio at the device rate (usually 48 kHz). Converting it here, with a proper
    anti-aliasing filter, replaces the linear interpolation the old front end did, which folded
    everything above 8 kHz back into the speech band.

    Overlap-save: a few input samples of context are kept on both sides of every block, so block
    boundaries leave no clicks and the output equals a one-shot ``resample_poly`` up to rounding.
    """

    def __init__(self, in_rate: int, out_rate: int = SAMPLE_RATE) -> None:
        g = math.gcd(int(in_rate), int(out_rate))
        self.up = int(out_rate) // g
        self.down = int(in_rate) // g
        self.passthrough = self.up == self.down
        # FIR half-width of resample_poly is 10 * max(up, down) taps at the upsampled rate.
        half_in = math.ceil(10 * max(self.up, self.down) / self.up) + 2
        self._ctx = -(-half_in // self.down) * self.down  # multiple of `down`, so block starts stay aligned
        self._buf = np.zeros(0, dtype=np.float32)
        self._buf_start = 0  # global input index of _buf[0]; always a multiple of `down`
        self._out_done = 0  # global output samples already returned

    def process(self, x: np.ndarray, final: bool = False) -> np.ndarray:
        x = np.asarray(x, dtype=np.float32)
        if self.passthrough:
            return x
        self._buf = np.concatenate([self._buf, x]) if len(x) else self._buf
        total_in = self._buf_start + len(self._buf)
        if final:
            out_end = -(-total_in * self.up // self.down)
        else:
            out_end = max(0, (total_in - self._ctx) * self.up // self.down)
        if out_end <= self._out_done or len(self._buf) == 0:
            return np.zeros(0, dtype=np.float32)

        y = resample_poly(self._buf, self.up, self.down)
        base = self._buf_start * self.up // self.down
        out = y[self._out_done - base : out_end - base].astype(np.float32)
        self._out_done = out_end

        keep_from = max(0, (self._out_done * self.down // self.up - self._ctx) // self.down * self.down)
        drop = keep_from - self._buf_start
        if drop > 0:
            self._buf = self._buf[drop:]
            self._buf_start = keep_from
        return out
