# YouTubeText

English | [繁體中文](docs/README.zh-Hant.md) | [简体中文](docs/README.zh-Hans.md) | [Español](docs/README.es.md) | [日本語](docs/README.ja.md)

YouTubeText is a terminal tool for Apple Silicon macOS and Windows x64 that turns
YouTube and Bilibili videos into clean, timestamped transcripts. It exports a timestamped
Markdown file, a clean Markdown file without timestamps, a text file, and
structured metadata.

YouTubeText extracts complete text only. It does not summarize videos, analyze
arguments, or require an LLM, Ollama, or a cloud AI API.

## Features

- Submit one URL or a queue of URLs in the same command.
- Detect YouTube and Bilibili sources and read their metadata.
- Prefer manual or automatic platform captions when available.
- Read burned-in subtitles with Apple Vision on macOS or RapidOCR on Windows
  when no platform track is usable.
- Fall back to local MLX Whisper on macOS or faster-whisper on Windows when OCR
  is not usable.
- Export both timestamped and clean, no-timestamp Markdown transcripts.
- Automatically choose URL concurrency from physical memory and, on Windows,
  CPU capacity.
- Reuse completed transcripts, resume interrupted media downloads, and reuse
  completed OCR batches.
- Inspect a URL with `--plan` before downloading any subtitle or media file.
- Isolate failures: one bad URL does not cancel the rest of the queue.

## Pipeline

```text
URL queue
  └─ Detect platform and read metadata
       ├─ Usable platform caption ─────────────→ Clean cues ─────────→ Export
       └─ No usable caption
            ├─ --resume cache hit ─────────────────────────────→ Export
            └─ cache miss or resume off → analysis video → local OCR
                                                            ├─ usable → Export
                                                            └─ unusable → Whisper → Export
```

The platform-caption path never downloads the video. Caption files are small,
temporary inputs. Normal runs keep OCR and Whisper media in a supervised,
per-run temporary directory and remove it when the run ends. With `--resume`,
media is stored under the user cache's `tasks/` directory, retained after an
interruption, and removed after success.

## Requirements

- Apple Silicon Mac (`arm64`) with macOS 13 or later, or 64-bit x86 Windows
- Python 3.11–3.14
- FFmpeg available on `PATH`
- On macOS only: Xcode Command Line Tools to build the Apple Vision OCR helper
- `uv` is recommended

On macOS, install FFmpeg with Homebrew if it is missing:

```bash
brew install ffmpeg
```

On macOS, install Apple's command-line tools if they are missing:

```bash
xcode-select --install
```

## Installation

The supported installation method on both platforms is currently a source
checkout. YouTubeText is not yet published as a PyPI package or prebuilt wheel.

On macOS, run:

```bash
git clone https://github.com/yuchenzhu-research/YouTubeText.git
cd YouTubeText
./scripts/install.sh
```

If GitHub SSH is already configured:

```bash
git clone git@github.com:yuchenzhu-research/YouTubeText.git
```

On Windows x64, install 64-bit Python and FFmpeg first, add FFmpeg's `bin`
directory to `PATH`, then run in PowerShell:

```powershell
git clone https://github.com/yuchenzhu-research/YouTubeText.git
Set-Location YouTubeText
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\install.ps1
```

Both installers create `.venv` and install the host-specific Python dependencies.
The macOS installer also builds the Apple Vision OCR helper; the Windows installer
does not use Swift. Neither installer installs FFmpeg. The macOS installer does not
use `sudo` or install Homebrew automatically.

Check the local environment after installation on macOS:

```bash
./.venv/bin/youtubetext doctor
```

## Quick start

Process one video:

```bash
./.venv/bin/youtubetext "https://www.youtube.com/watch?v=VIDEO_ID"
```

Queue YouTube and Bilibili URLs with two concurrent URL tasks:

```bash
./.venv/bin/youtubetext \
  "https://www.youtube.com/watch?v=VIDEO_ID" \
  "https://www.bilibili.com/video/BV_ID" \
  --jobs 2
```

Choose an output directory and Traditional Chinese recognition:

```bash
./.venv/bin/youtubetext URL \
  --output ~/Desktop/YouTubeText-output \
  --language zh-Hant
```

Return machine-readable results:

```bash
./.venv/bin/youtubetext URL --json
```

Show every option:

```bash
./.venv/bin/youtubetext --help
```

On Windows, use the executable in `.venv\Scripts` from PowerShell; for example:

```powershell
.\.venv\Scripts\youtubetext.exe doctor
.\.venv\Scripts\youtubetext.exe "https://www.youtube.com/watch?v=VIDEO_ID"
.\.venv\Scripts\youtubetext.exe URL_1 URL_2 --jobs 2
.\.venv\Scripts\youtubetext.exe URL --plan
.\.venv\Scripts\youtubetext.exe URL --resume
.\.venv\Scripts\youtubetext.exe cache
```

