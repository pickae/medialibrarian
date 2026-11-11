"""convert-video's run: what it settles at startup, and what it does per file.

The decisions - the profile tables, the quality level, the chunk plan, the
argument string, the -t test - are `medialib/cli/convert_video.py`. This is the
run around them: the per-file plan and its look-ahead, and the closing report.
What it settles once is `convert_video_startup.py`; what it asks the tools,
`convert_video_probes.py`; the Dolby Vision and HDR10+ side-work,
`convert_video_hdr.py`; and the encodes and the mux, `convert_video_encode.py`.

The order of the per-file work is the point of it. The video encode is by far the
most expensive thing here and lives in RAM until the mux writes it out, so it is
never lost to a failure of the cheap parts: if the audio, or the mux of the
subtitles, attachments, chapters and metadata, fails, the finished video is still
written to disk on its own and marked " (video only)".
"""

import os
import shutil
import sys
import threading
import time

from medialib import commands
from medialib.cli import convert_video as rules
from medialib.cli import convert_video_encode as encode
from medialib.cli import convert_video_hdr as hdr
from medialib.cli import convert_video_probes as probes
from medialib.cli import convert_video_source as videosource
from medialib.cli import convert_video_startup as startup
from medialib.lib import (
    clioptions,
    console,
    durationcheck,
    dynamicqueue,
    ffmpegselect,
    formatting,
    pausecontrol,
    ramscratch,
    resolutions,
    runlog,
    safety,
    scenecuts,
    segments,
    settings,
    statusline,
    tooldeps,
    upscale,
    videocrop,
    videograin,
    workerpool,
)
from medialib.lib.runlog import log

# How many files to convert are kept prepared ahead of the one encoding. Files
# the preparation passes over - up to date, or not worth converting - do not
# count, so this is two encodes' worth of slack for a preparation that runs
# long, at the cost of nothing more than their probes being done early: the
# one thing a prepared file can hold that is large, a Dolby Vision
# intermediate, has a slot of its own (Run.dv_slot).
LOOKAHEAD = 2


# --- one file -----------------------------------------------------------------

class Plan:
    """One file, prepared: everything settled about it before its encode starts.

    The file after the one encoding is prepared while that one runs, so what
    varies from file to file cannot live on the run's Settings - a chunk worker
    of the file encoding would read the NEXT file's crop and grain. Each file is
    handed a Settings of its own instead, and that copy is what its workers get.
    """

    def __init__(self, relative: str, settings) -> None:
        self.relative = relative
        self.settings = settings
        self.source = os.path.join(settings.input_dir, relative)
        self.output = rules.output_path_of(relative, settings.input_dir,
                                           settings.output_dir)
        self.directory = segments.chunk_dir_for(settings.chunk_root, relative)
        self.duration = 0.0
        # The picture's own length, which can be shorter than the container's.
        self.video_duration = 0.0
        self.replacing = False
        self.size_text = ""
        # The chunk boundaries, start to end, or [] for a file encoded whole,
        # and what each interior one was moved onto.
        self.bounds: list = []
        self.cut_kinds: list = []
        # The scratch holding a Dolby Vision intermediate, and whether this file
        # holds the run's one slot for it.
        self.scratch = ""
        self.holds_dv_slot = False
        self.source_dv_profile = ""
        # The source's HDR10+ metadata read out for the encode to be given back,
        # or "" when this file does not keep it.
        self.hdr10plus_metadata = ""


def chunk_bounds(duration: float, count: int) -> list:
    """Where a file of <duration> is cut for <count> chunks, from 0 to the end,
    or [] when it is encoded whole."""
    if int(duration) < 2 or count <= 1:
        return []
    # Never make sub-second chunks.
    count = min(count, int(duration))
    bounds = (["0"] + rules.equal_boundaries(duration, count)
              + ["%.3f" % duration])
    return bounds if len(bounds) > 2 else []


