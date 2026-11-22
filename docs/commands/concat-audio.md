# `concat-audio`

Concatenates the audio in each input subfolder into one output file per
subfolder, with chapters and an embedded cover.

    concat-audio [options] <inputDir> <outputDir>

| At a glance | |
| --- | --- |
| **Takes** | one subfolder per book, holding only one of mp3, opus, aac, m4a (incl. xHE-AAC) or flac — in folders of any depth |
| **Writes** | one audiobook file per subfolder into `<outputDir>` |
| **Input** | untouched, unless pretreatment (`-p`) is opted into, which renames folders and files in place - except that a cover found online is added beside the audio (see [Chapters and cover](#chapters-and-cover)); nothing there is overwritten |
| **Network** | none |

## Default behavior

With no options, a run:

- joins each subfolder's audio in name order into one book, **without re-encoding** (FLAC is re-encoded to FLAC); AAC and m4a books come out as `.m4b`
- passes over a subfolder holding more than one audio format
- takes chapters from a cue sheet, else makes one per file, and embeds a cover ([below](#chapters-and-cover))
- **never overwrites an existing book**: one already at its output name is taken to be from an earlier run and skipped, so a rerun picks up where the last stopped; two subfolders of one run that clean to the same name fail the second
- leaves the input alone (`-p`)

The naming of the files and subfolders should reflect the order they are to be
joined in.

## What a run prints

One progress line per subfolder. ffmpeg's own output is kept rather than
shown, and replayed only for a book whose join failed or came out short — its
last lines under the error, all of it kept in
the run's error log under `logs/error/`.

- `-v` adds each subfolder's steps and replays more of a failed join
- `-q` prints only failures

## Several books at once

Each book is built whole in RAM — joined, chaptered and given its cover — and
only the finished file is written to `<outputDir>`, one book at a time. How
many are built at once depends on where the input is:

- **in RAM**: one per CPU thread
- **on disk**: two, since a mechanical drive read by more than that spends its
  time seeking

A book too big for the RAM left over is built in the disk overflow scratch
instead.

## Chapters and cover

Each is taken from the first source that has one. The steps below that ask
a [language model](../models.md) do so only when the run is given `-A`
(`convert-and-concat` hands its `-A` down):

| | First | Then | Last |
| --- | --- | --- | --- |
| **Chapters** | a `.cue` sheet — the only one, or else the one beside the audio sharing its name, or else the largest | one chapter per individual file | — |
| **Cover** | an image file in the input folder | the first page of a PDF booklet (needs `pdftoppm`) | the art embedded in the audio files |

Chapters made from the files are named after them. Where nearly all those
names are only numbers (`01`, `Track 02`, `CD1 - 03`), a
[language model](../models.md) that can **search the web** is asked for the
book's table of contents (the `contents-search` task). It is told the same
things as the cover search below, plus each chapter's start and length. Its
titles are taken only when it gives exactly one for every chapter. Any other
count means the files aren't cut where the book's chapters are, so the
numbers stay. `contentsSearchLlm=off` skips this step.

Among image files in the input folder, cover names win first (`cover` +
`front`, `front`, `cover` without `back`, `inlay`, `folder`, `back`, in
decreasing order). When none of those match and the folder holds two or more
images (for instance `scan01.jpg`, `cd.jpg`, `booklet3.jpg`), a
[language model](../models.md) that **sees pictures** — the `cover-look` task
— is shown those images with their file names and the folder's name and picks
which one is the front cover; when none looks like one, no vision model is set
up, or `coverLookLlm=off`, the first image found is taken. Those image files
and their names go to that model, so use one you trust with them.

A book that none of the three gave a cover is looked for **online**. What it
probably is decides where: a name ending in a `[YouTube id]` (yt-dlp's
default) is a YouTube podcast, and that video's own thumbnail is fetched;
otherwise, under `-A`, when a [language model](../models.md) is set up and
`contentKindLlm` is on, a light model (the `audio-content` task) classifies
whether the recording is a podcast, an audio drama, an audiobook, music or
classical music from the folder's path and file names, the tracks' tags, the
duration, and the channel count. Without a model, music is told by its tags
or where it is kept: a genre that is not speech reads as music, and a
classical genre, a conductor, a catalogue number such as "Op. 27", or a
folder named Klassik or Classical on the way to it reads as classical; an
untagged folder under one named Music reads as music. Music is looked for as
an album (iTunes, Deezer, MusicBrainz's Cover Art Archive); classical music
as one performance of a work, by its performer and the work - an untagged
recording's own folder naming the performer and the one above it the work -
and taken only when the release's artist is that performer and its title
names the work - and, where the folder names its label ("35 Lindqvist
(Nordlabel, 1972)"), only a release that label's copyright line names, since
a performer may have recorded the work more than once. Under convert-and-concat these clues are read from the
source folders, not its intermediate tree. When the run has no `-A`, no
model is available, `contentKindLlm=off`, or the model is unsure, and
nothing says music, it falls back to the length and channel-count heuristic: a name
starting with a `yyyymmdd` date, or a length of 30 to 200 minutes in mono,
reads as a podcast (YouTube, then the show's artwork); 30 to 200 minutes in
stereo reads as an audio drama; anything longer as an audiobook (Audible,
Amazon, iTunes, then another language's edition or the printed book). An answer
is taken only when it is the same title and nothing about it — author, running
time — says otherwise; otherwise the book stays without a cover. Where it looks
is a table, [`coverproviders.tsv`](../../medialib/config/coverproviders.tsv);
the `coverSearch`, `coverSearchTable` and `contentKindLlm`
[settings](../settings.md) switch these steps off or point the search at your
own table.

Where that finds nothing, a [language model](../models.md) is asked to
**search the web** for the cover - the `cover-search` task, which only a
model that can search takes: Claude, or any other through an agent harness.
It is told the folder as it lies (every file's name), the tags inside the
tracks, the name the book is written out under, what it probably is and why,
and what a good cover is: the recording's own, in its language, the
audiobook's over the printed book's, as large as can be had. Then a model
that **sees pictures** - the `cover-look` task, which leans to a small, cheap
one - is shown the first few images it named and picks the one that is
plausibly this book's cover, or none, and the book stays without. With no
such model, the search's first is taken. `coverSearchLlm=off` skips this
step alone; the files' names and tags go to those models, so use ones you
trust with them.

A cover found online either way is also **kept in the input folder**, as
`cover.jpg` (or `.png`, `.webp`; `cover (2).jpg` where that name is taken -
nothing is overwritten), so the folder has its cover from then on and the
next run takes it from there.

## Metadata tags

After chapters and cover are embedded, a [language model](../models.md) that
can search the web can look up the recording's metadata tags - the `book-tags`
task (`title`, `author`, `narrator`, `series`, `series_index`, `year`,
`language`, and `abridged`). It is told the folder's file names, the tracks'
tags, what the book probably is, and any hints the cover search returned when
that ran; it also runs when the book already had a local cover. Music is
asked for an album's tags instead (`title`, `album`, `artist`,
`album_artist`, `year`, `genre`, `label`), and classical music for its
`composer`, `work`, `performer`, `conductor` and `ensemble` as tags of their
own, with the performers - never the composer - as its artist.

Only tags that are currently empty on the joined output file are filled —
non-empty tags are never overwritten, and the output file is never renamed.
`bookTagsLlm=off` switches this step off; the files' names and tags go to the
model, so use one you trust with them.

---

**Options:** `concat-audio -h` lists every option and its default. **Shared
rules:** [file safety](../file-safety.md) ·
[output never inside the input](../file-safety.md#the-output-folder-must-not-sit-inside-the-input) ·
[stopping a run](../file-safety.md#stopping-a-run) ·
[missing tools](../requirements.md#missing-tools-are-refused-up-front) ·
[which ffmpeg](../requirements.md#which-ffmpeg-a-run-uses) ·
[what a run prints](../output.md) ·
[settings](../settings.md)
