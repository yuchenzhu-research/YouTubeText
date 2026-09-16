# YouTubeText

[English](../README.md) | [繁體中文](README.zh-Hant.md) | [简体中文](README.zh-Hans.md) | Español | [日本語](README.ja.md)

YouTubeText es una herramienta de terminal para macOS con Apple Silicon y Windows x64 que
convierte vídeos de YouTube y Bilibili en transcripciones limpias con marcas de
tiempo. Exporta un archivo Markdown con marcas de tiempo, un archivo Markdown
limpio sin ellas, un archivo de texto y metadatos estructurados.

YouTubeText solo extrae el texto completo. No resume vídeos, no analiza
argumentos ni requiere un LLM, Ollama o una API de IA en la nube.

## Funciones

- Envía una URL o una cola de URLs en el mismo comando.
- Detecta fuentes de YouTube y Bilibili y lee sus metadatos.
- Da prioridad a los subtítulos manuales o automáticos de la plataforma cuando están disponibles.
- Lee subtítulos incrustados con Apple Vision en macOS o RapidOCR en Windows cuando no hay una pista de la plataforma utilizable.
- Recurre a MLX Whisper en macOS o faster-whisper en Windows cuando no se puede usar OCR.
- Exporta transcripciones Markdown con marcas de tiempo y transcripciones limpias sin ellas.
- Elige automáticamente la concurrencia de URLs según la memoria física y, en Windows, la capacidad de la CPU.
- Reutiliza transcripciones terminadas, reanuda descargas multimedia
  interrumpidas y reutiliza lotes de OCR completados.
- Inspecciona una URL con `--plan` antes de descargar cualquier archivo de subtítulos o contenido multimedia.
- Aísla los fallos: una URL problemática no cancela el resto de la cola.

## Flujo de procesamiento

```text
Cola de URLs
  └─ Detectar la plataforma y leer los metadatos
       ├─ Subtítulo utilizable de la plataforma ─────────────→ Limpiar segmentos ─────────→ Exportar
       └─ Sin subtítulo utilizable
            ├─ acierto de caché con --resume ─────────────────────────────→ Exportar
            └─ fallo de caché o reanudación desactivada → vídeo de análisis → OCR local
                                                                          ├─ utilizable → Exportar
                                                                          └─ no utilizable → Whisper → Exportar
```

La ruta de subtítulos de la plataforma nunca descarga el vídeo. Los archivos de
subtítulos son entradas temporales pequeñas. Las ejecuciones normales guardan
los medios para OCR y Whisper en un directorio temporal supervisado y exclusivo
de cada ejecución, y lo eliminan al finalizar. Con `--resume`, los medios se
guardan en el directorio `tasks/` de la caché del usuario, se conservan después
de una interrupción y se eliminan tras una finalización correcta.

## Requisitos

- Mac con Apple Silicon (`arm64`) y macOS 13 o posterior, o Windows x86 de 64 bits
- Python 3.11–3.14
- FFmpeg disponible en `PATH`
- Solo en macOS: Xcode Command Line Tools para compilar el auxiliar de OCR de Apple Vision
- Se recomienda `uv`

En macOS, instala FFmpeg con Homebrew si no está disponible:

```bash
brew install ffmpeg
```

En macOS, instala las herramientas de línea de comandos de Apple si no están disponibles:

```bash
xcode-select --install
```

## Instalación

El método de instalación compatible en ambas plataformas parte de una copia del
código fuente. YouTubeText todavía no se publica como paquete de PyPI ni como wheel
precompilado.

En macOS, ejecuta:

```bash
git clone https://github.com/yuchenzhu-research/YouTubeText.git
cd YouTubeText
./scripts/install.sh
```

Si SSH de GitHub ya está configurado:

```bash
git clone git@github.com:yuchenzhu-research/YouTubeText.git
```

En Windows x64, instala primero Python de 64 bits y FFmpeg, añade el directorio
`bin` de FFmpeg a `PATH` y ejecuta en PowerShell:

