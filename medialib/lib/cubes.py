"""The census hypercubes: what a cube is made of, as SQL.

``content-census-bi`` is the command around it; this says which columns of a
census report are MEASURES, which are DIMENSIONS, and what a raw column has to
be turned into before it can be one.

Four cubes and not one, for the same reason the census writes four reports: the
interesting columns differ per content type. A film has a resolution, a frame rate
and a dynamic range; an audiobook has channels; a novel has neither. Forcing all
four into one table would give a fact table three quarters empty and a cube whose
every axis is meaningless for three quarters of its rows.
"""

from __future__ import annotations

from medialib.lib import aspectratios, census, codecs, resolutions

# In the order the census writes them.
TYPES = ("audio", "video", "books", "comics")

# Spelled out rather than sniffed, and the reader is given these types rather than
# left to guess: a census column is empty when nobody stated the value, and a report
# whose first thousand rows happen to have no bitrate would otherwise be read as a
# text column and then refuse to be summed. The types are also the check - a report
# that does not fit them is not a report from this census.
COLUMN_SPECS = {
    "audio": (("path", "VARCHAR"), ("sizeBytes", "BIGINT"),
              ("durationSeconds", "DOUBLE"), ("bitrateBitsPerSecond", "BIGINT"),
              ("channels", "INTEGER"), ("codec", "VARCHAR"), ("chapters", "INTEGER")),
    "video": (("path", "VARCHAR"), ("sizeBytes", "BIGINT"),
              ("durationSeconds", "DOUBLE"),
              ("videoBitrateBitsPerSecond", "BIGINT"),
              ("bitrateAdequacy", "VARCHAR"), ("resolution", "VARCHAR"),
              ("frameRateFps", "DOUBLE"), ("videoCodec", "VARCHAR"),
              ("container", "VARCHAR"), ("dynamicRange", "VARCHAR"),
              ("audioTracks", "INTEGER"), ("firstAudioChannels", "INTEGER"),
              ("firstAudioCodec", "VARCHAR"),
              ("firstAudioBitrateBitsPerSecond", "BIGINT"),
              ("subtitleTracks", "INTEGER"), ("chapters", "INTEGER")),
    "books": (("path", "VARCHAR"), ("sizeBytes", "BIGINT"), ("pages", "INTEGER"),
              ("words", "BIGINT"), ("characters", "BIGINT")),
    "comics": (("path", "VARCHAR"), ("sizeBytes", "BIGINT"), ("pages", "INTEGER"),
               ("firstPageResolution", "VARCHAR"), ("container", "VARCHAR"),
               ("imageCodec", "VARCHAR")),
}

# Named once; the SQL below is generated from these, so adding an axis is one line
# here and one expression in fact_sql.
DIMENSIONS = {
    "audio": ("library", "container", "channels", "codec"),
    "video": ("library", "resolution", "hfr", "dynamicRange", "videoCodec",
              "bitrateAdequacy", "container", "audioTracks", "firstAudioChannels",
              "firstAudioCodec", "subtitleTracks"),
    "books": ("library", "format"),
    "comics": ("library", "resolution", "container", "imageCodec"),
}

# "files" is COUNT(*); the bitrates are duration-weighted; the rest are plain SUMs.
MEASURES = {
    "audio": ("files", "sizeBytes", "durationSeconds", "chapters",
              "bitrateBitsPerSecond"),
    "video": ("files", "sizeBytes", "durationSeconds", "chapters",
              "videoBitrateBitsPerSecond", "firstAudioBitrateBitsPerSecond"),
    "books": ("files", "sizeBytes", "pages", "words", "characters"),
    "comics": ("files", "sizeBytes", "pages"),
}

_WEIGHTED = ("bitrateBitsPerSecond", "videoBitrateBitsPerSecond",
             "firstAudioBitrateBitsPerSecond")

# How many explicit GROUPING SETS go in one statement. See cube_sql.
GROUPING_SET_CHUNK = 32


def _base_name(path: str) -> str:
    """The last path segment, whichever slash was used to write it."""
    base = path.rsplit("/", 1)[-1]
    return base.rsplit("\\", 1)[-1]