class Run:
    """The run's mutable state: what the closing report is read out of."""

    def __init__(self, settings) -> None:
        self.settings = settings
        self.start = time.time()
        self.converted = 0
        self.skipped = 0
        self.failed = 0
        self.frames = 0
        self.video_seconds = 0.0
        # A Dolby Vision intermediate is most of a film's video stream, and the
        # file prepared ahead must not hold a second one beside the encoding
        # file's: whoever converts one takes this, and the encode that is done
        # reading it hands it back.
        self.dv_slot = threading.Semaphore(1)
        self.tally = threading.Lock()

    def prepare(self, relative: str) -> "Plan | None":
        """ONE video's preparation: resume-skip an up-to-date output, ask -t
        whether it is worth converting, and settle the grain, the Dolby Vision
        mode, the crop, the HDR10+ and the chunk boundaries. None for a file
        that is not converted.
        """
        settings = rules.Settings(**self.settings.__dict__)
        plan = Plan(relative, settings)
        source = plan.source

        # A pause holds the NEXT file off too: preparing it is ffmpeg runs of
        # its own that would otherwise start up on a machine whose CPU was just
        # handed back.
        pausecontrol.wait_while_paused()

        plan.duration = duration = videosource._media_duration(source)
        plan.video_duration = videosource._video_duration(source, duration)
        # An output already here is the out-of-date result of an earlier run, and
        # replacing it is the whole point of converting this file again. Remembered
        # now, because after the encode a file at that path is no longer
        # distinguishable from one that turned up while the encode ran.
        #
        # Up to date means as long as the source by the measure a finished
        # mux is accepted by: compared with no slack at all, an output whose
        # audio ends a few milliseconds before the source's does is "short",
        # and every rerun encodes the whole library again; compared with less
        # slack than the length check allows, an output that check let through
        # is encoded again every run. An output cut short by more still counts
        # as not done.
        plan.replacing = os.path.isfile(plan.output)
        if plan.replacing:
            out_duration = videosource._media_duration(plan.output)
            if duration > 0 and durationcheck.length_matches(duration,
                                                             out_duration):
                log("Up to date, skipping: " + relative)
                self.count("skipped")
                return None

        directory = plan.directory
        os.makedirs(directory, exist_ok=True)

        # The source's coded size, and the size it will actually be ENCODED at -
        # the same unless -r caps this one. The encoded size is what the chunk
        # planning goes by: a 2160p source scaled down to 1080p does a 1080p
        # file's worth of encoding work. Settled a second time once -c has
        # measured the file, since a crop moves it too.
        width, height, _order, sar = videosource.video_dimensions(source)
        enc_width, enc_height = resolutions.capped(width, height,
                                                   settings.max_resolution)
        # With -u the -t test judges the size the file would be enlarged to.
        # The crop is not measured yet, so this is the uncropped picture's.
        enlarged = resolutions.enlarged(width, height,
                                        settings.upscale_resolution, sar)
        if enlarged:
            enc_width, enc_height = enlarged

        # Film grain: measure THIS source, so a clean file is not given grain it
        # never had and a grainy one gets as much as it actually has.
        source_grain = ""
        if settings.grain_probe_wanted:
            pausecontrol.wait_while_paused()
            settings.grain_level = videograin.grain_level_for(
                source, relative, videosource._media_duration,
                videosource._dimensions_line, runlog.jobs_per_core,
                settings.decode_accel)
            source_grain = settings.grain_level

        if settings.test_source_bitrate:
            if not source_grain:
                pausecontrol.wait_while_paused()
                source_grain = videograin.source_grain_for(
                    source, relative, videosource._media_duration,
                    videosource._dimensions_line, runlog.jobs_per_core,
                    settings.decode_accel)
            if not rules.conversion_worthwhile(relative, width, height,
                                               enc_width, enc_height,
                                               source_grain, settings):
                shutil.rmtree(directory, ignore_errors=True)
                self.count("skipped")
                return None

        # Checked between the probes, because Ctrl+C only kills the ffmpeg
        # running at that moment: the run waits for this thread on its way out,
        # and every probe started after the interrupt would run to its end.
        if safety.abort_requested():
            return None
        fields = videosource.interlace_counts(source, settings.decode_accel)

        if safety.abort_requested():
            return None
        self._settle_dolby_vision(plan)
        if safety.abort_requested():
            self._release_dv(plan)
            return None

        # After the Dolby Vision decision, because a file that keeps its RPU
        # cannot be cropped, and after the -t test, so a source this run is not
        # going to convert is not measured for a crop it will never use.
        settings.crop = self._settle_crop(relative, source, settings)
        crop_width, crop_height = rules.cropped_size(width, height,
                                                     settings.crop)
        enc_width, enc_height = resolutions.capped(crop_width, crop_height,
                                                   settings.max_resolution)
        # After the crop, because it is the picture left inside the bands that
        # is enlarged, and the engine is built for exactly that frame.
        if settings.upscale_stack is not None:
            if not self._settle_upscale(plan, width, height, sar, crop_width,
                                        crop_height, fields):
                self._release_dv(plan)
                shutil.rmtree(directory, ignore_errors=True)
                self.count("skipped")
                return None
            if settings.upscale_size:
                enc_width, enc_height = (
                    int(side) for side in settings.upscale_size.split("x"))
        # An upscaled file is not encoded as-is, so neither warning would be
        # true of it: its fields were checked to be progressive, and its pixels
        # come out square.
        if not settings.upscale_size:
            rules.warn_source_geometry(
                relative, videosource.interlace_verdict_of(fields), sar)
        plan.size_text = self._size_text(width, height, crop_width,
                                         crop_height, enc_width, enc_height,
                                         upscaled=bool(settings.upscale_size))

        # After the crop, because whether the metadata survives it depends on
        # what the frame it describes turns into.
        if safety.abort_requested():
            self._release_dv(plan)
            return None
        plan.hdr10plus_metadata = hdr.prepare_hdr10plus(
            relative, source, directory, settings,
            reframed=bool(settings.crop) or (str(enc_width), str(enc_height))
            != (str(width), str(height)))

        # A hardware encode uses one chunk per NVENC engine so the GPU's engines
        # all stay busy; a software encode uses the resolution-driven count that
        # fills the CPU cores.
        count = (settings.nvenc_engines if settings.hardware_encode
                 else rules.chunk_count_for(enc_width, enc_height,
                                            settings.cores))
        plan.bounds = chunk_bounds(duration, count)
        # Last, because it reads the video the encode will: the Dolby Vision
        # intermediate where there is one.
        if plan.bounds and safety.abort_requested():
            self._release_dv(plan)
            return None
        if plan.bounds:
            pausecontrol.wait_while_paused()
            plan.bounds, plan.cut_kinds = scenecuts.aligned_bounds(
                rules.video_source_for(relative, settings.input_dir,
                                       settings.dolby_vision_source),
                plan.bounds, settings.decode_accel)
        return plan

    def convert(self, plan: Plan) -> int:
        """ONE prepared video, produced in three overlapping parts and muxed.

        VIDEO is cut into chunks - one per NVENC engine for a hardware encode, or
        resolution-driven for a software one - and encoded in parallel. AUDIO is
        transcoded in software alongside it, because with hardware video work the
        CPU is otherwise idle and even for a software encode the few tracks are
        cheap. Then the MUX.
        """
        return self.start_convert(plan)()

    def start_convert(self, plan: Plan):
        """The part of :meth:`convert` the encoder is busy for: the audio
        started, the video encoded. What is left - waiting for the audio, the
        mux and the checks of what came out - is handed back as a callable that
        returns the file's status, so the run can start the NEXT file's video
        encode while it runs: none of it needs the encoder, and done in front of
        the next encode it is the encoder sitting idle between two films.
        """
        settings = plan.settings
        relative, directory = plan.relative, plan.directory
        try:
            # The audio starts NOW so it runs alongside the video pass: with
            # hardware video decode and encode the CPU is otherwise idle, and
            # even for a software encode the few tracks are cheap, so it
            # overlaps for free instead of running afterwards. Started the way
            # every worker is, not forked: the look-ahead and the previous
            # file's mux are threads of this process.
            audio = workerpool.start_worker(
                _audio_worker, (relative, directory, settings),
                os.path.join(settings.input_dir, relative))

            self._encode(settings, relative, directory, plan.video_duration,
                         plan.bounds,
                         plan.size_text, scenecuts.describe(plan.cut_kinds))
            frames, micros, _written = rules.sum_encode_progress(directory)
        finally:
            # The intermediate has been read for the last time - everything
            # left reads the ORIGINAL. Handed back per file because it is a
            # whole film's video stream, and the file prepared ahead may be
            # waiting to convert its own.
            self._release_dv(plan)

        # Video is done: stop the row and leave it behind on its own console
        # line. The rest runs without one: its lines scroll above the next
        # file's row.
        console.hide_active()

        def tail() -> int:
            audio.join()
            if safety.abort_requested():
                return safety.INTERRUPTED_EXIT_STATUS
            if self.finish(relative, directory, plan.duration,
                           audio.exitcode or 0, replacing=plan.replacing,
                           plan=plan) != 0:
                return 1
            # Only a file that came out complete counts towards the stats: a
            # skipped or failed one did work that produced nothing, and
            # averaging it in would report a throughput the finished library
            # does not reflect.
            with self.tally:
                self.converted += 1
                self.frames += frames
                self.video_seconds += micros / 1000000
            return 0

        return tail

    def count(self, field: str) -> None:
        """One more file skipped or failed: said from the look-ahead and the
        mux threads as well as the run's own, so under the tally's lock."""
        with self.tally:
            setattr(self, field, getattr(self, field) + 1)

    def _release_dv(self, plan: Plan) -> None:
        if plan.scratch:
            ramscratch.release_exit_cleanup([plan.scratch])
            plan.scratch = ""
            plan.settings.dolby_vision_source = ""
        if plan.holds_dv_slot:
            plan.holds_dv_slot = False
            self.dv_slot.release()

    def _take_dv_slot(self) -> bool:
        """The run's one Dolby Vision slot, waited for while the file encoding
        holds it; False when the run is interrupted first."""
        while not self.dv_slot.acquire(timeout=1):
            if safety.abort_requested():
                return False
        return True

    def _size_text(self, width, height, crop_width, crop_height,
                   enc_width, enc_height, upscaled: bool = False) -> str:
        """The sizes this file passes through, in the order the filters change
        them: what it arrived as, what the crop left, and what is encoded.

        Each step is named on its own rather than collapsed into one arrow,
        because the two do quite different things to a picture - a crop throws
        bands away and a scale resamples what is left - and "1920x1080 ->
        1280x536" alone does not say which of them happened, or that both did. A
        step that changes nothing is not stated.
        """
        if not width and not height:
            return "size unknown"
        text = "%sx%s" % (width or "?", height or "?")
        if (crop_width, crop_height) != (width, height):
            text += " -> cropped %sx%s" % (crop_width, crop_height)
        if (enc_width, enc_height) != (crop_width, crop_height):
            text += " -> %s%sx%s" % ("upscaled " if upscaled else "",
                                     enc_width, enc_height)
        return text

    def _settle_dolby_vision(self, plan: Plan) -> None:
        """The per-file Dolby Vision decision, which every chunk then inherits.

        A dual-layer profile 7 source is the one flavour that can be MADE
        encodable, by converting it to single-layer 8.1 first - done here, and not
        asked of the user as a separate ingest pass. Only worth it for an encoder
        that can code an RPU at all: with NVENC the result would be dropped again
        by the encode this is preparing for.

        The conversion waits for the run's Dolby Vision slot, so a file prepared
        while another encodes from its own intermediate is prepared once that
        encode is done with it, not beside it.
        """
        settings = plan.settings
        settings.dolby_vision_source = ""
        profile, enhancement = videosource.dolby_vision_profile(plan.source)
        if (enhancement == "1" and settings.dv_encoder_support
                and self._take_dv_slot()):
            pausecontrol.wait_while_paused()
            prepared, scratch = hdr.normalise_dolby_vision(
                plan.relative, settings, settings.output_dir)
            if prepared:
                settings.dolby_vision_source = prepared
                plan.scratch = scratch
                plan.holds_dv_slot = True
                # Re-classified rather than assumed, so the mode is decided from
                # the file that will actually be encoded.
                profile, enhancement = videosource.dolby_vision_profile(
                    prepared)
            else:
                self.dv_slot.release()
        settings.dolby_vision_mode = hdr.dolby_vision_mode_for(
            plan.relative, profile, enhancement, settings)
        plan.source_dv_profile = profile

        # The conversion was preparation for an encode that carries the RPU.
        # Where that turns out to be impossible anyway, the intermediate buys
        # nothing: its base layer is the same HDR10 the source already has.
        if plan.scratch and settings.dolby_vision_mode != "1":
            self._release_dv(plan)

    def _settle_crop(self, relative: str, source: str, settings) -> str:
        """The per-file crop -c asks for, which every chunk then inherits, or ""
        for a file that keeps its whole frame.

        Measured from the ORIGINAL rather than from a Dolby Vision intermediate:
        the two carry the same picture, and the original is the file that is
        certainly there. The one file that is never cropped is the one keeping its
        RPU, because that RPU describes where the picture sits in the frame it was
        graded in - cut the bands off and the metadata is measuring a frame that no
        longer exists, which is a worse outcome than a few coded rows of black.
        """
        if not settings.crop_wanted:
            return ""
        if settings.dolby_vision_mode == "1":
            log("Crop: not measured - this file carries its Dolby Vision RPU "
                "through the encode, and the RPU describes the uncropped frame: "
                "%s" % relative)
            return ""
        pausecontrol.wait_while_paused()
        return videocrop.crop_for(
            source, relative, videosource._media_duration,
            videosource._dimensions_line, runlog.jobs_per_core,
            settings.decode_accel)

    def _settle_upscale(self, plan: Plan, width, height, sar, crop_width,
                        crop_height, fields) -> bool:
        """The -u decision for one file, which every chunk then inherits: False
        for a file that is not converted at all, True otherwise - with
        ``upscale_size`` set when it is enlarged, and left empty when it keeps
        its own size.

        Kept at its own size: a picture that fills the tier already, and an HDR
        one, because no upscaling network is trained on HDR and a PQ picture
        fed to one comes back with its tones wrong. Skipped: a source whose
        fields are woven, since the network would enlarge the combing into the
        picture, and one the upscaler cannot be set up for. Skipped rather than
        encoded as-is, because a finished output is never converted again, and
        these are files a later run - one that can deinterlace, or once the
        problem is fixed - should still get to.
        """
        settings = plan.settings
        relative = plan.relative
        target = resolutions.enlarged(crop_width, crop_height,
                                      settings.upscale_resolution, sar)
        if target is None:
            return True
        color_space, color_range, transfer = videosource.video_color_space(
            plan.source)
        if transfer in ("smpte2084", "arib-std-b67"):
            log("Upscale: not for an HDR source - no upscaling network is "
                "trained on HDR - so it keeps its own size: %s" % relative)
            return True
        blocked = videosource.upscale_blocked_by_fields(fields)
        if blocked:
            log("Upscale: skipping %s - %s. An interlaced or telecined picture "
                "has to be deinterlaced before it is enlarged, which this run "
                "does not do." % (relative, blocked))
            return False
        fps = videosource.video_frame_rate(plan.source)
        if not fps:
            log("Upscale: skipping %s - its frame rate could not be read, and "
                "the upscaler reads it frame by frame." % relative)
            return False

        pausecontrol.wait_while_paused()
        stack = settings.upscale_stack
        engine, problem = upscale.engine_for(stack, int(crop_width),
                                             int(crop_height))
        if not engine:
            log("Upscale: skipping %s - no TensorRT engine could be built for "
                "its %sx%s picture: %s" % (relative, crop_width, crop_height,
                                           problem))
            return False
        # Indexed here, once, so the chunks opening it in parallel read the
        # index instead of each decoding the whole film to make its own.
        index = os.path.join(plan.directory, "bestsource")
        if not upscale.index_source(stack, plan.source, index, fps):
            log("Upscale: skipping %s - the upscaler's source filter could not "
                "read it." % relative)
            return False

        settings.upscale_size = "%dx%d" % target
        settings.upscale_engine = engine
        settings.upscale_index = index
        settings.upscale_fps = fps
        settings.upscale_crop = upscale.crop_edges(settings.crop, width, height)
        settings.upscale_matrix = upscale.source_matrix(color_space, height)
        settings.upscale_range = "full" if color_range == "pc" else "limited"
        return True

    def _encode(self, settings, relative: str, directory: str,
                duration: float, bounds: list, size_text: str,
                cuts: str = "") -> int:
        """The video pass, whole or cut at <bounds>, with the status row pinned
        under it.

        A failing pass is deliberately not fatal: the completeness check is the
        single arbiter of whether there is a usable video, so a broken encode skips
        that ONE file instead of aborting the whole run.
        """
        started = int(time.time())
        paused_at_start = pausecontrol.paused_seconds(started)
        console.current().show(
            lambda: rules.video_status_text(
                directory, duration, relative, started, paused_at_start,
                cols=statusline.state.cols, paused=pausecontrol.pause_requested(),
                paused_now=pausecontrol.paused_seconds(int(time.time())),
                now=int(time.time())), travels=False)

        if not bounds:
            log("Encoding whole %s (%s)..." % (relative, size_text))
            encode.encode_video_whole(relative, directory, settings)
            return 0

        total = len(bounds) - 1
        log("Chunking %s (%s) into %d parts%s..."
            % (relative, size_text, total, " (cut %s)" % cuts if cuts else ""))
        tokens = []
        for index in range(total):
            start, end = bounds[index], bounds[index + 1]
            span = "%.3f" % (formatting.awk_number(end)
                             - formatting.awk_number(start))
            tokens.append(rules.UNIT.join(
                [relative, str(index), str(total), start, span]))
        # -P is the CHUNK count, not the core count: the count is already sized to
        # fill the encoder, so this is the one place video parallelism lives.
        _run_chunk_pool(settings, tokens, total)
        # An interrupt must not fall through into the re-concatenation of a
        # half-encoded chunk set.
        if safety.abort_requested():
            return safety.INTERRUPTED_EXIT_STATUS
        encode.reconcat_video_only(relative, total, settings)
        return 0

    def finish(self, relative: str, directory: str, duration: float,
               audio_status: int, replacing: bool = False,
               plan: "Plan | None" = None) -> int:
        """Everything after both encodes: keep or discard the video, mux the final
        file, and confirm what came out.

        From here on the finished video is never discarded because something cheap
        failed - unless the VIDEO itself is incomplete, any later failure still
        writes the encode out on its own rather than letting hours of encoding die
        with the RAM scratch. Where the encode is written is settled here too, and
        only now: the output folder and the output name are both things the encode
        can have outlived.
        """
        from medialib.lib import dolbyvision, hdr10plus
        settings = plan.settings if plan else self.settings
        hdr10plus_metadata = plan.hdr10plus_metadata if plan else ""
        source_dv_profile = plan.source_dv_profile if plan else ""

        video_duration = plan.video_duration if plan else duration
        if not rules.video_intermediate_complete(directory, video_duration):
            runlog.error("Video encoding failed (no complete encoded video to "
                         "keep), skipping: " + relative,
                         kind="tool-bad-output", files=[relative, directory])
            shutil.rmtree(directory, ignore_errors=True)
            return 1

        if audio_status != 0:
            encode.write_video_only(relative, directory,
                                    "audio encoding failed", settings)
            shutil.rmtree(directory, ignore_errors=True)
            return 1

        # Written into the video before the mux rather than into the finished
        # file, so a failure here costs nothing but the HDR10+ itself.
        kept_hdr10plus = bool(hdr10plus_metadata) and hdr.restore_hdr10plus(
            relative, directory, hdr10plus_metadata, settings)

        output = encode.output_path_for(relative, settings, replacing)
        if encode.mux_final(relative, directory, settings, output) != 0:
            # A half-written output would only confuse the next run's resume
            # check.
            probes._remove(output)
            encode.write_video_only(relative, directory,
                                    "the final mux failed (audio, subtitles, "
                                    "attachments, chapters or metadata)",
                                    settings)
            shutil.rmtree(directory, ignore_errors=True)
            return 1

        # The video intermediate was measured against the source above, but that
        # was the picture alone: the mux is another stream copy, of a different
        # set of streams, and it reports success on what it managed to write. So
        # the FILE is measured too, and the encode is kept beside it the way a
        # failed mux keeps it - hours of encoding must not die with a container.
        if not durationcheck.verify(
                relative, os.path.join(settings.input_dir, relative), output,
                duration):
            encode.write_video_only(relative, directory,
                                    "the muxed output was not as long as "
                                    "the source", settings)
            shutil.rmtree(directory, ignore_errors=True)
            return 1

        # This run produced the complete file, so a failsafe left by an earlier
        # run for the same source is now just a duplicate of its video stream.
        probes._remove(rules.video_only_path_for(relative, settings.output_dir,
                                                 settings.input_dir))

        # The encoder embedded the RPU and ffmpeg derives the container's record
        # from that, but both the chunk re-join and the mux are stream copies, so
        # the cheap check that the output still SIGNALS it is worth making.
        if settings.dolby_vision_mode == "1":
            out_profile, _el = videosource.dolby_vision_profile(output)
            if out_profile:
                log("Dolby Vision preserved: profile %s -> profile %s: %s"
                    % (source_dv_profile, out_profile, relative))
            else:
                log("WARNING: Dolby Vision was encoded but the output does not "
                    "signal it: " + relative)

        # The same check for HDR10+, whose messages the mux copies along with
        # the frames they sit in front of.
        if kept_hdr10plus:
            if hdr10plus.stream_has_hdr10plus(output):
                log("HDR10+ preserved%s: %s"
                    % (" alongside Dolby Vision"
                       if settings.dolby_vision_mode == "1" else "", relative))
            else:
                log("WARNING: HDR10+ was written into the encode but the "
                    "output does not signal it: " + relative)

        # Whatever produced the record, the LEVEL in it has to describe the video
        # that is actually here: a level that overstates the file is refused by a
        # player outright, which looks like a broken file rather than metadata.
        dolbyvision.normalise_config_level(output, relative,
                                           script_dir=settings.script_dir,
                                           log=log)
        shutil.rmtree(directory, ignore_errors=True)
        return 0

    def footer(self) -> None:
        """The same figures the live row showed, aggregated over every file.

        Only the files that came out COMPLETE feed the duration, frame and
        throughput figures: a resume-skipped file did no work and a failed one did
        work that produced nothing. Time spent PAUSED is taken out of the
        wall-clock every figure divides by, because during it there was nothing
        encoding to describe.
        """
        total = time.time() - self.start
        paused = pausecontrol.paused_seconds(int(time.time()))
        encoding = max(0.0, total - paused)

        # The figures are progress, which -q leaves out; the recap of files that
        # came out short is a failure, which it does not.
        out = sys.stderr
        say = runlog.say
        say("\nStats\n=====\n", out)
        say("Total time:        %.2f s (%s)\n"
            % (total, formatting.fmt_hms("%.2f" % total)), out)
        if paused > 0:
            say("Of that, paused:   %d s (%s) - left out of the figures "
                "below\n" % (paused, formatting.fmt_hms(str(paused))), out)
        say("Files converted:   %d\n" % self.converted, out)
        say("Files skipped:     %d\n" % self.skipped, out)
        say("Files failed:      %d\n" % self.failed, out)
        # A run that converted nothing has no encode to measure, and a row of
        # zeros would only read like one that went badly.
        if self.converted > 0:
            say("Total duration:    %.0f s (%s)\n"
                % (self.video_seconds,
                   formatting.fmt_hms("%.3f" % self.video_seconds)), out)
            say("Frames encoded:    %d\n" % self.frames, out)
            say("Time per file:     %.2f s\n" % (encoding / self.converted),
                out)
        if self.converted > 0 and encoding > 0:
            say("Average fps:       %.1f\n" % (self.frames / encoding), out)
            say("Real-time speedup: %sx\n" % formatting.fmt_ratio(
                "%.6f" % (self.video_seconds / encoding)), out)
        out.flush()
        durationcheck.report(out)


