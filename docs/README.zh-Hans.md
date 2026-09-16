# YouTubeText

[English](../README.md) | [繁體中文](README.zh-Hant.md) | 简体中文 | [Español](README.es.md) | [日本語](README.ja.md)

YouTubeText 是一款适用于 Apple Silicon macOS 与 Windows x64 的终端工具，可将 YouTube 和
Bilibili 视频转换为干净且带时间戳的文字稿。它会导出一份带时间戳的
Markdown 文件、一份不含时间戳的干净 Markdown 文件、一份文本文件，以及
结构化元数据。

YouTubeText 只提取完整文字。它不会总结视频、分析论点，也不需要 LLM、
Ollama 或云端 AI API。

## 功能

- 在同一个命令中提交一个 URL 或一列 URL 队列。
- 检测 YouTube 和 Bilibili 来源并读取其元数据。
- 有可用字幕时，优先采用平台提供的人工或自动字幕。
- 没有可用的平台字幕轨时，在 macOS 使用 Apple Vision、在 Windows 使用 RapidOCR 读取内嵌字幕。
- OCR 无法使用时，在 macOS 回退到本地 MLX Whisper、在 Windows 回退到 faster-whisper 语音识别。
- 同时导出带时间戳和干净、不含时间戳的 Markdown 文字稿。
- 根据物理内存，以及 Windows 上的 CPU 容量，自动选择 URL 并行数。
- 复用已完成的文字稿、续传中断的媒体下载，以及复用已完成的 OCR 批次。
- 下载任何字幕或媒体文件前，使用 `--plan` 检查 URL。
- 隔离失败：单个错误 URL 不会取消队列中的其他任务。

## 处理流程

```text
URL 队列
  └─ 检测平台并读取元数据
       ├─ 可用的平台字幕 ─────────────────→ 清理字幕段落 ───────→ 导出
       └─ 无可用字幕
            ├─ --resume 缓存命中 ─────────────────────────────→ 导出
            └─ 缓存未命中或未启用断点续跑 → 分析视频 → 本地 OCR
                                                        ├─ 可用 → 导出
                                                        └─ 不可用 → Whisper → 导出
```

平台字幕路径绝不下载视频。字幕文件是小型的临时输入。普通运行会将 OCR 和
Whisper 媒体保存在受监管的单次运行临时目录中，并在运行结束时移除。使用
`--resume` 时，媒体会存储在用户缓存的 `tasks/` 目录下，在中断后保留，
并在成功后移除。

## 系统要求

- Apple Silicon Mac (`arm64`) 与 macOS 13 或更高版本，或 64 位 x86 Windows
- Python 3.11–3.14
- `PATH` 中可用的 FFmpeg
- 仅 macOS：用于构建 Apple Vision OCR 辅助程序的 Xcode Command Line Tools
- 建议使用 `uv`

在 macOS 上，如果尚未安装 FFmpeg，请使用 Homebrew 安装：

```bash
brew install ffmpeg
```

在 macOS 上，如果尚未安装 Apple 命令行工具，请运行：

```bash
xcode-select --install
```

## 安装

两个平台当前支持的安装方式都是签出源代码。YouTubeText 尚未发布为 PyPI 软件包或预构建
wheel。

在 macOS 上运行：

```bash
git clone https://github.com/yuchenzhu-research/YouTubeText.git
cd YouTubeText
./scripts/install.sh
```

如果已经配置 GitHub SSH：

```bash
git clone git@github.com:yuchenzhu-research/YouTubeText.git
```

在 Windows x64 上，先安装 64 位 Python 和 FFmpeg，将 FFmpeg 的 `bin` 目录
加入 `PATH`，然后在 PowerShell 运行：

```powershell
git clone https://github.com/yuchenzhu-research/YouTubeText.git
Set-Location YouTubeText
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\install.ps1
```

两个安装程序都会创建 `.venv` 并安装对应平台的 Python 依赖项。macOS 安装程序
还会构建 Apple Vision OCR 辅助程序；Windows 安装程序不使用 Swift。两者都不会
安装 FFmpeg。macOS 安装程序不会使用 `sudo`，也不会自动安装 Homebrew。

在 macOS 安装后检查本地环境：

```bash
./.venv/bin/youtubetext doctor
```

## 快速开始

处理一个视频：

```bash
./.venv/bin/youtubetext "https://www.youtube.com/watch?v=VIDEO_ID"
```

