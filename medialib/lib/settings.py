"""The settings a user gives these commands: what each is called, what it is
for, which commands read it, and where a run finds it.

Every setting is an environment variable, and every command finds it in the
same two places: the environment the command was started in, and a settings
file - ``~/.config/medialib/settings``, or ``$XDG_CONFIG_HOME/medialib/settings``
where that is set. The environment wins, so a value given for one run
overrides the file without editing it. Beneath both sits
``medialib/config/settings.defaults``, shipped with the package, which holds
every default that is a value; code asks :func:`value` and carries none.

The file is one ``name=value`` per line, the way a shell writes an
assignment, so a line copied out of a shell profile works as it is: an
``export`` in front is allowed, a value may be quoted, and a line starting
with ``#`` is a comment. It is read when a command starts, into that
command's environment, which is how a tool or a helper the command starts
gets the value too.

Three things are refused rather than guessed at. A file that holds a secret -
a password, a key, a token - and that another account on the machine can
read stops the run until it is made private, because a settings file is
exactly where those end up. A line that is not an assignment stops the run
too, naming the line. A name nothing here reads is only warned about, with
the setting it most looks like, because a typo is the likeliest reason for it
and a run that refused over it would be refusing over nothing.

:data:`SETTINGS` is the one list of the names. Nothing reads a setting the
list does not hold - the tests read every module and fail on one - and
`docs/settings.md` is written from it, so the page cannot fall behind either.
Variables the commands set for one another, in upper case, are not settings
and are not listed: nobody is meant to set them.
"""

from __future__ import annotations

import difflib
import functools
import os
import shlex
import stat
import sys
from typing import NamedTuple

__all__ = ["Setting", "SETTINGS", "INTERNAL", "SettingsError", "config_home",
           "cache_home", "settings_file", "shipped", "value", "load",
           "read_file", "shell_value", "page"]

# Where a run is told to look instead, for the suite - which must never read
# the settings of the machine it runs on - and for anyone keeping two.
FILE_VARIABLE = "CLI_SETTINGS_FILE"


class Setting(NamedTuple):
    """One setting: its name, what it does, the commands that read it, its
    default as a user would describe it, whether it is a secret, and whether
    it is a tuning knob or test hook rather than something a user sets."""

    name: str
    about: str
    commands: tuple
    default: str = "unset"
    secret: bool = False
    tuning: bool = False


class SettingsError(Exception):
    """The settings file cannot be used as it stands."""


_NARRATION = ("read-library",)
_COMICS = ("content-census", "convert-comics")
# Every command that may pick an ffmpeg build.
_FFMPEG = ("concat-audio", "content-census", "convert-and-concat", "convert-audio",
           "convert-video", "ingest-movies", "ingest-music", "read-library",
           "transcribe-audio", "ytdlp")
# Every command that keeps scratch in RAM.
_SCRATCH = ("concat-audio", "content-census", "content-census-bi", "convert-and-concat",
            "convert-audio", "convert-comics", "convert-images", "convert-video",
            "generate-chapters", "ingest-books", "ingest-movies", "ingest-music", "read-library",
            "transcribe-audio", "ytdlp")

# Every command that asks a language model.
_AI_CHAPTERS = ("concat-audio", "content-census", "convert-and-concat",
                "convert-audio", "convert-comics", "convert-video",
                "find-fragment-candidates", "find-gaps", "generate-chapters",
                "ingest-books", "ingest-movies", "ingest-music",
                "plex-letterboxd-sync", "read-library", "ytdlp")

# Every command in the repository (info logs and error logs).
_ALL_COMMANDS = (
    "clean-folder-structure", "clean-folder-structure-adb", "concat-audio",
    "content-census", "content-census-bi", "convert-and-concat",
    "convert-audio", "convert-comics", "convert-images", "convert-video",
    "cue-to-chapters", "find-fragment-candidates", "find-gaps",
    "generate-chapters", "ingest-books", "ingest-movies", "ingest-music",
    "plex-letterboxd-sync", "read-library", "transcribe-audio", "ytdlp",
)

