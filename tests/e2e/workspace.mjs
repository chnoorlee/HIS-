import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
import { createRequire } from 'node:module';

const require = createRequire(import.meta.url);
let playwright;
try { playwright = require('playwright'); }
catch {
  playwright = require(path.join(process.env.USERPROFILE, '.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright'));
}
const baseURL = process.env.HIS_E2E_URL || 'http://127.0.0.1:5173';
const output = path.resolve('.local/e2e');
await fs.mkdir(output, { recursive: true });
const browser = await playwright.chromium.launch({
  channel: process.env.HIS_E2E_CHANNEL || 'msedge', headless: true,
  args: ['--use-fake-device-for-media-stream', '--use-fake-ui-for-media-stream'],
});
const context = await browser.newContext({ viewport: { width: 1440, height: 1000 }, permissions: ['microphone'] });
const page = await context.newPage();
const errors = [];
page.on('pageerror', error => errors.push(error.message));
page.on('dialog', dialog => dialog.accept());
const steps = [];
async function step(name, action) { await action(); steps.push(name); console.log('PASS ' + name); }
async function capture(name) { await page.screenshot({ path: path.join(output, name + '.png'), fullPage: true }); }
async function assertNoOverflow() {
  const size = await page.evaluate(() => ({ width: document.documentElement.scrollWidth, viewport: innerWidth }));
  assert.ok(size.width <= size.viewport + 1, JSON.stringify(size));
}
try {
  await step('Doctor login and encounter selection', async () => {
    await page.goto(baseURL);
    await page.getByLabel('院内账号').fill('doctor');
    await page.getByLabel('密码', { exact: true }).fill('Doctor123!');
    await page.getByRole('button', { name: '进入工作台' }).click();
    await page.getByRole('button', { name: /李明/ }).waitFor();
    await capture('desktop-initial');
  });
  await step('Create fixed-patient capture session', async () => {
    await page.getByRole('button', { name: '新建录音会话' }).click();
    await page.getByRole('button', { name: '建立会话' }).click();
    await page.getByRole('dialog').waitFor({ state: 'hidden' });
  });
  await step('Create and edit all admission sections', async () => {
    await page.getByRole('button', { name: '新建入院记录', exact: true }).first().click();
    await page.getByRole('textbox', { name: '主诉', exact: true }).waitFor();
    const inputs = page.locator('.document-block textarea');
    assert.ok(await inputs.count() >= 14, 'Admission must contain all 14 sections');
    for (const input of await inputs.all()) {
      const title = await input.getAttribute('aria-label');
      await input.fill('虚构系统测试：医生明确提供并核实的' + title + '。');
    }
    await page.getByRole('button', { name: '保存', exact: true }).click();
    await page.getByText(/版本 v\d+ 已保存/).waitFor();
    await capture('desktop-admission');
  });
  await step('Review exact saved note revision and export draft', async () => {
    await page.getByRole('button', { name: '审核', exact: true }).click();
    await page.getByRole('button', { name: /确认审核 v/ }).click();
    await page.getByRole('dialog').waitFor({ state: 'hidden' });
    await page.getByRole('button', { name: '写入 EMR', exact: true }).click();
    await page.getByText('写回记录', { exact: true }).waitFor();
    await page.getByText('已确认', { exact: true }).waitFor();
    await capture('desktop-reviewed');
  });
  await step('Immutable version history and source navigation', async () => {
    await page.getByRole('button', { name: '文书版本记录' }).click();
    await page.getByRole('dialog').waitFor();
    await capture('desktop-history');
    await page.getByRole('dialog').getByRole('button', { name: '关闭', exact: true }).click();
    await page.getByRole('button', { name: '院内资料', exact: true }).click();
    await page.getByText('生命体征（模拟护理记录）', { exact: true }).waitFor();
    await assertNoOverflow();
  });
  await step('Mobile viewport and patient drawer', async () => {
    await page.setViewportSize({ width: 390, height: 844 });
    await page.getByRole('button', { name: '病历工作台', exact: true }).click();
    await assertNoOverflow();
    await capture('mobile-workspace');
    await page.getByRole('button', { name: '展开患者列表' }).click();
    await page.getByRole('button', { name: /陈芳/ }).click();
    await capture('mobile-second-patient');
    await assertNoOverflow();
  });
  await step('Microphone capture, durable upload, pause/resume and final manifest', async () => {
    await page.setViewportSize({ width: 1440, height: 1000 });
    await page.getByRole('button', { name: '新建录音会话' }).click();
    await page.getByRole('button', { name: '建立会话' }).click();
    await page.getByRole('dialog').waitFor({ state: 'hidden' });
    const sessionId = await page.getByLabel('当前录音会话').inputValue();
    await page.getByRole('button', { name: '开始录音' }).click();
    await page.getByText(/[1-9]\d* 片已持久化确认/).waitFor();
    await page.getByRole('button', { name: '暂停录音', exact: true }).click();
    await page.getByRole('button', { name: '恢复录音', exact: true }).waitFor();
    await page.getByText(/[1-9]\d* 片已持久化确认/).waitFor();
    const count = Number((await page.locator('.recording-foot').innerText()).match(/(\d+) 片已持久化确认/)[1]);
    await page.getByRole('button', { name: '恢复录音', exact: true }).click();
    await page.waitForFunction(previous => {
      const value = document.querySelector('.recording-foot')?.textContent?.match(/(\d+) 片已持久化确认/);
      return value && Number(value[1]) > previous;
    }, count);
    await page.getByRole('button', { name: '结束', exact: true }).click();
    await page.getByRole('button', { name: '开始录音' }).waitFor();
    const token = await page.evaluate(() => sessionStorage.getItem('his_access_token'));
    const response = await context.request.get(`${baseURL}/api/v1/sessions/${sessionId}/audio-manifest`, {
      headers: { Authorization: `Bearer ${token}` },
    });
    assert.equal(response.status(), 200);
    const manifest = await response.json();
    assert.equal(manifest.status, 'COMPLETE');
    assert.ok(manifest.chunks.length >= 2);
    assert.deepEqual(manifest.channels[0].gaps, []);
    assert.equal(manifest.channels[0].contiguous_sample_end, manifest.final_manifest[0].sample_end);
    await capture('desktop-capture-complete');
  });
  assert.deepEqual(errors, [], 'Browser must not have uncaught runtime errors');
  await fs.writeFile(path.join(output, 'result.json'), JSON.stringify({ baseURL, passed: steps, errors, at: new Date().toISOString() }, null, 2));
} catch (error) {
  await capture('failure');
  await fs.writeFile(path.join(output, 'failure.txt'), error.stack + '\n' + await page.locator('body').innerText());
  throw error;
} finally {
  await browser.close();
}