def report_type(path: str) -> str:
    """Which content type this report file holds, from its name, or "".

    "audioMusic.csv" is audio and "videoFilms.tsv" is video. The prefix is what the
    census names its reports with, matched case-insensitively the way every suffix
    in this repo is.
    """
    lower = census.shell_lower(_base_name(path))
    if not (lower.endswith(".csv") or lower.endswith(".tsv")):
        return ""
    for content in TYPES:
        if lower.startswith(content):
            return content
    return ""


def report_library(path: str) -> str:
    """The library a report describes - "Films" out of "videoFilms.csv"."""
    content = report_type(path)
    if not content:
        return ""
    base = _base_name(path)
    # The extension goes, then the type prefix, by LENGTH: the prefix was matched
    # case-insensitively, so it may not be spelled the way the table spells it.
    stem = base.rsplit(".", 1)[0] if "." in base else base
    return stem[len(content):]


def column_spec(content: str) -> list | None:
    """That report's columns, one "name TYPE" per line, in report order."""
    spec = COLUMN_SPECS.get(content)
    if spec is None:
        return None
    return [f"{name} {sql_type}" for name, sql_type in spec]


def column_names(content: str) -> str | None:
    """Just the names, space separated, in order."""
    spec = COLUMN_SPECS.get(content)
    if spec is None:
        return None
    return " ".join(name for name, _ in spec)


def header_matches(content: str, header: str, separator: str) -> bool:
    """Is that first line the header this census writes for this type?

    Asked of the census's own column list rather than of the spec above, so the
    answer is the census's and the two cannot drift. A carriage return is stripped
    first: a report that has been through a Windows editor is still that report.
    """
    line = census.columns(content, separator)
    if line is None:
        return False
    stripped = header[:-1] if header.endswith("\r") else header
    return stripped == line


def sql_string(text: str) -> str:
    """The text as a single-quoted SQL literal, its own quotes doubled.

    The only escape a SQL string literal has, and what carries a path holding an
    apostrophe into a query unharmed.
    """
    return "'" + text.replace("'", "''") + "'"


def text_dimension(expression: str) -> str:
    """A text column as a dimension - trimmed, lower-cased, "unknown" when empty.

    Lower-cased because a dimension is a bucket name and "AAC" and "aac" are one
    bucket; the raw table keeps the original spelling for anyone who wants it.
    """
    return f"COALESCE(NULLIF(lower(trim({expression})), ''), 'unknown')"


def number_dimension(expression: str) -> str:
    """A small integer column as a dimension - a channel count, a track count.

    Rendered as text, because every dimension has to be able to hold the "ALL" of
    a rolled-up level and the "unknown" of a value the census could not read.
    """
    return f"COALESCE(CAST({expression} AS VARCHAR), 'unknown')"


def library_dimension(content: str) -> str:
    """Which censused library a row came from, out of its report's file name.

    The name comes from the reader's own filename column, so several reports of one
    type can be loaded into one table and still be told apart.
    """
    return ("COALESCE(NULLIF(regexp_replace(parse_filename(filename, true), "
            f"'^{content}', ''), ''), 'unknown')")


def suffix_dimension(path_expression: str) -> str:
    """The file's own suffix, lower-cased - a container, a book format."""
    return ("COALESCE(NULLIF(lower(regexp_extract("
            f"{path_expression}, '\\.([A-Za-z0-9]+)$', 1)), ''), 'unknown')")


def dynamic_range_case(expression: str) -> str:
    """The census's seven-value enum collapsed to the three an axis wants.

    Dolby Vision is tested first because a Dolby Vision file is also an HDR10 or
    HLG file in every case but profile 5, and the axis has to say which of the
    three it will be PLAYED as. Nothing falls through to a guess.
    """
    return f"""CASE
            WHEN {expression} IS NULL OR trim({expression}) = '' THEN 'unknown'
            WHEN upper({expression}) LIKE '%DOLBYVISION%' OR upper({expression}) LIKE '%DOLBY VISION%' THEN 'DV'
            WHEN upper({expression}) = 'SDR' THEN 'SDR'
            WHEN upper({expression}) LIKE '%HDR%' OR upper({expression}) LIKE '%HLG%' OR upper({expression}) LIKE '%PQ%' THEN 'HDR'
            ELSE 'unknown'
        END"""