SETTINGS: tuple[Setting, ...] = (
    # --- logins and keys
    Setting("tmdbApiKey", "TheMovieDB API key, for naming films - and tagging their "
            "folders with an id, or matching them to Letterboxd's; without it the "
            "lookup is skipped", ("ingest-movies", "plex-letterboxd-sync"), secret=True),
    Setting("plexUrl", "The Plex server, e.g. `http://192.168.1.10:32400`",
            ("ingest-movies", "plex-letterboxd-sync"),
            "unset - ingest-movies then asks `http://localhost:32400`"),
    Setting("plexToken", "That server's X-Plex-Token",
            ("ingest-movies", "plex-letterboxd-sync"), secret=True),
    Setting("openSubtitlesUser", "OpenSubtitles.org login; with "
            "`openSubtitlesPassword`, subtitles are looked for there first",
            ("ingest-movies",), secret=True),
    Setting("openSubtitlesPassword", "OpenSubtitles.org password", ("ingest-movies",),
            secret=True),
    Setting("openSubtitlesComUser", "OpenSubtitles.com login - a separate service, "
            "asked second", ("ingest-movies",), secret=True),
    Setting("openSubtitlesComPassword", "OpenSubtitles.com password", ("ingest-movies",),
            secret=True),
    Setting("subDlApiKey", "API key of a free SubDL account, asked third for subtitles",
            ("ingest-movies",), secret=True),
    Setting("spokenLanguages", "The languages you speak: ingest-movies fetches "
            "and tests subtitles in them, and ytdlp -C takes a YouTube "
            "episode's transcript in its own language when it is one of them, "
            "in English when it is not. A comma list of ISO 639-1 codes, in the "
            "order to fetch them; track names are also read in the names these "
            "languages give each other", ("ingest-movies", "ytdlp")),
    Setting("subtitleOcrEngines", "The OCR engines ingest-movies reads a film's "
            "picture subtitles with: a comma list of `vision`, `paddle` and "
            "`tesseract`, tried in the order named until one reads the "
            "subtitle", ("ingest-movies",),
            "a vision model from the roster's `subtitle-ocr` task where there "
            "is one (docs/models.md), paddle on the GPU where it fits, "
            "tesseract, then paddle on the CPU"),
    # --- what a run keeps
    Setting("keepLanguages", "The languages ingest-movies' improved copy keeps "
            "beside the film's own language: a comma list of ISO 639-1 codes; "
            "the film's own language is kept whatever it says",
            ("ingest-movies",)),
    Setting("losslessAudioTarget", "The codec ingest-movies' improved copy "
            "converts the lossless audio to: `opus` (the default, at a "
            "per-channel target bitrate) or `flac`, which takes the channels "
            "as they are, up to eight of them",
            ("ingest-movies",), "opus"),
    Setting("mediaServerNaming", "The server the names are written for: "
            "`plex` or `jellyfin`. The id tag is written the way that server "
            "reads it - `{imdb-tt…}` and `[imdbid-tt…]` - and an edition as its "
            "tag, or its version label after the name; names written either "
            "way are read back whatever it says, so a library can be "
            "switched", ("ingest-movies",), "plex"),
    Setting("movieJunkExtensions", "The file suffixes ingest-movies deletes "
            "from a release as junk, space-separated; empty deletes none - the "
            "sample clips, the tiny .mkv files and the .unwanted folders go "
            "whatever it says", ("ingest-movies",), "txt nfo exe doc sfv"),
    Setting("infoLogLevel", "How much of what a run says is kept in "
            "`logs/info/`, one file per run: `off`, `quiet` (errors and results), "
            "`normal` or `verbose` - apart from the console's `-q` and `-v`",
            _ALL_COMMANDS, "normal"),
    Setting("errorTriageLlm", "Whether unexplained errors left in a run's error "
            "log (those no built-in cause matched) are handed to a language model "
            "at the end of the run for an advisory likely cause and next step "
            "(the `error-triage` task, see docs/models.md): error records hold "
            "library paths, so a local model is preferred. Advisory only - never "
            "retries commands, deletes files or changes exit codes: `on` or `off`",
            _ALL_COMMANDS, "on"),
    # --- where work is done
    Setting("ramScratchBase", "Where each run's scratch directories go",
            _SCRATCH, "`/dev/shm`, else the system temp directory"),
    Setting("ramScratchMaxPercent", "The largest share of the RAM filesystem, in "
            "percent, one intermediate file may take", _SCRATCH),
    Setting("ramScratchMinFreeBytes", "With this little free in RAM, new scratch "
            "goes to disk instead", _SCRATCH),
    Setting("ramScratchDiskBase", "Where scratch goes on disk once RAM is nearly full",
            _SCRATCH, "the cache directory's `ramScratchOverflow`"),
    Setting("censusRamBase", "The census's scratch, ahead of `ramScratchBase`",
            ("content-census", "content-census-bi"), "`ramScratchBase`"),
    Setting("comicsRamBase", "convert-comics' scratch, ahead of `ramScratchBase`",
            ("convert-comics",), "`ramScratchBase`"),
    Setting("musicRamBase", "ingest-music's scratch, ahead of `ramScratchBase`",
            ("ingest-music",), "`ramScratchBase`"),
    Setting("readLibraryRamBase", "read-library's scratch, ahead of `ramScratchBase`",
            _NARRATION, "`ramScratchBase`"),
    # --- tools and sites
    Setting("ffmpegOverride", "The ffmpeg every run uses, by its path; a run refuses "
            "to start when it is not executable", _FFMPEG),
    Setting("ffmpegLadder", "The directories searched for ffmpeg after `PATH`, "
            "separated as `PATH` is", _FFMPEG,
            "`~/.local/bin`, `/opt/homebrew/bin`, `/usr/local/bin`, `/opt/ffmpeg/bin`, "
            "`/usr/bin`", tuning=True),
    Setting("beetsConfig", "The beets config ingest-music's tagging phase runs "
            "with", ("ingest-music",),
            "the shipped `medialib/config/beets.yaml`"),
    Setting("imageGpuEncoder", "The GPU AVIF encoder (av1gpu) tried before "
            "ImageMagick, by name or path; empty to encode every image on the CPU",
            ("convert-comics", "convert-images"), "`av1gpu`"),
    Setting("upscaleHome", "The VapourSynth environment `convert-video -u` upscales "
            "with", ("convert-video",), "`~/vapoursynth`"),
    Setting("upscaleModel", "The upscaling model: a path, or a file name under "
            "`<upscaleHome>/models`", ("convert-video",), "`2xLiveActionV1_SPAN_490000.onnx`"),
    Setting("coverSearch", "Whether a cover nothing local supplied is looked for "
            "online - for a book always, for a converted track only when it is a "
            "long or dated one among tracks that carry covers: `on` or `off`",
            ("concat-audio", "convert-and-concat", "convert-audio"), "on"),
    Setting("coverSearchTable", "The table of where that search looks, in what "
            "order, by its path", ("concat-audio", "convert-and-concat", "convert-audio"),
            "the shipped `medialib/config/coverproviders.tsv`"),
    Setting("contentKindLlm", "Whether a language model classifies a recording "
            "as podcast, audio drama or audiobook before that online cover "
            "search runs (the `audio-content` task, see docs/models.md), instead "
            "of guessing from length and channel count alone: it is told the "
            "folder's path and file names, the tracks' tags, the duration and "
            "the channel layout, and falls back to the length heuristic when "
            "uncertain or unavailable: `on` or `off`",
            ("concat-audio", "convert-and-concat", "convert-audio"), "on"),
    Setting("coverSearchLlm", "Whether a book or converted track that search "
            "found no cover for is handed to a language model to search the web "
            "for one (the `cover-search` task, see docs/models.md): it is told "
            "the file names, tags and what the recording probably is. Only a "
            "model that can search is asked - Claude, or one behind "
            "`llmHarness` - and for a converted track a vision model "
            "(`cover-look`) must also accept the image: `on` or `off`",
            ("concat-audio", "convert-and-concat", "convert-audio"), "on"),
    Setting("coverLookLlm", "Whether a book folder holding two or more image "
            "files that none of the cover-name words (`front`, `cover`, "
            "`inlay`, `folder`, `back`) matched is handed to a vision model "
            "to pick the front cover (the `cover-look` task, see "
            "docs/models.md): those image files and their names are sent to "
            "the model, and when none looks like a cover or no vision model "
            "is set up, the first image is taken: `on` or `off`",
            ("concat-audio", "convert-and-concat"), "on"),
    Setting("contentsSearchLlm", "Whether a book whose chapters its file names "
            "only number is handed to a language model to search the web for "
            "its table of contents (the `contents-search` task, see "
            "docs/models.md); its titles are taken only when there is one for "
            "every chapter. Only a model that can search is asked - Claude, or "
            "one behind `llmHarness`: `on` or `off`",
            ("concat-audio", "convert-and-concat"), "on"),
    Setting("bookTagsLlm", "Whether a joined book's empty metadata tags "
            "(title, author, narrator, series, series index, year, language, "
            "abridged) are looked up by a language model on the web (the "
            "`book-tags` task, see docs/models.md): it is told the folder's "
            "file names, the tracks' tags and what the book probably is. Only "
            "empty tags are filled and the output file is never renamed. Only "
            "a model that can search is asked - Claude, or one behind "
            "`llmHarness`: `on` or `off`",
            ("concat-audio", "convert-and-concat"), "on"),
    Setting("undatedYearLlm", "Whether a film folder with no year in its folder "
            "or file names is handed to a language model to search the web for "
            "its release year (the `film-year` task, see docs/models.md) before "
            "TMDb identification: it is told the undated folder's name, its "
            "file names, and its measured runtime and container write year. "
            "The returned year only feeds TMDb verification and never renames "
            "files on its own. Only a model that can search is asked - Claude, "
            "or one behind `llmHarness`: `on` or `off`",
            ("ingest-movies",), "on"),
    Setting("musicSearchLlm", "Whether an album beets skipped is handed to a "
            "language model to look up its MusicBrainz release ID on the web "
            "(the `music-search` task, see docs/models.md): it is told the "
            "folder and file names, track durations, existing tags and any "
            "rip logs or cue sheets in the folder. Only a model that can "
            "search is asked - Claude, or one behind `llmHarness`: `on` or "
            "`off`",
            ("ingest-music",), "on"),
    Setting("fragmentCandidatesLlm", "Whether the candidates "
            "find-fragment-candidates finds are handed to a language model to "
            "sort into release groups, quality or source tags, publishers and "
            "real title words and write proposed additions as a unified diff "
            "under `logs/` (the `fragment-classify` task, see docs/models.md): "
            "candidate tokens and sample folder/file names from the library "
            "are sent to the model; `data/fragments.txt` is never edited "
            "directly: `on` or `off`",
            ("find-fragment-candidates",), "on"),
    Setting("findGapsLlm", "Whether find-gaps asks a language model with web "
            "search to check numbered, episodic and dated runs against real "
            "episode lists and release schedules (the `gaps-check` task, see "
            "docs/models.md): it is told folder, series and episode names, "
            "number and date ranges, and the deterministic gaps already found, "
            "and only appends notes and suspected missing items to the report. "
            "Only a model that can search is asked - Claude, or one behind "
            "`llmHarness`: `on` or `off`",
            ("find-gaps",), "on"),
    Setting("letterboxdMatchLlm", "Whether unmatched or ambiguous Letterboxd "
            "entries are handed to a language model to suggest a Plex film or "
            "TMDb id in the sync report only (the `letterboxd-match` task, see "
            "docs/models.md): Letterboxd entry titles and years and candidate "
            "Plex film titles, years and ids are sent to the model, and nothing "
            "in Plex is changed from its answer: `on` or `off`",
            ("plex-letterboxd-sync",), "on"),
    Setting("bookJunkImagesLlm", "Whether ingest-books -d asks a vision model "
            "to tell book illustrations from advertising and publisher promos "
            "(the `book-image-check` task, see docs/models.md): candidate "
            "images inside the e-book and their file names are sent to the "
            "model, and only a high-confidence ad verdict is removed - anything "
            "else is kept. Without a vision model, or set to `off`, falls back "
            "to the fixed junk-name substrings: `on` or `off`",
            ("ingest-books",), "on"),
    Setting("chapterDbSite", "The chapter database's address", ("ingest-movies",),
            "`https://chapterdb.plex.tv`", tuning=True),
    Setting("dvdCompareSite", "The disc database's address", ("ingest-movies",),
            "`http://www.dvdcompare.net/comparisons/`", tuning=True),
    Setting("viewerCdnBase", "Where the census page loads its pivot-table engine "
            "from; a local copy makes the page fetch nothing", ("content-census-bi",),
            "`https://cdn.jsdelivr.net/npm`"),
    Setting("viewerPerspectiveVersion", "Which release of that engine the page loads",
            ("content-census-bi",), "5.3.0"),
    Setting("ytdlp", "The yt-dlp a download runs with, instead of the one found: "
            "a command name on PATH or a path. `YTDLP` is accepted too",
            ("ytdlp",), "unset - the one found, printed at the start of the run"),
    Setting("skipYtdlpUpgrade", "Set to 1 and a nightly yt-dlp is not brought up "
            "to date before the fetch. `SKIP_YTDLP_UPGRADE` is accepted too",
            ("ytdlp",),
            "unset - a nightly is upgraded; a preview (-p) never is"),
    Setting("podcastAdsLlm", "Whether a language model looks for host-read ads, "
            "sponsor reads and self-promotion in the transcripts of RSS and site "
            "podcast episodes (the `podcast-ads` task, see docs/models.md), "
            "which have no SponsorBlock: `on` or `off`. Detected stretches are "
            "marked as skippable chapters by default; `podcastAdsCut` opts in "
            "to cutting them out", ("ytdlp",), "on"),
    Setting("podcastAdsCut", "Whether the ad, sponsor and self-promotion "
            "stretches `podcastAdsLlm` finds in an RSS or site podcast episode "
            "are cut out of the file instead of only marked as chapters a "
            "player can skip: `off` or `on`", ("ytdlp",), "off"),
    # --- language models
    Setting("llmModels", "The roster file: every language model you have, "
            "and which tasks use which - see docs/models.md. The single-model "
            "settings below add one more, called `llm`", _AI_CHAPTERS,
            "`models.toml` beside the settings file, where there is one"),
    Setting("llmLean", "Which way every task leans in choosing among the "
            "models that can do it: `light` (small, cheap and fast first), "
            "`balanced` (the roster's order) or `precise` (large and "
            "reasoning first); the roster file can say it per task",
            _AI_CHAPTERS, "unset - each task's own"),
    Setting("llmApi", "The protocol the model is asked over: `openai` "
            "for any server speaking OpenAI's chat protocol - Ollama, llama.cpp, vLLM, "
            "LM Studio and most hosted APIs - or `anthropic` for Claude",
            _AI_CHAPTERS, "`openai`"),
    Setting("llmUrl", "That server's address, up to and including the `/v1`, "
            "e.g. `http://localhost:11434/v1`; for `openai`, a comma list of servers, "
            "the preferred first, e.g. `http://localhost:8081/v1,"
            "http://localhost:8080/v1` - each request goes to the first "
            "that answers, or can be started; for `anthropic`, only to reach another "
            "endpoint than Anthropic's own", _AI_CHAPTERS,
            "unset - required for `openai`"),
    Setting("llmServe", "The command that starts the first of those servers "
            "when it is not running and the GPU has room for it; the run stops it "
            "again once it has been idle five minutes, and at its end. A server found "
            "running is left running", _AI_CHAPTERS),
    Setting("llmServeMib", "The free GPU memory, in MiB, that server is "
            "started with; with less, the next server down the list is asked",
            _AI_CHAPTERS, "26000"),
    Setting("llmModel", "The model's name as that server knows it; a server "
            "that does not list it is asked for the first model it lists",
            _AI_CHAPTERS, "unset - the first model the server lists, required for "
            "a hosted `openai` API; `claude-opus-5-5` for `anthropic`"),
    Setting("llmKey", "The API key, where the server asks for one; for "
            "`anthropic`, `ANTHROPIC_API_KEY` or an `ant auth login` profile does too",
            _AI_CHAPTERS, secret=True),
    Setting("llmContext", "How many tokens the model's context window holds, "
            "as the server runs it: a transcript longer than fits is summarised in "
            "parts before it is chaptered. A server that lists its window "
            "(`max_model_len`) is held to the smaller of the two", _AI_CHAPTERS,
            "the window the server lists, else 32000"),
    Setting("llmOptions", "More fields for every request over the `openai` "
            "protocol, as a JSON object - whatever that server takes, e.g. "
            "`{\"reasoning_effort\": \"low\"}` or `{\"temperature\": 0.2}`; an output "
            "limit given here replaces the one the run works out, and a request "
            "that searches - the chapter instructions (`generate-chapters -P`, "
            "`convert-audio -P`, `convert-video -P`, "
            "`ingest-movies -P`, `ytdlp -P`), the cover search - sends its own "
            "`reasoning_effort`", _AI_CHAPTERS),
    Setting("llmHarness", "The agent harnesses you have installed, a comma "
            "list, the preferred first - only `opencode` is supported. With "
            "one, the chapter instructions (`generate-chapters -P` and the "
            "like) are asked of the same model through the harness instead of "
            "straight to its server, so a local model can search the web as "
            "Claude does, e.g. to spell the speakers' names - and concat-audio "
            "or convert-audio can ask it to search the web for a cover. Each "
            "request is "
            "one throwaway session that is deleted when its answer is read, "
            "so your own sessions stay easy to find. The harness runs with "
            "its permissions auto-approved, and its prompt is the transcript "
            "(for a cover, the book's file names and tags), so use it only "
            "with a model you trust with both",
            _AI_CHAPTERS, "unset - instructions are asked of the server alone"),
    Setting("llmHarnessModel", "The model the harness asks, as it names "
            "models - for opencode `provider/model`, as `opencode models` "
            "lists them, e.g. `ninfer/qwen3.8-27b`. Required with "
            "`llmHarness`: the harness's own default can be a hosted "
            "model, and the transcript would be sent to it", _AI_CHAPTERS),
    Setting("llmTraits", "What the model is, where finding out gets it wrong: "
            "a comma list of `tools`, `vision`, `reasoning`, `images_out`, `moe`, "
            "`cheap`, each with `-` in front for one it lacks, and `params=`, "
            "`active=` (billions), `tok_s=`, `cost_in=`, `cost_out=` (dollars a "
            "million tokens), `size=` (`small`, `medium`, `large`) and `speed=` "
            "(`fast`, `slow`) - e.g. `vision,-reasoning,params=30,active=3`. "
            "What is not said is asked of a local server (llama.cpp's `/props`, "
            "Ollama's `/api/show`) or read off the model's name",
            _AI_CHAPTERS, "unset - all found out"),
    Setting("llmVramGB", "The GPU memory, in GB, the model needs while "
            "whisper is using the same card - ingest-movies starts it beside the "
            "commentary transcription only when that much is free, and otherwise waits "
            "for its turn; only a model served from this machine or its network is "
            "waited for", ("ingest-movies",),
            "unset - the model takes the whole card, so it never runs beside whisper"),
    Setting("llmJobs", "How many requests are sent to the model at once, "
            "over the whole run. Whether the model is local is read off "
            "`llmUrl`: localhost, a private or Tailscale address, or a "
            "name on the LAN", _AI_CHAPTERS,
            "2 for a local model; no limit for a hosted one"),
    # --- comic PDFs
    Setting("comicPdfExtensions", "The file suffixes that may be comic PDFs, "
            "space-separated", ("content-census",), "pdf"),
    Setting("comicPdfMinCoverage", "How much of a page, in percent, its one image has "
            "to cover for it to be a comic page", _COMICS),
    Setting("comicPdfMinPixels", "How wide and high a page image has to be, in pixels",
            _COMICS),
    Setting("comicPdfMinPageShare", "How many of a PDF's pages, in percent, have to be "
            "comic pages for it to be a comic", _COMICS),
    Setting("comicPdfMinDpi", "The lowest resolution a comic PDF is rendered at",
            _COMICS),
    Setting("comicPdfMaxDpi", "The highest resolution a comic PDF is rendered at",
            _COMICS),
    Setting("comicPdfFallbackDpi", "The resolution used where the PDF's own cannot be "
            "read", _COMICS),
    Setting("comicPdfJpegQuality", "The JPEG quality comic PDF pages are rendered at",
            _COMICS),
    Setting("comicEmptyPageLlm", "Whether small comic pages (under 40 KiB "
            "instead of the size-only 10 KiB cutoff) are shown to a vision "
            "model to judge whether they hold real comic-book content worth "
            "keeping rather than being blank, an intentionally-blank notice, "
            "just a page number, or a uniform colour (the `comic-page-check` "
            "task, see docs/models.md): a small rendered copy is sent to the "
            "model. When `off` or with no vision model, the existing 10 KiB "
            "size-only rule applies unchanged: `on` or `off`",
            ("convert-comics",), "on"),
    Setting("comicPdfLlm", "Whether a PDF that the one-image-per-page rule did not "
            "call a comic - either near the threshold or with no single-image pages "
            "at all - is judged by a vision model (the `comic-pdf-judge` task, see "
            "docs/models.md): 2-3 rendered PDF pages and the file name are sent to "
            "it, and the verdict is cached so convert-comics and content-census "
            "agree: `on` or `off`", _COMICS, "on"),
    Setting("comicPdfCache", "Where those vision verdicts for PDFs are cached, keyed "
            "by each file's path, size and modification time", _COMICS,
            "`comic-pdf-verdicts.json` in the cache directory", tuning=True),
    # --- reading books aloud
    Setting("bookLanguageLlm", "Whether read-library asks a small language model "
            "when a book's metadata language and its text disagree or neither "
            "gives a confident answer (the `book-language` task, see "
            "docs/models.md): a ~2 KB excerpt and the book's title/metadata are "
            "sent to the model: `on` or `off`", _NARRATION, "on"),
    Setting("narrationHome", "The ebook2audiobook checkout read-library drives; `-c` "
            "overrides it", _NARRATION, "`~/ebook2audiobook`"),
    Setting("narrationLanguage", "The language a book is read in when its own cannot "
            "be found", _NARRATION),
    Setting("narrationFormat", "The format the engine writes an audiobook in",
            _NARRATION),
    Setting("narrationChannel", "The engine's channel layout", _NARRATION, tuning=True),
    Setting("narrationVramPerBookGB", "The GPU memory, in GB, each book read at once "
            "needs - which sets the default `-j`", _NARRATION),
    Setting("narrationEngineLanguages", "Which languages each engine speaks, as "
            "`engine:codes`", _NARRATION),
    Setting("narrationPythonMinMinor", "The oldest Python 3 minor version the "
            "engine's environment is built with", _NARRATION, tuning=True),
    Setting("narrationPythonMaxMinor", "The newest Python 3 minor version the "
            "engine's environment is built with", _NARRATION, tuning=True),
    Setting("narrationTorchcodecPairs", "Which torchcodec goes with which torch, for "
            "repairing a mismatch", _NARRATION, tuning=True),
    Setting("narrationSoundfileFallbackMark", "The text that marks the engine shim "
            "this package wrote", _NARRATION, "`_load_with_soundfile_fallback`", tuning=True),
    Setting("narrationProgressTail", "How much of the end of the engine's log, in "
            "bytes, is read for its progress", _NARRATION, tuning=True),
    Setting("narrationLosslessTolerance", "How far the engine's lossless copy may be "
            "from the book's length and still be found", _NARRATION, tuning=True),
    Setting("narrationVoiceFallbackNames", "The voice-sample names that mean \"any "
            "language\"", _NARRATION, tuning=True),
    Setting("audiobookLosslessFilters", "The ffmpeg filters the lossless copy of an "
            "audiobook is made with; empty for none", _NARRATION,
            "`dynaudnorm=f=150:g=15,afftdn=nf=-70`"),
    Setting("audiobookLosslessCompression", "The FLAC compression level of that copy",
            _NARRATION, "5"),
    Setting("voiceSampleMaxSeconds", "A voice sample longer than this, in seconds, is "
            "cut down to a stretch of speech", _NARRATION, tuning=True),
    Setting("voiceSampleSearchSeconds", "How much of a long sample's middle, in "
            "seconds, is searched for speech", _NARRATION, tuning=True),
    Setting("voiceSampleSeconds", "How long the stretch of speech taken is, in seconds",
            _NARRATION, tuning=True),
    Setting("voiceSampleMinSeconds", "The shortest stretch still taken, in seconds",
            _NARRATION, tuning=True),
    Setting("voiceSampleMaxGap", "The longest pause, in seconds, inside that stretch",
            _NARRATION, tuning=True),
    Setting("voiceSilenceNoise", "What counts as silence when looking for it",
            _NARRATION, tuning=True),
    Setting("voiceSilenceMinDur", "The shortest silence, in seconds, that counts",
            _NARRATION, tuning=True),
    Setting("voiceSampleRate", "The sample rate a prepared voice sample is written at",
            _NARRATION, tuning=True),
    Setting("voiceSampleCodec", "The codec a prepared voice sample is written in",
            _NARRATION, tuning=True),
    # --- naming
    Setting("coverName", "The name clean-folder-structure gives a folder's cover "
            "art: `folder` (the default) or `cover`, or empty for no rename at "
            "all - the art then keeps the name it came in under",
            ("clean-folder-structure",), "folder"),
    Setting("dateForm", "The form clean-folder-structure's -d writes a leading "
            "date in: `compact` (the default, `YYYYMMDD`) or `iso` "
            "(`YYYY-MM-DD`); -y sorts files that lead with a date in either "
            "form into their year", ("clean-folder-structure",), "compact"),
)

