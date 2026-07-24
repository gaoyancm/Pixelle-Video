# 阶段02-C持久化媒体任务API

阶段02-C在现有FastAPI应用中提供受信网络内使用的持久化任务控制面。它只创建和管理数据库任务，不启动Worker、不调用ComfyUI，也不等待媒体生成完成。

## 端点

统一前缀为`/api/media/jobs`：

- `POST /api/media/jobs`：创建任务，要求`Idempotency-Key`；
- `GET /api/media/jobs/{job_id}`：读取安全任务视图；
- `GET /api/media/jobs`：按可选状态及`limit/offset`稳定分页；
- `POST /api/media/jobs/{job_id}/cancel`：持久化取消请求；
- `POST /api/media/jobs/{job_id}/retry`：为可重试失败任务创建新任务，要求`Idempotency-Key`。

创建和重试首次成功返回`201`；同一幂等语义重复请求返回原任务及`200`。列表固定按`created_at DESC, job_id DESC`排序，以`limit + 1`计算`has_more`，不返回精确`total`。

## 输入与工作流白名单

服务端唯一白名单位于`pixelle_video/services/comfyui_workflows.py::WORKFLOW_SPECS`，仅包含阶段01验证的四个工作流。外部Schema只接受：

- `workflow`；
- 该工作流注册的受控`parameters`；
- I2V工作流所需的单个受管`asset_id`。

调用方不能提交任务ID、状态、provider、node、执行器、工作流路径、输出路径、数据库字段、submission token、租约或`retry_of_job_id`。

`parameters`在任务入库前执行强类型和边界校验：

| 参数 | 类型与范围 | 适用工作流 |
|---|---|---|
| `prompt` | 必填字符串，去除空白后非空，1..4096字符 | 全部4个 |
| `negative_prompt` | 可选字符串，0..4096字符 | 全部4个 |
| `width`、`height` | 严格整数，1..16384 | 全部4个 |
| `frame_count` | 严格整数，1..10000 | 全部4个 |
| `seed` | 严格整数，0..18446744073709551615 | 全部4个 |
| `steps` | 严格整数，1..1000 | 仅两个I2V工作流 |
| `cfg` | 有限浮点数，0..100 | 仅两个I2V工作流 |

字段适用性由`WORKFLOW_SPECS.parameter_targets`派生；未知字段、错误类型、越界值、不适用字段及超过32 KiB的参数被固定`422 invalid_request`拒绝，且不会创建任务。I2V外部只接受单值`asset_id`；`asset_ids`、路径、URL和`input_image`均不属于公开Schema。

## 幂等、取消和重试

原始幂等键只接受1至128个安全ASCII字符，使用SHA-256转换成持久化作用域键；原始值不入库、不记录日志。创建作用域为`create`，重试作用域为`retry + 原任务ID`，因此相同原始键不会在两种操作或不同重试来源之间误命中。数据库唯一约束是并发正确性的最终保障。

取消只持久化`cancel_requested_at`。重复取消安全，但不承诺已经提交到远端GPU的工作立即停止，也不调用ComfyUI取消接口。

重试仅复用状态机`can_retry()`允许的`failed`错误类别，原任务保持不变；新任务具有新ID、新submission token、递增的`retry_count`及`retry_of_job_id`。`submission_unknown`始终不可重试。

## 数据库与运行边界

先显式运行Alembic迁移：

```text
alembic upgrade head
```

FastAPI只惰性建立连接池，不自动迁移、不自动建表、不启动Worker。API和外部02-B Worker必须指向同一数据库。

本阶段没有认证、授权、多租户、限流、配额或公网防护。在这些能力和网络隔离完成前，不得把端点直接暴露到公网。

未分类异常只在这5个新端点内映射为固定、脱敏的`500 internal_error`，响应不包含原始异常文本或内部信息。
