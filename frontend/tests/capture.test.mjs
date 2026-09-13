import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { webcrypto } from "node:crypto";
import test from "node:test";
import vm from "node:vm";
import ts from "typescript";

const captureSource = ts.transpileModule(readFileSync(new URL("../src/capture.ts", import.meta.url), "utf8"), {
  compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS },
}).outputText;

async function until(predicate) {
  for (let attempt = 0; attempt < 100; attempt++) {
    if (predicate()) return;
    await new Promise(resolve => setTimeout(resolve, 5));
  }
  assert.fail("Expected capture state was not reached");
}

function harness({ deferredMicrophone = false, digestFailure = false } = {}) {
  const sockets = [], nodes = [], states = [];
  let trackStops = 0, contextCloses = 0, releaseMicrophone;
  const trackEvents = new Map();
  const track = {
    readyState: "live", muted: false,
    stop() { trackStops++; this.readyState = "ended"; },
    addEventListener(type, listener) { trackEvents.set(type, listener); },
    emit(type) { if (type === "ended") this.readyState = "ended"; if (type === "mute") this.muted = true; trackEvents.get(type)?.(); },
  };
  const stream = { getTracks: () => [track], getAudioTracks: () => [track] };
  class MockSocket {
    static OPEN = 1;
    readyState = 0;
    chunks = [];
    constructor() {
      sockets.push(this);
      queueMicrotask(() => { if (this.readyState !== 3) { this.readyState = 1; this.onopen?.(); } });
    }
    send(message) { this.chunks.push(JSON.parse(message)); }
    close() { this.readyState = 3; this.onclose?.(); }
    ack(chunk, fields = {}) {
      this.onmessage({ data: JSON.stringify({ type: "ACK_DURABLE", session_id: chunk.session_id, channel_id: chunk.channel_id, capture_epoch: chunk.capture_epoch, seq: chunk.seq, sha256: chunk.sha256, contiguous_seq: chunk.seq, ...fields }) });
    }
  }
  class MockNode {
    port = { sent: [], postMessage: message => this.port.sent.push(message), onmessage: null };
    constructor() { nodes.push(this); }
    connect(target) { return target; }
    disconnect() {}
    pcm(samples) { this.port.onmessage({ data: { type: "pcm", pcm: new Int16Array(samples).buffer } }); }
    finish() {
      const stop = this.port.sent.findLast(message => message?.type === "stop");
      assert.ok(stop, "The worklet must receive a stop request");
      this.port.onmessage({ data: { type: "flushed", request_id: stop.request_id } });
    }
  }
  class MockContext {
    sampleRate = 16000;
    audioWorklet = { addModule: async () => {} };
    createMediaStreamSource() { return { connect() {} }; }
    createGain() { return { gain: {}, connect() {} }; }
    async resume() {}
    async close() { contextCloses++; }
  }
  const exports = {};
  vm.runInNewContext(captureSource, {
    exports, require: () => ({ post: async () => ({ ticket: "synthetic-ticket" }) }),
    navigator: { mediaDevices: { getUserMedia: () => deferredMicrophone ? new Promise(resolve => { releaseMicrophone = () => resolve(stream); }) : Promise.resolve(stream) } },
    AudioContext: MockContext, AudioWorkletNode: MockNode, WebSocket: MockSocket,
    window: { setTimeout }, setTimeout, clearTimeout, DOMException,
    crypto: digestFailure ? { randomUUID: () => webcrypto.randomUUID(), subtle: { digest: async () => { throw new Error("digest unavailable"); } } } : webcrypto,
    location: { protocol: "http:", host: "localhost" }, btoa,
  });
  const capture = new exports.BrowserCapture("session-a", state => states.push(state));
  return { capture, sockets, nodes, states, track, get trackStops() { return trackStops; }, get contextCloses() { return contextCloses; }, releaseMicrophone: () => releaseMicrophone() };
}

test("disposing a pending microphone request stops the late stream without opening an upload", async () => {
  const h = harness({ deferredMicrophone: true });
  const start = h.capture.start();
  await h.capture.dispose();
  h.releaseMicrophone();
  await assert.rejects(start, error => error.name === "AbortError");
  assert.equal(h.trackStops, 1);
  assert.equal(h.sockets.length, 0);
  assert.equal(h.states.at(-1).state, "STOPPED");
});