将 YouTube 和 Bilibili URL 加入队列，并同时运行两个 URL 任务：

```bash
./.venv/bin/youtubetext \
  "https://www.youtube.com/watch?v=VIDEO_ID" \
  "https://www.bilibili.com/video/BV_ID" \
  --jobs 2
```

选择输出目录，并提示原始文字可能是繁体中文：

```bash
./.venv/bin/youtubetext URL \
  --output ~/Desktop/YouTubeText-output \
  --language zh-Hant
```

返回机器可读的结果：

```bash
./.venv/bin/youtubetext URL --json
```

显示所有选项：

```bash
./.venv/bin/youtubetext --help
```

在 Windows 上，请从 PowerShell 使用 `.venv\Scripts` 中的可执行文件，例如：

```powershell
.\.venv\Scripts\youtubetext.exe doctor
.\.venv\Scripts\youtubetext.exe "https://www.youtube.com/watch?v=VIDEO_ID"
.\.venv\Scripts\youtubetext.exe URL_1 URL_2 --jobs 2
.\.venv\Scripts\youtubetext.exe URL --plan
.\.venv\Scripts\youtubetext.exe URL --resume
.\.venv\Scripts\youtubetext.exe cache
```

下文其余命令示例使用 macOS 的可执行文件路径。在 Windows 上，请将
`./.venv/bin/youtubetext` 替换为 `.\.venv\Scripts\youtubetext.exe`。

## 执行前计划

使用 `--plan` 检查全新运行将采取的操作；此时尚未下载任何字幕或媒体文件：

```bash
./.venv/bin/youtubetext URL --plan
./.venv/bin/youtubetext URL_1 URL_2 --plan --json
```

执行前计划会为每个 URL 发出网络元数据请求，并报告：

- 来源元数据和时长；
- 平台声明的最佳字幕轨；
- 预计使用的字幕、OCR 和 Whisper 路径；
- 字幕或媒体下载是 `required`、`conditional` 还是 `none`；
- 主要媒体类型和任何音频回退方案。

声明的字幕轨会标记为 `advertised-unvalidated`：执行前计划不会下载或解析它，
因此尚无法保证可用性。执行前计划不会下载字幕、音频、视频或 Whisper 模型；
不会采样帧或运行 OCR/Whisper；不会创建输出目录；也不会读取或写入断点续跑状态。

计划采用缓存假设 `no-resume-reuse`。因此，稍后使用 `--resume` 运行时，可能会
跳过计划中显示为必需的工作。`--plan` 和 `--resume` 不能同时使用。多个 URL
会并行检查，结果保持输入顺序，且一个元数据失败不会隐藏其他计划。

## 断点续跑与缓存

明确启用可重复使用的本地状态：

```bash
./.venv/bin/youtubetext URL --resume
```

YouTubeText 仍会先检查平台是否提供了新的字幕。缓存未命中时，yt-dlp 可继续
中断的 `.part` 下载；已完成的媒体文件会直接重复使用；本地 OCR 引擎则会在
每批最多 32 帧全部成功识别后保存原始 OCR 观察结果。含有帧错误的批次不会写入
检查点，而会在下次运行时重试。采样图像会在识别后逐批删除；如果有写入检查点，
则会先安全保存再删除。Whisper 推理目前还不能从中间位置断点续跑。

在 macOS 上，已完成的结构化文字稿存储在：

```text
~/Library/Caches/YouTubeText/transcripts/
```

在 Windows 上，缓存位于用户的平台缓存目录；运行 `youtubetext cache` 可查看
准确位置。两个平台的未完成任务都存储在同级的 `tasks/` 目录中。在 macOS 上，
目录和文件使用 `0700` 和 `0600` 权限；Windows 则使用其文件系统权限。
两个进程请求相同任务时会共用一把锁：一个执行
工作，另一个等待并重复使用其结果。匿名请求和不同的 Cookie 文件会使用各自
独立的缓存范围。

在不修改缓存的情况下检查缓存用量：

```bash
./.venv/bin/youtubetext cache
./.venv/bin/youtubetext cache --json
```

在 macOS 上，只移除失败或中断任务的媒体和 OCR 检查点，同时保留所有已完成的文字稿：

```bash
./.venv/bin/youtubetext cache clear-incomplete
```

