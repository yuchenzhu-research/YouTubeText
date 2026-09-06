# YouTubeText

YouTubeText 是一款优先面向 macOS（Apple Silicon）的本地视频文字工具。它把
YouTube 与 Bilibili 链接转换成带时间戳的完整文字、Markdown、TXT、摘要与逻辑分析。

## 计划中的处理链

1. 提交一个或多个 URL，分别创建可取消、可重试的任务。
2. 识别来源并读取标题、时长、作者等元数据。
3. 优先抓取平台提供的人工字幕或自动字幕。
4. 没有字幕轨道时，先尝试识别画面中的硬字幕；没有硬字幕时使用 Whisper 识别语音。
5. 使用本地 Ollama 或用户选择的云端 API 清洗文字、生成摘要、翻译并分析论证逻辑。
6. 导出完整 Markdown 与 TXT，保留来源和时间戳。

OCR、ASR、LLM 与平台访问都通过独立接口接入。首版实现 Apple Vision OCR、MLX
Whisper、本地 Ollama/OpenAI 兼容 API，以及 YouTube/Bilibili 两个平台；后续平台只需
增加适配器。

## 本地参考代码

MediaBrief 原始仓库保存在本机 `reference/mediabrief/`，用于阅读和行为对照；
`reference/` 已被 Git 忽略，不会被提交到 YouTubeText 仓库。采用或改写其代码时，
必须保留 MIT 许可要求的版权与许可声明，详见 `THIRD_PARTY_NOTICES.md`。
