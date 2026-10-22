# YouTubeText

[English](../README.md) | [繁體中文](README.zh-Hant.md) | [简体中文](README.zh-Hans.md) | Español | [日本語](README.ja.md)

YouTubeText es una herramienta de terminal para macOS con Apple Silicon y Windows x64 que
convierte vídeos de YouTube y Bilibili y archivos locales de audio/vídeo en transcripciones limpias con marcas de
tiempo. Exporta un archivo Markdown con marcas de tiempo, un archivo Markdown
limpio sin ellas, un archivo de texto, subtítulos SRT/WebVTT y segmentos de
transcripción y metadatos estructurados.

YouTubeText extrae transcripciones. No resume vídeos, no analiza
argumentos ni requiere un LLM, Ollama o una API de IA en la nube.

## Funciones

- Envía URLs, rutas locales y entradas `file://` en la misma cola.
- Prueba hasta cinco pistas anunciadas y lee archivos SRT/WebVTT adyacentes como `local-captions`.
- Elige densidad y región de subtítulos con `--ocr-profile` y `--ocr-region`.
- Detecta fuentes de YouTube y Bilibili y lee sus metadatos.
- Da prioridad a los subtítulos manuales o automáticos de la plataforma cuando están disponibles.
- Lee subtítulos incrustados con Apple Vision en macOS o RapidOCR en Windows cuando no hay una pista de la plataforma utilizable.
- Recurre a MLX Whisper en macOS o faster-whisper en Windows cuando no se puede usar OCR.
- Exporta transcripciones Markdown con marcas de tiempo y transcripciones limpias sin ellas.
- Exporta subtítulos SRT y WebVTT y todos los segmentos de la transcripción en JSON.
- Elige automáticamente la concurrencia de entradas según la memoria física y, en Windows, la capacidad de la CPU.
- Reutiliza transcripciones terminadas, reanuda descargas multimedia
  interrumpidas y reutiliza lotes de OCR y fragmentos Whisper completados.
- Inspecciona una entrada con `--plan` antes de descargar cualquier archivo de subtítulos o contenido multimedia.
- Aísla los fallos: una entrada problemática no cancela el resto de la cola.

## Flujo de procesamiento

```text
Cola INPUT (URLs, rutas locales, file://)
  └─ Leer metadatos de la fuente
       ├─ Subtítulo de plataforma / archivo local utilizable → Limpiar → Exportar
       └─ Sin subtítulos utilizables
            ├─ Acierto de caché --resume → Exportar
            ├─ Audio local → Whisper → Exportar
            └─ Vídeo remoto/local → OCR en flujo
                                     ├─ utilizable → Exportar
                                     └─ no utilizable → Whisper → Exportar
```

Si falla la descarga o lectura de una pista, se prueban hasta cinco candidatos
anunciados por idioma/tipo en el orden de preferencia existente. Si todos fallan,
advertencias de longitud limitada explican la alternativa, sin otra API en la nube.

Las rutas de subtítulos y OCR utilizable incluyen las comprobaciones condicionales
de huecos descritas a continuación antes de exportar.

Los subtítulos de la plataforma sin huecos temporales sospechosos no requieren
descargar medios. Sus archivos son entradas temporales pequeñas. En modo `auto`,
los huecos sospechosos pueden provocar una descarga solo de audio para la
comprobación y Whisper; ese audio se guarda en un directorio temporal desechable.
Las ejecuciones normales guardan
los medios para OCR y Whisper en un directorio temporal supervisado y exclusivo
de cada ejecución, y lo eliminan al finalizar. Con `--resume`, los medios se
guardan en el directorio `tasks/` de la caché del usuario, se conservan después
de una interrupción y se eliminan tras una finalización correcta, salvo si sigue
pendiente la verificación de huecos de OCR.

## Comprobaciones de integridad

Antes de exportar, YouTubeText busca huecos largos antes, entre y después de los
segmentos de la transcripción. El umbral es el 10% de la duración del vídeo, con
un mínimo de 60 segundos y un máximo de 300. Esta regla no se aplica a vídeos de
menos de 90 segundos o de duración desconocida.