The remaining command examples use the macOS executable path. On Windows,
replace `./.venv/bin/youtubetext` with `.\.venv\Scripts\youtubetext.exe`.

## Preflight plan

Use `--plan` to inspect what a fresh run would do before any subtitle or media
file is downloaded:

```bash
./.venv/bin/youtubetext URL --plan
./.venv/bin/youtubetext URL_1 URL_2 --plan --json
```

Preflight makes a network metadata request for each URL and reports:

- source metadata and duration;
- the best caption track advertised by the platform;
- the expected caption, OCR, and Whisper route;
- whether subtitle or media downloads are `required`, `conditional`, or `none`;
- the primary media type and any audio fallback.

An advertised track is marked `advertised-unvalidated`: preflight does not
download or parse it, so availability is not yet guaranteed. Preflight does not
download subtitles, audio, video, or Whisper models; does not sample frames or
run OCR/Whisper; does not create an output directory; and does not read or write
resume state.

Plans use the cache assumption `no-resume-reuse`. A later execution with
`--resume` may therefore skip work shown as required. `--plan` and `--resume`
cannot be used together. Multiple URLs are inspected concurrently, results stay
in input order, and one metadata failure does not hide the other plans.

## Resume and cache

Enable reusable local state explicitly:

```bash
./.venv/bin/youtubetext URL --resume
```

YouTubeText still checks the platform for a newly available caption first. On a
cache miss, yt-dlp can continue an interrupted `.part` download; a completed
media file is reused directly; and the local OCR backend saves raw observations after
each successfully recognized batch of up to 32 frames. A batch containing frame
errors is not checkpointed and is retried on the next run. Sampled images are
deleted batch by batch after recognition; when a checkpoint is written, it is
saved before deletion. Whisper inference cannot yet resume from the middle.

On macOS, completed structured transcripts are stored under:

```text
~/Library/Caches/YouTubeText/transcripts/
```

On Windows, the cache is in the user's platform cache directory; run
`youtubetext cache` to see its exact location. Incomplete tasks are stored in a
sibling `tasks/` directory on either platform. On macOS, directories and files
use `0700` and `0600` permissions; Windows uses its own filesystem permissions. Two
processes requesting the same task use one lock: one performs the work and the
other waits to reuse its result. Anonymous requests and different cookie files
use separate cache scopes.

Inspect cache usage without modifying it:

```bash
./.venv/bin/youtubetext cache
./.venv/bin/youtubetext cache --json
```

On macOS, remove only failed or interrupted task media and OCR checkpoints while
retaining all completed transcripts:

```bash
./.venv/bin/youtubetext cache clear-incomplete
```

Active, locked tasks are skipped. Removed temporary media cannot be recovered,
but it can be downloaded again from the original URL. On Windows,
`cache clear-incomplete` is not yet supported and does not delete task data;
`cache` and `cache --json` remain read-only and available. To remove everything
on macOS, including completed transcripts, delete
`~/Library/Caches/YouTubeText/` manually.

## Authentication and cookies

Use the login state from a local browser for restricted videos:

```bash
./.venv/bin/youtubetext URL --cookies-from-browser safari
```

Or provide a Netscape-format cookie file:

```bash
./.venv/bin/youtubetext URL --cookies-file /path/to/cookies.txt
```

The two options are mutually exclusive. Cookie contents are never written to
the output directory, metadata, JSON results, or resume cache. Cookie files are
copied into memory for each yt-dlp call; the original file is not modified.

Because a browser name cannot reliably identify the active account,
`--cookies-from-browser` cannot be combined with `--resume`. Use
`--cookies-file` when authentication and resume are both required. Safari may
need Full Disk Access for the terminal in macOS Privacy & Security settings.
The Safari example is macOS-specific; on Windows, select an installed browser
supported by yt-dlp.

## Modes

| Mode | Behavior |
| --- | --- |
| `auto` | Prefer platform captions, then OCR, then Whisper. |
| `captions` | Require a platform caption track; fail when none is available. |
| `ocr` | Ignore platform captions and read burned-in subtitles only. |
| `whisper` | Skip captions and OCR; run local speech recognition directly. |
| `hybrid` | Run OCR and Whisper; visible captions take priority when their time ranges overlap. |

Force OCR, for example:

```bash
./.venv/bin/youtubetext URL --mode ocr --language zh-Hant
```

## Languages

`--language` supports:

```text
auto, en, zh-Hans, zh-Hant, es, ja, ko, fr, de, pt, it, ru, ar, hi, vi
```

On macOS, Apple Vision uses title and author context to prioritize recognition
languages when `auto` is selected. On Windows, RapidOCR's default PP-OCRv6 small
model recognizes `en`, `zh-Hans`, `zh-Hant`, `es`, `ja`, `fr`, `de`, `pt`, `it`, and
`vi` without switching models; `--language` does not restrict its output to one
language. It does not support OCR for `ko`, `ru`, `ar`, or `hi`: forced OCR mode
reports an error, while `auto` and `hybrid` warn and use Whisper. Platform
captions and Whisper can still use those languages. Both Whisper backends can
detect spoken language automatically.

