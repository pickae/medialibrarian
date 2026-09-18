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
    durationcheck,
    enums,
    ffmpegselect,
    plexnames,
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
    long_names: tmdblookup.LongNames

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
    """Every film folder's main movie, improved.

    Extras subfolders are skipped, as are folders already carrying an improved
    copy and folders whose several mkvs are several FILMS - there is no telling
    which of those is the feature. Several files of ONE film are improved one by
    one: an edition and a part are each their own container with their own
    tracks, and what makes them one film is that they are named systematically.
    """
    for directory in [root] + rules._folders_below(root):
        if rules.is_bonus_folder(directory):
            continue
        base, _tag = plexnames.untagged_base(os.path.basename(
            directory.rstrip("/")))
        names = [name for name in rules._names_in(directory)
                 if os.path.isfile(os.path.join(directory, name))]
        for name in plexnames.one_film_in(base, names):
            movie = os.path.join(directory, name)
            if _already_improved(movie):
                continue
            state.improve_main_movie(movie)


def _already_improved(movie: str) -> bool:
    """Whether this film's improved copy has already been made, which is what a
    second run over the same library skips itself on.

    The "(old)" copy beside it says so, matched by the name it WOULD be given
    rather than the name it has: it is the one file the tagging leaves alone, so
    it still carries the version name its film was imported under.
    """
    directory, name = os.path.split(movie)
    stem = os.path.splitext(name)[0]
    if os.path.isfile(os.path.join(directory, stem + plexnames.OLD_SUFFIX)):
        return True

    base, tag = plexnames.untagged_base(os.path.basename(
        directory.rstrip("/")))
    for other in rules._names_in(directory):
        if not other.endswith(plexnames.OLD_SUFFIX):
            continue
        kept = other[:-len(plexnames.OLD_SUFFIX)]
        # Guarded, or a stray "(old)" belonging to nothing would read as the
        # plain film's copy: an unreadable stem gives no edition and no part,
        # and that is exactly the plain film's name.
        if not kept.startswith(base):
            continue
        if plexnames.plex_stem(base, tag,
                               *plexnames.read_stem(base, kept)) == stem:
            return True
    return False


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

    if clioptions.args_out_of_range(len(result.positionals), 1, None):
        sys.stderr.write(clioptions.no_args_text(declaration))
        return 1

    # -w turns a dry run into renames, and without -t or -i there is none to
    # turn. Refused rather than ignored: a -tw typed as -w would otherwise be a
    # full ingest, hours of converting and remuxing, on a library asked for
    # nothing but its names.
    if result.values.get("writeTags") and not (result.values.get("tagsOnly")
                                               or result.values.get("idList")):
        sys.stderr.write(clioptions.usage_error_text(
            declaration,
            "-w on its own does nothing to carry out: it is what turns the dry "
            "run of -t or\n-i into real renames, and neither was given. "
            "Without one of them this is a FULL\nINGEST - converting, "
            "transcribing and remuxing - which -w has no part in.\n\n"
            "Did you mean -tw, or -iw?"))
        return 1

    script_dir = script_dir or commands.script_dir()

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

    roots, names = _resolve_roots(declaration, result.positionals)
    if roots is None:
        return 1

    # An id list is only ever read by the tagging phase, so asking for one asks
    # for that phase.
    if result.values.get("tagsOnly") or result.values.get("idList"):
        return _tags_only(program, roots, names,
                          bool(result.values.get("writeTags")),
                          result.values.get("idList") or "")

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

    # Asked of each folder before any of them is set up for, and a folder with
    # nothing to ingest is dropped rather than ending the run: the others were
    # named in the same breath and are not answerable for it. One folder on its
    # own still gets the full refusal, which is the whole of what it was asked
    # to do.
    wanted = "movies (.mkv, or a video to remux into Matroska: %s)" % (
        enums.extension_list(list(enums.SOURCE_VIDEO_EXTENSIONS)))
    ingestable = [root for root in roots
                  if rules._files_below(root, matches=lambda name:
                                        enums.lower_extension_of(name) == "mkv"
                                        or enums.lower_extension_of(name)
                                        in enums.SOURCE_VIDEO_EXTENSIONS)]
    for root in roots:
        if root not in ingestable:
            if len(roots) == 1:
                return safety.fail_no_relevant_input(root, wanted)
            log('Nothing this ingest can read under "%s" - skipped' % root)
    if not ingestable:
        return 1

    ramscratch.init_ram_base()
    ram_root, status = ramscratch.ram_scratch_dir("ingestMovies")
    if status != 0:
        return 1
    ramscratch.add_exit_cleanup([ram_root])

    safety.init_safety_log(os.path.join(ram_root, "safetySkips.log"))
    durationcheck.init_log(os.path.join(ram_root, "lengthMismatch.log"))
    skips = safety.RunSkipLog()
    long_names = tmdblookup.LongNames()
    safety.init_abort_flag(os.path.join(ram_root, "abortRequested"))
    safety.trap_run_abort()
    # Named above the phases a Ctrl+C can cut short, so an ingest stopped halfway
    # still recaps the renames it held back instead of leaving them buried in
    # output that scrolled away hours ago.
    def recap() -> None:
        safety.report_safety_skips()
        durationcheck.report()
        for line in long_names.report():
            sys.stderr.write(line + "\n")

    safety.set_run_footer(recap)

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
                ffsubsync_quality=ffsubsync_quality, long_names=long_names)

    try:
        for root in ingestable:
            _ingest(state, root, subtitle_work)
    finally:
        ramscratch.run_exit_cleanup()
    return workerpool.exit_status(1 if durationcheck.failures() else 0)


