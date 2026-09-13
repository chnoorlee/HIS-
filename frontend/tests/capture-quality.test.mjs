import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import vm from "node:vm";
import ts from "typescript";

const compiled = ts.transpileModule(readFileSync(new URL("../src/capture-quality.ts", import.meta.url), "utf8"), {
  compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS },
}).outputText;
const exports = {};
vm.runInNewContext(compiled, { exports });
const { updateQuality, qualityStatus } = exports;

test("a healthy doctor channel cannot conceal a persistently quiet patient channel", () => {
  const readings = {};
  for (let at = 0; at <= 8000; at += 1000) {
    readings.patient = updateQuality(readings.patient, { rms: 0, clipping_fraction: 0 }, at);
    readings.doctor = updateQuality(readings.doctor, { rms: 0.3, clipping_fraction: 0 }, at);
  }
  assert.equal(qualityStatus(readings.patient, 0, 8000).kind, "warn");
  assert.equal(qualityStatus(readings.doctor, 0, 8000).kind, "normal");
  readings.patient = updateQuality(readings.patient, { rms: 0.1, clipping_fraction: 0 }, 9000);
  assert.equal(qualityStatus(readings.patient, 0, 9000).kind, "normal");
});

test("clipping requires a continuous window and a missing update cannot look healthy", () => {
  let reading;
  for (let at = 0; at <= 2000; at += 1000) reading = updateQuality(reading, { rms: 0.8, clipping_fraction: 0.04 }, at);
  assert.equal(qualityStatus(reading, 0, 2000).kind, "bad");
  assert.equal(qualityStatus(reading, 0, 7001).kind, "warn");
  reading = updateQuality(reading, { rms: 0.8, clipping_fraction: 0.04 }, 8000);
  assert.equal(qualityStatus(reading, 0, 8000).kind, "normal");
});

test("malformed measurements do not refresh the last valid signal", () => {
  const original = updateQuality(undefined, { rms: 0.2, clipping_fraction: 0 }, 0);
  for (const payload of [{ rms: NaN, clipping_fraction: 0 }, { rms: 1.1, clipping_fraction: 0 }, { rms: 0.2, clipping_fraction: "0" }]) {
    assert.equal(updateQuality(original, payload, 9000), original);
  }
  assert.equal(qualityStatus(original, 0, 9000).kind, "warn");
  assert.equal(qualityStatus(undefined, 0, 4001).kind, "warn");
});

test("elapsed wall time without new samples cannot establish sustained clipping or silence", () => {
  const clipped = updateQuality(undefined, { rms: 0.8, clipping_fraction: 0.04 }, 0);
  assert.equal(qualityStatus(clipped, 0, 2000).kind, "normal");
  let quiet;
  for (let at = 0; at <= 6000; at += 1000) quiet = updateQuality(quiet, { rms: 0, clipping_fraction: 0 }, at);
  assert.equal(qualityStatus(quiet, 0, 8000).kind, "muted");
  assert.equal(qualityStatus(quiet, 0, 11000).kind, "warn");
});