```powershell
git clone https://github.com/yuchenzhu-research/YouTubeText.git
Set-Location YouTubeText
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\install.ps1
```

Ambos instaladores crean `.venv` e instalan las dependencias de Python propias
del sistema. El instalador de macOS también compila el auxiliar de OCR de Apple
Vision; el de Windows no usa Swift. Ninguno instala FFmpeg. El instalador de
macOS no usa `sudo` ni instala Homebrew automáticamente.

En macOS, comprueba el entorno local después de la instalación:

```bash
./.venv/bin/youtubetext doctor
```

## Inicio rápido

Procesa un vídeo:

```bash
./.venv/bin/youtubetext "https://www.youtube.com/watch?v=VIDEO_ID"
```

Pon en cola URLs de YouTube y Bilibili con dos tareas de URL simultáneas:

```bash
./.venv/bin/youtubetext \
  "https://www.youtube.com/watch?v=VIDEO_ID" \
  "https://www.bilibili.com/video/BV_ID" \
  --jobs 2
```

Elige un directorio de salida e indica que el texto de origen puede estar en chino tradicional:

```bash
./.venv/bin/youtubetext URL \
  --output ~/Desktop/YouTubeText-output \
  --language zh-Hant
```

Devuelve resultados legibles por máquinas:

```bash
./.venv/bin/youtubetext URL --json
```

Muestra todas las opciones:

```bash
./.venv/bin/youtubetext --help
```

En Windows, usa el ejecutable de `.venv\Scripts` desde PowerShell; por ejemplo:

```powershell
.\.venv\Scripts\youtubetext.exe doctor
.\.venv\Scripts\youtubetext.exe "https://www.youtube.com/watch?v=VIDEO_ID"
.\.venv\Scripts\youtubetext.exe URL_1 URL_2 --jobs 2
.\.venv\Scripts\youtubetext.exe URL --plan
.\.venv\Scripts\youtubetext.exe URL --resume
.\.venv\Scripts\youtubetext.exe cache
```

Los demás ejemplos de comandos usan la ruta del ejecutable de macOS. En
Windows, sustituye `./.venv/bin/youtubetext` por `.\.venv\Scripts\youtubetext.exe`.

## Plan previo a la ejecución

Usa `--plan` para inspeccionar qué haría una ejecución nueva antes de descargar
cualquier archivo de subtítulos o contenido multimedia:

```bash
./.venv/bin/youtubetext URL --plan
./.venv/bin/youtubetext URL_1 URL_2 --plan --json
```

El plan previo a la ejecución realiza una solicitud de metadatos en red por cada
URL e informa de:

- los metadatos de la fuente y la duración;
- la mejor pista de subtítulos anunciada por la plataforma;
- la ruta prevista para subtítulos, OCR y Whisper;
- si las descargas de subtítulos o medios son `required`, `conditional` o `none`;
- el tipo principal de medio y cualquier alternativa de audio.

Una pista anunciada se marca como `advertised-unvalidated`: el plan previo a la
ejecución no la descarga ni la analiza, por lo que todavía no se garantiza su
disponibilidad. El plan previo a la ejecución no descarga subtítulos, audio,
vídeo ni modelos de Whisper; no muestrea fotogramas ni ejecuta OCR/Whisper; no
crea un directorio de salida; y no lee ni escribe el estado de reanudación.

Los planes usan el supuesto de caché `no-resume-reuse`. Por ello, una ejecución
posterior con `--resume` puede omitir trabajo mostrado como obligatorio.
`--plan` y `--resume` no se pueden usar juntos. Se inspeccionan varias URLs de
forma simultánea, los resultados conservan el orden de entrada y un fallo de
metadatos no oculta los demás planes.

## Reanudación y caché

Activa explícitamente el estado local reutilizable:

```bash
./.venv/bin/youtubetext URL --resume
```