# Where the films TMDb could not identify are listed when -i named no file of
# its own. In the current directory and not in the library: a run over the
# library deletes stray .txt files as junk, and this is a worklist rather than
# part of the collection.
#
# One file per folder given, named after it, because the lists are worked
# through by hand: two libraries' unnamed films in one file would be a worklist
# nobody could tell apart, and each folder overwriting the last one's file
# would be worse.
UNMATCHED_LIST = "ingest-movies-unmatched-%s.tsv"

# And the folders holding more than one film, which no id can settle - named
# the same way and for the same reason.
AMBIGUOUS_LIST = "ingest-movies-ambiguous-%s.txt"

# And what a dry run WOULD have renamed. A library of any size prints thousands
# of those lines, and a file is where they can be read through rather than
# scrolled past.
RENAMES_LIST = "ingest-movies-renames-%s.txt"

# And how close the folders in the first two lists came - what TMDb was asked,
# what it offered, and why none of it was certain. A dry run only: it is what
# says whether the two lists above are the right length, and the answer is only
# worth having before anything has been renamed.
NEAR_MISS_LIST = "ingest-movies-nearmisses-%s.txt"

# And the folders holding one film under several of its own titles, which is
# the one outcome nothing else records: no name changed, so the rename list is
# silent about them, and they are not a problem, so the other two are too.
ALIAS_LIST = "ingest-movies-othertitles-%s.txt"


