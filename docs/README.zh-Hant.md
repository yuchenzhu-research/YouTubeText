# YouTubeText

[English](../README.md) | 繁體中文 | [简体中文](README.zh-Hans.md) | [Español](README.es.md) | [日本語](README.ja.md)

YouTubeText 是一款適用於 Apple Silicon Mac 的終端工具，可將 YouTube 和
Bilibili 影片轉換為乾淨且帶時間戳記的逐字稿。它會匯出一份帶時間戳記的
Markdown 檔案、一份不含時間戳記的乾淨 Markdown 檔案、一份文字檔案，以及
結構化中繼資料。

YouTubeText 只擷取完整文字。它不會總結影片、分析論點，也不需要 LLM、
Ollama 或雲端 AI API。

## 功能

- 在同一個命令中提交一個 URL 或一列 URL 佇列。
- 偵測 YouTube 和 Bilibili 來源並讀取其中繼資料。
- 有可用字幕時，優先採用平台提供的人工或自動字幕。
- 沒有可用的平台字幕軌時，使用 Apple Vision 讀取內嵌字幕。
- OCR 無法使用時，退回到本機 MLX Whisper 語音辨識。
- 同時匯出帶時間戳記與乾淨、不含時間戳記的 Markdown 逐字稿。
- 根據 Mac 的實體記憶體，自動選擇安全的 URL 並行數。
- 重複使用已完成的逐字稿、續傳中斷的媒體下載，以及重複使用已完成的 OCR 批次。
- 下載任何字幕或媒體檔案前，使用 `--plan` 檢查 URL。
- 隔離失敗：單一錯誤 URL 不會取消佇列中的其他任務。

## 處理流程

```text
URL 佇列
  └─ 偵測平台並讀取中繼資料
       ├─ 可用的平台字幕 ─────────────────→ 清理字幕段落 ───────→ 匯出
       └─ 無可用字幕
            ├─ --resume 快取命中 ─────────────────────────────→ 匯出
            └─ 快取未命中或未啟用續跑 → 分析影片 → Vision OCR
                                                    ├─ 可用 → 匯出
                                                    └─ 不可用 → Whisper → 匯出
```

平台字幕路徑絕不下載影片。字幕檔案是小型的暫存輸入。一般執行會將 OCR 和
Whisper 媒體保存在受監管的單次執行暫存目錄中，並在執行結束時移除。使用
`--resume` 時，媒體會儲存在使用者快取的 `tasks/` 目錄下，在中斷後保留，
並在成功後移除。

## 系統需求

- Apple Silicon Mac (`arm64`)
- macOS 13 或更新版本
- Python 3.11–3.14
- FFmpeg
- Xcode Command Line Tools，用於建置 Apple Vision OCR 輔助程式
- 建議使用 `uv`

如果尚未安裝 FFmpeg，請使用 Homebrew 安裝：

```bash
brew install ffmpeg
```

如果尚未安裝 Apple 命令列工具，請執行：

```bash
xcode-select --install
```

## 安裝

目前支援的安裝方式是簽出原始碼。YouTubeText 尚未發佈為 PyPI 套件或預先
建置的 wheel。

```bash
git clone https://github.com/yuchenzhu-research/YouTubeText.git
cd YouTubeText
./scripts/install.sh
```

如果已設定 GitHub SSH：

```bash
git clone git@github.com:yuchenzhu-research/YouTubeText.git
```

安裝程式會建立 `.venv`、安裝 Python 相依套件，並建置 Apple Vision OCR
輔助程式。它不會使用 `sudo`，也不會自動安裝 Homebrew。

安裝後檢查本機環境：

```bash
./.venv/bin/youtubetext doctor
```

## 快速開始

處理一部影片：

```bash
./.venv/bin/youtubetext "https://www.youtube.com/watch?v=VIDEO_ID"
```

將 YouTube 與 Bilibili URL 排入佇列，並同時執行兩個 URL 任務：

```bash
./.venv/bin/youtubetext \
  "https://www.youtube.com/watch?v=VIDEO_ID" \
  "https://www.bilibili.com/video/BV_ID" \
  --jobs 2
```

選擇輸出目錄與繁體中文辨識：

```bash
./.venv/bin/youtubetext URL \
  --output ~/Desktop/YouTubeText-output \
  --language zh-Hant
```

傳回機器可讀的結果：

```bash
./.venv/bin/youtubetext URL --json
```

顯示所有選項：

```bash
./.venv/bin/youtubetext --help
```

## 執行前計畫

使用 `--plan` 檢查全新執行將採取的動作；此時尚未下載任何字幕或媒體檔案：