# Spelled like settings and not settings: what a command works out and hands
# to the commands, workers or engine it starts, overwriting whatever was
# there. Setting one changes nothing, so no page offers them.
INTERNAL = frozenset((
    "narrationPython", "narrationDevice", "narrationEnvReady", "narrationVerbose",
    "narrationVoiceMap", "narrationEngine", "censusVideoExtensions",
    "censusPageExtensions", "censusAllExtensions", "imageStatsFile",
    "imageGpuLane", "imageGpuTrim",
    # The usage text a refusal prints after itself, which only a shell caller
    # ever set.
    "usage",
))

BY_NAME = {setting.name: setting for setting in SETTINGS}


def config_home() -> str:
    """The medialib configuration home: ``$XDG_CONFIG_HOME/medialib`` where
    that is set, ``~/.config/medialib`` where it is not - where the settings
    file and the user's profile files live."""
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(
        os.path.expanduser("~"), ".config")
    return os.path.join(base, "medialib")


def cache_home() -> str:
    """The medialib cache home: ``$XDG_CACHE_HOME/medialib`` where that is
    set, otherwise the platform's per-user cache directory (``~/.cache``,
    ``~/Library/Caches`` on macOS, ``%LOCALAPPDATA%`` on Windows)."""
    xdg = os.environ.get("XDG_CACHE_HOME", "").strip()
    if xdg:
        return os.path.join(xdg, "medialib")
    if os.name == "nt":
        local = os.environ.get("LOCALAPPDATA", "").strip()
        base = local or os.path.join(os.path.expanduser("~"), ".cache")
    elif sys.platform == "darwin":
        base = os.path.join(os.path.expanduser("~"), "Library", "Caches")
    else:
        base = os.path.join(os.path.expanduser("~"), ".cache")
    return os.path.join(base, "medialib")


