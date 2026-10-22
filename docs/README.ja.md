# YouTubeText

[English](../README.md) | [繁體中文](README.zh-Hant.md) | [简体中文](README.zh-Hans.md) | [Español](README.es.md) | 日本語

YouTubeTextは、YouTubeおよびBilibiliの動画やローカルの音声・動画ファイルから、読みやすくタイムスタンプ付きの
文字起こしを生成するApple Silicon搭載macOSとWindows x64向けのターミナルツールです。タイムスタンプ
付きMarkdownファイル、タイムスタンプなしの読みやすいMarkdownファイル、テキスト
ファイル、SRT/WebVTT字幕、構造化された文字起こしセグメントとメタデータを出力します。

YouTubeTextは文字起こしを抽出します。動画の要約や論理分析は行わず、LLM、
Ollama、クラウドAI APIも必要ありません。

## 機能

- URL、ローカルパス、`file://`入力を同じキューに送信できます。
- 最大5件の字幕候補を試し、隣接するSRT/WebVTTを`local-captions`として読み取ります。
- `--ocr-profile`と`--ocr-region`でOCRのサンプリング密度と字幕領域を選択します。
- YouTubeとBilibiliのソースを判別し、メタデータを読み取ります。
- 利用可能な場合は、プラットフォームの手動字幕または自動字幕を優先します。
- 利用可能なプラットフォーム字幕がない場合は、macOSではApple Vision、WindowsではRapidOCRで焼き込み字幕を読み取ります。
- OCRを利用できない場合は、macOSではローカルのMLX Whisper、Windowsではfaster-whisperの音声認識にフォールバックします。
- タイムスタンプ付きと、タイムスタンプなしの読みやすいMarkdown文字起こしを出力します。
- SRT・WebVTT字幕と、文字起こし全文のセグメントを含むJSONを出力します。
- 物理メモリと、WindowsではCPU性能から入力の並行処理数を自動的に選択します。
- 完了した文字起こしを再利用し、中断されたメディアのダウンロードを再開し、
  完了済みOCRバッチとWhisperチャンクを再利用できます。
- 字幕またはメディアファイルをダウンロードする前に、`--plan`で入力を確認できます。
- 障害を分離し、1つの入力が失敗してもキュー内の残りをキャンセルしません。

## パイプライン

```text
INPUTキュー（URL、ローカルパス、file://）
  └─ ソースのメタデータを読み取る
       ├─ 利用可能なプラットフォーム字幕／ローカル字幕 → 整形 → 出力
       └─ 利用可能な字幕なし
            ├─ --resume キャッシュヒット → 出力
            ├─ ローカル音声 → Whisper → 出力
            └─ リモート／ローカル動画 → ストリーミングOCR
                                        ├─ 利用可能 → 出力
                                        └─ 利用不可 → Whisper → 出力
```

プラットフォーム字幕のダウンロードや解析が失敗すると、既存の言語・種類の優先順で、
最大5件の提示された候補を試します。すべて失敗した場合は長さを制限した警告で
代替経路を説明し、追加のクラウドAPIは使用しません。

字幕と利用可能なOCRの経路では、出力前に下記の条件に応じて時間の空白を確認します。

プラットフォーム字幕に不審な時間の空白がなければ、メディアのダウンロードは不要です。
字幕ファイルは小さな一時入力です。`auto`モードで不審な空白がある場合は、確認と
Whisperのために音声だけをダウンロードし、使用後に削除する一時ディレクトリへ保存する
ことがあります。通常の実行では、OCRおよびWhisper用のメディアを、実行ごとに管理
される一時ディレクトリに保存し、実行終了時に削除します。`--resume`を指定すると、
メディアはユーザーキャッシュの`tasks/`ディレクトリに保存され、中断後も保持され、
正常終了後に削除されます。ただし、OCRの空白の検証が未完了の場合は保持されます。

## 完全性チェック

出力前に、YouTubeTextは文字起こしセグメントの前後とセグメント間の長い空白を
確認します。閾値は動画の長さの10%で、最小60秒、最大300秒です。90秒未満の動画や
長さが不明な動画には、この判定を適用しません。

