# `convert-images`

Batch-converts images to AVIF, WebP or JPEG XL (or back to JPEG), with optional
whitespace cropping and parallel encoding.

    convert-images [options] <inputDir> <outputDir>

| At a glance | |
| --- | --- |
| **Takes** | a folder of JPEG, PNG, WebP, AVIF, HEIC, JPEG XL, JPEG 2000, TIFF, BMP, GIF, TGA, PCX, Netpbm, PSD or ICO images |
| **Writes** | the converted images under `<outputDir>` |
| **Input** | never modified — so nothing is lost by pointing a run at an animated GIF, which is read for its first frame |
| **Network** | none |

## Default behavior

With no options, a run:

- writes **AVIF** at quality 60, speed 5, colour at a quarter resolution (4:2:0) (`-e`, `-l`, `-s`, `-u`)
- keeps every image at its own size: no resizing, no cropping (`-m`, `-c`)
- **skips an image that is already starved** ([below](#starved-images); `-a`)
- skips an image whose output already exists, so a rerun only does what is left
- picks the number of parallel encoders itself (`-j`)

## Formats

| `-e` | Writes | Notes |
| --- | --- | --- |
| `avif` (default) | AVIF | `-u 444` keeps colour at full resolution instead of a quarter of it: sharper coloured lines and lettering, files about a quarter larger. [`convert-comics`](convert-comics.md) asks for it by default |
| `webp` | WebP | no lossy 4:4:4, so `-u` does not apply |
| `jxl` | JPEG XL | does not subsample colour anyway |
| any, with `-r` | JPEG, from that format | [below](#starved-images) |

`-s` stays one knob across all three: each encoder's own effort setting is
derived from it, so a lower `-s` is a slower encode whichever format is being
written.

## Starved images

**An image that is already starved is left alone.** Whether a file is big
enough depends on its format, its pixel count and what is in it together, and
below that point whatever is left of the picture is all there is: encoding it
again spends a second generation of loss on something that had none to spare.
Such images are counted in the closing report and skipped.

- **The check is one header read per image**, not a decode: `identify -ping`
  stops at the header, which states the format, the pixel size and whether there
  is colour in the picture. What is *in* the picture — photograph, flat artwork,
  line art — would cost a full decode, so it is left unmeasured here and read at
  its most forgiving. An image nothing could measure is converted, not skipped.

| Run | Starved images |
| --- | --- |
| default | skipped |
| `-a` | converted regardless, which is what [`convert-comics`](convert-comics.md) asks for — inside one book every page has to come out in the same format |
| `-r` | never asked about: converting back to JPEG is about what can open the file, and a source too small to improve is exactly as unopenable as a large one |

---

**Options:** `convert-images -h` lists every option and its default. **Shared
rules:** [file safety](../file-safety.md) ·
[output never inside the input](../file-safety.md#the-output-folder-must-not-sit-inside-the-input) ·
[stopping a run](../file-safety.md#stopping-a-run) ·
[missing tools](../requirements.md#missing-tools-are-refused-up-front) ·
[what a run prints](../output.md)
