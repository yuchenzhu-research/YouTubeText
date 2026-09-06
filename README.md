# YouTubeText

YouTubeText 是一款面向 Apple Silicon Mac 的终端工具，可将 YouTube 和 Bilibili
视频转换为干净、带时间戳的 Markdown 与 TXT 全文。

它只负责提取完整文字，不生成摘要，不分析观点，也不需要 Ollama、LLM 或云端 AI API。

## 功能

- 同时提交一个或多个 URL，并按本机内存自动安排并行任务。
- 识别 YouTube、Bilibili 来源并读取标题、作者、时长等元数据。
- 优先下载平台提供的人工字幕或自动字幕。
- 无字幕轨时，使用 macOS Apple Vision 识别画面中的硬字幕。
- 没有可用硬字幕时，使用本地 MLX Whisper 识别语音。
- 输出 `transcript.md`、`transcript.txt` 和 `metadata.json`。
- 每个 URL 独立成功或失败，一个任务出错不会取消其他任务。

## 处理流程

```text
URL 队列
  └─ 识别平台并读取元数据
       ├─ 有字幕轨 ──────────────→ 清洗字幕 ───────────────→ 导出
       └─ 无字幕轨 → 下载临时低清视频 → Apple Vision OCR
                                              ├─ 文字质量合格 → 导出
                                              └─ 不合格 → MLX Whisper → 导出
```

平台字幕路径不会下载视频。OCR 和 Whisper 所需的媒体只保存在系统缓存中的临时任务
目录，任务完成或失败后会自动删除。

## 系统要求

- Apple Silicon Mac（arm64）
- macOS 13 或更高版本
- Python 3.11–3.14
- FFmpeg
- Xcode Command Line Tools（用于编译 Apple Vision OCR 辅助程序）
- 推荐安装 `uv`

缺少 FFmpeg 时可运行：

```bash
brew install ffmpeg
```

缺少 Apple Command Line Tools 时可运行：

```bash
xcode-select --install
```

## 安装

当前唯一正式支持的安装方式是克隆 Git 仓库后运行 `scripts/install.sh`；项目尚未提供
PyPI 包或 wheel 安装包。

```bash
git clone https://github.com/yuchenzhu-research/YouTubeText.git
cd YouTubeText
./scripts/install.sh
```

已经配置 GitHub SSH Key 时，也可以使用
`git@github.com:yuchenzhu-research/YouTubeText.git`。

安装脚本会创建仓库内的 `.venv`、安装 Python 依赖并编译 Apple Vision OCR
辅助程序。它不会使用 `sudo`，也不会自动安装 Homebrew。

安装后可先检查本机环境：

```bash
./.venv/bin/youtubetext doctor
```

## 使用

处理一个视频：

```bash
./.venv/bin/youtubetext "https://www.youtube.com/watch?v=VIDEO_ID"
```

处理多个 URL，并允许两个 URL 任务并行：

```bash
./.venv/bin/youtubetext \
  "https://www.youtube.com/watch?v=VIDEO_ID" \
  "https://www.bilibili.com/video/BV_ID" \
  --jobs 2
```

指定输出目录和繁体中文：

```bash
./.venv/bin/youtubetext URL \
  --output ~/Desktop/YouTubeText-output \
  --language zh-Hant
```

需要机器可读结果时：

```bash
./.venv/bin/youtubetext URL --json
```

查看全部选项：

```bash
./.venv/bin/youtubetext --help
```

## 模式

| 模式 | 行为 |
| --- | --- |
| `auto` | 平台字幕优先；没有字幕时尝试 OCR；OCR 不可用时回退 Whisper。 |
| `captions` | 只接受平台字幕；没有字幕轨就报错。 |
| `ocr` | 只识别画面硬字幕；没有有效字幕就报错。 |
| `whisper` | 跳过字幕下载和 OCR，直接进行本地语音识别。 |
| `hybrid` | 同时使用 OCR 与 Whisper；可见字幕优先，语音结果补充空白时间段。 |

例如强制使用 OCR：

