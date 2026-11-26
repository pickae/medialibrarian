"""ingest-series's run: the phase sequence, carried out.

The decisions - what a folder is, where an episode goes, what it is renamed
to - are `medialib/cli/ingest_series.py`. Everything that works on one video
file is the movie ingest's run (`medialib/cli/ingest_movies_run.py`), handed
this command's walker (the episodes) and its language rule (the show's).
"""

from __future__ import annotations

import dataclasses
import os
import shutil
import sys
import tempfile

from medialib import commands
from medialib.cli import convert_video_chapters as chapterwork
from medialib.cli import ingest_movies as movierules
from medialib.cli import ingest_movies_carry as carryphase
from medialib.cli import ingest_movies_convert as convertphase
from medialib.cli import ingest_movies_lanes as lanes
from medialib.cli import ingest_movies_plex as plexphase
from medialib.cli import ingest_movies_run as movierun
from medialib.cli import ingest_series as rules
from medialib.lib import (
    aicommentarynames,
    aisubtitleads,
    chapterdb,
    clioptions,
    commentarytranscription,
    console,
    diskturn,
    donelog,
    durationcheck,
    episodes,
    ffmpegselect,
    gputurn,
    keepparts,
    languages,
    mediaserver,
    plexcompare,
    plexnames,
    ramscratch,
    runcontext,
    runlog,
    safety,
    seriesfolders,
    seriesidentify,
    seriesnames,
    seriessplit,
    settings,
    statusline,
    subtitlefiles,
    subtitleocr,
    titlematch,
    tmdbclient,
    tooldeps,
    treewalk,
    tvdbclient,
    workerpool,
)
from medialib.lib.filmmatch import Film
from medialib.lib.runlog import log

# Every file this command leaves behind about a run, under logs/ in the
# medialib data directory, as ingest-movies' are.
LOGS = "ingest-series"
UNMATCHED_LIST = os.path.join(LOGS, "ingest-series-unmatched-%s.tsv")
AMBIGUOUS_LIST = os.path.join(LOGS, "ingest-series-ambiguous-%s.txt")
RENAMES_LIST = os.path.join(LOGS, "ingest-series-renames-%s.txt")
NEAR_MISS_LIST = os.path.join(LOGS, "ingest-series-nearmisses-%s.txt")
CONFLICTS_LIST = os.path.join(LOGS, "ingest-series-conflicts-%s.txt")
DUPLICATES_LIST = os.path.join(LOGS, "ingest-series-duplicates.txt")
SUBTITLES_LIST = os.path.join(LOGS, "ingest-series-subtitles-%s.txt")
SUBTITLES_DONE = os.path.join(LOGS, "ingest-series-subtitles-done.txt")
BACKUPS_LIST = os.path.join(LOGS, "ingest-series-backups-%s.txt")
SPLIT_LIST = os.path.join(LOGS, "ingest-series-unsplit-%s.txt")
FILMS_LIST = os.path.join(LOGS, "ingest-series-films-%s.txt")
COMMENTARY_NAMES_LIST = os.path.join(LOGS,
                                     "ingest-series-commentarynames-%s.txt")


# --- the shows, as the run knows them -----------------------------------------

class Shows:
    """Which show each show folder is, asked once and kept: what the remux
    reads an episode's original language off, before the tagging names the
    folder. Keyed by the folder's path as the run found it."""

    def __init__(self) -> None:
        self._shows: dict = {}

    def remember(self, folder: str, show) -> None:
        self._shows[_key(folder)] = show

    def of(self, folder: str):
        key = _key(folder)
        if key not in self._shows:
            self._shows[key] = _show_for(folder)
        return self._shows[key]

    def languages_of(self, episode: str):
        """What the show an episode is of is spoken in."""
        try:
            show = self.of(seriesnames.series_folder_of(episode))
        except Exception as error:  # noqa: BLE001 - the first track's stands
            log("  WARNING: could not ask the catalogue for the show's "
                "languages (%s); keeping the first audio track's: %s"
                % (error, episode))
            show = None
        return seriesidentify.languages_of(show)


def _key(folder: str) -> str:
    return os.path.normcase(os.path.abspath(folder))


def _show_for(folder: str, ids: dict | None = None, notes: list | None = None):
    """The show a folder is: an id the list names, the tag it carries, or
    the ladder - None where none settles it."""
    name = os.path.basename(folder.rstrip("/\\"))
    on_disk = rules.episodes_on_disk(folder)
    if ids and name in ids:
        provider, number = ids[name]
        return seriesidentify.by_tag(provider, number, on_disk)
    read = seriesfolders.read_series_folder(name)
    if read.tag:
        provider, number = plexnames.id_of(read.tag)
        return seriesidentify.by_tag(provider, number, on_disk)
    if not os.environ.get("tmdbApiKey") and not tvdbclient.available():
        return None
    result = seriesidentify.identify(read.title, read.year, on_disk)
    if result.show is None and result.ambiguous:
        # Told apart by the episodes' lengths, measured only now that two
        # shows fit.
        measured = rules.episodes_on_disk(folder, movierules._duration_of)
        result = seriesidentify.identify(read.title, read.year, measured)
    if notes is not None:
        notes.extend(result.notes)
        if result.ambiguous:
            notes.append("AMBIGUOUS")
    return result.show


# --- the command ----------------------------------------------------------------

def main(argv: list, program: str = "ingest-series",
         script_dir: str = "") -> int:
    declaration = rules.spec(program)
    try:
        result = clioptions.parse(declaration, argv)
        clioptions.settle_verbosity(result)
    except clioptions.HelpRequested:
        sys.stdout.write(clioptions.help_text(declaration))
        return 0
    except clioptions.UsageError as error:
        clioptions.refuse_usage(declaration, error.message)
        return 1
    if clioptions.args_out_of_range(len(result.positionals), 1, None):
        sys.stderr.write(clioptions.no_args_text(declaration))
        return 1
    if "O" in result.given:
        return _only_phases(declaration, result, program, script_dir)
    result.values["extrasChapters"] = "1" if "A" in result.given else ""
    return _main_one(declaration, result, program, script_dir)


