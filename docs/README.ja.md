# YouTubeText

[English](../README.md) | [繁體中文](README.zh-Hant.md) | [简体中文](README.zh-Hans.md) | [Español](README.es.md) | 日本語

YouTubeTextは、YouTubeおよびBilibiliの動画から、読みやすくタイムスタンプ付きの
文字起こしを生成するApple Silicon Mac向けのターミナルツールです。タイムスタンプ
付きMarkdownファイル、タイムスタンプなしの読みやすいMarkdownファイル、テキスト
ファイル、構造化メタデータを出力します。

YouTubeTextが抽出するのは全文のみです。動画の要約や論理分析は行わず、LLM、
Ollama、クラウドAI APIも必要ありません。

## 機能

- 1つのコマンドで、単一のURLまたは複数URLのキューを送信できます。
- YouTubeとBilibiliのソースを判別し、メタデータを読み取ります。
- 利用可能な場合は、プラットフォームの手動字幕または自動字幕を優先します。
- 利用可能なプラットフォーム字幕がない場合は、Apple Visionで焼き込み字幕を読み取ります。
- OCRを利用できない場合は、ローカルのMLX Whisper音声認識にフォールバックします。
- タイムスタンプ付きと、タイムスタンプなしの読みやすいMarkdown文字起こしを出力します。
- Macの物理メモリから安全なURL並行処理数を自動的に選択します。
- 完了した文字起こしを再利用し、中断されたメディアのダウンロードを再開し、
  完了済みOCRバッチを再利用できます。
- 字幕またはメディアファイルをダウンロードする前に、`--plan`でURLを確認できます。
- 障害を分離し、1つのURLが失敗してもキュー内の残りをキャンセルしません。

## パイプライン

```text
URLキュー
  └─ プラットフォームを判別してメタデータを読み取る
       ├─ 利用可能なプラットフォーム字幕 ─────────────→ 字幕を整形 ─────────→ 出力
       └─ 利用可能な字幕なし
            ├─ --resume のキャッシュヒット ─────────────────────────────→ 出力
            └─ キャッシュミスまたは再開無効 → 解析用動画 → Vision OCR
                                                     ├─ 利用可能 → 出力
                                                     └─ 利用不可 → Whisper → 出力
```

プラットフォーム字幕の経路では、動画をダウンロードしません。字幕ファイルは小さな
一時入力です。通常の実行では、OCRおよびWhisper用のメディアを、実行ごとに管理
される一時ディレクトリに保存し、実行終了時に削除します。`--resume`を指定すると、
メディアはユーザーキャッシュの`tasks/`ディレクトリに保存され、中断後も保持され、
正常終了後に削除されます。

## 必要条件

- Apple Silicon Mac（`arm64`）
- macOS 13以降
- Python 3.11–3.14
- FFmpeg
- Apple Vision OCRヘルパーのビルドに使用するXcode Command Line Tools
- `uv`を推奨

FFmpegがない場合は、Homebrewでインストールします。

```bash
brew install ffmpeg
```

Appleのコマンドラインツールがない場合はインストールします。

```bash
xcode-select --install
```

## インストール

現在サポートされているインストール方法は、ソースのチェックアウトです。
YouTubeTextは、まだPyPIパッケージまたはビルド済みwheelとして公開されていません。

```bash
git clone https://github.com/yuchenzhu-research/YouTubeText.git
cd YouTubeText
./scripts/install.sh
```

GitHub SSHをすでに設定している場合：

```bash
git clone git@github.com:yuchenzhu-research/YouTubeText.git
```

インストーラーは`.venv`を作成し、Python依存関係をインストールして、Apple Vision
OCRヘルパーをビルドします。`sudo`は使用せず、Homebrewも自動ではインストールしません。

インストール後にローカル環境を確認します。

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

出力ディレクトリと繁体字中国語認識を選択します。

```bash
./.venv/bin/youtubetext URL \
  --output ~/Desktop/YouTubeText-output \
  --language zh-Hant
```

機械可読な結果を返します。

```bash
./.venv/bin/youtubetext URL --json
```

すべてのオプションを表示します。

```bash
./.venv/bin/youtubetext --help
```

## 実行前プラン

字幕またはメディアファイルをダウンロードする前に、新規実行で行われる処理を
`--plan`で確認できます。

```bash
./.venv/bin/youtubetext URL --plan
./.venv/bin/youtubetext URL_1 URL_2 --plan --json
```