def settings_file() -> str:
    """Where the settings file is, whether or not there is one."""
    named = os.environ.get(FILE_VARIABLE, "")
    if named:
        return named
    return os.path.join(config_home(), "settings")


def shipped_file() -> str:
    """The defaults file that ships with the package: the value each setting
    has beneath the environment and the user's own file."""
    return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "config", "settings.defaults")


@functools.cache
def shipped() -> dict[str, str]:
    """The shipped defaults, name to value."""
    return read_file(shipped_file())


def value(name: str) -> str:
    """The value of a setting: the environment's - where the user's file was
    loaded into it - or else the shipped default, "" for one with neither.
    A blank value is no value, so the shipped default stands in for it."""
    return os.environ.get(name, "").strip() or shipped().get(name, "")


def read_file(path: str) -> dict[str, str]:
    """The assignments in a settings file, in its order.

    SettingsError for a line that is not one, naming the line.
    """
    found: dict[str, str] = {}
    with open(path, encoding="utf-8") as handle:
        for number, line in enumerate(handle, 1):
            text = line.strip()
            if not text or text.startswith("#"):
                continue
            if text.startswith("export "):
                text = text[len("export "):].lstrip()
            name, sep, value = text.partition("=")
            if not sep or not name.isidentifier():
                raise SettingsError("%s, line %d: not a name=value line"
                                    % (path, number))
            try:
                found[name] = shell_value(value, {**os.environ, **found})
            except ValueError as error:
                raise SettingsError("%s, line %d: %s" % (path, number, error)) from error
    return found


