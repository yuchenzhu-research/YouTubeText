# YouTubeText

[English](../README.md) | [繁體中文](README.zh-Hant.md) | 简体中文 | [Español](README.es.md) | [日本語](README.ja.md)

YouTubeText 是一款适用于 Apple Silicon macOS 与 Windows x64 的终端工具，可将 YouTube 和
Bilibili 视频及本地音频／视频文件转换为干净且带时间戳的文字稿。它会导出一份带时间戳的
Markdown 文件、一份不含时间戳的干净 Markdown 文件、一份文本文件，以及
SRT/WebVTT 字幕、结构化文字稿片段和元数据。

YouTubeText 提取文字稿。它不会总结视频、分析论点，也不需要 LLM、
Ollama 或云端 AI API。

## 功能

- 在同一个队列中提交 URL、本地路径和 `file://` 输入。
- 检测 YouTube 和 Bilibili 来源并读取其元数据。
- 优先采用平台人工或自动字幕，最多尝试五条候选字幕轨。
- 将相邻 SRT/WebVTT 文件读取为 `local-captions`，不复制来源媒体。
- 没有可用字幕时，在 macOS 使用 Apple Vision、在 Windows 使用 RapidOCR 读取内嵌字幕。
- OCR 不可用时，在 macOS 回退到 MLX Whisper、在 Windows 回退到 faster-whisper。
- 导出带时间戳和不含时间戳的 Markdown、TXT、SRT、WebVTT、逐段 JSON 和元数据。
- 根据物理内存，以及 Windows 上的 CPU 容量，自动选择输入并行数。
- 复用已完成的文字稿、媒体下载、OCR 批次和 Whisper 分块。
- 使用 `--ocr-profile` 和 `--ocr-region` 选择 OCR 采样密度和字幕区域。
- 转录或下载媒体前，使用 `--plan` 检查输入。
- 隔离失败：单个错误输入不会取消队列中的其他任务。

## 处理流程

```text
INPUT 队列（URL、本地路径、file://）
  └─ 读取来源元数据
       ├─ 可用的平台字幕／本地字幕文件 → 清理字幕片段 → 导出
       └─ 无可用字幕
            ├─ --resume 缓存命中 → 导出
            ├─ 本地音频 → Whisper → 导出
            └─ 远端／本地视频 → 流式 OCR
                                  ├─ 可用 → 导出
                                  └─ 不可用 → Whisper → 导出
```

字幕和可用 OCR 路径都会在导出前，按下方条件进行时间空白检查。

平台字幕下载或解析失败时，会按现有偏好顺序，最多尝试五条平台提示的语言／类型
候选字幕。所有尝试均失败时，会以长度受限的警告说明回退原因，不使用额外云端 API。

平台字幕没有可疑时间空白时，不需要下载媒体。字幕文件是小型的临时输入。
在 `auto` 模式下，可疑的字幕空白可能触发仅音频下载，用于检查和 Whisper；
这份音频会保存在运行后移除的临时目录中。普通运行会将 OCR 和
Whisper 媒体保存在受监管的单次运行临时目录中，并在运行结束时移除。使用
`--resume` 时，媒体会存储在用户缓存的 `tasks/` 目录下，在中断后保留，
并在成功后移除；OCR 空白检查仍待完成时则保留。

## 完整性检查

导出前，YouTubeText 会检查文字稿片段之前、之间和之后的长时间空白。
可疑空白阈值为视频时长的 10%，最低 60 秒、最高 300 秒。短于 90 秒或
时长未知的视频不使用此规则判断。

在 `auto` 模式下，可用的平台字幕、本地字幕文件或 OCR 结果若有可疑空白，会接受 FFmpeg
静音检查。OCR 复用分析视频；远端平台字幕只下载音频进行检查，本地字幕文件则就地读取原始媒体。空白区间
若是静音，就保留原文；若有非静音音频或无法完成检查，就运行 Whisper，
只合并与可疑空白重叠的 Whisper 片段，保留原文。Whisper 若失败，仍会保留原文字稿并明确警告
无法确认完整性。强制 `captions` 和 `ocr` 模式会保留所选方法，返回长时间
空白警告，不会额外启动语音识别。