def _only_phases(declaration, result, program: str, script_dir: str) -> int:
    """-O: the single-phase sweeps it names, one after the other, stopping
    at the first that fails."""
    try:
        keepparts.parse(result.values["onlyPhases"], rules.ONLY_PHASES,
                        flag="-O", word="phase")
    except clioptions.UsageError as error:
        clioptions.refuse_usage(declaration, error.message)
        return 1
    phases = result.values["onlyPhases"].split(",")
    for letter, key in (("r", "convertRemuxes"), ("k", None), ("o", None)):
        if (letter in result.given if key is None
                else result.values.get(key)):
            clioptions.refuse_usage(
                declaration,
                "-O names the phases to run on their own, and -%s belongs to "
                "a full ingest.\nDrop one." % letter)
            return 1
    for phase in phases:
        if phase in rules.ONLY_NEEDS_AI and "A" not in result.given:
            clioptions.refuse_usage(
                declaration, "-O %s asks a language model, and one is asked "
                "only under -A." % phase)
            return 1
    if "P" in result.given and "extras-chapters" not in phases:
        clioptions.refuse_usage(
            declaration,
            "-P is for the chapter model of the extras-chapters phase, and "
            "extras-chapters is\nnot among the phases named in -O.")
        return 1
    if result.values.get("idList") and "tagging" not in phases:
        clioptions.refuse_usage(
            declaration,
            "-i is the id list the tagging phase reads back, and tagging is "
            "not among the\nphases named in -O.")
        return 1
    if result.values.get("writeTags") and not any(
            phase in rules.ONLY_WRITES for phase in phases):
        clioptions.refuse_usage(
            declaration,
            "-w carries out the dry run of tagging, subtitles or backups, "
            "and none of the\nphases named in -O is one. Drop the -w.")
        return 1
    for number, phase in enumerate(phases, 1):
        if len(phases) > 1:
            log("-O phase %d/%d: %s" % (number, len(phases), phase))
        values = dict(result.values)
        if phase not in rules.ONLY_WRITES:
            values["writeTags"] = False
        status = _main_one(declaration,
                           dataclasses.replace(result, values=values),
                           program, script_dir, phase)
        if status:
            return status
    return 0


def _main_one(declaration, result, program: str, script_dir: str,
              phase: str = "") -> int:
    try:
        kept = movierules.kept_languages()
        wanted = languages.spoken_languages()
        keep_parts = (keepparts.parse(result.values["keepParts"],
                                      ("audio", "tracks", "dv"))
                      if "k" in result.given else frozenset())
        omit_phases = (keepparts.parse(result.values["omitPhases"],
                                       rules.OMIT_PHASES, flag="-o",
                                       word="phase")
                       if "o" in result.given else frozenset())
        plexnames.naming_style()
        movierules.lossless_audio_target()
    except clioptions.UsageError as error:
        clioptions.refuse_usage(declaration, error.message)
        return 1

    if result.values.get("writeTags") and not phase:
        clioptions.refuse_usage(
            declaration,
            "-w on its own does nothing to carry out: it is what turns the dry "
            "run of the\ntagging, subtitles and backups phases of -O into the "
            "real thing, and none was\ngiven. Without one this is a FULL "
            "INGEST.\n\nDid you mean -wO tagging?")
        return 1
    if result.values.get("idList") and not phase:
        clioptions.refuse_usage(
            declaration,
            "-i is the id list the tagging phase reads back, and no phase was "
            "named.\nDid you mean -O tagging -i <file>?")
        return 1
    if "k" in result.given and result.values.get("convertRemuxes"):
        clioptions.refuse_usage(
            declaration,
            "-k says take nothing away, and -r converts the remuxes by "
            "encoding\nthem: they contradict each other. Drop one.")
        return 1
    if ("P" in result.given and not result.values.get("extrasChapters")
            and phase != "extras-chapters"):
        clioptions.refuse_usage(declaration, "-P (instructions for the chapter "
                                "model) needs -A, or -O extras-chapters.")
        return 1

    script_dir = script_dir or commands.script_dir()
    runcontext.settle("LOG_TIMESTAMPS", "1")
    roots, names = movierun._resolve_roots(declaration, result.positionals)
    if roots is None:
        return 1

    # A film is ingest-movies' to ingest: a folder holding one feature-length
    # video and no episode is passed over by every walk of the run, and
    # listed at its end.
    films: dict = {}
    for root, name in zip(roots, names, strict=True):
        films[name] = rules.films_in(root, movierules._duration_of)
    held = [path for found in films.values() for path in found]
    try:
        with treewalk.holding_out(held):
            return _run_phases(result, program, script_dir, roots, names,
                               kept, wanted, keep_parts, omit_phases, phase)
    finally:
        _report_films(script_dir, films)
        ramscratch.run_exit_cleanup()


def _report_films(script_dir: str, films: dict) -> None:
    for name, found in films.items():
        listing = commands.logs_file(script_dir, FILMS_LIST % name)
        if rules.write_lines(listing, "Folders holding a film rather than a "
                             "show, left alone - ingest-movies is for them:",
                             found):
            log('%d folder(s) hold a film rather than a show and were left '
                'alone - listed in "%s"' % (len(found), listing))


def _run_phases(result, program: str, script_dir: str, roots: list,
                names: list, kept: tuple, wanted: tuple,
                keep_parts: frozenset, omit_phases: frozenset,
                phase: str) -> int:
    write = bool(result.values.get("writeTags"))
    if phase == "layout":
        return _layout_only(program, script_dir, roots)
    if phase == "roles":
        return movierun._roles_only(program, roots)
    if phase == "ocr":
        return movierun._ocr_only(program, script_dir, roots, wanted,
                                  items=rules.episode_files)
    if phase == "commentary":
        return _commentary_only(program, script_dir, roots)
    if phase == "tagging":
        if not movierun._may_overwrite(
                script_dir, names, (UNMATCHED_LIST, AMBIGUOUS_LIST,
                                    RENAMES_LIST, NEAR_MISS_LIST,
                                    CONFLICTS_LIST), (DUPLICATES_LIST,)):
            return 1
        return _tags_only(program, script_dir, roots, names, write,
                          result.values.get("idList") or "")
    if phase == "subtitles":
        if not movierun._may_overwrite(script_dir, names, (SUBTITLES_LIST,)):
            return 1
        return _subtitles_only(program, script_dir, roots, names, write,
                               wanted)
    if phase == "names":
        if tooldeps.require_tools(program, ["mkvmerge", "mkvextract"]
                                  + (["mkvpropedit"] if write else [])):
            return 1
        if not movierun._may_overwrite(script_dir, names,
                                       (COMMENTARY_NAMES_LIST,)):
            return 1
        return _names_only(roots, names, write, script_dir)
    if phase == "chapters":
        if tooldeps.require_tools(program, ["ffprobe", "curl", "mkvpropedit"]):
            return 1
        for root in roots:
            _chapter_phase(root)
            safety.exit_if_aborted()
        return 0
    if phase == "extras-chapters":
        return movierun._extras_chapters_only(
            program, script_dir, roots,
            (result.values.get("chapterInstructions") or "").strip())
    if phase == "split":
        return _split_only(program, script_dir, roots, names)
    if phase == "plex":
        if tooldeps.require_tools(program, ["curl", "mkvmerge", "mediainfo",
                                            "ffprobe", "ffmpeg"]):
            return 1
        return compare_roots(roots, log)
    if phase == "backups":
        if not movierun._may_overwrite(script_dir, names, (BACKUPS_LIST,)):
            return 1
        return movierun._drop_backups(program, script_dir, roots, names,
                                      write, kept,
                                      items=rules.all_episode_files,
                                      backups_list=BACKUPS_LIST)
    return _full_ingest(result, program, script_dir, roots, names, kept,
                        wanted, keep_parts, omit_phases)