実行前プランは、URLごとにネットワーク経由でメタデータを取得し、次の内容を報告します。

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
`--plan`と`--resume`は同時に使用できません。複数URLは並行して確認され、結果は
入力順を維持します。また、1件のメタデータ取得失敗によって他のプランが非表示に
なることはありません。

## 再開とキャッシュ

再利用可能なローカル状態を明示的に有効にします。

```bash
./.venv/bin/youtubetext URL --resume
```

YouTubeTextは、まずプラットフォームに新しい字幕がないかを引き続き確認します。
キャッシュミスの場合、yt-dlpは中断された`.part`ダウンロードを継続でき、完成済みの
メディアファイルはそのまま再利用されます。また、Apple Visionは最大32フレームの
各バッチが正常に認識された後に、生のOCR観測結果を保存します。フレームエラーを
含むバッチはチェックポイントに保存されず、次回の実行時に再試行されます。
サンプリングした画像は、認識後に必ずバッチ単位で削除されます。チェックポイントを
書き込む場合は、画像を削除する前に保存します。Whisperの推論は、まだ途中から
再開できません。

完成済みの構造化文字起こしは、次の場所に保存されます。

```text
~/Library/Caches/YouTubeText/transcripts/
```

未完了のタスクは、同じ階層の`tasks/`ディレクトリに保存されます。ディレクトリと
ファイルは、`0700`および`0600`の権限で現在のユーザーだけに制限されます。同じ
タスクを要求する2つのプロセスは1つのロックを使用し、一方が処理を行い、もう一方は
その結果を再利用できるまで待機します。匿名リクエストと異なるCookieファイルには、
別々のキャッシュスコープが使用されます。

変更を加えずにキャッシュ使用量を確認します。

```bash
./.venv/bin/youtubetext cache
./.venv/bin/youtubetext cache --json
```

完了済みの文字起こしをすべて保持したまま、失敗または中断したタスクのメディアと
OCRチェックポイントだけを削除します。

```bash
./.venv/bin/youtubetext cache clear-incomplete
```

実行中でロックされているタスクはスキップされます。削除された一時メディアは復元
できませんが、元のURLから再ダウンロードできます。完了済みの文字起こしを含む
すべてを削除するには、`~/Library/Caches/YouTubeText/`を手動で削除してください。

## 認証とCookie

制限付き動画には、ローカルブラウザのログイン状態を使用できます。

```bash
./.venv/bin/youtubetext URL --cookies-from-browser safari
```

または、Netscape形式のCookieファイルを指定します。

```bash
./.venv/bin/youtubetext URL --cookies-file /path/to/cookies.txt
```

2つのオプションは同時に使用できません。Cookieの内容が出力ディレクトリ、
メタデータ、JSON結果、再開キャッシュに書き込まれることはありません。Cookie
ファイルは、yt-dlpの呼び出しごとにメモリへコピーされ、元のファイルは変更されません。

ブラウザ名だけでは使用中のアカウントを確実に識別できないため、
`--cookies-from-browser`と`--resume`は併用できません。認証と再開の両方が必要な
場合は、`--cookies-file`を使用してください。Safariでは、macOSの「プライバシーと
セキュリティ」設定でターミナルにフルディスクアクセスを許可する必要がある場合があります。

## モード

| モード | 動作 |
| --- | --- |
| `auto` | プラットフォーム字幕、OCR、Whisperの順に優先します。 |
| `captions` | プラットフォーム字幕トラックを必須とし、利用できない場合は失敗します。 |
| `ocr` | プラットフォーム字幕を無視し、焼き込み字幕だけを読み取ります。 |
| `whisper` | 字幕とOCRをスキップし、ローカル音声認識を直接実行します。 |
| `hybrid` | OCRとWhisperを実行し、時間範囲が重なる場合は表示字幕を優先します。 |

たとえば、OCRを強制します。

```bash
./.venv/bin/youtubetext URL --mode ocr --language zh-Hant
```

## 言語

`--language`は次の値をサポートします。

```text
auto, en, zh-Hans, zh-Hant, es, ja, ko, fr, de, pt, it, ru, ar, hi, vi
```

`auto`を選択すると、Apple Visionはタイトルと投稿者の文脈を使って認識言語を
優先します。Whisperは、音声言語を独自に検出します。

プラットフォーム字幕の優先言語は、順序付きリストとして別途指定できます。

```bash
./.venv/bin/youtubetext URL \
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
    metadata.json
```

- `transcript.md`：セグメントごとのタイムスタンプが付いた完全なMarkdown文字起こし。
- `transcript-clean.md`：タイムラインマーカーのない完全なMarkdown文字起こし。
- `transcript.txt`：タイムスタンプ付きのプレーンテキスト。
- `metadata.json`：ソース、言語、抽出方法、セグメント数、警告。

