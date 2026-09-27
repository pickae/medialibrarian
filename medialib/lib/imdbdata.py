"""A local copy of IMDb's own title lists, asked where TMDb could not answer.

What `ingest-movies` writes is an IMDb id, and TMDb is only ever a way of
reaching one. IMDb publishes the two lists that question needs - every title,
its type, year and runtime, and every regional title it has been released
under - as files anyone may download:

    https://datasets.imdbws.com/  (title.basics.tsv.gz, title.akas.tsv.gz)
    https://developer.imdb.com/non-commercial-datasets/

**For personal and non-commercial use only**, which is what those terms say
and what this library is. A copy that is going to be used for anything else
needs IMDb's licence, not these files.

Kept under the checkout's ``data/imdb/`` beside what else a run keeps there,
refreshed when it is a month old, and read into a DuckDB database there - the
same engine the census builds its cubes in, driven the same way, so the
library asks for one database program and not two. Its own file rather than
the census's: a census rebuilds its database from nothing on every run.

What the database holds is a way of REACHING a film and nothing more: each
title folded to the squeezed key :mod:`medialib.lib.titlematch` would fold it
to, in the spellings a library writes it in - accents dropped, and spelled
out, with a leading article and without. The certainty rule is the lookup's,
and is asked of what comes back exactly as it is asked of TMDb's answers.
"""

import json
import os
import shutil
import subprocess
import time
from collections.abc import Callable

# Where IMDb publishes the lists.
BASE_URL = "https://datasets.imdbws.com/"
BASICS = "title.basics.tsv.gz"
AKAS = "title.akas.tsv.gz"
DATABASE = "imdb.duckdb"

# The kinds of title a film folder can hold. A series and its episodes are not
# films, and neither is a game; a television film, a special, a direct-to-video
# release and a short are, and they are exactly what TMDb is thinnest on.
KINDS = ("movie", "tvMovie", "tvSpecial", "video", "short")

# How old the lists may get before they are fetched again. IMDb refreshes them
# daily; a film's id does not change, and what a month adds is the films
# released in it.
MAX_AGE_DAYS = 30

# The articles a title may lead with, in the languages the fold knows. Kept in
# step with :data:`medialib.lib.titlematch.ARTICLES` by the tests.
ARTICLES = ("the", "a", "an", "le", "la", "les", "l", "un", "une", "des", "du",
            "de", "der", "die", "das", "den", "dem", "ein", "eine", "einer",
            "einen", "het", "een", "el", "los", "las", "una", "unos", "unas",
            "il", "lo", "gli", "i", "uno", "o", "os", "as")


def directory(script_dir: str) -> str:
    """Where the lists and the database live: the checkout's ``data/imdb``."""
    return os.path.join(script_dir, "data", "imdb")


def prepare(where: str, log: Callable[[str], None]) -> str:
    """The database, fetched and built as far as it needs to be, or "" when
    there is none to be had - which only costs the lookups it would have
    answered, and is said once.

    Nothing here is required. Without DuckDB, or with no copy and no network,
    the tagging runs as it always did.
    """
    if not shutil.which("duckdb"):
        log("WARNING: duckdb not found, so the local IMDb lists are not "
            "asked (https://duckdb.org/docs/installation)")
        return ""
    try:
        os.makedirs(where, exist_ok=True)
    except OSError as error:
        log("WARNING: cannot keep the IMDb lists in %s: %s" % (where, error))
        return ""
    for name in (BASICS, AKAS):
        _fetch(where, name, log)
    lists = [os.path.join(where, name) for name in (BASICS, AKAS)]
    if not all(os.path.isfile(path) for path in lists):
        log("WARNING: the IMDb lists could not be fetched, so they are not "
            "asked")
        return ""
    database = os.path.join(where, DATABASE)
    newest = max(os.path.getmtime(path) for path in lists)
    if not os.path.isfile(database) or os.path.getmtime(database) < newest:
        if not _build(where, database, log):
            return database if os.path.isfile(database) else ""
    return database


