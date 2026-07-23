# 阶段02-B：持久化媒体任务 Worker 设计与运行

## 1. 边界

阶段02-B只建设 AI Media Platform“基座B与统一可靠执行层”中的独立 Worker。它复用阶段01的私有 ComfyUI 适配器和阶段02-A的任务契约、SQLite 模型、迁移及 CAS 仓储，不改造 API、Web UI 或四个工作流 JSON，也不包含基座A、LLM策划编排、Prompt/知识/QC、广告及内容生产流水线。

本阶段的保证不是严格的远端 exactly-once。数据库状态转换由版本号、租约所有者和 CAS 保护；已知 `prompt_id` 的任务只恢复查询；提交结果不确定的任务不会自动重提。

## 2. 执行结构

独立入口为：

```powershell
python -m pixelle_video.media_jobs.worker_cli --config config.yaml
```

`--once`执行一轮后退出，适合离线检查；`--worker-id`可显式指定实例标识。未指定时，进程使用主机名和随机后缀生成在该进程生命周期内稳定的标识。

执行链为：

1. Worker从数据库扫描可领取任务；
2. 仓储通过短事务和 CAS 写入 `lease_owner`、`lease_expires_at`及新版本；
3. heartbeat独立续租，慢 HTTP 不占用数据库事务，也不阻塞续租；
4. Executor根据数据库现场只执行一个可恢复步骤；
5. 结束当前步骤后释放租约，后续轮询继续推进。

收到停止信号后不再领取新任务，当前步骤在安全边界收尾，heartbeat随之停止。失去租约或 CAS 冲突后，旧 Worker 不再写状态、输出或终态。

## 3. 配置

```yaml
media_jobs:
  enabled: true
  database_url: "sqlite+aiosqlite:///data/media_jobs.db"
  worker_mode: external
  poll_interval_seconds: 2.0
  history_poll_interval_seconds: 2.0
  recovery_scan_interval_seconds: 10.0
  lease_seconds: 60
  heartbeat_seconds: 20
  default_timeout_seconds: 900
  input_root: "data/media-inputs"
  output_root: "data/media-outputs"
```

相对路径以配置文件所在目录解析。`heartbeat_seconds`必须小于`lease_seconds`。运行前需要先执行现有 Alembic 升级：

```powershell
uv run alembic upgrade head
```

阶段02-A已有02-B所需字段和约束，因此本阶段没有新增迁移。配置默认关闭，现有同步媒体调用不受影响。

## 4. 租约和恢复语义

| 数据库现场 | 自动处理 |
|---|---|
| `queued`且无有效租约 | 正常竞争领取 |
| `submitting`且无`submit_started_at`、无`prompt_id` | 安全回队 |
| `submitting`且有`submit_started_at`、无`prompt_id` | 进入或保持`submission_unknown`，只对账 |
| `submitting`或`running`且有`prompt_id` | 只查询 queue/history，不再次提交 |
| `running`但无`prompt_id` | 按内部不变量破坏失败，不猜测性重提 |
| 任一终态 | 不领取、不恢复、不复活 |

提交前，Executor先持久化`submit_started_at`，再调用`POST /prompt`。成功响应中的`prompt_id`立即通过持有者 CAS 写入数据库。响应超时、丢失或不可解析时进入`submission_unknown`，不会根据“未找到”推断远端未接收。

## 5. ComfyUI执行与对账

Executor继续调用阶段01的工作流注册和构建入口。仓库实际注册的四个私有工作流是：

- A800 Wan2.2 T2V 33帧；
- A800 Wan2.2 T2V 81帧；
- RTX 4090 Wan2.1 I2V 33帧；
- RTX 4090 Wan2.1 I2V 81帧。

本阶段没有虚构T2I/I2I工作流，也没有修改工作流 JSON。

提交时使用持久化的稳定 submission token 作为`client_id`，并放入`extra_data.pixelle_submission_token`。有限对账只接受 queue/history 中明确回显的同一 token：

- 唯一匹配：补写对应`prompt_id`并恢复跟踪；
- 零匹配、多匹配、证据冲突、结构不完整或远端不可达：保持未知；
- 不使用文件名、时间窗口或输入相似度进行猜测；
- 不自动重新提交。

该回显相关性已由伪造 HTTP 协议离线验证，尚未在真实 ComfyUI 上验证，因此不能宣称所有真实部署都会保留`extra_data`。

## 6. 完成、取消和超时

只有 history 明确完成且存在合法输出时才进入`succeeded`。输出通过`/view`下载到受管输出根目录下的任务子目录，并保存类型、大小、哈希等元数据。路径逃逸、输出缺失或类型不合法均不能成功。

在调用`/view`前，Worker还会校验完整的远端输出引用：文件名必须是单个安全文件名；`subfolder`只允许空值或不含`.`、`..`、绝对路径、反斜杠、任何Windows盘符限定形式（包括`C:relative`）和UNC形式的POSIX相对层级；`type`必须明确为受管的`output`。危险引用不会发起`/view`请求、不会写本地文件，也不会持久化输出元数据。

取消和超时是平台终态协调：

- `cancel_requested_at`存在时，平台可进入`cancelled`；
- `deadline_at`到期时，平台可进入`timed_out`；
- 终态写入由 CAS 保证最多一个生效；
- 本阶段没有安全的按`prompt_id`远端取消能力，禁止调用全局`/interrupt`；
- 因此平台取消或超时不保证远端计算停止，远端仍可能继续运行。

## 7. 安全、运维与限制

- Worker日志只记录诊断所需的`job_id`、`worker_id`、状态和错误类别，不记录Prompt、API Key或素材绝对隐私路径。
- 输入只接受受管根目录下的`asset_id`引用；输出文件名和目录经过安全校验。
- 数据库是唯一任务事实源，进程内对象可丢失。
- SQLite适合Windows本地开发、离线验证和有限的同机多进程竞争测试；这不等于生产多节点数据库能力。
- 多 Worker只验证同一任务的所有权安全，不提供分布式GPU节点容量调度。
- 本阶段没有连接真实4090/A800、没有启动真实ComfyUI、没有生成真实媒体。
- 本阶段没有创建提交，也没有推送GitHub。

## 8. 离线测试

测试使用伪造 ComfyUI HTTP 响应覆盖四个实际工作流、竞争领取、有效租约续租、过期租约拒绝续租、崩溃接管、已知`prompt_id`恢复、提交不确定对账、完整远端输出引用安全、取消、超时及两方和三方终态竞争。测试不依赖真实网络、GPU或长时间sleep。
