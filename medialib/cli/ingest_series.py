"""ingest-series: a library of television shows, sorted, named and improved
in place.

The counterpart of ingest-movies for shows. What works on one video file is
the movie ingest's, called as it is - the track flags, the lossless audio, the
commentary, the improved-copy remux, the subtitles' sync test, the backups.
What answers "which show is this, and what is each episode called" is this
module's: the layout of series, season and extras folders, and the tagging
that names a show from TMDb (or TheTVDB) and its episodes from the
catalogue's titles.

This module holds the decisions - what a folder is, where a file goes, what
it is renamed to; ``ingest_series_run`` carries them out.
"""

from __future__ import annotations

import os
import re
import sys
from typing import NamedTuple

from medialib import commands
from medialib.cli import ingest_movies as movies
from medialib.lib import (
    clioptions,
    enums,
    episodes,
    languages,
    plexnames,
    safety,
    seriesfolders,
    seriesidentify,
    seriesnames,
    subtitletracks,
    titlematch,
)
from medialib.lib.runlog import log

USAGE_HEAD = """Usage:
    {program} [options] <seriesDir>...

    seriesDir     directory holding the shows to ingest, or one show's own
                  folder. Several may be given: each is worked through in
                  full, one after the other, in the order they were typed.
                  With tagging each gets lists of its own, named after it.

    Options
    -------"""

OPT_SPEC = """
r |  | also convert the remuxes, last of all, with convert-video -k,
                  writing each converted copy beside its episode as a second
                  version of it. The episode itself is left as it is. A full
                  ingest only.
k | [part] | keep <part>, taking nothing away: audio (lossless audio stays
                  lossless), tracks (no track is dropped) and dv (a dual-layer
                  Dolby Vision profile 7 keeps its enhancement layer),
                  comma-separated; a bare -k keeps all three. Cannot be
                  combined with -r.
o | [phase] | omit <phase> from a full ingest: junk, subtitles, ocr,
                  roles, commentary, chapters, plex, tagging and split,
                  comma-separated; a bare -o omits all nine.
O | <phases> | do only these phases, each on its own, over the whole tree
                  below each folder given, in the order typed and stopping at
                  the first that fails: layout, roles, ocr, tagging,
                  subtitles, commentary, names, chapters, extras-chapters,
                  split, plex and backups, comma-separated. Cannot be
                  combined with -r, -k or -o.
A |  | let language models help (docs/models.md): in a full ingest
                  also chapter the extras that have no chapters and split
                  the files holding several episodes, and in any run let the
                  track, commentary, subtitle and extras steps ask a model.
                  Without -A none is asked, and -O extras-chapters and -O
                  split are refused.
P | <text> | with -A or -O extras-chapters: more instructions for the
                  model, added to every request.
w |  | with -O tagging, subtitles, names or backups: carry out what
                  their dry run only prints. On its own it is refused.
i | <file> | with -O tagging: the unmatched list, filled in with ids, read
                  back - a TMDb number, tvdb-<number> or an IMDb id. A dry
                  run unless -w is given."""

OPT_VARS = ("r:convertRemuxes P:chapterInstructions "
            "w:writeTags i:idList k:keepParts o:omitPhases "
            "O:onlyPhases")
OPT_COLUMN = 18
OPT_LONG = ("r:convert-remuxes P:prompt w:write "
            "i:ids k:keep o:omit O:only")

# The single-phase sweeps -O names, in the order the page lists them.
ONLY_PHASES = ("layout", "roles", "ocr", "tagging", "subtitles", "commentary",
               "names", "chapters", "extras-chapters", "split", "plex",
               "backups")

# Of those, the ones whose dry run -w carries out.
ONLY_WRITES = ("tagging", "subtitles", "names", "backups")

# Of those, the ones that ask a language model, and so need -A.
ONLY_NEEDS_AI = ("extras-chapters", "split")

# The phases a full ingest can be told to omit with -o.
OMIT_PHASES = ("junk", "subtitles", "ocr", "roles", "commentary",
               "chapters", "plex", "tagging", "split")