YouTubeText sigue comprobando primero si la plataforma ofrece un subtítulo
nuevo. Si no se encuentra en caché, yt-dlp puede continuar una descarga
`.part` interrumpida; un archivo multimedia completo se reutiliza directamente;
y el motor OCR local guarda las observaciones sin procesar después de cada lote de
hasta 32 fotogramas reconocido correctamente. Un lote que contenga errores de
fotograma no se guarda como punto de control y se vuelve a intentar en la
siguiente ejecución. Las imágenes muestreadas se eliminan lote a lote después
del reconocimiento; cuando se escribe un punto de control, se guarda antes de
eliminarlas. La inferencia de Whisper todavía no puede reanudarse a mitad del
proceso.

En macOS, las transcripciones estructuradas terminadas se guardan en:

```text
~/Library/Caches/YouTubeText/transcripts/
```

En Windows, la caché se encuentra en el directorio de caché del usuario; ejecuta
`youtubetext cache` para ver la ubicación exacta. En ambas plataformas, las
tareas incompletas se guardan en el directorio hermano `tasks/`. En macOS, los
directorios y archivos usan permisos `0700` y `0600`; Windows emplea los permisos
de su sistema de archivos. Dos procesos que soliciten la misma tarea usan un único bloqueo: uno
realiza el trabajo y el otro espera para reutilizar el resultado. Las solicitudes
anónimas y los distintos archivos de cookies usan ámbitos de caché separados.

Consulta el uso de la caché sin modificarla:

```bash
./.venv/bin/youtubetext cache
./.venv/bin/youtubetext cache --json
```

En macOS, elimina solo los medios de tareas fallidas o interrumpidas y sus puntos de
control de OCR, conservando todas las transcripciones terminadas:

```bash
./.venv/bin/youtubetext cache clear-incomplete
```

Las tareas activas y bloqueadas se omiten. Los medios temporales eliminados no
se pueden recuperar, pero pueden descargarse de nuevo desde la URL original.
En Windows, `cache clear-incomplete` aún no está disponible y no elimina los
datos de las tareas; `cache` y `cache --json` siguen disponibles en modo de solo
lectura. Para eliminarlo todo en macOS, incluidas las transcripciones terminadas,
borra `~/Library/Caches/YouTubeText/` manualmente.

## Autenticación y cookies

Usa la sesión iniciada de un navegador local para vídeos restringidos:

```bash
./.venv/bin/youtubetext URL --cookies-from-browser safari
```

O proporciona un archivo de cookies en formato Netscape:

```bash
./.venv/bin/youtubetext URL --cookies-file /path/to/cookies.txt
```

Las dos opciones se excluyen mutuamente. El contenido de las cookies nunca se
escribe en el directorio de salida, los metadatos, los resultados JSON ni la
caché de reanudación. Los archivos de cookies se copian en memoria en cada
llamada a yt-dlp; el archivo original no se modifica.

Como un nombre de navegador no permite identificar de forma fiable la cuenta
activa, `--cookies-from-browser` no se puede combinar con `--resume`. Usa
`--cookies-file` cuando se necesiten a la vez autenticación y reanudación. Es
posible que Safari necesite Acceso total al disco para el terminal en los
ajustes de Privacidad y seguridad de macOS.
El ejemplo de Safari es exclusivo de macOS; en Windows, elige un navegador
instalado que sea compatible con yt-dlp.

## Modos

| Modo | Comportamiento |
| --- | --- |
| `auto` | Da prioridad a los subtítulos de la plataforma, después a OCR y por último a Whisper. |
| `captions` | Exige una pista de subtítulos de la plataforma; falla cuando no hay ninguna disponible. |
| `ocr` | Ignora los subtítulos de la plataforma y lee únicamente los subtítulos incrustados. |
| `whisper` | Omite los subtítulos y OCR; ejecuta directamente el reconocimiento de voz local. |
| `hybrid` | Ejecuta OCR y Whisper; los subtítulos visibles tienen prioridad cuando sus intervalos de tiempo se solapan. |

