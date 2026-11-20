# `ingest-books`

Ingests a folder of e-books into a clean, uniform epub/pdf library, mirroring
each source's name and sub-folder into the output.

    ingest-books [options] <inputDir> <outputDir>

| At a glance | |
| --- | --- |
| **Takes** | a folder of e-books |
| **Writes** | the finished books under `<outputDir>`; a collision keeps both via a ` (N)` suffix |
| **Input** | never modified |
| **Reruns** | a book whose output already exists from a previous run is skipped |
| **Network** | none |

## Default behavior

With no options, a run:

- copies PDFs, **converts other formats to epub, and re-converts every epub** (an epub goes across as a copy with `-k`; `-t` for plain text)
- **keeps everything in a book**: fonts, images, a PDF's pictures (`-d`)
- skips a book already in the output, and keeps both on a name collision
- ignores formats it does not list, and does not clean the names afterwards (`-c`)

Books are processed in parallel across all cores, and only the finished file is
written to disk.

| Source | Becomes |
| --- | --- |
| PDF | copied across |
| `mobi`/`chm`/`azw3`/`azw`/`lit`/`txt`/`fb2`/`docx`/`rtf`/`djvu` | converted to epub |
| epub (and the just-converted epubs) | re-converted once more for consistent readability — copied across instead with `-k` |

## Options

| Option | Effect |
| --- | --- |
| `-d` | the reading-device version: throws part of each book away ([below](#discarding-extras--d)) |
| `-c` | runs [`clean-folder-structure`](clean-folder-structure.md) on the output when done |
| `-k` | keeps the source's epub: an epub is copied across as a PDF is, and the other formats are converted once, without the re-conversion |
| `-t` | text mode: every book to a raw `.txt` instead of the epub pipeline |
| `-z` | text mode only: also a zpaq archive of the result |

### Discarding extras (`-d`)

**`-d` is what throws part of a book away**, and it is off by default. The
default is the version worth keeping, since nothing `-d` discards can be
recovered from the output; `-d` is the version for reading on a device:

| `-d` | Does |
| --- | --- |
| embedded fonts | dropped |
| junk/teaser images | classified by a vision model (`book-image-check`, see [models](../models.md)) when available, else removed via the fixed name-substring list |
| illustrations | downscaled to at most fullHD |
| a PDF's images | stripped |

When a vision model is configured and `bookJunkImagesLlm` is on (the default),
`-d` shows each non-cover candidate image in an e-book (resized to at most
`1024x1024`) to the `book-image-check` task, together with its file name inside
the e-book and the book's title. Only a high-confidence `ad` verdict removes an
image; `book`, `unsure`, low/medium confidence, unreadable JSON, or any model
error always keeps it — including rescuing real book illustrations whose
filename happens to match a junk-name substring. With `bookJunkImagesLlm=off`
or no vision model available, `-d` falls back to the fixed junk-name substrings
(`teaser`, `backadd`). Candidate images and their file names are sent to that
model, so use one you trust with them.

An illustration has to be worth downscaling on *both* counts before `-d` touches
it: over the size threshold, and not already starved. Scaling a picture that has
been compressed past what its format and size need, and re-compressing it at a
fixed quality on the way, would take a second helping out of something that had
none to give — so those pass through untouched, exactly as an image under the
threshold does.

Without `-d` the run needs Calibre alone — Ghostscript, `unzip`/`zip` and
ImageMagick are only asked for when there is something for them to strip; and
with `-k`, an epub that is already one is never asked of Calibre at all.

---

**Options:** `ingest-books -h` lists every option and its default. **Shared
rules:** [file safety](../file-safety.md) ·
[output never inside the input](../file-safety.md#the-output-folder-must-not-sit-inside-the-input) ·
[stopping a run](../file-safety.md#stopping-a-run) ·
[missing tools](../requirements.md#missing-tools-are-refused-up-front) ·
[what a run prints](../output.md) ·
[settings](../settings.md)
