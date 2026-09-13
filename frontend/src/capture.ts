import { post } from "./api";
import type { CaptureEnd } from "./bridge";
interface Chunk {
  type: "chunk";
  protocol_version: number;
  session_id: string;
  capture_epoch: number;
  channel_id: string;
  seq: number;
  sample_start: number;
  sample_count: number;
  sample_rate: number;
  encoding: string;
  sha256: string;
  data: string;
}
export interface CaptureState {
  state: string;
  level: number;
  pending: number;
  durable: number;
  message: string;
}
export class BrowserCapture {
  private context?: AudioContext;
  private stream?: MediaStream;
  private node?: AudioWorkletNode;
  private socket?: WebSocket;
  private epoch = Date.now();
  private seq = 0;
  private sampleEnd = 0;
  private generation = 0;
  private disposed = false;
  private captureFailure?: Error;
  private pending = new Map<number, Chunk>();
  private sent = new Map<number, string>();
  private acknowledged = new Set<number>();
  private flushWaiters = new Map<string, { resolve: () => void; reject: (error: Error) => void }>();
  private serial: Promise<void> = Promise.resolve();
  private current: CaptureState = { state: "IDLE", level: 0, pending: 0, durable: 0, message: "" };
  constructor(private sessionId: string, private update: (state: CaptureState) => void) {}
  private check(generation: number) {
    if (this.disposed || generation !== this.generation) throw new DOMException("Capture cancelled", "AbortError");
  }
  private publish(value: Partial<CaptureState>) {
    if (this.disposed) return;
    this.current = { ...this.current, ...value, pending: this.pending.size };
    this.update(this.current);
  }
  private failInput(message: string) {
    this.captureFailure ??= new Error(message);
    this.pause();
    this.publish({ state: "ERROR", message: this.captureFailure.message });
  }
  private checkInput() {
    if (this.captureFailure) throw this.captureFailure;
    const tracks = this.stream?.getAudioTracks() ?? [];
    if (!tracks.length || tracks.some((track) => track.readyState !== "live" || track.muted)) {
      this.failInput("麦克风不可用，无法继续录音。请核查并隔离当前会话后重新采集。");
      throw this.captureFailure;
    }
  }
  async start(deviceId?: string) {
    const generation = this.generation;
    this.check(generation);
    this.publish({ state: "STARTING" });
    try {
      if (!navigator.mediaDevices?.getUserMedia) throw new Error("当前浏览器无法访问麦克风，请使用 HTTPS 或本机地址。");
      const stream = await navigator.mediaDevices.getUserMedia({
        audio: { deviceId: deviceId ? { exact: deviceId } : undefined, channelCount: 1, sampleRate: 16000, echoCancellation: true, noiseSuppression: true },
        video: false,
      });
      if (this.disposed || generation !== this.generation) { stream.getTracks().forEach((track) => track.stop()); this.check(generation); }
      this.stream = stream;
      this.stream.getAudioTracks().forEach((track) => {
        for (const type of ["ended", "mute"]) track.addEventListener(type, () => {
          if (this.disposed || generation !== this.generation) return;
          this.failInput(type === "ended" ? "麦克风已断开，无法确认音频完整性。请核查并隔离当前会话后重新采集。" : "麦克风输入已中断，无法确认音频完整性。请核查并隔离当前会话后重新采集。");
        });
      });
      this.checkInput();
      this.context = new AudioContext({ sampleRate: 16000 });
      if (this.context.sampleRate !== 16000) throw new Error("当前音频设备不支持 16 kHz 采音，请改用 Windows 客户端。");
      await this.context.audioWorklet.addModule("/capture-worklet.js");
      this.check(generation);
      await this.connect();
      this.check(generation);
      this.node = new AudioWorkletNode(this.context, "pcm-capture");
      this.node.port.onmessage = (event: MessageEvent<{ type: string; level?: number; pcm?: ArrayBuffer; request_id?: string }>) => {
        if (this.disposed || generation !== this.generation) return;
        if (event.data.type === "flushed" && event.data.request_id) {
          this.flushWaiters.get(event.data.request_id)?.resolve();
          this.flushWaiters.delete(event.data.request_id);
        }
        if (event.data.type === "level") this.publish({ level: event.data.level ?? 0 });
        if (event.data.pcm) {
          const buffer = event.data.pcm;
          this.serial = this.serial.then(() => this.enqueue(buffer, generation)).catch((error) => {
            if (this.disposed || generation !== this.generation) return;
            this.captureFailure = error instanceof Error ? error : new Error(String(error));
            this.pause();
            this.publish({ state: "ERROR", message: String(error) });
          });
        }
      };
      const source = this.context.createMediaStreamSource(this.stream);
      source.connect(this.node);
      const mute = this.context.createGain();
      mute.gain.value = 0;
      this.node.connect(mute).connect(this.context.destination);
      await this.context.resume();
      this.check(generation);
      this.checkInput();
      this.publish({ state: "RECORDING", message: "" });
    } catch (error) {
      if (!this.disposed && generation === this.generation) await this.dispose();
      throw error;
    }
  }
  private send(socket: WebSocket, chunk: Chunk) {
    this.sent.set(chunk.seq, chunk.sha256);
    try { socket.send(JSON.stringify(chunk)); }
    catch { this.pause(); this.publish({ message: "音频连接中断，原始分片已保留，请恢复连接后继续。" }); }
  }
  private async connect() {
    const generation = this.generation;
    this.check(generation);
    const ticket = await post<{ ticket: string }>(`/sessions/${this.sessionId}/stream-ticket`, { device_id: "browser-single-channel", channels: ["doctor_mic"] });
    this.check(generation);
    await new Promise<void>((resolve, reject) => {
      const socket = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/api/v1/audio?ticket=${encodeURIComponent(ticket.ticket)}`);
      this.socket = socket;
      let opened = false;
      const timer = window.setTimeout(() => { socket.close(); reject(new Error("音频接入连接超时")); }, 10000);
      socket.onopen = () => {
        clearTimeout(timer);
        if (this.disposed || generation !== this.generation) { socket.close(); reject(new DOMException("Capture cancelled", "AbortError")); return; }
        opened = true;
        for (const chunk of this.pending.values()) this.send(socket, chunk);
        resolve();
      };
      socket.onerror = () => { clearTimeout(timer); reject(new Error("无法建立音频连接")); };
      socket.onclose = () => {
        clearTimeout(timer);
        if (!opened) reject(new DOMException("Audio connection closed", this.disposed ? "AbortError" : "NetworkError"));
        if (this.disposed || generation !== this.generation) return;
        if (this.current.state === "RECORDING") {
          this.pause();
          this.publish({ message: "音频连接中断，已暂停。待确认音频仅保留于当前页面内存。" });
        }
      };
      socket.onmessage = (event) => {
        if (this.disposed || generation !== this.generation) return;
        try {
          const data = JSON.parse(event.data);
          if (data.type === "ACK_DURABLE") {
            if (data.session_id !== this.sessionId || data.channel_id !== "doctor_mic" || String(data.capture_epoch) !== String(this.epoch) || !Number.isInteger(data.seq) || data.seq < 0 || this.sent.get(data.seq) !== data.sha256 || !this.sent.has(data.seq) || !Number.isInteger(data.contiguous_seq) || data.contiguous_seq < -1 || data.contiguous_seq >= this.seq)
              throw new Error("音频确认与已发送分片不匹配，原始分片已保留。请核查服务端。");
            // A waterline alone cannot prove a particular local chunk was persisted.
            this.pending.delete(data.seq);
            this.acknowledged.add(data.seq);
            this.publish({ durable: this.acknowledged.size });
          }
          if (data.type === "ERROR") throw new Error(data.message ?? data.code ?? "音频服务拒绝分片");
        } catch (error) {
          this.pause();
          this.publish({ state: "ERROR", message: String(error) });
        }
      };
    });
    this.check(generation);
  }
  private async enqueue(buffer: ArrayBuffer, generation: number) {
    this.check(generation);
    const bytes = new Uint8Array(buffer);
    const hash = new Uint8Array(await crypto.subtle.digest("SHA-256", buffer));
    this.check(generation);
    let binary = "";
    for (const byte of bytes) binary += String.fromCharCode(byte);
    const chunk: Chunk = {
      type: "chunk", protocol_version: 1, session_id: this.sessionId, capture_epoch: this.epoch,
      channel_id: "doctor_mic", seq: this.seq++, sample_start: this.sampleEnd,
      sample_count: bytes.length / 2, sample_rate: 16000, encoding: "pcm_s16le",
      sha256: [...hash].map((v) => v.toString(16).padStart(2, "0")).join(""), data: btoa(binary),
    };
    this.sampleEnd += chunk.sample_count;
    this.pending.set(chunk.seq, chunk);
    if (this.socket?.readyState === WebSocket.OPEN) this.send(this.socket, chunk);
    this.publish({});
    if (this.pending.size >= 30) { this.pause(); this.publish({ message: "待确认缓冲已达上限，录音已暂停。请恢复连接后继续。" }); }
  }
  pause() {
    if (this.disposed) return;
    this.node?.port.postMessage("pause");
    this.publish({ state: "PAUSED", level: 0 });
  }
  async resume() {
    const generation = this.generation;
    this.check(generation);
    this.checkInput();
    if (!this.context || !this.node) throw new Error("采音设备尚未就绪");
    if (this.socket?.readyState !== WebSocket.OPEN) await this.connect();
    this.check(generation);
    if (this.pending.size >= 30) throw new Error("音频缓冲尚未确认，暂不能继续录音。");
    await this.context.resume();
    this.check(generation);
    this.checkInput();
    this.node.port.postMessage("resume");
    this.publish({ state: "RECORDING", message: "" });
  }
  private async flush() {
    if (!this.node) return;
    const requestId = crypto.randomUUID();
    await new Promise<void>((resolve, reject) => {
      const timer = window.setTimeout(() => { this.flushWaiters.delete(requestId); reject(new Error("音频尾段尚未完成输出，请重试结束录音。")); }, 5000);
      this.flushWaiters.set(requestId, { resolve: () => { clearTimeout(timer); resolve(); }, reject: (error) => { clearTimeout(timer); reject(error); } });
      this.node!.port.postMessage({ type: "stop", request_id: requestId });
    });
  }
  async stop(): Promise<{ channels: CaptureEnd[]; pending_chunks: number }> {
    const generation = this.generation;
    this.check(generation);
    this.checkInput();
    this.publish({ state: "FINALIZING", level: 0 });
    await this.flush();
    this.check(generation);
    await this.serial;
    this.check(generation);
    if (this.captureFailure) throw new Error(`最终音频未能完整处理，不能提交结束声明：${this.captureFailure.message}`);
    if (this.pending.size && this.socket?.readyState !== WebSocket.OPEN) await this.connect();
    this.check(generation);
    const deadline = Date.now() + 6000;
    while (this.pending.size && Date.now() < deadline) { await new Promise((resolve) => setTimeout(resolve, 100)); this.check(generation); }
    const result = { channels: [{ channel_id: "doctor_mic", capture_epoch: this.epoch, last_seq: this.seq - 1, sample_end: this.sampleEnd }], pending_chunks: this.pending.size };
    if (!this.pending.size) await this.dispose();
    return result;
  }
  async dispose() {
    if (this.disposed) return;
    this.publish({ state: "STOPPED", level: 0 });
    this.disposed = true;
    this.generation += 1;
    this.flushWaiters.forEach((waiter) => waiter.reject(new DOMException("Capture cancelled", "AbortError")));
    this.flushWaiters.clear();
    this.stream?.getTracks().forEach((track) => track.stop());
    if (this.node) { this.node.port.onmessage = null; this.node.disconnect(); }
    this.socket?.close();
    await this.context?.close().catch(() => undefined);
  }
}