Set an ordered list of preferred platform-caption languages separately:

```bash
./.venv/bin/youtubetext URL \
  --caption-language zh-Hant \
  --caption-language zh-Hans \
  --caption-language en
```

## Output files

The default output root is `YouTubeText-output/` in the current directory:

```text
YouTubeText-output/
  VIDEO_ID-video-title/
    transcript.md
    transcript-clean.md
    transcript.txt
    metadata.json
```

- `transcript.md`: complete Markdown transcript with per-segment timestamps.
- `transcript-clean.md`: complete Markdown transcript without timeline markers.
- `transcript.txt`: plain text with timestamps.
- `metadata.json`: source, language, extraction method, segment count, and warnings.

## Concurrency and resource control

`--jobs 0` is the default and derives a memory-based URL concurrency ceiling:

| Physical memory | Automatic URL jobs |
| --- | ---: |
| Less than 12 GiB | 1 |
| 12–23 GiB | 2 |
| 24–39 GiB | 3 |
| 40 GiB or more | 4 |

On Windows, CPU capacity can lower the automatic URL count further. Override it
with `--jobs 1` through `--jobs 8`. Network work uses at most four slots;
Apple Vision OCR uses at most two on macOS, while RapidOCR uses one on Windows.
Whisper is serialized on both platforms to limit memory and compute contention.

OCR normally samples one frame per second and caps long videos at 2,400 frames.
The local OCR backend receives batches of up to 32 cropped frames, and those images are always
removed batch by batch after recognition. With `--resume`, a successfully
recognized batch is checkpointed before deletion; a batch containing frame errors
is not checkpointed and will be retried on the next run. Checkpoints store
timestamps, content hashes, and raw text boxes, never frame images or temporary
paths.

## Whisper models

Whisper runs only when captions and burned-in subtitles are unusable, or when
`whisper` / `hybrid` mode is selected. A model is downloaded on first use and
then reused from a local model cache. On macOS, compatible MLX weights already
present in the standard Hugging Face cache are reused as well. Windows uses
faster-whisper with CTranslate2 weights in a separate cache.

The following download and memory estimates apply to the macOS MLX backend;
Windows model sizes and runtime memory can differ:

| Model | Approximate download | Approximate runtime memory |
| --- | ---: | ---: |
| `base` | 144 MB | 1 GiB |
| `small` | 481 MB | 2 GiB |
| `large-v3-turbo` | 1.61 GB | 6 GiB |

Automatic selection uses `small` on lower-memory Macs and `large-v3-turbo` on
Macs with at least 16 GiB. Windows defaults to `small`. Override the choice
with `--whisper-model`. The Windows doctor checks backend availability but does
not yet inspect the CTranslate2 model cache.

## Development and architecture

Run the complete development workflow:

```bash
./scripts/dev.sh
```

Pass pytest arguments through the script when needed:

```bash
./scripts/dev.sh tests/test_acquisition.py -q
```

On Windows, run the development script from PowerShell:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\dev.ps1
```

Primary modules:

- `sources/`: YouTube/Bilibili metadata and platform captions.
- `planning.py`: read-only preflight routes and download requirements.
- `media.py`: resumable media downloads and bounded frame sampling.
- `resume.py`: transcript reuse, task locks, OCR checkpoints, and task cleanup.
- `ocr/`: Apple Vision or RapidOCR and burned-in caption assembly.
- `asr/`: MLX Whisper or faster-whisper and local model selection.
- `acquisition.py`: the authoritative captions/OCR/Whisper routing policy.
- `runtime.py`: host resource detection and ordered multi-URL scheduling.
- `export.py`: grouped atomic writes for Markdown, TXT, and JSON outputs.
- `cli.py`: terminal interface.

## Current limitations

- macOS Apple Silicon and Windows x64 pass the automated CI suite. A real-video
  end-to-end run on a physical Windows machine is still pending.
- Windows `cache clear-incomplete` is not yet implemented.
- Platform access depends on yt-dlp and may be affected by region, account,
  cookies, or platform changes.
- Apple Vision OCR is optimized for subtitles near the bottom of the frame.
  Other layouts or heavily stylized text may require Whisper mode.
- Bilibili metadata and temporary media paths have been exercised live; caption
  compatibility still needs broader testing across public videos.
- Whisper cannot resume from an intermediate inference position.

## Reference and notices

The design was informed by [MediaBrief](https://github.com/EvilIrving/mediabrief).
A local reference checkout may be kept under `reference/mediabrief/`; the entire
`reference/` directory is intentionally ignored by Git and is not part of this
repository. See [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md) for third-party
software notices.
