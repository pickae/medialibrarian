# `convert-comics`

Recompresses a tree of comic books — `.cbr`/`.cbz`/`.cb7` archives, and PDFs that
hold one large image per page — into a tree of `.cbz` archives of AVIF pages.

    convert-comics [options] <inputDir> <outputDir>

| At a glance | |
| --- | --- |
| **Takes** | a tree of `.cbr`/`.cbz`/`.cb7` archives and comic PDFs |
| **Writes** | one `.cbz` per book in its mirrored parent folder: the same depth and roughly the same file count as the input, not one extra folder level per book |
| **Input** | renamed in place when an archive wears the extension of another - a zip called `.cbr` becomes `.cbz` (`-k input` keeps the name); duplicate files removed, empty folders pruned |
| **Reruns** | a `.cbz` left by an earlier run is skipped, so a grown library neither duplicates nor rewrites finished books |
| **Network** | none, unless the run is given `-A` and a hosted vision model is configured for `comic-page-check` (`comicEmptyPageLlm=off`) or borderline PDFs (`comicPdfLlm=off`) |

## Default behavior

With no options, a run:

- writes pages as **AVIF** at quality 45, speed 4, colour at full resolution (4:4:4) (`-Q`, `-s`, `-u`)
- **scales pages taller than 2960 px down** to that height (`-m`)
- **trims light, plain margins off every page**, treating colours within 10% of the edge as margin (`-f`; the trim is what `-k` keeps in)
- **drops blank pages** (what the trim ends with deciding, so `-k` keeps them in with it); when a vision model is configured, small pages under 40 KiB are judged by the model instead of the 10 KiB size-only rule ([below](#empty-page-check))
- **repackages a book of mostly starved pages** instead of re-encoding it ([below](#starved-scans))
- inspects every PDF and skips one that is not a scanned comic ([below](#pdfs))

## How a book is converted

1. Each book is extracted and flattened into a single folder of pages. A book
   is opened as what its contents are, not what its extension says, and **an
   archive under the wrong extension is renamed in the input** — a zip called
   `.cbr` becomes `.cbz`, a RAR called `.cbz` becomes `.cbr`. With `input` in
   `-k` the rename stays out: the book is opened by content either way, so it
   comes out the same under the name it was given. The `__MACOSX/`
   and `._` files a Mac leaves in an archive are not pages and are dropped. A
   book with a member that asks to write outside its own folder — spelled
   `/etc/…` or `../…`, or a link pointing out of the tree — is not unpacked at
   all.
2. The pages are converted to AVIF, trimmed on the sides, skipped if empty, and numbered `01`, `02`, … in their reading order (`-k` keeps the trim and the skipping in, so a kept page is the page it came in as).
3. The finished folder of pages is zipped back into one **stored** (level 0 —
   AVIF does not compress further) `.cbz` in its mirrored parent folder.

Pages and AVIFs are intermediate and never leave RAM: the `.cbz` files are the
only thing written to disk.

**With [av1gpu](https://github.com/maddog107/av1gpu) installed, the pages are
encoded on the NVIDIA card and the CPU at once**, both kept busy:

- The card takes JPEG, PNG and WebP pages of up to 100 megapixels that have
  no transparency and, for a PNG or WebP, are stored upright, and it does their
  trim and their shrink to `-m` itself, after its own decode (av1gpu 0.7.0 or
  newer). A trimmed page's margin is the page's own rather than a flat border.
- The CPU takes everything else - other formats, pages with transparency,
  pages the card did not write.
- `-Q` means what it means on the CPU: the card is asked for the quality whose
  pages score the same SSIMULACRA2 as the CPU's on comic pages, with its
  `turbo` preset.

With no card every page is encoded on the CPU.

## PDFs

A comic that arrived as a **PDF** is treated as the same book in a different
container: its pages are rendered out at the resolution the PDF's own page
images report, and everything after that is the archive path unchanged.

Unlike an archive, a PDF does not announce that it *is* a comic, so every PDF is
inspected first and taken when nearly all of its pages hold exactly one image
covering (nearly) the whole page. A PDF that misses that bar but is near it, or
has zero matching pages at all (as digitally drawn comics and tiled scans do),
is shown to a vision [model](../models.md) (2–3 rendered pages and the file
name) under `-A`, when one is configured and `comicPdfLlm` is on. Only a confident `comic`
verdict overrides the scan check; `magazine`, `text`, `other` or any error
leaves it skipped. Verdicts are cached per file (path, size and modification
time, under `comicPdfCache`) so `convert-comics` and `content-census` always
agree without asking twice. A magazine, a manual or a text e-book therefore
does not get rasterised page by page into a large, unreadable `.cbz`: it is
reported with the numbers it was judged on and left alone.

## Colour

**Pages keep their colour at full resolution** (4:4:4). The usual 4:2:0 stores
colour at a quarter of the picture's resolution, which is invisible on a photo
but puts a coloured fringe on thin inked lines and lettering.

| `-u` | Colour | Size |
| --- | --- | --- |
| `444` (default) | full resolution | about a quarter more per page, which the slower default speed (`-s 4`) and lower default quality (`-Q 45`) pay back, so books come out about the size they did at 4:2:0 |
| `420` | a quarter of it | smaller books, with colour fringes on thin lines and lettering |

4:4:4 is AV1's High profile: software decoders read it, but some hardware
decoders in phones and tablets do not, so check your reader app.

## Starved scans

**A book that is already a starved scan is repackaged rather than converted.**
Its pages are judged before any of them is encoded:

| Share of starved pages | What happens |
| --- | --- |
| more than 80% | the book goes into a `.cbz` with its *own* pages: what is left of such a scan is all there is, and a second generation of loss over nearly every page would buy a small saving off a file that is already small |
| 80% or less | every page is converted, thin ones included |

The decision is taken once for the whole book, because a `.cbz` whose pages are
half AVIF and half JPEG is worse than either. Books repackaged this way are named
in the closing report.

## Empty-page check

Without a vision model (or with `comicEmptyPageLlm=off`), an AVIF page that
encodes to less than 10 KiB is dropped as empty (unless `-k pages` is given)
and larger pages are untouched — the existing size-only rule applies unchanged.

Under `-A`, when a vision model is configured and `comicEmptyPageLlm` is on (the
`comic-page-check` task in [language models](../models.md), needing `vision`
and leaning `light`), the hard rules are eased and the model judges small
candidate pages (up to 40 KiB):

- Each candidate page is rendered to a small JPEG (up to 1024×1024) and the
  model is asked whether it has real content that you would keep in a comic
  book rather than being blank, a "this page is intentionally blank" notice,
  just a page number, or just a uniform color.
- Pages the model judges blank (with medium or high confidence) are dropped —
  even when a sparse placeholder or lone page number encoded between 10 KiB and
  40 KiB — while sparse pages with real content (such as title pages or faint
  sketches under 10 KiB) are kept.
- If the model sees content, has low confidence, is unsure, or fails to answer,
  the page is kept.
- Model usage is recorded across the run and printed in the closing report when
  any requests were made.

## Keeping everything (`-k`)

**A run with `-k` takes nothing away.** The `-k` is given in parts - `pages`
and `input`, comma-separated - and a bare `-k` keeps both.

`pages` keeps the two edits a run otherwise makes to what a page is: the trim
of the light, plain margins off every page, and the drop of the page the trim
ends with deciding is blank. What a kept page still gets is everything that
only shapes the file: the `-m` cap, the encode, the numbering. The closing
report names only what occurred, so a kept run carries no trimmed line and no
blank line at all - and the page a run without the part would drop is in the
book, numbered like its siblings. `-f` is the fuzz of the trim, and the part
keeps the trim in, so the two are refused together with the page, before any
book is touched.

`input` keeps the one rename that changes a name the user chose: an archive
under the wrong extension - a zip called `.cbr`, a RAR called `.cbz` - keeps
the name it was given. The book is opened as what its contents are either
way, so the run comes out the same book under the kept name.

---

**Options:** `convert-comics -h` lists every option and its default. **Shared
rules:** [file safety](../file-safety.md) ·
[output never inside the input](../file-safety.md#the-output-folder-must-not-sit-inside-the-input) ·
[stopping a run](../file-safety.md#stopping-a-run) ·
[missing tools](../requirements.md#missing-tools-are-refused-up-front) ·
[what a run prints](../output.md) ·
[settings](../settings.md) ·
[language models](../models.md)