USAGE_TAIL = """

    Phases
    ------
    A full ingest runs all but backups (and extras-chapters and split
    without -A), and remuxes each episode once into an improved copy,
    keeping the original as "<name> - old.mkv". -O runs them one at a time.
    junk             deletes release junk (movieJunkExtensions) and samples
    layout           remuxes into Matroska, sorts the episodes into Season
                     folders, the specials into Season 00 and the extras into
                     the folders the servers read
    roles            puts the subtitles' forced and SDH flags right
    ocr              reads picture subtitles into text (seconv)
    tagging          names each show from TMDb, or TheTVDB (tmdbApiKey,
                     tvdbApiKey), and each episode after its title
    subtitles        downloads, syncs and tests the subtitles spokenLanguages
                     names
    commentary       finds and transcribes the commentary tracks (whisper)
    names            names numbered commentaries after who introduces
                     themselves in them (-A)
    chapters         gives the episodes their disc's chapters from ChapterDB
    extras-chapters  chapters the extras by the language model (-A)
    split            cuts a file holding several episodes into one per
                     episode, where the model confirms the seam (-A)
    plex             compares each episode with the media server's copy,
                     and carries over what it lacks
    backups          drops the (old) originals whose copies pass the checks

    Dependencies
    ------------
    Required: ffmpeg, mkvtoolnix, mediainfo. The optional tools, and what a
    run does without each, are in docs/requirements.md.

    Everything else: docs/commands/ingest-series.md"""

# The words a release writes after its title, which a show's name is cut
# before: a resolution, a source, a codec.
_RELEASE_WORDS = re.compile(
    r"(?<![A-Za-z0-9])(?:2160p|1080p|1080i|720p|576p|480p|4k|uhd|bluray|"
    r"blu-ray|bdrip|brrip|web-?dl|webrip|web|hdtv|dvdrip|dvd|remux|x264|"
    r"x265|h\.?264|h\.?265|hevc|avc|complete)(?![A-Za-z0-9])", re.I)
_YEAR_WORD = re.compile(r"(?<![0-9])\(?([12][0-9]{3})\)?(?![0-9])")


def spec(program: str) -> clioptions.Spec:
    omit_arm = ("^(" + "|".join(OMIT_PHASES) + ")(,("
                + "|".join(OMIT_PHASES) + "))*$")
    return clioptions.with_shared_options(clioptions.Spec(
        head=USAGE_HEAD.format(program=program),
        options=OPT_SPEC,
        long=OPT_LONG,
        vars=OPT_VARS,
        flags=("optionalArg:k:^(audio|tracks|dv)(,(audio|tracks|dv))*$"
               " optionalArg:o:" + omit_arm),
        column=OPT_COLUMN,
        tail=USAGE_TAIL,
        no_args_with_credits=False,
        no_args_stream="stderr",
    ))


# --- reading a release's name -------------------------------------------------

def show_title_of(name: str) -> tuple[str, str]:
    """(title, year) a release's file or folder name gives its show: what
    stands before the episode or season token, the year it writes, or the
    first release word - with dots and underscores read as spaces."""
    stem = name
    if "." in name and enums.lower_extension_of(name) in (
            "mkv", *enums.SOURCE_VIDEO_EXTENSIONS):
        stem = os.path.splitext(name)[0]
    stem = plexnames.ID_TAG_RE.sub(" ", stem)
    text = stem if " " in stem.strip() else stem.replace(".", " ")
    text = text.replace("_", " ")
    cut = len(text)
    for pattern in (episodes.EPISODE, episodes.CROSS_EPISODE,
                    episodes.NAMES_SEASON, _RELEASE_WORDS):
        match = pattern.search(text)
        if match and match.start() > 0:
            cut = min(cut, match.start())
    head = text[:cut]
    year = ""
    years = list(_YEAR_WORD.finditer(head))
    if years and years[-1].start() > 0:
        year = years[-1].group(1)
        head = head[:years[-1].start()]
    title = " ".join(head.replace(" - ", " ").split()).strip(" -.[(")
    return title, year


