# `convert-video`

Re-encodes a folder of videos into a clean, uniform library, mirroring each
source's name and sub-folder into the output. Video goes to a modern codec
(AV1/x265, **always 10-bit**), audio to Opus (surround downmixed to stereo), and
subtitle/attachment/chapter/metadata streams are copied across. The input tree is never modified, empty
output folders are pruned at the end, and reruns are no-ops (an output that
already spans its input is skipped).

**Hardware acceleration** is detected at runtime, so the same command behaves
correctly on a server and at the desktop: an NVIDIA GPU with NVENC plus a
hardware profile encodes on NVENC, an Intel iGPU is used as decoder even for
software profiles, and missing hardware falls back gracefully to software.
**Parallelism is per file, not across files** — one file already saturates the
encoder, so files are processed one at a time and cut into chunks that are
re-concatenated transparently.

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

- **A source whose fields are woven is skipped, not upscaled.** The network
  would enlarge the combing into the picture, and a telecined film is combed in
  two frames of every five, which the ordinary interlace warning's majority
  verdict calls progressive. So `-u` asks for nearly every measured frame to be
  progressive. What fails it is left out of the run (not converted at all) so a
  later run that can deinterlace still gets to it.
- **An HDR source keeps its own size**: no upscaling network is trained on HDR.
- **Nothing degrades quietly.** Without a working upscaler `-u` is refused
  before anything is written, naming what is missing. Encoding the small files at
  their own size instead would leave outputs that every later run skips as
  finished.
- **The upscaled frame is judged at its new size**: the quality bias, the chunk
  count and `-t`'s adequacy test all go by the size it is enlarged to, and its
  colour signalling is the 709 matrix the upscaler hands it over in.

