"""ingest-movies's run: the phase sequence, and the improved-copy remux.

The decisions - which track is kept, dropped or swapped, which folder is bonus
material, what a name cleans to - are `medialib/cli/ingest_movies.py`. This is
what those decisions are carried out by, and it is separate for the same reason
the census is: the rules are worth reading on their own, and the run around them
is mostly plumbing.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile

from medialib import commands
from medialib.cli import ingest_movies as rules
from medialib.cli.ingest_movies import log
from medialib.lib import (
    chapterdb,
    cleannamesindividually,
    clioptions,
    commentarynames,
    commentarytranscription,
    dolbyvision,
    donelog,
    durationcheck,
    dvdcompare,
    dynamicqueue,
    enums,
    ffmpegselect,
    fixedpoint,
    imdbdata,
    overwrite,
    plexnames,
    ramscratch,
    research,
    runlog,
    safety,
    subtitleads,
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

# A commentary transcript of a film running M minutes weighs on the order of a
# kilobyte per minute - the words a commentator speaks are proportional to the
# film's length. A sidecar that comes in under half of that is the transcript a
# version of the script wrote before it could tell a commentary's language, which
# forced a non-English commentary through the English model and came back almost
# empty. Below it, a -a run discards the sidecar and re-transcribes.
MIN_COMMENTARY_KB_PER_MINUTE = 0.5


class Run:
    """One run's settings, and the work its jobs do."""

    # Declared, not defaulted: the settings dict supplies every one, so a name
    # it does not carry is still an AttributeError at the read.
    script_dir: str
    ram_root: str
    skips: safety.RunSkipLog
    fragments_file: str
    whisper: dict
    # What settling ``whisper`` said, still to be printed: held back until
    # there is a commentary to transcribe, and empty once it has been.
    whisper_said: list
    ffsubsync_quality: str
    long_names: tmdblookup.LongNames
    unfixed_movies: list

    def __init__(self, **settings) -> None:
        self.__dict__.update(settings)

    # --- the improved copy ----------------------------------------------------

    def improve_main_movie(self, movie: str) -> None:
        """Remux ONE main movie into an improved copy, keeping the original
        beside it as "<name> (old).mkv" - so nothing is ever lost and a second
        run is a no-op, the "(old)" sibling being what makes the folder skip
        itself."""
        # The heading is held back until there is something to put under it,
        # so a film that needs nothing done is one line rather than two naming
        # it twice.
        headed: list[bool] = []

        def heading() -> None:
            if not headed:
                log("Improving main movie: " + movie)
                headed.append(True)

        def say(line: str) -> None:
            heading()
            log(line)

        base = os.path.splitext(movie)[0]

        tracks = rules._identify(movie)
        if not tracks:
            say("  Skipping (no tracks read): " + movie)
            return
        rules._object_flags(movie, tracks)

        changed = rules.decide_actions(tracks, base)
        for position, winner in rules.apply_surround_ladder(tracks):
            changed = True
            say("  Surround ladder: dropping audio track %s (%s, %s) in favour "
                "of %s" % (tracks[position].id, tracks[position].name,
                           tracks[position].language, tracks[winner].name))

        transcripts = rules.gather_commentary_transcripts(base, tracks,
                                                          say=say)
        if transcripts:
            changed = True

        job = self.decide_dolby_vision_job(movie, tracks, say=say)

        # An overstated Dolby Vision LEVEL, corrected in place. This is not part
        # of the remux and must not wait for one: the level is a container
        # element, so correcting it is two bytes and no remux at all, and a film
        # whose level is the only thing wrong with it needs nothing else done -
        # it would otherwise return just below, uncorrected.
        dolbyvision.normalise_config_level(movie, script_dir=self.script_dir,
                                           log=say)

        if not changed and not job["wanted"]:
            log(("  No improvements needed: " if headed
                 else "No improvements needed: ") + movie)
            return

        heading()
        self.remux(movie, base, tracks, transcripts, job)

    def decide_dolby_vision_job(self, movie: str, tracks: list,
                                say=None) -> dict:
        """``decideDolbyVisionJob``: which Dolby Vision job this file needs, if
        any - converting a real dual-layer profile 7 to 8.1, or dropping a claim
        of ANY profile that the video does not back up with an RPU.

        Only the cheap probes and the eligibility checks happen here; the work on
        the stream is deferred until it is certain the remux runs at all.
        ``say`` is where its warnings go, the run's log when not given.
        """
        say = say or log
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
                say("  WARNING: dovi_tool not installed, leaving Dolby Vision "
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
            say("  WARNING: %s needs exactly one video track, this has %d, "
                "leaving as is: %s" % (description, job["video_count"], movie))
            job["action"] = ""
        elif not info["FPS_SPEC"]:
            say("  WARNING: %s needs a frame rate, which mediainfo does not "
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
        path, on_disk, status = ramscratch.ram_scratch_dir_for(
            _scratch_need(rules._size_of(movie), job["wanted"],
                          job["stream_size"]),
            "improveMovie", os.path.dirname(movie))
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
            # Both report a status, where 0 is the success. 0 is falsy here,
            # so the == 0 is what makes the boolean.
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
            # Always given, even empty: an opus left by an older run can still
            # carry the movie's title, which mkvmerge would otherwise take as
            # the name of a track that had none.
            name = track.name if track.name and track.name != "null" else ""
            extra += ["--track-name", "0:" + name]
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


def _scratch_need(film_bytes: int, video_work: bool, stream_size) -> str:
    """The bytes one film's improvement needs in its scratch, as text: a whole
    copy of the film, and with Dolby Vision work the prepared video stream on
    top.

    The stream is its measured size where mediainfo reported one, and the
    whole film as the upper bound where it did not - a video track cannot be
    larger than the file it is in. An unreadable film size is the empty
    string rather than a number, and that is what tells the scratch it has
    nothing to size itself against.
    """
    if not film_bytes:
        return ""
    need = film_bytes
    if video_work:
        need += (int(stream_size) if str(stream_size).isdigit()
                 else film_bytes)
    return str(need)


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

    # -w carries out a dry run, and without -t, -i or -s there is none to
    # carry out. Refused rather than ignored: a -tw typed as -w would otherwise
    # be a full ingest, hours of converting and remuxing, on a library asked
    # for nothing but its names.
    if result.values.get("writeTags") and not (
            result.values.get("tagsOnly") or result.values.get("idList")
            or result.values.get("subtitlesOnly")
            or result.values.get("commentaryNames")):
        for letter, key in (("a", "commentaryOnly"), ("c", "chaptersOnly")):
            if result.values.get(key):
                # The transcription and the chapter lookup are not dry runs,
                # so there is nothing for -w to carry out.
                sys.stderr.write(clioptions.usage_error_text(
                    declaration,
                    "-w has no part in -%s: it carries out the dry run of -t, "
                    "-i or -s, and -%s\nis not a dry run. Drop the -w."
                    % (letter, letter)))
                return 1
        sys.stderr.write(clioptions.usage_error_text(
            declaration,
            "-w on its own does nothing to carry out: it is what turns the dry "
            "run of -t,\n-i or -s into the real thing, and none was given. "
            "Without one of them this is a\nFULL INGEST - converting, "
            "transcribing and remuxing - which -w has no part in.\n\n"
            "Did you mean -tw, -iw or -sw?"))
        return 1

    # -a is the commentary phase on its own and -t and -i the tagging phase:
    # two runs over the same folders, neither knowing what the other would do
    # to the names, so they are refused together rather than run in a guessed
    # order.
    if result.values.get("commentaryOnly") and (result.values.get("tagsOnly")
                                                or result.values.get("idList")):
        sys.stderr.write(clioptions.usage_error_text(
            declaration,
            "-a transcribes the commentary tracks and nothing else, and -t and "
            "-i tag and nothing\nelse: they are two runs, not one. Give the "
            "folders to each of them in turn."))
        return 1

    # -s the same, against both: the subtitles are searched for by the id the
    # tagging gives a film's name, so the order matters and is not guessed.
    if result.values.get("subtitlesOnly") and (
            result.values.get("commentaryOnly")
            or result.values.get("tagsOnly") or result.values.get("idList")):
        sys.stderr.write(clioptions.usage_error_text(
            declaration,
            "-s tests and fetches subtitles and nothing else, and -a, -t and -i "
            "each do a\nphase of their own: they are two runs, not one. Tag "
            "first (-tw), so the\nsubtitles are searched for by the ids it "
            "gives, then run -s."))
        return 1

    # -n the same, against every other: it names the tracks the transcription
    # and the tagging name their files after, so the order matters and is not
    # guessed.
    if result.values.get("commentaryNames") and (
            result.values.get("commentaryOnly")
            or result.values.get("chaptersOnly")
            or result.values.get("subtitlesOnly")
            or result.values.get("tagsOnly") or result.values.get("idList")):
        sys.stderr.write(clioptions.usage_error_text(
            declaration,
            "-n names the commentary tracks and nothing else, and -a, -c, -s, "
            "-t and -i each do\na phase of their own: they are two runs, not "
            "one. Transcribe first (-a), so the\ntranscripts are there to "
            "tell the commentaries apart by, then run -n."))
        return 1

    # -c the same, against every other phase of its own: the chapters are
    # looked up by the title the tagging gives a folder.
    if result.values.get("chaptersOnly") and (
            result.values.get("commentaryOnly") or result.values.get("tagsOnly")
            or result.values.get("idList")
            or result.values.get("subtitlesOnly")):
        sys.stderr.write(clioptions.usage_error_text(
            declaration,
            "-c looks up chapters and nothing else, and -a, -t, -i and -s each "
            "do a phase\nof their own: they are two runs, not one. Tag first "
            "(-tw), so the chapters are\nlooked up by the titles it gives, "
            "then run -c."))
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

    # Commentary only is the one phase of the full ingest it names, run on the
    # folders given: the walk and the transcription, and not the rest of the
    # run around them.
    if result.values.get("commentaryOnly"):
        if not _may_overwrite(script_dir, names, (), (COMMENTARY_ORPHANS_LIST,
                                                    UNFIXED_MOVIES_LIST)):
            return 1
        return _commentary_only(program, script_dir, roots, fragments_file)

    if result.values.get("commentaryNames"):
        write = bool(result.values.get("writeTags"))
        if tooldeps.require_tools(program, ["curl", "mkvmerge"]
                                  + (["mkvpropedit"] if write else [])):
            return 1
        if not _may_overwrite(script_dir, names, (COMMENTARY_NAMES_LIST,)):
            return 1
        site = dvdcompare.Site(dvdcompare.directory(script_dir), log)
        return _commentary_names(roots, names, write, site.releases,
                                 fragments_file, script_dir, queued=True)

    if result.values.get("subtitlesOnly"):
        if not _may_overwrite(script_dir, names, (SUBTITLES_LIST,)):
            return 1
        return _subtitles_only(program, script_dir, roots, names,
                               bool(result.values.get("writeTags")))

    if result.values.get("chaptersOnly"):
        return _chapters_only(program, roots)

    # An id list is only ever read by the tagging phase, so asking for one asks
    # for that phase.
    if result.values.get("tagsOnly") or result.values.get("idList"):
        if not _may_overwrite(script_dir, names,
                              *_tag_lists(bool(result.values.get("idList")),
                                          bool(result.values.get("writeTags")))):
            return 1
        return _tags_only(program, script_dir, roots, names,
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
    # Only the subtitle work syncs anything, so with it off there is no
    # alignment to check and nothing to warn about how well it would be.
    ffsubsync_quality = _settle_ffsubsync_quality() if subtitle_work else "no"
    if subtitle_work:
        _warn_without_subcleaner()

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
    # The movies a transcription was skipped for because their name carried no
    # dot before its extension, gathered across every folder and reported at the
    # end: the name says the film cannot be told from its folder, and the film
    # is what someone has to look at.
    unfixed_movies: list = []

    # Named above the phases a Ctrl+C can cut short, so an ingest stopped halfway
    # still recaps the renames it held back instead of leaving them buried in
    # output that scrolled away hours ago.
    def recap() -> None:
        safety.report_safety_skips()
        durationcheck.report()
        for line in long_names.report():
            sys.stderr.write(line + "\n")
        _report_unfixed_movies(unfixed_movies)

    safety.set_run_footer(recap)

    whisper = {}
    whisper_said: list = []
    if subtitle_work:
        # Done once, before anything is queued, so every worker inherits the
        # answer instead of probing the GPU again. Skipped when the subtitle work
        # is off: the probe IS a whisper run, so without pipx it would spend the
        # startup failing its way down the whole table to reach a conclusion
        # nothing will use. What it found is only said once there is a
        # commentary to transcribe with it: most films have none, and a
        # library without one would otherwise open on the GPU's plan for work
        # it never gets.
        from medialib.lib import whisper as whisper_lib
        whisper = whisper_lib.init_whisper_model(
            str(runlog.cpu_count()), ram_root, whisper_said.append)

    state = Run(script_dir=script_dir, ram_root=ram_root, skips=skips,
                fragments_file=fragments_file, whisper=whisper,
                whisper_said=whisper_said,
                ffsubsync_quality=ffsubsync_quality, long_names=long_names,
                unfixed_movies=unfixed_movies)

    listed = dict(zip(roots, names, strict=True))
    try:
        for root in ingestable:
            _ingest(state, root, subtitle_work, listed[root])
    finally:
        ramscratch.run_exit_cleanup()
    return workerpool.exit_status(1 if durationcheck.failures() else 0)


# Every file this command leaves behind about a run goes in one folder of its
# own under the script directory's logs/, and not in the library: a run over the
# library deletes stray .txt files as junk, and these are worklists rather than
# part of the collection.
LOGS = "ingest-movies"

# Where the films TMDb could not identify are listed when -i named no file of
# its own. One file per folder given, named after it, because the lists are
# worked through by hand: two libraries' unnamed films in one file would be a
# worklist nobody could tell apart, and each folder overwriting the last one's
# file would be worse.
UNMATCHED_LIST = os.path.join(LOGS, "ingest-movies-unmatched-%s.tsv")

# And the folders holding more than one film, which no id can settle - named
# the same way and for the same reason.
AMBIGUOUS_LIST = os.path.join(LOGS, "ingest-movies-ambiguous-%s.txt")

# And what a dry run WOULD have renamed. A library of any size prints thousands
# of those lines, and a file is where they can be read through rather than
# scrolled past.
RENAMES_LIST = os.path.join(LOGS, "ingest-movies-renames-%s.txt")

# And how close the folders in the first two lists came - what TMDb was asked,
# what it offered, and why none of it was certain. A dry run only: it is what
# says whether the two lists above are the right length, and the answer is only
# worth having before anything has been renamed.
NEAR_MISS_LIST = os.path.join(LOGS, "ingest-movies-nearmisses-%s.txt")

# And the folders holding one film under several of its own titles, which is
# the one outcome nothing else records: no name changed, so the rename list is
# silent about them, and they are not a problem, so the other two are too.
ALIAS_LIST = os.path.join(LOGS, "ingest-movies-othertitles-%s.txt")

# And the renames held back because their target was already there - per folder,
# like the rest, since the fix for each is made in that folder.
CONFLICTS_LIST = os.path.join(LOGS, "ingest-movies-conflicts-%s.txt")

# And the films kept in more than one folder. One file for the whole run rather
# than one per folder: the two copies are as likely to be in two of the folders
# given as in one.
DUPLICATES_LIST = os.path.join(LOGS, "ingest-movies-duplicates.txt")

# And the subtitles a -s run found out of step with their film, or could not
# test at all: the worklist of a dry run, and under -w the record of what was
# deleted.
SUBTITLES_LIST = os.path.join(LOGS, "ingest-movies-subtitles-%s.txt")

# And the films a -sw run has finished, by id, which every -s run after it
# skips: the ffsubsync passes are what a run over a library spends its hours
# on. One file for every folder, appended to rather than replaced - a film is
# known by its id however it has since been moved or renamed.
SUBTITLES_DONE = os.path.join(LOGS, "ingest-movies-subtitles-done.txt")

# And the commentary tracks a -n run named, or would name, and the films it
# left alone with the reason for each.
COMMENTARY_NAMES_LIST = os.path.join(LOGS,
                                     "ingest-movies-commentarynames-%s.txt")

# And the two lists of a -a run, one for the whole run rather than per folder.
COMMENTARY_ORPHANS_LIST = os.path.join(LOGS, "commentaryOrphans.txt")
UNFIXED_MOVIES_LIST = os.path.join(LOGS, "unfixedMovies.txt")


def _tag_lists(id_list: bool, write: bool) -> tuple[tuple, tuple]:
    """The lists a -t or -i run can write: (per folder, for the whole run).
    Under -i only the conflicts and duplicates, and the near misses only on a
    dry run - the same choices :func:`_tags_only` makes."""
    if id_list:
        return (CONFLICTS_LIST,), (DUPLICATES_LIST,)
    per_folder = (CONFLICTS_LIST, UNMATCHED_LIST, AMBIGUOUS_LIST, RENAMES_LIST,
                  ALIAS_LIST) + (() if write else (NEAR_MISS_LIST,))
    return per_folder, (DUPLICATES_LIST,)


def _may_overwrite(script_dir: str, names: list, per_folder: tuple,
                   shared: tuple = ()) -> bool:
    """Asked before a phase that writes lists runs at all: whether the ones an
    earlier run left for these folders may be replaced."""
    return overwrite.confirm_overwrite(
        [commands.logs_file(script_dir, pattern % name)
         for name in names for pattern in per_folder]
        + [commands.logs_file(script_dir, path) for path in shared])


def _tags_only(program: str, script_dir: str, roots: list, names: list,
               write: bool, id_list: str) -> int:
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
    writes none of those lists - they are -t's, and a run that rewrote them
    from a partial answer would lose the question. The conflicts and the
    duplicates are written by both, being a record of the run and not a
    question. What -i shows is what those hand-written ids would do, on
    screen, and -iw is what carries it out.

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
        log("Read %d hand-written id(s) from %s" % (len(ids), id_list)
            + (", and %d row(s) still blank" % len(blank) if blank else ""))

    long_names = tmdblookup.LongNames()
    # This pass and not the full ingest: it is the one run over a library
    # someone is sitting in front of, where the films TMDb cannot name are
    # the whole point of running it - and the copy is most of a gigabyte to
    # fetch the first time.
    imdb = imdbdata.prepare(imdbdata.directory(script_dir), log)
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
    # Every folder's id across all the folders given, since the same film kept
    # twice is as likely to be on two disks as in two corners of one.
    tagged: dict = {}
    for root, name in zip(roots, names, strict=True):
        if len(roots) > 1:
            log('Tagging "%s"' % root)
        unmatched: list = []
        ambiguous: list | None = None if id_list else []
        planned: list | None = None if id_list else []
        alias_titles: list | None = None if id_list else []
        near_misses: list | None = None if id_list or write else []
        skips = safety.RunSkipLog()
        # Recursive here and only here: this mode is pointed at a library, where
        # a full ingest is pointed at the folder that holds the films.
        tmdblookup.tag_plex_ids(root, log, skips, dry_run=not write, ids=ids,
                                unmatched=unmatched, recursive=True,
                                ambiguous=ambiguous, planned=planned,
                                near_misses=near_misses,
                                aliases=alias_titles, seen=seen,
                                skip=set(blank), long_names=long_names,
                                imdb=imdb, tagged=tagged)
        # Under -i as well: this is not one of -t's worklists but a record of
        # what the run did, and a -iw run refuses renames like any other.
        if skips.skips:
            listing = commands.logs_file(script_dir, CONFLICTS_LIST % name)
            if tmdblookup.write_conflict_list(listing, skips.skips, root, log):
                log('%d rename(s) in "%s" were held back, the name already '
                    'taken - listed in "%s"' % (len(skips.skips), root, listing))
        if id_list:
            shared += unmatched
            continue
        if unmatched:
            listing = commands.logs_file(script_dir, UNMATCHED_LIST % name)
            if tmdblookup.write_id_list(listing, unmatched, None, log):
                log('%d film(s) in "%s" could not be identified - listed in '
                    '"%s" to fill in by hand' % (len(unmatched), root, listing))
        if ambiguous:
            listing = commands.logs_file(script_dir, AMBIGUOUS_LIST % name)
            if tmdblookup.write_ambiguous_list(listing, ambiguous, root, log):
                log('%d folder(s) in "%s" hold more than one film - listed in '
                    '"%s"' % (len(ambiguous), root, listing))
        if planned:
            listing = commands.logs_file(script_dir, RENAMES_LIST % name)
            if tmdblookup.write_rename_list(listing, planned, root, log):
                log('%d rename(s) in "%s" would be made - listed in "%s"'
                    % (len(planned), root, listing))
        if alias_titles:
            listing = commands.logs_file(script_dir, ALIAS_LIST % name)
            if tmdblookup.write_alias_list(listing, alias_titles, root, log):
                log('%d folder(s) in "%s" hold one film under several of its '
                    'titles - listed in "%s"'
                    % (len(alias_titles), root, listing))
        if near_misses:
            listing = commands.logs_file(script_dir, NEAR_MISS_LIST % name)
            if tmdblookup.write_near_miss_list(listing, near_misses, root, log):
                log('%d folder(s) in "%s" were left alone - what was asked and '
                    'what came back is in "%s"'
                    % (len(near_misses), root, listing))

    for line in long_names.report():
        sys.stderr.write(line + "\n")
    duplicates = tmdblookup.duplicate_films(tagged)
    if duplicates:
        listing = commands.logs_file(script_dir, DUPLICATES_LIST)
        if tmdblookup.write_duplicate_list(listing, duplicates, log):
            log('WARNING: %d film(s) are kept in more than one folder - listed '
                'in "%s"' % (len(duplicates), listing))

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


def _subtitles_only(program: str, script_dir: str, roots: list, names: list,
                    write: bool) -> int:
    """The subtitle phase on its own, and a test of the subtitles already there.

    Every ``<movie>.xx.srt`` beside a film anywhere under each folder given is
    put to the test a downloaded one is kept by - the alignment ffsubsync finds
    has to stand out from every other offset - and, like -t, this is a DRY RUN
    unless asked otherwise: a subtitle thrown out is gone. The dry run tests a
    copy, says what each would come to and lists the ones out of step; -w syncs
    the ones in step, deletes the rest, and then downloads what that film is
    missing the way a full ingest does, which puts each download to the same
    test, before going on to the next. A subtitle thrown out here is therefore
    fetched again in the same run. Each film is announced with its place in the
    folder, so one nothing is done to is named all the same.

    Nothing else the full run does is set up: no RAM scratch, no whisper, no
    renaming, and no commentary - its transcripts are named after their track
    and are never one of the sidecars tested.
    """
    # Which ffmpeg of the ones installed, before the preflight asks whether PATH
    # can reach one.
    ffmpegselect.select_ffmpeg()
    ffmpegselect.report_ffmpeg_selection()

    # ffsubsync reads the film's speech with ffmpeg, and ffprobe says which
    # subtitle needs converting to SubRip first. pipx runs the downloads, which
    # only -w makes.
    tools = ["ffmpeg", "ffprobe", "ffsubsync"] + (["pipx"] if write else [])
    if tooldeps.require_tools(program, tools):
        return 1

    # The test IS ffsubsync's refusal, so an ffsubsync that cannot refuse has
    # nothing to test with: every subtitle would pass, and -w would call a
    # library of wrong ones in step. The refusal below says so itself, so the
    # probe's own warning about it would only say it twice.
    ffsubsync_quality = _settle_ffsubsync_quality(judged="a subtitle",
                                                  say_unchecked=False)
    if ffsubsync_quality == "no":
        sys.stderr.write("This ffsubsync cannot refuse an alignment, so every "
                         "subtitle would pass the test.\nUpgrade ffsubsync. "
                         "Nothing was changed.\n")
        return 1

    providers = subtitlefiles.providers_to_ask() if write else ()
    download = bool(providers)
    if write and subtitlefiles.missing_login_warning(providers):
        # Said once here rather than once per film and language.
        log(subtitlefiles.missing_login_warning(providers)
            + ("" if download else ", the subtitles already here are only "
               "tested"))
    _warn_without_subcleaner()

    done = donelog.DoneLog(commands.logs_file(script_dir, SUBTITLES_DONE))
    totals = {"kept": 0, "discarded": 0, "untested": 0}
    for root, name in zip(roots, names, strict=True):
        log('Phase: testing the subtitles already beside the films in "%s"'
            % root + (" and downloading the missing" if download else "")
            + ("" if write else " - DRY RUN, nothing will be changed"))
        verdicts = subtitlefiles.check_subs(
            root, rules.MAX_SYNC_OFFSET, rules.MAX_SYNC_QUALITY_OFFSET,
            ffsubsync_quality, write, log, providers or None, done)
        for verdict, found in verdicts.items():
            totals[verdict] += len(found)
        _write_subtitle_list(commands.logs_file(script_dir,
                                                SUBTITLES_LIST % name),
                             root, verdicts, write)

    log(_subtitle_totals(totals))
    if not write:
        # What -w would do to THESE subtitles: syncing the ones in step and
        # throwing out the rest are each only promised where there are some.
        steps = []
        if totals["kept"]:
            steps.append("sync the subtitles in step")
        if totals["discarded"]:
            steps.append("throw out the ones out of step")
        steps.append("download what is missing")
        log("Dry run: nothing was changed. Pass -w to %s."
            % (", ".join(steps[:-1]) + " and " + steps[-1] if len(steps) > 1
               else steps[0]))
    return 0


def _subtitle_totals(totals: dict) -> str:
    """The -s run's closing count. The in-step and out-of-step figures are
    always given, a zero being the answer to the question the run was asked;
    the untested one only when some could not be tested, and a run that found
    no subtitle at all says that rather than three zeros."""
    if not any(totals.values()):
        return "No subtitle found beside any film"
    line = "%d subtitle(s) in step, %d out of step" % (totals["kept"],
                                                      totals["discarded"])
    if totals["untested"]:
        line += ", %d could not be tested" % totals["untested"]
    return line


def _chapters_only(program: str, roots: list) -> int:
    """The chapter phase on its own: every tagged film under each folder given
    that has no chapters, or numbered ones a named set can replace, looked up
    in the ChapterDB archive and written in place."""
    if tooldeps.require_tools(program, ["ffprobe", "curl", "mkvpropedit"]):
        return 1
    totals: dict = {}
    for root in roots:
        log('Phase: looking up chapters for the films in "%s"' % root)
        for verdict, films in _queued_chapters(root).items():
            totals[verdict] = totals.get(verdict, 0) + len(films)
        safety.exit_if_aborted()
    _report_chapters(totals)
    return 0


def _queued_chapters(root: str) -> dict:
    """One folder's chapters, looked up and written at once: the lookup,
    paced to the archive, is the producer of a dynamic queue, and each film
    it has found a set for is written by a worker while it goes on to the
    next - so the lookup, which is what the run waits for, never waits for a
    write."""
    outcome = chapterdb.empty_outcome()
    found: list = []

    def prepared():
        for movie, verdict, existing, chosen in chapterdb.findings(root, log):
            if verdict != "found":
                outcome[verdict].append(movie)
                continue
            found.append(movie)
            yield movie, (movie, existing, chosen)

    verdicts = _apply_as_prepared(prepared(), _write_chapter_set)
    for movie, verdict in zip(found, verdicts, strict=True):
        outcome[verdict or "failed"].append(movie)
    return outcome


def _write_chapter_set(work: tuple) -> str:
    movie, existing, chosen = work
    return chapterdb.apply_set(movie, existing, chosen, log)


def _chapter_phase(root: str, ahead: dict) -> None:
    """The full ingest's chapter lookup, which needs curl and nothing that
    would stop the rest of the run without it - asked of the pages its
    research fetched while the run did everything else."""
    if "chapters" not in ahead:
        log("Phase: looking up chapters - SKIPPED (curl is not installed)")
        return
    log("Phase: looking up chapters for films without named ones")
    ahead["chapters"].finish(log)
    outcome = chapterdb.add_chapters(root, log, ahead["chapter pages"])
    _report_chapters({verdict: len(films)
                      for verdict, films in outcome.items()})


# How each verdict of the chapter lookup is counted in its closing line.
_CHAPTER_VERDICTS = (("added", "given chapters"),
                     ("replaced", "had numbered ones replaced"),
                     ("kept", "already named"),
                     ("unmatched", "with no set that fits"),
                     ("untagged", "untagged"), ("failed", "failed"))


def _report_chapters(totals: dict) -> None:
    """The chapter lookup's closing count: only the verdicts some film came
    to, and a run that found no film at all says that rather than six
    zeros."""
    parts = [(totals[verdict], said)
             for verdict, said in _CHAPTER_VERDICTS if totals.get(verdict)]
    if not parts:
        log("Chapters: no film found")
        return
    log("Chapters: " + ", ".join(
        ("%d film(s) %s" if index == 0 else "%d %s") % part
        for index, part in enumerate(parts)))


def _write_subtitle_list(listing: str, root: str, verdicts: dict,
                         write: bool) -> None:
    """The subtitles of one folder found out of step, and the ones that could
    not be tested, one path per line under a heading each. A folder with none
    of either leaves no list - and removes the one an earlier run left, which
    would otherwise read as still standing."""
    out, untested = verdicts["discarded"], verdicts["untested"]
    if not out and not untested:
        try:
            os.remove(listing)
        except OSError:
            pass
        return
    with open(listing, "w", encoding="utf-8") as handle:
        if out:
            handle.write("# Out of step with their film - %s:\n"
                         % ("thrown out" if write else "would be thrown out"))
            for srt in out:
                handle.write(srt + "\n")
        if untested:
            handle.write("# Could not be tested - left alone:\n")
            for srt in untested:
                handle.write(srt + "\n")
    # Only the heading or headings the list actually has.
    if out and untested:
        what = "%d subtitle(s) in \"%s\" out of step and %d untested" % (
            len(out), root, len(untested))
    elif out:
        what = '%d subtitle(s) in "%s" out of step' % (len(out), root)
    else:
        what = '%d subtitle(s) in "%s" untested' % (len(untested), root)
    log('%s - listed in "%s"' % (what, listing))


def _commentary_names(roots: list, names: list, write: bool, lookup,
                      fragments_file: str, script_dir: str,
                      queued: bool = False) -> int:
    """The commentary naming phase on its own: a film whose commentary tracks
    are only numbered has them named after who is speaking in each, read from
    the disc database - and nothing else is done.

    A DRY RUN unless asked otherwise, like -t: a name put on the wrong track is
    what this phase must never do, and the dry run is where its refusals are
    read. Every folder given leaves a list of what was named and what was left
    alone, and why. ``lookup`` is the database, asked ``(title, year, kind
    of disc)`` and answering with the film's releases or None. ``queued``
    renames each film while the database is asked about the next.
    """
    for root, name in zip(roots, names, strict=True):
        log('Phase: naming numbered commentary tracks in "%s"' % root
            + ("" if write else " - DRY RUN, nothing will be renamed"))
        _commentary_names_in(root, name, write, lookup, fragments_file,
                             script_dir, queued)
        safety.exit_if_aborted()
    if not write:
        log("Dry run: nothing was renamed. Pass -w to carry these out.")
    return 0


def _commentary_names_in(root: str, name: str, write: bool, lookup,
                         fragments_file: str, script_dir: str,
                         queued: bool = False) -> None:
    """One folder's commentary tracks named, or what would be, and its list
    written.

    Queued, the naming is a dynamic queue: working out each film's names -
    the database asked at its polite pace - is the producer, and each film
    whose names are settled is renamed by a worker while the producer goes on
    to the next. The database is what the run waits for, and it is never kept
    waiting for a rename. Unqueued, each film is renamed as it is settled: the
    full ingest's way, whose research asked the database long before.
    """
    named: list = []
    refused: list = []
    planned: list = []

    def plans():
        films = list(_commentary_films(root))
        for index, (movie, base) in enumerate(films, start=1):
            log(runlog.film_header(index, len(films), root, movie))
            plan, why = _plan_commentary_names(movie, base, lookup,
                                               fragments_file)
            relative = "./" + os.path.relpath(movie, root)
            if plan is None:
                if why:
                    log("  left alone: " + why)
                    if why not in _QUIET_REFUSALS:
                        refused.append((relative, why))
                continue
            yield relative, plan

    if not write:
        named = [(relative, plan["lines"]) for relative, plan in plans()]
    elif not queued:
        for relative, plan in plans():
            failure = _carry_out_commentary_names(plan)
            if failure:
                refused.append((relative, failure))
            else:
                named.append((relative, plan["lines"]))
    else:
        def prepared():
            for relative, plan in plans():
                planned.append((relative, plan))
                yield plan["movie"], plan

        failures = _apply_as_prepared(prepared(), _carry_out_commentary_names)
        for (relative, plan), failure in zip(planned, failures, strict=True):
            if failure is None:
                refused.append((relative, "the rename was never carried out"))
            elif failure:
                refused.append((relative, failure))
            else:
                named.append((relative, plan["lines"]))
    _write_commentary_names_list(
        commands.logs_file(script_dir, COMMENTARY_NAMES_LIST % name),
        root, named, refused, write)


def _commentary_films(root: str):
    """Every film under ``root`` whose commentaries could be named, as
    ``(movie, the folder's name without its id)``: the one film of each folder
    that is not bonus material."""
    for directory in [root] + rules._folders_below(root):
        if rules.is_bonus_folder(directory):
            continue
        base, _tag = plexnames.untagged_base(os.path.basename(
            directory.rstrip("/")))
        entries = [entry for entry in rules._names_in(directory)
                   if os.path.isfile(os.path.join(directory, entry))]
        for film in plexnames.one_film_in(base, entries):
            yield os.path.join(directory, film), base


# The reasons a film is left alone that are not worth a line in the list: it
# has nothing to name, or somebody already named it. They are still said under
# the film's header as the walk passes it.
_QUIET_REFUSALS = ("no commentary track", "a commentary track already has a name")


def _commentary_film(movie: str, base: str) -> tuple:
    """What naming a film's commentaries starts from, the database not yet
    asked: ((tracks, its commentaries, those as the naming reads them, title,
    year, kind of disc), "") - or (None, why not)."""
    tracks = rules._identify(movie)
    commentaries = [track for track in tracks
                    if track.is_audio and track.is_commentary]
    file_tracks = [commentarynames.FileCommentary(track.id, track.name)
                   for track in commentaries]
    ordered, why = commentarynames.order_file_commentaries(file_tracks)
    if not ordered:
        return None, why

    year = plexnames.year_of(base)
    title = plexnames.untitled_base(base, year).strip()
    video = next((track for track in tracks if track.is_video), None)
    width, _x, height = (video.dimensions if video else "").partition("x")
    kind = commentarynames.disc_format(width, height)
    return (tracks, commentaries, file_tracks, title, year, kind), ""


def _commentary_questions(root: str, lookup) -> None:
    """Every film under ``root`` with commentaries to name asked about, and
    nothing else done: the full ingest's research, on a thread of its own."""
    for movie, base in _commentary_films(root):
        if safety.abort_requested():
            return
        film, _why = _commentary_film(movie, base)
        if film is not None and film[5]:
            lookup(*film[3:])


def _plan_commentary_names(movie: str, base: str, lookup,
                           fragments_file: str) -> tuple:
    """How one film's commentary tracks are to be named, said and not yet
    done: (the plan, "") - the renames as lines, and what carries them out -
    or (None, why not)."""
    film, why = _commentary_film(movie, base)
    if film is None:
        return None, why
    tracks, commentaries, file_tracks, title, year, kind = film
    stem = os.path.splitext(movie)[0]
    decision = commentarynames.decide(
        file_tracks, lookup(title, year, kind) if kind else None, kind,
        _commentary_transcripts(stem, file_tracks), title)
    if not decision.names:
        return None, decision.reason

    new = dict(decision.names)
    old = {track.id: track.name for track in commentaries}
    subtitles, why = commentarynames.subtitle_renames(
        tracks, [(old[track_id], name) for track_id, name in decision.names])
    if why:
        return None, why

    def clean(name: str) -> str:
        return rules.rename(name.replace("/", "").replace("&", "and"),
                            fragments_file)

    directory = os.path.dirname(movie)
    sidecars = commentarynames.sidecar_renames(
        rules._names_in(directory), os.path.basename(stem),
        [(track_id, old[track_id], name) for track_id, name in decision.names],
        clean)

    lines = ['track %s "%s" -> "%s"' % (track_id, old[track_id], name)
             for track_id, name in decision.names]
    subtitle_names = dict(subtitles)
    lines += ['subtitle track %s "%s" -> "%s"'
              % (track.id, track.name, subtitle_names[track.id])
              for track in tracks if track.id in subtitle_names]
    lines += ['"%s" -> "%s"' % pair for pair in sidecars]
    lines.append("(listed by: %s)" % decision.source)
    for line in lines:
        log("  " + line)

    arguments = []
    for position, track in enumerate(tracks, start=1):
        if track.id in new:
            arguments += ["--edit", "track:%d" % position,
                          "--set", "name=" + new[track.id],
                          "--set", "flag-commentary=1"]
        elif track.id in subtitle_names:
            arguments += ["--edit", "track:%d" % position,
                          "--set", "name=" + subtitle_names[track.id]]
    return {"movie": movie, "lines": lines, "arguments": arguments,
            "sidecars": sidecars}, ""


def _carry_out_commentary_names(plan: dict) -> str:
    """The renames a plan says, made: "" when they were, and why not when
    mkvpropedit could not make them."""
    movie = plan["movie"]
    try:
        original = os.stat(movie).st_mtime
    except OSError:
        original = None
    failure = rules._run_capture(["mkvpropedit", movie] + plan["arguments"])
    if failure:
        return "mkvpropedit could not rename the tracks: " + \
            failure.replace("\n", " | ")
    if original is not None:
        try:
            os.utime(movie, (original, original))
        except OSError:
            pass
    directory = os.path.dirname(movie)
    skips = safety.RunSkipLog()
    for old_name, new_name in plan["sidecars"]:
        if not safety.safe_rename(os.path.join(directory, old_name),
                                  os.path.join(directory, new_name), skips):
            log('  WARNING: sidecar not renamed, "%s" is in the way: %s'
                % (new_name, old_name))
    return ""


# The workers carrying out what a paced lookup has prepared. One is plenty: a
# rename or a chapter write takes a moment, the lookup seconds a film. The
# buffer is kept generous so a run of films the lookup settles from its kept
# pages never has it wait on the worker.
APPLY_JOBS = 1
APPLY_BUFFER_FACTOR = 8


def _apply_as_prepared(prepared, target) -> list:
    """Every ``(label, work)`` the ``prepared`` generator yields handed to
    ``target`` in a worker, while the generator goes on preparing the next -
    the dynamic queue, with the preparation as its producer. ``target``'s
    answers come back in the order prepared, None for one whose worker never
    answered: it was stopped, or died."""
    results = tempfile.mkdtemp(prefix="ingest-movies-results-")
    count = [0]

    def items():
        for label, work in prepared:
            count[0] += 1
            yield (label, count[0] - 1, work, results), 1

    try:
        dynamicqueue.run(items(), APPLY_JOBS, _in_apply_worker,
                         lambda item: (target, item),
                         buffer_factor=APPLY_BUFFER_FACTOR, log=log)
        answers: list = []
        for index in range(count[0]):
            try:
                with open(os.path.join(results, "%d.json" % index),
                          encoding="utf-8") as handle:
                    answers.append(json.load(handle))
            except (OSError, ValueError):
                answers.append(None)
        return answers
    finally:
        shutil.rmtree(results, ignore_errors=True)


def _in_apply_worker(target, item: tuple) -> None:
    """One prepared item carried out, in a worker PROCESS, its answer left
    where :func:`_apply_as_prepared` reads it back."""
    safety.trap_worker_abort()
    _label, index, work, results = item
    answer = target(work)
    path = os.path.join(results, "%d.json" % index)
    with open(path + ".part", "w", encoding="utf-8") as handle:
        json.dump(answer, handle)
    os.replace(path + ".part", path)


def _commentary_transcripts(stem: str, tracks: list) -> dict:
    """Each commentary track's transcript beside the film, as text: every
    ``.srt`` the transcription wrote for that track, in every language, run
    together. A track with none is left out."""
    directory, _name = os.path.split(stem)
    entries = sorted(rules._names_in(directory or "."))
    found = {}
    for track in tracks:
        prefix = os.path.basename(
            commentarytranscription.commentary_prefix(stem, track.id))
        texts = []
        for entry in entries:
            if not (entry.startswith(prefix) and entry.endswith(".srt")):
                continue
            try:
                with open(os.path.join(directory or ".", entry),
                          encoding="utf-8", errors="replace") as handle:
                    texts.append(handle.read())
            except OSError:
                continue
        if texts:
            found[track.id] = "\n\n".join(texts)
    return found


def _write_commentary_names_list(listing: str, root: str, named: list,
                                 refused: list, write: bool) -> None:
    """What one folder's commentary naming did, or would do, and every film it
    left alone with the reason. A folder with nothing to say of either leaves
    no list - and removes one an earlier run left."""
    if not named and not refused:
        try:
            os.remove(listing)
        except OSError:
            pass
        return
    with open(listing, "w", encoding="utf-8") as handle:
        if named:
            handle.write("# %s:\n" % ("Named" if write else "Would be named"))
            for movie, lines in named:
                handle.write(movie + "\n")
                for line in lines:
                    handle.write("    " + line + "\n")
        if refused:
            handle.write("# Left alone:\n")
            for movie, why in refused:
                handle.write("%s\n    %s\n" % (movie, why))
    log('%d film(s) in "%s" %s and %d left alone - listed in "%s"'
        % (len(named), root, "named" if write else "would be named",
           len(refused), listing))


def _sidecar_too_small(prefix: str, movie: str, durations: dict) -> bool:
    """Whether the commentary sidecar already beside the film is too small to be
    its real transcript.

    The size is judged against the film's length: a real transcript runs to the
    order of a kilobyte per minute, so a sidecar under half a kilobyte per
    minute is the one an older run wrote forcing a non-English commentary
    through the English model. The .srt files are the only thing this phase
    writes, and their TOTAL is what is measured - a healthy supported-language
    commentary is two of them, and their sum clears the bar either way. A film
    whose length cannot be read is left alone: an existing transcript is not
    thrown away over a figure that is not there.
    """
    seconds = durations.get(movie)
    if seconds is None:
        seconds = rules._duration_of(movie)
        durations[movie] = seconds
    minutes = seconds / 60.0
    if minutes <= 0:
        return False
    directory, start = os.path.split(prefix)
    directory = directory or "."
    try:
        names = os.listdir(directory)
    except OSError:
        return False
    total = 0
    found = False
    for name in names:
        if name.startswith(start) and name.endswith(".srt"):
            found = True
            try:
                total += os.path.getsize(os.path.join(directory, name))
            except OSError:
                pass
    if not found:
        return False
    return (total / 1024.0) < MIN_COMMENTARY_KB_PER_MINUTE * minutes


def _write_commentary_orphans(script_dir: str, orphans: list) -> None:
    """The transcripts a -a run found that name a track their film no longer
    numbers a commentary for, one absolute path per line. An empty run writes
    nothing, so a run with nothing to report leaves no file behind."""
    if not orphans:
        return
    path = commands.logs_file(script_dir, COMMENTARY_ORPHANS_LIST)
    with open(path, "w", encoding="utf-8") as handle:
        for orphan in orphans:
            handle.write(orphan + "\n")
    log("Commentary transcript(s) with no matching track in the film, "
        "written to %s" % path)


def _report_unfixed_movies(movies: list) -> None:
    """The movies a transcription was skipped for because their name carried no
    dot before its extension, named on stderr at the end of a full ingest. The
    full run has no logs/ folder of its own to leave them in - the screen is
    where the rest of its end-of-run report goes - and an empty list prints
    nothing, so a run with nothing to report is silent."""
    if not movies:
        return
    sys.stderr.write(
        "\nLeft untranscribed: no dot before the extension in the name, so the\n"
        "film's stem could not be read. Conform the name to the folder's or\n"
        "fix it by hand, then run the ingest again.\n")
    for movie in movies:
        sys.stderr.write("  " + movie + "\n")


def _write_unfixed_movies(script_dir: str, movies: list) -> None:
    """The movies a -a run left untranscribed because their name carried no dot
    before its extension, one absolute path per line. An empty run writes
    nothing, so a run with nothing to report leaves no file behind."""
    if not movies:
        return
    path = commands.logs_file(script_dir, UNFIXED_MOVIES_LIST)
    with open(path, "w", encoding="utf-8") as handle:
        for movie in movies:
            handle.write(movie + "\n")
    log("Movie(s) left untranscribed - no dot before the extension in the "
        "name, written to %s" % path)


def _commentary_only(program: str, script_dir: str, roots: list,
                     fragments_file: str) -> int:
    """The commentary transcription phase on its own.

    What runs is exactly what the full run runs for this phase - the walk of
    every folder given, the track test, the check for a transcript already
    beside the film and the queue drained over the whisper workers - and what
    does not is everything the full run runs around it: no subtitle
    downloading, no renaming of titles, no opus, no improved copies and no
    tagging. The one exception is conforming a mangled movie's name to its
    folder's spelling, which IS done: a movie whose name never got the dot
    before its mkv has no stem a transcript can be named from, and it must be
    put right or reported before anything is transcribed. A
    transcript that is written is written next to the film, and a second run
    over the same library finds every commentary already has its sidecar and
    does nothing, the way the phase does inside a full ingest.

    A transcript already beside a film is skipped, except one too small to be
    the film's real transcript: a real one runs to the order of a kilobyte per
    minute of film, and a sidecar under half of that is the one an older run
    wrote forcing a non-English commentary through the English model. That one
    is discarded and the commentary is transcribed for real.

    A transcript an older run numbered for a track that no longer stands where
    it numbered it is renumbered to the track's own number rather than
    transcribed a second time, when the name still says which commentary it is
    of - and once renumbered it is judged by the size rule like any other
    sidecar.

    A transcript that names a track its film no longer numbers a commentary for
    is one the film has outlived; the walk reports every one it finds, and the
    run leaves them in a file rather than losing them or acting on them.

    A movie whose name carries no dot before its extension has no stem a
    transcript can be named from: built from the empty stem it would be written
    in the library's root rather than the film's folder. The ones whose name
    can be restored to the folder's spelling are conformed before the walk, and
    the ones that cannot are left untranscribed and left in a file at the end,
    the same way the orphaned transcripts are.

    The transcription is the whole of the run, so its tools are the gate on it:
    without ffsubsync and pipx there is no subtitle worth writing, and there is
    nothing else to fall back to, so the warning they leave is the run.
    """
    # Which ffmpeg of the ones installed, before the preflight asks whether PATH
    # can reach one.
    ffmpegselect.select_ffmpeg()
    ffmpegselect.report_ffmpeg_selection()

    # The phase reads the tracks with mkvmerge, extracts with ffmpeg and probes
    # with ffprobe; mkvpropedit and mediainfo belong to the phases this run does
    # not do, and asking for them would refuse a host that is perfectly set up
    # for what was asked.
    if tooldeps.require_tools(program, ["ffmpeg", "ffprobe", "mkvmerge"]):
        return 1

    if not _settle_subtitle_work(commentary_only=True):
        # This run has no other phase to go on to, so say that it is over.
        log("Commentary-only has no other phase to run - nothing was done.")
        return 0
    # A transcript is judged by its own tight offset and never by the
    # confidence, so what a confidence-blind ffsubsync costs the downloads is
    # nothing this run would feel.
    ffsubsync_quality = _settle_ffsubsync_quality(judged="")

    ramscratch.init_ram_base()
    ram_root, status = ramscratch.ram_scratch_dir("ingestMovies")
    if status != 0:
        return 1
    ramscratch.add_exit_cleanup([ram_root])

    safety.init_abort_flag(os.path.join(ram_root, "abortRequested"))
    safety.trap_run_abort()

    # Done once, before anything is queued, so every worker inherits the
    # answer instead of probing the GPU again.
    from medialib.lib import whisper as whisper_lib
    whisper = whisper_lib.init_whisper_model(str(runlog.cpu_count()),
                                             ram_root, log)

    state = Run(script_dir=script_dir, ram_root=ram_root,
                skips=safety.RunSkipLog(), fragments_file=fragments_file,
                whisper=whisper, whisper_said=[],
                ffsubsync_quality=ffsubsync_quality,
                long_names=tmdblookup.LongNames())
    jobs = whisper["jobs"]

    # The transcripts the walk finds that name a track their film no longer
    # numbers a commentary for, gathered across every folder and left in a
    # file at the end: the -a run is the one that reports them, and it is the
    # whole of this run.
    orphans: list[str] = []
    # And the movies the walk could not transcribe because their name carried
    # no dot before its extension, gathered the same way: conformed before the
    # walk where the folder's spelling gave the answer, reported where it did
    # not.
    unfixed: list[str] = []
    try:
        for root in roots:
            if len(roots) > 1:
                log('Transcribing commentary in "%s"' % root)
            # A mangled name put right before the walk reads the tracks, so the
            # film is transcribed under the name its folder spells it rather
            # than left with no stem to read.
            rules.conform_movie_names(root, state.skips)
            # Fresh per root: the movie paths handed down are relative to this
            # root, so a cache keyed on them is only valid for it.
            durations: dict[str, int] = {}
            root_orphans: list[str] = []
            root_unfixed: list[str] = []
            commentarytranscription.export_commentary(
                root, rules.read_track_info, rules.is_bonus_folder,
                lambda name: rules.rename(name, state.fragments_file),
                rules.audio_stream_index, state.ram_root, state.whisper,
                log,
                lambda producer: _drain_commentary(state, producer, jobs),
                rules.MAX_WHISPER_SYNC_OFFSET, state.ffsubsync_quality,
                lambda prefix, movie, _durations=durations: _sidecar_too_small(
                    prefix, movie, _durations),
                same_commentary_name=rules._same_commentary,
                orphans=root_orphans, unfixed=root_unfixed)
            for relative in root_orphans:
                orphans.append(os.path.normpath(
                    os.path.join(root, relative)))
            for relative in root_unfixed:
                unfixed.append(os.path.normpath(
                    os.path.join(root, relative)))
            safety.exit_if_aborted()
    finally:
        ramscratch.run_exit_cleanup()
    _write_commentary_orphans(script_dir, orphans)
    _write_unfixed_movies(script_dir, unfixed)
    return workerpool.exit_status(0)


def _capitalise(text: str) -> str:
    """The first character upper-cased, the rest untouched."""
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


def _settle_subtitle_work(commentary_only: bool = False) -> bool:
    """The subtitle work is all-or-nothing, and that is not the same as optional.

    Neither source of subtitles is worth muxing in unaligned: a downloaded one is
    usually cut for a different release, and a whisper transcript is only as
    trustworthy as the alignment that proves it matches the audio. ffsubsync is
    what decides that and pipx is what runs the two producers, so missing either,
    the choice is between muxing subtitles nobody checked and not producing them
    at all - and both phases are skipped together, said once here rather than as
    a surprise per movie.

    ``commentary_only`` is the -a run, which has the transcription and nothing
    else: the warning names only what that run loses, and does not promise the
    rest of a full ingest it is not going to do.
    """
    if os.environ.get("SKIP_TOOL_PREFLIGHT"):
        return True
    missing = [name for name in ("ffsubsync", "pipx") if not _has_tool(name)]
    if not missing:
        return True
    if commentary_only:
        log("WARNING: %s not installed - skipping commentary transcription."
            % " ".join(missing))
        log("         Only ffsubsync can prove a transcript is in step with "
            "the audio, and an unverified subtitle")
        log("         track is worse than none.")
    else:
        log("WARNING: %s not installed - skipping subtitle downloading AND "
            "commentary transcription for this run." % " ".join(missing))
        log("         Both produce a subtitle that only ffsubsync can prove is "
            "in step with the audio, and an unverified")
        log("         subtitle track is worse than none. Everything else "
            "(naming, tags, opus, Dolby Vision, remuxing) runs.")
    # Only the step that is missing: a host with pipx already on it has no use
    # for the apt line, and one with ffsubsync already installed for the other.
    if missing == ["pipx"]:
        steps = "apt install pipx"
    elif missing == ["ffsubsync"]:
        steps = "pipx install ffsubsync"
    else:
        steps = "apt install pipx, then pipx install ffsubsync"
    log("         To enable %s: %s." % ("it" if commentary_only else "them",
                                        steps))
    return False


def _warn_without_subcleaner() -> None:
    """Said once at the start: without subcleaner a subtitle keeps the adverts
    it came with, and the run goes on."""
    if os.environ.get("SKIP_TOOL_PREFLIGHT") or subtitleads.available():
        return
    log("WARNING: subcleaner not installed - subtitles keep the adverts they "
        "came with.")
    log("         To enable it: %s."
        % tooldeps.tool_note(subtitleads.TOOL).split("|", 1)[1])


def _tool_help(name: str) -> str:
    """A tool's help page, or "" if it will not print one."""
    try:
        done = subprocess.run([name, "--help"], stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, text=True)
    except OSError:
        return ""
    return done.stdout or ""


def _settle_ffsubsync_quality(judged: str = "a downloaded subtitle",
                              say_unchecked: bool = True) -> str:
    """Which check this ffsubsync can make of an alignment: "confidence" when
    the confidence helper can watch it (and it knows
    ``--skip-sync-on-low-quality``), "yes" when it knows only the flag, "no"
    when it knows neither - see :func:`subtitlefiles.sync_subtitle`.

    Probed once here rather than per subtitle: an older ffsubsync handed an
    option it does not know fails argparse and exits non-zero, which BOTH sync
    call sites would read as a failed sync and answer by discarding a perfectly
    good subtitle. Captured whole rather than piped into a matcher, because a
    matcher that stops at its first hit can SIGPIPE the tool and turn a yes into
    a no.

    ``judged`` is what the run puts to the confidence test, for the warning a
    confidence-blind ffsubsync gets - and "" for a run that puts nothing to
    it, which is told nothing. ``say_unchecked`` False is for the caller that
    refuses the run over a "no" in words of its own.
    """
    if not _has_tool("ffsubsync"):
        return "no"
    if "--skip-sync-on-low-quality" not in (_tool_help("ffsubsync") or ""):
        if say_unchecked:
            log("WARNING: this ffsubsync has no --skip-sync-on-low-quality, "
                "so a bad alignment gets applied instead of rejected - "
                "upgrade ffsubsync to enable the check")
        return "no"
    if subtitlefiles.can_measure_confidence():
        return "confidence"
    if judged:
        log("WARNING: this ffsubsync cannot be asked how sure an alignment "
            "is, so %s is believed only within %ss of where it started - "
            "which throws out right subtitles found further off, and keeps "
            "wrong ones that land close"
            % (judged, rules.MAX_SYNC_QUALITY_OFFSET))
    return "yes"


def _ingest(state, root: str, subtitle_work: bool, name: str) -> None:
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
    fixedpoint.until_stable(lambda: rules.rename_folders(
        root, state.fragments_file, state.skips), MAX_RENAME_PASSES)
    fixedpoint.until_stable(lambda: rules.rename_movies(
        root, state.fragments_file, state.skips), MAX_RENAME_PASSES)

    # Once the names have settled, a movie whose name never got the dot before
    # its mkv can be conformed to the spelling its folder spells it - where the
    # folder already carries the id the file is matched against. Folders that
    # gain their id from the tagging below are conformed again after it.
    log("Phase: conforming mangled movie names to their folders'")
    rules.conform_movie_names(root, state.skips)

    log("Phase: moving and renaming pre-existing subtitles")
    subtitlefiles.move_subs(root)
    subtitlefiles.rename_subs(root, state.skips)

    log("Phase: refreshing mkv tags and track flags")
    rules.update_tags(root)
    rules.cleanup(root)

    # The names have settled and every film is a Matroska file, which is all
    # the chapter archive and the disc database are asked by: they are asked
    # now, at their polite pace, on threads of their own, and the answers wait
    # for the phases at the end that use them.
    ahead = _start_research(state, root)

    log("Phase: transcoding lossless audio to opus")
    _transcode_opus(state, root)

    if subtitle_work:
        log("Phase: extracting and transcribing commentary tracks")
        jobs = state.whisper["jobs"]
        commentarytranscription.export_commentary(
            root, rules.read_track_info, rules.is_bonus_folder,
            lambda name: rules.rename(name, state.fragments_file),
            rules.audio_stream_index, state.ram_root, state.whisper,
            log,
            lambda producer: _drain_commentary(state, producer, jobs),
            rules.MAX_WHISPER_SYNC_OFFSET, state.ffsubsync_quality,
            same_commentary_name=rules._same_commentary,
            unfixed=state.unfixed_movies)
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

    # The tagging gave every folder its id, and a mangled name that the first
    # pass could not anchor on a tag can now be conformed. The transcription is
    # already done - what a name still mangled then was left untranscribed and
    # is reported as such - but the name is right for the phases that follow and
    # for the next run.
    log("Phase: conforming mangled movie names to their folders'")
    rules.conform_movie_names(root, state.skips)

    # Asked again, now that the tagging has given the rest of the folders the
    # title they are looked up by: what was asked before is remembered, so this
    # is only the films the tagging named.
    _research_again(ahead, root)

    # After the tagging, so a film is searched for by the id its name now
    # carries rather than by a title a remake or a namesake also answers to.
    if subtitle_work:
        log("Phase: downloading missing subtitles")
        subtitlefiles.download_subs(
            root, subtitlefiles.providers_to_ask(),
            rules.MAX_SYNC_OFFSET, rules.MAX_SYNC_QUALITY_OFFSET,
            state.ffsubsync_quality, log)
    else:
        log("Phase: downloading missing subtitles - SKIPPED (no "
            "ffsubsync/pipx, see the warning at startup)")

    # Last of the work on the films, so the research has had the whole run to
    # be asked: after the tagging, so a film is named and looked up by the
    # title TMDb gave its folder; after the transcription, whose transcripts
    # tell several commentaries apart; and after the remux, whose copy the
    # names and the chapters are written into in place rather than carried
    # through another one.
    _commentary_name_phase(state, root, name, ahead)
    _chapter_phase(root, ahead)

    rules.cleanup(root)
    log("Phase: checking for folders without a movie")
    check_folders(root)

    safety.print_run_footer()
    log("Ingest complete: " + root)


def _start_research(state, root: str) -> dict:
    """The chapter archive and the disc database asked about every film under
    ``root`` that will want them, each on a thread of its own - or neither,
    without curl to ask them with.

    The chapters are remembered page by page rather than film by film: the
    remux and the opus transcode can move a film's length by a few
    milliseconds, and the lookup, asked again at the end, has to land on the
    same pages whatever length it is handed.
    """
    if not _has_tool("curl"):
        return {}
    chapter_pages = research.Research(
        "chapter", chapterdb.ask_archive, keep=lambda body: body is not None)

    def fetch(path: str, params=()):
        return chapter_pages(path, tuple(params))

    said: list = []
    site = dvdcompare.Site(dvdcompare.directory(state.script_dir),
                           said.append)
    ahead = {"chapters": chapter_pages, "chapter pages": fetch,
             "names": research.Research("commentary name", site.releases),
             "said": said}
    _research_again(ahead, root)
    return ahead


def _research_again(ahead: dict, root: str) -> None:
    """Every film under ``root`` handed to the research, again: a film already
    asked about is answered from memory, so this only reaches the ones whose
    question changed."""
    if not ahead:
        return
    ahead["chapters"].ahead(lambda: _chapter_questions(root,
                                                       ahead["chapter pages"]))
    ahead["names"].ahead(lambda: _commentary_questions(root, ahead["names"]))


def _chapter_questions(root: str, fetch) -> None:
    """Every film under ``root`` without named chapters looked up, and
    nothing written: the full ingest's research. Stops the first time the
    archive does not answer, the way the lookup itself does."""
    for movie in subtitlefiles.subtitle_movies(root):
        if safety.abort_requested():
            return
        verdict, title, existing = chapterdb.examine(movie)
        if verdict:
            continue
        _chosen, answered = chapterdb.find_set(title, existing, fetch)
        if not answered:
            return


def _commentary_name_phase(state, root: str, name: str, ahead: dict) -> None:
    """The full ingest's commentary naming: -n's, carried out rather than a
    dry run, asked of what the research learnt while the run did everything
    else."""
    if not ahead:
        log("Phase: naming numbered commentary tracks - SKIPPED (curl is not "
            "installed)")
        return
    log("Phase: naming numbered commentary tracks")
    ahead["names"].finish(log)
    _say_what_was_said(ahead)
    _commentary_names_in(root, name, True, ahead["names"],
                         state.fragments_file, state.script_dir)
    _say_what_was_said(ahead)


def _say_what_was_said(ahead: dict) -> None:
    """What the disc database had to say while it was asked, held back so it
    is printed in the phase it belongs to rather than in whichever one the
    thread happened to be asked in."""
    while ahead["said"]:
        log(ahead["said"].pop(0))


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


def _drain_commentary(state, producer, jobs: int) -> None:
    """The queue exportCommentary prepared, at ``jobs`` workers - a width that
    is whisper's and not the core count, one run already spanning the GPU or
    every CPU thread.

    ``producer`` yields ``(record, size)`` as it prepares the commentary, so
    the preparation runs beside the transcription rather than in front of it.
    At width one there is nothing to interleave with, so the records are
    prepared and run in this process one at a time; wider than that the dynamic
    queue runs them, keeping an adequate buffer of prepared items and giving a
    freed slot the next longest one.
    """
    producer = _announcing_whisper(state, producer)
    if jobs <= 1:
        for record, _size in producer:
            if safety.abort_requested():
                return
            _transcribe_one(state, record)
        return

    dynamicqueue.run(producer, jobs, _in_transcribe_worker,
                     lambda record: (state, record), log=log)


def _announcing_whisper(state, producer):
    """The records of ``producer``, with what settling whisper said printed in
    front of the first of them - the first moment it is about work this run
    will do."""
    for item in producer:
        while state.whisper_said:
            log(state.whisper_said.pop(0))
        yield item


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
