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

Each is taken from the first source that has one:

| | First | Then | Last |
| --- | --- | --- | --- |
| **Chapters** | a `.cue` sheet — the only one, or else the one beside the audio sharing its name, or else the largest | one chapter per individual file | — |
| **Cover** | an image file in the input folder | the first page of a PDF booklet (needs `pdftoppm`) | the art embedded in the audio files |

A book that none of the three gave a cover is looked for **online**. What it
probably is decides where: a name ending in a `[YouTube id]` (yt-dlp's
default) is a YouTube podcast, and that video's own thumbnail is fetched; a
name starting with a `yyyymmdd` date, or a length of 30 to 200 minutes, reads
as a podcast (YouTube, then the show's artwork);
anything longer as an audiobook (Audible, Amazon, iTunes, then another
language's edition or the printed book). An answer is taken only when it is
the same title and nothing about it — author, running time — says otherwise;
otherwise the book stays without a cover. Where it looks is a table,
[`coverproviders.tsv`](../../medialib/config/coverproviders.tsv); the
`coverSearch` and `coverSearchTable` [settings](../settings.md) switch the
search off or point it at your own copy.

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

---

**Options:** `concat-audio -h` lists every option and its default. **Shared
rules:** [file safety](../file-safety.md) ·
[output never inside the input](../file-safety.md#the-output-folder-must-not-sit-inside-the-input) ·
[stopping a run](../file-safety.md#stopping-a-run) ·
[missing tools](../requirements.md#missing-tools-are-refused-up-front) ·
[which ffmpeg](../requirements.md#which-ffmpeg-a-run-uses) ·
[what a run prints](../output.md) ·
[settings](../settings.md)