def shell_value(text: str, environ) -> str:
    """The value a shell gives ``name=<text>``.

    Read the way bash reads it, because the lines are copied out of a shell
    profile: what is single-quoted is taken as it is; what is double-quoted or
    not quoted at all has ``$NAME`` and ``${NAME}`` replaced from ``environ``
    and a backslash escaping the character after it; a ``~`` it starts with is
    the home directory; and the value ends at the first space not inside
    quotes, after which only a ``#`` comment may follow. ValueError for a
    quote left open, or for more than one word.
    """
    out: list[str] = []
    at, quote = 0, ""
    if text.startswith("~") and (len(text) == 1 or text[1] == "/"):
        out.append(os.path.expanduser("~"))
        at = 1
    while at < len(text):
        char = text[at]
        if quote == "'":
            if char == "'":
                quote = ""
            else:
                out.append(char)
        elif char == "\\" and at + 1 < len(text) and (
                not quote or text[at + 1] in '$`"\\'):
            out.append(text[at + 1])
            at += 1
        elif char == "$" and _variable(text, at + 1)[0]:
            name, length = _variable(text, at + 1)
            out.append(environ.get(name, ""))
            at += length
        elif quote == '"':
            if char == '"':
                quote = ""
            else:
                out.append(char)
        elif char in "'\"":
            quote = char
        elif char.isspace():
            rest = text[at:].strip()
            if rest and not rest.startswith("#"):
                raise ValueError("a value with spaces has to be quoted")
            break
        else:
            out.append(char)
        at += 1
    if quote:
        raise ValueError("a %s quote is never closed" % quote)
    return "".join(out)