def _audio_worker(relative: str, directory: str, settings) -> None:
    """The audio pass, in a process of its own so it overlaps the video.

    Its own interrupt handling: an asynchronous child inherits SIGINT
    ignored, so without a handler it would survive Ctrl+C outright.
    """
    safety.trap_worker_abort()
    raise SystemExit(encode.encode_audio_all(relative, directory, settings))


def _run_chunk_pool(settings, tokens: list, width: int) -> None:
    workerpool.run(tokens, width, encode.encode_video_chunk,
                   lambda token: (settings, token))


# --- the run ------------------------------------------------------------------

def main(argv: list, program: str = "convert-video",
         script_dir: str = "") -> int:
    declaration = rules.spec(program)
    # -t is the one option with a side effect beyond its assignment: the flag
    # itself switches the test on, and the percentage it may carry is a second
    # value. The option parser cannot express an optional argument, so the
    # flag is declared bare and the word after it is claimed only when it is
    # a number - which is what this hook is handed.
    saving = [rules.DEFAULT_BITRATE_SAVING]

    def on_option(letter: str, value: str) -> None:
        if letter == "t" and value:
            saving[0] = value

    try:
        result = clioptions.parse(declaration, argv, on_opt=on_option)
        clioptions.settle_verbosity(result)
    except clioptions.HelpRequested:
        sys.stdout.write(clioptions.help_text(declaration))
        return 0
    except clioptions.UsageError as error:
        clioptions.refuse_usage(declaration,
                                error.message)
        return 1

    if clioptions.args_out_of_range(len(result.positionals), 2, None):
        sys.stdout.write(clioptions.no_args_text(declaration))
        return 1

    script_dir = script_dir or commands.script_dir()

    settings = rules.Settings(
        script_dir=script_dir,
        input_dir=result.positionals[0],
        output_dir=result.positionals[1],
        cores=int(result.values["CORES"] or runlog.cpu_count()),
        video_profile=result.values["videoProfile"] or "av1BluRay",
        audio_profile=result.values["audioProfile"] or "opus",
        custom_audio_bitrate=result.values["customAudioBitrate"] or "",
        quality=result.values["videoQuality"] or "",
        quality_given="l" in result.given,
        max_resolution=result.values["maxVideoResolution"] or "",
        upscale_resolution=result.values["upscaleResolution"] or "",
        crop_wanted="c" in result.given,
        fast_decode=result.values["videoFastDecode"] or "",
        test_source_bitrate="t" in result.given,
        keep_tracks="k" in result.given,
        required_saving=saving[0])
    grain = result.values["videoGrain"] or ""
    engines_override = result.values["nvencEnginesOverride"] or ""

    # A -b bitrate on its own says everything opusCustom does, so let it select
    # that profile rather than rejecting the run over an -a the user never typed.
    # Only the untouched default is promoted - an explicit `-a opus -b 46` still
    # contradicts itself and is caught below.
    if ("a" not in result.given and settings.custom_audio_bitrate
            and settings.audio_profile == "opus"):
        settings.audio_profile = "opusCustom"

    # -k leaves the audio as the source has it, which is what the passthrough
    # profile already means to the audio encode: nothing is written for it.
    # An -a or -b says how the audio is to be re-encoded, which -k forbids.
    if settings.keep_tracks:
        if "a" in result.given or "b" in result.given:
            clioptions.refuse_usage(
                declaration,
                "-k keeps the source's audio exactly as it is, and -a and -b "
                "say how to re-encode\nit: give one or the other.")
            return 1
        settings.audio_profile = "passthrough"

    try:
        video_args = rules.profile_args(rules.VIDEO_PROFILES,
                                        settings.video_profile)
        rules.profile_args(rules.AUDIO_PROFILES, settings.audio_profile)
    except rules.UnknownProfile as unknown:
        sys.stderr.write(unknown.text())
        return 1

    refusal = startup._validate(settings, video_args, grain, engines_override)
    if refusal:
        clioptions.refuse_usage(declaration, refusal)
        return 1

    if not os.path.isdir(settings.input_dir):
        clioptions.refuse_missing_dir(declaration,
                                      settings.input_dir)
        return 1
    # Before the output folder is created: a re-encode written inside the input is
    # a video this script would find and re-encode again on the next run.
    if safety.require_separate_output(settings.input_dir, settings.output_dir):
        return 1

    try:
        return _settle_and_run(settings, program, video_args, grain,
                               engines_override)
    finally:
        # The ffmpeg pin and the run directory exist from here on, and every
        # refusal below them must hand them back, not only the walk's own end.
        ramscratch.run_exit_cleanup()


