import assert from "node:assert/strict";
import { mkdir } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import { chromium } from "playwright";
import { preview } from "vite";

const output = fileURLToPath(new URL("../../artifacts/pages/", import.meta.url));
await mkdir(output, { recursive: true });
let server;
let browser;
try {
  if (!process.env.HIS_PAGES_URL) {
    server = await preview({
      mode: "pages",
      preview: { host: "127.0.0.1", port: 4173, strictPort: false },
    });
  }
  const baseURL = process.env.HIS_PAGES_URL || server.resolvedUrls.local[0];
  browser = await chromium.launch({ headless: true });
  for (const viewport of [{ width: 1440, height: 1000 }, { width: 390, height: 844 }]) {
    const savedToken = viewport.width < 600 ? "pages-test-existing-token" : null;
    const context = await browser.newContext({ viewport });
    await context.addInitScript(({ savedToken }) => {
      if (savedToken && location.pathname.startsWith("/HIS-/")) sessionStorage.setItem("his_access_token", savedToken);
      window.__mediaAttempts = 0;
      Object.defineProperty(navigator, "mediaDevices", {
        value: {
          getUserMedia: async () => { window.__mediaAttempts++; throw new Error("Demo must not capture audio"); },
          enumerateDevices: async () => { window.__mediaAttempts++; return []; },
          addEventListener() {},
          removeEventListener() {},
        },
      });
    }, { savedToken });
    const page = await context.newPage();
    const errors = [];
    const requests = [];
    page.on("pageerror", (error) => errors.push(error.message));
    page.on("request", (request) => {
      requests.push(request.url());
      if (request.headers().authorization) errors.push(`Unexpected authorization header: ${request.url()}`);
    });
    page.on("response", (response) => {
      if (response.status() >= 400) errors.push(`${response.status()}: ${response.url()}`);
    });
    page.on("requestfailed", (request) => errors.push(`Request failed: ${request.url()}`));
    page.on("websocket", (socket) => errors.push(`Unexpected WebSocket: ${socket.url()}`));
    await page.goto(baseURL);
    const complaint = page.getByRole("textbox", { name: "主诉", exact: true });
    await complaint.waitFor();
    assert.ok(await complaint.inputValue(), "A synthetic note must load without a backend");
    assert.equal(await complaint.getAttribute("readonly"), "", "Clinical text must be read-only");
    assert.equal(await page.getByLabel("密码", { exact: true }).count(), 0, "Public demo must not collect credentials");
    assert.equal(await page.getByRole("button", { name: "保存", exact: true }).isDisabled(), true);
    assert.equal(await page.getByRole("button", { name: "新建录音会话", exact: true }).isDisabled(), true);
    assert.ok((await page.locator("body").innerText()).includes("公开演示"));
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1), true, "Viewport must not overflow horizontally");
    const mode = viewport.width > 600 ? "desktop" : "mobile";
    await page.screenshot({ path: `${output}/${mode}.png`, fullPage: true });
    await page.screenshot({ path: `${output}/${mode}-viewport.png` });

    const navigator = page.getByLabel("定位文书章节", { exact: true });
    const lastKey = await navigator.locator("option").last().getAttribute("value");
    await navigator.selectOption(lastKey);
    await page.waitForFunction((key) => document.querySelector('[aria-label="定位文书章节"]').value === key, lastKey);
    assert.equal(await navigator.inputValue(), lastKey);
    await page.getByRole("button", { name: "文书版本记录", exact: true }).click();
    await page.getByRole("dialog").waitFor();
    assert.ok(await page.locator(".version-row").count());
    await page.keyboard.press("Escape");

    await page.getByRole("tab", { name: /^事实/ }).click();
    await page.locator(".fact-item").first().waitFor();
    await page.getByRole("button", { name: "院内资料", exact: true }).click();
    await page.locator(".source-table .table-link").first().click();
    await page.getByRole("dialog").waitFor();
    await page.keyboard.press("Escape");
    assert.equal(await page.getByRole("button", { name: "同步院内资料", exact: true }).isDisabled(), true);
    for (const name of ["处理任务", "审计记录", "系统设置"]) {
      await page.getByRole("button", { name, exact: true }).click();
      await page.locator(".page-title h2").waitFor();
    }
    await page.getByRole("button", { name: "病历工作台", exact: true }).click();
    await complaint.waitFor();
    if (viewport.width < 600) {
      await page.getByRole("button", { name: "展开患者列表", exact: true }).click();
      const topbar = await page.locator(".topbar").boundingBox();
      const drawer = await page.locator(".sidebar.mobile-open").boundingBox();
      assert.ok(drawer.y >= topbar.y + topbar.height - 1, "Patient drawer must not overlap the header");
      assert.ok(drawer.y + drawer.height <= viewport.height + 1, "Patient drawer must fit the viewport");
      await page.screenshot({ path: `${output}/mobile-patients.png` });
      await page.setViewportSize({ width: 320, height: 640 });
      await page.waitForFunction(() => {
        const header = document.querySelector(".topbar").getBoundingClientRect();
        const drawer = document.querySelector(".sidebar.mobile-open").getBoundingClientRect();
        return drawer.top >= header.bottom - 1 && drawer.bottom <= innerHeight + 1;
      });
      await page.setViewportSize(viewport);
    }
    const patients = page.locator(".patient-row");
    assert.ok(await patients.count() >= 2, "Demo must allow patient switching");
    const nextPatient = await patients.nth(1).locator("strong").innerText();
    await patients.nth(1).click();
    await page.getByRole("heading", { name: nextPatient, exact: true }).waitFor();
    await complaint.waitFor();
    assert.equal(await page.evaluate(() => window.__mediaAttempts), 0);
    assert.equal(await page.evaluate(() => sessionStorage.getItem("his_access_token")), savedToken, "Demo must not alter existing credentials");
    await page.reload();
    await complaint.waitFor();
    assert.equal(new URL(page.url()).pathname, "/HIS-/", "Reload must retain repository base path");
    assert.equal(await page.evaluate(() => window.__mediaAttempts), 0);
    assert.equal(await page.evaluate(() => sessionStorage.getItem("his_access_token")), savedToken);
    assert.deepEqual(requests.filter((url) => {
      const request = new URL(url);
      return request.origin !== new URL(baseURL).origin || !request.pathname.startsWith("/HIS-/");
    }), [], "Public demo must only request its own static assets");
    assert.deepEqual(errors, []);
    console.log(`PASS ${mode}: static assets, note navigation, read-only controls, sources, pages, patient switch, reload, no backend or microphone`);
    await context.close();
  }
} finally {
  await browser?.close();
  if (server) {
    server.httpServer.closeAllConnections();
    await new Promise((resolve, reject) => server.httpServer.close((error) => error ? reject(error) : resolve()));
  }
}
