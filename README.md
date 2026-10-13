# ComfyUI TT_MiniMaxH3

面向 MiniMax H3 音视频模型的 ComfyUI 导演工具集：口播数字人分段生成、分段一键连跑、运动上下文接力、接缝质检修复、时长对齐裁剪。
仓库地址：[canyuevip/ComfyUI_TT_MiniMaxH3](https://github.com/canyuevip/ComfyUI_TT_MiniMaxH3)

English → [README_EN.md](README_EN.md)

> 本插件前身为 `ComfyUI_H3_Toolkit`，2026-10-14 起更名：节点 ID、分类与 HTTP 路由统一改为 `TT` / `TT_MiniMaxH3` 前缀。旧工作流里的 `H3Kit*` 节点类型需重新指向新节点。

## 功能介绍

TT_MiniMaxH3 是一套**自包含**的 MiniMax H3 长视频/口播工作流节点包。它不替换官方 H3 节点，而是补上官方链路缺的三件事：**按分段计划逐段生成、跑完自动接力下一段**、**上一段尾部画面/音频作为运动上下文引导下一段**、**自动合并并做接缝质检**。底层复用官方 `MiniMaxH3ImageToVideo` / `MiniMaxH3ReferenceToVideo` 的 conditioning 协议（`minimax_keyframes` / `minimax_refs` / 嵌套 `noise_mask`），因此与原生 H3 采样、CLIP Loader（`type` 必须选 `minimax`）直接兼容。

分段计划（每段的起始时间、时长、识别文字）由你提供：直接粘进控制台的**分段JSON**输入框，或用任意输出同格式 JSON 的节点接入——本仓库版本不含语音识别节点，详见[音频分段一键连跑](#音频分段一键连跑)。

### 核心能力

| 能力 | 说明 |
| --- | --- |
| 口播源音锁定 | 「🎵H3音视频条件」把源音频编码进 AV latent 的音频轨并置 `noise_mask=0`，采样只生成视频，口型/歌声与原音完全一致 |
| 首尾帧 + 参考生成 | 同一节点支持首帧、尾帧、最多 9 张参考图、最多 3 段参考视频（12fps 抽帧给 CLIP）、最多 3 段参考音频 |
| 联合 AV 解码 | 「🎞H3音视频解码」一次解码出图像帧与生成音频，并拆出可复用的视频/音频 latent |
| 分段计划驱动 | 「🎬音频分段串联·一键生成」完全以**分段JSON**（每段 `start_time`/`duration`/`text`）为权威，粘贴或接入皆可，不按总时长等分 |
| 一键连跑 | 提交整图后由后端进程内接力：逐段克隆图、递增段号、随机化 seed、直接入队，浏览器关掉也不影响 |
| 运动上下文接力 | 上一段尾部按 latent 步写入 `minimax_keyframes` 引导下一段，长视频口型与画面持续连贯 |
| 时长精确对齐 | 「⏱数字人/MV 时长裁剪」按目标秒数均匀抽帧并重算输出帧率，容器时长与源音频严格对齐 |
| 接缝质检与修复 | 「🧷桥帧门控」给尾帧打分；「🩺接缝医生」五维 z-score 定位坏接缝；「🎬视频合并（曝光锁定·接缝修复）」逐段修亮度并钉住段边界 |
| 编组开关 | 「🎚编组开关（按数量）」用一个整数控制画布前 N 个编组的开/关，跑批时只开需要的分支 |

## 主链路

一条完整的口播长视频链路如下，节点菜单分类即按此顺序排列：

```
模型 → video_vae → audio_vae ─┐
                              ├─ 🎵H3音视频条件 → KSampler → 🎞H3音视频解码 → 画面/音频
分段JSON（粘贴 / 上游节点）─┤                       ↑
                              ├─ 🎬音频分段串联·一键生成 → 📝逐段提示词取行
                              │        （逐段 start_time / duration / prompt / 文件名前缀）
上一段采样 latent → 💾H3上下文latent保存 → 📂H3上下文latent加载 → 🎬H3运动上下文 → 下一段条件
全部段完成 → 🎬视频合并 / 🎬视频合并（曝光锁定·接缝修复） → merged.mp4
```

* **必连**：`model` → `video_vae` → `audio_vae` → 条件节点；CLIP Loader 的 `type` 必须选 `minimax`。
* **可选**：首尾帧、参考图/视频/音频、源音频（锁定源音时必连）、运动上下文（`context_latent` 优先于 `context_frames`）。
* **输出**：条件节点给出正向 conditioning + 联合 AV latent + 透传源音频；解码节点输出 `images` / `audio` / 拆分后的视频与音频 latent。负向条件仍由官方 `CLIPTextEncode` 提供。

## 节点一览

### 条件与解码

| 节点 | ID | 说明 |
| --- | --- | --- |
| 🎵H3音视频条件（口播/参考） | `TTH3AudioConditioning` | 首尾帧/参考图/参考视频/参考音频 + 口播源音锁定，输出 conditioning + AV latent |
| 🎞H3音视频解码 | `TTH3AVDecode` | 联合 AV latent 一次解码出图像帧与生成音频，并拆分视频/音频 latent |

### 生成串联

| 节点 | ID | 说明 |
| --- | --- | --- |
| 🎬音频分段串联·一键生成 | `TTAudioChainConsole` | 分段计划来自**分段JSON**输入框（粘贴或转换为输入后接上游），逐段给出 `start_time`/`duration`/`prompt`/`文件名前缀` 并驱动后端连跑 |
| 🎬H3运动上下文（续接上一段） | `TTH3MotionContext` | 把上一段尾部画面/音频写成 keyframes 引导本段 |
| ✂H3运动上下文裁剪 | `TTH3MotionContextTrim` | 剪掉接力的暖场帧，只输出新生成部分 |
| 💾H3上下文latent保存 | `TTH3MotionContextSaveLatent` | 存 `{video, audio}` 双张量 safetensors（`h3_motion_context_av_v1`） |
| 📂H3上下文latent加载 | `TTH3MotionContextLoadLatent` | 按段号读取上一段 latent 直供接力，`clip_index=0` 表示首段 |
| 📝逐段提示词取行 | `TTChainPromptLine` | 按行或按 `=== SEGMENT n ===` 分块取用提示词，跟随链段号 |

### 视频处理

| 节点 | ID | 说明 |
| --- | --- | --- |
| 🎬视频合并（分段拼接） | `TTVideoMerge` | `segNNN` 直接拼接（`-c copy`）或重编码；可接「完成信号」收尾 |
| 🎬视频合并（曝光锁定·接缝修复） | `TTExposureMerge` | 逐段修开头亮度/跳变，再以首段中位亮度做有界增益对齐 |
| 🧷桥帧门控（尾帧质量） | `TTBridgeFrameGate` | 尾帧 Laplacian 清晰度 + 曝光打分，不合格自动回退更早的桥帧窗口 |
| 🩺接缝医生（五维z-score） | `TTSeamDoctor` | 运动/位移/颜色/清晰度/音频五维 z-score，只诊断不修图 |
| ⏱数字人/MV 时长裁剪 | `TTDurationCrop` | 按目标秒数均匀抽帧并重算输出帧率 |
| 🎞HyperVAE 2x 分帧加载 | `TTFANHyperVAE2xChunked` | 分块 pixel_shuffle 降低长视频解码峰值显存（需另装 `comfyui-minimax-h3-audio-T8`，未装自动跳过注册） |

### 音频处理 / 图片处理 / 工具

| 节点 | ID | 分类 | 说明 |
| --- | --- | --- | --- |
| 🔊MiniMax H3 多音频加载器 | `TTH3MultiAudioLoader` | 音频处理 | 最多 3 路音频路径 + 逐路起止修剪，缺失自动补静音 |
| Audio -> AudioPath (for WhisperX) | `AudioToAudioPath` | 音频处理 | `AUDIO` 落盘为 wav 路径，供 WhisperX 等外部语音识别工具产出分段 |
| 🖼MiniMax H3 多图加载器 | `TTH3MultiImageLoader` | 图片处理 | 最多 9 张参考图，九宫格缩略图 + 拖拽排序 + 上传 |
| 🎚编组开关（按数量） | `TTGroupSwitch` | 工具 | `开启数量=N` 即开启面板列表前 N 个编组，0 为全关 |

## 用法摘要

### 锁定源音（口播数字人，推荐）

1. 「🎵H3音视频条件」的**音频模式**选 `锁定源音`，并把口播音频连到**源音频**（此模式必须连，否则报错）。
2. 首帧连人物图；需要尾帧锚时连尾帧（官方按 `center` 缩放锚末帧）。
3. 幅面默认 `1344×768`，宽高必须能被 32 整除；`length` 默认 124 帧，会自动向上对齐到 `17n+5` 帧网格。
4. 采样后接「🎞H3音视频解码」，`images` + `audio` 即成片与音轨。

### 模型生成（文生/参考生音视频）

音频模式选 `模型生成`，源音频留空，音频由模型生成；参考素材请连**参考图/参考视频/参考音频**（提示词里用 `<Picture N>` / `<Video N>` / `<Audio N>` 引用，参考视频按 24fps 的 IMAGE 批次、至少 5 帧，抽帧给 CLIP 时按 12fps）。

### 长视频接力（多段连贯）

1. 每段采样输出的 latent 同时接「💾H3上下文latent保存」（`clip_index` = 本段号）。
2. 下一段用「📂H3上下文latent加载」读上一段（`clip_index` = 上一段号；首段填 0）。
3. 「🎬H3运动上下文」的 `context_length` 只能是 `5 / 22 / 39 / 56`（默认 22，只有这些是整数 latent 步），`audio_context_length` 默认 24（0 = 跟随视频窗口）。
4. `soften_alpha` 保持 0：实测 >0 会抹掉运动线索、产生发白僵图。
5. 「✂H3运动上下文裁剪」的 `trim_frames` 接运动上下文的 `trim_frames` 输出，剪掉接力暖场帧。

### 音频分段一键连跑

控制台按**分段JSON**逐段执行，不绑定分段来源。JSON 每段三个字段（秒），外层可以带 `total`：

```json
{"total": 21.0, "segments": [
  {"start_time": 0.0,  "duration": 11.2, "text": "第一段口播文字"},
  {"start_time": 11.2, "duration": 9.8,  "text": "第二段口播文字"}
]}
```

`segments` 也可以是裸数组；字段兼容 `start_time`/`start`、`text`/`prompt`；`duration <= 0` 的条目会被忽略。分段数、总时长、全文识别文字都由这份 JSON 算出，控制台不再单独暴露这些输入。

1. 用你自己的语音识别环节产出这份 JSON（WhisperX 等工具都行），段长建议 4–15 秒（H3 单段上限）。`AudioToAudioPath` 可把 ComfyUI 内的 `AUDIO` 落成 wav 供外部识别工具读取。
2. 把它粘进「🎬音频分段串联·一键生成」的**分段JSON**输入框，面板会立刻列出每段起止与文字；想在工作流里自动供给，就右键该输入 → 转换为输入，再接上游产出同格式 JSON 的节点。
3. **公共提示词**是所有分段共用的一条提示词；「识别文字并入提示词」开启时把本段识别文字拼在它前面，第 2 段起再追加无缝衔接指令。
4. **上下文帧数**（默认 22）必须与「🎬H3运动上下文」的 `context_length` 一致；第 2 段起会自动把 `start_time` 前移 帧数/帧率 秒。不做多段接力时设 0。
5. **文件名前缀**（默认 `h3_audio_chain`）决定输出目录 `output/h3_audio_chain/segNNN_*.mp4`。
6. 面板点 **▶ 开始生成** 提交整图；后端逐段克隆、递增段号、随机化 seed 并入队。**每轮段数** >0 时跑满即停，0 为跑到底；**停止连跑** 会把代数 +1 并清掉排队项。
7. 末段完成后，「🎬视频合并」接控制台的**完成信号**自动出 `merged.mp4`。

### 接缝质检与修复

* 「🧷桥帧门控」放在上一段解码之后：`窗口帧数` ≈ `context_length`，尾帧清晰度 z 低于 `-清晰度容差` 或曝光越界（默认 0.03–0.97）时自动回退 17/34 帧取更清晰的桥帧。
* 「🩺接缝医生」对相邻两段做五维 z-score（`报警阈值` 默认 3.0 ≈ 99.7% 置信带），输出诊断 JSON 定位坏接缝；它**只测不治**。
* 「🎬视频合并（曝光锁定·接缝修复）」默认输出 `merged_graded`：先修新段开头约 12 帧的低频亮度/色调，同镜头首帧跳变用运动外插补一帧（硬切不修），再以首段中位亮度为基准对后续段做 0.75–1.30 有界增益、段边界前 10 帧逐帧钉一次。慢、需重编码（libx264 crf19）。

## 依赖

**ComfyUI 核心**：需包含官方 MiniMax H3 节点与模型（`comfy_extras/nodes_minimax_h3.py`、`comfy/ldm/minimax/`）。本插件在 **ComfyUI 0.39.0** 上开发验证；另用到 `comfy_api`、`comfy.nested_tensor`、`comfy_extras.nodes_audio.vae_decode_audio`。

**Python 依赖**（`requirements.txt`）：

| 包 | 用途 | 缺失时表现 |
| --- | --- | --- |
| `torchaudio` | `AudioToAudioPath` 落盘 wav | 运行该节点时报错 |
| `imageio-ffmpeg` | 视频合并/接缝修复的 ffmpeg 来源之一 | 会回落到 `FFMPEG_PATH` / `bin/ffmpeg.exe` / 系统 PATH |

torch / numpy / Pillow / aiohttp / av / safetensors 由 ComfyUI 自带，无需重复安装。

**可选外部插件**：`TTFANHyperVAE2xChunked` 独立加载 `comfyui-minimax-h3-audio-T8` 的 `h3_t8/hyper_vae_2x.py`；未安装时该节点不注册，其余节点不受影响。

## 安装

### 方法一：手动安装（标准方式）

```bash
cd ComfyUI/custom_nodes
git clone https://github.com/canyuevip/ComfyUI_TT_MiniMaxH3.git
pip install -r ComfyUI_TT_MiniMaxH3/requirements.txt
```

重启 ComfyUI。节点分类在 `TT_MiniMaxH3` 下。本仓库版本不含任何模型自动下载与外部运行时安装步骤，装完即可离线运行。

### 方法二：ComfyUI Manager

1. 打开 ComfyUI Manager
2. Install via Git URL
3. 填入 `https://github.com/canyuevip/ComfyUI_TT_MiniMaxH3.git` 并安装
4. 重启 ComfyUI

## 外部资源

| 用途 | 位置 | 获取方式 |
| --- | --- | --- |
| H3 权重 / VAE / CLIP | `models/diffusion_models/`、`models/vae/`、`models/text_encoders/` | 走官方 MiniMax H3 资源，本插件不下载主模型 |

ffmpeg 查找顺序：环境变量 `FFMPEG_PATH` → 插件 `bin/ffmpeg.exe` → `imageio-ffmpeg` → 系统 PATH。仓库不含 ffmpeg 二进制。

## 快速开始（口播数字人）

1. 加载官方 H3 链路：UNet + CLIP Loader（`type=minimax`）+ `video_vae` + `audio_vae`。
2. 准备分段 JSON：用你自己的语音识别环节产出上面那段 `[{"start_time", "duration", "text"}]` 数组。
3. 连「🎬音频分段串联·一键生成」：把 JSON 粘进「分段JSON」框（或转换为输入后接上游），填公共提示词，确认上下文帧数与幅面。
4. 连「🎵H3音视频条件」：音频模式 `锁定源音`，源音频来自控制台的本段音频，首帧连人物图。
5. 采样输出（联合 AV latent）接「🎞H3音视频解码」取 `images` 与 `audio`；采样参数沿用官方 MiniMax H3 的推荐设置，本插件不改写 sampler 默认值。
6. 需要多段连贯时补上「💾保存 / 📂加载 / 🎬运动上下文 / ✂裁剪」四件套，或直接让控制台代跑段号。
7. 面板点 **▶ 开始生成**，末段完成后由「🎬视频合并」出 `merged.mp4`。

## 示例工作流

`example_workflows/` 预留了 3 条示例链路（单段口播、多段一键连跑、接缝质检修复），**目前尚未收录 JSON 文件**，计划与约定见 [example_workflows/README.md](example_workflows/README.md)。

## 常见问题

* **节点分类里找不到旧名字？** 插件已从 `H3工具` 更名为 `TT_MiniMaxH3`，节点 ID 前缀由 `H3Kit` 改为 `TT`，旧工作流需重新连节点。
* **分段 JSON 从哪来？** 本仓库版本不含语音识别节点：把外部识别环节产出的 JSON 直接粘进控制台的「分段JSON」输入框，或由任意上游节点输出该 JSON（文本节点、你自己的 WhisperX 类链路都行）；`AudioToAudioPath` 负责把 `AUDIO` 交给外部工具。
* **口型对不上/画面与音频时长不一致？** 用「⏱数字人/MV 时长裁剪」把生成段裁到源音频时长，它会重算输出帧率保证容器时长精确对齐。
* **多段接缝处闪一下/颜色跳变？** 先用「🩺接缝医生」定位是哪一维异常，再用「🎬视频合并（曝光锁定·接缝修复）」合并。
* **HyperVAE 2x 分帧加载节点没了？** 需另装 `comfyui-minimax-h3-audio-T8`，未装自动跳过注册。

## 面板与本地接口

浏览器面板（`js/`）通过以下本地接口工作，仅监听同源 ComfyUI 服务，不对外发送数据：

| 接口 | 用途 |
| --- | --- |
| `GET /tt/h3/image` | 多图加载器面板的绝对路径缩略图 |
| `GET /tt/h3/audio` | 多音频加载器面板的音频预览（支持 Range 拖动进度条） |
| `POST /tt/audio_chain/reset` | 停止连跑（代数 +1 并清掉排队项） |
| `GET /tt/audio_chain/status` | 查询链状态与分段计划（刷新页面后恢复面板） |
| `POST /tt/motion_context/clear_latents` | 清空 `h3_context` 编号槽文件 |
| `POST /tt/video/merge` | 供外部脚本触发的合并入口 |

## 版本与更新日志

当前版本：**v1.1.0**（2026-10-14）。完整变更记录见 [CHANGELOG.md](CHANGELOG.md)，打包发行见 [Releases](https://github.com/canyuevip/ComfyUI_TT_MiniMaxH3/releases)。

本插件有两个发行版本，同一套代码、同一个版本号：

| 版本 | 仓库 | 内容 |
| --- | --- | --- |
| 公开精简版 | 本仓库 | 18 个节点，不含「🎵音频自动分段」与「Qwen H3 提示词扩写（本地）」，零自动下载、零外部运行时 |
| 完整版 | `ComfyUI_TT_MiniMaxH3-full`（私有，需授权访问） | 20 个节点，含上述两个依赖大模型的节点 |

## 致谢

* [Comfy-Org/ComfyUI](https://github.com/Comfy-Org/ComfyUI) — 官方 MiniMax H3 支持与 conditioning 协议
* [MiniMax-AI](https://github.com/MiniMax-AI) — MiniMax H3 模型
* [NikoDemon80/ComfyUI-H3-Motion-Context](https://github.com/NikoDemon80/ComfyUI-H3-Motion-Context) — 运动上下文四件套的衍生来源

## 许可证

* 本插件以 **GPL-3.0** 发布（见 [LICENSE](LICENSE)），© 2026 canyuevip。
* `motion_ctx/` 衍生自 **NikoDemon80/ComfyUI-H3-Motion-Context**（GPL-3.0，© 2026 NikoDemon80），保留四个基础节点并接入本插件的一键连跑体系。