# --- the layout ------------------------------------------------------------------

def _lay_out(state, root: str, omit: frozenset = frozenset()) -> set:
    """The layout phases: the junk gone, every video Matroska, each loose
    episode and season pack in its show's folder, every season in a
    ``Season NN``, the specials in ``Season 00``, the extras where the
    servers read them and the subtitles beside their episode. Answers the
    files named as remuxes, before anything renames them."""
    if "junk" in omit:
        log("Phase: cleaning up junk files - OMITTED (-o junk)")
    else:
        log("Phase: cleaning up junk files")
        movierules.cleanup(root)
    log("Phase: muxing non-Matroska videos into Matroska")
    movierules.mkv_mux(root)
    named = (convertphase.named_remuxes(root)
             if getattr(state, "convert_remuxes", False) else set())
    log("Phase: normalising file extensions to lower case")
    safety.lower_case_extensions(root, state.skips)
    log("Phase: sorting loose episodes and season packs into their shows")
    rules.loose_episodes_into_shows(root, state.skips)
    rules.season_packs_into_shows(root, state.skips)
    log("Phase: sorting episodes into season folders")
    for show in rules.shows_in(root):
        rules.season_folders_named(show, state.skips)
        rules.episodes_into_seasons(show, state.skips)
    log("Phase: sorting extras into the folders the servers read")
    for show in rules.shows_in(root):
        rules.extras_sorted(show, state.skips)
    log("Phase: moving subtitles beside their episodes")
    for show in rules.shows_in(root):
        rules.subtitles_beside(show, state.skips)
    return named


def _layout_only(program: str, script_dir: str, roots: list) -> int:
    if tooldeps.require_tools(program, ["ffprobe", "mkvmerge", "mediainfo"]):
        return 1
    ramscratch.init_ram_base()
    ram_root, status = ramscratch.ram_scratch_dir("ingestSeries")
    if status != 0:
        return 1
    ramscratch.add_exit_cleanup([ram_root])
    safety.init_safety_log(os.path.join(ram_root, "safetySkips.log"))
    safety.init_abort_flag(os.path.join(ram_root, "abortRequested"))
    safety.trap_run_abort()
    state = movierun.Run(script_dir=script_dir, ram_root=ram_root,
                         skips=safety.RunSkipLog())
    safety.set_run_footer(safety.report_safety_skips)
    for root in roots:
        log("Laying out: " + root)
        _lay_out(state, root)
        movierules.cleanup(root)
        safety.exit_if_aborted()
    safety.print_run_footer()
    return 0


# --- the commentary ----------------------------------------------------------------

def _commentary_only(program: str, script_dir: str, roots: list) -> int:
    """The commentary transcription on its own, over every episode: the
    movie ingest's walk and queue, with nothing renamed first."""
    ffmpegselect.select_ffmpeg()
    ffmpegselect.report_ffmpeg_selection()
    if tooldeps.require_tools(program, ["ffmpeg", "ffprobe", "mkvmerge"]):
        return 1
    if not movierun._settle_subtitle_work(commentary_only=True):
        log("Commentary-only has no other phase to run - nothing was done.")
        return 0
    quality = movierun._settle_ffsubsync_quality(judged="")
    ramscratch.init_ram_base()
    ram_root, status = ramscratch.ram_scratch_dir("ingestSeries")
    if status != 0:
        return 1
    ramscratch.add_exit_cleanup([ram_root])
    safety.init_abort_flag(os.path.join(ram_root, "abortRequested"))
    safety.trap_run_abort()
    from medialib.lib import whisper as whisper_lib
    whisper = whisper_lib.init_whisper_model(str(runlog.cpu_count()),
                                             ram_root, log)
    state = movierun.Run(script_dir=script_dir, ram_root=ram_root,
                         skips=safety.RunSkipLog(), fragments_file="",
                         whisper=whisper, whisper_said=[],
                         ffsubsync_quality=quality, items=rules.episode_files)
    orphans: list = []
    for root in roots:
        root_orphans: list = []
        commentarytranscription.export_commentary(
            root, movierules.read_track_info, movierules.is_bonus_folder,
            lambda name: name, movierules.audio_stream_index, ram_root,
            whisper, log,
            lambda producer: movierun._drain_commentary(state, producer,
                                                        whisper["jobs"]),
            movierules.MAX_WHISPER_SYNC_OFFSET, quality,
            same_commentary_name=movierules._same_commentary,
            orphans=root_orphans)
        orphans.extend(os.path.normpath(os.path.join(root, relative))
                       for relative in root_orphans)
        safety.exit_if_aborted()
    movierun._write_commentary_orphans(script_dir, orphans)
    return workerpool.exit_status(0)


# --- the tagging -------------------------------------------------------------------

def _spelling(show, folder_title: str) -> str:
    """The catalogue title the show folder is named under: the first of the
    show's own titles the folder's title meets - which keeps a show filed
    under its own language's title - else the catalogue's main one."""
    want = titlematch.title_keys(folder_title)
    for title in show.titles:
        if titlematch.title_keys(title) & want:
            return title
    return show.title


def tag_shows(root: str, write: bool, say, skips, ids: dict | None = None,
              shows: Shows | None = None, tagged: dict | None = None,
              seen: set | None = None) -> dict:
    """Every show under ``root`` identified and, with ``write``, named: the
    series folder, its seasons and every episode. Answers the run's lists:
    unmatched, ambiguous, near misses and the renames planned."""
    lists: dict = {"unmatched": [], "ambiguous": [], "near": [],
                   "renames": [], "hints": []}
    for show_path in rules.shows_in(root):
        name = os.path.basename(show_path.rstrip("/\\"))
        if seen is not None:
            seen.add(name)
        notes: list = []
        show = _show_for(show_path, ids, notes)
        if show is None:
            if "AMBIGUOUS" in notes:
                lists["ambiguous"].append(name)
            else:
                lists["unmatched"].append(name)
            lists["near"].extend(line for line in notes if line != "AMBIGUOUS")
            say("Not identified, left as it is: " + name)
            continue
        if shows is not None:
            shows.remember(show_path, show)
        dated = any(episodes.date_of(os.path.basename(path))
                    and not episodes.has_episode_token(os.path.basename(path))
                    for path in rules.all_episode_files(show_path))
        seasons = (sorted(show.seasons) if dated
                   else sorted(rules.episodes_on_disk(show_path).episodes))
        show = seriesidentify.with_episodes(show, seasons)
        read = seriesfolders.read_series_folder(name)
        provider, number = show.tag_provider()
        if tagged is not None:
            tagged.setdefault("{%s-%s}" % (provider, number), []).append(
                show_path)
        renames = rules.plan_renames(show_path, show,
                                     spelling=_spelling(show, read.title))
        for target, sources in rules.duplicate_targets(renames):
            say("  WARNING: %d files would all be named %s - left as they "
                "are" % (len(sources), os.path.basename(target)))
            for source in sources:
                skips.record(source, target)
            renames = [rename for rename in renames
                       if os.path.normcase(rename.target) != target]
        hint = rules.order_hint(show)
        if hint:
            lists["hints"].append("%s: %s" % (name, hint))
            say("  NOTE: %s - %s" % (name, hint))
        say("%s: %s %s \"%s\"%s" % (name, "TMDb" if provider == "tmdb"
                                     else "TheTVDB", number, show.title,
                                     " (%s)" % show.year if show.year else ""))
        for rename in renames:
            lists["renames"].append("%s\n    -> %s"
                                    % (os.path.relpath(rename.source, root),
                                       os.path.basename(rename.target)))
        if write:
            rules.carry_out(renames, skips, say)
        else:
            for rename in renames:
                say("  would rename %s -> %s"
                    % (os.path.basename(rename.source),
                       os.path.basename(rename.target)))
    return lists