Por ejemplo, fuerza OCR:

```bash
./.venv/bin/youtubetext URL --mode ocr --language zh-Hant
```

## Idiomas

`--language` admite:

```text
auto, en, zh-Hans, zh-Hant, es, ja, ko, fr, de, pt, it, ru, ar, hi, vi
```

`--language` es una pista sobre el idioma hablado o el texto visible de origen,
no una selección del idioma de salida. Orienta la preferencia por subtítulos de
la plataforma, el OCR cuando está disponible y el idioma de voz de Whisper.
Whisper reduce tanto `zh-Hans` como `zh-Hant` a `zh`: el resultado puede indicar
`zh` y contener caracteres simplificados incluso si se eligió `zh-Hant`. La
herramienta no traduce ni convierte entre chino simplificado y tradicional.

En macOS, Apple Vision usa el contexto del título y del autor para priorizar
los idiomas de reconocimiento cuando se selecciona `auto`. En Windows, el
modelo predeterminado PP-OCRv6 small de RapidOCR reconoce `en`, `zh-Hans`,
`zh-Hant`, `es`, `ja`, `fr`, `de`, `pt`, `it` y `vi` sin cambiar de modelo;
`--language` no limita la salida a un solo idioma. El OCR no admite `ko`, `ru`,
`ar` ni `hi`: el modo OCR forzado muestra un error, mientras que `auto` e
`hybrid` avisan y usan Whisper. Los subtítulos de la plataforma y Whisper sí
pueden usar esos idiomas. Ambos motores Whisper pueden detectar automáticamente
el idioma hablado.

Configura por separado una lista ordenada de idiomas preferidos para los
subtítulos de la plataforma:

```bash
./.venv/bin/youtubetext URL \
  --caption-language zh-Hant \
  --caption-language zh-Hans \
  --caption-language en
```

## Archivos de salida

La raíz de salida predeterminada es `YouTubeText-output/` en el directorio
actual:

```text
YouTubeText-output/
  VIDEO_ID-video-title/
    transcript.md
    transcript-clean.md
    transcript.txt
    metadata.json
```

- `transcript.md`: transcripción Markdown completa con marcas de tiempo por segmento.
- `transcript-clean.md`: transcripción Markdown completa sin marcadores de línea temporal.
- `transcript.txt`: texto sin formato con marcas de tiempo.
- `metadata.json`: fuente, idioma, método de extracción, número de segmentos y advertencias.

## Concurrencia y control de recursos

`--jobs 0` es el valor predeterminado y determina un límite de concurrencia de
URLs a partir de la memoria física:

| Memoria física | Tareas de URL automáticas |
| --- | ---: |
| Menos de 12 GiB | 1 |
| 12–23 GiB | 2 |
| 24–39 GiB | 3 |
| 40 GiB o más | 4 |

En Windows, la capacidad de la CPU puede reducir aún más el valor automático.
Sobrescríbelo con valores de `--jobs 1` a `--jobs 8`. Se ejecutan como máximo
cuatro operaciones de red; Apple Vision usa hasta dos tareas de OCR en macOS y
RapidOCR usa una en Windows. Whisper se ejecuta en serie en ambas plataformas
para limitar la competencia por memoria y capacidad de cómputo.

Por lo general, OCR muestrea un fotograma por segundo y limita los vídeos largos
a 2.400 fotogramas. El motor OCR local recibe lotes de hasta 32 fotogramas recortados y esas
imágenes siempre se eliminan lote a lote después del reconocimiento. Con
`--resume`, un lote reconocido correctamente se guarda como punto de control
antes de eliminarlo; un lote que contenga errores de fotograma no se guarda y se
vuelve a intentar en la siguiente ejecución. Los puntos de control guardan
marcas de tiempo, hashes de contenido y cuadros de texto sin procesar, nunca
imágenes de fotogramas ni rutas temporales.

