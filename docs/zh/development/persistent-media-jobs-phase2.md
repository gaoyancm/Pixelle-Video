# 阶段02-A：持久化媒体任务契约与数据库基础

本阶段只建立持久化媒体任务的数据契约、状态机、异步数据库基础、Repository、幂等与
CAS，并未实现 worker、任务 API 或任何 ComfyUI/provider 执行链路。

## 范围和安全边界

阶段02-A包含：

- SQLAlchemy 2.x 异步 Session；
- 本地 SQLite + aiosqlite；
- Alembic 初始迁移；
- `media_jobs` 数据模型；
- 纯领域状态机；
- 异步 Repository、幂等创建和 CAS 更新；
- 租约、heartbeat、远端状态及输出元数据的持久化基础。

明确不包含：

- worker 或轮询循环；
- ComfyUI 执行器或 `ComfyUIAdapter` 修改；
- 任务/素材/输出 API；
- Redis、Celery、Web、AWS、鉴权或真实 PostgreSQL 部署；
- ComfyKit、RunningHub、API provider 调用链变更。

## 依赖

新增运行依赖：

- `sqlalchemy>=2.0,<3`：异步 ORM、事务和跨数据库查询；
- `alembic`：数据库 schema 版本管理；
- `aiosqlite`：Windows/本地 SQLite 异步驱动。

本阶段没有加入 PostgreSQL 驱动，也没有声称完成真实 PostgreSQL 验证。模型和迁移使用
字符串、整数、标准 JSON、`DateTime(timezone=True)`、约束及普通索引，避免依赖 SQLite
专有表结构。

## 配置

`config.example.yaml` 中的 `media_jobs` 默认关闭：

```yaml
media_jobs:
  enabled: false
  database_url: "sqlite+aiosqlite:///data/media_jobs.db"
  worker_mode: external
  poll_interval_seconds: 2.0
  lease_seconds: 60
  heartbeat_seconds: 20
  default_timeout_seconds: 900
  private_comfyui_enabled: true
  legacy_providers_enabled: false
  managed_output_root: "output/media_jobs"
  managed_asset_root: "data/media_assets"
```

构造或解析 `MediaJobsConfig` 不会创建 engine。只有显式调用
`MediaJobsDatabase.connect()` 且 `enabled=true` 时才创建异步 engine；关闭状态下调用会抛出
`MediaJobsDisabledError`，不会创建 SQLite 文件。

数据库 URL 可能包含密码，因此不得写入日志、错误正文或任务记录。示例只使用配置文件目录相对
SQLite 路径。

## 数据模型

核心表为 `media_jobs`，包含：

- 标识与路由：`job_id`、`workflow_type`、`workflow_key`、`executor_kind`、`provider`、
  `node_id`；
- 状态：`status`、`remote_status`、`remote_termination_status`、
  `remote_status_updated_at`；
- 输入：`input_json`、只含 `asset_id/role` 的 `input_assets_json`；
- 远端关联：`comfyui_prompt_id`、`submission_token`；
- 幂等：`idempotency_key` 唯一约束、`request_hash`；
- 时间：创建、更新、开始、结束、截止、提交开始、取消请求；
- 错误：`error_category` 和脱敏后的 `error_message`；
- 输出：只含受控相对路径、hash、MIME、大小和可选媒体信息的 JSON 元数据；
- 后续 worker 基础：租约、heartbeat、下一尝试时间、重试次数和 `version`。

时间通过 `UTCDateTime` 统一读写为 timezone-aware UTC。数据库不保存输出 BLOB、Windows
绝对路径、ComfyUI Base URL、远端 `/view` URL、API Key 或 Authorization 头。

## 平台状态机

平台状态：

```text
queued -> submitting | cancelled | timed_out
submitting -> running | failed | cancelled | timed_out
running -> succeeded | failed | cancelled | timed_out
succeeded | failed | cancelled | timed_out -> no transition
```

普通状态转换禁止 `submitting -> queued`。只有 Repository 的
`requeue_unsubmitted_job()` 可以安全回队，并在同一条原子更新中要求：状态为 `submitting`、版本匹配、
`submit_started_at IS NULL`、`comfyui_prompt_id IS NULL`，且错误类别不是 `submission_unknown`。
这只用于 worker 在真正发起远端提交前崩溃的恢复。
`submitting + submit_started_at != null + comfyui_prompt_id == null` 表示提交不确定；其错误
类别是 `submission_unknown`，不是新的平台状态，也不得自动重新提交。

`cancelled` 和 `timed_out` 只表示平台终态，不等于远端任务已停止。远端终止情况由
`remote_termination_status` 独立表达。

