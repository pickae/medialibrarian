"""TMDb-backed IMDb id lookup, and Plex/Jellyfin id tagging.

Ask an external API what a film is, and only rename when the answer is certain.

The port keeps the module's two external tools, ``iconv`` and ``curl``, as the
single source of truth for what they do: ``normalize_title`` runs the host's
``iconv -f UTF-8 -t ASCII//TRANSLIT`` (whose substitution table and its
exit-code quirks are glibc- and locale-specific, not something to re-derive in
Python) and the id lookup shells out to ``curl`` exactly the way the bash does.
What is rewritten is the glue - the JSON navigation (``json`` instead of
``jq``), the certainty rule, and the folder rename - which is where a port can
drift from the original.
"""

import json
import os
import re
import subprocess
import time
import unicodedata
from collections.abc import Callable

from medialib.lib import plexnames, safety
from medialib.lib.enums import shell_lower

# The TMDb endpoint everything is relative to.
_BASE = "https://api.themoviedb.org/3"

# A movie folder is "Title (Year)" where the Year is 1xxx or 2xxx. The greedy
# title takes everything up to the LAST "(Year)", so "Batman (1999) (2005)"
# reads as title "Batman (1999)", year "2005".
_YEAR_RE = re.compile(r"^(.+) \(([12][0-9]{3})\)$")


def normalize_title(title: str) -> str:
    """Fold a title to a comparison key, the way ``normalizeTitle`` does.

    Transliterate accents to ASCII (``iconv``), lower case, collapse every
    non-alphanumeric run to a single space, and trim. Only ever used to compare
    titles, never to rename - the on-disk name stays verbatim.

    The transliteration is delegated to the host's ``iconv`` rather than
    re-implemented, because its table and its exit-code rule (a handful of
    codepoints transliterate AND fail, which resets the string to the original)
    are a property of glibc and the active locale, not of the title.

    Only where the host's iconv is glibc's, though - see
    :func:`_iconv_drops_accents`. The other widespread implementation, GNU
    libiconv, is what macOS and MSYS ship, and it SPELLS an accent out rather
    than dropping it: "Amélie" comes back "Am'elie", which folds on to
    "am elie" and no longer matches the "amelie" the same film's ASCII
    spelling gives. Those hosts, and a host with no iconv at all, fold in
    Python instead (:func:`_fold_without_iconv`).
    """
    folded = None
    if _iconv_drops_accents():
        # No guard on the start, unlike everywhere else a tool is optional: the
        # probe above only answers true by having RUN iconv, so reaching here
        # means it was there a moment ago.
        proc = subprocess.run(
            ["iconv", "-f", "UTF-8", "-t", "ASCII//TRANSLIT"],
            input=title.encode("utf-8"), capture_output=True)
        if proc.returncode != 0:
            # iconv gave up somewhere: the original stands, exactly as the
            # shell's `|| s="$1"` does.
            folded = title
        else:
            folded = proc.stdout.decode("utf-8", "replace")
    if folded is None:
        folded = _fold_without_iconv(title)
    folded = shell_lower(folded)
    folded = re.sub(r"[^a-z0-9]+", " ", folded)
    return folded.strip(" ")


# One accented letter and what a glibc iconv makes of it. The probe is a single
# letter on purpose: what is being told apart is the two tables' rule for a
# base-plus-accent, which is the case every title in a Latin-script library
# turns on.
_TRANSLIT_PROBE = "é"          # e with acute
_TRANSLIT_PROBE_GLIBC = "e"

# The answer to that probe, worked out once. A list rather than a global name
# so the reset below can empty it without a `global` statement, the way the
# other modules here keep their per-run state.
_ICONV_FLAVOUR: list[bool] = []


def reset_iconv_flavour() -> None:
    """Forget what the host's iconv was found to do, so the next fold asks
    again. For the cases that stand a different iconv on PATH."""
    _ICONV_FLAVOUR.clear()


def _iconv_drops_accents() -> bool:
    """Whether this host's ``iconv`` transliterates the way the recorded
    behaviour expects: an accent DROPPED, "é" to "e".

    False for GNU libiconv, which answers "'e", and false when there is no
    iconv to ask. Asked once per process and remembered: a lookup over a movie
    library calls the fold once per folder, and the answer cannot change under
    a running command.
    """
    if not _ICONV_FLAVOUR:
        _ICONV_FLAVOUR.append(_probe_iconv())
    return _ICONV_FLAVOUR[0]