def _tags_only(program: str, script_dir: str, roots: list, names: list,
               write: bool, id_list: str) -> int:
    """The tagging on its own: each show identified and named. A dry run
    unless -w, writing the lists a person works through."""
    say = movierun._preview_log(write)
    blank: list = []
    ids = rules.read_id_list(id_list, blank) if id_list else {}
    if ids is None:
        sys.stderr.write('The id list "%s" cannot be read.\nNothing was '
                         "changed.\n" % id_list)
        return 1
    if not os.environ.get("tmdbApiKey") and not tvdbclient.available():
        sys.stderr.write("Neither tmdbApiKey nor tvdbApiKey is set, so there "
                         "is nothing to tag with.\n")
        return 1
    if tooldeps.require_tools(program, ["curl"]):
        return 1
    if not tvdbclient.available():
        say("tvdbApiKey is not set: a show TMDb cannot name is not asked of "
            "TheTVDB")
    tmdbclient.remember_answers()
    tvdbclient.remember_answers()
    say("Phase: naming shows and episodes (Plex/Jellyfin naming)"
        + ("" if write else " - DRY RUN, nothing will be renamed"))
    tagged: dict = {}
    seen: set = set()
    unmatched_all: list = []
    try:
        for root, name in zip(roots, names, strict=True):
            if len(roots) > 1:
                say('Tagging "%s"' % root)
            skips = safety.RunSkipLog()
            lists = tag_shows(root, write, say, skips, ids, tagged=tagged,
                              seen=seen)
            _write_tag_lists(script_dir, name, root, lists, skips, write,
                             bool(id_list), say)
            unmatched_all += lists["unmatched"]
    finally:
        tmdbclient.remember_answers(False)
        tvdbclient.remember_answers(False)
    duplicates = ["%s\n    %s" % (tag, "\n    ".join(folders))
                  for tag, folders in sorted(tagged.items())
                  if len(folders) > 1]
    if rules.write_lines(commands.logs_file(script_dir, DUPLICATES_LIST),
                         "Shows kept in more than one folder:", duplicates):
        say("WARNING: %d show(s) are kept in more than one folder - listed in "
            '"%s"' % (len(duplicates),
                      commands.logs_file(script_dir, DUPLICATES_LIST)))
    if id_list and write:
        rules.write_id_list(id_list, unmatched_all, ids, program)
    stale = sorted(set(ids or {}) - seen) if id_list else []
    for base in stale:
        sys.stderr.write('ERROR: the id list names "%s", which is not a show '
                         "folder here - not applied\n" % base)
    if not write:
        say("Dry run: nothing was renamed. Pass -w to carry these out.")
    return 0


def _write_tag_lists(script_dir: str, name: str, root: str, lists: dict,
                     skips, write: bool, id_list: bool, say) -> None:
    def path(pattern: str) -> str:
        return commands.logs_file(script_dir, pattern % name)

    if skips.skips:
        rules.write_lines(path(CONFLICTS_LIST), "Renames held back, the name "
                          "already taken:", ["%s\n    -> %s" % pair
                                             for pair in skips.skips])
        say('%d rename(s) in "%s" were held back, the name already taken - '
            'listed in "%s"' % (len(skips.skips), root, path(CONFLICTS_LIST)))
    if id_list:
        return
    if lists["unmatched"]:
        rules.write_id_list(path(UNMATCHED_LIST), lists["unmatched"])
        say('%d show(s) in "%s" could not be identified - listed in "%s" to '
            "fill in by hand" % (len(lists["unmatched"]), root,
                                 path(UNMATCHED_LIST)))
    if rules.write_lines(path(AMBIGUOUS_LIST), "Shows more than one catalogue "
                         "entry fits - give the id with -i:",
                         lists["ambiguous"]):
        say('%d show(s) in "%s" fit more than one catalogue entry - listed in '
            '"%s"' % (len(lists["ambiguous"]), root, path(AMBIGUOUS_LIST)))
    if rules.write_lines(path(RENAMES_LIST), "Every rename the tagging %s:"
                         % ("made" if write else "would make"),
                         lists["hints"] + lists["renames"]):
        say('%d rename(s) in "%s" %s - listed in "%s"'
            % (len(lists["renames"]), root, "made" if write else "would be made",
               path(RENAMES_LIST)))
    if not write:
        rules.write_lines(path(NEAR_MISS_LIST), "What the catalogues were "
                          "asked about the shows left alone:", lists["near"])


# --- the subtitles -----------------------------------------------------------------

