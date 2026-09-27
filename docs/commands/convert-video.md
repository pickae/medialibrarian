# `convert-video`

Re-encodes a folder of videos into a clean, uniform library: video to AV1 or
x265 (**always 10-bit**), audio to Opus, everything else copied across.

    convert-video [options] <inputDir> <outputDir>

| At a glance | |
| --- | --- |
| **Takes** | a folder of videos |
| **Writes** | the mirrored tree under `<outputDir>`: each source's name and sub-folder, re-encoded |
| **Input** | never modified |
| **Reruns** | a no-op: an output that already spans its input is skipped |
| **Network** | none |

Audio goes to Opus (surround downmixed to stereo), and subtitle, attachment,
chapter and metadata streams are copied across. Empty output folders are pruned
at the end.

**Contents:** [Options that change the encode](#options-that-change-the-encode) ·
[Hardware and parallelism](#hardware-and-parallelism) ·
[Picture size](#picture-size--m-and--u) · [Setting up the upscaler](#setting-up-the-upscaler) ·
[Cropping](#cropping--c) · [Quality and grain](#quality-and-grain) ·
[Only what has room to save](#only-what-has-room-to-save--t) ·
[HDR and Dolby Vision](#hdr-and-dolby-vision) · [Which ffmpeg](#which-ffmpeg) ·
[Failures and pausing](#failures-and-pausing)

## Options that change the encode

Apart from the preset, everything here is off unless asked for: without it every
file is encoded at its own size, at the preset's quality, with the grain it
measured.

| Option | Effect | Details |
| --- | --- | --- |
| `-p <profile>` | the video preset; `av1BluRay` is the default, a `*Nvenc` one encodes on the GPU | [Quality and grain](#quality-and-grain) |
| `-m <tier>` | caps the resolution; never scales up | [Picture size](#picture-size--m-and--u) |
| `-u <tier>` | upscales what is smaller, with a neural network on the GPU | [Picture size](#picture-size--m-and--u) |
| `-c` | crops the black bands off | [Cropping](#cropping--c) |
| `-q <level>` | names the quality level yourself, and turns the per-file bias off | [Quality and grain](#quality-and-grain) |
| `-g <level\|off>` | names the film grain yourself, or turns it off | [Quality and grain](#quality-and-grain) |
| `-t [percent]` | converts only what has room to save | [Only what has room to save](#only-what-has-room-to-save--t) |

## Hardware and parallelism

**Hardware acceleration** is detected at runtime, so the same command behaves
correctly on a server and at the desktop:

| Found | Used for |
| --- | --- |
| an NVIDIA GPU with NVENC, plus a hardware profile | encoding on NVENC |
| an Intel iGPU | decoding, even for software profiles |
| neither | software, gracefully |

**Parallelism is per file, not across files** — one file already saturates the
encoder, so files are processed one at a time and cut into chunks that are
re-concatenated transparently.

## Picture size (`-m` and `-u`)

**Resolution is a ceiling, not a target.** `-m` caps the output at a resolution
tier — named either by its line count (`720p` … `4320p`) or by a marketing name
(`fullHD`, `2K`, `4K`, `UltraHD`, `8K`, …), in any case — and everything above it
is scaled down to fit with its aspect ratio kept, so a 2.39:1 scope film capped at
1080p comes out 1920x800 rather than letterboxed. Anything already at or below the
tier is encoded at its own size: **`-m` never scales anything up.** The tiers are
the same ones `content-census-bi` reports a library by.

**`-u` upscales what is smaller, and only when asked.** `-u 1080p` enlarges every
picture smaller than 1080p to fill it, with a neural upscaler on the GPU rather
than a resize, and leaves the rest alone, so `-m 2160p -u 1080p` caps the 4K files
and brings the DVDs up in one run. The target is worked out from the shape a
picture is *displayed* at, after any `-c` crop, so an anamorphic DVD comes out
with square pixels at its real shape: a 16:9 disc becomes 1920x1080, a 4:3 one
1440x1080, and a letterboxed film inside a 4:3 frame is cropped first and fills
the width. A picture already filling the tier on one side, or short of it by less
than a tenth, is not touched.

- **The upscaled frame is judged at its new size**: the quality bias, the chunk
  count and `-t`'s adequacy test all go by the size it is enlarged to, and its
  colour signalling is the 709 matrix the upscaler hands it over in.
- **Nothing degrades quietly.** Without a working upscaler `-u` is refused
  before anything is written, naming what is missing. Encoding the small files at
  their own size instead would leave outputs that every later run skips as
  finished.

**Sources that are not corrected.** Interlaced and anamorphic sources are
reported, not corrected — such a file gets a warning and is encoded exactly as it
arrived. Deinterlace or un-squeeze upstream if you want that done.

| Source | Without `-u` | With `-u` |
| --- | --- | --- |
| interlaced | warned about, encoded as it arrived | **skipped, not upscaled** (below) |
| anamorphic | warned about, encoded as it arrived | comes out with square pixels |
| HDR | — | **keeps its own size**: no upscaling network is trained on HDR |

A source whose fields are woven would have the combing enlarged into the
picture, and a telecined film is combed in two frames of every five, which the
ordinary interlace warning's majority verdict calls progressive. So `-u` asks
for nearly every measured frame to be progressive. What fails it is left out of
the run (not converted at all) so a later run that can deinterlace still gets to
it.

### Setting up the upscaler

The upscaler is [VapourSynth](https://www.vapoursynth.com/) with
[vs-mlrt](https://github.com/AmusementClub/vs-mlrt)'s TensorRT plugin, driven
from an environment of its own, the way `read-library` drives its narration
checkout. It needs an NVIDIA GPU.

| Setting | Default | What it is |
| --- | --- | --- |
| `upscaleHome` | `~/vapoursynth` | the environment: `venv/`, `plugins/`, `models/`, `engines/` |
| `upscaleModel` | 2xLiveActionV1_SPAN | another `.onnx` in `models/` |

The default network is
[2xLiveActionV1_SPAN](https://openmodeldb.info/models/2x-LiveActionV1-SPAN), a
2x model trained on the damage real SD and HD video carries (compression,
chroma subsampling, scaling blur, halos) and deliberately not on noise, so it
keeps grain. On an RTX 5090 it runs at about 350 fps on DVD frames, so an
SD-to-1080p run is as fast as the encode alone. TensorRT compiles an engine per
frame size the first time it meets one, in seconds, and keeps it in `engines/`.

```bash
python3 -m venv ~/vapoursynth/venv
~/vapoursynth/venv/bin/pip install vapoursynth==79 vapoursynth-bestsource==21.0 onnx onnxconverter-common
~/vapoursynth/venv/bin/pip install --extra-index-url https://pypi.nvidia.com tensorrt-cu13
~/vapoursynth/venv/bin/vapoursynth config
```

then build vs-mlrt's `vstrt` plugin against that TensorRT (vs-mlrt publishes no
Linux binary) into `~/vapoursynth/plugins/libvstrt.so`, and put the network's
`.onnx` in `~/vapoursynth/models/`. VapourSynth stays at R79 because R80 dropped
the plugin interface `vstrt` is written against, and bestsource 21 is the last
release that accepts R79. The network is non-commercial (CC BY-NC-SA 4.0), which
is why it is fetched rather than shipped.

## Cropping (`-c`)

**`-c` crops the black bands off.** They are picture as far as an encoder is
concerned — scaled, filtered and coded like everything else — and a player that has
to letterbox anyway puts them back for free. But **a film does not have one shape**:
a feature with IMAX sequences opens up to a taller frame for them and closes again
afterwards, so a crop measured at any one place cuts the picture somewhere else.

- **Measured across the whole film.** Two dozen moments spread across the whole
  running time are measured, and only the band *every* one of them agreed on
  comes off — the smallest common band, which cannot cut a frame any sampled
  moment filled.
- **Symmetric by construction**: the same lines off the top and the bottom and
  the same columns off each side, because taking more off one side re-centres
  the picture.
- **Every uncertainty keeps pixels**: a moment that could not be read is
  skipped, a file too few of whose moments could be read is not cropped, and a
  band too thin to be letterboxing is left on.
- **Never with a Dolby Vision RPU**: a file keeping its RPU is never cropped —
  that RPU describes where the picture sits in the frame it was graded in.

`-m` then caps what is left, so a scope film stored in a 2160-line frame is judged
on its picture rather than on its bands.

## Quality and grain

**How hard each file is encoded is decided per file, not per preset.** A preset
states one quality level, and that level is then moved by the tier the file is
*encoded* at — a 2160p file two levels softer, an SD file two levels harder — since
the same number does not buy the same visible quality across the ladder. A source
capped by `-m` is judged by the size it comes out at, not the one it arrived at.
`-q` names a level yourself and turns the bias off.

**Film grain follows the source.** Every preset that synthesises grain measures each
file and synthesises what it measured, so a clean digital master and a 16mm blow-up
are not handed the same number.

| Preset | Grain |
| --- | --- |
| `av1BluRay` (default) | measured per file and synthesised |
| `av1Grain` | for grainy sources: on an SVT-AV1-HDR build it **keeps the real grain**, with the fork's film grain tune, instead of synthesising it; without one it synthesises it like `av1BluRay` at a lower quality level |
| `av1Animation` | none — the one case where grain is actively wrong |
| any, with `-g <level>` | that level, and nothing caps it |
| any, with `-g off` | none |

Synthesis is **lossy and irreversible**: the grain is denoised out of the stored
picture and a player re-generates a similar-looking one, so what comes back is an
imitation of it.

## Only what has room to save (`-t`)

**`-t` converts only what has room to save.** A re-encode is worth its hours when the
source has bits to spare, and worth nothing at all when it does not — a starved file
only comes back starved a generation further on. So `-t` measures each source's *video*
bitrate first (the audio is left out of it) and asks two questions before encoding
anything:

1. Is the source at least **adequate** for what it is — its codec, frame size,
   aspect ratio, frame rate and measured grain? If not, it is skipped: nothing
   here can improve it.
2. Would this run's own output still be adequate on half the bitrate? If not, it
   is skipped too, with the figures it was judged on.

`-t 30` is a looser run (convert anything that can save 30%) and `-t 0` keeps only
the starved check.

## HDR and Dolby Vision

Which files really keep their HDR is decided per file, and whatever cannot be
kept falls back to the HDR10 layer, so a file never loses its high dynamic range:

| Source | Kept | Needs | Where it cannot be kept |
| --- | --- | --- | --- |
| HDR10 | whenever present | — | — |
| Dolby Vision | wherever the encoder can code an RPU: the software encoders can, NVENC cannot. The finished file is checked to still signal DV | ffmpeg 7.1 or newer | the reason is reported and the file comes out as plain HDR10 |
| Dolby Vision, dual-layer profile 7 | normalised to single-layer profile 8.1 first — the same no-re-encode conversion `ingest-movies` does, so it keeps its Dolby Vision without being ingested beforehand | `dovi_tool`, `mkvmerge` | plain HDR10 |
| HDR10+ | on the HEVC profiles; a source carrying both Dolby Vision and HDR10+ keeps both, profile 7 included | `hdr10plus_tool`, `mkvmerge` | plain HDR10 (below) |

**HDR10+** is carried through no encoder, so the source's dynamic metadata is
read out before the encode and written back into the finished video after it —
the video is not re-encoded for it, and it works with whichever ffmpeg the run
settles on. Where it cannot be kept the reason is reported: on the AV1 profiles,
which it cannot be written back into yet; when a tool is missing, with a
warning; and when the encode does not have exactly the source's frames, or is
cropped or scaled while the metadata places windows in the original frame.

## Which ffmpeg

**A newer ffmpeg is preferred if the preset needs one.** The AV1 presets ask for
psychovisual SVT-AV1 parameters and the NVENC ones for `uhq` tuning, which a
distribution's ffmpeg is often a year or two too old to do — and a too-old SVT-AV1
**drops a parameter it does not know instead of failing**, so a build that applies
half the tuning looks exactly like one that applies all of it. So each candidate
build (see [Which ffmpeg a run uses](../requirements.md#which-ffmpeg-a-run-uses)) is asked to encode a single frame with
the preset's own arguments, and the first that takes all of them is used. The run
says which build it settled on and warns when that build cannot do everything the
preset asks.

**An SVT-AV1-HDR build is preferred where there is one.**
[SVT-AV1-HDR](https://github.com/juliobbv-p/svt-av1-hdr) is SVT-AV1 with
perceptual tuning of its own, shipped inside community ffmpeg builds rather than as
an encoder of its own. Among the builds that take the whole preset, one whose SVT-AV1
is SVT-AV1-HDR wins; without one, the choice is the one it always was. On it the AV1
presets leave the fork's own quantisation-matrix floor alone, and a PQ source gets
its PQ-specific curve by itself. The quality levels are still the ones tuned for
mainline SVT-AV1, and the fork spends bits differently, so sizes differ from a
mainline encode of the same preset.

## Failures and pausing

**The video encode is never lost to a failure of the cheap steps.** It costs
orders of magnitude more than everything else, so if the audio or the mux fails
the finished video is still written out as `<name> (video only).mkv` with a
warning, and a later rerun that completes properly supersedes and deletes it.
Only an incomplete video encode itself is discarded. Nor is it lost to the place
it was going: an output sub-folder deleted while the file was encoding is put
back to write into, and a name taken in the meantime by something else gets the
encode written beside it as `<name> (2).mkv` rather than over the top.

**A run can be paused and resumed from the keyboard.**

| Key | Effect |
| --- | --- |
| `p` | every video encoder of the moment stops where it is — however the work is being spread, over the CPU's chunks or the GPU's engines — so the cores or the NVENC engines are free for something else |
| `r` | they all carry on from exactly where they stopped |
| `Ctrl+C` | still ends the run, paused or not |

This frees **computation, not memory**: the encoders are stopped, not unloaded, so
their RAM and VRAM stay allocated, and the pause lives and dies with the run itself
(there is no pausing across a reboot, or from another terminal). Audio, one short
single-threaded process per track, deliberately keeps going. The time spent paused
is reported separately and kept out of the run's throughput figures. `ffmpeg`
itself has no pause — the keys are the command's, and what they move is the
operating system's stop/continue signal.

---

**Options:** `convert-video -h` lists every option and its default. **Shared
rules:** [file safety](../file-safety.md) ·
[output never inside the input](../file-safety.md#the-output-folder-must-not-sit-inside-the-input) ·
[stopping a run](../file-safety.md#stopping-a-run) ·
[pausing a run](../file-safety.md#pausing-a-run) ·
[missing tools](../requirements.md#missing-tools-are-refused-up-front) ·
[which ffmpeg](../requirements.md#which-ffmpeg-a-run-uses)
