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

## On the GPU

**With [av1gpu](https://github.com/maddog107/av1gpu) installed, a plain AVIF
run encodes on the NVIDIA card first** and ImageMagick takes whatever is left.
Nothing needs asking for: the run decides per image, and an image the card
cannot do, or did not write, is encoded on the CPU exactly as without it.

| Goes to the GPU | Stays on the CPU |
| --- | --- |
| JPEG, PNG and WebP | every other input format |
| no alpha channel | a PNG or WebP with transparency |
| a JPEG in any orientation, a PNG or WebP stored upright | a PNG or WebP with an EXIF rotation (av1gpu would drop it) |
| already within `-m` | a picture `-m` shrinks |
| up to 35.6 megapixels | larger, CMYK or 12-bit JPEGs (av1gpu declines them) |

The GPU writes the same 10-bit AVIF as the CPU, at 4:2:0 or, with `-u 444`,
4:4:4. A run with `-c`, `-r` or another `-e` is encoded on the CPU throughout,
since the GPU encodes the whole picture as it is. If the
card cannot start the encoder at all — no driver, or no memory to spare — the
run warns once and carries on on the CPU.

**`-l` and `-s` mean what they mean on the CPU, whichever road an image
takes.** av1gpu has a quality scale of its own and seven presets where libaom
has ten speeds, so the numbers are not passed through but translated by a
measurement ([below](#calibrating)):

- **`-l`** becomes the av1gpu quality whose pictures score the same
  SSIMULACRA2 as the CPU's at that `-l` and `-s` — about four points higher on
  this machine.
- **`-s`** keeps its ranking: the default `-s 5` is av1gpu's default `turbo`,
  slower speeds spread towards `slow` and faster ones towards `turbo3`, each by
  how much smaller or larger the CPU's files are at that speed.

| `-s` | 2–3 | 4 | 5 | 6 | 7–9 |
| --- | --- | --- | --- | --- | --- |
| GPU preset | `slow` | `fast` | `turbo` | `turbo2` | `turbo3` |

Measured here, the GPU's files are smaller than the CPU's at every setting: at
equal SSIMULACRA2, `turbo3` is 6.6 % below CPU `-s 5` and `slow` 12.7 %, while
the CPU's own range runs from −1.9 % at `-s 2` to +26 % at `-s 9`. So a lower
`-s` still buys smaller files on the GPU, and either road's file is at least
as good as the CPU's would be. The GPU writes 8 bits where ImageMagick writes
10; the measurement scores the pictures, so that is already counted.

The images go to the card in batches of about 24 gigapixels — about a minute's
work — since each call costs about two seconds of start-up whatever it holds,
and progress is printed as each batch finishes. The closing report gives each
road's images, megapixels and speed:

    GPU: 873 image(s), 12287 MP in 34.4 seconds = 356.7 MP/s
    CPU: 88 image(s), 4184 MP in 360.6 seconds = 11.6 MP/s

`imageGpuEncoder` names the encoder, by name on `PATH` or by path; set it empty
(`imageGpuEncoder= convert-images …`) to encode everything on the CPU
([settings](../settings.md)).

### Calibrating

    convert-images -k <sampleDir> <workDir>

measures the translation above on this machine's encoders and saves it beside
the [settings file](../settings.md) (`av1gpu-calibration.json`), where every
later run finds it; until then the one measured when this was written is used.
Run it again after updating av1gpu or ImageMagick.

It cuts two 1280×1280 crops from each of up to 12 images in `<sampleDir>`,
encodes them at every `-s` from 2 to 9 and `-l` from 30 to 90 on the CPU and at
every preset and quality on the GPU, scores each encode against its crop with
[ssimulacra2_rs](https://github.com/rust-av/ssimulacra2_bin)
(`cargo install --locked --no-default-features ssimulacra2_rs`), and prints
what each setting costs and what the GPU will be asked for. `<sampleDir>` is
only read; the crops, the encodes and `scores.csv` stay in `<workDir>`. It took
20 minutes here and converts nothing. `-s 0` and `1` are not measured — libaom
takes minutes a crop there — and take `-s 2`'s answer.

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
[what a run prints](../output.md) ·
[settings](../settings.md)