def _variable(text: str, at: int) -> tuple[str, int]:
    """The name a ``$`` just before ``at`` refers to, and how many characters
    after the ``$`` it takes - ("", 0) where it refers to none."""
    if text[at:at + 1] == "{":
        end = text.find("}", at)
        name = text[at + 1:end] if end > 0 else ""
        return (name, end - at + 1) if name.isidentifier() else ("", 0)
    end = at
    while end < len(text) and (text[end].isalnum() or text[end] == "_"):
        end += 1
    name = text[at:end]
    return (name, end - at) if name.isidentifier() else ("", 0)


def readable_by_others(path: str) -> bool:
    """Whether another account can read the file. Windows keeps no such
    bits in a file's mode, and is not asked."""
    if os.name == "nt":
        return False
    return bool(os.stat(path).st_mode & (stat.S_IRWXG | stat.S_IRWXO))


def load(stream=None) -> None:
    """Read the settings file into this process's environment, under what
    the environment already says. Nothing to do where there is no file.

    The first thing every command does. A file that cannot be used ends the
    run: SystemExit, after saying why.
    """
    stream = sys.stderr if stream is None else stream
    path = settings_file()
    if not os.path.isfile(path):
        _apply_shipped()
        return
    try:
        values = read_file(path)
        secrets = [name for name in values
                   if name in BY_NAME and BY_NAME[name].secret]
        if secrets and readable_by_others(path):
            raise SettingsError(
                "%s holds %s and other accounts on this machine can read it - "
                "make it private first: chmod 600 %s"
                % (path, ", ".join(secrets), shlex.quote(path)))
    except (OSError, SettingsError) as error:
        stream.write("==> ERROR: %s\n" % error)
        from medialib.lib import errorlog

        errorlog.record("config", str(error), files=[path], stack=False)
        raise SystemExit(1) from error
    for name, value in values.items():
        if name not in BY_NAME:
            near = difflib.get_close_matches(name, BY_NAME, n=1)
            stream.write("==> WARNING: %s sets %s, which no command reads%s\n"
                         % (path, name, " - did you mean %s?" % near[0] if near else ""))
        os.environ.setdefault(name, value)
    _apply_shipped()


