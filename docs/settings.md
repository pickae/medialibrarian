# Settings

Every setting is an environment variable. A command finds it in the
environment it was started in, or in the settings file
`~/.config/medialib/settings` (`$XDG_CONFIG_HOME/medialib/settings` where
that is set) - one `name=value` per line, the way a shell profile writes
it, `export` and quotes allowed. The environment wins, so one run can
override the file without editing it. Beneath both is the file that ships
with the package, `medialib/config/settings.defaults`, whose values the
Default column prints.

Every command that takes options can override a setting for one run
with `-x name=value`, repeatable: the value lands in the run's
environment and wins over the file. A secret is not taken there - the
file is where it goes.

A file that holds a secret (marked below) must be readable by you alone -
`chmod 600` - or every command refuses to start. A name in the file that
no command reads is warned about.

| Setting | What it does | Default | Read by |
| --- | --- | --- | --- |
| `audiobookLosslessCompression` | The FLAC compression level of that copy | 5 | `read-library` |
| `audiobookLosslessFilters` | The ffmpeg filters the lossless copy of an audiobook is made with; empty for none | `dynaudnorm=f=150:g=15,afftdn=nf=-70` | `read-library` |
| `beetsConfig` | The beets config ingest-music's tagging phase runs with | the shipped `medialib/config/beets.yaml` | `ingest-music` |
| `bookLanguageLlm` | Whether read-library asks a small language model when a book's metadata language and its text disagree or neither gives a confident answer (the `book-language` task, see docs/models.md): a ~2 KB excerpt and the book's title/metadata are sent to the model: `on` or `off` | on | `read-library` |
| `censusRamBase` | The census's scratch, ahead of `ramScratchBase` | `ramScratchBase` | `content-census`, `content-census-bi` |
| `comicPdfExtensions` | The file suffixes that may be comic PDFs, space-separated | pdf | `content-census` |
| `comicPdfFallbackDpi` | The resolution used where the PDF's own cannot be read | 300 | `content-census`, `convert-comics` |
| `comicPdfJpegQuality` | The JPEG quality comic PDF pages are rendered at | 92 | `content-census`, `convert-comics` |
| `comicPdfMaxDpi` | The highest resolution a comic PDF is rendered at | 600 | `content-census`, `convert-comics` |
| `comicPdfMinCoverage` | How much of a page, in percent, its one image has to cover for it to be a comic page | 80 | `content-census`, `convert-comics` |
| `comicPdfMinDpi` | The lowest resolution a comic PDF is rendered at | 150 | `content-census`, `convert-comics` |
| `comicPdfMinPageShare` | How many of a PDF's pages, in percent, have to be comic pages for it to be a comic | 90 | `content-census`, `convert-comics` |
| `comicPdfMinPixels` | How wide and high a page image has to be, in pixels | 400 | `content-census`, `convert-comics` |
| `comicsRamBase` | convert-comics' scratch, ahead of `ramScratchBase` | `ramScratchBase` | `convert-comics` |
| `contentsSearchLlm` | Whether a book whose chapters its file names only number is handed to a language model to search the web for its table of contents (the `contents-search` task, see docs/models.md); its titles are taken only when there is one for every chapter. Only a model that can search is asked - Claude, or one behind `llmHarness`: `on` or `off` | on | `concat-audio`, `convert-and-concat` |
| `coverName` | The name clean-folder-structure gives a folder's cover art: `folder` (the default) or `cover`, or empty for no rename at all - the art then keeps the name it came in under | folder | `clean-folder-structure` |
| `coverSearch` | Whether a cover nothing local supplied is looked for online - for a book always, for a converted track only when it is a long or dated one among tracks that carry covers: `on` or `off` | on | `concat-audio`, `convert-and-concat`, `convert-audio` |
| `coverSearchLlm` | Whether a book that search found no cover for is handed to a language model to search the web for one (the `cover-search` task, see docs/models.md): it is told the folder's file names, the tracks' tags and what the book probably is. Only a model that can search is asked - Claude, or one behind `llmHarness`: `on` or `off` | on | `concat-audio`, `convert-and-concat` |
| `coverSearchTable` | The table of where that search looks, in what order, by its path | the shipped `medialib/config/coverproviders.tsv` | `concat-audio`, `convert-and-concat`, `convert-audio` |
| `dateForm` | The form clean-folder-structure's -d writes a leading date in: `compact` (the default, `YYYYMMDD`) or `iso` (`YYYY-MM-DD`); -y sorts files that lead with a date in either form into their year | compact | `clean-folder-structure` |
| `ffmpegOverride` | The ffmpeg every run uses, by its path; a run refuses to start when it is not executable | unset | `concat-audio`, `content-census`, `convert-and-concat`, `convert-audio`, `convert-video`, `ingest-movies`, `ingest-music`, `read-library`, `transcribe-audio`, `ytdlp` |
| `findGapsLlm` | Whether find-gaps asks a language model with web search to check numbered, episodic and dated runs against real episode lists and release schedules (the `gaps-check` task, see docs/models.md): it is told folder, series and episode names, number and date ranges, and the deterministic gaps already found, and only appends notes and suspected missing items to the report. Only a model that can search is asked - Claude, or one behind `llmHarness`: `on` or `off` | on | `find-gaps` |
| `fragmentCandidatesLlm` | Whether the candidates find-fragment-candidates finds are handed to a language model to sort into release groups, quality or source tags, publishers and real title words and write proposed additions as a unified diff under `logs/` (the `fragment-classify` task, see docs/models.md): candidate tokens and sample folder/file names from the library are sent to the model; `data/fragments.txt` is never edited directly: `on` or `off` | on | `find-fragment-candidates` |
| `imageGpuEncoder` | The GPU AVIF encoder (av1gpu) tried before ImageMagick, by name or path; empty to encode every image on the CPU | `av1gpu` | `convert-comics`, `convert-images` |
| `infoLogLevel` | How much of what a run says is kept in `logs/info/`, one file per run: `off`, `quiet` (errors and results), `normal` or `verbose` - apart from the console's `-q` and `-v` | normal | every command |
| `keepLanguages` | The languages ingest-movies' improved copy keeps beside the film's own language: a comma list of ISO 639-1 codes; the film's own language is kept whatever it says | en | `ingest-movies` |
| `letterboxdMatchLlm` | Whether unmatched or ambiguous Letterboxd entries are handed to a language model to suggest a Plex film or TMDb id in the sync report only (the `letterboxd-match` task, see docs/models.md): Letterboxd entry titles and years and candidate Plex film titles, years and ids are sent to the model, and nothing in Plex is changed from its answer: `on` or `off` | on | `plex-letterboxd-sync` |
| `llmApi` | The protocol the model is asked over: `openai` for any server speaking OpenAI's chat protocol - Ollama, llama.cpp, vLLM, LM Studio and most hosted APIs - or `anthropic` for Claude | `openai` | `concat-audio`, `convert-and-concat`, `convert-audio`, `convert-video`, `find-fragment-candidates`, `find-gaps`, `generate-chapters`, `ingest-movies`, `ingest-music`, `plex-letterboxd-sync`, `read-library`, `ytdlp` |
| `llmContext` | How many tokens the model's context window holds, as the server runs it: a transcript longer than fits is summarised in parts before it is chaptered. A server that lists its window (`max_model_len`) is held to the smaller of the two | the window the server lists, else 32000 | `concat-audio`, `convert-and-concat`, `convert-audio`, `convert-video`, `find-fragment-candidates`, `find-gaps`, `generate-chapters`, `ingest-movies`, `ingest-music`, `plex-letterboxd-sync`, `read-library`, `ytdlp` |
| `llmHarness` | The agent harnesses you have installed, a comma list, the preferred first - only `opencode` is supported. With one, the chapter instructions (`generate-chapters -P` and the like) are asked of the same model through the harness instead of straight to its server, so a local model can search the web as Claude does, e.g. to spell the speakers' names - and concat-audio can ask it to search the web for a book's cover. Each request is one throwaway session that is deleted when its answer is read, so your own sessions stay easy to find. The harness runs with its permissions auto-approved, and its prompt is the transcript (for a cover, the book's file names and tags), so use it only with a model you trust with both | unset - instructions are asked of the server alone | `concat-audio`, `convert-and-concat`, `convert-audio`, `convert-video`, `find-fragment-candidates`, `find-gaps`, `generate-chapters`, `ingest-movies`, `ingest-music`, `plex-letterboxd-sync`, `read-library`, `ytdlp` |
| `llmHarnessModel` | The model the harness asks, as it names models - for opencode `provider/model`, as `opencode models` lists them, e.g. `ninfer/qwen3.8-27b`. Required with `llmHarness`: the harness's own default can be a hosted model, and the transcript would be sent to it | unset | `concat-audio`, `convert-and-concat`, `convert-audio`, `convert-video`, `find-fragment-candidates`, `find-gaps`, `generate-chapters`, `ingest-movies`, `ingest-music`, `plex-letterboxd-sync`, `read-library`, `ytdlp` |
| `llmJobs` | How many requests are sent to the model at once, over the whole run. Whether the model is local is read off `llmUrl`: localhost, a private or Tailscale address, or a name on the LAN | 2 for a local model; no limit for a hosted one | `concat-audio`, `convert-and-concat`, `convert-audio`, `convert-video`, `find-fragment-candidates`, `find-gaps`, `generate-chapters`, `ingest-movies`, `ingest-music`, `plex-letterboxd-sync`, `read-library`, `ytdlp` |
| `llmKey` (secret) | The API key, where the server asks for one; for `anthropic`, `ANTHROPIC_API_KEY` or an `ant auth login` profile does too | unset | `concat-audio`, `convert-and-concat`, `convert-audio`, `convert-video`, `find-fragment-candidates`, `find-gaps`, `generate-chapters`, `ingest-movies`, `ingest-music`, `plex-letterboxd-sync`, `read-library`, `ytdlp` |
| `llmLean` | Which way every task leans in choosing among the models that can do it: `light` (small, cheap and fast first), `balanced` (the roster's order) or `precise` (large and reasoning first); the roster file can say it per task | unset - each task's own | `concat-audio`, `convert-and-concat`, `convert-audio`, `convert-video`, `find-fragment-candidates`, `find-gaps`, `generate-chapters`, `ingest-movies`, `ingest-music`, `plex-letterboxd-sync`, `read-library`, `ytdlp` |
| `llmModel` | The model's name as that server knows it; a server that does not list it is asked for the first model it lists | unset - the first model the server lists, required for a hosted `openai` API; `claude-opus-5-5` for `anthropic` | `concat-audio`, `convert-and-concat`, `convert-audio`, `convert-video`, `find-fragment-candidates`, `find-gaps`, `generate-chapters`, `ingest-movies`, `ingest-music`, `plex-letterboxd-sync`, `read-library`, `ytdlp` |
| `llmModels` | The roster file: every language model you have, and which tasks use which - see docs/models.md. The single-model settings below add one more, called `llm` | `models.toml` beside the settings file, where there is one | `concat-audio`, `convert-and-concat`, `convert-audio`, `convert-video`, `find-fragment-candidates`, `find-gaps`, `generate-chapters`, `ingest-movies`, `ingest-music`, `plex-letterboxd-sync`, `read-library`, `ytdlp` |
| `llmOptions` | More fields for every request over the `openai` protocol, as a JSON object - whatever that server takes, e.g. `{"reasoning_effort": "low"}` or `{"temperature": 0.2}`; an output limit given here replaces the one the run works out, and a request that searches - the chapter instructions (`generate-chapters -P`, `convert-audio -P`, `convert-video -P`, `ingest-movies -P`, `ytdlp -P`), the cover search - sends its own `reasoning_effort` | unset | `concat-audio`, `convert-and-concat`, `convert-audio`, `convert-video`, `find-fragment-candidates`, `find-gaps`, `generate-chapters`, `ingest-movies`, `ingest-music`, `plex-letterboxd-sync`, `read-library`, `ytdlp` |
| `llmServe` | The command that starts the first of those servers when it is not running and the GPU has room for it; the run stops it again once it has been idle five minutes, and at its end. A server found running is left running | unset | `concat-audio`, `convert-and-concat`, `convert-audio`, `convert-video`, `find-fragment-candidates`, `find-gaps`, `generate-chapters`, `ingest-movies`, `ingest-music`, `plex-letterboxd-sync`, `read-library`, `ytdlp` |
| `llmServeMib` | The free GPU memory, in MiB, that server is started with; with less, the next server down the list is asked | 26000 | `concat-audio`, `convert-and-concat`, `convert-audio`, `convert-video`, `find-fragment-candidates`, `find-gaps`, `generate-chapters`, `ingest-movies`, `ingest-music`, `plex-letterboxd-sync`, `read-library`, `ytdlp` |
| `llmTraits` | What the model is, where finding out gets it wrong: a comma list of `tools`, `vision`, `reasoning`, `images_out`, `moe`, `cheap`, each with `-` in front for one it lacks, and `params=`, `active=` (billions), `tok_s=`, `cost_in=`, `cost_out=` (dollars a million tokens), `size=` (`small`, `medium`, `large`) and `speed=` (`fast`, `slow`) - e.g. `vision,-reasoning,params=30,active=3`. What is not said is asked of a local server (llama.cpp's `/props`, Ollama's `/api/show`) or read off the model's name | unset - all found out | `concat-audio`, `convert-and-concat`, `convert-audio`, `convert-video`, `find-fragment-candidates`, `find-gaps`, `generate-chapters`, `ingest-movies`, `ingest-music`, `plex-letterboxd-sync`, `read-library`, `ytdlp` |
| `llmUrl` | That server's address, up to and including the `/v1`, e.g. `http://localhost:11434/v1`; for `openai`, a comma list of servers, the preferred first, e.g. `http://localhost:8081/v1,http://localhost:8080/v1` - each request goes to the first that answers, or can be started; for `anthropic`, only to reach another endpoint than Anthropic's own | unset - required for `openai` | `concat-audio`, `convert-and-concat`, `convert-audio`, `convert-video`, `find-fragment-candidates`, `find-gaps`, `generate-chapters`, `ingest-movies`, `ingest-music`, `plex-letterboxd-sync`, `read-library`, `ytdlp` |
| `llmVramGB` | The GPU memory, in GB, the model needs while whisper is using the same card - ingest-movies starts it beside the commentary transcription only when that much is free, and otherwise waits for its turn; only a model served from this machine or its network is waited for | unset - the model takes the whole card, so it never runs beside whisper | `ingest-movies` |
| `losslessAudioTarget` | The codec ingest-movies' improved copy converts the lossless audio to: `opus` (the default, at a per-channel target bitrate) or `flac`, which takes the channels as they are, up to eight of them | opus | `ingest-movies` |
| `mediaServerNaming` | The server the names are written for: `plex` or `jellyfin`. The id tag is written the way that server reads it - `{imdb-tt…}` and `[imdbid-tt…]` - and an edition as its tag, or its version label after the name; names written either way are read back whatever it says, so a library can be switched | plex | `ingest-movies` |
| `movieJunkExtensions` | The file suffixes ingest-movies deletes from a release as junk, space-separated; empty deletes none - the sample clips, the tiny .mkv files and the .unwanted folders go whatever it says | txt nfo exe doc sfv | `ingest-movies` |
| `musicRamBase` | ingest-music's scratch, ahead of `ramScratchBase` | `ramScratchBase` | `ingest-music` |
| `musicSearchLlm` | Whether an album beets skipped is handed to a language model to look up its MusicBrainz release ID on the web (the `music-search` task, see docs/models.md): it is told the folder and file names, track durations, existing tags and any rip logs or cue sheets in the folder. Only a model that can search is asked - Claude, or one behind `llmHarness`: `on` or `off` | on | `ingest-music` |
| `narrationEngineLanguages` | Which languages each engine speaks, as `engine:codes` | xtts:ara,ces,deu,eng,fra,hin,hun,ita,jpn,kor,nld,pol,por,rus,spa,tur,zho | `read-library` |
| `narrationFormat` | The format the engine writes an audiobook in | m4b | `read-library` |
| `narrationHome` | The ebook2audiobook checkout read-library drives; `-c` overrides it | `~/ebook2audiobook` | `read-library` |
| `narrationLanguage` | The language a book is read in when its own cannot be found | unset | `read-library` |
| `narrationVramPerBookGB` | The GPU memory, in GB, each book read at once needs - which sets the default `-j` | 5 | `read-library` |
| `openSubtitlesComPassword` (secret) | OpenSubtitles.com password | unset | `ingest-movies` |
| `openSubtitlesComUser` (secret) | OpenSubtitles.com login - a separate service, asked second | unset | `ingest-movies` |
| `openSubtitlesPassword` (secret) | OpenSubtitles.org password | unset | `ingest-movies` |
| `openSubtitlesUser` (secret) | OpenSubtitles.org login; with `openSubtitlesPassword`, subtitles are looked for there first | unset | `ingest-movies` |
| `plexToken` (secret) | That server's X-Plex-Token | unset | `ingest-movies`, `plex-letterboxd-sync` |
| `plexUrl` | The Plex server, e.g. `http://192.168.1.10:32400` | unset - ingest-movies then asks `http://localhost:32400` | `ingest-movies`, `plex-letterboxd-sync` |
| `podcastAdsCut` | Whether the ad, sponsor and self-promotion stretches `podcastAdsLlm` finds in an RSS or site podcast episode are cut out of the file instead of only marked as chapters a player can skip: `off` or `on` | off | `ytdlp` |
| `podcastAdsLlm` | Whether a language model looks for host-read ads, sponsor reads and self-promotion in the transcripts of RSS and site podcast episodes (the `podcast-ads` task, see docs/models.md), which have no SponsorBlock: `on` or `off`. Detected stretches are marked as skippable chapters by default; `podcastAdsCut` opts in to cutting them out | on | `ytdlp` |
| `ramScratchBase` | Where each run's scratch directories go | `/dev/shm`, else the system temp directory | `concat-audio`, `content-census`, `content-census-bi`, `convert-and-concat`, `convert-audio`, `convert-comics`, `convert-images`, `convert-video`, `generate-chapters`, `ingest-books`, `ingest-movies`, `ingest-music`, `read-library`, `transcribe-audio`, `ytdlp` |
| `ramScratchDiskBase` | Where scratch goes on disk once RAM is nearly full | the cache directory's `ramScratchOverflow` | `concat-audio`, `content-census`, `content-census-bi`, `convert-and-concat`, `convert-audio`, `convert-comics`, `convert-images`, `convert-video`, `generate-chapters`, `ingest-books`, `ingest-movies`, `ingest-music`, `read-library`, `transcribe-audio`, `ytdlp` |
| `ramScratchMaxPercent` | The largest share of the RAM filesystem, in percent, one intermediate file may take | 50 | `concat-audio`, `content-census`, `content-census-bi`, `convert-and-concat`, `convert-audio`, `convert-comics`, `convert-images`, `convert-video`, `generate-chapters`, `ingest-books`, `ingest-movies`, `ingest-music`, `read-library`, `transcribe-audio`, `ytdlp` |
| `ramScratchMinFreeBytes` | With this little free in RAM, new scratch goes to disk instead | 1073741824 | `concat-audio`, `content-census`, `content-census-bi`, `convert-and-concat`, `convert-audio`, `convert-comics`, `convert-images`, `convert-video`, `generate-chapters`, `ingest-books`, `ingest-movies`, `ingest-music`, `read-library`, `transcribe-audio`, `ytdlp` |
| `readLibraryRamBase` | read-library's scratch, ahead of `ramScratchBase` | `ramScratchBase` | `read-library` |
| `skipYtdlpUpgrade` | Set to 1 and a nightly yt-dlp is not brought up to date before the fetch. `SKIP_YTDLP_UPGRADE` is accepted too | unset - a nightly is upgraded; a preview (-p) never is | `ytdlp` |
| `spokenLanguages` | The languages you speak: ingest-movies fetches and tests subtitles in them, and ytdlp -C takes a YouTube episode's transcript in its own language when it is one of them, in English when it is not. A comma list of ISO 639-1 codes, in the order to fetch them; track names are also read in the names these languages give each other | en,de,fr,nl,es,it | `ingest-movies`, `ytdlp` |
| `subDlApiKey` (secret) | API key of a free SubDL account, asked third for subtitles | unset | `ingest-movies` |
| `subtitleOcrEngines` | The OCR engines ingest-movies reads a film's picture subtitles with: a comma list of `vision`, `paddle` and `tesseract`, tried in the order named until one reads the subtitle | a vision model from the roster's `subtitle-ocr` task where there is one (docs/models.md), paddle on the GPU where it fits, tesseract, then paddle on the CPU | `ingest-movies` |
| `tmdbApiKey` (secret) | TheMovieDB API key, for naming films - and tagging their folders with an id, or matching them to Letterboxd's; without it the lookup is skipped | unset | `ingest-movies`, `plex-letterboxd-sync` |
| `upscaleHome` | The VapourSynth environment `convert-video -u` upscales with | `~/vapoursynth` | `convert-video` |
| `upscaleModel` | The upscaling model: a path, or a file name under `<upscaleHome>/models` | `2xLiveActionV1_SPAN_490000.onnx` | `convert-video` |
| `viewerCdnBase` | Where the census page loads its pivot-table engine from; a local copy makes the page fetch nothing | `https://cdn.jsdelivr.net/npm` | `content-census-bi` |
| `viewerPerspectiveVersion` | Which release of that engine the page loads | 5.3.0 | `content-census-bi` |
| `ytdlp` | The yt-dlp a download runs with, instead of the one found: a command name on PATH or a path. `YTDLP` is accepted too | unset - the one found, printed at the start of the run | `ytdlp` |

## Tuning and testing

These are read too, but the defaults are what the commands are
built and tested with.

| Setting | What it does | Default | Read by |
| --- | --- | --- | --- |
| `chapterDbSite` | The chapter database's address | `https://chapterdb.plex.tv` | `ingest-movies` |
| `dvdCompareSite` | The disc database's address | `http://www.dvdcompare.net/comparisons/` | `ingest-movies` |
| `ffmpegLadder` | The directories searched for ffmpeg after `PATH`, separated as `PATH` is | `~/.local/bin`, `/opt/homebrew/bin`, `/usr/local/bin`, `/opt/ffmpeg/bin`, `/usr/bin` | `concat-audio`, `content-census`, `convert-and-concat`, `convert-audio`, `convert-video`, `ingest-movies`, `ingest-music`, `read-library`, `transcribe-audio`, `ytdlp` |
| `narrationChannel` | The engine's channel layout | mono | `read-library` |
| `narrationLosslessTolerance` | How far the engine's lossless copy may be from the book's length and still be found | 0.02 | `read-library` |
| `narrationProgressTail` | How much of the end of the engine's log, in bytes, is read for its progress | 65536 | `read-library` |
| `narrationPythonMaxMinor` | The newest Python 3 minor version the engine's environment is built with | 12 | `read-library` |
| `narrationPythonMinMinor` | The oldest Python 3 minor version the engine's environment is built with | 10 | `read-library` |
| `narrationSoundfileFallbackMark` | The text that marks the engine shim this package wrote | `_load_with_soundfile_fallback` | `read-library` |
| `narrationTorchcodecPairs` | Which torchcodec goes with which torch, for repairing a mismatch | 2.9:0.9 2.10:0.10 2.11:0.11 2.13:0.15 2.14:0.17 | `read-library` |
| `narrationVoiceFallbackNames` | The voice-sample names that mean "any language" | default any fallback other | `read-library` |
| `voiceSampleCodec` | The codec a prepared voice sample is written in | pcm_s16le | `read-library` |
| `voiceSampleMaxGap` | The longest pause, in seconds, inside that stretch | 2.5 | `read-library` |
| `voiceSampleMaxSeconds` | A voice sample longer than this, in seconds, is cut down to a stretch of speech | 60 | `read-library` |
| `voiceSampleMinSeconds` | The shortest stretch still taken, in seconds | 5 | `read-library` |
| `voiceSampleRate` | The sample rate a prepared voice sample is written at | 24000 | `read-library` |
| `voiceSampleSearchSeconds` | How much of a long sample's middle, in seconds, is searched for speech | 180 | `read-library` |
| `voiceSampleSeconds` | How long the stretch of speech taken is, in seconds | 45 | `read-library` |
| `voiceSilenceMinDur` | The shortest silence, in seconds, that counts | 0.3 | `read-library` |
| `voiceSilenceNoise` | What counts as silence when looking for it | -30dB | `read-library` |