运行中且已锁定的任务会被跳过。已移除的临时媒体无法恢复，但可以从原始 URL
重新下载。在 Windows 上，`cache clear-incomplete` 暂不支持，不会删除任务数据；
只读的 `cache` 和 `cache --json` 仍可使用。若要在 macOS 移除包括已完成文字稿
在内的所有内容，请手动删除 `~/Library/Caches/YouTubeText/`。

## 身份验证与 Cookie

针对受限视频，使用本地浏览器的登录状态：

```bash
./.venv/bin/youtubetext URL --cookies-from-browser safari
```

或者提供 Netscape 格式的 Cookie 文件：

```bash
./.venv/bin/youtubetext URL --cookies-file /path/to/cookies.txt
```

这两个选项互斥。Cookie 内容绝不会写入输出目录、元数据、JSON 结果或断点续跑
缓存。每次调用 yt-dlp 时，Cookie 文件会被复制到内存中；原始文件不会被修改。

由于浏览器名称无法可靠识别当前使用的账号，`--cookies-from-browser` 不能与
`--resume` 一起使用。需要同时使用身份验证和断点续跑时，请使用
`--cookies-file`。Safari 可能需要在 macOS“隐私与安全性”设置中，授予终端
“完全磁盘访问权限”。Safari 示例仅适用于 macOS；在 Windows 上请选择
yt-dlp 支持且已安装的浏览器。

## 模式

| 模式 | 行为 |
| --- | --- |
| `auto` | 优先使用平台字幕，然后是 OCR，最后是 Whisper。 |
| `captions` | 要求平台字幕轨；没有可用字幕时失败。 |
| `ocr` | 忽略平台字幕，只读取内嵌字幕。 |
| `whisper` | 跳过字幕和 OCR；直接运行本地语音识别。 |
| `hybrid` | 运行 OCR 和 Whisper；当两者时间范围重叠时，画面可见字幕优先。 |

例如，强制使用 OCR：

```bash
./.venv/bin/youtubetext URL --mode ocr --language zh-Hant
```

## 语言

`--language` 支持：

```text
auto, en, zh-Hans, zh-Hant, es, ja, ko, fr, de, pt, it, ru, ar, hi, vi
```

`--language` 是原始语音或画面文字的语言提示，不是指定输出语言。它会影响
平台字幕偏好、可用 OCR 的识别语言，以及 Whisper 的语音语言选择。使用
Whisper 时，`zh-Hans` 和 `zh-Hant` 都会传成 `zh`；结果可能标记为
`zh`，即使选择了 `zh-Hant` 也可能输出简体字。本工具不会翻译或进行简繁转换。

在 macOS 选择 `auto` 时，Apple Vision 会利用标题和作者上下文设置识别语言的
优先顺序。Windows RapidOCR 默认的 PP-OCRv6 small 模型可识别 `en`、
`zh-Hans`、`zh-Hant`、`es`、`ja`、`fr`、`de`、`pt`、`it`、`vi`，
无需切换模型；`--language` 不会将输出限制为单一语言。该模型的 OCR 不支持
`ko`、`ru`、`ar`、`hi`：强制 OCR 模式会报错，`auto` 和 `hybrid`
会显示警告并改用 Whisper。平台字幕与 Whisper 仍可使用这些语言。
两种 Whisper 引擎都能自动检测口语语言。

另行设置一份按优先顺序排列的平台字幕语言列表：

```bash
./.venv/bin/youtubetext URL \
  --caption-language zh-Hant \
  --caption-language zh-Hans \
  --caption-language en
```

## 输出文件

默认输出根目录是当前目录中的 `YouTubeText-output/`：

```text
YouTubeText-output/
  VIDEO_ID-video-title/
    transcript.md
    transcript-clean.md
    transcript.txt
    metadata.json
```

- `transcript.md`：包含每个片段时间戳的完整 Markdown 文字稿。
- `transcript-clean.md`：不含时间轴标记的完整 Markdown 文字稿。
- `transcript.txt`：含时间戳的纯文本。
- `metadata.json`：来源、语言、提取方法、片段数量和警告。

## 并行与资源控制

`--jobs 0` 是默认值，会根据物理内存推导 URL 并行数上限：

| 物理内存 | 自动 URL 任务数 |
| --- | ---: |
| 少于 12 GiB | 1 |
| 12–23 GiB | 2 |
| 24–39 GiB | 3 |
| 40 GiB 或更多 | 4 |