def _fetch(where: str, name: str, log: Callable[[str], None]) -> None:
    """One list, fetched when it is missing or a month old - and then only if
    IMDb's copy is newer, which is what ``-z`` asks the server.

    Written beside the old one and moved over it, so a fetch that fails leaves
    the copy there was.
    """
    path = os.path.join(where, name)
    if os.path.isfile(path) and \
            time.time() - os.path.getmtime(path) < MAX_AGE_DAYS * 86400:
        return
    log("Fetching %s from %s" % (name, BASE_URL))
    part = path + ".part"
    argv = ["curl", "-fsS", "-R", "-o", part]
    if os.path.isfile(path):
        argv += ["-z", path]
    done = subprocess.run(argv + [BASE_URL + name])
    if done.returncode != 0:
        log("WARNING: could not fetch %s - keeping the copy there is, if any"
            % name)
    if os.path.isfile(part):
        os.replace(part, path)
    elif os.path.isfile(path) and done.returncode == 0:
        # Not modified: the copy is current, and saying so is a touch.
        os.utime(path)


def _fold(expression: str) -> str:
    """SQL folding a title the way :func:`medialib.lib.titlematch.normalize_title`
    does, near enough to reach it: the letters a transliteration spells out
    spelled out, the accents dropped, lower case, and every run of anything
    else one space."""
    for letter, spelling in (("ß", "ss"), ("ẞ", "SS"), ("æ", "ae"),
                             ("Æ", "AE"), ("œ", "oe"), ("Œ", "OE"),
                             ("ø", "o"), ("Ø", "O"), ("ł", "l"), ("Ł", "L"),
                             ("đ", "d"), ("Đ", "D"), ("ı", "i"), ("&", " and "),
                             ("+", " plus "), ("@", " at ")):
        expression = "replace(%s, '%s', '%s')" % (expression, letter, spelling)
    return ("trim(regexp_replace(lower(strip_accents(%s)), '[^a-z0-9]+', ' ', "
            "'g'))" % expression)


def _spelled_out(expression: str) -> str:
    """SQL writing the umlauts a German keyboard spells out as it spells them,
    the other half of :data:`medialib.lib.titlematch.SPELLED_OUT`."""
    for letter, spelling in (("ä", "ae"), ("Ä", "Ae"), ("ö", "oe"),
                             ("Ö", "Oe"), ("ü", "ue"), ("Ü", "Ue"),
                             ("å", "aa"), ("Å", "Aa"), ("ø", "oe"),
                             ("Ø", "Oe")):
        expression = "replace(%s, '%s', '%s')" % (expression, letter, spelling)
    return expression


def build_sql(basics: str, akas: str) -> str:
    """The whole build, as one script for one DuckDB process."""
    read = ("read_csv('%s', delim = '\\t', header = true, quote = '', "
            "escape = '', nullstr = '\\N', all_varchar = true, "
            "strict_mode = false)")
    kinds = ", ".join("'%s'" % kind for kind in KINDS)
    articles = "|".join(ARTICLES)
    return "\n".join([
        "SET preserve_insertion_order = false;",
        "CREATE TABLE titles AS",
        "  SELECT tconst, titleType AS kind, primaryTitle AS primary_title,",
        "         originalTitle AS original_title,",
        "         TRY_CAST(startYear AS INTEGER) AS year,",
        "         TRY_CAST(runtimeMinutes AS INTEGER) AS minutes",
        "  FROM " + read % basics.replace("'", "''"),
        "  WHERE titleType IN (%s) AND isAdult = '0';" % kinds,
        "CREATE TABLE names AS",
        "  SELECT tconst, primary_title AS title FROM titles",
        "  UNION SELECT tconst, original_title FROM titles",
        "  UNION SELECT titleId, title",
        "    FROM " + read % akas.replace("'", "''"),
        "    WHERE titleId IN (SELECT tconst FROM titles)",
        "      AND title IS NOT NULL;",
        "CREATE TABLE folded AS",
        "  SELECT DISTINCT tconst, %s AS plain, %s AS spelled FROM names;"
        % (_fold("title"), _fold(_spelled_out("title"))),
        "CREATE TABLE keys AS",
        "  SELECT DISTINCT replace(key, ' ', '') AS key, tconst FROM (",
        "    SELECT tconst, unnest([plain, spelled,",
        "        regexp_replace(plain, '^(%s) ', ''),"  % articles,
        "        regexp_replace(spelled, '^(%s) ', '')]) AS key" % articles,
        "    FROM folded)",
        "  WHERE key <> ''",
        "  ORDER BY key;",
        "DROP TABLE folded;",
        "",
    ])


