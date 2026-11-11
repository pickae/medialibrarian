"""What convert-video settles once, before the first file: the options checked
against each other, the grain level, the hardware it will encode and decode
on, and the summary it opens with.

Everything here runs in the parent before any work is handed out, so a run
that cannot go ahead says why and stops before it has touched a file.
"""

import subprocess

from medialib.cli import convert_video as rules
from medialib.cli import convert_video_probes as probes
from medialib.lib import (
    codecs,
    ffmpegselect,
    hostos,
    resolutions,
    videocrop,
    videograin,
)
from medialib.lib.runlog import log


def _validate(settings, video_args: str, grain: str,
              engines_override: str) -> str:
    """Every refusal that can be decided before a file is touched.

    All of them would otherwise fail per chunk, deep inside a parallel worker.
    """
    encoder = rules.encoder_of(rules.apply_video_quality(video_args))
    if settings.audio_profile == "opusCustom" \
            and not settings.custom_audio_bitrate:
        return ("The opusCustom audio profile requires an audio bitrate: "
                "-b <kbit/s>.")
    if settings.custom_audio_bitrate and settings.audio_profile != "opusCustom":
        return ("The -b audio bitrate only applies to the opusCustom profile "
                "(got -a %s)." % settings.audio_profile)
    if settings.custom_audio_bitrate and not _positive(
            settings.custom_audio_bitrate):
        return ('The -b audio bitrate must be a positive integer in kbit/s '
                '(got "%s").' % settings.custom_audio_bitrate)

    if settings.quality:
        if not rules.video_quality_flag(video_args):
            return ("The -l quality level only applies to the "
                    "constant-quality profiles; -p %s targets an average "
                    "bitrate instead." % settings.video_profile)
        top = rules.video_quality_max(video_args)
        if not settings.quality.isdigit() or int(settings.quality) > top:
            return ('The -l quality level must be an integer between 0 and %d '
                    'for -p %s (got "%s").'
                    % (top, settings.video_profile, settings.quality))

    if settings.max_resolution:
        named = resolutions.named(settings.max_resolution)
        if not named or not resolutions.ceiling(named):
            return ('Cannot scale to resolution tier "%s". Valid -m tiers: %s.'
                    % (settings.max_resolution, resolutions.spellings()))
        settings.max_resolution = named

    if settings.upscale_resolution:
        named = resolutions.named(settings.upscale_resolution)
        if not named or not resolutions.ceiling(named):
            return ('Cannot upscale to resolution tier "%s". Valid -u tiers: %s.'
                    % (settings.upscale_resolution, resolutions.spellings()))
        settings.upscale_resolution = named
        cap_box = resolutions.ceiling(settings.max_resolution or "")
        upscale_box = resolutions.ceiling(named)
        if cap_box and upscale_box:
            if upscale_box[0] > cap_box[0] or upscale_box[1] > cap_box[1]:
                return ("The -u upscale tier (%s) is above the -m ceiling (%s): "
                        "a file enlarged to it would only be scaled back down."
                        % (named, settings.max_resolution))

    saving = str(settings.required_saving)
    if not (saving.isdigit() and len(saving) <= 2):
        return ('The -t saving must be a whole percentage between 0 and 99 '
                '(got "%s").' % saving)

    if engines_override and "nvenc" not in encoder:
        return ("The -e NVENC engine count only applies to the *Nvenc video "
                "profiles (got -p %s)." % settings.video_profile)
    if engines_override and not _positive(engines_override):
        return ('The -e NVENC engine count must be a positive integer '
                '(got "%s").' % engines_override)

    if settings.fast_decode and encoder != "libsvtav1":
        return ("The -f fast-decode level only applies to the AV1 software "
                "profiles (got -p %s, which encodes with %s)."
                % (settings.video_profile, encoder))
    if settings.fast_decode and settings.fast_decode not in ("1", "2"):
        return ('The -f fast-decode level must be 1 or 2 (got "%s").'
                % settings.fast_decode)
    if grain and encoder != "libsvtav1":
        return ("The -g film grain level only applies to the AV1 software "
                "profiles (got -p %s, which encodes with %s)."
                % (settings.video_profile, encoder))
    if grain and grain != "off" and not (grain.isdigit()
                                         and 0 <= int(grain) <= 50):
        return ('The -g film grain level must be off, or an integer between 0 '
                'and 50 - where 0 asks for the per-source probe (got "%s").'
                % grain)
    settings.encoder = encoder
    return ""


def _positive(value: str) -> bool:
    return value.isdigit() and int(value) > 0