def _subtitles_only(program: str, script_dir: str, roots: list, names: list,
                    write: bool, wanted: tuple) -> int:
    """The subtitles beside every episode tested, and with -w synced or
    thrown out and the missing ones downloaded - searched for by the show's
    IMDb id with the season and episode. A dry run unless -w."""
    say = movierun._preview_log(write)
    ffmpegselect.select_ffmpeg()
    ffmpegselect.report_ffmpeg_selection()
    if tooldeps.require_tools(program, ["ffmpeg", "ffprobe", "ffsubsync"]
                              + (["pipx"] if write else [])):
        return 1
    quality = movierun._settle_ffsubsync_quality(judged="a subtitle",
                                                 say_unchecked=False)
    if quality == "no":
        sys.stderr.write("This ffsubsync cannot refuse an alignment, so every "
                         "subtitle would pass the test.\nUpgrade ffsubsync. "
                         "Nothing was changed.\n")
        return 1
    providers = subtitlefiles.providers_to_ask() if write else ()
    if write and subtitlefiles.missing_login_warning(providers):
        say(subtitlefiles.missing_login_warning(providers))
    movierun._warn_without_subcleaner()
    done = donelog.DoneLog(commands.logs_file(script_dir, SUBTITLES_DONE),
                           key=seriesnames.series_key)
    totals = {"kept": 0, "discarded": 0, "untested": 0}
    with tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR")) as meter_dir:
        meter = os.path.join(meter_dir, "subtitleAds.meter")
        with aisubtitleads.metering(meter):
            for root, name in zip(roots, names, strict=True):
                say('Phase: testing the subtitles beside the episodes in "%s"'
                    % root + ("" if write else " - DRY RUN, nothing will be "
                              "changed"))
                verdicts = subtitlefiles.check_subs(
                    root, movierules.MAX_SYNC_OFFSET,
                    movierules.MAX_SYNC_QUALITY_OFFSET, quality, write, say,
                    providers or None, done, wanted=wanted)
                for verdict, found in verdicts.items():
                    totals[verdict] += len(found)
                movierun._write_subtitle_list(
                    commands.logs_file(script_dir, SUBTITLES_LIST % name),
                    root, verdicts, write)
        say(movierun._subtitle_totals(totals))
        movierun._report_subtitle_ads_usage(meter)
    if not write:
        say("Dry run: nothing was changed. Pass -w to sync the subtitles in "
            "step, throw out the rest and download what is missing.")
    return 0


# --- the split -------------------------------------------------------------------

def multi_episode_files(root: str) -> list[str]:
    """The episode files under ``root`` that hold more than one episode."""
    return [path for path in rules.episode_files(root)
            if len(episodes.episodes_of(os.path.basename(path))) > 1]


def split_file(path: str, ram_root: str, say, meter_file: str = "",
               choose=None) -> str:
    """One multi-episode file cut into one file per episode, the original
    kept as "<name> - old.mkv"; "" when done, else why not."""
    name = os.path.basename(path)
    found = episodes.episodes_of(name)
    show = _show_for(seriesnames.series_folder_of(path))
    if show is None:
        return "its show is not identified, so there are no runtimes to go by"
    season = found[0][0]
    show = seriesidentify.with_episodes(show, [season])
    runtimes = []
    for _season, number in found:
        episode = show.episode(season, number)
        if episode is None or not episode.runtime:
            return "the catalogue gives no runtime for episode %d" % number
        runtimes.append(episode.runtime * 60.0)
    length = float(movierules._duration_of(path) or 0)
    from medialib.lib import chapterdb
    existing = chapterdb.existing_chapters(path)
    chapters = [float(start) for start in existing.starts] if existing else []

    def read_window(start: float, span: float):
        from medialib.lib import scenecuts
        return (scenecuts.window_frames(path, start, span),
                seriessplit.silences(path, start, span))

    transcript = _transcript_reader(path, ram_root)
    pick = choose or (lambda expected, candidates: seriessplit.ask_model(
        expected, candidates, transcript, say, meter_file=meter_file))
    plan = seriessplit.plan_split(length, runtimes, chapters, read_window,
                                  pick)
    if not plan.cuts:
        return plan.reason
    say("  Cutting at " + plan.reason)
    stem = os.path.splitext(path)[0]
    scratch = tempfile.mkdtemp(prefix="split-", dir=ram_root)
    try:
        pattern = os.path.join(scratch, "piece.mkv")
        status, said = seriessplit.cut(path, plan.cuts, pattern)
        if status >= 2:
            return "mkvmerge could not cut it: " + said.splitlines()[-1:][0] \
                if said else "mkvmerge could not cut it"
        made = seriessplit.pieces(pattern, len(found))
        if not all(os.path.isfile(piece) for piece in made):
            return "mkvmerge did not write every piece"
        read = seriesnames.read_episode_name(name)
        series = read.series if read else seriesnames.series_folder_of(path)
        folder = os.path.dirname(path)
        targets = []
        for (_season, number), piece in zip(found, made, strict=True):
            episode = show.episode(season, number)
            target = os.path.join(folder, seriesnames.episode_name(
                series, season, [number], episode.title if episode else ""))
            if os.path.lexists(target):
                return "%s is already there" % os.path.basename(target)
            targets.append((piece, target))
        old = stem + plexnames.OLD_SUFFIX
        if os.path.lexists(old):
            return '"%s" is already there' % os.path.basename(old)
        for piece, target in targets:
            shutil.move(piece, target)
        os.rename(path, old)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    return ""


def _transcript_reader(path: str, ram_root: str):
    """What is said between two moments of ``path``, from a text subtitle
    beside it or inside it: the model's view of either side of a seam."""
    cues: list = []
    stem = os.path.splitext(path)[0]
    for name in sorted(os.listdir(os.path.dirname(path) or ".")):
        full = os.path.join(os.path.dirname(path), name)
        if full.startswith(stem + ".") and name.endswith(".srt"):
            cues = _read_srt(full)
            if cues:
                break

    def said(start: float, end: float) -> str:
        return " ".join(text for at, text in cues if start <= at < end)[:1200]
    return said


def _read_srt(path: str) -> list:
    import re
    stamp = re.compile(r"^(\d+):(\d+):(\d+)[,.](\d+)\s*-->")
    cues, at = [], None
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            for line in handle:
                match = stamp.match(line.strip())
                if match:
                    hours, minutes, seconds, millis = (int(group) for group
                                                       in match.groups())
                    at = hours * 3600 + minutes * 60 + seconds + millis / 1000
                elif line.strip() and at is not None \
                        and not line.strip().isdigit():
                    cues.append((at, line.strip()))
    except OSError:
        return []
    return cues


def _split_phase(state, root: str, name: str, script_dir: str) -> None:
    files = multi_episode_files(root)
    if not files:
        log("  No file holds more than one episode")
        return
    left = []
    for path in files:
        log("Splitting: " + path)
        why = split_file(path, state.ram_root, log,
                         getattr(state, "split_meter", ""))
        if why:
            log("  Left whole: " + why)
            left.append("%s\n    %s" % (path, why))
        safety.exit_if_aborted()
    rules.write_lines(commands.logs_file(script_dir, SPLIT_LIST % name),
                      "Files holding several episodes, left whole:", left)


def _split_only(program: str, script_dir: str, roots: list,
                names: list) -> int:
    if tooldeps.require_tools(program, ["ffmpeg", "ffprobe", "mkvmerge",
                                        "curl"]):
        return 1
    ramscratch.init_ram_base()
    ram_root, status = ramscratch.ram_scratch_dir("ingestSeries")
    if status != 0:
        return 1
    ramscratch.add_exit_cleanup([ram_root])
    safety.init_abort_flag(os.path.join(ram_root, "abortRequested"))
    safety.trap_run_abort()
    state = movierun.Run(script_dir=script_dir, ram_root=ram_root)
    for root, name in zip(roots, names, strict=True):
        log('Phase: splitting the files holding several episodes in "%s"'
            % root)
        _split_phase(state, root, name, script_dir)
    return 0


