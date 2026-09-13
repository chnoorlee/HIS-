class PcmCaptureProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this.samples = [];
    this.enabled = true;
    this.port.onmessage = (event) => {
      if (event.data?.type === "stop") {
        this.enabled = false;
        this.flush();
        this.port.postMessage({ type: "flushed", request_id: event.data.request_id });
      }
      if (event.data === "flush") this.flush();
      if (event.data === "pause") {
        this.flush();
        this.enabled = false;
      }
      if (event.data === "resume") this.enabled = true;
    };
  }
  flush() {
    if (this.samples.length) {
      const pcm = new Int16Array(this.samples.length);
      for (let i = 0; i < this.samples.length; i++)
        pcm[i] = Math.max(-1, Math.min(1, this.samples[i])) * 32767;
      this.port.postMessage({ type: "pcm", pcm: pcm.buffer }, [pcm.buffer]);
      this.samples = [];
    }
  }
  process(inputs) {
    if (!this.enabled) return true;
    const channel = inputs[0]?.[0];
    if (!channel) return true;
    let sum = 0;
    for (const value of channel) {
      this.samples.push(value);
      sum += value * value;
    }
    this.port.postMessage({
      type: "level",
      level: Math.sqrt(sum / channel.length),
    });
    if (this.samples.length >= 16000) this.flush();
    return true;
  }
}
registerProcessor("pcm-capture", PcmCaptureProcessor);
