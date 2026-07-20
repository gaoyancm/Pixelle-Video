# 第一阶段私有 ComfyUI 双节点开发

本阶段完成离线接入，并在 GPU 明确开启后分别完成 A800 与 RTX 4090 的真实链路
验收。代码不会修改 GPU 服务器上的 ComfyUI、节点或模型，也不包含云服务、SSH、
密码或 Token 配置。

## 工作流存放与注册

Pixelle-Video 会扫描 `workflows/selfhost/` 与 `data/workflows/selfhost/`。媒体工作流
文件名必须以 `image_` 或 `video_` 开头；扫描后使用
`selfhost/<文件名>.json` 作为工作流 key。

本阶段接入：

| 工作流类型 | 文件 | 路由节点 |
| --- | --- | --- |
| `a800_wan22_t2v_33f` | `video_a800_wan22_t2v_4step_33f_api.json` | A800 |
| `a800_wan22_t2v_81f` | `video_a800_wan22_t2v_4step_81f_api.json` | A800 |
| `gpu_4090_wan21_i2v_33f` | `video_4090_wan21_i2v_fp8_512x512_33f_api.json` | RTX 4090 |
| `gpu_4090_wan21_i2v_81f` | `video_4090_wan21_i2v_fp8_512x512_81f_api.json` | RTX 4090 |

前三份文件保留最初已验证 API JSON 的节点、模型名和默认参数。4090 81帧文件由
4090 33帧文件派生，仅将节点 `50.length` 改为 81，并已通过真实在线验收。运行时
适配器先读取文件，在内存副本上注入参数，不回写 JSON。

## 完成状态

| 第一阶段目标 | 状态 |
| --- | --- |
| 检查 Pixelle-Video 结构和原有 ComfyUI 调用 | 完成 |
| 使用 uv 启动 Web/API 空载验证 | 完成 |
| 确认工作流存放和注册方式 | 完成 |
| 接入三份原始已验证 API JSON | 完成，内容校验一致 |
| 建立 A800/4090 双节点配置 | 完成 |
| 建立统一 ComfyUI 适配器 | 完成 |
| GPU 离线 mock 测试 | 完成 |
| A800 与 4090 真实链路验收 | 完成 |
| 最小测试和开发文档 | 完成 |

## 节点配置

`config.example.yaml` 中的 `comfyui.nodes` 是无敏感信息的结构示例。每个节点包括：

- `id`：稳定的内部标识；
- `name`：显示名称；
- `base_url`：ComfyUI Base URL；
- `workflow_types`：该节点可执行的工作流类型；
- `enabled`：是否允许路由；
- `timeout_seconds`：HTTP 与整项任务超时；
- `concurrency`：进程内最大在途任务数。

示例使用回环地址并默认 `enabled: false`。真实地址只应写入已被 Git 忽略的
`config.yaml`，不要提交到仓库。GPU 离线时保持禁用。

## 适配器接口

统一适配器位于 `pixelle_video.services.comfyui_adapter.ComfyUIAdapter`，提供：

- `build_workflow()`：按登记的节点 ID 注入提示词、尺寸、帧数、种子等参数；
- `upload_image()`：将 4090 图生视频输入图上传到 ComfyUI；
- `submit()`：调用 `POST /prompt`；
- `query_status()`：组合 `/history/{prompt_id}` 与 `/queue` 返回统一状态；
- `get_outputs()`：解析历史记录并生成 `/view` 输出 URL；
- `execute()`：提交、轮询和取回输出的便捷闭环。

私有节点请求固定使用 `trust_env=False`，避免本机 HTTP/HTTPS 代理截获回环地址或
SSH 端口转发流量。任务完成、失败、超时、网络异常或调用方取消时都会释放对应
节点的进程内并发槽。文生视频工作流不会上传上层误传的无关图片。

Pixelle `MediaService` 只对上述四个登记工作流使用新适配器。其他 selfhost 和
RunningHub 工作流继续走原有 ComfyKit 路径。

## 参数节点

A800：正提示词 `89.text`、负提示词 `72.text`、宽高帧数
`74.width/height/length`、种子 `81.noise_seed`、输出前缀
`80.filename_prefix`。

4090：输入图片 `52.image`、正提示词 `6.text`、负提示词 `7.text`、宽高帧数
`50.width/height/length`、种子/步数/CFG `3.seed/steps/cfg`、WEBM 输出前缀
`47.filename_prefix`。

## 离线验证

无需启动 GPU：

```powershell
$env:PYTHONIOENCODING = "utf-8"
uv run --extra dev pytest
uv run --extra dev ruff check `
  pixelle_video/config/__init__.py `
  pixelle_video/config/manager.py `
  pixelle_video/config/schema.py `
  pixelle_video/service.py `
  pixelle_video/services/media.py `
  pixelle_video/services/comfyui_adapter.py `
  pixelle_video/services/comfyui_workflows.py `
  tests/test_comfyui_adapter.py
```

测试使用 `httpx.MockTransport` 模拟图片上传、任务提交、状态和输出，不会连接
真实 ComfyUI。全仓库 Ruff 仍会报告上游基线已有的格式问题，因此第一阶段验收只对
本阶段修改的 Python 文件执行 Ruff；这些文件必须全部通过。

## 在线验证记录

真实联调只在用户明确开启对应 GPU 后执行，结果分别保存在：

- [A800 在线验收记录](private-comfyui-phase1-a800-validation.md)
- [RTX 4090 在线验收记录](private-comfyui-phase1-4090-validation.md)

本地输出保存在 Git 忽略的 `output/online-validation/`，不会作为源码提交。

## 第一阶段最终验收

2026-07-19 最终收口结果：

- 分支：`phase1-comfyui-private-gpu`
- 上游基线：`848b054e4fae40dabc62ec58e960b573e83793ac`
- Python：uv 管理的 3.11.15
- Pytest：10 项全部通过
- Ruff：本阶段修改的 Python 文件全部通过
- 原始 JSON：三份与 `02-workflows` 来源内容一致
- 4090 81 帧派生 JSON：除 `50.length` 从 33 改为 81 外无其他差异
- API 空载验证：`/health`、`/`、`/openapi.json` 均通过，OpenAPI 共 21 条路径
- API 工作流资源：四个私有工作流全部可发现
- Web 空载验证：Streamlit 首页 HTTP 200
- 敏感信息检查：未发现远端主机、SSH 命令、密码、Token 或私钥
- 测试结束状态：本地 API/Web 进程已关闭，收口阶段未连接或提交 GPU 任务

Windows 下启动 API/Web 验收前设置 `PYTHONIOENCODING=utf-8` 和 `PYTHONUTF8=1`，
避免启动横幅或中文日志在重定向输出时使用错误的系统代码页。

第一阶段不包含生产级任务持久化、跨进程并发、自动重试、用户鉴权、云端部署或
GPU 节点运维；这些属于后续阶段。
