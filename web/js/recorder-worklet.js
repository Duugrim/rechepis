/* Runs on the audio thread: collects the microphone into 16-bit mono blocks and posts them to the page.
   Keeping this off the main thread means a busy UI can never cause gaps in the recording. */
class PcmCapture extends AudioWorkletProcessor {
  constructor() {
    super();
    this.size = 2048;
    this.buffer = new Int16Array(this.size);
    this.filled = 0;
    this.peak = 0;
    this.sum = 0;
    this.count = 0;
  }

  process(inputs) {
    const channels = inputs[0];
    if (channels && channels.length) {
      const frames = channels[0].length;
      for (let i = 0; i < frames; i++) {
        let v = 0;
        for (let c = 0; c < channels.length; c++) v += channels[c][i];
        v /= channels.length;
        const a = Math.abs(v);
        if (a > this.peak) this.peak = a;
        this.sum += v * v;
        this.count += 1;
        this.buffer[this.filled++] = v < 0 ? Math.max(-1, v) * 32768 : Math.min(1, v) * 32767;
        if (this.filled === this.size) this.flush();
      }
    }
    return true;
  }

  flush() {
    const rms = Math.sqrt(this.sum / Math.max(1, this.count));
    this.port.postMessage({ pcm: this.buffer.buffer, peak: this.peak, rms }, [this.buffer.buffer]);
    this.buffer = new Int16Array(this.size);
    this.filled = 0;
    this.peak = 0;
    this.sum = 0;
    this.count = 0;
  }
}

registerProcessor("pcm-capture", PcmCapture);
