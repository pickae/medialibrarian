# `content-census`

Reads a library and writes down what is in it: one row per file, one report per
content type (audio, video, images, books, comics), written into the folder that
was censused and named after it. Every file whose suffix is in one of the central
extension lists is included; nothing in the tree is renamed, moved or converted.

**Several libraries can be censused in one call**, each as its own library with
its own reports — which is what makes the `library` axis of
[`content-census-bi`](content-census-bi.md) usable without doing the runs by hand.

`-b` builds the cubes in the same command, and is only a shorthand, deliberately
so: the two halves have very different costs. The census reads every file and
converts every book to count its words — the hour-long half — while the cubes are
seconds of arithmetic over the finished CSVs. When the reports already exist, run
`content-census-bi` on its own and don't pay for the walk again.

`-o`'s folder is **made if it is not there yet** — pointing a run at a fresh
`~/reports/tonight` needs no `mkdir` first — and given back again if the run then
refuses, so "nothing was changed" stays true of directories too.

Two folders **named the same** are refused up front, naming both, rather than
censused: their reports would collide, and since a library is known by its report
name from there on, the failure would otherwise be silent.

**`-d` takes the libraries from below the paths given.** A disk is usually not
one library but a shelf of them, and naming forty by hand is not an answer:

```
content-census -b -d 1 -o ~/reports /mnt/discOne /mnt/discTwo
```

`-d 1` makes every subfolder of a given path a library of its own, `-d 2` every
grandchild, and so on; `-d 0` (the default) censuses the paths themselves.
Exactly that level is a library and everything below it belongs to it — a file
lying *beside* those folders, or above them next to the path given, belongs to no
library and is not censused. Each library gets its own reports, named after the
way down to it (`videoDiscOneFilms.csv`), so
[`content-census-bi`](content-census-bi.md)'s `library` axis gets a level to drill
down into without a second census.

- **Dynamic range** is read through the same judgements
  [`ingest-movies`](ingest-movies.md) acts on, so the two cannot disagree about a
  file. What the container *claims* is what is recorded: verifying that a Dolby
  Vision RPU is really in the bitstream costs a pass over the whole stream, which
  is the ingest's job, not a census's.
- **`-a` judges every video, soundtrack and image starved, adequate or
  generous** for what it is, in a column of its own that becomes an axis of the
  cube like any other. Whether a file is big enough depends on the codec, the
  size and what is in it together, so no single column can answer it — and the
  answer is what "which of this is worth re-encoding" actually asks. Each of the
  three is taken with the very model its own converter decides one file with, so
  the census and the conversion cannot disagree about a film, an album or a scan.
  Off by default because the image half is dear: telling a photograph from a page
  of flat artwork means decoding the picture, which over a library of scans is a
  run of its own.
- **The dear axis of each model is the one a census leaves unmeasured**, and in
  each case that is the forgiving direction: a video's grain and an image's
  content only ever *raise* what the file needs, so nothing is called starved
  that a run which did measure them would call adequate. A soundtrack is read as
  spoken word when the container says so or when it runs past twenty minutes, and
  as music otherwise — the mistakes that makes (a symphony movement, a DJ set)
  all land on the lower requirement too. A file whose size, bitrate or dimensions
  nobody stated reads `unknown`.
- **A lossless file is never starved.** A PNG, a TIFF, a FLAC and a WAV hold
  every pixel or sample they were given, so "it was not given enough bytes" is
  not a thing that can be true of one however small it is — a 400-byte lossless
  icon is a small picture, not a degraded one. Their verdict is floored at
  `adequate`, which leaves the reading that still means something: whether
  re-encoding would save anything.
- **Loose images are a report of their own** — path, size, resolution and format,
  plus the verdict under `-a`. The format is what the file's own header says and
  not what its suffix claims, so a `.jpg` holding a PNG is recorded as the PNG it
  is.
- **Books are counted on their text**: each one is converted with Calibre and
  counted from that, so epub, mobi, chm, azw3, lit and PDF all answer the same
  way instead of each needing its own extractor.
- **A file with no chapter marks counts as one chapter**, not as none: it is one
  unbroken chapter, itself. So the `chapters` column adds up to how many parts a
  library holds rather than to how many somebody happened to mark up, and a mean
  chapter length is not a division by zero.
- **A `.pdf` is decided, not assumed**: it is in both the comic and the book
  extension lists, so which report it lands in comes from the same probe
  [`convert-comics`](convert-comics.md) gates on — a scan goes to comics, a text
  book to books.
- **A lying suffix is skipped, not guessed at**: an `.mka` that holds a video
  track, an `.mp4` with no video in it, a `.cbz` that is not an archive or holds
  no page. Each one is named at the end of the run, with the reason.
- **Missing optional tools cost a column, not the run**: without `mediainfo` the
  dynamic range comes from ffprobe instead (HDR10+ then reads as HDR10), without
  poppler-utils no PDF can be examined, without Calibre the word and character
  counts stay empty. Each is said once at startup rather than discovered per file.

The census is **serial within a disk and parallel across them**. Reading one
library is one metadata read per file across a whole tree, which on the spinning
disks such a library lives on is seek-bound: eight of those at once over one disk
finish later than in order. Several `<inputPath>`s, though, are several disks, so
the run forks one worker per path given and no more — and the libraries `-d`
finds *under* one path are censused one after the other inside its worker, since
they share its head. Books are the exception to the serial half: their cost is a
text conversion on the CPU, not a read on the disk, so the books of a library run
one per core while the rest of it still goes through one at a time. `Ctrl+C`
stops the walk but still writes the reports for the files already read, in every
library that was being read.