def show_folder_name(title: str, year: str) -> str:
    return seriesnames.safe_title(title) + (" (%s)" % year if year else "")


# --- the walk ------------------------------------------------------------------

def is_one_show(root: str) -> bool:
    """Whether a folder handed to the run is one show's own folder rather
    than a library of them: it holds season folders, it is itself named for
    a season, or the episodes loose in it are of a show its own name names.
    A folder of loose episodes named for something else - "Downloads" - is a
    library they are sorted out of."""
    name = os.path.basename(root.rstrip("/\\"))
    if not seriesfolders.looks_like_series(root):
        return False
    if seriesfolders.season_folders(root) \
            or episodes.season_of_folder(name) is not None:
        return True
    # Read as a release names itself, so a season pack handed over whole
    # ("Show.S02.1080p") is the show it is a season of.
    own = show_title_of(name)[0]
    for video in seriesfolders.videos_in(root):
        title, _year = show_title_of(video)
        if title and own and titlematch.equivalent(title, own):
            return True
    return False


def shows_in(root: str) -> list[str]:
    """The show folders a run works through under ``root``: ``root`` itself
    when it is one show, else every show directly under it."""
    if is_one_show(root):
        return [root]
    return seriesfolders.series_folders(root)


def films_in(root: str, duration=None) -> list[str]:
    """The folders under ``root`` that hold a film rather than a show - the
    reverse guard, ingest-series' skipping of what ingest-movies is for."""
    if is_one_show(root):
        return []
    found = []
    for name in movies._names_in(root):
        path = os.path.join(root, name)
        if os.path.isdir(path) and not name.startswith(".") \
                and not seriesfolders.looks_like_series(path) \
                and not plexnames.is_bonus_folder_name(name) \
                and seriesfolders.looks_like_film(path, duration):
            found.append(path)
    return found


def _is_episode_video(name: str) -> bool:
    return (enums.lower_extension_of(name) == "mkv"
            and not plexnames.is_kept_copy(name)
            and not seriesnames.is_inline_extra(name))


def episode_files(root: str, say=None) -> list[str]:
    """Every episode file under ``root`` the per-file phases work through:
    the Matroska videos of every show's seasons (and any loose in a show
    folder), not the extras, not a kept or converted copy. One already
    improved is skipped, and said so to ``say``."""
    from medialib.cli import ingest_movies_run as movierun
    found = []
    for show in shows_in(root):
        folders = [show] + [path for _season, path
                            in seriesfolders.season_folders(show)]
        for folder in folders:
            for name in sorted(movies._names_in(folder)):
                path = os.path.join(folder, name)
                if not os.path.isfile(path) or not _is_episode_video(name):
                    continue
                improved = movierun._already_improved(path)
                if improved:
                    if say:
                        say("Already improved, left as it is: %s - %s"
                            % (path, improved))
                    continue
                found.append(path)
    return found


def all_episode_files(root: str, say=None) -> list[str]:
    """:func:`episode_files`, improved or not."""
    found: list[str] = []
    for show in shows_in(root):
        for folder in [show] + [path for _season, path
                                in seriesfolders.season_folders(show)]:
            found.extend(os.path.join(folder, name)
                         for name in sorted(movies._names_in(folder))
                         if os.path.isfile(os.path.join(folder, name))
                         and _is_episode_video(name))
    return found


def episodes_on_disk(show: str, durations=None) -> seriesidentify.OnDisk:
    """What a show folder holds, as the identification asks it: {season:
    {episode numbers}}, and with ``durations`` each episode's length."""
    held: dict = {}
    lengths: dict = {}
    for season, numbers, path in _episodes_with_numbers(show):
        held.setdefault(season, set()).update(numbers)
        if durations is not None and len(numbers) == 1:
            lengths[(season, numbers[0])] = durations(path)
    return seriesidentify.OnDisk(held, lengths)


