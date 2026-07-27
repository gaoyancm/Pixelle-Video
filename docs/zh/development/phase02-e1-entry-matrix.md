# 阶段02-E1入口与旁路矩阵

本清单冻结阶段02-E1的迁移边界。E1只建立判定与单路由Facade；所有现有公开
入口仍保持原事实源、任务ID、HTTP状态和响应形状。表中“预定批次”不是实施授权。

| 入口族 | 可见性/模式 | 当前任务ID、状态与资产事实源 | 当前直调 | E1判定 | 当前状态/预定批次 |
|---|---|---|---|---|---|
| `POST /api/media/jobs`及查询/取消/重试 | 公开/异步 | SQLite `media_jobs`；`media_assets` | Worker内受控Adapter | 四叶子原生契约 | 已持久化；不经兼容Facade |
| `POST /api/video/generate/sync` | 公开/同步 | 无任务ID；调用栈与旧输出文件 | `PixelleVideoCore.generate_video` | `legacy_composite` | E1冻结；阶段03后另议 |
| `POST /api/video/generate/async` | 公开/异步 | 内存TaskManager；旧输出文件 | `PixelleVideoCore.generate_video` | `legacy_composite` | E1冻结；阶段03后另议 |
| `GET/DELETE /api/tasks` | 公开/异步控制 | 内存TaskManager | 无Provider直调 | 仅旧任务事实源 | E1冻结；随来源入口迁移 |
| image、TTS、frame旧API | 公开/同步 | 路径型结果/调用栈 | Provider、ComfyKit或本地执行 | 未声明进入Facade | 后续独立批次 |
| `PixelleVideoCore.generate_video` | Library/同步复合 | 文件型History/调用栈 | pipeline、Provider、ComfyKit | `legacy_composite` | E1冻结；阶段03后另议 |
| Library `media`四个私有工作流 | Library/同步叶子 | 路径型结果 | `ComfyUIAdapter.execute` | 可规范化为`persistent_leaf` | E2候选；E1不迁移 |
| Library `media`其他工作流 | Library/同步 | 路径型结果 | Provider或ComfyKit | 未声明进入Facade | 后续独立批次 |
| Streamlit Quick Create、批量、Asset、Digital Human | 内部Web/同步复合 | 文件型History与UI回调 | Core、Provider、ComfyKit | `legacy_composite` | E1冻结；阶段03后另议 |
| Streamlit I2V中四个私有工作流叶子 | 内部Web/同步叶子 | 路径型结果 | Adapter或ComfyKit | 可规范化为`persistent_leaf` | E2候选；E1不迁移 |
| Provider模块`__main__`示例 | 开发工具/同步 | Provider远端与本地文件 | 真实Provider客户端 | 未声明进入Facade | 精确旁路；后续另议 |
| `/api/files`与旧History | 公开下载/内部Web | 受控路径及JSON索引 | 不执行Provider | 非提交入口 | 保留兼容；不与资产双写 |

## 四个封闭叶子契约

稳定标识以`WORKFLOW_SPECS`为唯一来源。四者任务执行器均为
`private_comfyui`，Provider均为`private_comfyui`，输出由Worker登记到
`media_assets`并建立`output`关系。

| 标识 | 工作流文件 | 输入 |
|---|---|---|
| `a800_wan22_t2v_33f` | `video_a800_wan22_t2v_4step_33f_api.json` | 必填`prompt`；可选negative prompt、宽高、帧数、seed；禁止资产 |
| `a800_wan22_t2v_81f` | `video_a800_wan22_t2v_4step_81f_api.json` | 同上 |
| `gpu_4090_wan21_i2v_33f` | `video_4090_wan21_i2v_fp8_512x512_33f_api.json` | 必填`prompt`和一个可用image资产；另可选negative prompt、宽高、帧数、seed、steps、cfg |
| `gpu_4090_wan21_i2v_81f` | `video_4090_wan21_i2v_fp8_512x512_81f_api.json` | 同上 |

取消、deadline、租约、重试及`submission_unknown`继续完全由既有持久化任务
Repository、Worker和Executor负责；Facade不复制这些状态，也不执行Provider。

## E1架构门禁

扫描范围仅为`pixelle_video/media_migration`。允许名单为空。该区域不得导入或调用
`ComfyUIAdapter`、四叶子执行接口、`MediaJobRepository`、旧`TaskManager`，
也不得同时持有两类状态源。正向样例是当前纯判定与注入式单路由分发；负向样例
由架构测试中的临时源码覆盖。