`can_retry()` 只决定是否允许创建一个新任务；终态行永远不会原地复活。

## Repository 接口

`MediaJobRepository` 当前提供：

- `create_job()`：创建任务并处理并发幂等；
- `get_job()`、`list_jobs()`、`get_by_idempotency_key()`；
- `transition_status()`：状态与版本双条件 CAS；
- `set_safe_error()`：写入结构化类别和脱敏摘要；
- `update_prompt_and_remote_status()`；
- `claim_lease()`、`update_heartbeat()`、`clear_lease()`；
- `write_output_metadata()`。

所有写操作使用短事务。CAS 更新必须同时匹配 `job_id`、预期 `status` 和预期 `version`，
成功后 `version + 1`；不匹配时抛出 `CASConflictError`，不会静默覆盖。

## 幂等规则

`idempotency_key` 由数据库唯一约束提供最终并发防线：

1. key 相同且 `request_hash` 相同：返回已存在任务，`created=false`；
2. key 相同但 hash 不同：抛出 `IdempotencyConflictError`；
3. key 为空：每次创建新 UUID 任务；
4. 不依赖“先查询、再插入”作为唯一保护。

`request_hash` 不接受调用方提供，而是在 `MediaJobRepository.create_job()` 内根据
`workflow_type`、`workflow_key`、`executor_kind`、`provider`、`node_id`、`input_json` 和
`input_assets_json` 重新计算。它不包含 job/idempotency/submission 标识、时间、重试计数或运行状态。
计算使用排序键、紧凑 JSON、UTF-8 和 SHA-256；对象键序不影响结果，数组顺序保留。持久化输入会拒绝 API Key、
Authorization、数据库 URL/密码等凭据字段名。

## SQLite 设置

相对 SQLite URL 始终以实际加载配置文件所在目录为基准，不依赖 API 或 worker 的启动 cwd。
程序直接构造配置且没有配置文件位置时，必须向 `MediaJobsDatabase` 提供明确 `base_dir`，或使用绝对
SQLite URL；不会静默回退到 cwd。`enabled=false` 时不解析路径、不建目录、不创建 engine 或数据库。

每条 SQLite 连接启用：

- `PRAGMA journal_mode=WAL`；
- `PRAGMA foreign_keys=ON`；
- `PRAGMA busy_timeout=5000`。

ORM 元数据与 0001 迁移对数据库级 `server_default` 保持一致。JSON 字段两侧均不设置数据库级默认值。
`updated_at` 的后续更新时间由 Repository/应用层显式写入，不依赖数据库触发器，也不宣称数据库会自动更新。

测试数据库必须通过 pytest `tmp_path` 创建；Windows 环境若系统 `%TEMP%` 不可访问，可用：

```powershell
.\.venv\Scripts\pytest.exe -p no:cacheprovider `
  --basetemp output/pytest-phase2a
```

`output/` 已被 Git 忽略。正式 SQLite 文件及 `-wal/-shm` 也有显式忽略规则。

## Alembic

初始 revision：`0001_create_media_jobs`。

升级：

```powershell
.\.venv\Scripts\alembic.exe upgrade head
```

默认 URL 会在 `data/media_jobs.db` 建库，因此只有在明确启用并准备初始化数据库后才应运行。
自动化测试会覆盖 Alembic 配置中的 URL，使用临时目录。

初始迁移提供 `downgrade()`，它会删除 `media_jobs` 表及全部任务数据。只允许在一次性测试
库或已完成数据库和平台输出备份后执行；常规环境采用备份后的前向迁移策略。

## 离线验证

阶段02-A测试覆盖：

- 所有合法/非法状态转换和终态保护；
- 配置默认关闭及无数据库副作用；
- 创建、读取、关闭并重新打开 SQLite；
- JSON/UTC 往返；
- 同key同hash、同key不同hash、并发幂等和空key；
- CAS成功、过期版本、错误状态及取消/完成竞争；
- 租约、heartbeat、prompt ID、远端状态；
- 输出相对路径和安全错误脱敏；
- Alembic空库升级和重复 `upgrade head`；
- 阶段01全部回归测试。

## 已知限制和阶段02-B

- 当前没有 worker，任务不会被领取或执行；
- 没有任务 API；
- 没有素材登记、输出下载或远端取消；
- 没有真实 PostgreSQL 验证；
- 没有多 worker 节点容量协调；
- 没有修改或任务化任何 provider。

阶段02-B建议在本基础上实现独立单 worker 与四个私有 ComfyUI 工作流执行器，复用
阶段01适配器能力并持久化每个远端步骤。开始02-B前仍需单独授权。
