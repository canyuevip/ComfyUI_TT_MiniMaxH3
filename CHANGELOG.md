# 更新日志

本插件版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)，格式遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)。

本仓库为精简发行版：不含「🎵音频自动分段（人声分离+语音识别）」与「Qwen H3 提示词扩写（本地）」两个依赖大模型的节点，零自动下载、零外部运行时。分段计划请直接粘进控制台的「分段JSON」。

## [1.1.0] - 2026-10-14

### 变更

- 🎬 **音频分段串联·一键生成** `TTAudioChainConsole` 不再绑定分段来源：「分段JSON」从强制输入改成输入框，可直接粘贴任意来源的分段计划（`{"total": …, "segments": [{"start_time", "duration", "text"/"prompt"}, …]}`，`segments` 也接受裸数组），需要上游节点自动供给时右键「转换为输入」再接线（本版本不含语音识别节点，分段计划来自外部环节）。
- 「提示词」改为**公共提示词**：所有分段共用一条，本段识别文字与无缝衔接说明照旧自动拼接。
- 分段数、总时长、全文识别文字全部由「分段JSON」算出，不再作为输入接口暴露。
- 串联控制台面板移除「🔍 先分析」按钮：粘贴 JSON 后面板立刻解析并列出每段起止与文字；接上游时由本段执行回传的计划显示。

### 移除

- `TTAudioChainConsole` 的输入 `分段数`、`总时长_秒`、`全文文本`。
- `TTAudioChainConsole` 的输出 `info`、`分段ID`、`基础前缀`：进度信息在面板显示，段号已含在「文件名前缀」的 `segNNN` 里，合并节点用「文件名前缀」与「总段数」即可。保留 `start_time`、`duration`、`prompt`、`本段时长`、`文件名前缀`、`文字`、`总段数`、`分段JSON`、`完成信号`。

### 升级说明

旧工作流打开后需要重新提供分段计划（粘贴「分段JSON」，或把该输入转换回来重连上游节点），并检查原先接在 `info` / `分段ID` / `基础前缀` 上的连线。

## [1.0.0] - 2026-10-14

首个正式版本。

### 新增

- 🎵 **H3 音视频条件（口播/参考）** `TTH3AudioConditioning`：首尾帧 + 参考图/视频/音频 + 源音锁定，单节点产出 H3 原生条件；协议对齐 ComfyUI 核心官方节点（`minimax_keyframes` / `minimax_refs` / 嵌套 `noise_mask`），实现自包含。
- 🎞 **H3 音视频解码** `TTH3AVDecode`：联合 AV 潜空间一次解码出图像帧与生成音频，并拆出可复用的视频/音频潜空间。
- 🎬 **运动上下文四件套** `TTH3MotionContext` / `TTH3MotionContextTrim` / `TTH3MotionContextSaveLatent` / `TTH3MotionContextLoadLatent`：上一段尾部画面与音频作为运动上下文接入下一段，长视频口型与画面连贯；衍生自 ComfyUI-H3-Motion-Context（GPL-3.0）。
- 🎬 **音频分段串联·一键生成** `TTAudioChainConsole` + 📝 **逐段提示词取行** `TTChainPromptLine`：按分段计划逐段生成、服务端接力连跑，浏览器刷新/关闭不中断；支持每轮段数停点、末段自动合并与保存。
- 🧵 **接缝处理**：🎬视频合并（曝光锁定·接缝修复）`TTExposureMerge`、🧷桥帧门控 `TTBridgeFrameGate`、🩺接缝医生（五维 z-score）`TTSeamDoctor`。
- ⏱ **数字人/MV 时长裁剪** `TTDurationCrop`：生成视频精确对齐源音频时长，输出帧率自动重算。
- 🎬 **视频合并（分段拼接）** `TTVideoMerge`、🔊 **多音频加载器** `TTH3MultiAudioLoader`、🖼 **多图加载器** `TTH3MultiImageLoader`、🎚 **编组开关（按数量）** `TTGroupSwitch`、`AudioToAudioPath`（AUDIO → 音频文件路径，供外部语音识别工具使用）。
- 🎞 **HyperVAE 2x 分帧加载** `TTFANHyperVAE2xChunked`：降低长视频 pixel_shuffle 峰值显存；依赖 comfyui-minimax-h3-audio-T8，未安装时自动跳过注册。
- 前端面板（`js/`）：串联控制台、编组开关、多图/多音加载器面板，配套本地接口 `GET /tt/h3/image`、`GET /tt/h3/audio`、`POST /tt/audio_chain/reset`、`GET /tt/audio_chain/status`、`POST /tt/motion_context/clear_latents`、`POST /tt/video/merge`。
- 中英双语 README、`CHANGELOG.md`、`example_workflows/` 目录、`requirements.txt`、GPL-3.0 许可与第三方署名披露。

### 变更

- 条件与解码由第三方 T8 节点切换为本插件自研节点，接口改为中文（源音频 / 锁定源音），不再需要额外插件。
- 尾帧硬锚改为 Hybrid 参考：九段串联实测锚段出现口型冻结，关闭硬钉后累积劣化与冻结消失、接缝连续。
- 接力窗口取已交付的生成尾部并对齐 `target_seconds` 裁剪，修复段间开头重复/静止。
- 许可证确定为 GPL-3.0。

### 移除

- 15 个早期实验节点与尾帧锚门控节点。

[1.0.0]: https://github.com/canyuevip/ComfyUI_TT_MiniMaxH3/releases/tag/v1.0.0
