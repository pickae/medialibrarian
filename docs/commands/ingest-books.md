# `ingest-books`

Ingests a folder of e-books into a clean, uniform library, mirroring each
source's name and sub-folder into the output. Books are processed in parallel
across all cores, and only the finished file is written to disk:

- PDFs are copied across.
- `mobi`/`chm`/`azw3`/`lit`/`txt` sources are converted to epub.
- Epub sources (and the just-converted epubs) are re-converted once more for
  consistent readability.

**`-d` is what throws part of a book away**, and it is off by default: with it,
embedded fonts are dropped, junk/teaser images are removed via an extensible
name-substring list, illustrations are downscaled to at most fullHD, and a PDF's
images are stripped. That is the version for reading on a device; the default is
the version worth keeping, since nothing `-d` discards can be recovered from the
output. Without `-d` the run needs Calibre alone — Ghostscript, `unzip`/`zip`
and ImageMagick are only asked for when there is something for them to strip.

An illustration has to be worth downscaling on *both* counts before `-d` touches
it: over the size threshold, and not already starved. Scaling a picture that has
been compressed past what its format and size need, and re-compressing it at a
fixed quality on the way, would take a second helping out of something that had
none to give — so those pass through untouched, exactly as an image under the
threshold does.

The input tree is never modified, and emitted books never clobber an existing
output (a collision keeps both via a ` (N)` suffix).
