# 阶段02-B实施报告

## 1. 结论

阶段02-B“独立 Worker 执行层”已按限定范围完成实现，当前停在等待独立只读验收节点。未创建本地提交，未推送GitHub。

本阶段只是 AI Media Platform“基座B与统一可靠执行层”的一部分，不代表整个项目路线。基座A（OpenMontage＋seedance-2.0方向）、LLM策划编排、Prompt/知识/QC，以及广告、短视频、长篇AI剧和动画流水线均未纳入本阶段，也未被取消。

## 2. 基线核验

- 仓库：`D:\AI-Media-Platform\06-source\pixelle-video`
- 分支：`phase2-persistent-media-jobs`
- 起始HEAD：`809f34c4431101fed13a344ccf290671a00b4cc1`
- 起始工作树：干净
- 阶段02-A离线基线：`84 passed`

以上条件在修改前已核验。

## 3. 实施范围

- 02-B1：增加独立 Worker 入口、轮询配置、稳定 Worker 标识和优雅退出。
- 02-B2：增加候选扫描、租约竞争、heartbeat、过期接管和旧所有者写保护。
- 02-B3：增加可恢复 ComfyUI Executor，复用阶段01构建器，持久化提交边界、`prompt_id`、history状态和输出。
- 02-B4：增加基于明确 submission token 回显的有限`submission_unknown`对账。
- 02-B5：增加完成、无输出失败、平台取消、持久化超时和终态竞争协调。

独立只读验收提出的三项限定修复已完成：heartbeat只能在当前租约尚未过期时续租；远端`filename`、`subfolder`和`type`在调用`/view`前完整白名单校验；成功/取消/超时增加三方并发CAS直接测试。

复验补充的Windows盘符相对路径缺口亦已修复：`subfolder`除绝对Windows路径外，任何`PureWindowsPath(...).drive`非空的路径（例如`C:relative`和`D:folder/file`）均被拒绝，且新增多盘符直接测试确认不会调用`/view`。

阶段02-A模型已包含所需字段，本阶段无需新增迁移，也没有修改历史迁移。

## 4. 关键设计

Worker以数据库为唯一事实源，扫描与领取分离，所有权变更使用短事务 CAS。heartbeat与慢 HTTP并行运行；每次成功续租后同步本地版本。租约丢失后Executor停止写入。

提交分为准备和产生远端副作用两个阶段：先持久化`submit_started_at`，再调用`POST /prompt`；成功后立即持久化`prompt_id`。已知`prompt_id`的恢复任务只查询，不重提。

仓库实际存在的四个工作流是A800 T2V 33/81帧及4090 I2V 33/81帧，并非四种T2I/I2I/T2V/I2V类别。实现按四个真实注册键逐项参数化验证，没有修改JSON或虚构工作流。这是对契约措辞的最小事实调整，不降低覆盖或安全保证。

对账只认可 queue/history 中明确且唯一的`extra_data.pixelle_submission_token`回显。没有唯一证据时保持未知，绝不自动重提。

## 5. 安全不变量

- 同一版本和租约现场最多一个 Worker 领取成功。
- heartbeat、状态、输出和终态写入均校验 owner及版本。
- heartbeat额外校验数据库中的租约尚未到期，过期所有者不能通过续租复活。
- 过期接管后旧 Worker不能继续写。
- `POST /prompt`之前先写提交标记。
- 已有`prompt_id`不再次提交。
- `submission_unknown`没有唯一证据不补写、不重提。
- `running`但无`prompt_id`不会猜测性重提。
- 合法输出持久化后才允许成功。
- 远端`/view`只接受安全文件名、受限POSIX相对`subfolder`和明确的`output`存储类型。
- 完成、取消、超时竞争最多一个终态生效，终态不复活。
- 没有调用全局`/interrupt`。
- 输入和输出路径限制在受管根目录，日志不写密钥和Prompt。

## 6. 文件变更

新增：

- `pixelle_video/media_jobs/worker.py`
- `pixelle_video/media_jobs/executor.py`
- `pixelle_video/media_jobs/worker_cli.py`
- `tests/test_media_job_worker.py`
- `tests/test_media_job_executor.py`
- `tests/test_media_job_recovery.py`
- `docs/zh/development/persistent-media-jobs-phase2-worker.md`
- `docs/zh/development/persistent-media-jobs-phase2b-report.md`

修改：

- `config.example.yaml`
- `pixelle_video/config/schema.py`
- `pixelle_video/media_jobs/__init__.py`
- `pixelle_video/media_jobs/repository.py`
- `pixelle_video/services/comfyui_adapter.py`
- `tests/test_media_job_config.py`

迁移：无。API、Web、provider路由、依赖锁文件和四个私有工作流JSON均未修改。

## 7. 测试结果

最终验证结果：

- Ruff（本阶段变更的Python文件）：`All checks passed!`
- 锁文件离线一致性：`uv lock --check --offline`，`Resolved 160 packages`
- 原阶段02-A基线文件：`84 passed in 3.01s`
- 新增阶段02-B测试：`65 passed in 5.02s`
- 全量离线测试：`149 passed in 6.92s`
- 跳过：0
- xfail：0
- 警告：0（pytest输出无警告段）

测试命令统一使用`UV_OFFLINE=1`、仓库内临时uv缓存、UTF-8输出、`-p no:cacheprovider`和独立`--basetemp`。新增测试覆盖65项，原84项测试未删除、未跳过、未弱化。所有协议测试使用伪造HTTP服务，不连接真实ComfyUI或GPU。

## 8. 已知限制

- 不保证严格 exactly-once 远端提交；远端已接收而响应丢失的窗口客观存在。
- submission token回显能力只经过伪造协议验证，未在真实ComfyUI上验证。真实服务不回显时，任务会诚实停留在`submission_unknown`等待人工处理。
- 平台取消或超时不保证远端计算停止；本阶段禁止且未使用全局`/interrupt`。
- SQLite验证不等于生产多节点能力；未实现PostgreSQL、消息队列、分布式GPU容量调度或自动故障转移。
- 本阶段未做真实GPU、真实ComfyUI或真实媒体生成测试。

## 9. 范围外事项确认

未改造API、Web UI或Streamlit；未增加用户任务管理页面；未引入Redis、Celery、RabbitMQ、Kafka或PostgreSQL；未实施鉴权、计费、配额、审计、多租户、AWS、Docker生产部署；未接入OpenMontage、seedance-2.0、LLM、Prompt/知识/QC、广告或任何内容流水线；未修改远程ComfyUI、下载模型或改写工作流JSON。

## 10. Git状态

当前仍在`phase2-persistent-media-jobs`，HEAD保持起始值`809f34c4431101fed13a344ccf290671a00b4cc1`。阶段02-B变更保持未提交状态；未推送GitHub。工作树包含第6节列出的14个预期文件，没有阶段02-B本地提交。

## 11. 建议下一步

只建议对当前未提交工作树进行独立只读验收。验收完成前不创建提交、不推送、不开始02-C或其他后续模块。
