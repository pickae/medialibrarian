# `ingest-series`

Sorts, names and improves a library of TV shows in place: one folder per show
with a `Season NN` folder per season, every episode named after its title in
the catalogue, the show folder tagged with its TMDb (or TheTVDB) id - and each
episode put through the same per-file improvements an ingested film gets.

    ingest-series [options] <seriesDir>...

| At a glance | |
| --- | --- |
| **Takes** | one or more folders of shows, or one show's own folder |
| **Writes** | in place: the improved `.mkv` and `.srt` sidecars; the sweeps also write lists under `logs/ingest-series/` |
| **Input** | changed — that is the job; each remuxed original is kept as `<name> - old.mkv` |
| **Reruns** | an episode that already has its `(old)` backup is not remuxed again |
| **Network** | TMDb, TheTVDB (with `tvdbApiKey`), OpenSubtitles, SubDL, and your own media server |

It is the counterpart of [`ingest-movies`](ingest-movies.md), and shares its
options, phase names and settings: what works on one video file - the track
flags, the lossless audio, the commentary transcription, the improved-copy
remux, the subtitles' sync test, the backups - is the movie ingest's, run on
each episode. What answers *which show is this, and what is each episode
called* is this command's own.

## What the names look like

The names are written for the server the `mediaServerNaming` setting names
(or else `mediaServer`), and names written for either are read back:

| | Plex | Jellyfin |
| --- | --- | --- |
| Show folder | `Show Title (2005) {tmdb-123}` | `Show Title (2005) [tmdbid-123]` |
| Season folder | `Season 01` (specials: `Season 00`) | the same |
| Episode | `Show Title (2005) - s01e02 - Episode Title.mkv` | `Show Title (2005) S01E02 Episode Title.mkv` |
| Two episodes in one file | `Show Title (2005) - s01e02-e03 - First Title.mkv` | `Show Title (2005) S01E02-E03 First Title.mkv` |
| `-r` converted copy | `… - s01e02 - Episode Title [AV1].mkv` | `… S01E02 Episode Title - AV1.mkv` |

- The id tag goes on the **show folder only**: both servers read it there.
- The show's title stays in every episode name, so an episode copied out of
  its folder still says which show it is, and subtitle sites matching on the
  file name find it. Where a name would be too long, the episode title is
  shortened first.
- Jellyfin reads anything after ` - ` as a version label, so its episode title
  follows the token with a space only.
- The converted copy of `-r` sits beside its episode as a **second version** of
  it: Plex merges the two, Jellyfin offers the label. Episodes have no
  editions.
- An episode's own extras are named after it: `<episode>-deleted.mkv`,
  `<episode>-featurette.mkv` (`-deleted2` for the second).

## Default behavior

With no options, a run goes through every phase over the folders given:

- deletes release junk, as `ingest-movies` does (`movieJunkExtensions`)
- remuxes non-Matroska video into `.mkv`
- **lays the shows out**: a loose episode goes into a folder for its show, a
  season pack (`Show.S02.1080p…`) becomes that show's `Season 02`; `S1`,
  `Staffel 1`, `Series 2` folders are renamed `Season 01`/`02` (merged where two
  name one season); loose episodes go into the season folder their token names
  and specials into `Season 00`; extras folders are sorted into the folders the
  servers read (`Behind The Scenes`, `Deleted Scenes`, `Featurettes`, …) at show
  or season level, and an extra that names an episode is put beside it, named
  after it; subtitles a release kept in a `Subs` folder go beside their episode
  as `<episode>.<xx>.srt`
- refreshes track flags and finds the subtitle roles and commentaries the file
  does not flag