def _episodes_with_numbers(show: str):
    """(season, episode numbers, path) for every episode video of a show
    whose name or folder says which it is."""
    for season_number, folder in [(None, show)] + seriesfolders.season_folders(show):
        for name in sorted(movies._names_in(folder)):
            path = os.path.join(folder, name)
            if not os.path.isfile(path) or not seriesfolders.is_video(name) \
                    or seriesnames.is_inline_extra(name):
                continue
            found = episodes.episodes_of(name)
            if found:
                yield found[0][0], tuple(number for _s, number in found), path
                continue
            number = episodes.numbered_episode(name)
            if number is not None and season_number is not None:
                yield season_number, (number,), path


# --- the layout ----------------------------------------------------------------

def _move(source: str, target: str, skips) -> bool:
    """One rename, refused - and recorded - where the target is taken."""
    if os.path.lexists(target):
        if os.path.normcase(source) != os.path.normcase(target):
            skips.record(source, target)
            return False
    try:
        os.makedirs(os.path.dirname(target), exist_ok=True)
        os.rename(source, target)
    except OSError:
        return False
    return True


def _merge_folder(source: str, target: str, skips) -> None:
    """``source``'s contents moved into ``target``, which is there already;
    a name taken in it is left behind and recorded."""
    for name in movies._names_in(source):
        from_path = os.path.join(source, name)
        to_path = os.path.join(target, name)
        if os.path.isdir(from_path) and os.path.isdir(to_path):
            _merge_folder(from_path, to_path, skips)
        else:
            _move(from_path, to_path, skips)
    try:
        os.rmdir(source)
    except OSError:
        pass


def _rename_folder(source: str, target: str, skips) -> str:
    """A folder renamed, or merged into the one of that name already there.
    Answers where its contents now are."""
    if os.path.normcase(os.path.abspath(source)) \
            == os.path.normcase(os.path.abspath(target)):
        if source != target:
            # Only the case differs: two steps, or the system takes it for
            # one name.
            step = target + ".renaming"
            if _move(source, step, skips):
                _move(step, target, skips)
        return target
    if os.path.isdir(target):
        _merge_folder(source, target, skips)
        return target
    return target if _move(source, target, skips) else source


def loose_episodes_into_shows(root: str, skips) -> int:
    """A loose episode file directly under a library folder put in a folder
    for its show, named for what its name says the show is. Answers how many
    were moved."""
    if is_one_show(root):
        return 0
    moved = 0
    for name in sorted(movies._names_in(root)):
        path = os.path.join(root, name)
        if not os.path.isfile(path) or not seriesfolders.is_video(name) \
                or not episodes.has_episode_token(name):
            continue
        title, year = show_title_of(name)
        if not title:
            continue
        target = os.path.join(root, show_folder_name(title, year), name)
        if _move(path, target, skips):
            moved += 1
    return moved


def season_packs_into_shows(root: str, skips) -> int:
    """A folder directly under a library that is one season of a show - a
    season pack, "Show.S02.1080p.BluRay" - made that show's "Season 02"
    inside a folder for the show. Answers how many were moved."""
    if is_one_show(root):
        return 0
    moved = 0
    for name in sorted(movies._names_in(root)):
        path = os.path.join(root, name)
        if not os.path.isdir(path) or plexnames.is_bonus_folder_name(name):
            continue
        if seriesfolders.season_folders(path):
            continue
        match = episodes.NAMES_SEASON.search(name)
        if match is None or match.start() == 0 \
                or not seriesfolders.videos_in(path):
            continue
        title, year = show_title_of(name)
        if not title:
            continue
        season = int(match.group(1))
        target = os.path.join(root, show_folder_name(title, year),
                              seriesnames.season_folder_name(season))
        os.makedirs(os.path.dirname(target), exist_ok=True)
        _rename_folder(path, target, skips)
        moved += 1
    return moved