def _tags_only(program: str, roots: list, names: list, write: bool,
               id_list: str) -> int:
    """The naming phase on its own: the id, the editions and a split film's
    stacking token, and not one thing else.

    A dry run unless asked otherwise, because this is the phase whose whole
    effect is the names - there is no output to inspect afterwards and compare,
    only the library it already renamed. Everything the full run sets up first -
    the RAM scratch, the whisper model, the ffmpeg it picked - belongs to work
    this mode does not do, so none of it is built.

    Two modes share this, and what they write is what tells them apart. -t is
    the pass that has nothing to go on but TMDb: it tags what it can and writes
    down everything it could not, one set of lists per folder, for someone to
    read and fill in. -i is the pass that reads a filled-in list back, and it
    writes no lists at all - they are -t's, and a run that rewrote them from a
    partial answer would lose the question. What -i shows is what those
    hand-written ids would do, on screen, and -iw is what carries it out.

    Under -t every folder given is tagged in turn, each with a list of its own
    named after it. Under -i the one file someone named holds them all and is
    read once for every folder.
    """
    # First, being about what was typed rather than about the environment: a -i
    # file that cannot be read STOPS the run, the way a -f one does. Read as an
    # empty list it would throw away every id in the file someone meant and put
    # the whole library back through the lookups that already failed on it,
    # which is the one outcome -i exists to avoid.
    blank: list = []
    ids = tmdblookup.read_id_list(id_list, blank) if id_list else {}
    if ids is None:
        sys.stderr.write('The id list "%s" cannot be read.\nNothing was '
                         "changed.\n" % id_list)
        return 1

    if not os.environ.get("tmdbApiKey"):
        sys.stderr.write("tmdbApiKey is not set, so there is nothing to tag "
                         "with.\n")
        return 1
    if tooldeps.require_tools(program, ["curl"]):
        return 1

    if id_list:
        log("Read %d hand-written id(s) from %s, and %d row(s) still blank"
            % (len(ids), id_list, len(blank)))

    skips = safety.RunSkipLog()
    long_names = tmdblookup.LongNames()
    log("Phase: tagging movies with IMDb ids (Plex/Jellyfin naming)"
        + ("" if write else " - DRY RUN, nothing will be renamed"))
    # Every folder's unnamed films together, for the one file -i named. Only
    # that mode collects them: without -i each folder's are written as its own
    # folder is finished.
    shared: list = []
    # Every film folder the walk considered, across all the folders given: a row
    # in the id list is checked against the whole run rather than against each
    # folder in turn, or the one that named a film in the second library would
    # be called missing by the first.
    seen: set | None = set() if id_list else None
    for root, name in zip(roots, names, strict=True):
        if len(roots) > 1:
            log('Tagging "%s"' % root)
        unmatched: list = []
        ambiguous: list | None = None if id_list else []
        planned: list | None = None if id_list else []
        alias_titles: list | None = None if id_list else []
        near_misses: list | None = None if id_list or write else []
        # Recursive here and only here: this mode is pointed at a library, where
        # a full ingest is pointed at the folder that holds the films.
        tmdblookup.tag_plex_ids(root, log, skips, dry_run=not write, ids=ids,
                                unmatched=unmatched, recursive=True,
                                ambiguous=ambiguous, planned=planned,
                                near_misses=near_misses,
                                aliases=alias_titles, seen=seen,
                                skip=set(blank), long_names=long_names)
        if id_list:
            shared += unmatched
            continue
        if unmatched:
            listing = UNMATCHED_LIST % name
            if tmdblookup.write_id_list(listing, unmatched, None, log):
                log('%d film(s) in "%s" could not be identified - listed in '
                    '"%s" to fill in by hand' % (len(unmatched), root, listing))
        if ambiguous:
            listing = AMBIGUOUS_LIST % name
            if tmdblookup.write_ambiguous_list(listing, ambiguous, root, log):
                log('%d folder(s) in "%s" hold more than one film - listed in '
                    '"%s"' % (len(ambiguous), root, listing))
        if planned:
            listing = RENAMES_LIST % name
            if tmdblookup.write_rename_list(listing, planned, root, log):
                log('%d rename(s) in "%s" would be made - listed in "%s"'
                    % (len(planned), root, listing))
        if alias_titles:
            listing = ALIAS_LIST % name
            if tmdblookup.write_alias_list(listing, alias_titles, root, log):
                log('%d folder(s) in "%s" hold one film under several of its '
                    'titles - listed in "%s"'
                    % (len(alias_titles), root, listing))
        if near_misses:
            listing = NEAR_MISS_LIST % name
            if tmdblookup.write_near_miss_list(listing, near_misses, root, log):
                log('%d folder(s) in "%s" were left alone - what was asked and '
                    'what came back is in "%s"'
                    % (len(near_misses), root, listing))

    for line in skips.report():
        sys.stderr.write(line + "\n")
    for line in long_names.report():
        sys.stderr.write(line + "\n")

    # A row someone filled in that names no folder here. Said per row, and the
    # run carries on to the next: the other ids are somebody's afternoon of
    # looking things up, and one stale line is not a reason to drop them. A row
    # still BLANK is not this - those are simply the ones still to do.
    stale = sorted(set(ids) - (seen or set())) if id_list else []
    if stale:
        sys.stderr.write(
            "\nERROR: %d row(s) in %s carry an id but name no film folder "
            "that is here.\nThe id was not applied to anything. Check the "
            "name against the library, or\ndrop the row:\n"
            % (len(stale), id_list))
        for base in stale:
            sys.stderr.write('  "%s"\t%s\n' % (base, ids[base]))

    # The ids already in the file are written back beside what is still unnamed:
    # they are the only record anywhere of a lookup someone did by hand, and
    # dropping them would un-identify the film on the very next run. Only -iw
    # rewrites it, because -i is read while the file is being filled in and a
    # dry run has no business editing the document it was handed.
    if id_list and write:
        if tmdblookup.write_id_list(id_list, shared, ids, log) and shared:
            log('%d film(s) could not be identified - listed in "%s" to fill '
                "in by hand" % (len(shared), id_list))

    # Once, at the end, rather than a line per film: a row left blank was never
    # asked about, so there is nothing per film to say about it.
    if id_list:
        unasked = len(set(blank) & (seen or set()))
        if unasked:
            log("%d film(s) had no id filled in and were not asked about"
                % unasked)

    if not write:
        log("Dry run: nothing was renamed. Pass -w to carry these out.")
    return 0