test("only an exact chunk ACK releases audio; a waterline cannot release other chunks", async () => {
  const h = harness();
  await h.capture.start();
  h.nodes[0].pcm([1, 2]);
  h.nodes[0].pcm([3, 4]);
  await until(() => h.sockets[0].chunks.length === 2);
  const [first, second] = h.sockets[0].chunks;
  h.sockets[0].ack(first, { sha256: "incorrect" });
  assert.equal(h.states.at(-1).pending, 2);
  assert.equal(h.states.at(-1).state, "ERROR");
  h.sockets[0].ack(second, { contiguous_seq: 1 });
  assert.equal(h.states.at(-1).pending, 1);
  assert.equal(h.states.at(-1).durable, 1);
  h.sockets[0].ack(second);
  assert.equal(h.states.at(-1).durable, 1);
  h.sockets[0].ack(first);
  assert.equal(h.states.at(-1).pending, 0);
  await h.capture.dispose();
});

test("stop waits for the worklet tail, hashing, and its durable ACK before finalizing", async () => {
  const h = harness();
  await h.capture.start();
  let completed = false;
  const resultPromise = h.capture.stop().then(result => { completed = true; return result; });
  await new Promise(resolve => setTimeout(resolve, 10));
  assert.equal(completed, false);
  h.nodes[0].pcm([12, 34, 56]);
  h.nodes[0].finish();
  await until(() => h.sockets[0].chunks.length === 1);
  assert.equal(completed, false);
  h.sockets[0].ack(h.sockets[0].chunks[0]);
  const result = await resultPromise;
  assert.equal(result.pending_chunks, 0);
  assert.equal(result.channels[0].last_seq, 0);
  assert.equal(result.channels[0].sample_end, 3);
  assert.equal(h.trackStops, 1);
  assert.equal(h.contextCloses, 1);
});

test("failed tail processing blocks the final declaration", async () => {
  const h = harness({ digestFailure: true });
  await h.capture.start();
  const resultPromise = h.capture.stop();
  h.nodes[0].pcm([1, 2, 3]);
  h.nodes[0].finish();
  await assert.rejects(resultPromise, /digest unavailable/);
  assert.equal(h.sockets[0].chunks.length, 0);
  await h.capture.dispose();
});

test("a disconnected capture replays the original unacknowledged bytes before resuming", async () => {
  const h = harness();
  await h.capture.start();
  h.nodes[0].pcm([100, 200, 300]);
  await until(() => h.sockets[0].chunks.length === 1);
  const original = h.sockets[0].chunks[0];
  h.sockets[0].close();
  assert.equal(h.states.at(-1).state, "PAUSED");
  await h.capture.resume();
  assert.deepEqual(h.sockets[1].chunks[0], original);
  h.sockets[1].ack(original);
  assert.equal(h.states.at(-1).pending, 0);
  await h.capture.dispose();
});

for (const type of ["ended", "mute"]) test(`microphone ${type} blocks resume and a complete final declaration`, async () => {
  const h = harness();
  await h.capture.start();
  h.nodes[0].pcm([100, 200]);
  await until(() => h.sockets[0].chunks.length === 1);
  h.track.emit(type);
  assert.equal(h.states.at(-1).state, "ERROR");
  assert.equal(h.states.at(-1).pending, 1, "Unacknowledged source bytes must remain retained");
  h.track.muted = false;
  await assert.rejects(h.capture.resume(), /麦克风/);
  assert.equal(h.states.at(-1).state, "ERROR");
  await assert.rejects(h.capture.stop(), /麦克风/);
  assert.equal(h.states.at(-1).pending, 1);
  await h.capture.dispose();
});

test("a device that ends without an event is rechecked before resume", async () => {
  const h = harness();
  await h.capture.start();
  h.capture.pause();
  h.track.readyState = "ended";
  await assert.rejects(h.capture.resume(), /麦克风不可用/);
  assert.equal(h.states.at(-1).state, "ERROR");
  await h.capture.dispose();
});

test("the worklet emits the final PCM before its flush acknowledgement and stops collecting", () => {
  let Processor;
  vm.runInNewContext(readFileSync(new URL("../public/capture-worklet.js", import.meta.url), "utf8"), {
    AudioWorkletProcessor: class { port = { messages: [], postMessage(message) { this.messages.push(message); } }; },
    registerProcessor: (_name, implementation) => { Processor = implementation; },
  });
  const processor = new Processor();
  processor.process([[new Float32Array([0.5, -0.5])]]);
  processor.port.onmessage({ data: { type: "stop", request_id: "tail" } });
  const pcm = processor.port.messages.find(message => message.type === "pcm");
  assert.equal(pcm.pcm.byteLength, 4);
  assert.equal(processor.port.messages.at(-1).type, "flushed");
  const count = processor.port.messages.length;
  processor.process([[new Float32Array([1, 1])]]);
  assert.equal(processor.port.messages.length, count);
});