def season_folders_named(show: str, skips) -> int:
    """Every season folder of a show named ``Season NN`` - "S1", "Staffel
    2", "Series 3", "Specials" - merged where two name one season. Answers
    how many were renamed."""
    renamed = 0
    for name in sorted(movies._names_in(show)):
        path = os.path.join(show, name)
        if not os.path.isdir(path):
            continue
        season = episodes.season_of_folder(name)
        if season is None:
            match = episodes.NAMES_SEASON.search(name)
            if match is None or plexnames.is_bonus_folder_name(name) \
                    or not seriesfolders.videos_in(path):
                continue
            season = int(match.group(1))
        wanted = seriesnames.season_folder_name(season)
        if name != wanted:
            _rename_folder(path, os.path.join(show, wanted), skips)
            renamed += 1
    return renamed


def episodes_into_seasons(show: str, skips) -> int:
    """Every loose episode of a show - and its sidecars - moved into the
    season folder its token names; a special (season 0) into Season 00.
    Answers how many were moved."""
    moved = 0
    for name in sorted(movies._names_in(show)):
        path = os.path.join(show, name)
        if not os.path.isfile(path) or not seriesfolders.is_video(name):
            continue
        found = episodes.episodes_of(name)
        if not found:
            continue
        folder = os.path.join(show, seriesnames.season_folder_name(found[0][0]))
        stem = os.path.splitext(name)[0]
        for other in sorted(movies._names_in(show)):
            if other == name or not other.startswith(stem) \
                    or not os.path.isfile(os.path.join(show, other)):
                continue
            if other[len(stem):len(stem) + 1] in (".", " ", "-"):
                _move(os.path.join(show, other), os.path.join(folder, other),
                      skips)
        if _move(path, os.path.join(folder, name), skips):
            moved += 1
    return moved


class EpisodeExtra(NamedTuple):
    """An extra that is one episode's own: where it is, the episode it is
    of, and its category."""

    path: str
    episode: str
    category: str


def _episode_for(season_folder: str, found: list) -> str:
    """The episode video in ``season_folder`` holding the first of
    ``found``'s (season, episode)."""
    for name in sorted(movies._names_in(season_folder)):
        if not _is_episode_video(name) and not seriesfolders.is_video(name):
            continue
        if seriesnames.is_inline_extra(name):
            continue
        theirs = episodes.episodes_of(name)
        if theirs and theirs[0] == found[0] \
                and movies.bonus_category_for(
                    episodes.EPISODE.sub(" ", name)) == "":
            return os.path.join(season_folder, name)
    return ""


def extras_sorted(show: str, skips) -> int:
    """A show's extras put where the servers read them.

    At series and season level, a folder of extras ("Extras", "Bonus",
    "Featurettes") is sorted into the category folders by the movie ingest's
    keywords, what no keyword places staying in "Featurettes". An extra that
    names an episode - "S01E02 Deleted Scene" - is that episode's own, and is
    put beside it named after it, ``<episode>-deleted.mkv``. A video in a
    season folder that carries an episode token AND a bonus keyword, beside
    the episode itself, is the same. Answers how many were moved.
    """
    moved = 0
    levels = [show] + [path for _season, path
                       in seriesfolders.season_folders(show)]
    for level in levels:
        for name in sorted(movies._names_in(level)):
            folder = os.path.join(level, name)
            if not os.path.isdir(folder):
                continue
            folded = enums.shell_lower(name)
            if not (folded.endswith(("extras", "bonus", "featurettes"))
                    or plexnames.is_bonus_folder_name(name)):
                continue
            for parent, _dirs, files in os.walk(folder):
                for file in sorted(files):
                    if not seriesfolders.is_video(file):
                        continue
                    path = os.path.join(parent, file)
                    moved += _place_extra(show, level, path, file, skips)
        if level != show:
            for name in sorted(movies._names_in(level)):
                path = os.path.join(level, name)
                if not os.path.isfile(path) or not seriesfolders.is_video(name) \
                        or seriesnames.is_inline_extra(name):
                    continue
                found = episodes.episodes_of(name)
                category = movies.bonus_category_for(
                    episodes.EPISODE.sub(" ", os.path.splitext(name)[0]))
                if not found or not category:
                    continue
                episode = _episode_for(level, found)
                if episode and episode != path:
                    moved += _inline(path, episode, category, skips)
    movies._remove_empty_below(show)
    return moved