def hfr_case(expression: str) -> str:
    """High frame rate, at strictly more than 30fps - so 29.97 and 30 are "no"."""
    return f"""CASE
            WHEN {expression} IS NULL THEN 'unknown'
            WHEN {expression} > 30 THEN 'yes'
            ELSE 'no'
        END"""

# Two ladders, because a bitrate means different things by two orders of magnitude
# depending on what it measures. The labels are zero-padded so that SORTING THEM AS
# TEXT puts them in order: a pivot table orders a dimension alphabetically, under
# which "<120kbps" comes before "<36kbps" and the band reads backwards.
_BITRATE_LADDERS = {
    "audio": ((36000, "<036kbps"), (48000, "<048kbps"), (80000, "<080kbps"),
              (120000, "<120kbps"), (250000, "<250kbps"), (750000, "<750kbps")),
    "video": ((1000000, "<01.0Mbps"), (2500000, "<02.5Mbps"), (5000000, "<05.0Mbps"),
              (7500000, "<07.5Mbps"), (10000000, "<10.0Mbps"), (15000000, "<15.0Mbps"),
              (20000000, "<20.0Mbps"), (25000000, "<25.0Mbps")),
}
_BITRATE_TOP = {"audio": "'>750kbps'", "video": "'>25.0Mbps'"}


def bitrate_tier_case(expression: str, ladder: str) -> str | None:
    """A bitrate as one of the bands of that ladder.

    A bitrate is the one number in this census that resists being a measure: it is
    bits per second, so it cannot be summed, and averaging it plainly weights a
    twelve-second clip like a three-hour film. As an AXIS all of that goes away.
    """
    bands = _BITRATE_LADDERS.get(ladder)
    if bands is None:
        return None
    lines = [f"CASE\n            WHEN {expression} IS NULL THEN 'unknown'"]
    for threshold, label in bands:
        lines.append(f"            WHEN {expression} < {threshold} THEN '{label}'")
    lines.append(f"            ELSE {_BITRATE_TOP[ladder]}")
    lines.append("        END")
    return "\n".join(lines)


def weighted_bitrate(value: str, weight: str) -> str:
    """A bucket's duration-weighted mean bitrate - total bits over total seconds.

    Both FILTERs are the same condition on purpose: numerator and denominator must
    be taken over exactly the same files, or one with a duration but no stated
    bitrate would add seconds and no bits and pull the answer down. NULLIF guards
    the bucket in which nothing states a bitrate at all, which then reads as
    unknown rather than as 0.
    """
    return f"""CAST(ROUND(
            SUM({value} * {weight}) FILTER (WHERE {value} IS NOT NULL AND {weight} IS NOT NULL)
            / NULLIF(SUM({weight}) FILTER (WHERE {value} IS NOT NULL AND {weight} IS NOT NULL), 0)
        ) AS BIGINT)"""

def load_sql(content: str, files, separator: str = ",") -> str | None:
    """The raw table for one content type, read from every report of that type.

    One table per type however many libraries were censused: the reports of a type
    all have the same columns, and which library a row came from is not lost - it
    is read back out of the reader's filename column. So a census of the films, the
    series and the documentaries rolls up into one cube that can still be split
    three ways along an axis.

    ``nullstr = ''`` is what makes an empty census cell a NULL rather than a 0 or
    an error, which is the whole point of the census leaving it empty.
    """
    spec = COLUMN_SPECS.get(content)
    if spec is None:
        return None
    # A .tsv from this census is not quoted at all - there is no agreed TSV quoting
    # - so the reader must not treat a quote in a path as one either.
    quoting = ("quote = '', escape = ''" if separator == "\t"
               else "quote = '\"', escape = '\"'")
    columns = ", ".join(f"'{name}': '{sql_type}'" for name, sql_type in spec)
    paths = ", ".join(sql_string(path) for path in files)
    return (f"CREATE OR REPLACE TABLE {content}Files AS\n"
            "SELECT * FROM read_csv(\n"
            f"    [{paths}],\n"
            "    header = true,\n"
            f"    delim = '{separator}',\n"
            f"    {quoting},\n"
            "    nullstr = '',\n"
            f"    columns = {{{columns}}},\n"
            "    filename = true\n"
            ");\n")