```bash
./.venv/bin/youtubetext URL --mode ocr --language zh-Hant
```

## 语言

`--language` 支持：

```text
auto, en, zh-Hans, zh-Hant, es, ja, ko, fr, de, pt, it, ru, ar, hi, vi
```

其中 `en`、`zh-Hans`、`zh-Hant` 和 `es` 分别对应英语、简体中文、繁体中文和
西班牙语。使用 `auto` 时，Apple Vision 会根据标题优先排列识别语言，并从最终 OCR
文字判断简体或繁体；Whisper 则使用自己的语音语言检测。

平台字幕可以单独设置多个优先语言：

```bash
./.venv/bin/youtubetext URL \
  --caption-language zh-Hant \
  --caption-language zh-Hans \
  --caption-language en
```

## 输出

默认写入当前目录的 `YouTubeText-output/`：

```text
YouTubeText-output/
  VIDEO_ID-视频标题/
    transcript.md
    transcript.txt
    metadata.json
```

Markdown 和 TXT 都包含时间戳与完整文字。JSON 保存来源 URL、平台、作者、时长、
语言、提取方式、片段数量和警告。

## 并行与资源控制

`--jobs 0` 是默认设置，会根据物理内存自动选择同时处理的 URL 数量：

| 内存 | 自动并行任务数 |
| --- | ---: |
| 少于 12 GiB | 1 |
| 12–23 GiB | 2 |
| 24–39 GiB | 3 |
| 40 GiB 及以上 | 4 |

也可以通过 `--jobs 1` 到 `--jobs 8` 手动设置。网络下载最多同时进行 4 个，Apple
Vision OCR 最多同时进行 2 个，Whisper 固定串行运行，以避免统一内存和 Metal 资源突增。

OCR 默认每秒抽取一帧，长视频最多保留 2,400 帧；每 32 帧调用一次 Vision，并在
识别后立即删除该批图片。

## Whisper 模型

Whisper 只在没有可用字幕轨和硬字幕，或明确选择 `whisper` / `hybrid` 模式时运行。
模型首次需要时才下载，并会复用 Hugging Face 已有缓存。

| 模型 | 下载体积（约） | 运行内存（约） |
| --- | ---: | ---: |
| `base` | 144 MB | 1 GiB |
| `small` | 481 MB | 2 GiB |
| `large-v3-turbo` | 1.61 GB | 6 GiB |

自动选择在低内存 Mac 上使用 `small`，16 GiB 及以上使用
`large-v3-turbo`。可用 `--whisper-model` 手动覆盖。

## 开发

```bash
./scripts/dev.sh
```

该脚本会同步开发依赖、重新编译原生 OCR 辅助程序并运行完整测试。也可以把 pytest
参数直接传给它：

```bash
./scripts/dev.sh tests/test_acquisition.py -q
```

主要模块：

- `sources/`：YouTube、Bilibili 元数据与平台字幕。
- `media.py`：临时媒体下载与受限抽帧。
- `ocr/`：Apple Vision 调用和硬字幕合并。
- `asr/`：MLX Whisper、模型选择与缓存复用。
- `acquisition.py`：字幕、OCR、Whisper 之间的路由策略。
- `runtime.py`：本机资源检测与多 URL 调度。
- `export.py`：Markdown、TXT、JSON 原子写入。
- `cli.py`：终端入口。

## 当前限制

- 目前只支持 Apple Silicon macOS。
- 平台访问能力依赖 yt-dlp；受地区、账号、Cookies 或平台变更影响的视频可能失败。
- Apple Vision OCR 针对视频下方的硬字幕优化；其他位置或高度装饰化的文字可能需要
  手动选择 Whisper。
- Bilibili 元数据和临时媒体下载已实时验证，字幕兼容仍需更多公开视频验证。

## 参考实现

MediaBrief 原始仓库保存在本机 `reference/mediabrief/`，仅用于架构和行为对照。
`reference/` 已被 Git 忽略，不会提交到 YouTubeText 仓库。第三方许可信息见
[`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md)。