## Modelos de Whisper

Whisper solo se ejecuta cuando los subtítulos de la plataforma y los subtítulos
incrustados no se pueden usar, o cuando se selecciona el modo `whisper` /
`hybrid`. El modelo se descarga la primera vez que se usa y después se
reutiliza desde una caché local de modelos. En macOS también se reutilizan los
pesos MLX compatibles de la caché estándar de Hugging Face. Windows usa
faster-whisper con pesos CTranslate2 en una caché independiente.

Las siguientes estimaciones de descarga y memoria se aplican solo al motor MLX
de macOS; los tamaños de modelo y el uso de memoria pueden variar en Windows:

| Modelo | Descarga aproximada | Memoria aproximada durante la ejecución |
| --- | ---: | ---: |
| `base` | 144 MB | 1 GiB |
| `small` | 481 MB | 2 GiB |
| `large-v3-turbo` | 1.61 GB | 6 GiB |

La selección automática usa `small` en Macs con menos memoria y
`large-v3-turbo` en Macs con al menos 16 GiB. Windows usa `small` de forma
predeterminada. Puedes sobrescribir la selección con `--whisper-model`.
El diagnóstico de Windows comprueba los motores, pero todavía no inspecciona
la caché de modelos CTranslate2.

## Desarrollo y arquitectura

Ejecuta el flujo de desarrollo completo:

```bash
./scripts/dev.sh
```

Pasa argumentos de pytest mediante el script cuando sea necesario:

```bash
./scripts/dev.sh tests/test_acquisition.py -q
```

En Windows, ejecuta el script de desarrollo desde PowerShell:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\dev.ps1
```

Módulos principales:

- `sources/`: metadatos y subtítulos de YouTube/Bilibili.
- `planning.py`: plan previo a la ejecución de solo lectura, rutas de procesamiento y requisitos de descarga.
- `media.py`: descargas multimedia reanudables y muestreo acotado de fotogramas.
- `resume.py`: reutilización de transcripciones, bloqueos de tareas, puntos de control de OCR y limpieza de tareas.
- `ocr/`: Apple Vision o RapidOCR y ensamblaje de subtítulos incrustados.
- `asr/`: MLX Whisper o faster-whisper y selección local del modelo.
- `acquisition.py`: política de enrutamiento normativa entre subtítulos/OCR/Whisper.
- `runtime.py`: detección de recursos del equipo y programación ordenada de varias URLs.
- `export.py`: escrituras atómicas agrupadas para salidas Markdown, TXT y JSON.
- `cli.py`: interfaz de terminal.

## Limitaciones actuales

- macOS con Apple Silicon y Windows x64 superan la suite automatizada de CI.
  Aún falta validar un vídeo real de extremo a extremo en un equipo físico con Windows.
- `cache clear-incomplete` aún no está implementado en Windows.
- El acceso a las plataformas depende de yt-dlp y puede verse afectado por la
  región, la cuenta, las cookies o los cambios de la plataforma.
- El OCR de Apple Vision está optimizado para subtítulos cerca de la parte
  inferior del fotograma. Otros diseños o textos muy estilizados pueden requerir
  el modo Whisper.
- Los metadatos y las rutas de medios temporales de Bilibili se han probado en
  directo; la compatibilidad con subtítulos todavía necesita pruebas más amplias
  en vídeos públicos.
- Whisper no puede reanudarse desde una posición intermedia de inferencia.

## Referencia y avisos

El diseño se inspiró en [MediaBrief](https://github.com/EvilIrving/mediabrief).
Se puede conservar una copia local de referencia en `reference/mediabrief/`; el
directorio `reference/` completo se ignora deliberadamente en Git y no forma
parte de este repositorio. Consulta
[`THIRD_PARTY_NOTICES.md`](../THIRD_PARTY_NOTICES.md) para ver los avisos sobre
software de terceros.
