# `ingest-movies`

Sorts, cleans and improves a movie library in place: one folder per film, Plex
names with the IMDb id, subtitles in the languages the `spokenLanguages`
setting names (six by default), lossless audio converted to the target codec,
transcribed commentaries and the chapters the disc had.

    ingest-movies [options] <movieDir>...

| At a glance | |
| --- | --- |
| **Takes** | one or more folders of films |
| **Writes** | in place: the improved `.mkv` and `.srt` sidecars (the converted lossless audio is made in RAM and only ever lands inside the improved `.mkv`); the sweeps also write lists under `logs/ingest-movies/` |
| **Input** | changed — that is the job; each remuxed original is kept as `<name> (old).mkv` |
| **Reruns** | a film that already has its `(old)` backup is not remuxed again |
| **Network** | TMDb, OpenSubtitles, SubDL, IMDb's title lists, ChapterDB and dvdcompare.net — the last two a few seconds apart, with some jitter — and your own Plex server |

## Default behavior

With no options, a run goes through every phase in one pass over the folders
given (one level deep, not the whole tree):

- **deletes release junk**: files with the `movieJunkExtensions` suffixes (`txt nfo exe doc sfv` by default - an empty setting deletes none), samples, `.mkv`s under 1000 KiB, folders named `.unwanted` (qBittorrent's skipped files), and folders left empty
- **remuxes non-Matroska video** (avi, mp4, mov, …) into `.mkv`, deleting the source
- sorts loose films into a folder each, with extras where Plex looks for them
- cleans folder, film and subtitle names
- **transcodes lossless audio to Opus** — or FLAC, the `losslessAudioTarget` setting — leaving lossy tracks as they are
- **finds forced and SDH subtitles the file does not flag**, and puts their flags right ([details](#subtitles-the-file-flags-wrong))
- **finds commentaries the file does not flag** and flags them before anything else reads the tracks ([details](#commentaries-the-file-does-not-flag))
- **reads picture subtitles into text** where a language has no text subtitle (needs `seconv`) ([details](#picture-subtitles-read-into-text))
- transcribes commentary tracks with whisper, and names numbered ones after who speaks in them (dvdcompare.net)
- **remuxes each film once into an improved copy**, keeping the original as `<name> (old).mkv` ([details](#what-a-full-ingest-does))
- tags names with the IMDb id (needs `tmdbApiKey`), looks up chapters, and downloads missing subtitles (needs an OpenSubtitles or SubDL login)
- last, **compares each film with the copy your Plex library already has**, if any ([details](#plex-comparison))
- with `-r`, after all of that, **converts the remuxes** and writes each converted copy beside its film ([details](#remux-conversion--r))
- with `-o`, any of the phases above is left out ([details](#leaving-phases-out--o))

Each phase can also be run on its own, over a whole library, and several in a
row: see [single-task sweeps](#single-task-sweeps) and [`-O`](#single-task-sweeps-chained--o).

**Contents:** [Default behavior](#default-behavior) · [Settings](#settings) · [Single-task sweeps](#single-task-sweeps) ·
[What a full ingest does](#what-a-full-ingest-does) ·
[Naming films](#naming-films) · [Feeding ids back](#feeding-ids-back--i) ·
[Subtitles](#subtitles) · [Commentary transcripts](#commentary-transcripts) ·
[Commentary names](#commentary-names) · [Chapters](#chapters) ·
[Plex comparison](#plex-comparison) · [Remux conversion](#remux-conversion--r) · [Keeping everything](#keeping-everything--k) ·
[Leaving phases out](#leaving-phases-out--o) · [Chaining sweeps](#single-task-sweeps-chained--o) ·
[Several folders](#several-folders)

## Settings

These are read from the environment:

| Variable | Needed for | Without it |
| --- | --- | --- |
| `tmdbApiKey` | naming films (TMDb lookups) | the lookup is skipped, and the run says so |
| `openSubtitlesUser`, `openSubtitlesPassword` | downloading missing subtitles from OpenSubtitles.org, the first source | the other sources below are still asked, and the run says OpenSubtitles was skipped |
| `openSubtitlesComUser`, `openSubtitlesComPassword` | asking OpenSubtitles.com second — a separate service, with free accounts of its own | it is not asked |
| `subDlApiKey` | asking SubDL third — the key a free account at subdl.com gets | it is not asked |
| `spokenLanguages` | the languages whose subtitles are fetched and tested; a code the language table does not know stops the run before a film is touched | the six it knows by default (en, de, fr, nl, es, it) |
| `mediaServerNaming` | the server the names are written for | `plex`; the names are then written and read the way that server spells them |
| `subtitleOcrEngines` | the engines a picture subtitle is read with, in the order to try them | the order below |
| `subtitleOcrUrl` | the servers holding a vision model, for the `vision` engine: a comma list, the preferred first | that engine is not tried |
| `subtitleOcrServe` | the command that starts the first of them when it is not running | it is only used when it is running |
| `subtitleOcrServeMib` | the free GPU memory it is started with | 26000 |
| `subtitleOcrModel` | that model's name, asked of a server that lists it | the first model each server lists |
| `subtitleOcrOptions` | more fields for every request to it, as a JSON object | none |
| `subtitleOcrJobs` | how many requests are sent to that model at once, each with up to 16 pictures | 32 |
| `losslessAudioTarget` | the codec the lossless audio is converted to | `opus`, at a per-channel target bitrate; `flac` takes the channels as they are, up to eight of them |
| `plexUrl` | the Plex server the films are compared with | the server on this machine, `http://localhost:32400` |
| `plexToken` | that server's X-Plex-Token | the server is asked without one, which it refuses unless told to trust this address; the comparison is then skipped |
| `listProposalsLlm` | proposing additions to the hand-kept word and extension lists from logged refusals, near-misses, unmatched/ambiguous folders, and skipped files (the `curated-lists` task in [docs/models.md](../models.md), preferring a local reasoning model); writes proposals to `logs/ingest-movies-list-proposals-<timestamp>.txt` for manual review and never edits source files automatically | `on`; set to `off` to skip without asking any model (also skipped quietly when nothing was logged or no model is configured) |

With none of the three set, no subtitle is downloaded; the ones already beside
a film are still tested.

## Single-task sweeps

A full ingest does everything in one pass. Each of these phases, named in `-O`, does **one**
phase of it and nothing else: no conversion, no remux and none of the other
phases. They walk the **whole tree** below each folder given, so they can be
pointed at a library.

| Phase | Does only | By default | With `-w` | Leaves behind | Run it after |
| --- | --- | --- | --- | --- | --- |
| `-O layout` | the layout: junk deleted, videos remuxed into Matroska, a folder per film, extras sorted, names cleaned, subtitles moved beside their film | not a dry run | left off | — | — |
| [`-O roles`](#subtitles-the-file-flags-wrong) | measuring the subtitles for forced and SDH, and fixing those flags | writes as it goes | left off | — | — |
| [`-O ocr`](#picture-subtitles-read-into-text) | reading picture subtitles into `.srt` sidecars | writes as it goes | left off | sidecars beside the films | — |
| [`-O tagging`](#naming-films) | the server naming: IMDb id, editions, stacking token | dry run: prints every rename | carries the renames out | [lists](#the-lists-tagging-writes) in `logs/ingest-movies/` | — |
| [`-O tagging -i <file>`](#feeding-ids-back--i) | the same naming, from the id list you filled in | dry run: prints, writes only the conflicts and duplicates lists | renames, and brings the file up to date | — | `-O tagging` |
| [`-O subtitles`](#subtitles) | testing the subtitles already there | dry run: says which would be kept | syncs, deletes the out-of-step ones, downloads the missing | `logs/ingest-movies/ingest-movies-subtitles-<folder>.txt` | `-wO tagging` |
| [`-O commentary`](#commentary-transcripts) | transcribing commentary tracks | writes as it goes | left off | transcripts beside the films | — |
| [`-O names`](#commentary-names) | naming numbered commentary tracks | dry run | names the tracks | `logs/ingest-movies/ingest-movies-commentarynames-<folder>.txt` | `-O commentary` |
| [`-O chapters`](#chapters) | chapters from the ChapterDB archive | writes as it goes | left off | — | `-wO tagging` |
| [`-O extras-chapters`](#chaptering-the-extras--c) | chapters for the extras, worked out by the language model (`-P` adds instructions) | writes as it goes | left off | — | — |
| [`-O plex`](#plex-comparison) | comparing with the Plex library | changes nothing | left off | — | `-wO tagging` |
| [`-O backups`](#dropping-the-old-backups) | dropping the `(old)` originals the checks passed | dry run: says which would be dropped and which would stay, with the reason | tags the copies and drops the originals | `logs/ingest-movies/ingest-movies-backups-<folder>.txt` | `-wO tagging` |

`-O subtitles`, `-O commentary`, `-O names` and `-O chapters` take the films in name order and open each with
`[n/total] <film>`, its place in the folder, so a film nothing is done to still
shows up. What is said about that film follows, indented under it. For `-O commentary`
that is one line per commentary track. A transcription or a chapter write
that finishes later names its own film.

**`-w` on its own is refused.** Without `-O` naming `tagging`, `subtitles`, `names` or `backups` there is
no dry run to carry out, and the run it would otherwise start is a full ingest.
Named with `layout`, `roles`, `ocr`, `commentary`, `chapters`, `extras-chapters` or `plex` only, it is refused as well: the
layout, the transcription and the chapter lookups are not dry runs and the comparison changes
nothing, so there is nothing for `-w` to carry out. Beside a phase that has a dry
run it is left off those.

Following those dependencies, a library is brought up to date in this order:

```bash
ingest-movies -O tagging ~/Films     # review what the naming would do
ingest-movies -wO tagging ~/Films     # tag the films with their ids
ingest-movies -wO subtitles ~/Films     # subtitles, now searched for by id
ingest-movies -O commentary ~/Films     # transcribe the commentaries
ingest-movies -wO names ~/Films     # name them from those transcripts
ingest-movies -O chapters ~/Films     # chapters for the tagged films
ingest-movies -O plex ~/Films     # which of them the Plex library already has
ingest-movies -wO backups ~/Films     # drop the (old) originals the checks passed
```

## What a full ingest does

ChapterDB and dvdcompare.net are asked at a polite pace, a few seconds apart,
so asking them about a whole library takes a while. The full ingest doesn't
wait for that: once the names have settled, early in the run, each site is
asked in the background about every film that will need it, while the
run carries on with the transcoding, transcription and remux. The tagging
names more folders, so those are handed over too. Commentary naming and
chapters come last, after the subtitle downloads, and by then the answers are
usually all in. If those lookups are still going, the run says so and waits
for them.

The work on the films themselves overlaps the same way, each kind on the part
of the machine it uses:

- **Subtitles are fetched ahead.** In the background, the run asks TMDb
  which film each folder is (the same questions the tagging asks at the end,
  whose answers it then reuses) and downloads the missing subtitles of every
  film it got an id for while the rest of the run goes on. They are still
  fetched one film at a time, and this is finished before the tagging
  renames anything. A film it did not get to is fetched after the tagging.
- **The lossless transcode beside the transcription.** With whisper on the GPU, the
  transcode runs at the same time as the commentary transcription, one job
  narrower so whisper keeps its cores. With whisper on the CPU they would only
  fight over the same cores, so the transcode goes first.
- **Each film is remuxed as soon as it is ready**: once its own converted
  lossless tracks and transcripts are done and its subtitles have been
  fetched, one film at a time, while the transcode and the transcription
  carry on with the others. The films it didn't get to are remuxed after the
  transcription.
- **One disk-heavy job at a time per spinning disk.** A lossless transcode, a
  commentary extract, a remux and a subtitle sync against the film all read a
  whole film, and on a hard disk two of them at once are slower than one after
  the other. So they take turns on each spinning disk, which the run detects,
  encrypted and LVM disks included. An SSD or NVMe disk is shared.

Each film's lines from the background work are printed together when that film
is done, rather than mixed into the other work's output.

### Subtitles

Subtitles are downloaded after the films are tagged with their IMDb ids, and a
tagged film is searched for by its id, so only a subtitle catalogued under that
very film is taken — never one for a remake or another film of the same name. A
downloaded subtitle is kept only when it lines up with the film's speech at one
offset far better than at any other, however far that offset is from where it
started; a subtitle that fits nowhere in particular belongs to some other film
or cut and is thrown away, to be tried again on the next run.

The sources are asked in turn, each only once its login is set (see
[Settings](#settings)): OpenSubtitles.org, then OpenSubtitles.com, then SubDL.
When a source has none, or the ones it had were thrown away, the next is asked —
up to two subtitles from each source and five in all, every one put to the same
test whichever source it came from. A thrown-away subtitle is not offered again
in that run, but nothing remembers it past the run.

Before it is tested, a downloaded subtitle has its adverts taken out — the cues
a catalogue or an uploader adds before the film starts or after it ends — by
[subcleaner](https://github.com/KBlixt/subcleaner). It is optional, and not
packaged anywhere:

```bash
git clone https://github.com/KBlixt/subcleaner ~/.local/share/subcleaner
chmod +x ~/.local/share/subcleaner/subcleaner.py
ln -s ~/.local/share/subcleaner/subcleaner.py ~/.local/bin/subcleaner
```

subcleaner only knows the words of a few languages by heart; for the others to
be cleaned too (German, French and Italian among ours), set
`require_language_profile = false` in its `subcleaner.conf` — the file appears
in `~/.local/share/subcleaner` the first time it runs. The run warns once a language when it
is not.

Where the film carries a picture subtitle of its own (a Blu-ray's or a DVD's,
not forced), the times its lines are on screen stand in for the speech: they
are exact, and they still work where speech detection does not — a musical,
whose sung lines over an orchestra it cannot pick out. A subtitle that does not
line up with them is still tried against the speech before it is thrown away.

### Subtitles the file flags wrong

Many discs carry a forced or SDH subtitle that is neither flagged nor named as
one, and a film in a foreign language often flags its one full subtitle as
forced. So when the track flags are refreshed, each film's subtitles are
measured, and a flag the measurement disagrees with is set or cleared in the
same header edit:

| Measured | What it says |
| --- | --- |
| how many lines a subtitle has, beside the longest other subtitle of the film | under a quarter of it is **forced** (the signs and the odd foreign line); a track flagged forced with more than 60% of it is the **whole dialogue**, and loses the forced flag and the word "forced" from its name |
| the text of a text subtitle that says it is the plain one | when one line in ten or more is a sound in brackets, a note of music or a speaker's name in capitals, it is **SDH** |

The line counts come from the statistics tags mkvmerge writes into a file; for a
file without them, ffprobe counts the packets, which reads the whole film. A
Blu-ray's picture subtitle spends two packets on each line. Nothing is measured
against a subtitle of fewer than 100 lines, one counted at over 5000 (a picture
subtitle that redraws its lines packet by packet), or a commentary, and a
commentary subtitle is never changed. Each correction is logged in one line
saying what the file said and what was measured. Only the flags (and that
name) change: no track is dropped for its role, so `-k tracks` keeps every
one, and `-o roles` leaves the flags as the file has them.

A downloaded `<name>.xx.srt` whose text reads as SDH is muxed in flagged SDH and
named "SDH". The sidecar keeps its `.xx.srt` name, which is what tells the next
run's download that the language is there.

### Picture subtitles read into text

A Blu-ray's subtitles (PGS) and a DVD's (VobSub) are pictures of the lines: a
player shows them, but nothing can search, restyle or sync them. Where a film
has no text subtitle in a language `spokenLanguages` names, and no sidecar for
it beside the film — none was downloaded ahead of the remux, or none passed the
sync test — its picture subtitle in that language is read into one by OCR, just
before the remux, and muxed in like a download. Each role gets its own sidecar
(see [Subtitles the file flags wrong](#subtitles-the-file-flags-wrong)):

| Picture subtitle | Read into |
| --- | --- |
| the plain one — the longest, where there are several | `<name>.xx.srt` |
| the SDH one | `<name>.xx.sdh.srt`, muxed flagged for the hearing impaired; and `<name>.xx.srt` when there is no plain one and no SDH text subtitle |
| the forced one | `<name>.xx.forced.srt`, muxed flagged forced |
| a commentary's, or a broadcast's (DVB) | nothing: it stays a picture |

Its lines keep the picture subtitle's own times, so it is in step with the
film by construction, and is not put to the sync test.

The reading is done by Subtitle Edit's `seconv`, which is optional: without it
picture subtitles stay pictures, and the run says so once at the start. It is
a self-contained download from the
[Subtitle Edit releases](https://github.com/SubtitleEdit/subtitleedit/releases):

```bash
tar xzf SeConv-Linux-x64.tar.gz -C ~/.local/share/seconv
ln -s ~/.local/share/seconv/seconv ~/.local/bin/seconv
```

A film's subtitles may be in any language, so an engine that reads them all is
preferred wherever the machine can run it at speed. Each is tried where it is
ready, and one that fails or reads nothing hands the subtitle to the next:

| Order | Engine | Languages | Needs |
| --- | --- | --- | --- |
| 1 | `vision`, a vision model | any | `subtitleOcrUrl`: servers with the OpenAI chat API holding one — llama-server, or any that lists its model at `/v1/models` — tried in order (below) |
| 2 | `paddle` (PP-OCR) on the GPU | a hundred-odd | `paddleocr` with a CUDA build of paddle, and 2 GiB free on the card — asked afresh for each subtitle, since another program may hold it |
| 3 | `tesseract` on the CPU | the ones whose data is installed: `apt install tesseract-ocr-fra` for French, `tesseract-ocr-all` for all | `tesseract` |
| 4 | `paddle` on the CPU, the GPU hidden from it | a hundred-odd, at about a second a line | `paddleocr` |

`paddleocr` installs with `pipx install --python python3.12 paddleocr`, then
`pipx inject paddleocr "paddlepaddle<3.3"` (3.3 fails on the CPU), or a CUDA
build of paddlepaddle for the GPU. A GPU too full for paddle after all is one
more failure the next engine answers, so the run never stops over it. A
language tesseract has no data for is said once. `subtitleOcrEngines` names
other engines or another order.

Measured on a French Blu-ray, against the pictures:

| Engine | Forced track, 57 lines | Full track, 1,342 pictures | Reads |
| --- | --- | --- | --- |
| `vision`, a 35B-A3B model, 16 pictures a request | 3 s | 43 s | best: accents, quotes and line breaks right; a slip in a word now and then |
| `vision`, the same, one picture a request | 2 s | 210 s | as well, but a blank picture gets an apology and two-line pictures lose their break |
| `vision`, a 27B model, one picture a request | 5 s | 237 s | as well |
| `tesseract` | 9 s | 232 s | well, but `V` comes out as `\/`, `Il` as `\|\|` and accented capitals lose their accent |
| `paddle` on the CPU | 90 s | 27 min | garbles about half the lines, with seconv's picture cleaning or without it |

The servers in `subtitleOcrUrl` are tried for each subtitle in the order
given, the first started with `subtitleOcrServe` when the GPU has
`subtitleOcrServeMib` free, the way
[`generate-chapters`](generate-chapters.md#several-servers-one-card) does it
with its own servers; with none to be had, the engines after `vision` read.

For example, a small vision model started on port 8082 when the card is
free, and the larger model a chat server keeps on port 8080 when that server
holds the card:

```bash
subtitleOcrUrl=http://127.0.0.1:8082/v1,http://127.0.0.1:8080/v1
subtitleOcrServe=llama-server --model /models/vision.gguf --mmproj /models/mmproj.gguf --port 8082
```

The film's picture subtitles are taken out with `mkvextract` first, all in one
pass: `seconv` handed the film itself would hold the whole of it in memory.

`-o ocr` leaves the picture subtitles as they are.

### Commentary tracks

A track counts as a commentary when its mkv commentary flag says so *or* when its
name does — in any of the supported languages, so a German disc's
"Audiokommentar" is picked up as readily as an "Audio Commentary". Commentary is
transcribed **in the language it is spoken in** — read from its track name or
language tag, and heard by whisper when neither says:

| Commentary spoken in | Transcripts written |
| --- | --- |
| English | `<name>.en.srt` |
| another supported language | the native `<name>.xx.srt` *and* an English translation |
| anything else | only the English translation |

### Commentaries the file does not flag

A disc's commentary often arrives with neither the flag nor a name that says
what it is — "English", "Stereo", or no name at all. Left like that it is not
transcribed, and the remux may even drop it as a lesser copy of the main mix.

So before anything reads the tracks, each film still to be improved has its
mono and stereo audio tracks after the first one listened to: three minutes,
in three windows from the middle of the film, of each of them and of the
film's first audio track, heard by whisper. A commentary gives itself away
twice over:

| Signal | A commentary | A downmix, the original mono, a dub, a score |
| --- | --- | --- |
| how much of it is speech | far more than the film: it is talked over end to end | about as much as the film, or none at all |
| how many of its words the film's track also has | few: the dialogue is ducked under the commentators | most of them — except a dub, which is in another language |

A track both say so of is flagged as a commentary and named "Commentary N" in
place, which the commentary naming later replaces with who speaks in it. A
track only one says so of is left as it is and listed at the end of the run, to
be listened to. A track flagged for the visually impaired, or named as an audio
description, a score or music, is not listened to at all.

A commentary the file does flag but leaves without a name is numbered the same
way, among the others in file order. Otherwise its transcript would be called
just "Commentary", like every other unnamed one, and the commentary naming
could not tell them apart.

This needs whisper, so it runs only when the transcription does, and only on
films that have not been improved yet.

### The remux

As the last work on each film it remuxes an improved copy in **one** `mkvmerge`
call, so the film is remuxed once and only one output exists at a time:

| What | What the remux does |
| --- | --- |
| Lossless audio converted to the target codec | the `losslessAudioTarget` setting: `opus`, the default, at a per-channel target bitrate, or `flac`, which takes the channels as they are, up to eight of them. A track the target cannot take stays lossless. A TrueHD Atmos bed loses its objects either way, since both carry the discrete bed. The conversion is swapped in place, keeping index, language, name and flags; it is encoded into RAM and never written beside the film, and a run cut short encodes it again next time |
| Excessive audio and image-subtitle tracks | dropped: everything not in a kept language - the `keepLanguages` setting, a comma list of ISO 639-1 codes, `en` by default - or the film's own language. That is TMDb's original language (with `tmdbApiKey` set), asked by the id the folder carries or the tagging would give it, and the first audio track's as well when TMDb lists it as spoken in the film or lists no spoken languages - TMDb has some films' original language wrong, and some films are made in several. With no answer, or no soundtrack in the original language, it is the first audio track's alone. Chinese varieties (Cantonese, Mandarin, ...) count as one language, as do the written Norwegians, Serbo-Croatian, Arabic, Persian and Malay, since discs tag them unevenly. Commentary **audio** is exempt from the excessive-language rule, so a foreign-language commentary is never dropped from the film its subtitles were just made for |
| Downloaded subtitles (`<name>.xx.srt`) that passed the sync test, and the ones read off a picture subtitle, with their `.forced.srt` and `.sdh.srt` siblings | muxed in, placed after the other subtitles and before the commentary ones, a forced one flagged forced and an SDH one flagged for the hearing impaired — unless the film already carries a text subtitle of that language and role. A plain `.xx.srt` that reads as SDH goes in flagged SDH. A picture subtitle, or a text one of another role, doesn't count, so the sidecar goes in beside it. The `.srt` stays beside the film |
| Commentary audio | moved below the other audio tracks |
| Commentary transcripts | appended, each tagged with its own language |
| Dual-layer Dolby Vision profile 7 | **normalised to single-layer profile 8.1**, so DV survives on hardware that cannot read the enhancement layer. Metadata only — the video is **never re-encoded**. Needs `dovi_tool` |
| A Dolby Vision level that overstates the video, whatever the profile | **corrected**, in the container metadata only: a player refuses a file that asks for more than its hardware has |
| Profile 5, profile 8.x, HDR10, HDR10+ and SDR | left untouched |
| A Dolby Vision claim the video does not back up | **dropped**, so the file stops lying about itself (below) |

What the container reports is only a claim, so every DV file's stream is probed
for an RPU that is really there, and a hollow claim is stripped, leaving the
file reporting what its bitstream really is. A file whose RPU is really there is
only checked, never rewritten, and a copy that came out less HDR than the source
is thrown away and the original left alone.

The original is preserved as `<name> (old).mkv`, so nothing is lost and a rerun
is a no-op — which is also what makes the Dolby Vision work a one-time job per
film: a film that already has its `(old)` backup is not remuxed again. Once the
copy has stood the checks, [`-O backups`](#dropping-the-old-backups) drops that
backup.

### The Plex names

Last, once each film is the file it is going to stay, the folder and everything
in it are renamed — renamed only, in place — into the three names the server
reads:

| Name | Goes on | What it does |
| --- | --- | --- |
| `{imdb-ttXXXXXXX}` | the folder and every file | forces the match |
| `{edition-...}` | a folder's second, third and fourth *version* of one film | the server collapses them into one film with a named picker: `Your Film (2020) colorized.mkv` becomes `Your Film (2020) {imdb-tt0000000} {edition-Colorized}.mkv` |
| `Part1` / `cd1` | a split film's parts | stays **last**, because that is where the server's scanner reads it — a tag appended after it would stop the parts stacking at all |

The `mediaServerNaming` setting names the server the names are written for —
`plex` (the default) or `jellyfin`. For Jellyfin the id tag is written
`[imdbid-ttXXXXXXX]` or `[tmdbid-XXXXXXX]`, and an edition as a version label
after the whole name instead of a tag: `Your Film (2020) [imdbid-tt0000000]
- Colorized.mkv`. A name written either way is read back the same, so a library
may be switched to the other server and the run still tells every film apart:
the id is read off both spellings, and the record a run keeps across runs
answers a film under either of its names.

Every sidecar follows its own film, including a commentary transcript whose name
extends the film's. The `(old)` backup keeps its own name, and running the
ingest again changes nothing.

## Naming films

**`-O tagging` does only this phase** — no conversion, no remux, no downloads, no
transcription — and does it as a **dry run** unless `-w` is given, printing
every rename it would make. Nothing is renamed for a film TMDb could not name,
nor for a folder holding a film that is not its own; those go on
[the lists](#the-lists-tagging-writes) instead. Requests to TMDb are paced to ten a
second.

### A year Plex cannot see

Before anything is looked up, a folder whose **year is not written where a
reader can see it** is renamed onto one where it is:

| Written as | The problem |
| --- | --- |
| a separator where the space before the bracket should be (`The Movie-(1999)`) | not a year to Plex's scanner |
| a run of spaces instead of one | the title carries a trailing space into every lookup |
| a year that kept only one of its two brackets | not a year to Plex's scanner |

The name is put right on the disk, so the folders no catalogue can name are
fixed along with the ones it can.

### When a title matches

A film is named when **one** candidate carries its title — primary, original or
any alternative — and was released in the folder's year *in some country*, which
is what keeps a festival premiere and a release a year later from being two
different films. "Carries its title" is not a string compare. All of these are
one title said differently, and the readings **compose**, so a name that went
wrong in several ways at once is still found:

- an accent dropped, or written out the way a language writes it when it cannot
  be reached — `ä` reads as `a` and as `ae`, `ø` as `o` and as `oe`;
- a dash, apostrophe, comma or point written as a space, or as nothing at all,
  which is also how an abbreviation with its points meets one without;
- a roman numeral or a spelled-out one for an arabic one — `Part II`, `Part
  Two` and `Part 2` are one number — and the word that only *labels* a number
  dropped beside it, so `The Hills Beyond 2` meets `The Hills Beyond Part 2`
  and `Falkenauge 2. Teil`;
- a trailing `1` dropped altogether — the first of a series is written
  `Nordwind I`, `Nordwind 1` and `Nordwind` and meant identically. Only the
  first at the end: a trailing `2` is the sequel. A number with title on
  **both** sides of it may go at any value — `Nordwind 2: Der Sturm` is also
  written `Nordwind Der Sturm`, and what surrounds the number still says which
  film it is;
- the name a sequel has of its own, which nobody says out loud: `Missing Since
  2` is the film a catalogue holds as `Missing Since 2: The Beginning`, and a
  catalogue that runs a market's title on after the film's own is the same
  thing from the other side. Only after a series **number**, and only where a
  separator introduces it — a title with no number in it is only its name, and
  what follows one with no introduction is what a library writes after a title:
  a source, a resolution, a language, an extension. A year in what is cut comes
  back with the title, so `Falkenauge 2 (1985)` and `Falkenauge 2 (1999)` stay
  two films;
- a possessive `'s` that one side has and the other does not;
- an article gone from anywhere in the name, or several of them, or moved to
  the end the way a catalogue that sorts by the first real word moves it;
- `&` written out, in any of the Latin-script languages the table knows;
- upper case, lower case, or any mixture;
- a letter from the wrong keyboard — a Cyrillic or Greek letter that is the
  same *shape* as a Latin one reads as the Latin one, so a name that looks
  identical on screen is treated as identical.

### What a name carries besides the title

**What kind of thing the file is.** A `Movie`, `Film` or `Documentary` written in
front of a title, into the middle of it or on to the end of it says the same
nothing in each position, so `Quiet Harbour Documentary` is the film
catalogued as `Quiet Harbour`. In front of the title it comes off on sight —
nothing standing before the title is the title; further in it has to have a
title in front of it to be a label at all.

**Two titles at once.** A folder called `Falcons Forever aka Nordwind Rising
(1998)`, where whoever named it could not choose between the film's names and
wrote both. Nothing was ever released under that, so it is taken apart at the
`aka` — in any of the spellings a keyboard makes it, `AKA`, `a.k.a.`, `a/k/a`,
`also known as`, and the bracket a library holds the second name at arm's length
in — and the titles it holds are asked about **left to right**, the order they
were written in, until one answers. Whichever does names the film, and the
folder is written under that one alone. Only where the marker plainly holds two
titles apart: `Nakamura Lane` keeps its name, and so does a film actually called
`Aka Ruri`.

### Asking again

A film nothing is found under is looked for again under the spellings a library
adds and a catalogue does not — a "Movie"/"Special" in front of the name, the
"Documentary" written into it, a franchise repeated on every film in it, its
numerals as figures. And under the one it *leaves out*: a library that keeps a
franchise in a folder of its own writes half the title on each, so the folder
above is read as the first half and the two together are asked about as one
title. A tail that is only a number is never asked about on its own — a
catalogue asked for `Part 2` answers with every film ever released in halves.

**Where TMDb cannot name a film, `-O tagging` asks a local copy of IMDb's own title
lists** — the romanisations its search does not find, and the films it states
no runtime for, which is what a folder with no year is settled on. The lists
are fetched into `data/imdb` in the medialib data directory the first time
(about 750 MB),
refreshed when they are a month old, and read with `duckdb`; without it the
pass runs on TMDb alone. IMDb offers them for personal, non-commercial use
only. A full ingest never asks them.

Only the first handful of TMDb's results are read in full: those already
carrying the folder's title first, then the rest by how near their names are to
it, popularity only breaking a tie.

### Choosing between candidates

Before the length is asked, three narrowings settle most cases:

- **A candidate with no IMDb id is set aside.**
- **A title as written beats one reached through a reading, and a film's own
  name beats one of the names it is also known by** — a folder called `1815`
  is not left unnamed because some other film also answers to it through a
  dropped article.
- **A re-release does not answer for the film it was timed to** — where another
  candidate was *first* released around the wanted year, the older one is set
  aside.

All three narrow and never widen: where every candidate is equally strong — or
equally useless — they all stand and the length decides.

**Then the film's own length decides.** Where that leaves several candidates or
none, the folder is tagged only if exactly one candidate's runtime fits what is
on the disk and every other is ruled out, which is also what lets a folder whose
year is off by one be named at all. A folder of named editions offers no length
— which of the cuts the catalogue's one runtime is for is exactly what is not
known — so it falls back to the year alone.

### How the name is written

Once a film is named, **TMDb's own spelling of whichever title matched is what
the folder and every file in it are written under** — including a film found
only by reading the folder above it as the first half of its title, which is
then written out in full — the one spelling of the several that is known to be
right. Whichever title matched, and not the catalogue's primary one: a French
film found under its French name stays French, because the English title it is
also catalogued under folds to something the folder never said.

That is also what settles most of the folders no id can otherwise reach:

- A file whose name says the folder's film in another hand is respelled onto the
  folder's own rather than reported as a second film.
- A `Part 1` written with the number held off becomes the `Part1` Plex stacks on.
- A `(1)`, `(copy)` or `- Copy` that a file manager left is taken off, unless
  doing so would put two files under one name, which is a real duplicate and is
  still reported.

### Folders whose names cannot agree

Where the names genuinely cannot be made to agree — a second feature in the
folder, parts that each have a title of their own — **the id still goes on**,
provided every id already written in the folder is the same id. Nothing else
moves. One id that disagrees stops it: two ids in a folder is how a sequel ends
up filed under the film before it, and that is a mistake to be shown rather than
a spelling to be tidied.

**The same film named in another language.** One kind of disagreement the names
themselves can never settle: a folder whose files are the same film named in
another language. `Die Stille Insel` and `L'Isola Quieta` share not a syllable with
each other, and TMDb lists both as titles of the film the folder matched. Where every file in a folder is accounted for
that way, the folder takes TMDb's spelling and the id, and **every file keeps
its own name** — which of a film's languages a file is named in is a thing you
chose, and a lookup is no reason to overwrite it. All that is added is the id,
so Plex knows they are one film. One file the catalogue cannot account for and
the folder is left alone: a second feature is still a second feature.

Nothing is renamed for those folders, so no other list would mention them — they
get one of their own, `ingest-movies-othertitles-<folder>.txt`, naming each file
and the catalogued title it answered to. It is written on real runs as well as
dry ones, because it records what *happened*. Such a folder **stays on the
lists** either way, marked `id applied, names still to settle`. The id is no
longer missing; the names still are, and whether they really could not be
settled is exactly the thing worth looking at — an id going on quietly is what
would keep the folder off the list it belongs on.

**A language kept as an edition** is the other half of that. A library that
cannot mux every language into one file keeps one file per language and writes
the language after the year. Once the catalogue confirms the film, that suffix
becomes an `{edition-}` tag and the title in front of it takes the folder's
spelling — Plex then collapses them into one film with a named picker, and the
language survives in the tag. A file whose title is the *only* thing
distinguishing it keeps its name, because there is no tag for the language to
survive in.

**When nothing answers to the folder's own name, its files are asked under
theirs**: a library holding a film under two languages often names the folder in
a third, and the files are then the only names a catalogue has ever heard of.
Every film in the folder must answer, and all of them to the same id — a folder
whose films answer to two ids is a folder holding two films.

### The lists tagging writes

Each folder given gets these lists, in `logs/ingest-movies/` in the medialib
data directory:

| List | What is in it | Written |
|---|---|---|
| `ingest-movies-unmatched-<folder>.tsv` | films TMDb could not name — fill in their ids and feed the file back with `-i` | always |
| `ingest-movies-ambiguous-<folder>.txt` | folders holding more than one film, which no id can settle — give the files names that say which release each one is | always |
| `ingest-movies-othertitles-<folder>.txt` | folders holding one film under several of its titles ([above](#folders-whose-names-cannot-agree)) | always |
| `ingest-movies-conflicts-<folder>.txt` | renames held back because the name was already taken — most often by the same film, tagged before | always, `-i` too |
| `ingest-movies-renames-<folder>.txt` | every rename the run would make | dry run only |
| `ingest-movies-nearmisses-<folder>.txt` | for each folder in the first two: what TMDb was asked, what it offered, and why each offer was refused | dry run only |

**A list an earlier run left is not replaced without asking**, for
`-O subtitles`, `-O names` and `-O commentary` too: see
[overwriting a report](../output.md#overwriting-a-report).

A folder is on the unmatched list or the ambiguous one, never both.

**A film kept twice is warned about.** When two separate folders carry the
same id, whether it was already on them or the run gives it to them, `-O tagging`
lists both in `ingest-movies-duplicates.txt` — one file for the whole run, since
they may be side by side, in different corners of the library, or in two of the
folders given (two disks, say). That is nearly always one film kept twice by accident, so which copy to
keep is left to you. The several files inside one folder are its versions and
are never counted.

## Feeding ids back (`-i`)

A line of the unmatched list, filled in:

```
Your Film (1975)	tt0000002
```

An id is read however you have it to hand (`tt0000002`, `imdb-tt0000002`, a bare
TMDb number), answers before the network is asked, and stays in the file so the
lookup you did by hand is not lost.

Feeding that list back is **`-O tagging -i <file>`**, the same tagging phase going on what you
filled in — and a **dry run** too, unless `-w` is given. It prints what your ids
would rename and writes neither `-O tagging`'s worklists nor the file you are still
filling in. Only the conflicts and duplicates lists are left, since they record
what the run found rather than asking you anything. `-wO tagging -i` carries the renames out and brings
the file up to date.

| Row in the file | What happens |
| --- | --- |
| an id you filled in | **used, not checked.** TMDb is not asked to agree with a lookup you did by hand, because it is the one that failed to name the film in the first place |
| a row left **blank** | not asked about again either, for that same reason — the run says how many there were, once, at the end, and nothing per film |
| a row that names no folder in the library | an error naming that row, and the run carries on to the next |
| a file that **cannot be read** | **stops the run**: counted as no ids at all, a path typed wrong would throw away every id in the real file and put the whole library back through the lookups that already failed on it |

## Subtitles

**`-O subtitles` does only the subtitles.** Every `<movie>.xx.srt` already beside a film
is put to the test a downloaded subtitle is kept by: its best alignment with the
film's speech has to stand out from every other. Commentary transcripts are
never tested — they are named after their track — and neither are forced
subtitles or the languages the `spokenLanguages` setting does not name.

| | Dry run (`-O subtitles`) | `-wO subtitles` |
| --- | --- | --- |
| subtitle in step | said to be kept | adverts taken out, synced |
| subtitle out of step | listed in `logs/ingest-movies/ingest-movies-subtitles-<folder>.txt` | deleted |
| subtitle missing | — | downloaded, synced and tested, exactly as a full ingest does — so one thrown out is fetched again in the same run |
| subtitle ffsubsync cannot align at all | left alone | left alone, since it may be one that cannot be fetched again |

A dry run tests each subtitle on a copy, its adverts taken out first, and says
how many advert cues the real run would remove.

With `-wO subtitles` each film's missing subtitles are downloaded before the run moves on to
the next film. Under the film, one line per subtitle says what it was aligned
to, by how much, and what became of it:

```
==> [3/120] The Movie (1999)/The Movie (1999).mkv
==>   English: in step with the film's subtitle track (shifted +1.2 s, confidence 4.81) - synced and kept
==>   German: out of step with the audio (confidence 0.42) - thrown out
==>   German: none found to download
==>   French: 2 advert cues removed
==>   French: downloaded from OpenSubtitles.org, in step with the audio (shifted -0.4 s, confidence 2.10) - synced and kept
```

**Films already done are skipped.** Testing and syncing a whole library takes
hours, so every film `-wO subtitles` gets through is recorded by its id tag in
`logs/ingest-movies/ingest-movies-subtitles-done.txt` as soon as it is
finished, and every later `-O subtitles` or `-wO subtitles` run skips the films recorded there,
saying how many it skipped. A run that is stopped halfway keeps every film it
finished. A film is known by its `{imdb-…}` or `{tmdb-…}` tag, plus its
edition and part when it has them, so moving or renaming it does not undo the
record. Each line also records the languages its run got through, and a later
run skips a film only when that list holds every language it wants — a
language added to the `spokenLanguages` setting is fetched for a film once,
the first run after the change, and never again. These films are never recorded:

- a film with no id tag, since its name is only a guess at which film it is
- a film with a subtitle ffsubsync could not align at all
- a film the run was interrupted on
- any film of a `-wO subtitles` run made with no subtitle source's login set, since
  nothing was downloaded

To have a film done again, delete its line from the file. To redo every film,
delete the file.

## Commentary transcripts

**`-O commentary` does only the audio commentary phase.** Every commentary track that does
not already have its transcript beside the film is transcribed next to it — in
the language it is spoken in, on the GPU when whisper can use it (the largest
model the free memory fits), on every CPU thread with `base.en` otherwise — and
nothing else: no subtitle downloading, no renaming of
titles, no conversion, no remux, no tagging. The transcripts stay beside the
films rather than muxed into them, and a rerun over the same library finds every
commentary already transcribed and does nothing. A film whose name lacks the dot
before its `.mkv` is first renamed to its folder's spelling; one that cannot be
is left untranscribed and reported at the end.

A transcript already beside a film is left alone, with two exceptions:

- **Too small to be real.** A real transcript runs to the order of a kilobyte
  per minute of film, and a sidecar under half of that is the one an older run
  wrote forcing a non-English commentary through the English model. That one is
  discarded and the commentary is transcribed for real.
- **Numbered for a track that has moved.** A transcript an older run wrote
  numbered for the track it then stood under, when the film's tracks have since
  moved and that number no longer names this commentary, is renumbered to the
  track's current number rather than transcribed again — where the rest of the
  name still says which commentary it is. The srt itself is left untouched, and
  the renumbered sidecar is judged by the size rule like any other.

## Commentary names

**`-O names` names numbered commentary tracks.** A rip often calls its commentaries
`Commentary 1`, `Commentary 2` — the disc said which was which only in its
menu. `-O names` looks the film up on [dvdcompare.net](https://www.dvdcompare.net),
which lists every release's commentaries by who speaks in them, and gives each
track its real name: `Commentary by director …`. Only a film whose every
commentary is still merely numbered is touched — in any of the languages the
table knows, and spelled a letter wrong or not (`Kommentar 2`,
`Audiosommentary 3`) — and
nothing else is done. It is a **dry run** unless `-w` is given.

It names a film only when it is sure:

- The **picture size says which kind of disc** the file came from — 4K,
  Blu-ray or DVD — and only that kind's releases count (a Blu-ray is also looked
  for among the Blu-ray discs of the 4K sets).
- The file has **exactly as many commentaries** as such a release lists, and
  every release that lists that many agrees on what they are.
- For **two or more**, a listing's order is its menu's and says nothing about
  the order of the disc's audio, so **the transcripts beside the film decide
  which track is which**: commentators nearly always open by saying who they
  are, and each track's opening minutes have to introduce one listed
  commentary's people and nobody else's, in one way of pairing them only. One
  commentary whose people never introduce themselves may take the track that
  is left over. Run `-O commentary` first, so the transcripts are there.

The disc's own subtitles for a commentary (`English (Commentary #2)`), the
transcripts the ingest appended to the film, and the transcripts and extracts
beside it (`… 2 Commentary 1.en.srt`) are renamed along with the track. Every
folder given leaves `logs/ingest-movies/ingest-movies-commentarynames-<folder>.txt`: what was
named — or would be — and every film left alone, with the reason. The pages
asked for are kept in `data/dvdcompare` in the medialib data directory. The site is asked about
five seconds apart, give or take two, and after it fails to answer once it isn't
asked again that run. With `-w`, each film is renamed while the next one is
being looked up, so the lookup never waits on a rename.

The full ingest does this too, and writes: after the transcription, which the
transcripts come from, and after the tagging, so each film is looked up by the
title TMDb gave its folder, and the same list is written.

## Chapters

**`-O chapters` does only the chapters.** Every tagged film under the given folders that
has no chapters, or only numbered ones (`Chapter 01`, `Kapitel 3`, a bare
number or time), is looked up in the read-only
[ChapterDB archive](https://chapterdb.plex.tv/): chapter times read off DVDs and
Blu-rays, mostly with the names from the disc's menu. The full ingest does the
same lookup near its end, after the tagging, since a film is looked up by the
title its folder now carries — an untagged folder is never looked up.

**A set must be the disc the film was ripped from.** It has to run as long as
the film to within **two seconds**, which rules out every other cut and every
PAL transfer. It also has to have at least three chapters, start at the start,
be in order, and reach past the middle of the film. Of the sets that fit:

| The film has | What it gets |
| --- | --- |
| no chapters | the best named set, or a numbered one when that is all there is |
| numbered chapters | **replaced** only by a named set with the same number of chapters, each starting within a second of the film's own: the same marks, now with names. Numbered chapters at other times — a different authoring, or marks a muxer put every five minutes — are kept, since there is no point dropping marks the film already has |
| named chapters | not looked up at all |

Among sets that fit, English names win, then the set more people confirmed.

**Written in place, never remuxed.** `mkvpropedit --chapters` replaces every
chapter the film had, so it never ends up carrying two sets. It writes as it
goes: there is no dry run, and no `-w`. Each film's set is written while the
next film is looked up, so the lookup never waits on a write. The archive is
asked about three seconds apart, give or take one. The archive takes no new
sets, so a recent film will not be in it; if it stops answering, the rest of
the run does without it. Needs `curl`.

## Plex comparison

**Is the film already in the library, and which copy is better?** The full
ingest asks this last, once each film is the file it will stay; `-O plex` asks it
alone, of every film under the folders given. The server is `plexUrl`, or else
the one on this machine; the library is read once per run. A film is found by
its id tag — Plex keeps the IMDb and TMDb ids of every film it matched — or, for
one the tagging could not name, by its title and year. A copy in the film's own
folder is the film itself, or a version kept beside it on purpose, and does not
count.

**What the library's copy has and the film lacks is carried over** by the full
ingest, into its one remux, before the film is written: the names of
commentaries the film only numbers (as many, in the same languages), named
chapters where it has numbered ones or none (moved to its timing), and the
audio it lacks — a soundtrack in a language it keeps, the commentaries with
their transcripts, an isolated score or an audio description — at the
measured offset. A lossless track goes to the target codec, put to the film's
speed where the two differ; a lossy one at another speed is left. The picture,
the subtitles and a better soundtrack in a language the film already has are
never carried: those are left for you to decide. `-o plex` carries nothing.

A film not in the library says so. One that is gets the library's file printed,
so you do not have to search for it, and a verdict. The closing count lists
every such film again with its library copy beside it, MIXED first.

| Verdict | When |
| --- | --- |
| the new file is better | it wins on something and loses on nothing |
| the existing file is better | the other way round |
| equivalent | neither wins on anything |
| **MIXED** | each has something the other lacks — these want you, and the run lists them first at its end, with what could not be carried over and why |
| **different version** | the edition tags differ, or the lengths differ by a minute or more (a 4% difference is called out as a PAL speed-up) |

What is weighed: the resolution (2160p, 1080p, 720p, SD — by width as much as
height, so a scope film's 1920×800 is 1080p), the HDR (Dolby Vision over HDR10+
over HDR10 over SDR), the soundtrack of each language (channels, then object
audio, then lossless), a language only one of them has, the number of
commentaries, the other bonus audio by kind (isolated score, music and effects,
audio description, interview) and the chapters (named over numbered over
none). **Subtitles are not weighed.** Whatever either file wins is listed
under the verdict.

**A few seconds apart is the same cut.** A length difference under a minute is
most often a studio logo more or less in front of the film. The offset the
library file's tracks then need is measured on one soundtrack both share, at
five points of the film: the loudness of the two is matched first, which
survives a different mix or codec, and then the waveform places it to the
millisecond. Offsets that lie on one straight line, to within 40 ms, are one
cut — a logo more or less in front, or another speed (24 against 23.976
frames a second, or PAL) — whatever the lengths; ones that do not mean the
cuts differ inside the film, and are printed as a warning. The offset is measured whenever
the existing file has something worth carrying over, or the lengths differ by
half a second or more. Needs `curl`, `mkvmerge`, `mediainfo`, `ffprobe`,
`ffmpeg`, and numpy for the offset.

`-O plex` changes nothing, on disk or on the server.

## Chaptering the extras (`-C`)

**`-C` adds the extras to a full ingest.** Every bonus video — anything under
a Plex bonus folder (`Featurettes`, `Deleted Scenes`, `Interviews`, …),
never a film's main movie — that has no chapters is given some, worked out by
the language model the `llm` [settings](../settings.md) name, the way
[`generate-chapters`](generate-chapters.md) does it. An extra that has chapters
keeps them, and one too short to divide is left alone. This is separate from
the [ChapterDB lookup](#chapters), which is only for films.

What is said is read from the extra's dialogue subtitle, or else transcribed
with whisper, chosen the way [`convert-video -C`](convert-video.md#chapters--c)
chooses.

The chapters are written in place with `mkvpropedit`, so the extra is not
remuxed. That cannot add a subtitle track, so a transcript whisper made is used
for the chapters and then discarded.

**It runs in the background, beside the rest of the run.** Extras are done one
at a time. An extra with a dialogue subtitle is chaptered from the start,
beside the transcode and the commentary transcription. An extra that has to be
transcribed waits until the commentary transcription is finished, and is then
transcribed while the remux goes on. All of it is finished before the tagging
renames anything.

**The GPU is shared only where it has room.** A model served from this machine
or its network (a `llmUrl` that is local) runs beside the commentary
transcription only when nvidia-smi shows free memory for it. Set the
`llmVramGB` [setting](../settings.md) to what it needs; unset, the
model takes the whole card and never runs beside whisper. A server the run
starts itself (`llmServe`, see
[generate-chapters](generate-chapters.md#several-servers-one-card)) is started
only with that room; without it, the next server listed is asked. A hosted
model, or a machine with no NVIDIA card, never waits. The progress row shows
it as `chapters`. The model is asked for up front, so a run whose model is not
set up is refused before any ingesting. Needs the settings `llm*` and,
for a transcribed extra, the same whisper set-up as the commentaries.

`-P "<text>"` adds your own instructions to every request the model gets, as
[`generate-chapters -P`](generate-chapters.md#your-own-instructions) does. It
needs `-C` or `-O extras-chapters`.

## Remux conversion (`-r`)

**A full ingest with `-r` converts the remuxes as its last step.** A film counts
as a remux when any of these holds:

| Test | Read from |
| --- | --- |
| its name, or a folder above it, contains `remux` | the name it arrived under, before the cleaning removes the word |
| 2160p HEVC with dual-layer Dolby Vision profile 7, at a generous bitrate (twice what [`convert-video -t`](convert-video.md#only-what-has-room-to-save--t) counts as adequate) | the original kept as `(old)`, because the remux has turned profile 7 into 8.1 by then |
| any other 2160p HEVC — HDR10 alone, or profile 8.1 with no `(old)` copy left — at a very generous bitrate (four times adequate: about 45 Mbit/s for 16:9, above what web releases spend) | the same |
| SDR 1080p H.264 at a very generous bitrate (four times adequate: about 20 Mbit/s for 16:9) | the same |

The remuxes of a folder go to **one** `convert-video -k` run, with convert-video's
default video settings. Each finished video is combined with the audio,
subtitles, attachments, chapters and tags of the ingested film, unchanged, and the
result is written beside the film:

    Film (2020) {imdb-tt0000000}.mkv
    Film (2020) {imdb-tt0000000} {edition-AV1}.mkv     <- the converted copy

The copy is named as an [edition](#the-plex-names), written the way the server
writes editions, so it shows the two files as one film with a version picker.
A film that already has an edition gets `AV1` added to it
(`{edition-Director's Cut AV1}`), and a split film's part token stays
last, so the converted parts stack.

The film itself is not changed. Later phases and later runs skip the copy in the
same way they skip the `(old)` original: it is not tagged, transcribed, remuxed or
fetched subtitles for, and a later `-O tagging` rename does not rename it. Converting the
same remux again is left to convert-video: a copy as long as its film is skipped,
and an unfinished one is redone.

## Dropping the `(old)` backups

**`-O backups` drops the `(old)` originals the improved copies were kept beside, and
nothing else.** It is the end of the remux's work: a film whose original still
stands beside it as `<name> (old).mkv` is asked about its copy, and the backup
goes where the copy is the whole of the original.

The copy is asked three things:

| Check | Standard |
| --- | --- |
| the same length as the original | the library's own length measure — the one the ingest applied when it accepted the copy |
| a video stream | the copy is the film, not a soundtrack that lost its picture |
| at least the tracks the rules keep | the rules' own answer for the original's tracks — the same decision the copy was made by. A copy the rules would not have made is not the one that stands in for the original |

**A dry run unless `-w` is given.** It says which original would be dropped and
which would stay, with the reason, and changes nothing on disk. The ones kept
are listed per folder, one per line under a heading that says why:

    logs/ingest-movies/ingest-movies-backups-<folder>.txt

With `-w` the copy that passes is tagged first — `MEDIALIB_INGESTED`, written
with mkvpropedit the way the tagging writes and the file's time put back — and
only then is its original dropped. A copy the tag cannot be written on keeps
its original: the tag is what would stand in for the backup, and the backup is
not dropped in favour of nothing.

The tag is what tells a later run the film is done: the "already remuxed" check
accepts either the `(old)` backup or the tag in its place, so a film whose
backup has been dropped is not remuxed a second time.

## Keeping everything (`-k`)

**A full ingest with `-k` takes nothing away.** The `-k` is given in parts -
`audio`, `tracks` and `dv`, comma-separated - and a bare `-k` keeps all three.
`audio` encodes nothing of what it carries: lossless audio stays lossless
rather than going to the target codec, and a track of the library's copy that
would need
encoding (put to another speed) is not carried. `tracks` drops nothing: no
audio or subtitle track is dropped - not another language, not an image
subtitle, not an audio description, not the lesser of two surround tiers - and
what is carried over from the Plex library's copy is its soundtracks in every
language, lossless as they are. `dv` keeps the enhancement layer of a
dual-layer Dolby Vision profile 7. A part settles only its own measure, so
`-k audio` still drops the excessive languages, and `-k tracks` still converts
the lossless audio to the target codec. Everything that only adds or corrects still
happens with every part: the cleaning, renaming and IMDb tagging, the track
names and flags, the subtitles downloaded and the commentary transcripts muxed
in, the chapters, a false Dolby Vision claim or an overstated level corrected.

The word after `-k` is claimed by `-k` when it is a part: a folder literally
named `audio` is written `./audio`. `-k` is refused with `-r`, which encodes.

## Leaving phases out (`-o`)

**A full ingest with `-o` does everything except the phase it names.** The
phases are `junk` (the release junk is left where it is), `subtitles` (nothing
is fetched and the missing subtitles are not downloaded), `ocr` (the picture
subtitles are not read into text), `roles` (the subtitles are not measured for
being forced or SDH, and their flags stay as the file has them), `commentary`
(commentaries are not listened for, transcribed or named), `chapters` (the
chapter archive is not asked), `plex` (the films are not compared with the Plex
library and nothing is carried over from its copy) and `tagging` (the films are
not tagged with their IMDb ids). A bare `-o` leaves them all out.

The phases are comma-separated, in any order, and a phase the run will not omit
is refused with the page, like a bad option. A folder literally named after one
of them — `plex` is the likeliest — is written `./plex`: the word after `-o`
goes to `-o` when it is a phase. Omitting `tagging` is the one with a
downstream effect: the later phases then see untagged folders.

## Single-task sweeps, chained (`-O`)

**`-O <phases>` runs single-task sweeps by name**, in the order typed, as if each
had been typed as its own command on the same folders. The phases are
`layout`, `roles`, `ocr`, `tagging`, `subtitles`, `commentary`, `names`,
`chapters`, `extras-chapters`, `plex` and `backups`, comma-separated like `-o` and
`-k`; the [sweeps table](#single-task-sweeps) says what each does:

```bash
ingest-movies -O tagging ~/Films
ingest-movies -wO tagging,subtitles ~/Films       # tag, then the subtitles
ingest-movies -O commentary,names,chapters ~/Films
ingest-movies -O layout,ocr,roles ~/Films
ingest-movies -O extras-chapters -P "a chapter per scene" ~/Films
```

Nothing is reordered or checked for sense, so the order is the one the
[sweeps table](#single-task-sweeps) gives. A phase that fails stops the chain.
`-i <file>` is the id list the `tagging` phase reads back.
`-O` is refused with `-r`, `-C`, `-k` and `-o`, which belong to a full ingest.

## Several folders

**Several folders may be given**, and each is worked through in full before the
next is started, in the order they were typed. Each leaves lists of its own,
named after it — `ingest-movies-unmatched-Films.tsv`,
`ingest-movies-ambiguous-Films.txt` — so two libraries' worklists never land in
one file. An `-i` list you named yourself is the exception: it is read for every
folder and, under `-wO tagging -i`, written back once, holding them all. Two folders of the
same name are refused before anything is touched, since their lists would be
written to one path.

---

**Options:** `ingest-movies -h` lists every option and its default. **Shared
rules:** [file safety](../file-safety.md) ·
[stopping a run](../file-safety.md#stopping-a-run) ·
[missing tools](../requirements.md#missing-tools-are-refused-up-front) ·
[which ffmpeg](../requirements.md#which-ffmpeg-a-run-uses) ·
[what leaves the machine](../file-safety.md#what-leaves-the-machine) ·
[what a run prints](../output.md) ·
[settings](../settings.md)