`auto`モードでは、利用可能なプラットフォーム字幕、ローカル字幕、OCR結果に不審な空白があると、
FFmpegで無音を確認します。OCRは解析用動画を再利用し、リモート字幕は確認用の音声だけをダウンロードします。
ローカル字幕では元のメディアをその場所で読み取ります。空白区間が無音なら元の文章を保持します。
無音ではない音声がある場合や確認できない場合はWhisperを実行し、元の文章を
保持しながら、不審な空白と重なるWhisperのセグメントだけを統合します。
Whisperが失敗した場合も、元の文字起こしを保持し、
完全性を確認できない旨を警告します。強制指定の`captions`・`ocr`モードでは選択した
方法を維持し、追加の音声認識を実行せずに長い空白を警告します。

両方のJSONファイルと`--json`の結果に、`status`、`coverage_ratio`、`duration_seconds`、
`covered_seconds`、`long_gaps`（各項目は`start_seconds`と`end_seconds`を含む）を
持つ`completeness`レポートが含まれます。`needs-review`や`unverified`などの状態は
評価結果を示すもので、文字起こしの完全性を保証する状態はありません。
すべての警告はターミナルにも表示されます。

実際の接続検証では[TED動画](https://www.youtube.com/watch?v=iG9CE55wbtY)
（1,203秒、英語の手動字幕427セグメント）を使い、7種類すべてのファイルを出力しました。
SRTとWebVTTを再解析した結果、文章は`segments.json`と一致し、時刻の誤差は
最大0.51ミリ秒でした。[時刻のみのテストデータ](../tests/fixtures/ted_caption_timing.json)には
字幕の文章を含めず、元の時刻と、冒頭・内部・末尾の欠落を意図的に加えた合計4件の
オフライン回帰テストで使用します。これはプラットフォーム字幕の出力経路の検証です。
より広い実動画のOCR/Whisper CER/WER評価は未実施です。

生成メディアの基本動作テストでは、実際のMLX Whisper `base`で英語17語の音声を
処理し、単語の編集誤りは0件でした。ネイティブApple Visionを`accurate`で6秒動画に
適用し、各2秒の字幕3件をすべて認識しました。これらの小規模な生成例から、
幅広い実動画の精度を判断することはできません。

307.5秒の生成音声を実際に処理し、最初の300秒チャンクの完了後に中断して、
残りのチャンクだけを再実行しました。一時音声チャンクは削除済みです。

## 必要条件

- Apple Silicon Mac（`arm64`）とmacOS 13以降、または64ビットx86 Windows
- Python 3.11–3.14
- `PATH`から実行できるFFmpegとFFprobe
- Macのソースビルドのみ：Apple Vision OCRヘルパー用のXcode Command Line Tools
- `uv`を推奨

macOSでFFmpegがない場合は、Homebrewでインストールします。

```bash
brew install ffmpeg
```

Macでソースからビルドする場合、Appleのコマンドラインツールがなければインストールします。

```bash
xcode-select --install
```

## インストール

両プラットフォームで、引き続きソースからのインストールをサポートします。
0.2.0のmacOS ARM64 wheelは、ソースディレクトリ外の新規環境で基本動作を検証済みです。
Apple Visionヘルパーを内蔵し、実行時にSwiftコンパイラーは不要です。Windows CIには
汎用wheelのビルドを設定しましたが、その新しいwheelのWindowsでの検証は未実施です。
PyPIやGitHub Releaseへの公開はまだ行っていません。

ビルド済みmacOS wheelがある場合は、次のようにインストールします。

```bash
python -m pip install ./youtubetext-0.2.0-py3-none-macosx_13_0_arm64.whl
```

ビルド済みWindows wheelはPowerShellからインストールします。

```powershell
python -m pip install .\youtubetext-0.2.0-py3-none-any.whl
```

FFmpeg/FFprobeとPython 3.11–3.14は別途必要です。音声モデルは初回使用時に
ダウンロードします。`youtubetext-0.2.0.tar.gz`を含むMacのソースインストールには、
Xcode Command Line Toolsが必要です。以下はソースを取得してインストールする手順です。

macOSでは次を実行します。

```bash
git clone https://github.com/yuchenzhu-research/YouTubeText.git
cd YouTubeText
./scripts/install.sh
```

GitHub SSHをすでに設定している場合：

```bash
git clone git@github.com:yuchenzhu-research/YouTubeText.git
```

Windows x64では、まず64ビットPythonとFFmpegをインストールし、FFmpegの`bin`
ディレクトリを`PATH`に追加してから、PowerShellで次を実行します。

```powershell
git clone https://github.com/yuchenzhu-research/YouTubeText.git
Set-Location YouTubeText
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\install.ps1
```

両インストーラーは`.venv`を作成し、プラットフォーム別のPython依存関係を
インストールします。macOS版はApple Vision OCRヘルパーもビルドしますが、
Windows版はSwiftを使用しません。どちらもFFmpegはインストールしません。
macOS版は`sudo`を使用せず、Homebrewも自動ではインストールしません。

macOSではインストール後にローカル環境を確認します。

```bash
./.venv/bin/youtubetext doctor
```

## クイックスタート

1本の動画を処理します。

```bash
./.venv/bin/youtubetext "https://www.youtube.com/watch?v=VIDEO_ID"
```

YouTubeとBilibiliのURLを、2つのURLタスクで並行処理するキューに追加します。

```bash
./.venv/bin/youtubetext \
  "https://www.youtube.com/watch?v=VIDEO_ID" \
  "https://www.bilibili.com/video/BV_ID" \
  --jobs 2
```

出力ディレクトリを選び、元の文字が繁体字中国語であることをヒントとして指定します。

```bash
./.venv/bin/youtubetext INPUT \
  --output ~/Desktop/YouTubeText-output \
  --language zh-Hant
```

機械可読な結果を返します。

```bash
./.venv/bin/youtubetext INPUT --json
```

すべてのオプションを表示します。

```bash
./.venv/bin/youtubetext --help
```

Windowsでは、PowerShellから`.venv\Scripts`内の実行ファイルを使用します。例：

```powershell
.\.venv\Scripts\youtubetext.exe doctor
.\.venv\Scripts\youtubetext.exe "https://www.youtube.com/watch?v=VIDEO_ID"
.\.venv\Scripts\youtubetext.exe INPUT_1 INPUT_2 --jobs 2
.\.venv\Scripts\youtubetext.exe INPUT --plan
.\.venv\Scripts\youtubetext.exe INPUT --resume
.\.venv\Scripts\youtubetext.exe cache
```

以降のコマンド例ではmacOSの実行ファイルパスを使います。Windowsでは
`./.venv/bin/youtubetext`を`.\.venv\Scripts\youtubetext.exe`に置き換えてください。

## ローカル入力

CLIは`INPUT [INPUT...]`を受け付けます。リモートURL、ローカルの音声・動画パス、
このコンピューターの`file://` URLを指定できます。空白を含むパスは引用符で囲んでください。

```bash
./.venv/bin/youtubetext "/path/to/video.mp4" "/path/to/audio.wav" --resume
./.venv/bin/youtubetext "file:///path/to/video.mp4" --plan
```

元のファイルはその場所で読み取り、タスク領域へのコピーや削除は行いません。
内容のSHA-256識別子により、元のバイトが変わると古いキャッシュは無効になります。
同じ名前の隣接ファイル`video.vtt`や`video.srt`は、`auto`または`captions`で
`local-captions`として利用されます。両方ある場合はWebVTTを優先します。
利用可能な字幕がなければ、`auto`は音声をWhisperへ送り、動画には通常の
OCRからWhisperへ進む方針を適用します。`ocr`と`hybrid`には動画ストリームが必要です。
ローカルの`--plan`はFFprobeと内容ハッシュを使い、ネットワーク接続やメディア・モデルの
ダウンロードは行いません。

## 実行前プラン

字幕またはメディアファイルをダウンロードする前に、新規実行で行われる処理を
`--plan`で確認できます。

```bash
./.venv/bin/youtubetext INPUT --plan
./.venv/bin/youtubetext INPUT_1 INPUT_2 --plan --json
```

リモートURLの実行前プランはネットワークでメタデータを取得し、ローカル入力は
その場所で確認します。次の内容を報告します。

- ソースのメタデータと再生時間
- プラットフォームが提示する最適な字幕トラック
- 字幕、OCR、Whisperで想定される処理経路
- 字幕またはメディアのダウンロードが`required`、`conditional`、`none`のいずれであるか
- 主要なメディア形式と、利用可能な音声フォールバック

提示されたトラックには`advertised-unvalidated`と表示されます。実行前プランでは
ダウンロードも解析も行わないため、その時点では利用可能であることが保証されません。
実行前プランは字幕、音声、動画、Whisperモデルをダウンロードせず、フレームの
サンプリングやOCR/Whisperの実行も行わず、出力ディレクトリも作成せず、再開状態の
読み書きも行いません。

プランは`no-resume-reuse`というキャッシュの仮定を使用します。そのため、後から
`--resume`で実行すると、必須と表示された処理が省略される場合があります。
`--plan`と`--resume`は同時に使用できません。複数の入力は並行して確認され、結果は
入力順を維持します。また、1件のメタデータ取得失敗によって他のプランが非表示に
なることはありません。

## 再開とキャッシュ

再利用可能なローカル状態を明示的に有効にします。

```bash
./.venv/bin/youtubetext INPUT --resume
```

リモートURLでは最新のプラットフォーム字幕を先に確認し、ローカル字幕は再度読み取ります。
キャッシュミスの場合、yt-dlpは中断された`.part`ダウンロードを継続でき、完成済みの
メディアファイルはそのまま再利用されます。また、ローカルOCRエンジンは最大32フレームの
各バッチが正常に認識された後に、生のOCR観測結果を保存します。フレームエラーを
含むバッチはチェックポイントに保存されず、次回の実行時に再試行されます。
サンプリングした画像は、認識後に必ずバッチ単位で削除されます。チェックポイントを
書き込む場合は、画像を削除する前に保存します。

`--resume`では、Whisperの完了した各チャンクを保存します。各チャンクは最大300秒で、
次と2秒重なります。一時音声チャンクは同時に1つだけ存在します。メディアの内容、モデル、
言語、エンジンの版、分割設定が一致すると、完了したチャンクを再利用します。
中断したチャンクは再実行します。重複除去は保守的に行い、異なる文章や異なる時刻の
繰り返しは統合しません。短いファイルは直接処理できます。ローカルメディアの長さを
確認できない場合は、チャンクのチェックポイントを使わずにWhisperを実行します。

OCRの空白に対する追加のWhisper検証が失敗しても、元の文字起こしは出力されます。
ただし`--resume`では、メディアとOCRチェックポイントは未完了タスクとして保持され、
完成済み文字起こしのキャッシュには保存されません。次の`--resume`では検証を再試行し、
正常にダウンロードできたメディアと完了済みOCRバッチを再利用します。字幕の検証は
常に使用後に削除する一時ディレクトリを使い、`--resume`でもプラットフォーム字幕を
改めて確認します。

macOSでは、完成済みの構造化文字起こしは次の場所に保存されます。

```text
~/Library/Caches/YouTubeText/transcripts/
```

Windowsでは、キャッシュはユーザーのプラットフォームキャッシュディレクトリに
保存されます。正確な場所は`youtubetext cache`で確認できます。両プラットフォームの
未完了タスクは、同じ階層の`tasks/`ディレクトリに保存されます。macOSでは
ディレクトリとファイルに`0700`および`0600`の権限を使用し、Windowsでは
ファイルシステムの権限を使用します。同じ
タスクを要求する2つのプロセスは1つのロックを使用し、一方が処理を行い、もう一方は
その結果を再利用できるまで待機します。匿名リクエストと異なるCookieファイルには、
別々のキャッシュスコープが使用されます。

変更を加えずにキャッシュ使用量を確認します。

```bash
./.venv/bin/youtubetext cache
./.venv/bin/youtubetext cache --json
```

macOSでは、完了済みの文字起こしをすべて保持したまま、失敗または中断したタスクのメディアと
OCR/Whisperチェックポイントだけを削除します。

```bash
./.venv/bin/youtubetext cache clear-incomplete
```

実行中でロックされているタスクはスキップされます。削除された一時メディアは復元
できませんが、元のURLから再ダウンロードできます。Windowsでは
`cache clear-incomplete`はまだ未対応で、タスクデータは削除されません。
読み取り専用の`cache`と`cache --json`は利用できます。macOSで完了済みの
文字起こしも含めてすべて削除するには、`~/Library/Caches/YouTubeText/`を手動で削除してください。

## 認証とCookie

制限付き動画には、ローカルブラウザのログイン状態を使用できます。

```bash
./.venv/bin/youtubetext INPUT --cookies-from-browser safari
```

または、Netscape形式のCookieファイルを指定します。

```bash
./.venv/bin/youtubetext INPUT --cookies-file /path/to/cookies.txt
```

2つのオプションは同時に使用できません。Cookieの内容が出力ディレクトリ、
メタデータ、JSON結果、再開キャッシュに書き込まれることはありません。Cookie
ファイルは、yt-dlpの呼び出しごとにメモリへコピーされ、元のファイルは変更されません。

ブラウザ名だけでは使用中のアカウントを確実に識別できないため、
`--cookies-from-browser`と`--resume`は併用できません。認証と再開の両方が必要な
場合は、`--cookies-file`を使用してください。Safariでは、macOSの「プライバシーと
セキュリティ」設定でターミナルにフルディスクアクセスを許可する必要がある場合があります。
Safariの例はmacOS専用です。Windowsでは、インストール済みでyt-dlpに対応する
ブラウザーを選んでください。

## モード

| モード | 動作 |
| --- | --- |
| `auto` | プラットフォーム字幕やローカル字幕を優先し、動画にはOCR、音声や代替経路にはWhisperを使います。 |
| `captions` | 利用可能なプラットフォーム字幕またはローカルSRT/VTT字幕が必要です。 |
| `ocr` | プラットフォーム字幕を無視し、焼き込み字幕だけを読み取ります。 |
| `whisper` | 字幕とOCRをスキップし、ローカル音声認識を直接実行します。 |
| `hybrid` | OCRとWhisperを実行し、時間範囲が重なる場合は表示字幕を優先します。 |

たとえば、OCRを強制します。

```bash
./.venv/bin/youtubetext INPUT --mode ocr --language zh-Hant
```

## 言語

`--language`は次の値をサポートします。

```text
auto, en, zh-Hans, zh-Hant, es, ja, ko, fr, de, pt, it, ru, ar, hi, vi
```

`--language`は元の音声または画面上の文字の言語を示すヒントで、出力言語の
指定ではありません。プラットフォーム字幕の優先設定、対応するOCR、Whisperの
音声言語の選択に使われます。Whisperでは`zh-Hans`と`zh-Hant`がどちらも
`zh`として渡されるため、`zh-Hant`を選んでも結果の言語が`zh`となり、
簡体字で出力される場合があります。このツールは翻訳や簡体字・繁体字の変換は行いません。

macOSで`auto`を選択すると、Apple Visionはタイトルと投稿者の文脈を使って
認識言語を優先します。WindowsのRapidOCRに既定で含まれるPP-OCRv6 smallは、
モデル切り替えなしで`en`、`zh-Hans`、`zh-Hant`、`es`、`ja`、`fr`、
`de`、`pt`、`it`、`vi`を認識します。`--language`は出力を1言語に
限定しません。OCRは`ko`、`ru`、`ar`、`hi`には非対応です。OCR強制モード
ではエラーになり、`auto`と`hybrid`では警告のうえWhisperに切り替わります。
プラットフォーム字幕とWhisperではこれらの言語も利用できます。
どちらのWhisperエンジンも音声言語を自動検出できます。

プラットフォーム字幕の優先言語は、順序付きリストとして別途指定できます。

```bash
./.venv/bin/youtubetext INPUT \
  --caption-language zh-Hant \
  --caption-language zh-Hans \
  --caption-language en
```

## 出力ファイル

デフォルトの出力ルートは、現在のディレクトリにある`YouTubeText-output/`です。

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

- `transcript.md`：セグメントごとのタイムスタンプが付いた完全なMarkdown文字起こし。
- `transcript-clean.md`：タイムラインマーカーのない完全なMarkdown文字起こし。
- `transcript.txt`：タイムスタンプ付きのプレーンテキスト。
- `transcript.srt`：SRT対応のプレーヤーやエディター向けの、番号付き字幕セグメント。
- `transcript.vtt`：Webプレーヤーなどの対応ツール向けのWebVTT字幕セグメント。
- `segments.json`：各セグメントの開始・終了時刻を含む、文字起こしの全文。
- `metadata.json`：ソース、言語、抽出方法、セグメント数、警告、完全性の評価。

`segments.json`は`schema_version: 1`を使用し、ソースのメタデータ、言語、
抽出方法、警告、完全性の評価、順序付きの`segments`リストを含みます。各セグメントは
`start_seconds`、`end_seconds`、`text`、`confidence`を持ち、時刻は秒単位で
元の精度を保持します。SRTとWebVTTはミリ秒単位のタイムスタンプを使います。
`--json`は結果と出力ファイルのパスを返します。文字起こしの内容は
`segments.json`を読み取ってください。

## OCRの設定

`--ocr-profile`は固定サンプリング間隔を指定します。`fast`は1秒、`balanced`
（既定）は0.5秒、`accurate`は0.25秒です。2〜3時間の動画も同じ密度を維持し、
動画全体の2,400フレーム上限はありません。最大32フレームずつ処理し、認識後に削除します。
切り抜き画像が完全に同じ場合はOCR結果を再利用し、各サンプリング時刻は保持します。

`--ocr-region X Y WIDTH HEIGHT`は左上から測る正規化座標です。既定の
`0 0.55 1 0.45`は下部45%を対象にします。幅と高さは正の値で、領域全体を
フレーム内に収める必要があります。

```bash
./.venv/bin/youtubetext INPUT --ocr-profile accurate --ocr-region 0 0.55 1 0.45
```

固定密度でサンプリングし、同一画像を再利用します。選択した間隔より短く表示される
字幕は見逃す場合があります。密度を上げると処理量が増えます。

## 並行処理とリソース制御

`--jobs 0`がデフォルトで、物理メモリから入力の並行処理数の上限を決定します。

| 物理メモリ | 自動入力ジョブ数 |
| --- | ---: |
| 12 GiB未満 | 1 |
| 12–23 GiB | 2 |
| 24–39 GiB | 3 |
| 40 GiB以上 | 4 |

WindowsではCPU性能に応じて自動値がさらに小さくなる場合があります。
`--jobs 1`から`--jobs 8`を指定すると上書きできます。ネットワーク処理は最大4件、
macOSのApple Vision OCRは最大2件、WindowsのRapidOCRは1件を実行します。
Whisperは両プラットフォームで直列に実行し、メモリと計算資源の競合を抑えます。

OCRエンジンには最大32フレームのバッチを順次渡します。`--resume`では、
正常に認識したバッチを画像の削除前に保存します。フレームエラーを含むバッチは
保存せず、次回再試行します。チェックポイントには時刻、内容ハッシュ、生の文字領域を
保存し、画像や一時パスは保存しません。

## Whisperモデル

Whisperは、プラットフォーム字幕と焼き込み字幕のどちらも利用できない場合、`auto`で
不審な空白を補う必要がある場合、または`whisper` / `hybrid`モードが選択された場合に
実行されます。モデルは初回
使用時にダウンロードされ、その後はローカルのモデルキャッシュから再利用されます。
macOSでは、標準のHugging Faceキャッシュに互換性のあるMLXの重みがすでに
存在する場合、それも再利用されます。Windowsでは、別のキャッシュに保存された
CTranslate2の重みをfaster-whisperが使用します。

以下のダウンロード容量とメモリ使用量の目安はmacOSのMLXエンジンのみを対象と
します。Windowsではモデル容量と実行時メモリ使用量が異なる場合があります。

| モデル | おおよそのダウンロード容量 | おおよその実行時メモリ |
| --- | ---: | ---: |
| `base` | 144 MB | 1 GiB |
| `small` | 481 MB | 2 GiB |
| `large-v3-turbo` | 1.61 GB | 6 GiB |

自動選択では、メモリの少ないMacで`small`を使用し、16 GiB以上のMacでは
`large-v3-turbo`を使用します。Windowsではデフォルトで`small`を使用します。
`--whisper-model`で上書きできます。Windowsのdoctorはバックエンドの可用性を
確認しますが、CTranslate2モデルのキャッシュはまだ調べません。

## 開発とアーキテクチャ

開発ワークフロー全体を実行します。

```bash
./scripts/dev.sh
```

必要に応じて、スクリプト経由でpytestの引数を渡せます。

```bash
./scripts/dev.sh tests/test_acquisition.py -q
```

Windowsでは、PowerShellから開発スクリプトを実行します。

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\dev.ps1
```

主要モジュール：

- `sources/`：YouTube/Bilibiliのメタデータと試行回数を制限した字幕候補の切り替え。
- `inputs.py`：ローカルのメタデータ、内容識別、隣接字幕ファイル。
- `planning.py`：読み取り専用の実行前プラン、処理経路、ダウンロード要件。
- `media.py`：再開可能なメディアダウンロードと上限付きフレームサンプリング。
- `resume.py`：文字起こしの再利用、タスクロック、OCR/Whisperチェックポイント、クリーンアップ。
- `ocr/`：Apple VisionまたはRapidOCRと焼き込み字幕の組み立て。
- `asr/`：MLX Whisperまたはfaster-whisper、モデル選択、再開可能な音声チャンク。
- `acquisition.py`：字幕/OCR/Whisper間の正式なルーティング方針。
- `runtime.py`：ホストのリソース検出と、複数入力の順序を維持したスケジューリング。
- `export.py`：Markdown、TXT、SRT、WebVTT、JSON出力をまとめてアトミックに書き込む処理。
- `cli.py`：ターミナルインターフェース。

## 現在の制限

- Apple Silicon搭載macOSとWindows x64は自動CIテストに合格しています。
  Windows CIでは実際のローカルOCR・Whisperモデルと、オフラインのCLIから
  エクスポートまでの処理も検証済みです。Windows実機での実動画を使った
  エンドツーエンドの確認はまだ行っていません。公開URLはホスト型CIからの
  アクセスを拒否する場合があります。
- Windowsの`cache clear-incomplete`は現在利用できません。junctionや
  reparse pointをまたぐ安全な削除には、追加の検証が必要です。
- プラットフォームへのアクセスはyt-dlpに依存し、地域、アカウント、Cookie、
  プラットフォームの変更による影響を受ける場合があります。
- 既定のOCR領域は下部45%です。字幕の位置は`--ocr-region`で調整できますが、
  大きく装飾された文字ではWhisperモードが必要な場合があります。
- Bilibili動画のエンドツーエンドの文字起こしはmacOSで実際に検証済みです。
  プラットフォーム字幕の互換性は、公開動画を使ったより広範なテストが必要です。
- Whisperは完了したチャンクの境界から再開し、中断したチャンクを再実行します。
- OCRは固定間隔なので、`accurate`でも表示が0.25秒未満の字幕を見逃す場合があります。
- 時間の空白や無音の確認は、文字起こしの正確さや音声の欠落を証明しません。
  長い休止や音楽は確認が必要になる場合があります。これらのチェックについて、
  実際の動画による精度評価はまだ確立されていません。

## 参考資料とライセンス表記

設計にあたっては、[MediaBrief](https://github.com/EvilIrving/mediabrief)を参考に
しました。ローカルの参照用チェックアウトは`reference/mediabrief/`に保存できます。
`reference/`ディレクトリ全体は意図的にGitの対象外としており、このリポジトリには
含まれません。サードパーティ製ソフトウェアに関する通知については、
[`THIRD_PARTY_NOTICES.md`](../THIRD_PARTY_NOTICES.md)を参照してください。

0.2.0の変更は[CHANGELOG.md](../CHANGELOG.md)を参照してください。

YouTubeTextは[MITライセンス](../LICENSE)で提供します。
