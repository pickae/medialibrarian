# Settings

Every setting is an environment variable. A command finds it in the
environment it was started in, or in the settings file
`~/.config/medialib/settings` (`$XDG_CONFIG_HOME/medialib/settings` where
that is set) - one `name=value` per line, the way a shell profile writes
it, `export` and quotes allowed. The environment wins, so one run can
override the file without editing it.

A file that holds a secret (marked below) must be readable by you alone -
`chmod 600` - or every command refuses to start. A name in the file that
no command reads is warned about.

This page is written from `medialib/lib/settings.py`; the tests fail if
the two disagree.

| Setting | What it does | Default | Read by |
| --- | --- | --- | --- |
| `audiobookLosslessCompression` | The FLAC compression level of that copy | 5 | `read-library` |
| `audiobookLosslessFilters` | The ffmpeg filters the lossless copy of an audiobook is made with; empty for none | `dynaudnorm=f=150:g=15,afftdn=nf=-70` | `read-library` |
| `censusRamBase` | The census's scratch, ahead of `ramScratchBase` | `ramScratchBase` | `content-census`, `content-census-bi` |
| `chapterDbSite` | The chapter database's address - there for the test suite | `https://chapterdb.plex.tv` | `ingest-movies` |
| `comicPdfExtensions` | The file suffixes that may be comic PDFs, space-separated | pdf | `content-census` |
| `comicPdfFallbackDpi` | The resolution used where the PDF's own cannot be read | 300 | `content-census`, `convert-comics` |
| `comicPdfJpegQuality` | The JPEG quality comic PDF pages are rendered at | 92 | `content-census`, `convert-comics` |
| `comicPdfMaxDpi` | The highest resolution a comic PDF is rendered at | 600 | `content-census`, `convert-comics` |
| `comicPdfMinCoverage` | How much of a page, in percent, its one image has to cover for it to be a comic page | 80 | `content-census`, `convert-comics` |
| `comicPdfMinDpi` | The lowest resolution a comic PDF is rendered at | 150 | `content-census`, `convert-comics` |
| `comicPdfMinPageShare` | How many of a PDF's pages, in percent, have to be comic pages for it to be a comic | 90 | `content-census`, `convert-comics` |
| `comicPdfMinPixels` | How wide and high a page image has to be, in pixels | 400 | `content-census`, `convert-comics` |
| `comicsRamBase` | convert-comics' scratch, ahead of `ramScratchBase` | `ramScratchBase` | `convert-comics` |
| `dvdCompareSite` | The disc database's address - there for the test suite | `http://www.dvdcompare.net/comparisons/` | `ingest-movies` |
| `ffmpegLadder` | The directories searched for ffmpeg after `PATH`, separated as `PATH` is - there for the test suite | `~/.local/bin`, `/opt/homebrew/bin`, `/usr/local/bin`, `/opt/ffmpeg/bin`, `/usr/bin` | `concat-audio`, `content-census`, `convert-and-concat`, `convert-audio`, `convert-video`, `ingest-movies`, `ingest-music`, `read-library`, `transcribe-audio`, `ytdlp` |
| `ffmpegOverride` | The ffmpeg every run uses, by its path; a run refuses to start when it is not executable | unset | `concat-audio`, `content-census`, `convert-and-concat`, `convert-audio`, `convert-video`, `ingest-movies`, `ingest-music`, `read-library`, `transcribe-audio`, `ytdlp` |
| `musicRamBase` | ingest-music's scratch, ahead of `ramScratchBase` | `ramScratchBase` | `ingest-music` |
| `narrationChannel` | The engine's channel layout | mono | `read-library` |
| `narrationEngineLanguages` | Which languages each engine speaks, as `engine:codes` | XTTS's seventeen | `read-library` |
| `narrationFormat` | The format the engine writes an audiobook in | m4b | `read-library` |
| `narrationHome` | The ebook2audiobook checkout read-library drives; `-c` overrides it | `~/ebook2audiobook` | `read-library` |
| `narrationLanguage` | The language a book is read in when its own cannot be found | unset | `read-library` |
| `narrationLosslessTolerance` | How far the engine's lossless copy may be from the book's length and still be found | 0.02 | `read-library` |
| `narrationProgressTail` | How much of the end of the engine's log, in bytes, is read for its progress | 65536 | `read-library` |
| `narrationPythonMaxMinor` | The newest Python 3 minor version the engine's environment is built with | 12 | `read-library` |
| `narrationPythonMinMinor` | The oldest Python 3 minor version the engine's environment is built with | 10 | `read-library` |
| `narrationSoundfileFallbackMark` | The text that marks the engine shim this package wrote | `_load_with_soundfile_fallback` | `read-library` |
| `narrationTorchcodecPairs` | Which torchcodec goes with which torch, for repairing a mismatch | 2.9:0.9 2.10:0.10 2.11:0.11 2.13:0.15 2.14:0.17 | `read-library` |
| `narrationVoiceFallbackNames` | The voice-sample names that mean "any language" | default any fallback other | `read-library` |
| `narrationVramPerBookGB` | The GPU memory, in GB, each book read at once needs - which sets the default `-j` | 5 | `read-library` |
| `openSubtitlesComPassword` (secret) | OpenSubtitles.com password | unset | `ingest-movies` |
| `openSubtitlesComUser` (secret) | OpenSubtitles.com login - a separate service, asked second | unset | `ingest-movies` |
| `openSubtitlesPassword` (secret) | OpenSubtitles.org password | unset | `ingest-movies` |
| `openSubtitlesUser` (secret) | OpenSubtitles.org login; with `openSubtitlesPassword`, subtitles are looked for there first | unset | `ingest-movies` |
| `plexToken` (secret) | That server's X-Plex-Token | unset | `plex-letterboxd-sync` |
| `plexUrl` | The Plex server, e.g. `http://192.168.1.10:32400` | unset | `plex-letterboxd-sync` |
| `ramScratchBase` | Where each run's scratch directories go | `/dev/shm`, else the system temp directory | `concat-audio`, `content-census`, `content-census-bi`, `convert-and-concat`, `convert-audio`, `convert-comics`, `convert-images`, `convert-video`, `ingest-books`, `ingest-movies`, `ingest-music`, `read-library`, `transcribe-audio`, `ytdlp` |
| `ramScratchDiskBase` | Where scratch goes on disk once RAM is nearly full | the cache directory's `ramScratchOverflow` | `concat-audio`, `content-census`, `content-census-bi`, `convert-and-concat`, `convert-audio`, `convert-comics`, `convert-images`, `convert-video`, `ingest-books`, `ingest-movies`, `ingest-music`, `read-library`, `transcribe-audio`, `ytdlp` |
| `ramScratchMaxPercent` | The largest share of the RAM filesystem, in percent, one intermediate file may take | 50 | `concat-audio`, `content-census`, `content-census-bi`, `convert-and-concat`, `convert-audio`, `convert-comics`, `convert-images`, `convert-video`, `ingest-books`, `ingest-movies`, `ingest-music`, `read-library`, `transcribe-audio`, `ytdlp` |
| `ramScratchMinFreeBytes` | With this little free in RAM, new scratch goes to disk instead | 1073741824 | `concat-audio`, `content-census`, `content-census-bi`, `convert-and-concat`, `convert-audio`, `convert-comics`, `convert-images`, `convert-video`, `ingest-books`, `ingest-movies`, `ingest-music`, `read-library`, `transcribe-audio`, `ytdlp` |
| `readLibraryRamBase` | read-library's scratch, ahead of `ramScratchBase` | `ramScratchBase` | `read-library` |
| `subDlApiKey` (secret) | API key of a free SubDL account, asked third for subtitles | unset | `ingest-movies` |
| `tmdbApiKey` (secret) | TheMovieDB API key, for naming films - and tagging their folders with an id, or matching them to Letterboxd's; without it the lookup is skipped | unset | `ingest-movies`, `plex-letterboxd-sync` |
| `upscaleHome` | The VapourSynth environment `convert-video -u` upscales with | `~/vapoursynth` | `convert-video` |
| `upscaleModel` | The upscaling model: a path, or a file name under `<upscaleHome>/models` | `2xLiveActionV1_SPAN_490000.onnx` | `convert-video` |
| `viewerCdnBase` | Where the census page loads its pivot-table engine from; a local copy makes the page fetch nothing | `https://cdn.jsdelivr.net/npm` | `content-census-bi` |
| `viewerPerspectiveVersion` | Which release of that engine the page loads | 5.3.0 | `content-census-bi` |
| `voiceSampleCodec` | The codec a prepared voice sample is written in | pcm_s16le | `read-library` |
| `voiceSampleMaxGap` | The longest pause, in seconds, inside that stretch | 2.5 | `read-library` |
| `voiceSampleMaxSeconds` | A voice sample longer than this, in seconds, is cut down to a stretch of speech | 60 | `read-library` |
| `voiceSampleMinSeconds` | The shortest stretch still taken, in seconds | 5 | `read-library` |
| `voiceSampleRate` | The sample rate a prepared voice sample is written at | 24000 | `read-library` |
| `voiceSampleSearchSeconds` | How much of a long sample's middle, in seconds, is searched for speech | 180 | `read-library` |
| `voiceSampleSeconds` | How long the stretch of speech taken is, in seconds | 45 | `read-library` |
| `voiceSilenceMinDur` | The shortest silence, in seconds, that counts | 0.3 | `read-library` |
| `voiceSilenceNoise` | What counts as silence when looking for it | -30dB | `read-library` |