- **carries over** from the media server's copy of each episode what the new
  one lacks ([below](#media-server-comparison-and-carry-over-plex))
- transcodes lossless audio, transcribes commentaries and **remuxes each
  episode once** into an improved copy, keeping the original as
  `<name> - old.mkv` - with the same track choices `ingest-movies` makes: the
  languages kept are the show's own (the catalogue's original and spoken
  languages) and `keepLanguages`, at most one surround tier survives per
  language, audio descriptions and excess picture subtitles go, the commentary
  moves below the other audio; `-k` keeps what it names
- with `-A`, splits a file holding several episodes ([below](#splitting-a-multi-episode-file-split))
- **names each show and episode** ([below](#identifying-a-show))
- downloads, syncs and tests the missing subtitles, searched for by the show's
  IMDb id with the season and episode
- names numbered commentaries ([below](#commentary-names-names)) and gives the
  episodes their disc's chapters ([below](#chapters-chapters))
- compares each episode with the media server's copy
- with `-r`, converts the remuxes last of all

A folder that holds a film - one feature-length video, no season folder and no
episode token - is left alone and listed in
`logs/ingest-series/ingest-series-films-<folder>.txt`: it is
`ingest-movies`' to ingest. The reverse holds too: `ingest-movies` passes over
a folder that holds a show, says so, and lists it at the end of its run.

## Identifying a show

1. The folder's title and year are looked up on TMDb (`/search/tv`), under
   every spelling the film matching knows (accents, numerals, articles, …).
2. A candidate needs a season for every season on disk, each with at least as
   many episodes as the highest episode number on disk.
3. Where several still fit, the episodes' measured lengths are compared with
   the catalogue's runtimes; a show is named only when exactly one fits.
4. When TMDb names none that fits, **TheTVDB** is asked the same way
   (`tvdbApiKey`, and `tvdbPin` for a subscriber-supported key), its show tried
   in its aired, DVD and absolute orders. A show only TheTVDB names is tagged
   `{tvdb-…}`, and the run says which order the server must be set to read it
   in (Plex: the show's *Episode ordering*; Jellyfin: the TheTVDB plugin).
   Without `tvdbApiKey` this step is skipped, and said so.

**Episode numbers are never changed to fit a catalogue.** A show already
tagged is looked up by its tag. An episode named by its air date
(`Show - 2011-11-15.mkv`) is matched to the episode that aired that day, and an
`Episode 4` in a season folder is that season's episode 4.

### The lists tagging writes

All under `logs/ingest-series/`, one set per folder given:

| List | What it holds |
| --- | --- |
| `ingest-series-unmatched-<folder>.tsv` | shows nothing identified - fill in the id after the tab and feed it back with `-i` |
| `ingest-series-ambiguous-<folder>.txt` | shows more than one catalogue entry fits |
| `ingest-series-renames-<folder>.txt` | every rename made (or, in a dry run, that would be) and the order hints |
| `ingest-series-nearmisses-<folder>.txt` | what the catalogues were asked and answered, for the shows left alone (dry run) |
| `ingest-series-conflicts-<folder>.txt` | renames held back, the name being taken |
| `ingest-series-duplicates.txt` | shows kept in more than one folder |

## Feeding ids back (`-i`)

    ingest-series -O tagging -i logs/ingest-series/ingest-series-unmatched-Shows.tsv Shows
    ingest-series -wO tagging -i logs/ingest-series/ingest-series-unmatched-Shows.tsv Shows

The id after the tab is a TMDb number (`1234`), a TheTVDB one
(`tvdb-81189`), or an IMDb id (`tt0000001`). A dry run unless `-w` is given.

## Single-task sweeps (`-O`)

`-O` runs the named phases on their own, in the order typed: `layout`,
`roles`, `ocr`, `tagging`, `subtitles`, `commentary`, `names`, `chapters`,
`extras-chapters`, `split`, `plex` and `backups`. `tagging`, `subtitles`,
`names` and `backups` are dry runs unless `-w` is given. `extras-chapters` and
`split` need `-A`.

Subtitles a `-wO subtitles` run has finished are recorded in
`logs/ingest-series/ingest-series-subtitles-done.txt`, by the show's id and the
episode, and skipped by later runs.

## Splitting a multi-episode file (`split`)

A file holding several episodes keeps its range name (`s01e02-e03`) unless the
run is given `-A`. With it, the file is cut into one file per episode:

1. the catalogue's runtimes of the episodes give where each seam should be,
   narrowed by the file's own chapter marks near it;
2. black frames and silences in a window round that point give the candidate
   seams - a seam is where black and silence meet;
3. a language model (the `episode-seam` task, see [models](../models.md)) reads
   what is said either side of each candidate and picks one, or answers
   *unsure* - which leaves the file whole and lists it in
   `ingest-series-unsplit-<folder>.txt`;
4. `mkvmerge` cuts at the chosen times without re-encoding, so the cut lands on
   the nearest keyframe. The original is kept as `<name> - old.mkv`.

## Media server comparison and carry-over (`plex`)

Each episode is looked up in the media server's show libraries by its show's
ids - the folder's tag and every other id the catalogue gives the show, since
the server may hold it under TheTVDB's where the folder carries TMDb's - and
its season and episode, and compared with the copy there the way
`ingest-movies` compares a film.

In a full ingest the same look-up starts as soon as the layout is done, and
what the server's copy has that the new one lacks is carried into the improved
copy, exactly as `ingest-movies` carries a film's: a soundtrack in a language
the remux keeps, a commentary (with its transcript, or transcribed), the
commentary names and named chapters. `-o plex` leaves both out.

## Commentary names (`names`)

A commentary the file only numbers ("Commentary 1") is named after who
introduces themselves in its first minutes, read by the commentary-names
model under `-A` from the episode's subtitles or a transcript of its opening.
The disc database is not asked: dvdcompare lists a show's commentaries per
disc, not per episode, and a track named after another episode's speakers is
worse than one left numbered. The list of what was named and what was left
alone is `ingest-series-commentarynames-<folder>.txt`.

## Chapters (`chapters`)

An episode of a tagged show with no chapters, or only numbered ones, is looked
up in ChapterDB under its show's title. A set is taken only when its title
names this episode - its `S01E02`, or its title - or, for an episode that
already has numbered chapters, when the set's marks fall where those are: the
episodes of one show run to the same length, and the length alone says nothing
about which episode a set is of.

## Not done for shows (yet)

- commentary names from the disc database (see above);
- a language model's help identifying a show or dating an undated folder.

## Settings

`ingest-series` reads `ingest-movies`' settings - the logins, `spokenLanguages`,
`keepLanguages`, `losslessAudioTarget`, `mediaServer`, `mediaServerNaming`,
`movieJunkExtensions` and the model switches it shares - plus its own:

| Setting | What it is for |
| --- | --- |
| `tvdbApiKey` | TheTVDB's v4 project key, for the fallback lookup |
| `tvdbPin` | the subscriber PIN a user-supported TheTVDB key needs; most need none |

The full list is on the [settings page](../settings.md).