The upscaler is [VapourSynth](https://www.vapoursynth.com/) with
[vs-mlrt](https://github.com/AmusementClub/vs-mlrt)'s TensorRT plugin, driven
from an environment of its own under `upscaleHome` (default `~/vapoursynth`),
the way `read-library` drives its narration checkout. The default network is
[2xLiveActionV1_SPAN](https://openmodeldb.info/models/2x-LiveActionV1-SPAN), a
2x model trained on the damage real SD and HD video carries (compression,
chroma subsampling, scaling blur, halos) and deliberately not on noise, so it
keeps grain. `upscaleModel` names another `.onnx` in `models/`. On an RTX 5090
it runs at about 350 fps on DVD frames, so an SD-to-1080p run is as fast as the
encode alone. TensorRT compiles an engine per frame size the first time it
meets one, in seconds, and keeps it in `engines/`. Setting it up:

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

**`-c` crops the black bands off.** They are picture as far as an encoder is
concerned — scaled, filtered and coded like everything else — and a player that has
to letterbox anyway puts them back for free. But **a film does not have one shape**:
a feature with IMAX sequences opens up to a taller frame for them and closes again
afterwards, so a crop measured at any one place cuts the picture somewhere else. Two
dozen moments spread across the whole running time are measured instead, and only
the band *every* one of them agreed on comes off — the smallest common band, which
cannot cut a frame any sampled moment filled. The crop is **symmetric by
construction**, the same lines off the top and the bottom and the same columns off
each side, because taking more off one side re-centres the picture. Every
uncertainty resolves towards keeping pixels: a moment that could not be read is
skipped, a file too few of whose moments could be read is not cropped, and a band
too thin to be letterboxing is left on. A file keeping its Dolby Vision RPU is never
cropped — that RPU describes where the picture sits in the frame it was graded in.
`-m` then caps what is left, so a scope film stored in a 2160-line frame is judged
on its picture rather than on its bands.

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

**How hard each file is encoded is decided per file, not per preset.** A preset
states one quality level, and that level is then moved by the tier the file is
*encoded* at — a 2160p file two levels softer, an SD file two levels harder — since
the same number does not buy the same visible quality across the ladder. A source
capped by `-m` is judged by the size it comes out at, not the one it arrived at.
`-q` names a level yourself and turns the bias off.

**Film grain follows the source.** Every preset that synthesises grain measures each
file and synthesises what it measured, so a clean digital master and a 16mm blow-up
are not handed the same number; `av1Animation` synthesises none, which is the one case
where grain is actively wrong. `av1BluRay` is the default; `av1Grain` is the opt-in
for grainy sources: on an SVT-AV1-HDR build it **keeps the real grain**, with the
fork's film grain tune, instead of synthesising it, and without one it synthesises
it like `av1BluRay` at a lower quality level. `-g` names a level yourself and nothing caps it, and
`-g off` turns it off. This is **lossy and irreversible**: the grain is denoised out of
the stored picture and a player re-generates a similar-looking one, so what comes back
is an imitation of it.

**`-t` converts only what has room to save.** A re-encode is worth its hours when the
source has bits to spare, and worth nothing at all when it does not — a starved file
only comes back starved a generation further on. So `-t` measures each source's *video*
bitrate first (the audio is left out of it) and asks two questions before encoding
anything. Is the source at least **adequate** for what it is — its codec, frame size,
aspect ratio, frame rate and measured grain? If not, it is skipped: nothing here can
improve it. And would this run's own output still be adequate on half the bitrate? If
not, it is skipped too, with the figures it was judged on. `-t 30` is a looser run
(convert anything that can save 30%) and `-t 0` keeps only the starved check.

**Interlaced and anamorphic sources are reported, not corrected** — such a file
gets a warning and is encoded exactly as it arrived. Deinterlace or un-squeeze
upstream if you want that done. The one exception is an anamorphic file that
`-u` enlarges, which comes out with square pixels.

**HDR and Dolby Vision.** HDR10 metadata is preserved when present, and DV is
carried through wherever the encoder can code an RPU — the software encoders can,
NVENC cannot, and keeping DV needs ffmpeg 7.1 or newer. Which files really keep
it is decided per file, and the finished file is checked to still signal DV. A
**dual-layer profile 7** source — whose RPU no encoder can re-encode — is
normalised to single-layer profile 8.1 first, the same no-re-encode conversion
`ingest-movies` does, so it keeps its Dolby Vision without being ingested
beforehand; that needs `dovi_tool` and `mkvmerge`. Where DV cannot be kept (a
hardware profile, or a normalisation that could not run) the reason is reported
and the HDR10 layer is preserved, so such a file comes out as plain HDR10 rather
than losing its high dynamic range.

**HDR10+** is kept on the HEVC profiles. No encoder carries it through an encode,
so the source's dynamic metadata is read out before the encode and written back
into the finished video after it — the video is not re-encoded for it, and it
works with whichever ffmpeg the run settles on. It needs `hdr10plus_tool` and
`mkvmerge`. A source carrying both Dolby Vision and HDR10+ keeps both, profile 7
included. Where HDR10+ cannot be kept the reason is reported and the file comes
out as plain HDR10: on the AV1 profiles, which it cannot be written back into
yet; when a tool is missing, with a warning; and when the encode does not have
exactly the source's frames, or is cropped or scaled while the metadata places
windows in the original frame.

**The video encode is never lost to a failure of the cheap steps.** It costs
orders of magnitude more than everything else, so if the audio or the mux fails
the finished video is still written out as `<name> (video only).mkv` with a
warning, and a later rerun that completes properly supersedes and deletes it.
Only an incomplete video encode itself is discarded. Nor is it lost to the place
it was going: an output sub-folder deleted while the file was encoding is put
back to write into, and a name taken in the meantime by something else gets the
encode written beside it as `<name> (2).mkv` rather than over the top.

**A run can be paused and resumed from the keyboard.** Press `p` and every video
encoder of the moment stops where it is — however the work is being spread, over the
CPU's chunks or the GPU's engines — so the cores or the NVENC engines are free for
something else; press `r` and they all carry on from exactly where they stopped. This
frees **computation, not memory**: the encoders are stopped, not unloaded, so their RAM
and VRAM stay allocated, and the pause lives and dies with the run itself (there
is no pausing across a reboot, or from another terminal). Audio, one short
single-threaded process per track, deliberately keeps going. `Ctrl+C` still ends the
run, paused or not, and the time spent paused is reported separately and kept out of
the run's throughput figures. `ffmpeg` itself has no pause — the keys are the command's,
and what they move is the operating system's stop/continue signal.