def fact_sql(content: str) -> str | None:
    """The fact view - one row per file still, but every dimension already bucketed
    and never null, with the measures alongside.

    A view and not a table: it is one pass of CASE expressions over a table already
    in memory, so the bucketing can be corrected and the cube rebuilt without
    re-reading a line of CSV. It is also the thing to query when a bucket looks
    wrong - it holds the individual paths that went into it.
    """
    if content not in COLUMN_SPECS:
        return None
    library = library_dimension(content)
    if content == "audio":
        return (f"""CREATE OR REPLACE VIEW audioFacts AS
SELECT
    path,
    {library} AS library,
    {suffix_dimension('path')} AS container,
    {number_dimension('channels')} AS channels,
    {text_dimension('codec')} AS codec,
    {bitrate_tier_case('bitrateBitsPerSecond', 'audio')} AS bitrateTier,
    sizeBytes,
    durationSeconds,
    chapters,
    bitrateBitsPerSecond
FROM audioFiles;\n""")
    if content == "video":
        return (f"""CREATE OR REPLACE VIEW videoFacts AS
WITH measured AS (
    SELECT *,
        TRY_CAST(regexp_extract(resolution, '^([0-9]+)x([0-9]+)$', 1) AS BIGINT) AS pixelWidth,
        TRY_CAST(regexp_extract(resolution, '^([0-9]+)x([0-9]+)$', 2) AS BIGINT) AS pixelHeight
    FROM videoFiles
)
SELECT
    path,
    {library} AS library,
    {resolutions.tier_sql('pixelWidth', 'pixelHeight')} AS resolution,
    {aspectratios.bucket_sql('pixelWidth', 'pixelHeight')} AS aspectRatio,
    {hfr_case('frameRateFps')} AS hfr,
    {dynamic_range_case('dynamicRange')} AS dynamicRange,
    {text_dimension('videoCodec')} AS videoCodec,
    {codecs.family_sql('videoCodec')} AS videoCodecFamily,
    {codecs.era_sql('videoCodec')} AS videoCodecEra,
    {text_dimension('bitrateAdequacy')} AS bitrateAdequacy,
    {text_dimension('container')} AS container,
    {number_dimension('audioTracks')} AS audioTracks,
    {number_dimension('firstAudioChannels')} AS firstAudioChannels,
    {text_dimension('firstAudioCodec')} AS firstAudioCodec,
    {number_dimension('subtitleTracks')} AS subtitleTracks,
    {bitrate_tier_case('videoBitrateBitsPerSecond', 'video')} AS videoBitrateTier,
    {bitrate_tier_case('firstAudioBitrateBitsPerSecond', 'audio')} AS firstAudioBitrateTier,
    sizeBytes,
    durationSeconds,
    chapters,
    videoBitrateBitsPerSecond,
    firstAudioBitrateBitsPerSecond
FROM measured;\n""")
    if content == "books":
        return (f"""CREATE OR REPLACE VIEW booksFacts AS
SELECT
    path,
    {library} AS library,
    {suffix_dimension('path')} AS format,
    sizeBytes,
    pages,
    words,
    characters
FROM booksFiles;\n""")
    return (f"""CREATE OR REPLACE VIEW comicsFacts AS
WITH measured AS (
    SELECT *,
        TRY_CAST(regexp_extract(firstPageResolution, '^([0-9]+)x([0-9]+)$', 1) AS BIGINT) AS pageWidth
    FROM comicsFiles
)
SELECT
    path,
    {library} AS library,
    {resolutions.width_sql('pageWidth')} AS resolution,
    {text_dimension('container')} AS container,
    {text_dimension('imageCodec')} AS imageCodec,
    sizeBytes,
    pages
FROM measured;\n""")

def _measure_sql(measures) -> str:
    out = ""
    for measure in measures:
        if measure == "files":
            out += ",\n    COUNT(*) AS files"
        elif measure in _WEIGHTED:
            out += f",\n    {weighted_bitrate(measure, 'durationSeconds')} AS {measure}"
        else:
            out += f",\n    SUM({measure}) AS {measure}"
    return out