```bash
./.venv/bin/youtubetext URL --plan
./.venv/bin/youtubetext URL_1 URL_2 --plan --json
```

執行前計畫會為每個 URL 發出網路中繼資料請求，並報告：

- 來源中繼資料與時長；
- 平台宣告的最佳字幕軌；
- 預期使用的字幕、OCR 與 Whisper 路徑；
- 字幕或媒體下載是 `required`、`conditional` 還是 `none`；
- 主要媒體類型與任何音訊退回方案。

宣告的字幕軌會標記為 `advertised-unvalidated`：執行前計畫不會下載或解析它，
因此尚無法保證可用性。執行前計畫不會下載字幕、音訊、影片或 Whisper 模型；
不會取樣影格或執行 OCR/Whisper；不會建立輸出目錄；也不會讀取或寫入續跑狀態。

計畫採用快取假設 `no-resume-reuse`。因此，稍後使用 `--resume` 執行時，可能會
略過計畫中顯示為必需的工作。`--plan` 和 `--resume` 不能同時使用。多個 URL
會並行檢查，結果維持輸入順序，且一個中繼資料失敗不會隱藏其他計畫。

## 續跑與快取

明確啟用可重複使用的本機狀態：

```bash
./.venv/bin/youtubetext URL --resume
```

YouTubeText 仍會先檢查平台是否提供了新的字幕。快取未命中時，yt-dlp 可繼續
中斷的 `.part` 下載；已完成的媒體檔案會直接重複使用；Apple Vision 則會在
每批最多 32 個影格全部成功辨識後儲存原始 OCR 觀察結果。含有影格錯誤的批次
不會寫入檢查點，而會在下次執行時重試。取樣圖片會在辨識後逐批刪除；若有寫入
檢查點，則會先安全儲存再刪除。Whisper 推論目前還不能從中間位置續跑。

已完成的結構化逐字稿儲存在：

```text
~/Library/Caches/YouTubeText/transcripts/
```

未完成的任務儲存在同層的 `tasks/` 目錄中。目錄和檔案分別以 `0700` 與 `0600`
權限限制為僅目前使用者可存取。兩個處理程序請求相同任務時會共用一把鎖：一個
執行工作，另一個等待並重複使用其結果。匿名請求與不同的 Cookie 檔案會使用
各自獨立的快取範圍。

在不修改快取的情況下檢查快取用量：

```bash
./.venv/bin/youtubetext cache
./.venv/bin/youtubetext cache --json
```

只移除失敗或中斷任務的媒體與 OCR 檢查點，同時保留所有已完成的逐字稿：

```bash
./.venv/bin/youtubetext cache clear-incomplete
```

執行中且已鎖定的任務會被略過。已移除的暫存媒體無法復原，但可以從原始 URL
重新下載。若要移除包括已完成逐字稿在內的所有內容，請手動刪除
`~/Library/Caches/YouTubeText/`。

## 驗證與 Cookie

針對受限制的影片，使用本機瀏覽器的登入狀態：

```bash
./.venv/bin/youtubetext URL --cookies-from-browser safari
```

或者提供 Netscape 格式的 Cookie 檔案：

```bash
./.venv/bin/youtubetext URL --cookies-file /path/to/cookies.txt
```

這兩個選項互斥。Cookie 內容絕不會寫入輸出目錄、中繼資料、JSON 結果或續跑
快取。每次呼叫 yt-dlp 時，Cookie 檔案會被複製到記憶體中；原始檔案不會被
修改。

由於瀏覽器名稱無法可靠識別目前使用的帳戶，`--cookies-from-browser` 不能與
`--resume` 一起使用。需要同時使用驗證與續跑時，請使用 `--cookies-file`。
Safari 可能需要在 macOS「隱私權與安全性」設定中，授予終端「完整磁碟存取權」。

## 模式

| 模式 | 行為 |
| --- | --- |
| `auto` | 優先使用平台字幕，然後是 OCR，最後是 Whisper。 |
| `captions` | 要求平台字幕軌；沒有可用字幕時失敗。 |
| `ocr` | 忽略平台字幕，只讀取內嵌字幕。 |
| `whisper` | 略過字幕和 OCR；直接執行本機語音辨識。 |
| `hybrid` | 執行 OCR 和 Whisper；當兩者時間範圍重疊時，畫面可見字幕優先。 |

例如，強制使用 OCR：

```bash
./.venv/bin/youtubetext URL --mode ocr --language zh-Hant
```

## 語言

`--language` 支援：

```text
auto, en, zh-Hans, zh-Hant, es, ja, ko, fr, de, pt, it, ru, ar, hi, vi
```

選擇 `auto` 時，Apple Vision 會利用標題和作者內容設定辨識語言的優先順序。
Whisper 會自行偵測口語語言。