def _settle_and_run(settings, program: str, video_args: str, grain: str,
                    engines_override: str) -> int:
    # This script asks ffmpeg for things a distribution's package is often too old
    # to do, so the shared ladder is given a probe that answers "can this build do
    # what THIS run's profile asks". An SVT-AV1 profile also ranks the builds that
    # can: SVT-AV1-HDR first when the run uses what only it does, the newest
    # SVT-AV1 release first otherwise. Any other encoder has nothing to rank.
    rank = None
    if rules.encoder_of(video_args) == "libsvtav1":
        fork_use = ""
        if settings.video_profile == rules.SVT_AV1_HDR_GRAIN_PROFILE and not grain:
            fork_use = "its film grain tune for %s" % settings.video_profile
        elif probes.pq_source_to_convert(settings):
            fork_use = "its PQ variance-boost curve on a PQ source"
        rank = probes.SvtBuildRank(fork_use)
    ffmpegselect.select_ffmpeg(
        lambda binary: probes.ffmpeg_takes_profile(binary, video_args),
        prefer=rank)
    if tooldeps.require_tools(program, ["ffmpeg", "ffprobe"]
                              + (["mkvmerge"] if settings.keep_tracks else [])):
        return 1
    # Refused rather than degraded, and before the output folder exists: a run
    # that encoded the small files at their own size instead would leave outputs
    # that every later run skips as finished, upscaler or not.
    if settings.upscale_resolution:
        stack, problem = upscale.settle(startup._gpu_name())
        if stack is None:
            sys.stderr.write("%s: -u %s needs a working upscaler, and %s.\n"
                             % (program, settings.upscale_resolution, problem))
            return 1
        settings.upscale_stack = stack

    # The name patterns are deliberately the exact ones the conversion loop uses,
    # so the probe and the loop can never disagree about what counts. Asked
    # before the output folder is made, so a run with nothing to convert
    # leaves nothing behind.
    if not probes._sources(settings.input_dir):
        return safety.fail_no_relevant_input(settings.input_dir,
                                             "videos (.mkv / .mp4)")
    os.makedirs(settings.output_dir, exist_ok=True)
    ramscratch.init_ram_base()

    chosen = ffmpegselect.selected_ffmpeg()
    settings.svt_av1_hdr = bool(rank is not None and chosen
                                and rank.facts(chosen)[0])
    if rank is not None and chosen:
        settings.svt_build_note = rank.note(chosen)
    # An explicit -g is a request for synthesis (or for none), which the grain
    # tune does not do - so asking for it is also opting out of the tune.
    settings.grain_tune = (settings.svt_av1_hdr and not grain
                           and settings.video_profile
                           == rules.SVT_AV1_HDR_GRAIN_PROFILE)
    settings.grain_level, settings.grain_probe_wanted = startup._settle_grain(
        settings, grain, video_args)
    startup._detect_hardware(settings, video_args, engines_override)
    startup._summarise(settings, video_args, grain)

    return _run_all(settings)


