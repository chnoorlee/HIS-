# 安装、身份接入与恢复手册

本手册对应当前工程实现。开发环境只使用虚构数据；医院上线仍需配置真实身份源、批准的模型、EMR 接口、TLS、存储与备份策略。

## 本地开发与发布

1. 执行 `scripts/setup.ps1` 安装依赖并生成本机密钥，执行 `scripts/start.ps1` 启动工作台和 API。
2. 浏览器使用 `http://127.0.0.1:5173`。原生端执行 `desktop/publish.ps1`，启动 `desktop/artifacts/win-x64/HisVoice.Desktop.exe`。原生端仍需 WebView2 Runtime。
3. 使用 `scripts/test.ps1 -Desktop -E2E` 验证；浏览器验收需已有运行中的服务、Edge 和 Playwright。当前测试也兼容 Codex 随附的 Playwright 运行时。
4. PostgreSQL 开发部署使用 `docker compose --env-file .local/compose.env up -d --build`。该 Compose 明确使用演示身份、演示模型和模拟 EMR，不能直接当生产配置。
5. 若 Docker BuildKit 在中文工作区报告 session sharedkey 非 ASCII，可在当前 PowerShell 进程设置 `$env:COMPOSE_BAKE='false'`、`$env:DOCKER_BUILDKIT='0'` 后重新构建。这不会修复 Docker 引擎自身无法启动的问题。

`scripts/stop.ps1` 依据 PID、启动时间和可执行路径核对后停止本项目进程。记录文件使用 UTF-8，兼容 Windows PowerShell 5.1 中文路径。

## 医院配置

部署方通过受控环境变量或秘密管理服务提供以下配置，模型与 EMR 密钥只放后端：

| 配置 | 含义 |
| --- | --- |
| `HIS_ENV=production` | 禁止开发登录、SQLite、演示模型及模拟 EMR |
| `HIS_DATABASE_URL`、`HIS_DATA_DIR` | PostgreSQL 连接与加密音频卷；各医院独立数据面 |
| `HIS_JWT_SECRET`、`HIS_AUDIO_KEY` | 持久化签名材料与 Fernet 音频/恢复清单密钥；单独备份密钥 |
| `HIS_OIDC_ISSUER`、`HIS_OIDC_AUDIENCE`、`HIS_OIDC_JWKS_URL` | 医院令牌验证配置，必须使用批准的 HTTPS 身份源 |
| `HIS_OIDC_CLIENT_ID`、`HIS_OIDC_SCOPE` | 浏览器授权码 + PKCE；身份源登记 `/auth/callback` 回调和注销后地址 |
| `HIS_ALLOWED_ORIGINS` | 允许使用工作台的具体 HTTPS 来源，逗号分隔 |
| `HIS_ASR_PROVIDER=dashscope_realtime` | 配置 `HIS_ASR_BASE_URL` WSS 地址、`HIS_ASR_API_KEY` 与明确的 `HIS_ASR_MODEL` |
| `HIS_MODEL_PROVIDER=openai_compatible` | 配置 `HIS_LLM_BASE_URL`、`HIS_LLM_API_KEY`、`HIS_LLM_MODEL` |
| `HIS_APPROVED_MODEL_HOSTS` | 经医院批准的模型主机名列表；不会自动回退到其他服务 |
| `HIS_EMR_PROVIDER=http` | 配置 HTTPS `HIS_EMR_BASE_URL` 和 `HIS_EMR_API_KEY`，按接口契约适配院内系统 |
| `HIS_RUN_WORKER=false` | API 部署关闭内嵌 worker，独立进程执行 `python -m app.worker` |

ASR 也支持 `openai_compatible` HTTP 转录接口。该模式的分片识别不等同于供应商原生双向流。`unavailable` 状态保留人工录入能力，不会返回虚构识别结果。具体兼容契约见 `integrations/asr/README.md` 和 `docs/extraction-contract.md`。

## 首次身份与住院信息导入

后端从医院 OIDC 的 `sub` 映射本地账号，不依据姓名自动配对患者。由授权实施人员在 API/worker 停止时准备医院确认过的 JSON，格式见 `docs/provision.example.json`，然后在 backend 目录执行：

```powershell
.\.venv\Scripts\python.exe -m app.ops provision C:\HospitalConfig\provision.json
```