def _capitalise(text: str) -> str:
    """bash's ``${var^}``: the first character upper-cased, the rest untouched."""
    return text[:1].upper() + text[1:]


def _resolve_roots(declaration, arguments: list):
    """Every folder given, checked and named before any of them is touched - so
    a typo in the third of five is found now rather than after the first two
    have been ingested.

    Returns (roots, names), or (None, None) once it has written the refusal.
    The name is what a folder's lists are called after, so the same folder named
    twice is one folder, and two folders of the same name are a refusal: their
    lists would be written to one path and the second would silently replace the
    first.
    """
    roots: list = []
    names: list = []
    for argument in arguments:
        if not os.path.isdir(argument):
            sys.stderr.write(clioptions.missing_dir_text(declaration, argument))
            return None, None
        absolute = os.path.realpath(argument)
        if absolute in roots:
            log('Ignoring "%s": that folder is already in this run' % argument)
            continue
        name = os.path.basename(absolute)
        name = _capitalise(name) if name not in ("", "/") else "root"
        if name in names:
            sys.stderr.write(
                '\nError: two of the folders given are both named "%s":\n'
                "  %s\n  %s\n"
                "Their lists would be written to one file, and the second would "
                "replace the\nfirst. Rename one, or tag them in separate runs "
                "from separate folders.\nNothing was changed.\n"
                % (name, roots[names.index(name)], absolute))
            return None, None
        roots.append(absolute)
        names.append(name)
    return roots, names


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
    log("Phase: muxing non-Matroska videos into Matroska")
    rules.mkv_mux(root)
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

    log("Phase: refreshing mkv tags and track flags")
    rules.update_tags(root)
    rules.cleanup(root)

    log("Phase: transcoding lossless audio to opus")
    _transcode_opus(state, root)

    if subtitle_work:
        log("Phase: extracting and transcribing commentary tracks")
        from medialib.lib import whisper as whisper_lib
        jobs = whisper_lib.WHISPER_JOBS
        commentarytranscription.export_commentary(
            root, rules.read_track_info, rules.is_bonus_folder,
            lambda name: rules.rename(name, state.fragments_file),
            rules.audio_stream_index, state.ram_root, state.whisper,
            jobs, log,
            lambda records, _queue: _drain_commentary(state, records, jobs),
            rules.MAX_WHISPER_SYNC_OFFSET, state.ffsubsync_quality)
        safety.exit_if_aborted()
    else:
        log("Phase: extracting and transcribing commentary tracks - SKIPPED "
            "(no ffsubsync/pipx, see the warning at startup)")

    # An improved copy of each film's main movie, with the original kept as
    # "<name> (old).mkv".
    log("Phase: remuxing improved main movie copies")
    improve_main_movies(state, root)

    # Last, once every film is the file it is going to stay: a rename costs
    # nothing to hold back, and the phases above keep the names they gave.
    log("Phase: tagging movies with IMDb ids (Plex/Jellyfin naming)")
    tmdblookup.tag_plex_ids(root, log, state.skips,
                            long_names=state.long_names)

    rules.cleanup(root)
    log("Phase: checking for folders without a movie")
    check_folders(root)

    safety.print_run_footer()
    log("Ingest complete: " + root)


def _transcribe_one(state, record: str) -> None:
    commentarytranscription.transcribe_commentary(
        record, state.whisper, rules.MAX_WHISPER_SYNC_OFFSET,
        state.ffsubsync_quality, state.ram_root, log)


def _in_transcribe_worker(state, record: str) -> None:
    """One queued transcription, in a worker PROCESS - the interrupt handling
    belongs here and not in the transcription, which the serial path runs in the
    RUN's own process."""
    safety.trap_worker_abort()
    ramscratch.adopt_ram_base(getattr(state, "ram_base", ""))
    _transcribe_one(state, record)


def _drain_commentary(state, records: list, jobs: int) -> None:
    """The queue exportCommentary filled, at ``jobs`` workers - a width that is
    whisper's and not the core count, one run already spanning the GPU or every
    CPU thread."""
    if jobs <= 1:
        for record in records:
            if safety.abort_requested():
                return
            _transcribe_one(state, record)
        return

    workerpool.run(records, jobs, _in_transcribe_worker,
                   lambda record: (state, record))


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