En modo `auto`, los subtítulos de plataforma, archivos locales o resultados OCR utilizables con
huecos sospechosos pasan por una comprobación de silencio con FFmpeg. OCR reutiliza el vídeo de análisis; los subtítulos remotos solo descargan audio
para la comprobación y los locales leen el medio original en su ubicación. Si el hueco es silencioso, se conserva el texto
original. Si contiene audio no silencioso o no se puede verificar, se ejecuta
Whisper y solo se combinan sus segmentos que se solapan con los huecos
sospechosos, conservando el texto original. Si Whisper
falla, se conserva la transcripción original con una advertencia explícita de
incertidumbre. Los modos forzados `captions` y `ocr` mantienen el método elegido
y avisan de los huecos largos sin iniciar reconocimiento de voz adicional.

Ambos archivos JSON y el resultado de `--json` incluyen un informe `completeness` con `status`,
`coverage_ratio`, `duration_seconds`, `covered_seconds` y `long_gaps` (cada uno
con `start_seconds` y `end_seconds`). Estados como `needs-review` o `unverified`
registran la evaluación; ningún estado garantiza que la transcripción esté completa.
Todas las advertencias también se muestran en el terminal.

La validación en línea usó un [vídeo de TED](https://www.youtube.com/watch?v=iG9CE55wbtY)
(1.203 segundos, 427 segmentos de subtítulos manuales en inglés) y exportó los
siete archivos. Al volver a leer SRT y WebVTT, el texto coincidió con
`segments.json`, con un error temporal máximo de 0,51 milisegundos. Los
[datos de prueba solo temporales](../tests/fixtures/ted_caption_timing.json) no
conservan texto de subtítulos y cubren cuatro regresiones sin conexión: tiempos
originales y omisiones iniciales, internas y finales introducidas artificialmente.
Esto valida la exportación de subtítulos de plataforma; siguen pendientes las
evaluaciones amplias de CER/WER de OCR/Whisper con vídeos reales.

Las pruebas con medios generados también ejecutaron MLX Whisper `base` en un
clip inglés de 17 palabras (cero ediciones de palabras) y Apple Vision nativo con
`accurate` en un vídeo de 6 segundos (los tres subtítulos de 2 segundos reconocidos).
Estas pequeñas muestras generadas no establecen la precisión general con vídeos reales.

Una prueba real con audio generado de 307,5 segundos se interrumpió tras el primer
fragmento de 300 segundos y reanudó solo el restante; los fragmentos temporales se eliminaron.

## Requisitos

- Mac con Apple Silicon (`arm64`) y macOS 13 o posterior, o Windows x86 de 64 bits
- Python 3.11–3.14
- FFmpeg y FFprobe disponibles en `PATH`
- Solo para compilar fuentes en Mac: Xcode Command Line Tools para el auxiliar de OCR de Apple Vision
- Se recomienda `uv`

En macOS, instala FFmpeg con Homebrew si no está disponible:

```bash
brew install ffmpeg
```

Para compilar fuentes en Mac, instala las herramientas de línea de comandos de Apple si faltan:

```bash
xcode-select --install
```

## Instalación

La instalación desde código fuente sigue siendo compatible en ambas plataformas.
El wheel macOS ARM64 de 0.2.0 se ha construido y probado en un entorno nuevo fuera
del repositorio; incluye el auxiliar Apple Vision y no necesita Swift para ejecutarse.
Windows CI está configurado para crear un wheel portátil; ese nuevo wheel aún no
se ha verificado en Windows. No se ha publicado en PyPI ni como GitHub Release.

Si dispones del wheel macOS construido, instálalo con:

```bash
python -m pip install ./youtubetext-0.2.0-py3-none-macosx_13_0_arm64.whl
```

Para un wheel Windows construido, usa PowerShell:

```powershell
python -m pip install .\youtubetext-0.2.0-py3-none-any.whl
```

FFmpeg/FFprobe y Python 3.11–3.14 siguen siendo requisitos separados. Los modelos
de voz se descargan al usarlos por primera vez. Instalar desde fuentes en Mac,
incluido `youtubetext-0.2.0.tar.gz`, requiere Xcode Command Line Tools.
A continuación se muestra la instalación desde el repositorio.

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
./.venv/bin/youtubetext INPUT \
  --output ~/Desktop/YouTubeText-output \
  --language zh-Hant
```

Devuelve resultados legibles por máquinas:

```bash
./.venv/bin/youtubetext INPUT --json
```

Muestra todas las opciones:

```bash
./.venv/bin/youtubetext --help
```

En Windows, usa el ejecutable de `.venv\Scripts` desde PowerShell; por ejemplo:

```powershell
.\.venv\Scripts\youtubetext.exe doctor
.\.venv\Scripts\youtubetext.exe "https://www.youtube.com/watch?v=VIDEO_ID"
.\.venv\Scripts\youtubetext.exe INPUT_1 INPUT_2 --jobs 2
.\.venv\Scripts\youtubetext.exe INPUT --plan
.\.venv\Scripts\youtubetext.exe INPUT --resume
.\.venv\Scripts\youtubetext.exe cache
```

Los demás ejemplos de comandos usan la ruta del ejecutable de macOS. En
Windows, sustituye `./.venv/bin/youtubetext` por `.\.venv\Scripts\youtubetext.exe`.

## Entradas locales

La CLI acepta `INPUT [INPUT...]`: URLs remotas, rutas locales de audio/vídeo o URLs
`file://` de este equipo. Las rutas con espacios deben ir entre comillas.

```bash
./.venv/bin/youtubetext "/path/to/video.mp4" "/path/to/audio.wav" --resume
./.venv/bin/youtubetext "file:///path/to/video.mp4" --plan
```

Los originales se leen en su ubicación, sin copiarlos al almacenamiento de tareas
ni borrarlos. Una identidad SHA-256 invalida la caché cuando cambian sus bytes.
Un archivo adyacente con el mismo nombre base, como `video.vtt` o `video.srt`, se
usa como `local-captions` en `auto` o `captions`; se prefiere WebVTT si existen ambos.
Sin subtítulos utilizables, `auto` envía audio a Whisper y aplica al vídeo la ruta
habitual de OCR y después Whisper. `ocr` e `hybrid` requieren una pista de vídeo.
El `--plan` local usa FFprobe y lee un hash de contenido sin acceder a la red ni
descargar medios o modelos.

## Plan previo a la ejecución

Usa `--plan` para inspeccionar qué haría una ejecución nueva antes de descargar
cualquier archivo de subtítulos o contenido multimedia:

```bash
./.venv/bin/youtubetext INPUT --plan
./.venv/bin/youtubetext INPUT_1 INPUT_2 --plan --json
```

Para las URLs remotas, el plan consulta metadatos por red; los archivos locales
se inspeccionan en su ubicación. Informa de:

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
`--plan` y `--resume` no se pueden usar juntos. Se inspeccionan varias entradas de
forma simultánea, los resultados conservan el orden de entrada y un fallo de
metadatos no oculta los demás planes.

## Reanudación y caché

Activa explícitamente el estado local reutilizable:

```bash
./.venv/bin/youtubetext INPUT --resume
```

Para URLs remotas se comprueban primero los subtítulos actuales de la plataforma;
los subtítulos locales se vuelven a leer. Si no se encuentra en caché, yt-dlp puede continuar una descarga
`.part` interrumpida; un archivo multimedia completo se reutiliza directamente;
y el motor OCR local guarda las observaciones sin procesar después de cada lote de
hasta 32 fotogramas reconocido correctamente. Un lote que contenga errores de
fotograma no se guarda como punto de control y se vuelve a intentar en la
siguiente ejecución. Las imágenes muestreadas se eliminan lote a lote después
del reconocimiento; cuando se escribe un punto de control, se guarda antes de
eliminarlas.

Con `--resume`, Whisper guarda cada fragmento terminado de hasta 300 segundos,
incluido un solapamiento de 2 segundos con el siguiente. Solo existe un fragmento
de audio temporal a la vez. Se reutilizan los completados si coinciden el contenido
del medio, modelo, idioma, versión del motor y configuración de fragmentos. Se
repite el fragmento interrumpido; la deduplicación conservadora del solapamiento
preserva textos distintos y repeticiones en momentos distintos. Los archivos
cortos pueden procesarse directamente; si no se puede determinar su duración,
Whisper se ejecuta sin puntos de control por fragmento.

Si falla la verificación adicional de huecos de OCR con Whisper, se exporta la
transcripción original, pero con `--resume` se conservan sus medios y puntos de
control de OCR como tarea incompleta, sin guardar una transcripción terminada en
caché. La siguiente ejecución con `--resume` reintenta la verificación y reutiliza
los medios descargados correctamente y los lotes OCR terminados. La verificación
de subtítulos siempre usa un directorio temporal desechable y comprueba de nuevo
los subtítulos de la plataforma, incluso con `--resume`.

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
control de OCR/Whisper, conservando todas las transcripciones terminadas:

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
./.venv/bin/youtubetext INPUT --cookies-from-browser safari
```

O proporciona un archivo de cookies en formato Netscape:

```bash
./.venv/bin/youtubetext INPUT --cookies-file /path/to/cookies.txt
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
| `auto` | Prioriza subtítulos de plataforma o locales, después OCR para vídeo y Whisper para audio o como alternativa. |
| `captions` | Exige una pista de plataforma o un archivo SRT/VTT local utilizable. |
| `ocr` | Ignora los subtítulos de la plataforma y lee únicamente los subtítulos incrustados. |
| `whisper` | Omite los subtítulos y OCR; ejecuta directamente el reconocimiento de voz local. |
| `hybrid` | Ejecuta OCR y Whisper; los subtítulos visibles tienen prioridad cuando sus intervalos de tiempo se solapan. |

Por ejemplo, fuerza OCR:

```bash
./.venv/bin/youtubetext INPUT --mode ocr --language zh-Hant
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
./.venv/bin/youtubetext INPUT \
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
    transcript.srt
    transcript.vtt
    segments.json
    metadata.json
```

- `transcript.md`: transcripción Markdown completa con marcas de tiempo por segmento.
- `transcript-clean.md`: transcripción Markdown completa sin marcadores de línea temporal.
- `transcript.txt`: texto sin formato con marcas de tiempo.
- `transcript.srt`: segmentos de subtítulos numerados para reproductores y editores compatibles con SRT.
- `transcript.vtt`: segmentos de subtítulos WebVTT para reproductores web y otras herramientas compatibles.
- `segments.json`: texto completo de la transcripción con los tiempos de inicio y fin de cada segmento.
- `metadata.json`: fuente, idioma, método de extracción, número de segmentos, advertencias y evaluación de integridad.

`segments.json` usa `schema_version: 1` e incluye metadatos de la fuente, idioma,
método de extracción, advertencias, evaluación de integridad y una lista ordenada `segments`. Cada segmento
contiene `start_seconds`, `end_seconds`, `text` y `confidence`; los tiempos
conservan su precisión original en segundos. SRT y WebVTT usan marcas de tiempo
en milisegundos. `--json` informa del resultado y las rutas de los archivos de
salida; la transcripción en sí está en `segments.json`.

## Controles de OCR

`--ocr-profile` fija el intervalo de muestreo: `fast` usa 1 segundo, `balanced`
(predeterminado) 0,5 segundos y `accurate` 0,25 segundos. Los vídeos de dos o tres
horas mantienen esa densidad, sin un límite global de 2.400 fotogramas. Se procesan
lotes de hasta 32 fotogramas y se borran después del reconocimiento. Las imágenes
recortadas exactamente iguales reutilizan resultados OCR sin perder posiciones temporales.

`--ocr-region X Y WIDTH HEIGHT` usa coordenadas normalizadas desde la esquina
superior izquierda. El valor predeterminado `0 0.55 1 0.45` cubre el 45% inferior.
El ancho y alto deben ser positivos y la región debe quedar dentro del fotograma.

```bash
./.venv/bin/youtubetext INPUT --ocr-profile accurate --ocr-region 0 0.55 1 0.45
```

El muestreo tiene densidad fija y reutiliza imágenes idénticas. Puede omitir
subtítulos más breves que el intervalo elegido; mayor densidad implica más trabajo.

## Concurrencia y control de recursos

`--jobs 0` es el valor predeterminado y determina un límite de concurrencia de
entradas a partir de la memoria física:

| Memoria física | Tareas de entrada automáticas |
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

El motor OCR recibe en flujo lotes de hasta 32 fotogramas recortados. Con
`--resume`, los lotes reconocidos correctamente se guardan antes de borrar las
imágenes; los lotes con errores no se guardan y se reintentan en la próxima
ejecución. Los puntos de control incluyen tiempos, hashes y cajas de texto sin
procesar, pero nunca imágenes ni rutas temporales.

## Modelos de Whisper

Whisper se ejecuta cuando los subtítulos de la plataforma y los subtítulos
incrustados no se pueden usar, cuando `auto` necesita complementar huecos
sospechosos o cuando se selecciona el modo `whisper` /
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

- `sources/`: metadatos y alternativas acotadas de pistas de YouTube/Bilibili.
- `inputs.py`: metadatos locales, identidad de contenido y subtítulos adyacentes.
- `planning.py`: plan previo a la ejecución de solo lectura, rutas de procesamiento y requisitos de descarga.
- `media.py`: descargas multimedia reanudables y muestreo acotado de fotogramas.
- `resume.py`: reutilización de transcripciones, bloqueos, puntos de control OCR/Whisper y limpieza.
- `ocr/`: Apple Vision o RapidOCR y ensamblaje de subtítulos incrustados.
- `asr/`: MLX Whisper o faster-whisper, selección de modelos y fragmentos reanudables.
- `acquisition.py`: política de enrutamiento normativa entre subtítulos/OCR/Whisper.
- `runtime.py`: detección de recursos del equipo y programación ordenada de varias entradas.
- `export.py`: escrituras atómicas agrupadas para salidas Markdown, TXT, SRT, WebVTT y JSON.
- `cli.py`: interfaz de terminal.

## Limitaciones actuales

- macOS con Apple Silicon y Windows x64 superan la suite automatizada de CI.
  En Windows CI también se prueban modelos OCR y Whisper locales reales y el
  flujo sin conexión desde la CLI hasta la exportación. Aún falta validar un
  vídeo real de extremo a extremo en un equipo físico con Windows; las URL
  públicas pueden rechazar los ejecutores alojados de CI.
- `cache clear-incomplete` sigue sin estar disponible en Windows: el borrado
  seguro ante junctions y reparse points requiere más validación.
- El acceso a las plataformas depende de yt-dlp y puede verse afectado por la
  región, la cuenta, las cookies o los cambios de la plataforma.
- El recorte OCR predeterminado cubre el 45% inferior. Otras posiciones se ajustan
  con `--ocr-region`; el texto muy estilizado puede requerir el modo Whisper.
- La transcripción completa de Bilibili se ha probado en directo en macOS; la
  compatibilidad con subtítulos todavía necesita pruebas más amplias en vídeos
  públicos.
- Whisper se reanuda desde límites de fragmentos terminados; el fragmento interrumpido se repite.
- OCR usa intervalos fijos y puede omitir subtítulos de menos de 0,25 segundos incluso en modo `accurate`.
- Las comprobaciones de huecos y silencio no prueban la precisión de la
  transcripción ni que falte voz. Las pausas largas y la música pueden requerir
  revisión; estas comprobaciones aún no tienen una evaluación de precisión con vídeos reales.

## Referencia y avisos

El diseño se inspiró en [MediaBrief](https://github.com/EvilIrving/mediabrief).
Se puede conservar una copia local de referencia en `reference/mediabrief/`; el
directorio `reference/` completo se ignora deliberadamente en Git y no forma
parte de este repositorio. Consulta
[`THIRD_PARTY_NOTICES.md`](../THIRD_PARTY_NOTICES.md) para ver los avisos sobre
software de terceros.

Consulta [CHANGELOG.md](../CHANGELOG.md) para los cambios de 0.2.0.

YouTubeText se distribuye con [licencia MIT](../LICENSE).
