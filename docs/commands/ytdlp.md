# `ytdlp`

Downloads the audio of a list of podcast feeds into a library laid out for a
phone, remembering what it has already fetched so a re-run only picks up what is
new. Sponsor segments are cut out, a thumbnail and metadata are embedded, and
Opus is preferred over m4a.

    ytdlp [options] <outputPath> <archiveFile> [<dateRange>]
    ytdlp -l <archiveFile> <archiveFile>...

The three arguments are the things that change between runs — where the library
goes, where the "already have it" record lives, and optionally an upload-date
filter (`20260607`, `20260607..20260707`, `..20260707`, or a relative date such
as `today-2weeks`). A rolling window is just a relative end: `..today-10days` is
"nothing newer than ten days". An archive given as a bare name is kept in the
script directory's `logs/` folder — the home of the records a run keeps about
itself, and of `ingest-music`'s `beets.log` — and is read back from there on the
next run; a path is taken as given. Everything else lives in the tables.

**Merging archives (`-l`).** When the same tables are run on more than one
computer, each machine's archive `.log` drifts apart, and each would re-fetch what
the other already has. `-l` takes two or more `.log` files and nothing else, and
writes every line of all of them — sorted, duplicates dropped, Windows line
endings folded in — into the first, creating it if it is not there. Nothing is
downloaded.

**One table, both systems.** A table (`data/podcasts/*.tsv`) holds one row per
podcast — its folder, its file-name template, how many entries back to walk (`0`
for the whole feed), any arguments it alone needs, and its URL — and the argument
sets every feed shares live once. The row does not say how yt-dlp is called,
because that is what differs between the two systems and is worked out at run
time:

- **which yt-dlp**: a `yt-dlp.exe` beside the checkout, one on `PATH`, or an
  importable `yt_dlp` module — which covers the apt, pip and pipx installs, and
  the single downloaded binary. On Linux a `yt-dlp-nightly` is taken ahead of
  the release one wherever both are installed, since an extractor a site broke
  is fixed in a nightly weeks before it is in a release
  (`pipx install --suffix=-nightly --pip-args=--pre yt-dlp` puts one there).
  `YTDLP` overrides the search. The winner is printed, because "it works on the
  other machine" usually starts there.
- **which nightly**: when the winner turns out to be a nightly, the run brings
  it up to date before fetching anything — a fix that landed this morning is no
  use to a build from last month. The install's own upgrade path is used (pipx
  for a pipx venv, `-U --update-to nightly` for a downloaded binary; a pip or
  distro install is left to its package manager), and a failed upgrade is a
  warning rather than the end of the run. `SKIP_YTDLP_UPGRADE=1` turns the
  check off, and a preview (`-p`) never upgrades.
