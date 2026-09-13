# 住院语音病历工作台

面向住院办公室的语音采集、病史整理、证据核验和病历审核系统。已实现可本地运行的第一版，包括中文医生工作台、Windows 原生采音端、后端服务、模型适配和草稿回写流程。实现依据为根目录的《住院语音病历系统_项目架构设计》；逐项证据及待医院验收项见 [验收清单](docs/implementation-progress.md)。

支持入院记录、首次病程、日常病程和出院记录。入院记录包含 14 个结构化章节，模型候选事实保留来源、主体和不确定状态，医生审核绑定明确版本，回写目标为 EMR 草稿。

文书引用按章节核对版本，新增事实须纳入或明确排除，普通保存不会自动解除过期引用。规则与 API 契约见 [文书引用核对](docs/reference-review.md)；本轮改进及验证边界见 [自评与改进记录](docs/implementation-progress.md#本轮自评与改进)。

## 本机运行

Windows 需要 Python 3.12、Node.js 22 或更高版本。原生采音端另外需要 .NET 10 SDK 和 WebView2 Runtime。

```powershell
powershell -ExecutionPolicy Bypass -File scripts/setup.ps1
powershell -ExecutionPolicy Bypass -File scripts/start.ps1
```

医生工作台默认 `http://127.0.0.1:5173`，API 文档默认 `http://127.0.0.1:8787/docs`。端口占用时指定 `scripts/start.ps1 -ApiPort 8789 -WebPort 5175`。停止项目进程使用 `scripts/stop.ps1`。

开发账号：`doctor / Doctor123!`、`reviewer / Reviewer123!`、`admin / Admin123!`。仅用于虚构病例的本地验证，生产环境禁止这条身份路径。首次安装生成的密钥保存在已排除版本控制的 `.local/runtime.env` 中。

默认模型为明确标注的开发演示模式，真实 ASR 默认未配置。界面不会把手工录入或预置脚本冒充真实识别效果。真实模型、医院身份源和 EMR 连接器需要按医院批准的地址及凭据配置。

Windows 原生端的发布程序位于 `desktop/artifacts/win-x64/HisVoice.Desktop.exe`，先启动上述服务再打开。重新构建使用 `desktop/publish.ps1`，采音、缓存与恢复边界见 [原生客户端说明](desktop/README.md)。

## PostgreSQL 容器环境

```powershell
docker compose --env-file .local/compose.env up --build -d
docker compose --env-file .local/compose.env ps
```

该环境使用 PostgreSQL、独立任务进程和加密音频卷，仍然是采用虚构数据与模拟 EMR 的开发环境。网页端口 `5180`，API 端口 `8788`，数据库仅绑定本机 `55432`。不能用它的成功运行替代院内生产部署验收。

## 验证与目录

```powershell
powershell -ExecutionPolicy Bypass -File scripts/test.ps1 -Desktop -E2E
```

浏览器测试需要运行中的开发服务。只执行后端、适配器和前端构建检查时省略 `-Desktop -E2E`。PostgreSQL 并发测试通过 `HIS_TEST_POSTGRES_URL` 指向专用测试数据库，使用独立随机 schema；默认 SQLite 会明确跳过这些用例。

`backend/` 保存服务、迁移和后端测试；`frontend/` 保存医生工作台；`desktop/` 保存原生双通道采音端；`tests/` 保存跨模块验收；`infra/` 和 `scripts/` 保存部署及运维工具。[安装与恢复手册](docs/operations.md) 包含生产配置、OIDC 身份导入、备份和恢复隔离步骤。原始研究、试点和架构文档保持保留。