两份 JSON 文件和 `--json` 结果都包含 `completeness` 报告，字段有 `status`、`coverage_ratio`、
`duration_seconds`、`covered_seconds` 和 `long_gaps`（每项包含 `start_seconds`
和 `end_seconds`）。`needs-review` 或 `unverified` 等状态只记录评估结果，
任何状态都不保证文字稿完整。所有警告也会显示在终端。

实际联网验证使用一支 [TED 视频](https://www.youtube.com/watch?v=iG9CE55wbtY)
（1,203 秒、427 个英文人工字幕片段），成功导出全部七种文件。重新解析 SRT 和
WebVTT 后，文字与 `segments.json` 一致，时间误差不超过 0.51 毫秒。
[仅含时间的测试数据](../tests/fixtures/ted_caption_timing.json)不保留字幕文字，
覆盖四项离线回归测试：原始时间，以及人为加入的开头、中段、结尾遗漏。
此验证覆盖平台字幕导出；更广泛的真实视频 OCR/Whisper CER/WER 基准测试仍待执行。

合成媒体基本测试也实际运行了 MLX Whisper `base`：17 个英文单词的音频没有单词
编辑错误；原生 Apple Vision 以 `accurate` 处理 6 秒视频，识别出全部三段各 2 秒的字幕。
这些小型合成样例尚无法建立更广泛的真实视频准确率。

实际 307.5 秒合成音频测试在第一个 300 秒分块完成后中断，再次运行时只处理剩余
分块，且临时音频分块已移除。

## 系统要求

- Apple Silicon Mac (`arm64`) 与 macOS 13 或更高版本，或 64 位 x86 Windows
- Python 3.11–3.14
- `PATH` 中可用的 FFmpeg 和 FFprobe
- 仅 Mac 源码构建：用于构建 Apple Vision OCR 辅助程序的 Xcode Command Line Tools
- 建议使用 `uv`

在 macOS 上，如果尚未安装 FFmpeg，请使用 Homebrew 安装：

```bash
brew install ffmpeg
```

在 Mac 从源码构建时，如果尚未安装 Apple 命令行工具，请运行：

```bash
xcode-select --install
```

## 安装

两个平台都继续支持从源码安装。0.2.0 的 macOS ARM64 wheel 已构建，并在源码
目录之外的全新环境完成基本验证；它内附 Apple Vision 辅助程序，运行时不需要 Swift
编译器。Windows CI 已配置为构建通用 wheel，但这份新版 wheel 尚未在 Windows 验证。
目前尚未发布到 PyPI 或创建 GitHub Release。

取得已构建的 macOS wheel 后，可运行：

```bash
python -m pip install ./youtubetext-0.2.0-py3-none-macosx_13_0_arm64.whl
```

已构建的 Windows wheel 可在 PowerShell 安装：

```powershell
python -m pip install .\youtubetext-0.2.0-py3-none-any.whl
```

仍须另行安装 FFmpeg/FFprobe 和 Python 3.11–3.14；语音模型会在首次使用时下载。
Mac 从源码安装（包括 `youtubetext-0.2.0.tar.gz`）仍需要 Xcode Command Line Tools。
以下是检出源码的安装方式。

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
./.venv/bin/youtubetext INPUT \
  --output ~/Desktop/YouTubeText-output \
  --language zh-Hant
```

返回机器可读的结果：

```bash
./.venv/bin/youtubetext INPUT --json
```

显示所有选项：

```bash
./.venv/bin/youtubetext --help
```

在 Windows 上，请从 PowerShell 使用 `.venv\Scripts` 中的可执行文件，例如：

```powershell
.\.venv\Scripts\youtubetext.exe doctor
.\.venv\Scripts\youtubetext.exe "https://www.youtube.com/watch?v=VIDEO_ID"
.\.venv\Scripts\youtubetext.exe INPUT_1 INPUT_2 --jobs 2
.\.venv\Scripts\youtubetext.exe INPUT --plan
.\.venv\Scripts\youtubetext.exe INPUT --resume
.\.venv\Scripts\youtubetext.exe cache
```

下文其余命令示例使用 macOS 的可执行文件路径。在 Windows 上，请将
`./.venv/bin/youtubetext` 替换为 `.\.venv\Scripts\youtubetext.exe`。

## 本地输入

命令行接受 `INPUT [INPUT...]`：远端 URL、本地音频／视频路径，或指向这台电脑的
`file://` URL。含有空格的路径必须加上引号。

```bash
./.venv/bin/youtubetext "/path/to/video.mp4" "/path/to/audio.wav" --resume
./.venv/bin/youtubetext "file:///path/to/video.mp4" --plan
```

原始文件会就地读取，绝不复制到任务存储空间或删除。来源字节改变时，SHA-256
内容标识会使旧缓存失效。相邻的同名字幕文件，例如 `video.vtt` 或 `video.srt`，
会在 `auto` 或 `captions` 模式读取为 `local-captions`；两者都有时优先使用 WebVTT。
没有可用字幕文件时，`auto` 会将音频交给 Whisper，对视频则应用 OCR 再回退到 Whisper
的一般策略。`ocr` 和 `hybrid` 需要视频流。本地 `--plan` 使用 FFprobe 并读取内容哈希，
不联网、不下载媒体或模型。

## 执行前计划

使用 `--plan` 检查全新运行将采取的操作；此时尚未下载任何字幕或媒体文件：

```bash
./.venv/bin/youtubetext INPUT --plan
./.venv/bin/youtubetext INPUT_1 INPUT_2 --plan --json
```

远端 URL 的执行前计划会发出网络元数据请求；本地输入则就地检查。返回内容包括：

- 来源元数据和时长；
- 平台声明的最佳字幕轨；
- 预计使用的字幕、OCR 和 Whisper 路径；
- 字幕或媒体下载是 `required`、`conditional` 还是 `none`；
- 主要媒体类型和任何音频回退方案。

声明的字幕轨会标记为 `advertised-unvalidated`：执行前计划不会下载或解析它，
因此尚无法保证可用性。执行前计划不会下载字幕、音频、视频或 Whisper 模型；
不会采样帧或运行 OCR/Whisper；不会创建输出目录；也不会读取或写入断点续跑状态。

计划采用缓存假设 `no-resume-reuse`。因此，稍后使用 `--resume` 运行时，可能会
跳过计划中显示为必需的工作。`--plan` 和 `--resume` 不能同时使用。多个输入
会并行检查，结果保持输入顺序，且一个元数据失败不会隐藏其他计划。

## 断点续跑与缓存

明确启用可重复使用的本地状态：

```bash
./.venv/bin/youtubetext INPUT --resume
```

远端 URL 仍会先检查最新的平台字幕；本地字幕文件则重新读取。缓存未命中时，yt-dlp 可继续
中断的 `.part` 下载；已完成的媒体文件会直接重复使用；本地 OCR 引擎则会在
每批最多 32 帧全部成功识别后保存原始 OCR 观察结果。含有帧错误的批次不会写入
检查点，而会在下次运行时重试。采样图像会在识别后逐批删除；如果有写入检查点，
则会先安全保存再删除。

启用 `--resume` 时，Whisper 会为每个已完成分块保存检查点。每块最多 300 秒，
与下一块重叠 2 秒；磁盘上同时只有一个临时音频分块。媒体内容、模型、语言、
后端版本和分块配置一致时，可复用已完成分块；中断的分块会重新运行。
重叠的重复片段会保守去重，不合并不同文字或发生于不同时间的重复语句。
短文件可直接运行；无法判断本地媒体时长时，Whisper 会直接运行，不保存分块检查点。

OCR 空白的额外 Whisper 检查若失败，仍会导出原文字稿，但启用 `--resume` 时，
媒体和 OCR 检查点会保留为未完成任务，不会写入已完成文字稿缓存。下次
`--resume` 会重试检查，并复用已下载成功的媒体和已完成的 OCR 批次。
字幕检查始终使用运行后移除的临时目录，即使启用 `--resume`，也会重新检查平台字幕。

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

在 macOS 上，只移除失败或中断任务的媒体和 OCR/Whisper 检查点，同时保留所有已完成的文字稿：

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
./.venv/bin/youtubetext INPUT --cookies-from-browser safari
```

或者提供 Netscape 格式的 Cookie 文件：

```bash
./.venv/bin/youtubetext INPUT --cookies-file /path/to/cookies.txt
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
| `auto` | 优先使用平台字幕或本地字幕文件，再对视频使用 OCR、对音频或回退路径使用 Whisper。 |
| `captions` | 要求可用的平台字幕轨或本地 SRT/VTT 字幕文件。 |
| `ocr` | 忽略平台字幕，只读取内嵌字幕。 |
| `whisper` | 跳过字幕和 OCR；直接运行本地语音识别。 |
| `hybrid` | 运行 OCR 和 Whisper；当两者时间范围重叠时，画面可见字幕优先。 |

例如，强制使用 OCR：

```bash
./.venv/bin/youtubetext INPUT --mode ocr --language zh-Hant
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
./.venv/bin/youtubetext INPUT \
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
    transcript.srt
    transcript.vtt
    segments.json
    metadata.json
```

- `transcript.md`：包含每个片段时间戳的完整 Markdown 文字稿。
- `transcript-clean.md`：不含时间轴标记的完整 Markdown 文字稿。
- `transcript.txt`：含时间戳的纯文本。
- `transcript.srt`：含编号的字幕片段，可用于支持 SRT 的播放器和编辑器。
- `transcript.vtt`：WebVTT 字幕片段，可用于网页播放器和其他兼容工具。
- `segments.json`：文字稿全文，包含每个片段的开始和结束时间。
- `metadata.json`：来源、语言、提取方法、片段数量、警告和完整性评估。

`segments.json` 使用 `schema_version: 1`，包含来源元数据、语言、提取方法、
警告、完整性评估和有序的 `segments` 列表。每个片段都有 `start_seconds`、`end_seconds`、
`text` 和 `confidence`；时间以秒为单位保留原始精度。SRT 和 WebVTT 使用
毫秒时间戳。`--json` 返回结果和输出文件路径；文字稿内容请读取 `segments.json`。

## OCR 控制

`--ocr-profile` 设置固定采样间隔：`fast` 为 1 秒、`balanced`（默认）为 0.5 秒、
`accurate` 为 0.25 秒。两、三小时的视频仍保持相同密度，不再有全片 2,400 帧上限。
帧以每批最多 32 张的方式流式处理，识别后删除。完全相同的裁剪图片可复用
OCR 结果，同时保留每个采样时间位置。

`--ocr-region X Y WIDTH HEIGHT` 使用相对于画面的归一化坐标，从左上角量起。
默认值 `0 0.55 1 0.45` 覆盖底部 45%。宽度和高度必须为正，且区域不可超出画面。

```bash
./.venv/bin/youtubetext INPUT --ocr-profile accurate --ocr-region 0 0.55 1 0.45
```

这是固定密度采样和相同图片复用。显示时间短于所选间隔的字幕仍可能漏掉；
提高采样密度会增加处理工作量。

## 并行与资源控制

`--jobs 0` 是默认值，会根据物理内存推导输入并行数上限：

| 物理内存 | 自动输入任务数 |
| --- | ---: |
| 少于 12 GiB | 1 |
| 12–23 GiB | 2 |
| 24–39 GiB | 3 |
| 40 GiB 或更多 | 4 |

在 Windows 上，CPU 容量可能进一步降低自动并行数。使用 `--jobs 1` 到
`--jobs 8` 覆盖此设置。网络工作最多使用四个槽位；macOS 的 Apple Vision OCR
最多使用两个，Windows 的 RapidOCR 使用一个。两个平台都会串行运行 Whisper，
以限制内存和计算资源争用。

本地 OCR 引擎以流式方式每批接收最多 32 个裁剪帧。使用 `--resume` 时，全部
成功识别的批次会在删除前写入检查点；含有帧错误的批次不会写入检查点，并会在
下次运行时重试。检查点会存储时间戳、内容哈希和原始文本框，绝不会保留帧图像
或临时路径。

## Whisper 模型

Whisper 会在字幕和内嵌字幕均不可用、`auto` 需要补充可疑空白，或选择
`whisper` / `hybrid` 模式时运行。
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

- `sources/`：YouTube/Bilibili 元数据和有次数上限的平台候选字幕尝试。
- `inputs.py`：本地文件元数据、内容标识和相邻字幕文件。
- `planning.py`：只读的执行前路径和下载要求。
- `media.py`：可断点续跑的媒体下载和有界帧采样。
- `resume.py`：文字稿复用、任务锁、OCR/Whisper 检查点和任务清理。
- `ocr/`：Apple Vision 或 RapidOCR 与内嵌字幕组装。
- `asr/`：MLX Whisper 或 faster-whisper、本地模型选择和可续跑音频分块。
- `acquisition.py`：字幕/OCR/Whisper 的权威路由策略。
- `runtime.py`：主机资源检测和保持顺序的多输入调度。
- `export.py`：Markdown、TXT、SRT、WebVTT 和 JSON 输出的分组原子写入。
- `cli.py`：终端界面。

## 当前限制

- macOS Apple Silicon 与 Windows x64 已通过自动化 CI 测试。Windows CI 还使用真实本地 OCR、Whisper 模型，以及离线的命令行到导出全流程进行了验证；实体 Windows 电脑上的真实视频端到端验证仍待完成，公开视频网址也可能拒绝托管 CI 机器。
- Windows 的 `cache clear-incomplete` 暂不可用；跨 junction 和 reparse point 的安全删除仍需进一步验证。
- 平台访问取决于 yt-dlp，并可能受到地区、账号、Cookie 或平台变更的影响。
- 默认 OCR 裁剪覆盖画面底部 45%；其他字幕位置可使用 `--ocr-region` 调整。
  高度风格化的文字仍可能需要 Whisper 模式。
- 已在 macOS 上实际验证 Bilibili 视频的端到端转录；平台字幕兼容性仍需在更多
  公开视频上进行测试。
- Whisper 可从已完成分块的边界续跑；中断的分块会重新运行。
- OCR 使用固定采样间隔，即使在 `accurate` 模式，仍可能漏掉显示时间短于 0.25 秒的字幕。
- 时间空白和静音检查无法证明文字识别准确或确实漏掉语音。长时间停顿和音乐
  仍可能需要人工检查；目前尚未建立这些检查的真实视频准确率基准。

## 参考资料与声明

本设计参考了 [MediaBrief](https://github.com/EvilIrving/mediabrief)。本地参考
签出可保留在 `reference/mediabrief/` 下；整个 `reference/` 目录已被有意忽略，
且不属于此仓库。第三方软件声明请参阅
[`THIRD_PARTY_NOTICES.md`](../THIRD_PARTY_NOTICES.md)。

0.2.0 变更请参阅 [CHANGELOG.md](../CHANGELOG.md)。

YouTubeText 采用 [MIT 许可证](../LICENSE)。