容器中对应 `python -m app.ops provision /config/provision.json`。导入文件中的 issuer 必须匹配运行配置；账号默认停用，启用和就诊授权需要在输入中明确列出。重复导入可更新权限并撤销本地旧令牌，不能更换已有账号的 OIDC 主体，也不能更改既有就诊的患者/住院绑定。修改人口学资料或诊断快照会递增来源版本并使相关审核失效。

这是受控的离线导入入口。自动诊疗关系同步、各厂商 HIS/EMR 字段映射需要接入医院接口后验收。真实密钥和包含患者信息的导入文件不进入代码仓库。

## 备份与恢复

PostgreSQL 备份由医院维护的工具执行，例如 `pg_dump --format=custom --file=<受控备份路径> <数据库连接>`，连接凭据通过权限受控的连接服务文件或秘密管理设施提供。恢复到新的隔离数据库，使用 `pg_restore --exit-on-error --dbname=<新数据库> <备份>`，不覆盖运行中的数据库。保留恢复点、数据库迁移版本和备份核验结果。

音频使用独立卷，数据库备份不含音频文件。医院需分别决定是否备份短期音频，并记录数据库和对象的匹配恢复点。密钥丢失会使旧音频和恢复清单无法解密；备份存在不代表这些数据已经可恢复。

恢复顺序如下，所有 `app.ops` 操作均使用恢复目标的后端配置，且 API、worker、用户访问保持停止：

1. 在正常环境定期导出加密隔离/删除清单：`python -m app.ops ledger-export <hospital_id> <新的清单路径>`。保存到与主数据库故障域独立的受控存储。清单包含最小就诊标识，不包含病历正文，不能公开分发。
2. 在隔离网络恢复数据库及获准音频。运行 `python -m app.ops recovery-isolate <hospital_id>`。命令执行数据库迁移、关闭写回、停用全部身份及授权、递增采音/任务代次、失效既有审核；SENDING 转 UNKNOWN，尚未发送的 PREPARED 取消。
3. 重放晚于数据库恢复点的有效清单：`python -m app.ops ledger-replay <hospital_id> <清单路径>`。会话先隔离，再清理工作副本。即使数据库已标记 DELETED，也再次清理旧音频卷中恢复出的对象。在途 UNKNOWN 保留待核查，不因删除而重发。
4. 对照恢复点之后直到故障收敛的完整时间窗口，核查 EMR 操作键、目标文书及院方修正事项。必须涵盖本地备份中已经丢失的导出操作；仅核查数据库现有 UNKNOWN 行不够。
5. 核对撤权、删除、设备和密钥变更后，使用最新导入清单重新授权必要用户。生产 OIDC 令牌的 `iat` 必须晚于恢复隔离时间，旧令牌不会随账号重新启用而恢复有效；身份源还需完成院方要求的会话撤销。
6. 按清单核对隔离状态、缺片、来源可用性、任务代次、样本文书和外部结果后，才开放获准的读取/人工编辑。

当前版本没有自动解除灾备写回隔离的入口，普通管理开关也不能解除。`ledger-replay` 明确返回 `window_completeness_verified=false`。完整 EMR 恢复窗口清单与解除流程需在医院连接器验收时落实；证据不足时继续人工病历流程。恢复清单也不证明供应商副本已删除。

## 故障处理与升级

- ASR/LLM 故障：检查系统状态、失败任务及批准端点；保留原文与人工草稿，恢复任务不能覆盖医生已经确认的内容。
- 未确认音频：使用原 Windows 用户和原会话恢复加密缓存；出现缺失终点或 gap 时保持不完整，不能凭服务端最大序号补出结束边界。
- EMR UNKNOWN：执行只读核查；查询暂未找到也不代表可以再次发送。签署后修改归院内更正流程。
- 误录：隔离会话，核查派生资料与外部污染，再执行受控删除。音频过期后回放明确不可用，不能伪装证据仍在。
- 升级：先在虚构数据环境运行回归和迁移，再发布同一版本制品。API 与 worker 启动共用 PostgreSQL 迁移锁。回滚前确认数据迁移兼容性；不使用 destructive downgrade 回滚已经产生的临床数据。

当前运行证据及未验证项见 `docs/implementation-progress.md`。噪声质量、实体设备恢复、医院 SSO/EMR、高可用和 RPO/RTO 不能从单机自动化测试推断。
