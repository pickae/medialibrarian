"""ingest-movies's run: the phase sequence, and the improved-copy remux.

The decisions - which track is kept, dropped or swapped, which folder is bonus
material, what a name cleans to - are `medialib/cli/ingest_movies.py`. This is
what those decisions are carried out by, and it is separate for the same reason
the census is: the rules are worth reading on their own, and the run around them
is mostly plumbing.
"""

import os
import subprocess
import sys

from medialib import commands
from medialib.cli import ingest_movies as rules
from medialib.cli.ingest_movies import log
from medialib.lib import (
    cleannamesindividually,
    clioptions,
    commentarytranscription,
    dolbyvision,
    enums,
    ffmpegselect,
    ramscratch,
    runlog,
    safety,
    subtitlefiles,
    tmdblookup,
    tooldeps,
    workerpool,
)

# One job wants about four cores: audio work is I/O bound long before it is CPU
# bound.
CORES_PER_JOB = 4

# How far the name cleaning is repeated before it is called stable. Some changes
# around parentheses induce new double spaces, so a pass can create work for the
# next; the cap is only there so a rule that oscillates cannot loop forever.
MAX_RENAME_PASSES = 10


class Run:
    """One run's settings, and the work its jobs do."""

    # Declared, not defaulted: the settings dict supplies every one, so a name
    # it does not carry is still an AttributeError at the read.
    script_dir: str
    ram_root: str
    skips: safety.RunSkipLog
    fragments_file: str
    whisper: dict
    ffsubsync_quality: str

    def __init__(self, **settings) -> None:
        self.__dict__.update(settings)

    # --- the improved copy ----------------------------------------------------

    def improve_main_movie(self, movie: str) -> None:
        """Remux ONE main movie into an improved copy, keeping the original
        beside it as "<name> (old).mkv" - so nothing is ever lost and a second
        run is a no-op, the "(old)" sibling being what makes the folder skip
        itself."""
        log("Improving main movie: " + movie)
        base = os.path.splitext(movie)[0]

        tracks = rules._identify(movie)
        if not tracks:
            log("  Skipping (no tracks read): " + movie)
            return
        rules._object_flags(movie, tracks)

        changed = rules.decide_actions(tracks, base)
        for position, winner in rules.apply_surround_ladder(tracks):
            changed = True
            log("  Surround ladder: dropping audio track %s (%s, %s) in favour "
                "of %s" % (tracks[position].id, tracks[position].name,
                           tracks[position].language, tracks[winner].name))

        transcripts = rules.gather_commentary_transcripts(base, tracks)
        if transcripts:
            changed = True

        job = self.decide_dolby_vision_job(movie, tracks)

        # An overstated Dolby Vision LEVEL, corrected in place. This is not part
        # of the remux and must not wait for one: the level is a container
        # element, so correcting it is two bytes and no remux at all, and a film
        # whose level is the only thing wrong with it needs nothing else done -
        # it would otherwise return just below, uncorrected.
        dolbyvision.normalise_config_level(movie, script_dir=self.script_dir,
                                           log=log)

        if not changed and not job["wanted"]:
            log("  No improvements needed: " + movie)
            return

        self.remux(movie, base, tracks, transcripts, job)

    def decide_dolby_vision_job(self, movie: str, tracks: list) -> dict:
        """``decideDolbyVisionJob``: which Dolby Vision job this file needs, if
        any - converting a real dual-layer profile 7 to 8.1, or dropping a claim
        of ANY profile that the video does not back up with an RPU.

        Only the cheap probes and the eligibility checks happen here; the work on
        the stream is deferred until it is certain the remux runs at all.
        """
        video_indexes = [position for position, track in enumerate(tracks)
                         if track.is_video]
        info = dolbyvision.read_video_info(movie)
        job = {"wanted": False, "action": "", "fps": "", "source_hdr": False,
               "video_index": video_indexes[-1] if video_indexes else -1,
               "video_count": len(video_indexes),
               "stream_size": info["STREAM_SIZE"], "info": info}

        if not dolbyvision.claims_dolby_vision(info["PROFILE"],
                                               info["SETTINGS"]):
            return job

        is_profile7 = dolbyvision.is_profile7(info["PROFILE"], info["SETTINGS"])
        if not _has_tool("dovi_tool"):
            # Without dovi_tool neither job can even be DECIDED: the RPU probe is
            # what tells a real Dolby Vision file from one that only claims it.
            # Worth a word only for profile 7, the case with a known conversion
            # to miss.
            if is_profile7:
                log("  WARNING: dovi_tool not installed, leaving Dolby Vision "
                    "profile 7 as is: " + movie)
        elif dolbyvision.stream_has_rpu(movie):
            # Real Dolby Vision. Only dual-layer profile 7 has anything to gain;
            # profile 5 and 8.x are left exactly as they are.
            if is_profile7:
                job["action"] = "convert"
        else:
            # The container advertises Dolby Vision the video does not carry,
            # whatever profile it named: there is nothing to CONVERT, so the
            # false claim is dropped instead.
            job["action"] = "strip"
            job["source_hdr"] = dolbyvision.is_hdr(info["TRANSFER"],
                                                   info["HDR"])

        if not job["action"]:
            return job

        # Both jobs replace the video track, so both need the same two things: a
        # frame rate to force on the raw stream (which carries no timing of its
        # own), and exactly one video track, since dropping file 0's video would
        # otherwise throw away one that is not replaced.
        description = ("converting Dolby Vision profile 7 to 8.1"
                       if job["action"] == "convert"
                       else "dropping the false Dolby Vision claim")
        if job["video_count"] != 1:
            log("  WARNING: %s needs exactly one video track, this has %d, "
                "leaving as is: %s" % (description, job["video_count"], movie))
            job["action"] = ""
        elif not info["FPS_SPEC"]:
            log("  WARNING: %s needs a frame rate, which mediainfo does not "
                "report, leaving as is: %s" % (description, movie))
            job["action"] = ""
        else:
            job["fps"] = info["FPS_SPEC"]
            job["wanted"] = True
        return job

    def remux(self, movie: str, base: str, tracks: list, transcripts: list,
              job: dict) -> None:
        """The improved copy itself: ONE mkvmerge call, however many of the
        improvements apply."""
        scratch, on_disk = self._scratch_for(movie, job)
        if scratch is None:
            log("  WARNING: no scratch directory could be created, left "
                "untouched: " + movie)
            return
        ramscratch.add_exit_cleanup([scratch])
        if on_disk:
            log("  Too large for the RAM scratch, working on disk instead: "
                + movie)

        try:
            hevc = self._prepare_video(movie, scratch, job)
            if job["wanted"] and not hevc:
                job["wanted"] = False
                if not any(track.action != "keep" for track in tracks) \
                        and not transcripts:
                    log("  No other improvements needed, left untouched: "
                        + movie)
                    return
            argv, order = self._mkvmerge_command(movie, tracks, transcripts,
                                                 job, hevc)
            self._run_and_swap(movie, scratch, argv, order, job, hevc)
        finally:
            # Handed back now rather than at exit, on every outcome: a run over a
            # folder would otherwise hold one abandoned copy of every film it
            # rejected, and on the disk path those are hidden directories in the
            # library rather than tmpfs that clears at reboot.
            ramscratch.release_exit_cleanup([scratch])

    def _scratch_for(self, movie: str, job: dict) -> tuple:
        """Where this ONE film's two large intermediates go.

        Both are held at the same time - mkvmerge reads the prepared video stream
        while writing the remux - so what the scratch has to take is the video
        stream plus a whole copy of the film, which for a 4K remux is more than
        any tmpfs on a normal machine has. The decision is therefore per FILE:
        RAM for the ordinary film, a hidden directory beside the original for the
        one that would not fit - beside it because that file system already has
        to hold the result, which turns the final move into a rename.
        """
        need = rules._size_of(movie)
        if need and job["wanted"]:
            # The prepared video stream comes on top: its measured size where
            # mediainfo reported one, and the whole film as the upper bound where
            # it did not - a video track cannot be larger than the file it is in.
            size = job["stream_size"]
            need += int(size) if str(size).isdigit() else need
        # The byte count travels as text, the way the shell hands it over: an
        # unreadable size is the empty string rather than a number, and that is
        # what tells the scratch it has nothing to size itself against.
        path, on_disk, status = ramscratch.ram_scratch_dir_for(
            str(need) if need else "", "improveMovie",
            os.path.dirname(movie))
        if status != 0:
            return None, False
        return path, on_disk

    def _prepare_video(self, movie: str, scratch: str, job: dict) -> str:
        """The expensive half of the Dolby Vision work, now that the remux is
        going to happen anyway.

        Either way the video ends up as a raw HEVC stream that replaces the
        original track; only what is done to it on the way differs. Failing at it
        falls back to keeping the video exactly as it is.
        """
        if not job["wanted"]:
            return ""
        info = job["info"]
        hevc = os.path.join(scratch, "dv", os.path.basename(movie) + ".hevc")
        os.makedirs(os.path.dirname(hevc), exist_ok=True)
        if job["action"] == "convert":
            log("  Normalising Dolby Vision profile 7 -> 8.1 (%s, %s): %s"
                % (info["PROFILE"] or info["SETTINGS"], job["fps"], movie))
            # Both report a shell STATUS, where 0 is the success: the bash
            # callers read them with `&&`, and 0 is falsy here.
            done = dolbyvision.convert_to_profile81(movie, hevc, log=log) == 0
        else:
            # Say which of the two outcomes this is, since that is the whole
            # point: the copy claims HDR and nothing else, or nothing at all.
            log("  Dolby Vision (%s) is claimed by the container but the video"
                % (info["PROFILE"] or info["SETTINGS"]))
            log("  carries no RPU, so there is nothing to convert - dropping")
            if job["source_hdr"]:
                log("    the Dolby Vision claim, the copy reports HDR only "
                    "(%s, %s): %s" % (info["TRANSFER"] or "HDR", job["fps"],
                                      movie))
            else:
                log("    the Dolby Vision claim, the copy reports no HDR at all "
                    "(%s, %s): %s" % (info["TRANSFER"] or "unknown transfer",
                                      job["fps"], movie))
            done = dolbyvision.extract_video_stream(movie, hevc, log=log) == 0
        return hevc if done else ""

    def _mkvmerge_command(self, movie: str, tracks: list, transcripts: list,
                          job: dict, hevc: str) -> tuple:
        """File 0 is the original with unwanted tracks deselected; each swapped
        opus and each commentary srt is an extra input whose single track
        inherits the right language, name and flags.

        File ids are assigned deterministically - 0 the movie, then the prepared
        video stream, then the swapped opus inputs in track order, then the
        commentary subtitles - and ``--track-order`` puts every track back in its
        original slot, with the commentary subtitles appended at the very end.
        """
        extra, next_fid = [], 1
        video_fid = ""

        if job["wanted"] and hevc:
            video = tracks[job["video_index"]]
            video_fid = str(next_fid)
            next_fid += 1
            extra += ["--language", "0:" + (video.language or "und")]
            if video.name and video.name != "null":
                extra += ["--track-name", "0:" + video.name]
            extra += ["--default-track-flag",
                      "0:1" if video.default == "true" else "0:0"]
            if video.forced == "true":
                extra += ["--forced-display-flag", "0:1"]
            # A raw HEVC elementary stream has no timing of its own, so without
            # the duration mkvmerge falls back to 25 fps and desyncs every audio
            # track. --no-chapters for symmetry with the swapped-in audio:
            # chapters come from the main movie only.
            extra += ["--default-duration", "0:" + job["fps"],
                      "--no-chapters", hevc]

        opus_fid = {}
        for track in tracks:
            if track.action != "swap":
                continue
            opus_fid[track.id] = next_fid
            next_fid += 1
            extra += ["--language", "0:" + (track.language or "und")]
            if track.name and track.name != "null":
                extra += ["--track-name", "0:" + track.name]
            extra += ["--default-track-flag",
                      "0:1" if track.default == "true" else "0:0"]
            if track.forced == "true":
                extra += ["--forced-display-flag", "0:1"]
            if track.is_commentary:
                extra += ["--commentary-flag", "0:1"]
            extra += ["--no-chapters", track.opus]

        comm_fids = []
        for srt, language, title in transcripts:
            comm_fids.append(next_fid)
            next_fid += 1
            # mkvmerge takes the two-letter code of a transcript's suffix as
            # readily as the three-letter one.
            extra += ["--language", "0:" + language, "--track-name",
                      "0:" + title, "--commentary-flag", "0:1",
                      "--default-track-flag", "0:0", srt]

        selection = []
        audio = [t for t in tracks if t.is_audio]
        subtitles = [t for t in tracks if t.is_subtitle]
        if audio:
            kept = [t.id for t in audio if t.action == "keep"]
            selection += ["-a", ",".join(kept)] if kept else ["-A"]
        if subtitles:
            kept = [t.id for t in subtitles if t.action != "drop"]
            selection += ["-s", ",".join(kept)] if kept else ["-S"]
        if job["wanted"] and hevc:
            # The video now comes from the prepared stream, so file 0
            # contributes every track except its video one.
            selection += ["-D"]

        order = []
        for position, track in enumerate(tracks):
            if video_fid and position == job["video_index"]:
                order.append("%s:0" % video_fid)
            elif track.action == "keep":
                order.append("0:" + track.id)
            elif track.action == "swap":
                order.append("%s:0" % opus_fid[track.id])
        order += ["%s:0" % fid for fid in comm_fids]

        return selection + [movie] + extra, order

    def _run_and_swap(self, movie: str, scratch: str, argv: list, order: list,
                      job: dict, hevc: str) -> None:
        """Remux into the scratch, then swap the files on disk.

        mkvmerge exits 0 on success, 1 on non-fatal WARNINGS while still
        producing a valid file, and 2 on real errors, so 0 and 1 are both success
        - with any warning surfaced either way.
        """
        out = os.path.join(scratch, "improve", os.path.basename(movie))
        os.makedirs(os.path.dirname(out), exist_ok=True)
        status, output = _mkvmerge(["--quiet", "-o", out, "--track-order",
                                    ",".join(order)] + argv)
        # Free the converted stream the moment mkvmerge is done with it, so a run
        # over many films never holds two of them at once.
        if hevc:
            rules._remove(hevc)

        reject = self._dolby_vision_reject(out, job, status)
        if reject:
            rules._remove(out)
            # Say what the remux claimed and what mkvmerge had to say: without
            # those the only way to find out why the result was rejected is to
            # redo the whole extract-convert-mux by hand.
            log("  WARNING: %s, left original untouched: %s" % (reject, movie))
            if output:
                log("    mkvmerge output: " + output)
            return

        if status <= 1:
            old = os.path.splitext(movie)[0] + " (old).mkv"
            rules._rename_quiet(movie, old)
            rules._rename_quiet(out, movie)
            log('  Improved copy written (original kept as "%s"): %s'
                % (os.path.basename(old), movie))
            if output:
                log("    mkvmerge warnings (ignored): " + output)
        else:
            rules._remove(out)
            log("  WARNING: mkvmerge failed, left original untouched: " + movie)
            log("    reason: " + (output or "<no output>"))

    def _dolby_vision_reject(self, out: str, job: dict, status: int) -> str:
        """The Dolby Vision work, verified BEFORE the original is touched: a
        conversion has to REPORT profile 8, a strip has to report no Dolby Vision
        while still being as HDR as the source was. A result that does not is
        thrown away and the source left exactly as it was - still a perfectly
        playable file, if a mislabelled one."""
        if status > 1 or not job["wanted"]:
            return ""
        if job["action"] == "convert":
            ok, seen = dolbyvision.is_profile8(out)
            if not ok:
                return ('remux reports Dolby Vision profile "%s" instead of 8'
                        % (seen or "<none>"))
            return ""
        ok, seen = dolbyvision.is_dolby_vision_free(
            out, "1" if job["source_hdr"] else "0")
        if not ok:
            seen_profile, seen_settings, _hdr = seen
            if seen_profile or "RPU" in seen_settings.upper():
                return ('remux still claims Dolby Vision ("%s")'
                        % (seen_profile or seen_settings))
            return "remux lost the HDR metadata the source had"
        return ""


