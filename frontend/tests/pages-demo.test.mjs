import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import vm from "node:vm";
import ts from "typescript";

const compiled = ts.transpileModule(readFileSync(new URL("../src/demo.ts", import.meta.url), "utf8"), {
  compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS },
}).outputText;
const exports = {};
vm.runInNewContext(compiled, { exports, Response, URLSearchParams });
const { demoResponse } = exports;
const read = (path) => demoResponse(path).json();

test("public demo rejects mutations without modifying its authored data", async () => {
  const before = await read("/notes?encounter_id=demo-enc-001");
  for (const method of ["POST", "PATCH", "PUT", "DELETE", "patch"]) {
    const result = demoResponse(`/notes/${before[0].id}`, { method, body: JSON.stringify({ text: "changed" }) });
    assert.equal(result.status, 403);
    assert.equal((await result.json()).detail.code, "public_demo_read_only");
  }
  assert.deepEqual(await read("/notes?encounter_id=demo-enc-001"), before);
  const copy = await read(`/notes/${before[0].id}`);
  copy.blocks[0].text = "changed by consumer";
  assert.notEqual((await read(`/notes/${before[0].id}`)).blocks[0].text, copy.blocks[0].text);
});

test("public demo scopes clinical data to the selected encounter and session", async () => {
  const encounters = await read("/encounters");
  assert.ok(encounters.length > 1);
  const ids = new Set();
  for (const encounter of encounters) {
    const notes = await read(`/notes?encounter_id=${encounter.id}`);
    const sessions = await read(`/sessions?encounter_id=${encounter.id}`);
    assert.ok(notes.length && sessions.length);
    for (const note of notes) {
      assert.equal(note.encounter_id, encounter.id);
      assert.equal(ids.has(note.id), false);
      ids.add(note.id);
      const facts = await read(`/sessions/${note.session_id}/facts`);
      for (const block of note.blocks) {
        for (const id of block.fact_ids) assert.ok(facts.some((fact) => fact.id === id));
      }
    }
  }
  assert.deepEqual(await read("/notes?encounter_id=unknown"), []);
  assert.deepEqual(await read("/sessions/unknown/facts"), []);
});

test("public demo exposes no operational provider and fails closed for unavailable routes", async () => {
  const status = await read("/system/status");
  assert.equal(status.synthetic, true);
  assert.equal(status.fact_extraction_ready, false);
  assert.equal(demoResponse("/sessions/demo-session-001/events").status, 404);
  assert.equal(demoResponse("/sessions/demo-session-001/playback").status, 404);
  const settings = await read("/admin/settings");
  assert.equal(settings.export_enabled, false);
});