# --- the commentary names --------------------------------------------------------

def commentary_episodes(root: str):
    """Every episode whose commentaries could be named, as ``(episode, the
    show folder's name without its id)``: the title and year a commentary's
    speakers are looked for under."""
    for path in rules.all_episode_files(root):
        folder = os.path.basename(seriesnames.series_folder_of(path))
        base, _tag = plexnames.untagged_base(folder)
        yield path, base


def _no_releases(_title: str, _year: str, _kind: str):
    """The disc database is not asked about an episode: it lists a show's
    commentaries per disc, not per episode, and naming a track after the
    wrong episode's speakers is worse than leaving it numbered."""
    return None


def _names_in(state_whisper, root: str, name: str, write: bool,
              script_dir: str, usage, whisper_provider=None) -> None:
    movierun._commentary_names_in(
        root, name, write, _no_releases, "", script_dir, queued=False,
        usage=usage, whisper=state_whisper,
        whisper_provider=whisper_provider, films=commentary_episodes,
        names_list=COMMENTARY_NAMES_LIST)


def _names_only(roots: list, names: list, write: bool,
                script_dir: str) -> int:
    """The commentary naming on its own: each episode's numbered
    commentaries named after the speakers who introduce themselves in their
    first minutes (a language model, under -A). A dry run unless -w."""
    say = movierun._preview_log(write)
    if not aicommentarynames.available():
        say("No model is set up to read who introduces themselves (run with "
            "-A, docs/models.md): every episode's commentaries stay numbered")
    usage = aicommentarynames.Usage()
    settled: list = []

    def whisper() -> dict:
        if not settled:
            from medialib.lib import whisper as whisper_lib
            ramscratch.init_ram_base()
            ram_root, _status = ramscratch.ram_scratch_dir("ingestSeries")
            ramscratch.add_exit_cleanup([ram_root])
            settled.append(whisper_lib.init_whisper_model(
                str(runlog.cpu_count()), ram_root, log))
        return settled[0]

    for root, name in zip(roots, names, strict=True):
        say('Phase: naming numbered commentary tracks in "%s"' % root
            + ("" if write else " - DRY RUN, nothing will be renamed"))
        _names_in(None, root, name, write, script_dir, usage, whisper)
        safety.exit_if_aborted()
    if usage.requests:
        movierun._report_commentary_names_usage(usage)
    if not write:
        say("Dry run: nothing was renamed. Pass -w to carry these out.")
    return 0


# --- the chapters ----------------------------------------------------------------

def show_title(episode: str) -> str:
    """The title an episode is looked up in the chapter archive by: its
    show's, once the tagging has given the show folder its id - "" before,
    when the title is only a guess."""
    read = seriesfolders.read_series_folder(
        os.path.basename(seriesnames.series_folder_of(episode)))
    return read.title if read.tag else ""


def _folded(text: str) -> str:
    return "".join(character for character in text.lower()
                   if character.isalnum())


def row_fits_for(episode: str, existing):
    """Which of the archive's sets are this episode's: filed under its show,
    and naming the episode - its token, or its title - in the set's title.
    A set that names neither is taken only for an episode that already has
    numbered chapters, where :func:`chapterdb.choose` then holds it to the
    same marks: sets of one show run to the same length episode after
    episode, and a length alone says nothing about which this is."""
    show = show_title(episode)
    found = episodes.episodes_of(os.path.basename(episode))
    read = seriesnames.read_episode_name(os.path.basename(episode))
    title = _folded(read.title) if read and len(read.title) > 3 else ""
    numbered = existing is not None and existing.kind == "numbered"

    def fits(row) -> bool:
        written = chapterdb._comparable(row.title)
        if not _folded(written).startswith(_folded(show)):
            return False
        theirs = episodes.episodes_of(written)
        if found and theirs:
            return theirs[0] == found[0]
        if title and title in _folded(written):
            return True
        return numbered
    return fits


def _chapter_phase(root: str, meter_file: str = "") -> None:
    log('Phase: looking up chapters for the episodes in "%s"' % root)
    outcome = chapterdb.add_chapters(
        root, log, meter_file=meter_file,
        movies=rules.all_episode_files(root), title_of=show_title,
        row_fits_for=row_fits_for)
    movierun._report_chapters({verdict: len(found)
                               for verdict, found in outcome.items()})


# --- the carry-over from the media server's copy ----------------------------------

def episode_key(episode: str) -> tuple:
    """What an episode's carry-over is kept under for the run: its season
    folder (which the renames keep) and its season and episode."""
    found = episodes.episodes_of(os.path.basename(episode))
    return plexphase.folder_key(episode) + (found[0] if found else
                                            (os.path.basename(episode),))


def episode_edition(episode: str) -> tuple:
    """An episode's (edition, part): episodes have no edition."""
    read = seriesnames.read_episode_name(os.path.basename(episode))
    return "", read.part if read else ""


def _read_episodes(server) -> dict:
    return _episode_index(server.episode_copies())


def _show_tags(folder: str, shows: Shows | None) -> list:
    """Every id the show an episode is of is known by, Plex spelling - the
    server may hold it under another catalogue's than the folder's."""
    tags = []
    match = plexnames.ID_TAG_RE.search(os.path.basename(folder))
    if match:
        tags.append(plexnames.canonical_id(match.group(0)))
    show = shows.of(folder) if shows is not None else None
    if show is not None:
        for provider, number in (("tmdb", show.tmdb), ("tvdb", show.tvdb),
                                 ("imdb", show.imdb)):
            if number and "{%s-%s}" % (provider, number) not in tags:
                tags.append("{%s-%s}" % (provider, number))
    return tags


def found_episode(shows: Shows | None, episode: str, index: dict,
                  _tag: str = "") -> list:
    """The media server's copies of one episode, kept anywhere but beside
    it, as the carry-over reads a copy."""
    found = episodes.episodes_of(os.path.basename(episode))
    if not found:
        return []
    directory = os.path.dirname(os.path.abspath(episode))
    copies: list = []
    for tag in _show_tags(seriesnames.series_folder_of(episode), shows):
        for copy in index.get((tag, found[0][0], found[0][1]), ()):
            if copy in copies or any(
                    os.path.dirname(os.path.abspath(file)) == directory
                    for file in copy.files):
                continue
            copies.append(copy)
    return [mediaserver.Copy(copy.key, Film(copy.show_title), copy.section,
                             "", copy.files) for copy in copies]


