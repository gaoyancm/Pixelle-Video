# AI Media Platform 阶段02-C持久化任务API实施报告

## 1. 状态与结论

- 实施日期：2026-07-23
- 仓库：`D:\AI-Media-Platform\06-source\pixelle-video`
- 分支：`phase2-persistent-media-jobs`
- 起始HEAD：`745a900fd1ee21e25c88205d72184c3750503171`
- 结束HEAD：`745a900fd1ee21e25c88205d72184c3750503171`（未创建提交）
- 起始工作树：清洁；仅有已知`.pytest_cache`权限警告
- 结束工作树：仅包含本报告第2节列出的02-C未提交修改
- 本地与`origin/phase2-persistent-media-jobs`：起始`0 / 0`；结束HEAD仍为起始HEAD，未fetch、pull或push
- 实施结论：5个批准端点、应用服务、幂等隔离、取消、重试血缘、安全Schema、增量迁移及离线测试已完成，状态为“待独立只读验收”

## 2. 文件清单

新增：

- `api/routers/media_jobs.py`
- `api/schemas/media_jobs.py`
- `api/services/__init__.py`
- `api/services/media_jobs.py`
- `migrations/versions/0002_add_media_job_retry_lineage.py`
- `tests/test_media_jobs_api.py`
- `docs/zh/development/persistent-media-jobs-phase2c-api.md`
- `docs/zh/development/阶段02-C持久化任务API实施报告.md`

修改：

- `api/app.py`
- `api/dependencies.py`
- `api/routers/__init__.py`
- `pixelle_video/media_jobs/contracts.py`
- `pixelle_video/media_jobs/models.py`
- `pixelle_video/media_jobs/repository.py`
- `tests/test_media_job_migrations.py`
- `tests/test_media_job_repository.py`

删除：无。

明确未修改：旧`/api/tasks`、旧`/api/video/generate/*`、Worker/Executor/Worker CLI、ComfyUI Adapter、历史迁移`0001`、四个工作流JSON、Streamlit、provider实现、依赖及锁文件。

## 3. 端点与HTTP语义

| 端点 | 成功语义 |
|---|---|
| `POST /api/media/jobs` | 首次创建`201`；相同幂等语义返回原任务`200` |
| `GET /api/media/jobs/{job_id}` | 安全任务快照`200`；不存在`404 job_not_found` |
| `GET /api/media/jobs` | 状态过滤、`limit` 1..100、非负`offset`；返回`has_more`而无`total` |
| `POST /api/media/jobs/{job_id}/cancel` | 写入取消请求`200`；重复请求返回同一语义 |
| `POST /api/media/jobs/{job_id}/retry` | 新重试任务`201`；幂等重复返回原重试任务`200` |

外部创建字段只有`workflow`、受控`parameters`及I2V所需的单值受管`asset_id`。Router只做HTTP解析、应用服务调用和响应映射；业务规则与事务位于应用服务/Repository。

## 4. 工作流白名单

白名单唯一来源为`pixelle_video/services/comfyui_workflows.py::WORKFLOW_SPECS`：

- `a800_wan22_t2v_33f`
- `a800_wan22_t2v_81f`
- `gpu_4090_wan21_i2v_33f`
- `gpu_4090_wan21_i2v_81f`

API没有接受任意工作流路径、provider、node、ComfyUI URL或执行器配置。

## 5. 幂等与并发

`Idempotency-Key`要求1至128个`A-Z a-z 0-9 . _ ~ : -`字符。原始键经包含操作类型及可选原任务ID的SHA-256规范化，原始值不入库。

- 普通创建：持久化键作用域`create + 原始键`；
- 重试：持久化键作用域`retry + 原任务ID + 原始键`；
- 创建与重试不会互相命中；
- 不同原任务的重试不会互相命中；
- 相同作用域键由既有数据库唯一约束保证并发只产生一行；
- 同键同语义返回原任务，同键不同创建语义返回`409 idempotency_conflict`。

## 6. 取消和重试

取消仅可靠记录平台取消请求，Worker在安全点协调平台终态。API不调用远端取消或全局`/interrupt`，不承诺远端GPU即时停止。重复取消不回退状态、不产生重复副作用；终态返回`409 job_not_cancelable`。

