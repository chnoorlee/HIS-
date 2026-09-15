# GitHub Pages 部署

在线地址：[住院语音病历工作台](https://chnoorlee.github.io/HIS-/)。

## 部署范围

GitHub Pages 托管前端的**只读公开演示版**，复用实际工作台组件，提供随源码维护的虚构患者、文书、转写和来源数据。它不运行 FastAPI、PostgreSQL、ASR、大模型或 EMR 服务，也不连接访问者的本机后端。

演示版无需账号密码，不采集麦克风音频，不建立实时转写连接，不保存病历修改，不执行审核或 EMR 回写。完整应用仍按 [安装与恢复手册](operations.md) 部署前后端及院内服务。公开页面不能用于真实患者资料。

## 自动更新

[Deploy GitHub Pages 工作流](../.github/workflows/pages.yml) 在每次推送到 `main` 时自动运行，不限制修改文件类型。合并到 `main` 的拉取请求同样触发部署；尚未推送的本机修改和其他分支不会更新公开站点。

流程依次执行依赖安装、前端单元测试、正常应用构建、Pages 构建、桌面与移动端浏览器检查，最后将 `frontend/dist/` 发布到 `github-pages` 环境。构建或检查失败不会替换上一个成功发布的站点。工作流按顺序部署，避免并发发布互相覆盖。

可在 [GitHub Actions](https://github.com/chnoorlee/HIS-/actions/workflows/pages.yml) 查看进度，也可在 `main` 分支使用 **Run workflow** 手动重新部署。仓库的 **Settings → Pages → Source** 应为 **GitHub Actions**。工作流使用 GitHub 自带的 `GITHUB_TOKEN` 与 OIDC 权限，无需个人访问令牌或模型密钥。

站点的 [`deployment.json`](https://chnoorlee.github.io/HIS-/deployment.json) 记录已发布提交 SHA、构建模式和构建时间。页面更新以 Actions 部署成功为准；已打开的页面需要刷新才能加载新版本。

## 本地验证

需要 Node.js 24 和 npm。在 `frontend/` 中执行：

```powershell
npm ci
npm test
npm run build
npm run build:pages
npx playwright install chromium
npm run test:pages
npm run preview -- --mode pages --port 4173
```

预览地址为 `http://127.0.0.1:4173/HIS-/`。`test:pages` 会自行启动临时预览服务并在结束后关闭，验证子路径资源、桌面与手机浏览、只读限制，以及没有 API 请求或麦克风访问。

`npm run build:pages` 使用 Vite 的 `pages` 模式和 `/HIS-/` 资源前缀；普通 `npm run build` 和 `npm run dev` 保持完整应用模式。部署产物只来自 `frontend/dist/`，不会上传数据库、录音、`.local/` 或后端配置。

工作流依据 [GitHub Pages 自定义工作流文档](https://docs.github.com/en/pages/getting-started-with-github-pages/using-custom-workflows-with-github-pages)和 [Vite 静态部署文档](https://vite.dev/guide/static-deploy#github-pages)配置。
