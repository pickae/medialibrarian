# `ytdlp`

Downloads the audio of a list of podcast feeds into a library laid out for a
phone, remembering what it has already fetched so a re-run only picks up what is
new. Sponsor segments are cut out, a thumbnail and metadata are embedded, and
Opus is preferred over m4a.

    ytdlp [options] <outputPath> <archiveFile> [<dateRange>]
    ytdlp -l <archiveFile> <archiveFile>...

| At a glance | |
| --- | --- |
| **Takes** | one or more feed tables (`data/podcasts/*.tsv`) |
| **Writes** | the episodes under `<outputPath>`, and what was fetched into `<archiveFile>` |
| **Reruns** | fetch only what is new: the archive says what is already there |
| **Network** | yes — the feeds themselves |

## Default behavior

With no options, a run:

- **updates a nightly yt-dlp first** (`-p`)
- reads the feeds in `data/podcasts/podcasts.tsv` (`-t`), skipping those marked inactive (`-a`)
- downloads audio only with the `youtubeAudio` profile: **sponsor segments cut out**, Opus preferred, thumbnail and metadata embedded ([feed tables](#feed-tables))
- walks back at most 20 entries per feed, and names episodes `<upload date> <title>` (table columns)
- does not tidy up or build the phone's library afterwards (`-c`, `-i`)

**Contents:** [Default behavior](#default-behavior) · [Arguments](#arguments) · [Feed tables](#feed-tables) ·
[One table, both systems](#one-table-both-systems) ·
[Options that shape a run](#options-that-shape-a-run) ·
[Tidying up](#tidying-up-after-a-run--c) · [The phone's library](#building-the-phones-library-from-the-run--i) ·
[Merging archives](#merging-archives--l) · [What a run prints](#what-a-run-prints) ·
[Being refused](#being-refused)

## Arguments

The three arguments are the things that change between runs; everything else
lives in the tables.

| Argument | What it is |
| --- | --- |
| `<outputPath>` | where the library goes — or, with `-i`, the parent of the staging and library trees |
| `<archiveFile>` | the "already have it" record. A bare name is kept in the script directory's `logs/` folder — the home of the records a run keeps about itself, and of `ingest-music`'s `beets.log` — and read back from there on the next run; a path is taken as given |
| `<dateRange>` | optional upload-date filter (below) |

| `<dateRange>` | Fetches |
| --- | --- |
| `20260607` | everything since that day |
| `20260607..20260707` | a window |
| `..20260707` | everything up to that day |
| `today-2weeks` | relative dates work at either end |
| `..today-10days` | a rolling window: nothing newer than ten days |

## Feed tables

A table holds one row per podcast — its folder, its file-name template, how many
entries back to walk (`0` for the whole feed), any arguments it alone needs, and
its URL — and the argument sets every feed shares live once.

The tables are one machine's library rather than code, so `data/` is not tracked
and there is a sample to start from instead —
[`medialib/config/podcasts.example.tsv`](../../medialib/config/podcasts.example.tsv), a working table
whose header documents the columns and the directives:

    cp medialib/config/podcasts.example.tsv data/podcasts/podcasts.tsv

A podcast is paused by putting a `0` in its `active` column, not by commenting
its row out — so it still shows up as a podcast, in a `grep` and in the count.

### Profiles

`-t` may be given more than once, and each table says what it is with a
`#!profile` line:

| Profile | Fetches | At once |
| --- | --- | --- |
| `youtubeAudio` (default) | audio, sponsor reads cut out | one feed |
| `youtubeVideo` | video into Matroska, sponsor segments marked as chapters | one feed |
| `rssAudio` | audio, whatever the enclosure offers | ten feeds |
| `rssVideo` | video, likewise | ten feeds |
| `siteVideo` | a site that is neither: no SponsorBlock, no format ids, just the page's video | one page |

A `#!jobs <n>` line overrides a table's width and `-j` caps every table's
(downwards only). What may run alongside what is decided by the **provider**, not
by the table: two YouTube tables are one provider being asked for twice as much
at once, which is what gets a client throttled, so however many are given they
queue behind one another — while an RSS table is dozens of unrelated providers
who cannot see each other, so it runs alongside without waiting.

### Which tables there are

One per thing that would otherwise have to be passed differently — the output
root, the download archive, or what the feeds are fetched as. Several may be
given to one run with `-t`, as long as they share that run's output root and
archive.

| Table | Holds |
| --- | --- |
| `podcasts.tsv` | the phone's YouTube feeds |
| `podcastsRss.tsv` | the phone's RSS feeds |
| `youtubeAudio.tsv` | the desktop audio library |
| `youtubeAudioSecondArchive.tsv` | the same feeds, with an archive of its own |
| `youtubeVideo.tsv` | the video library |
| `siteVideo.tsv` | one site walked page by page |
| `youtubeAudioPaused.tsv` | the feeds that are switched off |

## One table, both systems

A row does not say how yt-dlp is called, because that is what differs between
the two systems and is worked out at run time:

| Settled at run time | How | Override |
| --- | --- | --- |
| **which yt-dlp** | a `yt-dlp.exe` beside the checkout, one on `PATH`, or an importable `yt_dlp` module — which covers the apt, pip and pipx installs, and the single downloaded binary. On Linux a `yt-dlp-nightly` is taken ahead of the release one. The winner is printed, because "it works on the other machine" usually starts there | `YTDLP` |
| **which nightly** | a nightly winner is brought up to date before anything is fetched, through the install's own upgrade path | `SKIP_YTDLP_UPGRADE=1`; a preview (`-p`) never upgrades |
| **which ffmpeg** | the [ladder](../requirements.md#which-ffmpeg-a-run-uses) is walked past any build that will not run, and the winner named to yt-dlp | a feed's own `--ffmpeg-location` |
| **which paths** | under Git Bash or Cygwin the output and archive paths are translated to their drive-letter form, since a native `yt-dlp.exe` does not know where the emulated root is mounted | — |
| **which quoting** | `-p` prints the calls instead of running them, quoted for the shell of the host it is printed on | `-s windows` / `-s linux` prints the *other* machine's calls from the same table |

- **Why a nightly.** An extractor a site broke is fixed in a nightly weeks before
  it is in a release (`pipx install --suffix=-nightly --pip-args=--pre yt-dlp`
  puts one there), and a fix that landed this morning is no use to a build from
  last month. The upgrade is pipx for a pipx venv and `-U --update-to nightly`
  for a downloaded binary; a pip or distro install is left to its package
  manager, and a failed upgrade is a warning rather than the end of the run.
- **Why the ffmpeg is named.** yt-dlp does its own muxing through ffmpeg —
  extracting the audio, merging the two streams into Matroska, embedding the
  thumbnail and the chapters — and finds one by searching `PATH` itself. On a
  machine whose only working ffmpeg is a hand-installed build under
  `/opt/ffmpeg` or `~/.local/bin`, naming it is the difference between the
  muxing working and not. A feed's own `--ffmpeg-location` still wins, since
  yt-dlp keeps the last one it is given.

## Options that shape a run

| Option | Effect |
| --- | --- |
| `-t <table>` | a table to read; may be given more than once |
| `-j <jobs>` | caps how many feeds any one table fetches at once (downwards only) |
| `-a` | runs the paused feeds (`active` = `0`) as well |
| `-m <text>` | narrows the run to the feeds whose folder or URL matches |
| `-p` | prints the yt-dlp calls and downloads nothing |
| `-s windows\|linux` | with `-p`, prints the other machine's calls |
| `-v` | puts yt-dlp's own output back |
| [`-c`](#tidying-up-after-a-run--c) | tidies up after the run |
| [`-i`](#building-the-phones-library-from-the-run--i) | builds the phone's library from the run |
| [`-l`](#merging-archives--l) | merges archives; downloads nothing |

### Tidying up after a run (`-c`)

A yt-dlp run does not leave one file per episode: it leaves the thumbnail it
embedded, any description, metadata json or subtitle files a row's `extraArgs`
asked for, and — if it was interrupted — the `.part` of a fragment. With `-c` the
run clears that up afterwards:

- the sidecars of the episodes **it** downloaded are removed (for video, the
  description and the metadata json are first *attached into* the Matroska,
  since they are the parts of a video that disappear with it);
- anything that is not Matroska is remuxed into one;
- the leavings of interrupted downloads are swept from the output root;
- folders left empty are pruned.

It works from the run's own manifests rather than by scanning the library, which
is what lets it tell a leftover thumbnail from a folder's cover art.

### Building the phone's library from the run (`-i`)

What comes off the network and what the phone plays are not the same file — the
download is the best the feed offers, the library copy is that re-encoded small
enough to carry around — so with `-i` they are not the same tree either.
`<outputPath>` becomes the parent of both:

    phone/Staging/Speech/…       the raw downloads, kept
    phone/Staging/Music/…
    phone/Ingested/…             the library, built out of both

When the run is over, each staging folder's names are cleaned
(`clean-folder-structure`) and its episodes converted into that one library:

| Staging folder | Converted with |
| --- | --- |
| podcast RSS | `convert-audio -c -m` |
| music | `convert-audio -c -b 65` |

There is one staging folder per **conversion** and not per table or profile,
because the converter is handed a folder rather than a file — and they all end up
in the same library, so the split is invisible in the result. The raw downloads
are kept: they are the only copy of what was published, and the archive file says
they will not be fetched again, so changing one's mind about a bitrate is a
re-run of the last step rather than of the whole night. A video table ignores
`-i` and downloads to `<outputPath>` as usual, since an audio converter would
throw its picture away. Normally given together with `-c`, so the embedded
thumbnail is gone before the library is built out of the folder it was sitting
in.

### Merging archives (`-l`)

When the same tables are run on more than one computer, each machine's archive
`.log` drifts apart, and each would re-fetch what the other already has. `-l`
takes two or more `.log` files and nothing else, and writes every line of all of
them — sorted, duplicates dropped, Windows line endings folded in — into the
first, creating it if it is not there. Nothing is downloaded.

## What a run prints

One line per episode, and nothing else; `-v` puts yt-dlp's own output back for
when something needs diagnosing. The episodes are counted from what yt-dlp
reports after all post-processing, so the closing figures are of finished files
on disk rather than of feeds walked.

The tables, the date range and the tools are all checked before anything is
downloaded, and one feed failing (a renamed channel, a geo-block) does not stop
the run: the failures are collected and named at the end.

## Being refused

| Refusal | What the run does |
| --- | --- |
| YouTube's *"Sign in to confirm you're not a bot"* | stops **that provider** on the spot (below) |
| a Cloudflare anti-bot challenge (403) | asks that feed again with `--extractor-args generic:impersonate` |

**The bot check.** Every YouTube profile asks as the android client
(`--extractor-args youtube:player-client=android`), which is handed the plain
player response rather than the challenge the default web client gets; a single
feed that needs another client can name one in its `extraArgs`, which come after
the profile's. When the refusal arrives anyway, yt-dlp has no special handling
for it — and because every feed is fetched with `-i`, so that one dead episode
does not end a feed, it would walk into the refusal once more for every
remaining episode, which is exactly what deepens the block. So the run watches
for it and stops that provider: the feeds and tables queued behind it are not
started, while tables of other providers carry on untouched. It is said once
where it happens and once more at the end, and the run does not report success.

**The Cloudflare challenge** is the opposite case: yt-dlp answers the 403 by
naming the argument that gets past it, so the run simply asks that feed again
and reports the episodes it then gets, rather than the refusal. Nothing else in
the run is affected, and a feed whose `extraArgs` already asks to impersonate is
left to its own answer. Getting past the challenge needs yt-dlp's
[impersonation dependency](https://github.com/yt-dlp/yt-dlp#impersonation);
without it there is nothing to retry with, and the run says so once and reports
the feed as failed.

---

**Options:** `ytdlp -h` lists every option and its default. **Shared rules:**
[file safety](../file-safety.md) ·
[stopping a run](../file-safety.md#stopping-a-run) — a stopped `ytdlp` finishes
the download in flight first ·
[missing tools](../requirements.md#missing-tools-are-refused-up-front) ·
[which ffmpeg](../requirements.md#which-ffmpeg-a-run-uses) ·
[what leaves the machine](../file-safety.md#what-leaves-the-machine)