def _probe_iconv() -> bool:
    try:
        proc = subprocess.run(
            ["iconv", "-f", "UTF-8", "-t", "ASCII//TRANSLIT"],
            input=_TRANSLIT_PROBE.encode("utf-8"), capture_output=True)
    except OSError:
        return False
    if proc.returncode != 0:
        return False
    return proc.stdout.decode("utf-8", "replace") == _TRANSLIT_PROBE_GLIBC


# The letters a decomposition cannot reach, with what glibc's table writes for
# each: they are single codepoints with no combining form, so NFKD leaves them
# whole and the ASCII pass would drop them altogether. Kept to the Latin
# alphabet's own oddities, which is what a film title in this library holds.
_LIGATURES = {
    "Æ": "AE", "æ": "ae",      # AE
    "Œ": "OE", "œ": "oe",      # OE
    "ß": "ss",                      # sharp s
    "Ø": "O", "ø": "o",        # O with stroke
    "Ð": "D", "ð": "d",        # eth
    "Đ": "D", "đ": "d",        # D with stroke
    "Ł": "L", "ł": "l",        # L with stroke
    "Þ": "TH", "þ": "th",      # thorn
    "Ħ": "H", "ħ": "h",        # H with stroke
    "Ŧ": "T", "ŧ": "t",        # T with stroke
}


def _fold_without_iconv(title: str) -> str:
    """The transliteration in Python: for a host whose iconv spells accents out
    rather than dropping them, and for one that has no iconv at all.

    Decompose, drop the combining marks, spell out the ligatures a
    decomposition cannot reach, and keep what is left of ASCII. That
    reproduces glibc's answer for the Latin script - which is what makes this
    a fallback and not a second behaviour: the same library folds to the same
    keys whichever host reads it.

    Where it still differs is a script glibc has an opinion about and Unicode
    does not - Cyrillic and Greek come back as "?" there and as nothing here.
    Both are degenerate keys either way, and both sides of every comparison
    are folded by this same function, so a title in one of those scripts
    matches or fails to match as a whole rather than half-folding.
    """
    spelled = "".join(_LIGATURES.get(character, character)
                      for character in title)
    decomposed = unicodedata.normalize("NFKD", spelled)
    kept = "".join(c for c in decomposed if not unicodedata.combining(c))
    return kept.encode("ascii", "ignore").decode("ascii")


# What TMDb asks of a caller: about ten requests a second. One film costs
# several - the search, an alternative-titles call per candidate, and the
# external-ids call - so a library goes through them far faster than a person
# would, and the limit is worth keeping to rather than finding out where the
# service draws it.
MAX_REQUESTS_PER_SECOND = 10.0

# The moment the last request went out, so the next can wait out the remainder
# of its slot. A list rather than a global name, the way the iconv answer above
# is kept, so it can be reset without a `global` statement.
_LAST_REQUEST: list[float] = []


def reset_rate_limit() -> None:
    """Forget when the last request went out, so the next one goes immediately.
    For a test that would otherwise pay the interval."""
    _LAST_REQUEST.clear()


def _wait_for_a_slot() -> None:
    """Hold the next request back until its slot comes round.

    A plain minimum interval rather than a rolling window: the calls here are
    sequential, so spacing each one evenly IS the rate, and it needs no history
    to be kept.
    """
    interval = 1.0 / MAX_REQUESTS_PER_SECOND
    now = time.monotonic()
    if _LAST_REQUEST:
        waiting = _LAST_REQUEST[0] + interval - now
        if waiting > 0:
            time.sleep(waiting)
            now = time.monotonic()
    _LAST_REQUEST[:] = [now]


# The escapes a curl config file's double-quoted value has, and all it has.
_CONFIG_ESCAPES = (("\\", "\\\\"), ('"', '\\"'), ("\t", "\\t"),
                   ("\n", "\\n"), ("\r", "\\r"), ("\v", "\\v"))


def _config_value(text: str) -> str:
    """One value as a curl config file spells it: double-quoted, with the six
    escapes that syntax has applied."""
    for character, escape in _CONFIG_ESCAPES:
        text = text.replace(character, escape)
    return '"' + text + '"'