def _place_extra(show: str, level: str, path: str, name: str, skips) -> int:
    found = episodes.episodes_of(name)
    category = movies.bonus_category_for(name) or "Featurettes"
    if found:
        season_folder = os.path.join(
            show, seriesnames.season_folder_name(found[0][0]))
        episode = _episode_for(season_folder, found) \
            if os.path.isdir(season_folder) else ""
        if episode:
            return _inline(path, episode, category, skips)
    target = safety.unique_suffix_path(os.path.join(level, category, name))
    if os.path.dirname(path) == os.path.dirname(target):
        return 0
    return 1 if _move(path, target, skips) else 0


def _inline(path: str, episode: str, category: str, skips) -> int:
    """An episode's own extra put beside it, named after it."""
    stem = os.path.splitext(os.path.basename(episode))[0]
    folder = os.path.dirname(episode)
    number = 1
    while True:
        name = seriesnames.inline_extra_name(
            stem, category, number,
            "." + enums.lower_extension_of(path))
        if not os.path.lexists(os.path.join(folder, name)):
            break
        number += 1
    return 1 if _move(path, os.path.join(folder, name), skips) else 0


def subtitles_beside(show: str, skips) -> int:
    """Every subtitle a release kept in a "Subs" folder - per episode, or
    named for one - moved beside its episode as ``<episode>.<xx>.srt``.
    Answers how many were moved."""
    moved = 0
    for parent, dirs, files in os.walk(show):
        dirs[:] = [name for name in dirs
                   if not plexnames.is_bonus_folder_name(name)]
        if "subs" not in enums.shell_lower(parent):
            continue
        for file in sorted(files):
            if enums.lower_extension_of(file) not in ("srt", "ass", "ssa",
                                                      "sub", "idx", "vtt"):
                continue
            path = os.path.join(parent, file)
            relative = os.path.relpath(path, show)
            found = episodes.episodes_of(relative)
            if not found:
                continue
            season_folder = os.path.join(
                show, seriesnames.season_folder_name(found[0][0]))
            episode = _episode_for(season_folder, found) \
                if os.path.isdir(season_folder) else ""
            if not episode:
                continue
            code = _subtitle_language(file)
            if not code:
                continue
            stem = os.path.splitext(episode)[0]
            role = ("forced" if languages.is_forced_name(file)
                    else "sdh" if languages.is_sdh_name(file) else "full")
            target = subtitletracks.sidecar_name(stem, code, role)
            if enums.lower_extension_of(file) != "srt":
                target = target[:-len("srt")] + enums.lower_extension_of(file)
            if _move(path, target, skips):
                moved += 1
    movies._remove_empty_below(show)
    return moved


def _subtitle_language(name: str) -> str:
    """The language a subtitle's file name says it is in, as code2."""
    stem = os.path.splitext(name)[0]
    for piece in reversed(re.split(r"[ ._\-\[\]()]+", stem)):
        if not piece:
            continue
        code = languages.code_from_tag(piece) if len(piece) in (2, 3) else ""
        if code:
            return code
        code = languages.code_from_name(piece)
        if code:
            return code
    return ""


# --- the tagging ---------------------------------------------------------------

class Rename(NamedTuple):
    """One rename the tagging makes: a file, or a folder."""

    source: str
    target: str


def episode_numbers(path: str, show: seriesidentify.Show | None) \
        -> tuple[int, tuple] | None:
    """(season, episode numbers) of one episode file: its token, or a date
    the catalogue aired one on, or "Episode N" in its season folder."""
    name = os.path.basename(path)
    found = episodes.episodes_of(name)
    if found:
        return found[0][0], tuple(number for _season, number in found)
    if show is not None:
        day = episodes.date_of(name)
        if day is not None:
            dated = show.by_date(day.isoformat())
            if dated is not None:
                return dated[0], (dated[1],)
    season = episodes.season_of_folder(os.path.basename(os.path.dirname(path)))
    number = episodes.numbered_episode(name)
    if season is not None and number is not None:
        return season, (number,)
    return None