def _settle_grain(settings, grain: str, video_args: str) -> tuple:
    """The grain decision for the whole run.

    No -g leaves the profile's own default in force, an explicit level is what
    every file gets, "off" is none, and 0 is what asks for the per-file probe. The
    probe is skipped for the encoders that could not act on the answer; an explicit
    -g 0 is NOT skipped on any AV1 profile, whatever that profile's own default
    says, because asking for the probe is asking to be told what the source has.
    """
    software_av1 = settings.encoder == "libsvtav1"
    # The source's own grain is kept, so there is none to synthesise and nothing
    # to measure for it; -t still measures a source it tests, on its own.
    if settings.grain_tune:
        return "0", False
    if grain == "":
        default = videograin.grain_default_for(settings.video_profile)
        if default != "probe":
            return default, False
        return "0", software_av1
    if grain == "0":
        return "0", software_av1
    if grain == "off":
        return "0", False
    return grain, False


def _detect_hardware(settings, video_args: str, engines_override: str) -> None:
    """Whether hardware is used, decided ONCE by actually exercising ffmpeg rather
    than trusting a capability list: only a functional test reflects what this box
    can really do right now - drivers loaded, device reachable, engine present.

    Two independent things: NVENC encoding on a dedicated NVIDIA GPU, only when a
    *Nvenc profile is selected, and VAAPI decoding on an Intel iGPU, used whenever
    available for BOTH paths because hardware decode is a pure time win with no
    quality tradeoff.
    """
    settings.decode_accel, said = _decode_accel()
    log(said)

    if "nvenc" not in settings.encoder:
        settings.hardware_encode = False
        settings.nvenc_engines = 1
        log("Software encode: %s on the CPU." % settings.encoder)
    else:
        settings.hardware_encode = True
        if not probes.nvenc_works(settings.encoder):
            log("The %s profile needs a working %s encoder, which this machine "
                "does not provide." % (settings.video_profile,
                                       settings.encoder))
            raise SystemExit(1)
        if not probes.nvenc_tune_works(settings.encoder,
                                       rules.NVENC_TUNE_WANTED):
            settings.nvenc_tune = rules.NVENC_TUNE_FALLBACK
        if engines_override:
            settings.nvenc_engines = int(engines_override)
            log("Hardware encode: %s on NVENC across %d engine(s) (from -e)."
                % (settings.encoder, settings.nvenc_engines))
        else:
            name = _gpu_name()
            settings.nvenc_engines = rules.nvenc_engines_for(name)
            if name:
                log('Hardware encode: %s on NVENC across %d engine(s) (guessed '
                    'for "%s"; override with -e).'
                    % (settings.encoder, settings.nvenc_engines, name))
            else:
                log("WARNING: nvidia-smi is unavailable - the NVENC engine "
                    "count is a blind guess of %d." % settings.nvenc_engines)
                log("         Use -e <count> to size the parallelism for your "
                    "card.")
                log("Hardware encode: %s on NVENC across %d engine(s) (the "
                    "guess above)." % (settings.encoder, settings.nvenc_engines))
        if settings.nvenc_tune == rules.NVENC_TUNE_WANTED:
            log("NVENC tuning: -tune %s, which enables lookahead and the "
                "temporal filter by itself." % settings.nvenc_tune)
        else:
            log("NVENC tuning: -tune %s - this ffmpeg/driver/GPU will not take "
                "-tune %s, so the profile falls back to it."
                % (settings.nvenc_tune, rules.NVENC_TUNE_WANTED))
        settings.nvenc_master_display = probes.encoder_takes_master_display(
            settings.encoder)

    settings.dv_encoder_support = probes.encoder_takes_dolby_vision(
        settings.encoder)


def _decode_accel() -> tuple[str, str]:
    """The hardware DECODE flags this run uses, and the line that says so.

    A ladder of one rung per platform's own interface, each behind a probe
    that opens the device for real:

      * VAAPI on an Intel iGPU, which is the Linux case and the original one;
      * VideoToolbox on macOS, where there is no ``/dev/dri`` to walk and the
        iGPU is reached through the framework instead - and where it is worth
        reaching for on every Mac, since Apple Silicon has no discrete GPU to
        fall back on;
      * nothing, and the decoding happens on the CPU.

    Hardware decode is used wherever it is found and for both encode paths,
    because it is a pure time win with no quality tradeoff - the frames come
    back to system memory either way.

    The macOS rung is asked only on macOS: the probe costs an ffmpeg start,
    and no other platform has the framework to find.
    """
    node = probes.intel_render_node()
    if node:
        return ("-hwaccel vaapi -hwaccel_device " + node,
                "Hardware decode: Intel iGPU via VAAPI (%s)." % node)
    if hostos.is_macos() and probes.videotoolbox_works():
        return ("-hwaccel videotoolbox",
                "Hardware decode: the GPU via VideoToolbox.")
    return ("", "Hardware decode: no usable iGPU found, decoding in software.")