def _curl(url: str, params) -> str | None:
    """One ``curl -fsSG`` call, the module's only way out to the network.

    ``params`` is an ordered (key, value) pair list, each becoming a
    ``--data-urlencode key=value``. Returns the body on success, or ``None``
    when curl fails (the ``-f`` makes an HTTP error a non-zero exit, which is
    how the caller tells "no answer" from an answer).

    Paced to :data:`MAX_REQUESTS_PER_SECOND` before it goes out, which is where
    the whole module's traffic is metered: every question reaching TMDb is one
    of these calls.

    Handed to curl through ``--config -`` rather than as argv, because one of
    those params is the API key and argv is not private: /proc/<pid>/cmdline is
    readable by every account on the machine, and a run over a library opens
    that window once per candidate folder. On stdin it reaches curl and nothing
    else.
    """
    _wait_for_a_slot()
    lines = ["url = " + _config_value(url), "get", "fail", "silent",
             "show-error"]
    for key, value in params:
        lines.append("data-urlencode = "
                     + _config_value("{}={}".format(key, value)))
    proc = subprocess.run(["curl", "--config", "-"],
                          input="\n".join(lines).encode("utf-8"),
                          stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    if proc.returncode != 0:
        return None
    return proc.stdout.decode("utf-8", "replace")


def _as_json(text: str | None):
    """A TMDb body as a dict, or an empty dict when it is not valid JSON.

    The bash pipes every body through ``jq`` with a ``?``/``//`` guard, which
    reads a non-object as "nothing"; an empty dict is the same here.
    """
    if not text:
        return {}
    try:
        parsed = json.loads(text)
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def tmdb_imdb_id(title: str, year: str) -> str:
    """The IMDb id (``ttXXXXXXX``) of a film, ONLY when the match is unambiguous.

    Returns "" (never the id) when it is not certain enough to rename on, so a
    caller can gate on an empty result and leave the film untouched. Certainty
    rule: among the search results released in the requested year, exactly one
    may carry the wanted title on its primary, original OR any translated/
    alternative title. Zero or several such candidates -> no output.
    """
    api_key = os.environ.get("tmdbApiKey", "")
    if not api_key:
        return ""
    want = normalize_title(title)
    if not want:
        return ""

    search = _curl(_BASE + "/search/movie", [
        ("api_key", api_key), ("query", title), ("year", year),
        ("include_adult", "false")])
    if search is None:
        return ""
    results = _as_json(search).get("results")
    if not isinstance(results, list):
        results = []

    # 2. the candidates released in the requested year, in result order.
    ids = []
    for row in results:
        if not isinstance(row, dict):
            continue
        release = row.get("release_date")
        release = release if isinstance(release, str) else ""
        if release[:4] == year:
            ids.append(row.get("id"))
    if not ids:
        return ""

    # 3. of those, how many carry the wanted title (primary, original or any
    #    alternative). A missing primary/original is the literal word "null"
    #    the way `jq -r` prints it, so it is compared (and missed) like any
    #    other title, not skipped.
    match_count = 0
    match_id = None
    for candidate in ids:
        titles = []
        for row in results:
            if isinstance(row, dict) and row.get("id") == candidate:
                primary = row.get("title")
                titles.append("null" if primary is None else primary)
                original = row.get("original_title")
                titles.append("null" if original is None else original)
        alt = _curl(_BASE + "/movie/{}/alternative_titles".format(
                    _id_token(candidate)),
                    [("api_key", api_key)])
        if alt:
            for entry in _as_json(alt).get("titles") or []:
                if isinstance(entry, dict) and entry.get("title") is not None:
                    # `.title // empty`: a null/missing alternative is skipped,
                    # unlike the search titles above.
                    titles.append(entry.get("title"))
        for t in titles:
            if t == "":
                continue
            if normalize_title(str(t)) == want:
                match_count += 1
                match_id = candidate
                break
    if match_count != 1:
        return ""

    # 4. resolve that one match's IMDb id; only a "tt..." string is a tag.
    ext = _curl(_BASE + "/movie/{}/external_ids".format(_id_token(match_id)),
                [("api_key", api_key)])
    if ext is None:
        return ""
    imdb = _as_json(ext).get("imdb_id")
    if not (isinstance(imdb, str) and imdb.startswith("tt")):
        return ""
    return imdb


def _id_token(value):
    """The id the way ``jq -r`` spells it in a URL: a missing id is the literal
    word "null", not nothing - the bash builds its path from what jq printed.

    Digits or that word, and nothing else. The value comes out of the API's own
    JSON and is spliced into a URL PATH, where a "/" or a "#" in it asks for
    something other than what this meant to ask for; a TMDb id that is not a
    number is not an id.
    """
    if value is None:
        return "null"
    text = str(value)
    return text if text.isdigit() else "null"


def _folder_spelling(directory: str, name: str) -> str:
    """The way ``find`` spells a child of ``directory``: a bare "." writes
    "./name", a trailing slash is not doubled, anything else joins with one
    slash."""
    if directory == ".":
        return "./" + name
    if directory.endswith("/"):
        return directory + name
    return directory + "/" + name


# The id list's own spelling: one film per line, its folder name and its id
# separated by a tab, "#" comments and blank lines ignored. A tab because a
# film's name is full of everything else - spaces, brackets, dashes, colons -
# and is the one character a folder name cannot contain.
_ID_COMMENT = "#"

_ID_FORMS = re.compile(r"^\{?(?:(imdb)-)?(tt[0-9]+)\}?$|^\{?(?:(tmdb)-)?([0-9]+)\}?$",
                       re.I)


def id_tag_for(text: str) -> str:
    """The tag a hand-written id becomes, or "" when it is not one.

    Written the way someone has it to hand: "tt0000001", "imdb-tt0000001",
    "{imdb-tt0000001}", a bare TMDb number, or "tmdb-12345". An IMDb id is a
    "tt" and digits; anything else that is only digits is TMDb's.
    """
    match = _ID_FORMS.match(text.strip())
    if not match:
        return ""
    if match.group(2):
        return "{imdb-" + match.group(2).lower() + "}"
    return "{tmdb-" + match.group(4) + "}"


def read_id_list(path: str) -> dict:
    """A hand-filled id list as {folder name: tag}.

    A line with no id yet is skipped rather than refused - the file is meant to
    be filled in over several sittings, and the ones still blank are simply the
    ones still to do.
    """
    found: dict[str, str] = {}
    try:
        with open(path, encoding="utf-8", errors="surrogateescape") as handle:
            lines = handle.read().splitlines()
    except OSError:
        return found
    for line in lines:
        if not line.strip() or line.lstrip().startswith(_ID_COMMENT):
            continue
        name, _tab, written = line.partition("\t")
        tag = id_tag_for(written)
        if name.strip() and tag:
            found[name.strip()] = tag
    return found


def write_id_list(path: str, names, ids: dict | None = None,
                  log: Callable[[str], None] | None = None) -> bool:
    """The id list as it stands: what has been filled in, then what has not.

    ``ids`` is written back verbatim. It is the file's whole point - the ids
    someone looked up by hand are the only record of them anywhere, and a film
    named from one still answers "identified" on the next run only because the
    line is still there. ``names`` are the ones still to do, each left blank.

    Rewritten whole rather than appended to, so a film that has since been
    identified by TMDb itself drops off the list instead of lingering on it.
    """
    lines = [
        _ID_COMMENT + " Films TheMovieDB could not identify on its own.",
        _ID_COMMENT + " Put the id after the tab - tt0000001, imdb-tt0000001",
        _ID_COMMENT + " or a bare TMDb number - and run the tagging again with",
        _ID_COMMENT + "   ingest-movies -t -i <this file> <library>",
        _ID_COMMENT + " adding -w once the dry run reads right. Lines still",
        _ID_COMMENT + " blank are simply the ones still to do.",
        "",
    ]
    lines += [name + "\t" + tag for name, tag in sorted((ids or {}).items())]
    lines += [name + "\t" for name in names]
    try:
        with open(path, "w", encoding="utf-8",
                  errors="surrogateescape") as handle:
            handle.write("\n".join(lines) + "\n")
    except OSError as error:
        if log is not None:
            log("WARNING: could not write the id list %s: %s" % (path, error))
        return False
    return True


def tag_plex_ids(directory: str, log: Callable[[str], None],
                 skip_log: safety.SkipLog | None = None,
                 dry_run: bool = False, ids: dict | None = None,
                 unmatched: list | None = None) -> int:
    """Name each confidently-matched movie folder, its films and their sidecars
    the way Plex reads them: the folder and every file carry the id tag, an
    edition carries its own, and a split film keeps its stacking token last.

    Renaming only, each name beside what it already was - the files inside the
    folder first, then the folder itself - so nothing is copied anywhere.

    The on-disk name is kept verbatim; only the tags are added. A folder that
    already carries an "{imdb-...}" or "{tmdb-...}" tag is not looked up again,
    but its files ARE brought into line with it, without a second ask of the
    network. The "<film> (old).mkv" an improved remux keeps is never renamed: it
    is the film as it arrived, and tagging it would offer Plex a second edition
    of every film that has one.

    ``dry_run`` asks TMDb the same questions and works out the same names, then
    prints each rename instead of doing it: the name is the thing being
    previewed, so the lookup still happens.

    ``ids`` is a hand-written {folder name: tag} that answers BEFORE the network
    is asked, for the films TMDb cannot identify on its own. ``unmatched``, when
    a list is passed, collects the folder names that neither could name - which
    is what the list to fill in is written from.
    """
    skip_log = skip_log if skip_log is not None else safety.SkipLog()
    if not os.environ.get("tmdbApiKey", ""):
        log("WARNING: tmdbApiKey not set, skipping IMDb id tagging")
        return 0

    # Immediate subdirectories only, in the filesystem's own order - the same
    # order the shell's `find . -maxdepth 1 -mindepth 1 -type d` walks, so the
    # "match" lines they each log come out in the same sequence.
    for folder in os.scandir(directory):
        if not folder.is_dir(follow_symlinks=False):
            continue
        base, tag = plexnames.untagged_base(folder.name)
        match = _YEAR_RE.match(base)
        if not match:
            continue
        asked = not tag
        by_hand = False
        if asked:
            # The hand-written id first: a film someone has already looked up is
            # not worth asking an API that has already failed to name it.
            tag = (ids or {}).get(base, "")
            by_hand = bool(tag)
            if not tag:
                imdb = tmdb_imdb_id(match.group(1), match.group(2))
                tag = "{imdb-" + imdb + "}" if imdb else ""
            if not tag and unmatched is not None:
                unmatched.append(base)

        names = [entry.name for entry in os.scandir(folder.path)
                 if entry.is_file(follow_symlinks=False)]
        # The folder's own name is settled BEFORE a single file is touched:
        # renaming the files under a folder that cannot take its name leaves
        # them spelled for a folder they are not in, beside the folder they are
        # spelled for.
        wanted = plexnames.folder_name(base, tag)
        target = _folder_spelling(directory, wanted)
        if wanted != folder.name and os.path.exists(target):
            skip_log.record(folder.path, target)
            log('  "{}" is already there, left "{}" untouched'
                .format(wanted, folder.name))
            continue

        # Said before the renames it explains, so a dry run reads as a film and
        # then what would happen to it. Only a folder this run ASKED about says
        # anything: one already tagged would repeat its line for the rest of
        # the library's life.
        if asked:
            _report(log, base, tag, plexnames.editions_in(base, names),
                    by_hand)
        _rename_in_place(folder, names, base, tag, target, skip_log, dry_run,
                         log)
    return 0


def _report(log: Callable[[str], None], base: str, tag: str,
            editions: list, by_hand: bool = False) -> None:
    """What this folder got, in one line - including the folder that got
    nothing, which is the answer people go looking for."""
    named = ", ".join(editions)
    source = "from the id list" if by_hand else "match"
    if tag and editions:
        log('  {}: "{}" -> {}, editions: {}'.format(source, base, tag, named))
    elif tag:
        log('  {}: "{}" -> {}'.format(source, base, tag))
    elif editions:
        log('  no confident TMDb match: "{}" - editions named ({}), no id'
            .format(base, named))
    else:
        log('  no confident TMDb match: "{}" - left as it is'.format(base))


def _rename_in_place(folder, names: list, base: str, tag: str, target: str,
                     skip_log: safety.SkipLog, dry_run: bool,
                     log: Callable[[str], None]) -> None:
    """One folder's films and sidecars, then the folder itself.

    That order and no other: renaming the folder first would move every path
    underneath it out from under the names just worked out.

    An empty ``tag`` still does the rest of the work: which release a file is
    is on the disk already, whether or not TMDb could say which film they are
    all versions of.
    """
    for old, new in plexnames.folder_renames(base, tag, names):
        _rename(os.path.join(folder.path, old),
                os.path.join(folder.path, new), skip_log, dry_run, log)
    _rename(folder.path, target, skip_log, dry_run, log)


def _rename(source: str, target: str, skip_log: safety.SkipLog,
            dry_run: bool = False,
            log: Callable[[str], None] | None = None) -> None:
    """A rename that refuses to land on something that is already there, or to
    hide what it moves - and that only SAYS what it would do on a dry run."""
    if source == target:
        return
    if safety.would_hide(target) or os.path.exists(target):
        skip_log.record(source, target)
        return
    if dry_run:
        if log is not None:
            log('    would rename: "{}" -> "{}"'.format(
                os.path.basename(source), os.path.basename(target)))
        return
    os.rename(source, target)
