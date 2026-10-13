# 示例工作流 / Example workflows

中文 | [English](#english)

本目录用于存放可直接拖入 ComfyUI 的示例工作流 JSON。**当前尚未收录任何工作流**，占位计划如下。

| 计划文件 | 链路 | 需要的模型 | 状态 |
| --- | --- | --- | --- |
| `tt_h3_talking_head.json` | 单段口播：条件（锁定源音） → 采样 → AV 解码 | H3 UNet + video/audio VAE + `minimax` CLIP | 待补充 |
| `tt_h3_long_chain.json` | 多段一键连跑：分段JSON → 控制台 → 上下文接力 → 合并 | 同上 | 待补充 |
| `tt_h3_seam_repair.json` | 接缝质检与修复：桥帧门控 + 接缝医生 + 曝光锁定合并 | 同上 | 待补充 |

约定：

- 示例只存工作流 JSON，**不打包模型、素材与个人图片**；素材请自行替换。
- 分段JSON 由示例内的文本节点提供（本仓库版本不含语音识别节点）。
- 节点类型使用 `TT*` 前缀（`H3Kit*` 为更名前的旧类型，已不再注册）。
- 采样参数沿用官方 MiniMax H3 推荐值，示例不额外改写。

## English

This folder is meant to hold example workflow JSON files that can be dragged straight into ComfyUI. **No workflows are included yet**; the planned set is listed in the table above.

- Examples contain workflow JSON only — no models, media or personal images.
- Node types use the `TT*` prefix; the old `H3Kit*` types are no longer registered.
- Sampler settings follow the official MiniMax H3 recommendations.