def _gpu_name() -> str:
    try:
        done = subprocess.run(["nvidia-smi", "--query-gpu=name",
                               "--format=csv,noheader"],
                              stdin=subprocess.DEVNULL,
                              stdout=subprocess.PIPE,
                              stderr=subprocess.DEVNULL)
    except OSError:
        return ""
    lines = done.stdout.decode("utf-8", "replace").splitlines()
    return lines[0].strip() if lines else ""


def _summarise(settings, video_args: str, grain: str) -> None:
    """Restate the decisions taken, so a run is self-explanatory.

    Stated unconditionally where the other scripts only speak up when a choice
    overruled PATH: a build that silently drops the psychovisual keys produces a
    perfectly good encode that is simply not the one that was asked for, and the
    alternative to this line is finding that out from the file.
    """
    ffmpegselect.report_ffmpeg_selection(always=True)
    if ffmpegselect.selected_full() != 1:
        log("WARNING: this ffmpeg does not accept every argument of -p %s - "
            "either it is too old for some of them, or the hardware to test "
            "them on is missing. Encoding anyway: an encoder parameter it does "
            "not recognise is dropped, so the result may not be the encode the "
            "profile describes." % settings.video_profile)

    settled = rules.apply_nvenc_tune(
        rules.apply_video_quality(video_args, given=settings.quality_given,
                                  quality=settings.quality),
        settings.nvenc_tune)
    if settings.svt_av1_hdr:
        settled = rules.svt_av1_hdr_args(settled, settings.grain_tune)
        log("SVT-AV1-HDR: this ffmpeg's libsvtav1 is SVT-AV1-HDR, so the "
            "profile runs without qm-min=0 (the fork keeps its own minimum) and "
            "a PQ source gets its PQ variance-boost curve. The quality levels "
            "are tuned for mainline SVT-AV1, so sizes will not match a mainline "
            "encode.")
    elif settings.encoder == "libsvtav1" and not settings.svt_build_note:
        log("SVT-AV1-HDR: not found, encoding with this ffmpeg's own "
            "libsvtav1.")
    if settings.encoder == "libsvtav1" and settings.svt_build_note:
        log("SVT-AV1-HDR: " + settings.svt_build_note + ".")
    log("Video profile: %s -> %s" % (settings.video_profile, settled))

    # What the run leaves alone is named on one row, with the flag that turns
    # each on: a row apiece would bury the few that change the encode.
    off = []

    if settings.crop_wanted:
        log("Crop: on (-c) - each source is measured at %d moments across its "
            "running time and only the smallest black bands any of them found "
            "come off, symmetrically.%s"
            % (videocrop.CROP_PROBE_SAMPLES,
               " A file keeping its Dolby Vision RPU is left whole."
               if settings.dv_encoder_support else ""))
    else:
        off.append("crop (-c)")

    ceiling = (resolutions.ceiling(settings.max_resolution)
               if settings.max_resolution else None)
    # The validation refused a tier with no ceiling long before this, so the
    # guard is here to keep that true rather than because it can fail today.
    if ceiling:
        # With -u a smaller source is enlarged instead, which its own row says.
        log("Resolution: capped at %s (%dx%d), aspect ratio kept%s."
            % (settings.max_resolution, ceiling[0], ceiling[1],
               "" if settings.upscale_stack is not None
               else "; a smaller source keeps its own size"))
    else:
        off.append("resolution ceiling (-m)")

    if settings.upscale_stack is not None:
        target = resolutions.ceiling(settings.upscale_resolution) or (0, 0)
        log("Upscale: a picture smaller than %s (%dx%d) is enlarged to fill it "
            "with %s, at the shape it is displayed at and with square pixels. "
            "An interlaced or telecined source is skipped, and an HDR one keeps "
            "its own size."
            % (settings.upscale_resolution, target[0], target[1],
               settings.upscale_stack.describe()))
    else:
        off.append("upscale (-u)")

    # Where the quality level comes from, stated because the bias is otherwise
    # invisible: the profile row above shows the unbiased level, not the one a
    # 2160p file will get.
    if not rules.video_quality_flag(settled):
        log("Video quality: none to set - %s targets an average bitrate, not a "
            "quality level." % settings.video_profile)
    elif settings.quality_given:
        log("Video quality: %s %s for every file (-l), so no resolution bias "
            "is applied." % (rules.video_quality_flag(settled),
                             settings.quality))
    else:
        log("Video quality: the %s profile default above, biased per file by "
            "the tier it is ENCODED at (%s; override with -l)."
            % (settings.video_profile,
               rules.quality_bias_spellings(settings.max_resolution)))

    _summarise_grain(settings, grain)

    if settings.fast_decode:
        log("Fast decode: level %s, trading some compression for a cheaper "
            "decode." % settings.fast_decode)
    # The one encoder that has the setting; for the others it is not "off".
    elif settings.encoder == "libsvtav1":
        off.append("fast decode (-f)")

    if settings.test_source_bitrate:
        log("Bitrate test: on (-t) - each source is measured first, and only a "
            "video that is not already starved AND whose %s encode would still "
            "be adequate on %s%% less bitrate is converted. Everything else is "
            "skipped with its figures."
            % (codecs.encoder_codec(settings.encoder),
               settings.required_saving))
    else:
        off.append("bitrate test (-t)")

    if off:
        log("Off: %s." % ", ".join(off))

    # Dolby Vision and HDR10+ are not summarised here: whether they can be kept
    # depends on what a source carries, so each file that has either says on
    # its own line what happens to it, and a run of files without them says
    # nothing about them at all.

    if settings.keep_tracks:
        log("Other tracks: kept as the source has them (-k) - audio, "
            "subtitles, attachments, chapters and tags combined with the "
            "encode by mkvmerge")
    elif settings.audio_profile == "passthrough":
        log("Audio profile: passthrough (source audio copied through, not "
            "re-encoded)")
    elif settings.custom_audio_bitrate:
        log("Audio profile: %s (%s kbit/s Opus applied to every track, "
            "surround downmixed to stereo)"
            % (settings.audio_profile, settings.custom_audio_bitrate))
    else:
        log("Audio profile: %s (per-channel Opus bitrate from the shared "
            "table, surround downmixed to stereo)" % settings.audio_profile)

    if settings.hardware_encode:
        engines = settings.nvenc_engines
        log("Video parallelism: hardware - %d NVENC engine(s), so %d chunk(s) "
            "per file (files run one at a time), with split encode disabled so "
            "the engines are not also striping single frames."
            % (engines, engines))
        # Said up front so an allocation failure reads as a memory ceiling
        # rather than a broken profile - and only where there is both more than
        # one session and a 4K encode for them to run out on.
        if engines > 1 and (not ceiling or ceiling[1] >= 2160):
            log("Note: %d encode session(s) run at once, so GPU memory is the "
                "ceiling on 4K sources - a driver allocation failure there "
                "means lowering -e, not a broken profile." % engines)
    else:
        log("Video parallelism: software - resolution-driven chunks per file, "
            "scaled to %d core(s) (files run one at a time)." % settings.cores)


