"""Audio core: capture-side resampling, pause detection, cutting, cleaning and levelling.

Everything here works on mono float32 arrays at ``SAMPLE_RATE`` and is independent of the web layer.
"""

SAMPLE_RATE = 16000