重试由Repository短事务原子完成原任务读取、`can_retry()`资格检查和子任务插入。只允许既有状态机批准的`failed`错误类别；`submission_unknown`、成功、取消、超时、排队、提交中和运行中均不可重试。新任务记录`retry_of_job_id`、递增`retry_count`并生成新submission token，原任务不改变。

## 7. 迁移

新增`0002_add_media_job_retry_lineage.py`，在`media_jobs`增加nullable `retry_of_job_id`及普通索引。既有行自然为NULL，无伪造回填。测试验证：

- 从空库升级到head；
- 从02-B的`0001`升级；
- downgrade回`0001`；
- 再次upgrade到head；
- ORM与迁移Schema一致；
- 历史`0001`未修改。

应用启动不自动运行迁移或建表。

## 8. 错误与安全Schema

固定错误结构为`{"error":{"code":"...","message":"..."}}`。已固定：

- `job_not_found`：404
- `workflow_not_allowed`：422
- `job_not_cancelable`：409
- `job_not_retryable`：409
- `idempotency_conflict`：409
- `invalid_request`：422
- `service_unavailable`：503
- `internal_error`：500

响应不序列化ORM对象；只返回公共ID、工作流、状态、安全时间、取消布尔值、重试来源、安全输出摘要及固定脱敏错误摘要。不返回相对/绝对路径、DSN、凭据、原始幂等键、submission token、request hash、prompt ID、provider/node、租约、version、远端原始响应或堆栈。

## 9. 测试结果

实施前基线：

- 完整离线测试：`149 passed in 7.17s`

实施后：

- 02-C API + Repository + 迁移定向：`59 passed in 4.47s`
- 完整离线回归（最终复跑）：`180 passed in 8.93s`
- 新增净测试数：31
- Ruff变更范围：通过；`api/app.py`既有为调整导入路径而置于`sys.path`处理后的`E402`按现状排除
- skipped/xfail：0
- 既有Pydantic弃用警告：12条；来自未修改旧Schema，不影响通过结果

新增测试覆盖四工作流、白名单/内部字段拒绝、固定错误、严格幂等键、同键同/异语义、并发创建、查询安全视图、状态过滤、分页边界、`has_more`、取消幂等及终态冲突、重试血缘/隔离/并发、`submission_unknown`拒绝、稳定次排序、OpenAPI操作数、迁移升级/降级/再升级和架构导入边界。

未运行：真实GPU/ComfyUI、真实媒体任务、公网、生产PostgreSQL、多节点、负载/长稳、认证/授权/渗透、AWS/Docker部署；均不在本阶段授权内。

## 10. 已知限制

- 仅面向本地或受信网络；无认证、授权、多租户、限流及配额；
- SQLite并发测试不等于生产PostgreSQL多节点保证；
- 取消不保证远端计算停止；
- `submission_unknown`只保留02-B有限对账语义且不可重试；
- 无事件/attempt历史、Webhook、SSE、WebSocket、资产下载或旧入口迁移；
- 精确生产部署、安全收口和资产API属于后续独立阶段，本轮未开始02-D。

## 11. 边界确认

本轮未启动GPU、ComfyUI、Uvicorn、外部Worker服务或真实媒体任务；未联网安装依赖；未创建提交、未推送、未创建PR、未合并、未rebase/reset/cherry-pick、未创建Tag/Release、未修改`main`或`upstream`、未开始02-D。

## 12. 首次独立验收后的必要修复

首次独立只读验收结论为“不通过，必须修复”，指出F-01至F-04。本报告保留首次实施测试事实，不把修复追溯描述为首次实施即已正确。后续必要修复事实如下：

- I2V公开字段由错误的`asset_ids`更正为单值`asset_id`，服务层转换为既有内部单元素资产列表；
- 公开参数使用集中强类型模型，并从`WORKFLOW_SPECS.parameter_targets`派生工作流适用性；
- 未知异常增加仅作用于新Router的固定脱敏`500 internal_error`映射；
- 增加资产、参数边界和无副作用、跨页稳定性、API并发重试、服务不可用、未知异常、OpenAPI脱敏及导入/旧入口架构边界测试；
- 真实修复后最终测试数量和命令见《阶段02-C必要修复报告.md》；修复后仍须新的独立只读复验。
