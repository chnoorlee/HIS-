import assert from "node:assert/strict";
import path from "node:path";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
let playwright;
try { playwright = require("playwright"); }
catch { playwright = require(path.join(process.env.USERPROFILE, ".cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright")); }
const baseURL = process.env.HIS_E2E_URL || "http://127.0.0.1:5173";
const browser = await playwright.chromium.launch({ channel: process.env.HIS_E2E_CHANNEL || "msedge", headless: true, args: ["--use-fake-device-for-media-stream", "--use-fake-ui-for-media-stream"] });
const errors = [];
async function login(page) {
  page.on("pageerror", error => errors.push(error.message));
  page.on("dialog", dialog => dialog.accept());
  await page.goto(baseURL);
  await page.getByLabel("院内账号").fill("doctor");
  await page.getByLabel("密码", { exact: true }).fill("Doctor123!");
  await page.getByRole("button", { name: "进入工作台" }).click();
  await page.getByRole("button", { name: /李明/ }).waitFor();
}
async function createSession(page) {
  await page.getByRole("button", { name: "新建录音会话" }).click();
  const createdResponse = page.waitForResponse(response => response.url().endsWith("/api/v1/sessions") && response.request().method() === "POST");
  await page.getByRole("button", { name: "建立会话" }).click();
  const created = await (await createdResponse).json();
  await page.getByRole("dialog").waitFor({ state: "hidden" });
  assert.equal(await page.getByLabel("当前录音会话").inputValue(), created.id, "The selected option must immediately identify the created session");
}
async function createAdmission(page) {
  await page.getByRole("tab", { name: "入院记录", exact: true }).click();
  const response = page.waitForResponse(value => value.url().endsWith("/api/v1/notes") && value.request().method() === "POST");
  await page.getByRole("button", { name: "新建入院记录", exact: true }).first().click();
  const note = await (await response).json();
  await page.waitForFunction(id => document.querySelector('select[aria-label="文书记录"]').value === id, note.id);
}
try {
  const context = await browser.newContext({ permissions: ["microphone"], viewport: { width: 1440, height: 1000 } });
  const page = await context.newPage();
  await login(page);
  await createSession(page);
  await createAdmission(page);
  await page.getByRole("textbox", { name: "主诉", exact: true }).waitFor();
  const inputs = page.locator(".document-block textarea");
  assert.equal(await inputs.count(), 14);
  for (const input of await inputs.all()) await input.fill("虚构回归记录：" + await input.getAttribute("aria-label"));
  await page.getByRole("button", { name: "刷新当前就诊" }).click();
  assert.equal(await page.getByRole("textbox", { name: "主诉", exact: true }).inputValue(), "虚构回归记录：主诉");
  assert.equal(await page.getByRole("button", { name: "保存", exact: true }).isEnabled(), true);
  await page.getByRole("button", { name: "保存", exact: true }).click();
  await page.getByText(/版本 v\d+ 已保存/).waitFor();
  console.log("PASS all admission fields survive refresh and save explicitly");

  const token = await page.evaluate(() => sessionStorage.getItem("his_access_token"));
  const noteId = await page.getByLabel("文书记录", { exact: true }).inputValue();
  const headers = { Authorization: `Bearer ${token}` };
  const saved = await (await context.request.get(`${baseURL}/api/v1/notes/${noteId}`, { headers })).json();
  assert.ok(saved.blocks.every(block => block.text === "虚构回归记录：" + block.title));
  await page.getByRole("button", { name: "审核", exact: true }).click();
  await page.getByRole("dialog").waitFor();
  await page.keyboard.press("Shift+Tab");
  assert.equal(await page.getByRole("button", { name: /确认审核 v/ }).evaluate(node => node === document.activeElement), true);
  await page.keyboard.press("Escape");
  await page.getByRole("dialog").waitFor({ state: "hidden" });
  console.log("PASS modal initial reverse tab is contained");

  await page.getByRole("textbox", { name: "主诉", exact: true }).fill("保留本地未保存主诉");
  const changed = await context.request.patch(`${baseURL}/api/v1/notes/${noteId}`, { headers, data: { base_revision: saved.revision, blocks: saved.blocks.map(block => ({ key: block.key, title: block.title, text: "远端更新：" + block.title, fact_ids: block.fact_ids })) } });
  assert.equal(changed.status(), 200);
  await page.getByRole("button", { name: "刷新当前就诊" }).click();
  await page.getByText("记录已被更新，当前编辑仍保留。", { exact: false }).waitFor();
  assert.equal(await page.getByRole("textbox", { name: "主诉", exact: true }).inputValue(), "保留本地未保存主诉");
  await page.getByRole("button", { name: "重新载入服务端版本" }).click();
  await page.getByText(/版本 v\d+ 已保存/).waitFor();
  assert.equal(await page.getByRole("textbox", { name: "主诉", exact: true }).inputValue(), "远端更新：主诉");
  console.log("PASS concurrent revision preserves local edits until explicit reload");

  let releaseSave;
  const held = new Promise(resolve => { releaseSave = resolve; });
  let arrived;
  const saveArrived = new Promise(resolve => { arrived = resolve; });
  await page.route(`**/api/v1/notes/${noteId}`, async route => {
    if (route.request().method() !== "PATCH") return route.continue();
    const response = await route.fetch();
    arrived();
    await held;
    await route.fulfill({ response });
  });
  await page.getByRole("textbox", { name: "主诉", exact: true }).fill("延迟回包的旧患者主诉");
  await page.getByRole("button", { name: "保存", exact: true }).click();
  await saveArrived;
  await page.getByRole("button", { name: /陈芳/ }).click();
  await page.getByText("读取就诊来源与文书…").waitFor({ state: "hidden" });
  await createSession(page);
  await createAdmission(page);
  await page.getByRole("textbox", { name: "主诉", exact: true }).fill("新患者的未保存主诉");
  const newNoteId = await page.getByLabel("文书记录", { exact: true }).inputValue();
  assert.notEqual(newNoteId, noteId);
  releaseSave();
  await page.waitForResponse(response => response.url().endsWith(`/notes/${noteId}`) && response.request().method() === "PATCH");
  await page.getByRole("button", { name: "刷新当前就诊" }).click();
  assert.equal(await page.getByLabel("文书记录", { exact: true }).inputValue(), newNoteId);
  assert.equal(await page.getByRole("textbox", { name: "主诉", exact: true }).inputValue(), "新患者的未保存主诉");
  assert.equal(await page.getByRole("button", { name: "保存", exact: true }).isEnabled(), true);
  console.log("PASS delayed previous-patient save cannot select a note or clear new edits");

  await createSession(page);
  const abandonedSession = await page.getByLabel("当前录音会话").inputValue();
  const paused = await context.request.post(`${baseURL}/api/v1/sessions/${abandonedSession}/pause`, { headers, data: {} });
  assert.equal(paused.status(), 200);
  await page.reload();
  await page.getByRole("button", { name: /陈芳/ }).waitFor();
  await page.getByRole("button", { name: /陈芳/ }).click();
  await page.getByText("当前页面未保留此会话的原始结束边界，不能继续录音或声明音频完整。请新建会话；如有原生缓存，请在 Windows 客户端恢复并核查。").waitFor();
  assert.equal(await page.getByRole("button", { name: "开始录音", exact: true }).isDisabled(), true);
  console.log("PASS browser reload refuses recording without the original final boundary");

  await createSession(page);
  await page.getByRole("button", { name: "开始录音", exact: true }).click();
  await page.getByText(/^[1-9]\d* 片已持久化确认$/).waitFor();
  let archiveComplete = false;
  await page.route("**/api/v1/sessions/*/finalize", async route => {
    const response = await route.fetch();
    assert.equal(response.status(), 200);
    assert.equal((await response.json()).status, "COMPLETE");
    archiveComplete = true;
    await route.fulfill({ response });
  });
  await page.route("**/api/v1/sessions?*", route => archiveComplete
    ? route.fulfill({ status: 503, contentType: "application/json", body: JSON.stringify({ detail: "synthetic_refresh_failure" }) })
    : route.continue());
  await page.getByRole("button", { name: "结束", exact: true }).click();
  await page.getByText("synthetic_refresh_failure").waitFor();
  assert.equal(await page.getByRole("button", { name: "新建录音会话" }).isEnabled(), true);
  await page.unroute("**/api/v1/sessions?*");
  await page.getByRole("button", { name: /李明/ }).click();
  assert.equal(await page.getByRole("button", { name: /李明/ }).evaluate(node => node.classList.contains("selected")), true);
  console.log("PASS successful real audio finalization releases the patient lock when refresh fails");

  await page.route("**/api/v1/sessions?*", route => route.fulfill({ status: 401, contentType: "application/json", body: JSON.stringify({ detail: "expired" }) }));
  await page.getByRole("button", { name: "刷新当前就诊" }).click();
  await page.getByLabel("院内账号").waitFor();
  assert.equal(await page.locator(".document-block,.patient-row,.audio-player").count(), 0);
  assert.equal(await page.evaluate(() => sessionStorage.getItem("his_access_token")), null);
  console.log("PASS expired identity clears all visible clinical context");
  await context.close();

  const nativeContext = await browser.newContext();
  await nativeContext.addInitScript(() => {
    const listeners = new Set();
    window.__emitNative = (type, payload) => listeners.forEach(listener => listener({ data: { type, payload } }));
    const originalTimeout = window.setTimeout.bind(window);
    window.setTimeout = (callback, timeout, ...args) => {
      const timer = originalTimeout(callback, timeout === 30000 ? (window.__holdRecovery ? 60000 : 100) : timeout, ...args);
      if (timeout === 30000 && window.__holdRecovery && !window.__expireHeldRecovery)
        window.__expireHeldRecovery = () => { clearTimeout(timer); callback(...args); };
      return timer;
    };
    window.__nativePauses = 0;
    window.chrome ??= {};
    window.chrome.webview = {
      addEventListener: (_type, listener) => listeners.add(listener),
      removeEventListener: (_type, listener) => listeners.delete(listener),
      postMessage: message => {
        if (message.payload?.api_base && message.payload.api_base !== "http://127.0.0.1:8787/api/v1") {
          setTimeout(() => listeners.forEach(listener => listener({ data: { type: "bridge.error", request_id: message.request_id, payload: { message: "The native API endpoint is fixed by hospital configuration." } } })), 0);
          return;
        }
        if (message.type === "capture.pause") window.__nativePauses++;
        if (message.type === "capture.recover" && window.__holdRecovery) return;
        if (message.type === "capture.start" && window.__nativeTimeout) return;
        if (message.type === "capture.start" && window.__nativeRejected) {
          setTimeout(() => listeners.forEach(listener => listener({ data: { type: "bridge.error", request_id: message.request_id, payload: { message: "Synthetic partial device startup failure" } } })), 0);
          return;
        }
        const payload = message.type === "devices.list"
          ? { devices: [{ id: "doctor", name: "Synthetic device", state: "active", is_default: true }] }
          : { channels: [{ channel_id: "doctor_mic", capture_epoch: 123, last_seq: 0, sample_end: 1 }], pending_chunks: 0, pending_gaps: window.__nativeBlocked ? 0 : 1, finalization_blocked: window.__nativeBlocked === true, reason: "recovery_requires_review" };
        setTimeout(() => listeners.forEach(listener => listener({ data: { type: "bridge.result", request_id: message.request_id, payload } })), 0);
      },
    };
  });
  const nativePage = await nativeContext.newPage();
  await login(nativePage);
  await createSession(nativePage);
  let declarations = 0;
  nativePage.on("request", request => { if (request.url().endsWith("/finalize")) declarations++; });
  await nativePage.getByRole("button", { name: "恢复本会话本地音频" }).click();
  await nativePage.getByText("本地音频与服务端清单尚未核实，请保留缓存并交管理员核查。").waitFor();
  await nativePage.getByRole("button", { name: "重试归档", exact: true }).click();
  await nativePage.getByText("存在未确认音频，尚未提交完整结束声明。").waitFor();
  assert.equal(declarations, 0);
  console.log("PASS native recovery with an unresolved gap cannot finalize");
  await nativePage.reload();
  await nativePage.getByRole("button", { name: /李明/ }).waitFor();
  await nativePage.evaluate(() => { window.__nativeTimeout = true; });
  await nativePage.getByRole("button", { name: "开始录音", exact: true }).click();
  await nativePage.getByText("桌面采音结果尚未确认，当前患者保持锁定。请重试归档或隔离会话。").waitFor();
  await nativePage.getByRole("button", { name: /陈芳/ }).click();
  await nativePage.getByText("录音会话已固定当前就诊，请先结束录音再切换患者。").waitFor();
  assert.equal(await nativePage.getByRole("button", { name: /李明/ }).evaluate(node => node.classList.contains("selected")), true);
  console.log("PASS uncertain native start keeps the patient lock after bridge timeout");
  await nativePage.reload();
  await nativePage.getByRole("button", { name: /李明/ }).waitFor();
  await nativePage.evaluate(() => { window.__nativeRejected = true; });
  await nativePage.getByRole("button", { name: "开始录音", exact: true }).click();
  await nativePage.getByText("Synthetic partial device startup failure").waitFor();
  assert.equal(await nativePage.getByLabel("当前录音会话").isDisabled(), true);
  assert.equal(await nativePage.getByRole("button", { name: "重试归档", exact: true }).isEnabled(), true);
  console.log("PASS rejected native startup preserves the capture lock for retained audio");
  await nativePage.reload();
  await nativePage.getByRole("button", { name: /李明/ }).waitFor();
  await nativePage.evaluate(() => { window.__nativeBlocked = true; });
  await nativePage.getByRole("button", { name: "恢复本会话本地音频" }).click();
  await nativePage.getByText("最终音频未能保存，请核查本地存储后处理。").waitFor();
  assert.equal(await nativePage.getByLabel("当前录音会话").isDisabled(), true);
  await nativePage.getByRole("button", { name: "重试归档", exact: true }).click();
  await nativePage.getByLabel("采音会话").getByText("最终音频未能保存，不能提交结束声明。请核查本地存储。").waitFor();
  assert.equal(declarations, 0);
  console.log("PASS blocked final audio recovery remains locked and cannot finalize");
  await nativePage.reload();
  await nativePage.getByRole("button", { name: /李明/ }).waitFor();
  await nativePage.evaluate(() => { window.__holdRecovery = true; });
  await nativePage.getByRole("button", { name: "恢复本会话本地音频" }).click();
  await nativePage.evaluate(() => window.dispatchEvent(new Event("his-auth-expired")));
  await nativePage.getByLabel("院内账号").fill("doctor");
  await nativePage.getByLabel("密码", { exact: true }).fill("Doctor123!");
  await nativePage.getByRole("button", { name: "进入工作台" }).click();
  await nativePage.getByRole("button", { name: /李明/ }).waitFor();
  await nativePage.getByRole("button", { name: "开始录音", exact: true }).click();
  await nativePage.getByText("正在采集", { exact: true }).waitFor();
  const pausesBeforeTimeout = await nativePage.evaluate(() => window.__nativePauses);
  await nativePage.evaluate(() => window.__expireHeldRecovery());
  await nativePage.getByRole("button", { name: /陈芳/ }).click();
  await nativePage.getByText("录音会话已固定当前就诊，请先结束录音再切换患者。").waitFor();
  assert.equal(await nativePage.evaluate(() => window.__nativePauses), pausesBeforeTimeout);
  assert.equal(await nativePage.getByText("正在采集", { exact: true }).isVisible(), true);
  console.log("PASS expired-identity recovery timeout cannot pause or unlock a new recording");
  await nativePage.evaluate(() => window.__emitNative("capture.error", { code: "upload_disconnected", message: "Synthetic upload disconnection" }));
  await nativePage.getByText("上传连接中断，音频保留在本地加密缓存，正在重连。").waitFor();
  assert.equal(await nativePage.getByText("正在采集", { exact: true }).isVisible(), true);
  assert.equal(await nativePage.getByRole("button", { name: "暂停录音", exact: true }).isEnabled(), true);
  assert.equal(await nativePage.getByRole("button", { name: "新建录音会话", exact: true }).isDisabled(), true);
  console.log("PASS upload disconnection preserves the actual native sampling state and patient lock");
  await nativePage.evaluate(() => {
    window.__emitNative("capture.state", { state: "PAUSED", reason: "authorization_grace_expired" });
    window.__emitNative("capture.error", { code: "authorization_grace_expired", message: "Synthetic capture paused by host" });
  });
  await nativePage.getByText("采集已暂停", { exact: true }).waitFor();
  assert.equal(await nativePage.getByRole("button", { name: "恢复录音", exact: true }).isEnabled(), true);
  assert.equal(await nativePage.getByLabel("当前录音会话").isDisabled(), true);
  console.log("PASS native sampling pauses only when the host publishes the pause state");
  assert.deepEqual(errors, []);
} finally {
  await browser.close();
}
