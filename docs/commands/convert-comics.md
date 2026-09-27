# `convert-comics`

Recompresses a tree of comic books — `.cbr`/`.cbz`/`.cb7` archives, and PDFs that
hold one large image per page — into a tree of `.cbz` archives of AVIF pages.

- Each book is extracted and flattened into a single folder of pages, the pages
  are converted to AVIF, and their names are cleaned and numbered.
- A comic that arrived as a **PDF** is treated as the same book in a different
  container: its pages are rendered out at the resolution the PDF's own page
  images report, and everything after that is the archive path unchanged.
- Unlike an archive, a PDF does not announce that it *is* a comic, so every PDF is
  inspected first and only converted when nearly all of its pages hold exactly
  one image covering (nearly) the whole page. A magazine, a manual or a text
  e-book therefore does not get rasterised page by page into a large, unreadable
  `.cbz`: it is reported with the numbers it was judged on and left alone.
- **Pages keep their colour at full resolution** (4:4:4). The usual 4:2:0 stores
  colour at a quarter of the picture's resolution, which is invisible on a photo
  but puts a coloured fringe on thin inked lines and lettering. 4:4:4 costs
  about a quarter more per page, which the slower default speed (`-s 4`) and
  lower default quality (`-q 45`) pay back, so books come out about the size
  they did at 4:2:0. `-u 420` goes back to 4:2:0 for smaller books. 4:4:4 is
  AV1's High profile: software decoders read it, but some hardware decoders in
  phones and tablets do not, so check your reader app.
- Each finished folder of pages is zipped back into one **stored** (level 0 —
  AVIF does not compress further) `.cbz` in its mirrored parent folder.
- Pages and AVIFs are intermediate and never leave RAM: the `.cbz` files are the
  only thing written to disk, so the output tree has the same depth and roughly
  the same file count as the input rather than one extra folder level per book.
- A `.cbz` left by an earlier run is skipped, so re-running over a grown library
  neither duplicates nor rewrites finished books.
- **A book that is already a starved scan is repackaged rather than converted.**
  Its pages are judged before any of them is encoded, and if more than 80% of
  them are starved the book goes into a `.cbz` with its *own* pages: what is left
  of such a scan is all there is, and a second generation of loss over nearly
  every page would buy a small saving off a file that is already small. Below
  that share every page is converted, thin ones included — the decision is taken
  once for the whole book, because a `.cbz` whose pages are half AVIF and half
  JPEG is worse than either. Books repackaged this way are named in the closing
  report.