def start_carry(state, root: str):
    """The carry-over of every episode under ``root`` from the media
    server's copy, looked up on a thread of its own from now on."""
    carry = carryphase.LibraryCarry(
        root, lambda: rules.episode_files(root), None, log, state.ram_root,
        state.keep_parts, read_library=_read_episodes,
        found_in=lambda episode, index, tag: found_episode(
            state.shows, episode, index, tag),
        key=episode_key, edition=episode_edition)
    carry.start()
    return carry


# --- the media server ------------------------------------------------------------

def _episode_index(copies: list) -> dict:
    index: dict = {}
    for copy in copies:
        for provider in ("tmdb", "tvdb", "imdb"):
            number = copy.show_ids.get(provider)
            if number:
                index.setdefault(("{%s-%s}" % (provider, number), copy.season,
                                  copy.episode), []).append(copy)
    return index


def compare_roots(roots: list, say) -> int:
    """Every episode under ``roots`` held up against the media server's copy
    of it, found by the show's id and the season and episode; 1 where the
    server could not be asked."""
    provider = mediaserver.provider()
    server = provider.local()
    say("%s: reading the show libraries at %s" % (provider.display, server.url))
    try:
        copies = server.episode_copies()
    except mediaserver.MediaServerError as error:
        say("%s: SKIPPED, the server could not be asked (%s) - %s"
            % (provider.display, error, provider.hint()))
        return 1
    say("%s: %d episode version(s) in the library" % (provider.display,
                                                      len(copies)))
    index = _episode_index(copies)
    tally: dict = {}
    for root in roots:
        for path in rules.all_episode_files(root):
            verdict, places = _compare_episode(path, index, say)
            tally.setdefault(verdict, []).append((path, places))
    plexphase._report(tally, say)
    return 0


def _compare_episode(path: str, index: dict, say) -> tuple:
    say("%s: %s" % (mediaserver.provider().display, os.path.basename(path)))
    folder = os.path.basename(seriesnames.series_folder_of(path))
    match = plexnames.ID_TAG_RE.search(folder)
    found = episodes.episodes_of(os.path.basename(path))
    if not match or not found:
        say("  not compared: the show carries no id yet (run the tagging)")
        return "unread", []
    key = (plexnames.canonical_id(match.group(0)), found[0][0], found[0][1])
    directory = os.path.dirname(os.path.abspath(path))
    elsewhere = [copy for copy in index.get(key, ())
                 if not any(os.path.dirname(os.path.abspath(file)) == directory
                            for file in copy.files)]
    if not elsewhere:
        say("  not in the library")
        return "absent", []
    read = seriesnames.read_episode_name(os.path.basename(path))
    part = read.part if read else ""
    new = None if part else plexcompare.read_profile(path, "")
    verdicts = []
    for copy in elsewhere:
        say("  in the library (%s):" % copy.section)
        for file in copy.files:
            say("    " + file)
        verdicts.append(plexphase._against(
            path, new, part, mediaserver.Copy(copy.key, Film(copy.show_title),
                                              copy.section, "", copy.files),
            say))
    places = [file for copy in elsewhere for file in copy.files]
    for verdict in plexphase._INTEREST:
        if verdict in verdicts:
            return verdict, places
    return "unread", places


# --- the full ingest -------------------------------------------------------------

def converted_stem(stem: str) -> str:
    """The stem an episode's converted copy is written under: a second
    version of the episode, named the way the server merges versions."""
    naming = seriesnames.style()
    if naming.version == "bracket":
        return "%s [%s]" % (stem, plexnames.CONVERTED_EDITION)
    return "%s - %s" % (stem, plexnames.CONVERTED_EDITION)


def _full_ingest(result, program: str, script_dir: str, roots: list,
                 names: list, kept: tuple, wanted: tuple,
                 keep_parts: frozenset, omit: frozenset) -> int:
    ffmpegselect.select_ffmpeg()
    ffmpegselect.report_ffmpeg_selection()
    tools = ["ffmpeg", "ffprobe", "mkvmerge", "mkvpropedit", "mediainfo"]
    if os.environ.get("tmdbApiKey") or tvdbclient.available():
        tools.append("curl")
    if result.values.get("extrasChapters"):
        tools += [spec for spec in tooldeps.command_tools("transcribe-audio")
                  if spec not in tools]
    if tooldeps.require_tools(program, tools):
        return 1
    extras_llm = None
    if result.values.get("extrasChapters"):
        extras_llm = chapterwork.model(
            program, (result.values.get("chapterInstructions") or "").strip(),
            "-A", log)
        if extras_llm is None:
            return 1
    subtitle_work = movierun._settle_subtitle_work()
    quality = movierun._settle_ffsubsync_quality() if subtitle_work else "no"
    if subtitle_work:
        movierun._warn_without_subcleaner()
    if "ocr" not in omit:
        movierun._warn_without_seconv()
    if not tvdbclient.available():
        log("tvdbApiKey is not set: a show TMDb cannot name is not asked of "
            "TheTVDB")

    ingestable = [root for root in roots if movierules._files_below(
        root, matches=seriesfolders.is_video)]
    for root in roots:
        if root not in ingestable:
            if len(roots) == 1:
                return safety.fail_no_relevant_input(root, "episodes (.mkv, "
                                                     "or a video to remux)")
            log('Nothing this ingest can read under "%s" - skipped' % root)
    if not ingestable:
        return 1

    ramscratch.init_ram_base()
    ram_root, status = ramscratch.ram_scratch_dir("ingestSeries")
    if status != 0:
        return 1
    ramscratch.add_exit_cleanup([ram_root])
    statusline.init_status_line()
    console.Console(ram_root).activate()
    safety.init_safety_log(os.path.join(ram_root, "safetySkips.log"))
    durationcheck.init_log(os.path.join(ram_root, "lengthMismatch.log"))
    disk_turns = diskturn.init(os.path.join(ram_root, "diskTurns"))
    tmdbclient.remember_answers()
    tvdbclient.remember_answers()
    skips = safety.RunSkipLog()
    safety.init_abort_flag(os.path.join(ram_root, "abortRequested"))
    safety.trap_run_abort()
    ocr_usage = subtitleocr.Usage()
    ads_meter = os.path.join(ram_root, "subtitleAds.meter")
    from medialib.lib import aicommentary
    judge_usage = aicommentary.Usage()
    names_usage = aicommentarynames.Usage()
    unsure: list = []

    def recap() -> None:
        safety.report_safety_skips()
        durationcheck.report()
        movierun._report_unsure_commentaries(unsure)
        if judge_usage.requests:
            movierun._report_commentary_judge_usage(judge_usage)
        if ocr_usage.requests:
            movierun._report_ocr_usage(ocr_usage)
        movierun._report_subtitle_ads_usage(ads_meter)
        if names_usage.requests:
            movierun._report_commentary_names_usage(names_usage)

    safety.set_run_footer(recap)
    whisper: dict = {}
    whisper_said: list = []
    if subtitle_work:
        from medialib.lib import whisper as whisper_lib
        whisper = whisper_lib.init_whisper_model(
            str(runlog.cpu_count()), ram_root, whisper_said.append)
    shows = Shows()
    state = movierun.Run(
        script_dir=script_dir, ram_root=ram_root, skips=skips,
        fragments_file="", whisper=whisper, whisper_said=whisper_said,
        ffsubsync_quality=quality, long_names=None, unfixed_movies=[],
        unsure_commentaries=unsure, disk_turns=disk_turns,
        scratch_headroom=0, ocr_usage=ocr_usage,
        commentary_judge_usage=judge_usage, bonus_meter="",
        commentary_names_usage=names_usage,
        convert_remuxes=bool(result.values.get("convertRemuxes")),
        keep_parts=keep_parts, omit_phases=omit, extras_llm=extras_llm,
        film_year_meter="", film_tags=None, carry=None,
        gpu_gate=gputurn.Gate(os.path.join(ram_root, "gpuTurns")),
        keep_languages=kept, subtitle_languages=wanted,
        items=rules.episode_files, languages_of=shows.languages_of,
        shows=shows, split_meter=os.path.join(ram_root, "split.meter"),
        ai=bool(result.values.get("extrasChapters")))
    listed = dict(zip(roots, names, strict=True))
    try:
        with aisubtitleads.metering(ads_meter):
            for root in ingestable:
                _ingest(state, root, subtitle_work, listed[root])
    finally:
        tmdbclient.remember_answers(False)
        tvdbclient.remember_answers(False)
        diskturn.adopt("")
        ramscratch.run_exit_cleanup()
    return workerpool.exit_status(1 if durationcheck.failures() else 0)


