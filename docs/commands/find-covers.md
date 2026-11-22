# `find-covers`

Looks online for a cover for every audio file in a tree that has none, and
writes what it finds into **copies** in a separate output tree. The input is
only read, so the results can be checked before anything is moved back.

    find-covers [options] <inputDir> <outputDir>

| At a glance | |
| --- | --- |
| **Takes** | one folder of audio files, read at every depth |
| **Writes** | for each file a cover was found for: a copy at the same relative path under `<outputDir>` with the cover embedded |
| **Input** | nothing is changed |
| **Reruns** | a file whose copy is already in `<outputDir>` is skipped, so a rerun picks up where the last one stopped |
| **Network** | the cover catalogues (`coverSearch`), and under `-A` the language model's server |

## Which files are looked for

Every audio file is looked at, except:

- one in a format that cannot carry a cover (only Opus, Ogg, FLAC, MP3 and
  MP4 — `.m4a`, `.m4b` — can);
- one that already carries a cover of its own;
- one whose **own folder** holds an image named as a cover — a name with
  `cover`, `folder`, `front` or `albumart` in it — or an image sharing the
  file's name (`track.jpg` beside `track.mp3`). That image is taken as its
  cover, since the converters would embed it. Only the file's own folder
  counts, not the folders above it.

With `-i`, those images are ignored and such a file is looked for as well.

Unlike [`convert-audio`](convert-audio.md#cover-art), there is no rule that
leaves album tracks out: every file without a cover is looked for, since
nothing is written next to the input. `-n` lists the files a run would look
for and stops there.

## The search

The search is the same one [`concat-audio`](concat-audio.md#chapters-and-cover)
and [`convert-audio`](convert-audio.md#cover-art) fall back on at the end of
their own cover search:

- A file that is the **only audio file in its folder** is looked for as a
  book, by the folder's name, the way `concat-audio` looks for one.
- Any other file is looked for as a track, by its own name and tags, the way
  `convert-audio` looks for one.

The cover catalogues are asked first. When they find nothing and the run is
given `-A`, a language model that can search the web is asked, and a model
that can see pictures judges the images it found (the `cover-search` and
`cover-look` tasks in [docs/models.md](../models.md)). The same settings
switch these steps on and off: `coverSearch`, `coverSearchLlm`,
`contentKindLlm`. A run with `coverSearch=off` has nothing to search with,
and is refused.

## What is written

A cover found is recompressed the way `convert-audio` recompresses one, then
embedded in a copy of the file at the same relative path under `<outputDir>`.
The copy keeps the source's modification time. Nothing else is written there:
where no cover was found, or the embed failed, there is no copy.

The run ends with the files no cover was found for, and a tally of what was
looked for and what was skipped.

---

**Options:** `find-covers -h` lists every option and its default.
**Shared rules:** [file safety](../file-safety.md) ·
[what a run prints](../output.md) ·
[settings](../settings.md)