另行設定一份依優先順序排列的平台字幕語言清單：

```bash
./.venv/bin/youtubetext URL \
  --caption-language zh-Hant \
  --caption-language zh-Hans \
  --caption-language en
```

## 輸出檔案

預設輸出根目錄是目前目錄中的 `YouTubeText-output/`：

```text
YouTubeText-output/
  VIDEO_ID-video-title/
    transcript.md
    transcript-clean.md
    transcript.txt
    metadata.json
```

- `transcript.md`：包含每個段落時間戳記的完整 Markdown 逐字稿。
- `transcript-clean.md`：不含時間軸標記的完整 Markdown 逐字稿。
- `transcript.txt`：含時間戳記的純文字。
- `metadata.json`：來源、語言、擷取方法、段落數量與警告。

## 並行與資源控制

`--jobs 0` 是預設值，會根據實體記憶體推導 URL 並行數：

| 實體記憶體 | 自動 URL 任務數 |
| --- | ---: |
| 少於 12 GiB | 1 |
| 12–23 GiB | 2 |
| 24–39 GiB | 3 |
| 40 GiB 或更多 | 4 |

使用 `--jobs 1` 到 `--jobs 8` 覆寫此設定。網路工作最多使用四個槽位，Apple
Vision OCR 最多使用兩個，而 Whisper 會序列化執行，以避免統一記憶體與 Metal
資源爭用。

OCR 通常每秒取樣一個影格，並將長影片限制為最多 2,400 個影格。Vision 每批
接收最多 32 個裁切影格，且這些圖片在辨識後一律逐批移除。使用 `--resume`
時，全部成功辨識的批次會在刪除前寫入檢查點；含有影格錯誤的批次不會寫入
檢查點，並會在下次執行時重試。檢查點會儲存時間戳記、內容雜湊與原始文字框，
絕不保留影格圖片或暫存路徑。

## Whisper 模型

Whisper 只會在字幕與內嵌字幕均不可用，或選擇 `whisper` / `hybrid` 模式時執行。
模型會在首次使用時下載，然後從本機模型快取中重複使用。標準 Hugging Face
快取中已有的相容權重也會重複使用。

| 模型 | 約略下載大小 | 約略執行時記憶體 |
| --- | ---: | ---: |
| `base` | 144 MB | 1 GiB |
| `small` | 481 MB | 2 GiB |
| `large-v3-turbo` | 1.61 GB | 6 GiB |

自動選擇會在記憶體較少的 Mac 上使用 `small`，在至少有 16 GiB 記憶體的 Mac
上使用 `large-v3-turbo`。使用 `--whisper-model` 可覆寫此設定。

## 開發與架構

執行完整的開發工作流程：

```bash
./scripts/dev.sh
```

需要時，透過指令碼傳入 pytest 引數：

```bash
./scripts/dev.sh tests/test_acquisition.py -q
```

主要模組：

- `sources/`：YouTube/Bilibili 中繼資料與平台字幕。
- `planning.py`：唯讀的執行前路徑與下載需求。
- `media.py`：可續跑的媒體下載與有界影格取樣。
- `resume.py`：逐字稿重複使用、任務鎖、OCR 檢查點與任務清理。
- `ocr/`：Apple Vision 呼叫與內嵌字幕組合。
- `asr/`：MLX Whisper、模型選擇與模型快取重複使用。
- `acquisition.py`：字幕/OCR/Whisper 的權威路由政策。
- `runtime.py`：Mac 資源偵測與保留順序的多 URL 排程。
- `export.py`：Markdown、TXT 與 JSON 輸出的分組原子寫入。
- `cli.py`：終端介面。

## 目前限制

- 僅支援 Apple Silicon macOS。
- 平台存取取決於 yt-dlp，並可能受到地區、帳戶、Cookie 或平台變更的影響。
- Apple Vision OCR 已針對畫面底部附近的字幕進行最佳化。其他版面或高度風格化
  的文字可能需要使用 Whisper 模式。
- 已透過即時測試驗證 Bilibili 中繼資料與暫存媒體路徑；平台字幕相容性仍需在
  更多公開影片上進行測試。
- Whisper 無法從推論過程的中間位置續跑。

## 參考資料與聲明

本設計參考了 [MediaBrief](https://github.com/EvilIrving/mediabrief)。本機參考
簽出可保留在 `reference/mediabrief/` 下；整個 `reference/` 目錄已刻意被 Git
忽略，且不屬於此儲存庫。第三方軟體聲明請參閱
[`THIRD_PARTY_NOTICES.md`](../THIRD_PARTY_NOTICES.md)。
