# `convert-images`

Batch-converts images to AVIF, WebP or JPEG XL (or back to JPEG), with optional
whitespace cropping and parallel encoding. AVIF is the default, and `-s` stays
one knob across all three: each encoder's own effort setting is derived from it,
so a lower `-s` is a slower encode whichever format is being written.

Input is every still format a library plausibly holds: JPEG, PNG, WebP, AVIF,
HEIC, JPEG XL, JPEG 2000, TIFF, BMP, GIF, TGA, PCX, Netpbm, PSD and ICO. An
animated GIF is read for its first frame; the input is never modified, so nothing
is lost by pointing a run at one.

- **An image that is already starved is left alone.** Whether a file is big
  enough depends on its format, its pixel count and what is in it together, and
  below that point whatever is left of the picture is all there is: encoding it
  again spends a second generation of loss on something that had none to spare.
  Such images are counted in the closing report and skipped. `-a` converts
  everything regardless, which is what [`convert-comics`](convert-comics.md) asks
  for — inside one book every page has to come out in the same format.
- **The check is one header read per image**, not a decode: `identify -ping`
  stops at the header, which states the format, the pixel size and whether there
  is colour in the picture. What is *in* the picture — photograph, flat artwork,
  line art — would cost a full decode, so it is left unmeasured here and read at
  its most forgiving. An image nothing could measure is converted, not skipped.
- `-r` never asks: converting back to JPEG is about what can open the file, and a
  source too small to improve is exactly as unopenable as a large one.
- `-u 444` writes AVIF with colour at full resolution instead of a quarter of
  it: sharper coloured lines and lettering, files about a quarter larger. AVIF
  only, because WebP has no lossy 4:4:4 and JPEG XL does not subsample colour
  anyway. [`convert-comics`](convert-comics.md) asks for it by default.