class _HeldOutput:
    """The run's stderr, holding back what the look-ahead says while a file
    encodes.

    The next file is prepared on a thread of its own while the current one
    encodes, and what that preparation logs would otherwise land on top of the
    pinned status row and between the lines of a file it has nothing to do
    with. So its lines are held while the run is busy, and let through - the
    held ones first - once the run is waiting on the preparation itself, which
    is also where they would have appeared had the file been prepared after the
    one before it.
    """

    def __init__(self, stream) -> None:
        self._stream = stream
        self._lock = threading.Lock()
        self._held: list = []
        self._live = True
        self.ahead: int | None = None

    def write(self, text: str) -> int:
        with self._lock:
            if not self._live and threading.get_ident() == self.ahead:
                self._held.append(text)
                return len(text)
            return self._stream.write(text)

    def live(self, on: bool) -> None:
        with self._lock:
            self._live = on
            if on and self._held:
                self._stream.write("".join(self._held))
                self._stream.flush()
                self._held = []

    def __getattr__(self, name: str):
        return getattr(self._stream, name)


def _run_all(settings) -> int:
    """The walk: one file at a time, because the chunk count already saturates the
    encoder and encoding files in parallel would oversubscribe it. What runs
    beside a file's encode is everything around it that does not need the
    encoder, so the encoder is not left idle between two files:

    - the NEXT files' preparation - the probes, the -t worthwhileness test, the
      grain and crop measurements, the Dolby Vision conversion, the HDR10+
      extraction and the chunk boundaries - fed by the dynamic queue up to
      LOOKAHEAD files ahead, passing over the files it skips as it goes;
    - the PREVIOUS file's tail - waiting for its audio, the mux and the checks of
      the muxed file - on a thread of its own while this file's video encodes.
    """
    statusline.init_status_line()

    # The pause state lives in the RAM scratch because the encoders a keypress has
    # to reach are in other processes. Armed only when nothing armed it already: a
    # wrapper that has pauses of its own owns that state, so one keypress holds
    # every layer of a run.
    if not os.environ.get("PAUSE_DIR"):
        directory, status = ramscratch.ram_scratch_dir("convertVideo.pause")
        if status == 0:
            pausecontrol.init(directory)
            ramscratch.add_exit_cleanup([directory])
    pausecontrol.start_pause_keys()
    if pausecontrol.pause_keys_active():
        log('Pause: press "p" to pause the video encoding (the %s freed, the '
            "encoders keep their memory and the audio keeps going) and \"r\" to "
            "carry on. Only for as long as this shell runs."
            % ("NVENC engines are" if settings.hardware_encode
               else "CPU is"))

    chunk_root, status = ramscratch.ram_scratch_dir("convertVideo.chunks")
    if status != 0:
        return 1
    ramscratch.add_exit_cleanup([chunk_root])
    settings.chunk_root = chunk_root
    if settings.upscale_stack is not None:
        settings.upscale_script = upscale.write_script(chunk_root)

    run = Run(settings)
    safety.set_run_footer(run.footer)
    durationcheck.init_log()
    safety.init_abort_flag()
    safety.trap_run_abort()

    # The input tree's sub-folder layout, mirrored under the output.
    for parent, dirs, _names in os.walk(settings.input_dir):
        dirs.sort()
        for name in dirs:
            os.makedirs(os.path.join(
                settings.output_dir,
                os.path.relpath(os.path.join(parent, name),
                                settings.input_dir)), exist_ok=True)

    output = _HeldOutput(sys.stderr)
    sys.stderr = output

    def prepared():
        # The dynamic queue's producer, on the queue's own thread: the files
        # still to convert, each prepared - or passed over - in turn.
        output.ahead = threading.get_ident()
        for relative in probes._sources(settings.input_dir):
            if safety.abort_requested():
                return
            plan = run.prepare(relative)
            if plan is not None:
                yield plan, plan.duration

    # The previous file's mux, running beside this one's encode: at most one at
    # a time, so the RAM scratch never holds more than two files' encodes.
    muxing: threading.Thread | None = None
    crashed: list = []

    def wait_for_mux() -> None:
        # What the tail raised is raised here, in the run's own thread, the
        # way it was when the tail ran in it.
        nonlocal muxing
        if muxing is not None:
            muxing.join()
            muxing = None
        if crashed:
            raise crashed.pop()

    def mux(tail) -> None:
        # One file that cannot be finished must not cost the rest of the run:
        # it has already said what went wrong, and kept its video encode where
        # it could.
        try:
            if tail() not in (0, safety.INTERRUPTED_EXIT_STATUS):
                run.count("failed")
        except BaseException as raised:  # noqa: BLE001 - re-raised above
            crashed.append(raised)

    queue = dynamicqueue.ahead(prepared(), LOOKAHEAD, log=log,
                               discard=run._release_dv)
    try:
        while True:
            # The look-ahead's held lines come out while the run waits on it,
            # which is also where they would have appeared had the file been
            # prepared after the one before it.
            output.live(True)
            plan = next(queue, None)
            if plan is None:
                break
            output.live(False)
            if safety.abort_requested():
                run._release_dv(plan)
                break
            tail = run.start_convert(plan)
            wait_for_mux()
            if safety.abort_requested():
                tail()
                break
            muxing = threading.Thread(target=mux, args=(tail,),
                                      name="convert-video-mux")
            muxing.start()
        wait_for_mux()
        # An interrupt stops the walk instead of counting every remaining
        # file as a failure. The report still goes out, covering the files
        # that did convert.
        if safety.abort_requested():
            queue.close()
            sys.stderr.write("\nInterrupted - stopping.\n")
            safety.print_run_footer()
            return safety.INTERRUPTED_EXIT_STATUS
    finally:
        # Closed, and so waited for, before the scratch cleanup: nothing the
        # look-ahead registers may land after it. Its probes were interrupted
        # with the rest, and it checks for an interrupt between files; what it
        # had prepared and the run never reached is let go of.
        if muxing is not None:
            muxing.join()
        queue.close()
        output.live(True)
        sys.stderr = output._stream
        console.hide_active()
        pausecontrol.stop_pause_keys()
        pausecontrol.kill_pausable_jobs()
        ramscratch.run_exit_cleanup()

    safety.print_run_footer()
    return workerpool.exit_status(1 if run.failed else 0)


def cli(argv: list | None = None) -> int:
    settings.load()
    runlog.start_run(argv)
    if argv is None:
        argv = sys.argv[1:]
    return main(argv, commands.program_name(__spec__.name),
                commands.script_dir())


if __name__ == "__main__":
    sys.exit(cli())