## 並行処理とリソース制御

`--jobs 0`がデフォルトで、物理メモリからURLの並行処理数を決定します。

| 物理メモリ | 自動URLジョブ数 |
| --- | ---: |
| 12 GiB未満 | 1 |
| 12–23 GiB | 2 |
| 24–39 GiB | 3 |
| 40 GiB以上 | 4 |

`--jobs 1`から`--jobs 8`を指定すると上書きできます。ネットワーク処理は最大4件、
Apple Vision OCRは最大2件を同時に実行します。Whisperは、ユニファイドメモリと
Metalの競合を避けるため、直列に実行されます。

OCRは通常1秒ごとに1フレームをサンプリングし、長い動画では2,400フレームを上限と
します。Visionには、切り抜かれた最大32フレームのバッチが渡され、画像は認識後に
必ずバッチ単位で削除されます。`--resume`を指定した場合、正常に認識されたバッチは
削除前にチェックポイントへ保存されます。フレームエラーを含むバッチは保存されず、
次回の実行時に再試行されます。チェックポイントに保存されるのは、タイムスタンプ、
コンテンツハッシュ、生のテキストボックスであり、フレーム画像や一時パスは保存されません。

## Whisperモデル

Whisperは、プラットフォーム字幕と焼き込み字幕のどちらも利用できない場合、または
`whisper` / `hybrid`モードが選択された場合にのみ実行されます。モデルは初回
使用時にダウンロードされ、その後はローカルのモデルキャッシュから再利用されます。
標準のHugging Faceキャッシュに互換性のある重みがすでに存在する場合は、それも
再利用されます。

| モデル | おおよそのダウンロード容量 | おおよその実行時メモリ |
| --- | ---: | ---: |
| `base` | 144 MB | 1 GiB |
| `small` | 481 MB | 2 GiB |
| `large-v3-turbo` | 1.61 GB | 6 GiB |

自動選択では、メモリの少ないMacで`small`を使用し、16 GiB以上のMacでは
`large-v3-turbo`を使用します。`--whisper-model`で上書きできます。

## 開発とアーキテクチャ

開発ワークフロー全体を実行します。

```bash
./scripts/dev.sh
```

必要に応じて、スクリプト経由でpytestの引数を渡せます。

```bash
./scripts/dev.sh tests/test_acquisition.py -q
```

主要モジュール：

- `sources/`：YouTube/Bilibiliのメタデータとプラットフォーム字幕。
- `planning.py`：読み取り専用の実行前プラン、処理経路、ダウンロード要件。
- `media.py`：再開可能なメディアダウンロードと上限付きフレームサンプリング。
- `resume.py`：文字起こしの再利用、タスクロック、OCRチェックポイント、タスクのクリーンアップ。
- `ocr/`：Apple Visionの呼び出しと焼き込み字幕の組み立て。
- `asr/`：MLX Whisper、モデル選択、モデルキャッシュの再利用。
- `acquisition.py`：字幕/OCR/Whisper間の正式なルーティング方針。
- `runtime.py`：Macのリソース検出と、複数URLの順序を維持したスケジューリング。
- `export.py`：Markdown、TXT、JSON出力をまとめてアトミックに書き込む処理。
- `cli.py`：ターミナルインターフェース。

## 現在の制限

- Apple Silicon搭載macOSのみをサポートしています。
- プラットフォームへのアクセスはyt-dlpに依存し、地域、アカウント、Cookie、
  プラットフォームの変更による影響を受ける場合があります。
- Apple Vision OCRは、フレーム下部付近の字幕向けに最適化されています。
  その他のレイアウトや大きく装飾されたテキストでは、Whisperモードが必要になる
  場合があります。
- Bilibiliのメタデータと一時メディアパスは実際の環境で検証済みですが、字幕の
  互換性については、公開動画を使ったより広範なテストが必要です。
- Whisperは、推論途中の位置から再開できません。

## 参考資料とライセンス表記

設計にあたっては、[MediaBrief](https://github.com/EvilIrving/mediabrief)を参考に
しました。ローカルの参照用チェックアウトは`reference/mediabrief/`に保存できます。
`reference/`ディレクトリ全体は意図的にGitの対象外としており、このリポジトリには
含まれません。サードパーティ製ソフトウェアに関する通知については、
[`THIRD_PARTY_NOTICES.md`](../THIRD_PARTY_NOTICES.md)を参照してください。