def _ingest(state, root: str, subtitle_work: bool, name: str) -> None:
    omit: frozenset = state.omit_phases
    log("Starting ingest: " + root)
    named_remuxes = _lay_out(state, root, omit)

    # Each show asked for now, so the remux keeps the language the show was
    # made in; the names are written only at the end.
    if os.environ.get("tmdbApiKey") or tvdbclient.available():
        for show in rules.shows_in(root):
            state.shows.of(show)

    # The server's copies looked up and measured against while the phases up
    # to the lanes run, as ingest-movies does.
    state.carry = None
    if movierun._has_tool("curl") and "plex" not in omit:
        state.carry = start_carry(state, root)

    if "roles" in omit:
        log("Phase: refreshing mkv tags and track flags - subtitle roles "
            "OMITTED (-o roles)")
    else:
        log("Phase: refreshing mkv tags and track flags")
    movierules.update_tags(root, roles="roles" not in omit)
    if "junk" not in omit:
        movierules.cleanup(root)

    if subtitle_work and "commentary" not in omit:
        log("Phase: listening for commentaries the files do not flag")
        movierun._detect_commentaries(state, root)

    if state.carry is not None:
        log("Phase: settling what each episode carries over from the media "
            "server's copy")
        state.carry.settle(state.carry_keeps, state.carry_names,
                           subtitle_work)
    elif "plex" in omit:
        log("Phase: settling what each episode carries over from the media "
            "server's copy - OMITTED (-o plex)")

    locks = lanes.FilmLocks()
    row = console.LaneRow((movierules.lossless_audio_target(), "whisper",
                           "remux", "subs", "chapters"))
    run_console = console.current()
    run_console.show(row.render, travels=False)
    try:
        movierun._films_in_lanes(state, root, subtitle_work, locks, row,
                                 fetching=None,
                                 commentary_work=subtitle_work
                                 and "commentary" not in omit)
    finally:
        run_console.hide()

    if state.ai and "split" not in omit:
        log("Phase: splitting the files holding several episodes")
        _split_phase(state, root, name, state.script_dir)
    elif "split" not in omit:
        log("Phase: splitting the files holding several episodes - SKIPPED "
            "(needs -A); each keeps its range name")

    if "tagging" in omit:
        log("Phase: naming shows and episodes - OMITTED (-o tagging)")
    elif not (os.environ.get("tmdbApiKey") or tvdbclient.available()):
        log("Phase: naming shows and episodes - SKIPPED (neither tmdbApiKey "
            "nor tvdbApiKey is set)")
    else:
        log("Phase: naming shows and episodes (Plex/Jellyfin naming)")
        lists = tag_shows(root, True, log, state.skips, shows=state.shows)
        _write_tag_lists(state.script_dir, name, root, lists, state.skips,
                         True, False, log)

    if subtitle_work and "subtitles" not in omit:
        log("Phase: downloading missing subtitles")
        subtitlefiles.download_subs(
            root, subtitlefiles.providers_to_ask(),
            movierules.MAX_SYNC_OFFSET, movierules.MAX_SYNC_QUALITY_OFFSET,
            state.ffsubsync_quality, log, wanted=state.subtitle_languages)
    elif "subtitles" in omit:
        log("Phase: downloading missing subtitles - OMITTED (-o subtitles)")
    else:
        log("Phase: downloading missing subtitles - SKIPPED (no "
            "ffsubsync/pipx, see the warning at startup)")

    if "commentary" in omit:
        log("Phase: naming numbered commentary tracks - OMITTED "
            "(-o commentary)")
    else:
        log("Phase: naming numbered commentary tracks")
        _names_in(state.whisper or None, root, name, True, state.script_dir,
                  state.commentary_names_usage)
    if "chapters" in omit:
        log("Phase: looking up chapters - OMITTED (-o chapters)")
    elif not movierun._has_tool("curl"):
        log("Phase: looking up chapters - SKIPPED (curl is not installed)")
    else:
        _chapter_phase(root, os.path.join(state.ram_root,
                                          "chapterdb-model.jsonl"))

    if "plex" in omit:
        log("Phase: comparing with the media server - OMITTED (-o plex)")
    elif movierun._has_tool("curl"):
        log("Phase: comparing with the media server")
        compare_roots([root], log)

    if state.convert_remuxes:
        log("Phase: converting the remuxes")
        convertphase.convert_remuxes(root, named_remuxes, state.ram_root,
                                     state.script_dir, log,
                                     films=rules.all_episode_files,
                                     converted=converted_stem)
        safety.exit_if_aborted()

    if "junk" not in omit:
        movierules.cleanup(root)
    safety.print_run_footer()
    log("Ingest complete: " + root)


def cli(argv: list | None = None) -> int:
    settings.load()
    runlog.start_run(argv)
    if tooldeps.require_declared_packages(
            commands.program_name(__spec__.name)):
        return 1
    if argv is None:
        argv = sys.argv[1:]
    return main(argv, commands.program_name(__spec__.name),
                commands.script_dir())


if __name__ == "__main__":
    sys.exit(cli())