- **which ffmpeg**: yt-dlp does its own muxing through ffmpeg — extracting the
  audio, merging the two streams into Matroska, embedding the thumbnail and the
  chapters — and finds one by searching `PATH` itself. So the run walks the
  [ladder](../requirements.md#which-ffmpeg-a-run-uses) past any build that will not run and names
  the winner to yt-dlp, which on a machine whose only working ffmpeg is a
  hand-installed build under `/opt/ffmpeg` or `~/.local/bin` is the difference
  between the muxing working and not. A feed's own `--ffmpeg-location` still
  wins, since yt-dlp keeps the last one it is given.
- **which paths**: under Git Bash or Cygwin the output and archive paths are
  translated to their drive-letter form, since a native `yt-dlp.exe` does not
  know where the emulated root is mounted.
- **which quoting**: `-p` prints the calls instead of running them, quoted for
  the shell of the host it is printed on. `-s windows` / `-s linux` prints the
  *other* machine's calls from the same table.

**Several tables, and what each one is.** `-t` may be given more than once, and
each table says what it is with a `#!profile` line:

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

**Which tables there are.** One per thing that would otherwise have to be passed
differently — the output root, the download archive, or what the feeds are
fetched as. `podcasts.tsv` and `podcastsRss.tsv` are the phone's YouTube and RSS
feeds; `youtubeAudio.tsv` and `youtubeAudioSecondArchive.tsv` (the same feeds,
its own archive) the desktop audio library; `youtubeVideo.tsv` the video
library; `siteVideo.tsv` one site walked page by page; `youtubeAudioPaused.tsv`
the feeds that are switched off. Several may be given to one run with `-t`, as
long as they share that run's output root and archive.

The tables are one machine's library rather than code, so `data/` is not tracked
and there is a sample to start from instead —
[`medialib/config/podcasts.example.tsv`](../../medialib/config/podcasts.example.tsv), a working table
whose header documents the columns and the directives:

    cp medialib/config/podcasts.example.tsv data/podcasts/podcasts.tsv

A podcast is paused by putting a `0` in its `active` column, not by commenting
its row out — so it still shows up as a podcast, in a `grep` and in the count.
`-a` runs the paused ones anyway, `-m <text>` narrows a run to the feeds whose
folder or URL matches.

**Tidying up after a run (`-c`).** A yt-dlp run does not leave one file per
episode: it leaves the thumbnail it embedded, any description, metadata json or
subtitle files a row's `extraArgs` asked for, and — if it was interrupted — the
`.part` of a fragment. With `-c` the run clears that up afterwards: the
sidecars of the episodes **it** downloaded are removed (for video, the
description and the metadata json are first *attached into* the Matroska,
since they are the parts of a video that disappear with it), anything
that is not Matroska is remuxed into one, the leavings of interrupted downloads
are swept from the output root, and folders left empty are pruned. It works from
the run's own manifests rather than by scanning the library, which is what lets
it tell a leftover thumbnail from a folder's cover art.

**Building the phone's library from the run (`-i`).** What comes off the network
and what the phone plays are not the same file — the download is the best the feed
offers, the library copy is that re-encoded small enough to carry around — so with
`-i` they are not the same tree either. `<outputPath>` becomes the parent of both:

    phone/Staging/Speech/…       the raw downloads, kept
    phone/Staging/Music/…
    phone/Ingested/…             the library, built out of both

When the run is over, each staging folder's names are cleaned
(`clean-folder-structure`) and its episodes converted into that one library
(`convert-audio` — `-c -m` for podcast RSS, `-c -b 65` for music). There is one
staging folder per **conversion** and not per table or profile, because the
converter is handed a folder rather than a file — and they all end up in the
same library, so the split is invisible in the result. The raw downloads are
kept: they are the only copy of what was published, and the archive file says
they will not be fetched again, so changing one's mind about a bitrate is a
re-run of the last step rather than of the whole night. A video table ignores
`-i` and downloads to `<outputPath>` as usual, since an audio converter would
throw its picture away. Normally given together with `-c`, so the embedded
thumbnail is gone before the library is built out of the folder it was sitting
in.

**What a run prints.** One line per episode, and nothing else; `-v` puts yt-dlp's
own output back for when something needs diagnosing. The episodes are counted from
what yt-dlp reports after all post-processing, so the closing figures are of
finished files on disk rather than of feeds walked.

The tables, the date range and the tools are all checked before anything is
downloaded, and one feed failing (a renamed channel, a geo-block) does not stop
the run: the failures are collected and named at the end.

**Being refused.** YouTube increasingly answers with *"Sign in to confirm you're
not a bot"*. Every YouTube profile therefore asks as the android client
(`--extractor-args youtube:player-client=android`), which is handed the plain
player response rather than the challenge the default web client gets; a single
feed that needs another client can name one in its `extraArgs`, which come after
the profile's. When the refusal arrives anyway, yt-dlp has no special handling
for it — and because every feed is
fetched with `-i`, so that one dead episode does not end a feed, it would walk
into the refusal once more for every remaining episode, which is exactly what
deepens the block. So the run watches for it and stops **that provider** on the
spot: the feeds and tables queued behind it are not started, while tables of
other providers carry on untouched. It is said once where it happens and once
more at the end, and the run does not report success.

A Cloudflare anti-bot challenge is the opposite case: yt-dlp answers the 403 by
naming the argument that gets past it, so the run simply asks that feed again
with `--extractor-args generic:impersonate` and reports the episodes it then
gets, rather than the refusal. Nothing else in the run is affected, and a feed
whose `extraArgs` already asks to impersonate is left to its own answer. Getting
past the challenge needs yt-dlp's [impersonation
dependency](https://github.com/yt-dlp/yt-dlp#impersonation); without it there is
nothing to retry with, and the run says so once and reports the feed as failed.

A run can also be stopped with Ctrl+C at any point: nothing that had not begun is
begun, and the run still reports what it managed before exiting — which is what
every command here does (see [Stopping a run](../file-safety.md#stopping-a-run)).