def _summarise_grain(settings, grain: str) -> None:
    if settings.encoder != "libsvtav1":
        log("Film grain: not available with %s, none synthesised."
            % settings.encoder)
    elif settings.grain_tune:
        log("Film grain: KEPT - %s on SVT-AV1-HDR encodes the source's own "
            "grain with the fork's film grain tune (tune 6) rather than "
            "denoising it and synthesising it again; nothing is measured for "
            "it (-g puts the profile back on synthesis)."
            % settings.video_profile)
    elif settings.grain_probe_wanted and grain:
        log("Film grain: -g 0 - measured per file and synthesised as measured "
            "(lossy: the source grain is denoised away and re-generated at "
            "playback).")
    elif settings.grain_probe_wanted:
        log("Film grain: measured per file and synthesised as measured, the %s "
            "default (-g pins one level for every file, -g off none). Lossy: "
            "the source grain is denoised away and re-generated at playback."
            % settings.video_profile)
    elif int(settings.grain_level or 0) > 0 and grain:
        log("Film grain: level %s for every file (-g), regardless of source or "
            "profile (lossy: the source grain is denoised away and re-generated "
            "at playback)." % settings.grain_level)
    elif int(settings.grain_level or 0) > 0:
        log("Film grain: level %s for every file, the %s default (-g pins "
            "another level, -g 0 measures each source, -g off none). Lossy: the "
            "source grain is denoised away and re-generated at playback."
            % (settings.grain_level, settings.video_profile))
    elif not grain:
        log("Film grain: none - the %s default is 0 (-g 0 measures each source "
            "instead)." % settings.video_profile)
    else:
        log("Film grain: off (-g off), none synthesised.")