def plan_renames(show_path: str, show: seriesidentify.Show,
                 naming=None, spelling: str = "") -> list[Rename]:
    """Every rename that names a show folder for the catalogue's show: the
    episodes (with their sidecars) first, then the season folders, then the
    series folder itself - each episode named after the catalogue's title of
    its first episode. ``spelling`` is the title to write, where the
    folder's own spelling of the show is one the catalogue knows."""
    naming = naming or seriesnames.style()
    provider, number = show.tag_provider()
    tag = plexnames.id_tag(provider, number)
    title = spelling or show.title
    base = seriesnames.safe_title(title) + (" (%s)" % show.year
                                            if show.year else "")
    renames: list[Rename] = []
    for _season, folder in [(None, show_path)] \
            + seriesfolders.season_folders(show_path):
        names = sorted(movies._names_in(folder))
        for name in names:
            path = os.path.join(folder, name)
            # A converted copy is an episode of its own, a second version; a
            # kept original follows its episode as a sidecar.
            old_copy = (plexnames.is_kept_copy(name)
                        and not plexnames.is_converted_copy(name))
            if not os.path.isfile(path) or not seriesfolders.is_video(name) \
                    or old_copy or seriesnames.is_inline_extra(name):
                continue
            numbers = episode_numbers(path, show)
            if numbers is None:
                continue
            season, eps = numbers
            read = seriesnames.read_episode_name(name)
            version = read.version if read else ""
            part = read.part if read else ""
            episode = show.episode(season, eps[0])
            episode_title = episode.title if episode else ""
            stem, extension = os.path.splitext(name)
            wanted = seriesnames.episode_name(
                base, season, eps, episode_title, extension.lower(), part,
                version, naming)
            wanted_stem = os.path.splitext(wanted)[0]
            if wanted != name:
                renames.append(Rename(path, os.path.join(folder, wanted)))
                renames.extend(_sidecar_renames(folder, names, stem,
                                                wanted_stem, name))
    for season, folder in seriesfolders.season_folders(show_path):
        wanted = seriesnames.season_folder_name(season)
        if os.path.basename(folder) != wanted:
            renames.append(Rename(folder, os.path.join(show_path, wanted)))
    folder_name = plexnames.folder_name(base, tag)
    if os.path.basename(show_path.rstrip("/\\")) != folder_name:
        renames.append(Rename(show_path, os.path.join(
            os.path.dirname(show_path.rstrip("/\\")), folder_name)))
    return renames


def _sidecar_renames(folder: str, names: list, stem: str, wanted: str,
                     own: str) -> list[Rename]:
    """The files that follow an episode's name: its subtitles and
    transcripts (``<stem>.…``), its kept original (``<stem> - old.mkv``) and
    its own extras (``<stem>-deleted.mkv``)."""
    found = []
    for other in names:
        if other == own or not other.startswith(stem):
            continue
        rest = other[len(stem):]
        kept = rest.lower().startswith(
            tuple(suffix.lower() for suffix in plexnames.OLD_SUFFIXES))
        extra = rest.startswith("-") and seriesnames.is_inline_extra(other)
        if rest.startswith(".") or kept or extra:
            found.append(Rename(os.path.join(folder, other),
                                os.path.join(folder, wanted + rest)))
    return found


def carry_out(renames: list[Rename], skips, log=log) -> int:
    """The renames made, in order; a target already there is refused and
    recorded - a folder there already is merged into. Answers how many were
    made."""
    done = 0
    moved: dict = {}
    for rename in renames:
        source = moved.get(rename.source, rename.source)
        if os.path.isdir(source):
            target = _rename_folder(source, rename.target, skips)
            if target != source:
                moved[rename.source] = target
                done += 1
                log("  %s -> %s" % (os.path.basename(source),
                                    os.path.basename(target)))
            continue
        if _move(source, rename.target, skips):
            done += 1
            log("  %s -> %s" % (os.path.basename(source),
                                os.path.basename(rename.target)))
    return done


