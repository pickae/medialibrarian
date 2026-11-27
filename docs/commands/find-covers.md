# `find-covers`

Looks online for a cover for every album and audio file in a tree that has
none, or only a small one, and writes what it finds into a separate output
tree. The input is only read, so the results can be checked before anything
is moved back.

    find-covers [options] <inputDir> <outputDir>

| At a glance | |
| --- | --- |
| **Takes** | one folder of audio files, read at every depth |
| **Writes** | at the same relative path under `<outputDir>`: a `folder.jpg` for each album a cover was found for; for any other file, a copy with the cover embedded, or under `-s` an image named after the file |
| **Input** | nothing is changed |
| **Reruns** | an album or file whose cover is already in `<outputDir>` is skipped, so a rerun picks up where the last one stopped |
| **Network** | the cover catalogues (`coverSearch`), and under `-A` the language model's server |

## Albums

A folder whose tracks are numbered the way a CD is ripped is an **album**: it
holds at least two audio files, and most of their names open with a track
number — `01 Title`, `1-03 Title`, `CD2-04 Title`, `03. Title`, `Track 07`.
Where the names carry no numbers, the first and last track tagged with a track
number and the same album make it one too. A name opening with a four-digit
year (`2019-05-01 Episode`) is no track number.

An album is looked for **once**, by its folder's name and its tracks' tags,
and what is found is written as one image for the whole folder:
`folder.jpg`, or `folder.png` when a PNG is what was found. Nothing is
embedded and no track is copied.

An album is left alone when its folder holds an image named as a cover — a
name with `cover`, `folder`, `front` or `albumart` in it. Whether its tracks
carry covers of their own does not matter: a folder without that image is
looked for even when every track carries one. `-i` looks past that image as
well.

`-t` turns this off: the tracks of an album are then looked for one by one,
like any other file.

## Other files

Any other audio file is looked at, except (a cover too small to count aside,
see [below](#small-covers)):

- one in a format that cannot carry a cover (only Opus, Ogg, FLAC, MP3 and
  MP4 — `.m4a`, `.m4b` — can) — unless `-s` writes its cover beside it;
- one that already carries a cover of its own;
- one whose **own folder** holds an image named as a cover, or an image
  sharing the file's name (`track.jpg` beside `track.mp3`). That image is
  taken as its cover, since the converters would embed it. Only the file's
  own folder counts, not the folders above it. With `-i`, those images are
  ignored and such a file is looked for as well;
- one shorter than `-l <minutes>`, or smaller than `-z <megabytes>`. Both
  allow a fraction and are off by default. A file whose length cannot be read
  is still looked for. Albums are not held to either.

What is found is embedded in a copy of the file. With `-s` it is instead
written beside where the file lies, as an image named after it — `track.jpg`
(or `.png`) for `track.mp3` — and the file itself is not copied.

### Small covers

A cover under **0.25 megapixels** (smaller than 500×500) does not count:
whether it is embedded or the image beside the file or album, it is looked for
anyway, to replace it. What is found is written only when it has more pixels
than the cover it has; otherwise nothing is written for it, and the run lists
it under what nothing larger was found for. A cover whose size cannot be read
is kept.

`-m <megapixels>` moves the threshold — `-m 1` looks past anything under
1000×1000, `-m 0.09` only past covers under 300×300 — and `-m 0` keeps every
cover, whatever its size.

Unlike [`convert-audio`](convert-audio.md#cover-art), there is no rule that
leaves album tracks out, since nothing is written next to the input. `-n`
lists the albums and files a run would look for and stops there.

## The search

The search is the same one [`concat-audio`](concat-audio.md#chapters-and-cover)
and [`convert-audio`](convert-audio.md#cover-art) fall back on at the end of
their own cover search:

- An album, and a file that is the **only audio file in its folder**, are
  looked for by the folder's name and the tracks' tags, the way `concat-audio`
  looks for a book. Whether that is a music album, an audiobook or something
  else is read off the tags' genre, the folders' names and the total length.
- Any other file is looked for as a track, by its own name and tags, the way
  `convert-audio` looks for one.

Where it lies under `<inputDir>` is a clue as well. Every folder between
`<inputDir>` and the album or file — `Jazz / Kites / Paper Sky / CD1` — is
handed to the search: the folders above its own as names that may be its
artist or author, and the whole path to the language models asked under
`-A`. An album split into disc folders (`CD1`, `Disc 2`, `Disk 01`) is
looked for by the folder above the disc, the album's own, with the one above
that as its artist.

The cover catalogues are asked first. When they find nothing and the run is
given `-A`, a language model that can search the web is asked, and a model
that can see pictures judges the images it found (the `cover-search` and
`cover-look` tasks in [docs/models.md](../models.md)). The same settings
switch these steps on and off: `coverSearch`, `coverSearchLlm`,
`contentKindLlm`. A run with `coverSearch=off` has nothing to search with,
and is refused.

## What is written

A cover embedded is recompressed the way `convert-audio` recompresses one,
into a copy of the file that keeps the source's modification time. A cover
written as an image is kept as found when it is a JPEG or PNG, and turned
into a JPEG otherwise. Nothing else is written: where no cover was found, or
it could not be written, there is nothing for that album or file.

The run ends with what no cover was found for, and a tally of what was looked
for and what was skipped.

---

**Options:** `find-covers -h` lists every option and its default.
**Shared rules:** [file safety](../file-safety.md) ·
[what a run prints](../output.md) ·
[settings](../settings.md)