def _apply_shipped() -> None:
    """The shipped defaults, beneath everything else, so a tool a command
    starts sees them as the command does."""
    for name, default in shipped().items():
        os.environ.setdefault(name, default)


def page() -> str:
    """docs/settings.md, as the registry says it."""
    from medialib import commands

    lines = [
        "# Settings",
        "",
        "Every setting is an environment variable. A command finds it in the",
        "environment it was started in, or in the settings file",
        "`~/.config/medialib/settings` (`$XDG_CONFIG_HOME/medialib/settings` where",
        "that is set) - one `name=value` per line, the way a shell profile writes",
        "it, `export` and quotes allowed. The environment wins, so one run can",
        "override the file without editing it. Beneath both is the file that ships",
        "with the package, `medialib/config/settings.defaults`, whose values the",
        "Default column prints.",
        "",
        "Every command that takes options can override a setting for one run",
        "with `-x name=value`, repeatable: the value lands in the run's",
        "environment and wins over the file. A secret is not taken there - the",
        "file is where it goes.",
        "",
        "A file that holds a secret (marked below) must be readable by you alone -",
        "`chmod 600` - or every command refuses to start. A name in the file that",
        "no command reads is warned about.",
    ]
    header = ["", "| Setting | What it does | Default | Read by |", "| --- | --- | --- | --- |"]

    def row(setting: Setting) -> str:
        if set(setting.commands) == set(commands.COMMANDS):
            read_by = "every command"
        else:
            read_by = ", ".join("`%s`" % command for command in setting.commands)
        return "| `%s`%s | %s | %s | %s |" % (
            setting.name, " (secret)" if setting.secret else "",
            setting.about.replace("|", "\\|"),
            shipped().get(setting.name, setting.default).replace("|", "\\|"),
            read_by)

    ordered = sorted(SETTINGS, key=lambda one: one.name.lower())
    lines += header + [row(setting) for setting in ordered if not setting.tuning]
    lines += ["", "## Tuning and testing", "",
              "These are read too, but the defaults are what the commands are",
              "built and tested with."]
    lines += header + [row(setting) for setting in ordered if setting.tuning]
    return "\n".join(lines) + "\n"