def duplicate_targets(renames: list[Rename]) -> list[tuple[str, list]]:
    """The targets two files would both be renamed to: one episode kept
    twice, which the tagging leaves for someone to choose between."""
    seen: dict = {}
    for rename in renames:
        seen.setdefault(os.path.normcase(rename.target), []).append(
            rename.source)
    return [(target, sources) for target, sources in seen.items()
            if len(sources) > 1]


def order_hint(show: seriesidentify.Show) -> str:
    """What someone has to set on the server for a show the catalogue numbers
    in an order other than TMDb's aired one - "" where nothing."""
    if show.source == "tmdb" or show.order in ("aired", "default", "official"):
        return ""
    order = seriesidentify.ORDER_NAMES.get(show.order, show.order)
    return ("set the show's episode ordering to TheTVDB %s (Plex: the show's "
            "Episode ordering; Jellyfin: the TheTVDB plugin)" % order)


def id_tag_for(text: str) -> tuple[str, str]:
    """A hand-written id as (provider, number): a bare number is TMDb's,
    "tvdb-81189" TheTVDB's, "tt…" IMDb's - ("", "") for anything else."""
    text = text.strip().strip("{}[]()")
    match = re.fullmatch(r"(?:(tmdb|tmdbid|tvdb|tvdbid|imdb|imdbid)-)?"
                         r"(tt[0-9]+|[0-9]+)", text, re.I)
    if not match:
        return "", ""
    number = match.group(2)
    if number.lower().startswith("tt"):
        return "imdb", number.lower()
    provider = (match.group(1) or "tmdb").lower()[:4]
    if provider == "imdb":
        return "", ""
    return provider, number


def read_id_list(path: str, blank: list | None = None) -> dict | None:
    """The filled-in unmatched list as {folder name: (provider, number)}, or
    None when it cannot be read."""
    try:
        with open(path, encoding="utf-8", errors="surrogateescape") as handle:
            lines = handle.read().splitlines()
    except OSError:
        return None
    found: dict = {}
    for line in lines:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        name, _tab, rest = line.partition("\t")
        written = rest.partition("\t")[0].partition("#")[0]
        parsed = id_tag_for(written)
        if not name.strip():
            continue
        if parsed[0]:
            found[name.strip()] = parsed
        elif blank is not None:
            blank.append(name.strip())
    return found


def write_id_list(path: str, names: list, ids: dict | None = None,
                  program: str = "ingest-series") -> bool:
    """The unmatched list: the ids filled in, then the shows still blank."""
    lines = [
        "# Shows neither TMDb nor TheTVDB could identify on their own.",
        "# Put the id after the tab - a TMDb number, tvdb-<number> or an",
        "# IMDb id (tt...) - and run the tagging again with",
        "#   %s -O tagging -i <this file> <library>" % program,
        "# adding -w once the dry run reads right. Lines still blank are",
        "# simply the ones still to do.",
    ]
    for name, (provider, number) in sorted((ids or {}).items()):
        lines.append("%s\t%s" % (name, number if provider == "tmdb"
                                 else "%s-%s" % (provider, number)
                                 if provider == "tvdb" else number))
    for name in names:
        if name not in (ids or {}):
            lines.append(name + "\t")
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")
    except OSError:
        return False
    return True


def write_lines(path: str, header: str, lines: list) -> bool:
    """A worklist: its header, then a line per entry. No entries removes
    the one an earlier run left."""
    if not lines:
        try:
            os.remove(path)
        except OSError:
            pass
        return False
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("# " + header + "\n")
            handle.write("\n".join(lines) + "\n")
    except OSError:
        return False
    return True


if __name__ == "__main__":
    from medialib.cli import ingest_series_run
    raise SystemExit(ingest_series_run.main(
        sys.argv[1:], commands.program_name(ingest_series_run.__name__),
        commands.script_dir()))