def improve_main_movies(state, root: str) -> None:
    """Every film folder's single main movie, improved.

    Extras subfolders are skipped, as are folders with zero or several main mkvs
    - there is no telling which one is the feature - and folders already carrying
    an improved copy, which is what makes a second run a no-op.
    """
    for directory in [root] + rules._folders_below(root):
        if rules.is_bonus_folder(directory):
            continue
        movies = [os.path.join(directory, name)
                  for name in sorted(rules._names_in(directory))
                  if name.endswith(".mkv") and not name.endswith(" (old).mkv")
                  and os.path.isfile(os.path.join(directory, name))]
        if len(movies) != 1:
            continue
        if os.path.isfile(os.path.splitext(movies[0])[0] + " (old).mkv"):
            continue
        state.improve_main_movie(movies[0])


def check_folders(root: str) -> None:
    """A folder holding no mkv at all, which usually means a file was named like
    the folder."""
    for directory in [root] + rules._folders_below(root):
        if not rules._files_below(directory,
                                  matches=lambda name: name.endswith(".mkv")):
            log("WARNING: folder without mkv (a file may be named like the "
                "folder): " + directory)


def _mkvmerge(argv: list) -> tuple:
    try:
        done = subprocess.run(["mkvmerge"] + argv, stdin=subprocess.DEVNULL,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    except OSError:
        return 127, "mkvmerge not found"
    return done.returncode, done.stdout.decode("utf-8",
                                               "surrogateescape").strip()


def _has_tool(name: str) -> bool:
    import shutil
    return shutil.which(name) is not None


def main(argv: list, program: str = "ingest-movies",
         script_dir: str = "") -> int:
    declaration = rules.spec(program)
    try:
        result = clioptions.parse(declaration, argv)
    except clioptions.HelpRequested:
        sys.stdout.write(clioptions.help_text(declaration))
        return 0
    except clioptions.UsageError as error:
        sys.stderr.write(clioptions.usage_error_text(declaration,
                                                     error.message))
        return 1

    if clioptions.args_out_of_range(len(result.positionals), 1, 1):
        sys.stderr.write(clioptions.no_args_text(declaration))
        return 1

    script_dir = script_dir or commands.script_dir()

    root = result.positionals[0]
    # This is a long run, so its log lines carry a wall-clock stamp.
    os.environ["LOG_TIMESTAMPS"] = "1"

    # Which fragments this run removes. A -f path that cannot be read STOPS the
    # run rather than being ignored: cleaning a whole library without the
    # fragments someone asked for would have to be undone by hand.
    fragments_file, ok = cleannamesindividually.fragments_file_for(
        result.values.get("fragmentsOverride"))
    if not ok:
        sys.stderr.write('The fragments file "%s" does not exist or is empty.\n'
                         % result.values.get("fragmentsOverride"))
        return 1

    if not os.path.isdir(root):
        sys.stderr.write(clioptions.missing_dir_text(declaration, root))
        return 1

    # Which ffmpeg of the ones installed, before the preflight asks whether PATH
    # can reach one.
    ffmpegselect.select_ffmpeg()
    ffmpegselect.report_ffmpeg_selection()

    # curl joins the list only when there is a key for it to use: without one the
    # IMDb-id tagging is skipped with a warning of its own.
    tools = ["ffmpeg", "ffprobe", "mkvmerge", "mkvpropedit", "mediainfo"]
    if os.environ.get("tmdbApiKey"):
        tools.append("curl")
    if tooldeps.require_tools(program, tools):
        return 1

    subtitle_work = _settle_subtitle_work()
    ffsubsync_quality = _settle_ffsubsync_quality()

    if not rules._files_below(root, matches=lambda name:
                              enums.lower_extension_of(name) == "mkv"
                              or enums.lower_extension_of(name)
                              in enums.SOURCE_VIDEO_EXTENSIONS):
        return safety.fail_no_relevant_input(
            root, "movies (.mkv, or a video to remux into Matroska: %s)"
            % enums.extension_list(list(enums.SOURCE_VIDEO_EXTENSIONS)))

    ramscratch.init_ram_base()
    ram_root, status = ramscratch.ram_scratch_dir("ingestMovies")
    if status != 0:
        return 1
    ramscratch.add_exit_cleanup([ram_root])

    safety.init_safety_log(os.path.join(ram_root, "safetySkips.log"))
    skips = safety.RunSkipLog()
    safety.init_abort_flag(os.path.join(ram_root, "abortRequested"))
    safety.trap_run_abort()
    # Named above the phases a Ctrl+C can cut short, so an ingest stopped halfway
    # still recaps the renames it held back instead of leaving them buried in
    # output that scrolled away hours ago.
    safety.set_run_footer(safety.report_safety_skips)

    whisper = {}
    if subtitle_work:
        # Done once, before anything is queued, so every worker inherits the
        # answer instead of probing the GPU again. Skipped when the subtitle work
        # is off: the probe IS a whisper run, so without pipx it would spend the
        # startup failing its way down the whole table to reach a conclusion
        # nothing will use.
        from medialib.lib import whisper as whisper_lib
        whisper = whisper_lib.init_whisper_model(
            str(runlog.cpu_count()), ram_root, log)

    state = Run(script_dir=script_dir, ram_root=ram_root, skips=skips,
                fragments_file=fragments_file, whisper=whisper,
                ffsubsync_quality=ffsubsync_quality)

    try:
        _ingest(state, root, subtitle_work)
    finally:
        ramscratch.run_exit_cleanup()
    return 0


def _settle_subtitle_work() -> bool:
    """The subtitle work is all-or-nothing, and that is not the same as optional.

    Neither source of subtitles is worth muxing in unaligned: a downloaded one is
    usually cut for a different release, and a whisper transcript is only as
    trustworthy as the alignment that proves it matches the audio. ffsubsync is
    what decides that and pipx is what runs the two producers, so missing either,
    the choice is between muxing subtitles nobody checked and not producing them
    at all - and both phases are skipped together, said once here rather than as
    a surprise per movie.
    """
    if os.environ.get("SKIP_TOOL_PREFLIGHT"):
        return True
    missing = [name for name in ("ffsubsync", "pipx") if not _has_tool(name)]
    if not missing:
        return True
    log("WARNING: %s not installed - skipping subtitle downloading AND "
        "commentary transcription for this run." % " ".join(missing))
    log("         Both produce a subtitle that only ffsubsync can prove is in "
        "step with the audio, and an unverified")
    log("         subtitle track is worse than none. Everything else (naming, "
        "tags, opus, Dolby Vision, remuxing) runs.")
    log("         To enable them: pipx install ffsubsync, and apt install "
        "pipx.")
    return False


def _tool_help(name: str) -> str:
    """A tool's help page, or "" if it will not print one."""
    try:
        done = subprocess.run([name, "--help"], stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, text=True)
    except OSError:
        return ""
    return done.stdout or ""


def _settle_ffsubsync_quality() -> str:
    """Whether this ffsubsync knows ``--skip-sync-on-low-quality``.

    Probed once here rather than per subtitle: an older ffsubsync handed an
    option it does not know fails argparse and exits non-zero, which BOTH sync
    call sites would read as a failed sync and answer by discarding a perfectly
    good subtitle. Captured whole rather than piped into a matcher, because a
    matcher that stops at its first hit can SIGPIPE the tool and turn a yes into
    a no.
    """
    if not _has_tool("ffsubsync"):
        return "no"
    if "--skip-sync-on-low-quality" in (_tool_help("ffsubsync") or ""):
        return "yes"
    log("WARNING: this ffsubsync has no --skip-sync-on-low-quality, so a bad "
        "alignment gets applied instead of rejected - upgrade ffsubsync to "
        "enable the check")
    return "no"


def _ingest(state, root: str, subtitle_work: bool) -> None:
    log("Starting ingest: " + root)

    log("Phase: cleaning up junk files")
    rules.cleanup(root)
    log("Phase: muxing non-mkv videos into Matroska")
    rules.mkv_mux(root, state.ram_root)
    log("Phase: sorting loose movies into subfolders")
    rules.movies_into_subfolders(root)
    log("Phase: normalising file extensions to lower case")
    safety.lower_case_extensions(root, state.skips)
    log("Phase: sorting bonus material into Plex folders")
    rules.extras_into_subfolders(root, state.skips)

    # Repeated until the names stop changing: some changes around parentheses
    # induce new double spaces, so a pass can create work for the next.
    log("Phase: cleaning up names")
    for _pass in range(MAX_RENAME_PASSES):
        if rules.rename_folders(root, state.fragments_file, state.skips) == 0:
            break
    for _pass in range(MAX_RENAME_PASSES):
        if rules.rename_movies(root, state.fragments_file, state.skips) == 0:
            break

    log("Phase: moving and renaming pre-existing subtitles")
    subtitlefiles.move_subs(root)
    subtitlefiles.rename_subs(root, state.skips)
    if subtitle_work:
        log("Phase: downloading missing subtitles")
        subtitlefiles.download_subs(
            root, os.environ.get("openSubtitlesUser", ""),
            os.environ.get("openSubtitlesPassword", ""),
            rules.MAX_SYNC_OFFSET, rules.MAX_SYNC_QUALITY_OFFSET,
            state.ffsubsync_quality, log)
    else:
        log("Phase: downloading missing subtitles - SKIPPED (no "
            "ffsubsync/pipx, see the warning at startup)")

    log("Phase: tagging movies with IMDb ids (Plex/Jellyfin naming)")
    tmdblookup.tag_plex_ids(root, log)

    log("Phase: refreshing mkv tags and track flags")
    rules.update_tags(root)
    rules.cleanup(root)

    log("Phase: transcoding lossless audio to opus")
    _transcode_opus(state, root)

    if subtitle_work:
        log("Phase: extracting and transcribing commentary tracks")
        commentarytranscription.export_commentary(
            root, rules.read_track_info, rules.is_bonus_folder,
            lambda name: rules.rename(name, state.fragments_file),
            rules.audio_stream_index, state.ram_root, state.whisper,
            runlog.jobs_per_core(CORES_PER_JOB), log, None,
            rules.MAX_WHISPER_SYNC_OFFSET, state.ffsubsync_quality)
    else:
        log("Phase: extracting and transcribing commentary tracks - SKIPPED "
            "(no ffsubsync/pipx, see the warning at startup)")

    # The final phase: an improved copy of each film's main movie, with the
    # original kept as "<name> (old).mkv".
    log("Phase: remuxing improved main movie copies")
    improve_main_movies(state, root)

    rules.cleanup(root)
    log("Phase: checking for folders without a movie")
    check_folders(root)

    safety.print_run_footer()
    log("Ingest complete: " + root)


def _transcode_opus(state, root: str) -> None:
    """Every film's tracks checked, one worker per film: usually only one
    lossless track per file, so the parallelism is at the file level."""
    movies = rules._files_below(root, matches=lambda name:
                                name.endswith(".mkv"))
    jobs = runlog.jobs_per_core(CORES_PER_JOB)
    _run_pool(state, movies, root, jobs)
    safety.exit_if_aborted()


def _in_worker(state, movie: str, root: str) -> None:
    safety.trap_worker_abort()
    ramscratch.adopt_ram_base(getattr(state, "ram_base", ""))
    rules.check_audio_tracks(movie, root)


def _run_pool(state, movies: list, root: str, jobs: int) -> None:
    if jobs <= 1:
        for movie in movies:
            if safety.abort_requested():
                return
            rules.check_audio_tracks(movie, root)
        return

    workerpool.run(movies, jobs, _in_worker,
                   lambda movie: (state, movie, root))


def cli(argv: list | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    return main(argv, commands.program_name(__spec__.name),
                commands.script_dir())


if __name__ == "__main__":
    sys.exit(cli())