def _build(where: str, database: str, log: Callable[[str], None]) -> bool:
    """The database, built beside the old one and moved over it only once it is
    whole - the same arrangement the census keeps for its own."""
    log("Building the local IMDb title index in %s (a few minutes)" % database)
    part = database + ".part"
    for stale in (part, part + ".wal"):
        if os.path.exists(stale):
            os.remove(stale)
    sql = build_sql(os.path.join(where, BASICS), os.path.join(where, AKAS))
    done = subprocess.run(["duckdb", "-bail", part], input=sql.encode("utf-8"),
                          stdout=subprocess.DEVNULL)
    if done.returncode != 0 or not os.path.isfile(part):
        log("WARNING: DuckDB could not build the IMDb title index")
        return False
    os.replace(part, database)
    return True


# How short a key may be and still be looked for a slip away. The same floor
# :data:`medialib.lib.titlematch.MIN_TYPO_LENGTH` puts on a slip: below it one
# edit reaches a great many other titles.
MIN_NEAR_LENGTH = 6


def titles_near(database: str, keys) -> list:
    """Every title the database holds under a key ONE edit away from one of
    these - a letter changed, one too many or too few, two neighbours swapped -
    in the shape :func:`titles_under` answers in.

    Only a way of reaching a film the fold cannot: whether the two titles are
    really one title with a slip in it is
    :func:`medialib.lib.titlematch.one_slip_apart`'s to say, and it says it
    over the titles as written, which is where a digit - never a slip - can
    still be seen.

    Every key is scanned, narrowed first to the ones it can be: a single edit
    changes the length by one at most, and in a key this long it leaves
    either the first two characters or the last two alone.
    """
    wanted = sorted(key for key in keys if key and key.isalnum()
                    and key.isascii() and len(key) >= MIN_NEAR_LENGTH)
    if not wanted:
        return []
    near = " OR ".join(
        "(length(key) BETWEEN %d AND %d AND (left(key, 2) = '%s' OR "
        "right(key, 2) = '%s') AND damerau_levenshtein(key, '%s') = 1)"
        % (len(key) - 1, len(key) + 1, key[:2], key[-2:], key)
        for key in wanted)
    return _rows("SELECT DISTINCT tconst FROM keys WHERE %s" % near, database)


def titles_under(database: str, keys) -> list:
    """Every title the database reaches under any of these squeezed keys, as
    dicts: ``tconst``, ``kind``, ``year``, ``minutes``, the two titles IMDb
    leads with, and ``titles`` - every title it is known by.

    [] for a key set that reaches nothing, and for a database that cannot be
    read: a lookup that could not be made is a lookup that did not answer.
    """
    wanted = sorted(key for key in keys if key and key.isalnum()
                    and key.isascii())
    if not wanted:
        return []
    listed = ", ".join("'%s'" % key for key in wanted)
    return _rows("SELECT tconst FROM keys WHERE key IN (%s)" % listed,
                 database)


def _rows(reached: str, database: str) -> list:
    """The titles ``reached`` selects the ids of, each with everything a
    candidate is read from."""
    query = ("SELECT t.tconst, t.kind, t.year, t.minutes, t.primary_title, "
             "t.original_title, list(DISTINCT n.title) AS titles "
             "FROM titles t JOIN names n USING (tconst) "
             "WHERE t.tconst IN (%s) "
             "GROUP BY ALL ORDER BY t.tconst" % reached)
    done = subprocess.run(["duckdb", "-readonly", "-json", database, "-c",
                           query], stdout=subprocess.PIPE,
                          stderr=subprocess.DEVNULL)
    if done.returncode != 0:
        return []
    text = done.stdout.decode("utf-8", "replace").strip()
    if not text:
        return []
    try:
        rows = json.loads(text)
    except ValueError:
        return []
    return [row for row in rows if isinstance(row, dict)]