def _masks(count: int) -> list:
    """Every subset of the axes, coarsest first.

    The grand total, then each axis on its own, then every pair, and so on to the
    finest grain. A subset is a bit mask over the axis list, which is what makes
    "every combination" one loop rather than n nested ones.
    """
    out = []
    for depth in range(count + 1):
        for mask in range(1 << count):
            if bin(mask).count("1") == depth:
                out.append(mask)
    return out


def cube_sql(content: str, chunk_size: int = GROUPING_SET_CHUNK) -> str | None:
    """The cube itself - every bucket at every level of every axis, in one table.

    GROUP BY CUBE, not ROLLUP: these axes are not a hierarchy, codec does not sit
    inside resolution, so what is wanted is every COMBINATION.

    But not as one statement. A database builds every grouping of a CUBE at once,
    and the video cube's eleven axes are 2048 simultaneous aggregations, each with
    its own hash table PER THREAD - gigabytes before a row has been read, over a
    census of ten files. So the groupings go out in batches of explicit GROUPING
    SETS instead, the first as the CREATE TABLE and the rest as INSERTs, which caps
    what is in flight at the batch size rather than at 2^n. Coarsest first, so the
    finished table is already in depth order.

    That batching is also why GROUPING() is not used to mark a rolled-up axis: a
    statement may not ask GROUPING() about an axis no grouping set in THAT statement
    mentions. The mark is read from the value instead - a rolled-up axis comes back
    NULL, and a dimension in the fact view is never null in any other case.
    """
    dimensions = DIMENSIONS.get(content)
    measures = MEASURES.get(content)
    if dimensions is None or measures is None:
        return None
    count = len(dimensions)
    measure_sql = _measure_sql(measures)
    masks = _masks(count)

    out: list[str] = []
    for start in range(0, len(masks), chunk_size):
        batch = masks[start:start + chunk_size]
        batch_mask = 0
        for mask in batch:
            batch_mask |= mask

        # Only the axes some grouping set in THIS batch groups by may be named in
        # its select list; the rest are rolled up in every one of its rows and are
        # written as the literal they would have become anyway.
        select = ""
        depth_parts = []
        for bit, dimension in enumerate(dimensions):
            if (batch_mask >> bit) & 1:
                select += f"    COALESCE({dimension}, 'ALL') AS {dimension},\n"
                depth_parts.append(f"CASE WHEN {dimension} IS NULL THEN 0 ELSE 1 END")
            else:
                select += f"    'ALL' AS {dimension},\n"
        depth_sql = " + ".join(depth_parts) or "0"

        sets = ", ".join(
            "(" + ", ".join(dimensions[bit] for bit in range(count) if (mask >> bit) & 1) + ")"
            for mask in batch)

        head = (f"CREATE OR REPLACE TABLE {content}Cube AS\nSELECT\n" if not out
                else f"INSERT INTO {content}Cube\nSELECT\n")
        out.append(f"{head}{select}    {depth_sql} AS depth{measure_sql}\n"
                   f"FROM {content}Facts\nGROUP BY GROUPING SETS ({sets});\n\n")
    return "".join(out)


def totals_sql(content: str) -> str | None:
    """The grand total - the one row in which every axis is rolled up.

    Read from the cube rather than computed again, which is the point of having
    built it.
    """
    dimensions = DIMENSIONS.get(content)
    if dimensions is None:
        return None
    return (f"SELECT * EXCLUDE ({', '.join(dimensions)}, depth) "
            f"FROM {content}Cube WHERE depth = 0;\n")


def export_sql(content: str, directory: str) -> str | None:
    """The cube as a .csv beside its reports.

    Ordered on the way out - coarsest level first, then along the axes in the order
    they are declared - so two exports of the same library are the same file and
    can be diffed.
    """
    dimensions = DIMENSIONS.get(content)
    if dimensions is None:
        return None
    directory = directory[:-1] if directory.endswith("/") else directory
    target = sql_string(f"{directory}/{content}Cube.csv")
    return (f"COPY (SELECT * FROM {content}Cube ORDER BY depth, "
            f"{', '.join(dimensions)}) TO {target} (FORMAT CSV, HEADER);\n")