在 Windows 上，CPU 容量可能进一步降低自动并行数。使用 `--jobs 1` 到
`--jobs 8` 覆盖此设置。网络工作最多使用四个槽位；macOS 的 Apple Vision OCR
最多使用两个，Windows 的 RapidOCR 使用一个。两个平台都会串行运行 Whisper，
以限制内存和计算资源争用。

OCR 通常每秒采样一帧，并将长视频限制为最多 2,400 帧。本地 OCR 引擎每批接收最多
32 个裁剪帧，并且这些图像在识别后始终逐批移除。使用 `--resume` 时，全部
成功识别的批次会在删除前写入检查点；含有帧错误的批次不会写入检查点，并会在
下次运行时重试。检查点会存储时间戳、内容哈希和原始文本框，绝不会保留帧图像
或临时路径。

## Whisper 模型

Whisper 只会在字幕和内嵌字幕均不可用，或选择 `whisper` / `hybrid` 模式时运行。
模型会在首次使用时下载，然后从本地模型缓存中复用。在 macOS 上，标准
Hugging Face 缓存中已有的兼容 MLX 权重也会复用。Windows 使用 faster-whisper
和独立缓存中的 CTranslate2 权重。

下表的下载量和内存估计仅适用于 macOS 的 MLX 引擎；Windows 的模型大小和
运行时内存需求可能不同：

| 模型 | 近似下载大小 | 近似运行时内存 |
| --- | ---: | ---: |
| `base` | 144 MB | 1 GiB |
| `small` | 481 MB | 2 GiB |
| `large-v3-turbo` | 1.61 GB | 6 GiB |

自动选择会在内存较少的 Mac 上使用 `small`，在至少有 16 GiB 内存的 Mac 上
使用 `large-v3-turbo`；Windows 默认使用 `small`。使用 `--whisper-model`
可覆盖此设置。Windows 的 doctor 会检查后端是否可用，但目前不会检查
CTranslate2 模型缓存。

## 开发与架构

运行完整的开发工作流：

```bash
./scripts/dev.sh
```

需要时，通过脚本传入 pytest 参数：

```bash
./scripts/dev.sh tests/test_acquisition.py -q
```

在 Windows 上，请从 PowerShell 运行开发脚本：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\dev.ps1
```

主要模块：

- `sources/`：YouTube/Bilibili 元数据和平台字幕。
- `planning.py`：只读的执行前路径和下载要求。
- `media.py`：可断点续跑的媒体下载和有界帧采样。
- `resume.py`：文字稿重复使用、任务锁、OCR 检查点和任务清理。
- `ocr/`：Apple Vision 或 RapidOCR 与内嵌字幕组装。
- `asr/`：MLX Whisper 或 faster-whisper 与本地模型选择。
- `acquisition.py`：字幕/OCR/Whisper 的权威路由策略。
- `runtime.py`：主机资源检测和保持顺序的多 URL 调度。
- `export.py`：Markdown、TXT 和 JSON 输出的分组原子写入。
- `cli.py`：终端界面。

## 当前限制

- macOS Apple Silicon 与 Windows x64 已通过自动化 CI 测试。Windows CI 还使用真实本地 OCR、Whisper 模型，以及离线的命令行到导出全流程进行了验证；实体 Windows 电脑上的真实视频端到端验证仍待完成，公开视频网址也可能拒绝托管 CI 机器。
- Windows 的 `cache clear-incomplete` 暂不可用；跨 junction 和 reparse point 的安全删除仍需进一步验证。
- 平台访问取决于 yt-dlp，并可能受到地区、账号、Cookie 或平台变更的影响。
- Apple Vision OCR 已针对画面底部附近的字幕进行优化。其他布局或高度风格化的
  文字可能需要使用 Whisper 模式。
- 已在 macOS 上实际验证 Bilibili 视频的端到端转录；平台字幕兼容性仍需在更多
  公开视频上进行测试。
- Whisper 无法从推理过程的中间位置断点续跑。

## 参考资料与声明

本设计参考了 [MediaBrief](https://github.com/EvilIrving/mediabrief)。本地参考
签出可保留在 `reference/mediabrief/` 下；整个 `reference/` 目录已被有意忽略，
且不属于此仓库。第三方软件声明请参阅
[`THIRD_PARTY_NOTICES.md`](../THIRD_PARTY_NOTICES.md)。
