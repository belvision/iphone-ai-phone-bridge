/* Local PCM bridge: no microphone capture, network access, or speaker playback. */
class NativeCaptureSource extends AudioWorkletProcessor {
  constructor() {
    super();
    this.capacity = Math.round(sampleRate * 0.2);
    this.ring = new Float32Array(this.capacity);
    this.read = 0;
    this.length = 0;
    this.active = true;
    this.dropped = 0;
    this.statsFrames = 0;
    this.statsPeak = 0;
    this.port.onmessage = ({ data }) => {
      if (data?.type === 'stop') { this.active = false; this.length = 0; return; }
      if (data?.type === 'clear') { this.length = 0; return; }
      if (!(data instanceof ArrayBuffer)) return;
      let samples = new Float32Array(data);
      if (samples.length > this.capacity) samples = samples.subarray(samples.length - this.capacity);
      const overflow = Math.max(0, this.length + samples.length - this.capacity);
      this.read = (this.read + overflow) % this.capacity;
      this.length -= overflow;
      this.dropped += overflow;
      for (let i = 0; i < samples.length; i++) {
        const value = samples[i];
        this.ring[(this.read + this.length + i) % this.capacity] = Number.isFinite(value) ? Math.max(-1, Math.min(1, value)) : 0;
      }
      this.length += samples.length;
    };
  }

  process(_inputs, outputs) {
    const output = outputs[0]?.[0];
    if (!this.active) return false;
    if (!output) return true;
    output.fill(0);
    const count = Math.min(output.length, this.length);
    for (let i = 0; i < count; i++) {
      output[i] = this.ring[(this.read + i) % this.capacity];
      this.statsPeak = Math.max(this.statsPeak, Math.abs(output[i]));
    }
    this.read = (this.read + count) % this.capacity;
    this.length -= count;
    this.statsFrames += output.length;
    if (this.statsFrames >= sampleRate / 2) {
      this.port.postMessage({ type: 'capture_stats', frames: this.statsFrames, peak: this.statsPeak, dropped: this.dropped, queued: this.length });
      this.statsFrames = this.statsPeak = 0;
    }
    return true;
  }
}

class NativeRemoteSink extends AudioWorkletProcessor {
  constructor() {
    super();
    this.chunkSize = Math.round(sampleRate * 0.02);
    this.chunk = new Float32Array(this.chunkSize);
    this.offset = 0;
    this.active = true;
    this.peak = 0;
    this.port.onmessage = ({ data }) => {
      if (data?.type === 'stop') { this.active = false; this.offset = 0; }
    };
  }

  process(inputs, outputs) {
    if (!this.active) return false;
    // The sink is connected to the physical destination only through silence.
    // Explicit zero output prevents any remote voice entering speaker loopback.
    for (const output of outputs) for (const channel of output) channel.fill(0);
    const channels = inputs[0];
    const frames = channels?.[0]?.length || outputs[0]?.[0]?.length || 128;
    for (let frame = 0; frame < frames; frame++) {
      let value = 0;
      if (channels?.length) {
        for (const channel of channels) value += channel[frame] || 0;
        value /= channels.length;
      }
      this.chunk[this.offset++] = Number.isFinite(value) ? Math.max(-1, Math.min(1, value)) : 0;
      this.peak = Math.max(this.peak, Math.abs(this.chunk[this.offset - 1]));
      if (this.offset === this.chunkSize) {
        const buffer = this.chunk.buffer;
        this.port.postMessage({ type: 'pcm', buffer, time: currentTime, peak: this.peak }, [buffer]);
        this.chunk = new Float32Array(this.chunkSize);
        this.offset = 0;
        this.peak = 0;
      }
    }
    return true;
  }
}

registerProcessor('native-capture-source', NativeCaptureSource);
registerProcessor('native-remote-sink', NativeRemoteSink);
