"""What looking at a cube looks like.

``cubes`` says what a cube is; this says what a person clicking on one sees. The
output is a single self-contained .html: opening it needs nothing but a browser,
and the library's own file listing never leaves the file.

The one thing to understand before changing anything here is the BASE GRAIN. The
page is given one row per COMBINATION of dimension values - every axis at its
finest level, nothing rolled up - and not one row per file, and emphatically not
the whole cube table (which holds every level of roll-up at once, so a tool that
aggregated for itself would count the same bytes once per grouping set). Every
measure is additive, so the browser folds those buckets into coarser ones and gets
exactly what summing the files would have.

The page's own units are its own: the census keeps bytes and seconds, because those
are exact whole numbers, and only the page is handed gigabytes and hours. Both are
a multiplication by a constant, which is what makes them safe where every roll-up
is an addition.
"""

import base64
import os

from medialib.lib import cubes

__all__ = [
    "viewer_base64",
    "viewer_title",
    "viewer_measures",
    "viewer_bitrate_tiers",
    "viewer_aspect_ratios",
    "viewer_codec_readings",
    "viewer_dimensions",
    "viewer_grain_columns",
    "viewer_grain_sql",
    "viewer_grain_export_sql",
    "viewer_opening_axis",
    "viewer_opening_columns",
    "viewer_json_list",
    "viewer_default_config",
    "viewer_column_type",
    "viewer_schema",
    "viewer_html_escape",
    "asset_url",
    "sri_attribute",
    "viewer_html",
]

# The Perspective build the page loads, pinned to an EXACT release: a major
# alone lets jsDelivr resolve the rest, so the page would fetch whatever that tag
# points at on the day it is opened. It is also what makes the hash below mean
# anything.
CDN_BASE = "https://cdn.jsdelivr.net/npm"
PERSPECTIVE_VERSION = "5.3.0"

# Subresource Integrity for what that release serves, so the browser refuses a
# file that is not the one the hash was taken from.
#
# The stylesheet, and only the stylesheet: the four scripts are ES modules, an
# `import` has nowhere to carry an integrity, and giving it one through a
# <link rel="modulepreload" integrity> makes the browser load Perspective twice
# over so the grid never renders. The version pin above covers those four.
PERSPECTIVE_SRI = {
    "@perspective-dev/viewer/dist/css/themes.css":
        "sha384-qk6T9jdsblsY4NN8njc0IjEtqAmPyiQ2Z+Nu/cXEbGZ+/ZQKDZFMCDHPmTXndrjA",
}

PERSPECTIVE_STYLESHEET = "@perspective-dev/viewer/dist/css/themes.css"

# The two scalings, spelled once so the SQL and the page's own hint cannot drift
# apart. Seconds to hours, and bytes to the DECIMAL gigabyte - 10^9 and not 2^30,
# because the number this is compared against is a disk's stated capacity.
SECONDS_PER_HOUR = "3600.0"
BYTES_PER_GIGABYTE = "1000000000.0"

_TITLES = {"audio": "Audio", "video": "Video", "books": "Books",
           "comics": "Comics"}

# The summable columns the export carries, in the order they should appear.
# "files" first, because it is the one every question starts with.
_MEASURES = {
    "audio": "files sizeGigabytes durationHours chapters",
    "video": "files sizeGigabytes durationHours chapters",
    "books": "files sizeGigabytes pages words characters",
    "comics": "files sizeGigabytes pages",
}

# The bitrate BANDS, the frame's shape, and the codec's family and generation:
# viewer axes rather than cube axes, because every axis added to a cube doubles its
# grouping sets - video's eleven are already 2048 of them - and the page rolls
# these up from the base grain at no such cost.
_BITRATE_TIERS = {"audio": "bitrateTier",
                  "video": "videoBitrateTier firstAudioBitrateTier"}
_ASPECT_RATIOS = {"video": "aspectRatio"}
_CODEC_READINGS = {"video": "videoCodecFamily videoCodecEra"}

# The axis a tab opens grouped by, and the measures it opens showing. The bitrate
# halves are left out of the opening columns: they are one click away, but they are
# a fraction rather than a number and would read as two nonsense columns.
_OPENING_AXIS = {"audio": "codec", "video": "resolution", "books": "format",
                 "comics": "resolution"}
_OPENING_COLUMNS = {
    "audio": "files durationHours sizeGigabytes",
    "video": "files durationHours sizeGigabytes",
    "books": "files words pages sizeGigabytes",
    "comics": "files pages sizeGigabytes",
}

# The Perspective type each column is given, spelled out rather than sniffed: a
# census column is empty when nobody stated the value, and a pivot engine that
# cannot infer a column of nothing refuses the whole table.
#
# The sizes are the other half of it. Perspective's "integer" is 32 bits, which a
# media library overruns on its first measure - 2^31 bytes is 2GB - so anything
# summed that can grow is a float, where a double holds every whole number up to
# 2^53 exactly. Only the genuinely small counts stay integers, so they read as
# "12" and not "12.00".
_INTEGER_COLUMNS = ("files", "chapters", "pages")
_FLOAT_COLUMNS = ("sizeGigabytes", "durationHours", "words", "characters",
                  "sizeBytes", "durationSeconds")

# The measure block of each type's grain SELECT, after the axes and COUNT(*).
_MEASURE_SQL = {
    "audio": ("    SUM(sizeBytes) / %s AS sizeGigabytes,\n"
              "    SUM(durationSeconds) / %s AS durationHours,\n"
              "    SUM(chapters) AS chapters"),
    "video": ("    SUM(sizeBytes) / %s AS sizeGigabytes,\n"
              "    SUM(durationSeconds) / %s AS durationHours,\n"
              "    SUM(chapters) AS chapters"),
    "books": ("    SUM(sizeBytes) / %s AS sizeGigabytes,\n"
              "    SUM(pages) AS pages,\n"
              "    SUM(words) AS words,\n"
              "    SUM(characters) AS characters"),
    "comics": ("    SUM(sizeBytes) / %s AS sizeGigabytes,\n"
               "    SUM(pages) AS pages"),
}


# --- the page's own text -------------------------------------------------------
# The two halves of the page either side of its per-tab data, exactly as
# The shell's heredocs produced them, with the three values it
# substitutes left as sentinels: @@TITLE@@ (already HTML-escaped), @@BASE@@ and
# @@VER@@.
#
# Derived from the shell by running it, not transcribed from it: the first heredoc
# is UNQUOTED, so bash expands into a page full of $, % and CSS percentages, and
# hand-applying those expansions is a mistake waiting somewhere in the middle of
# 9.5KB. What was run is what is here.

_PAGE_HEAD = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>@@TITLE@@</title>
<link rel="stylesheet" crossorigin="anonymous"@@CSSHASH@@
      href="@@BASE@@/@perspective-dev/viewer@@@VER@@/dist/css/themes.css">
<style>
    :root { color-scheme: light dark; }
    html, body {
        margin: 0; height: 100%;
        font-family: system-ui, -apple-system, "Segoe UI", sans-serif;
    }
    body { display: flex; flex-direction: column; }
    header {
        padding: 0.6rem 0.9rem 0;
        border-bottom: 1px solid rgba(128, 128, 128, 0.35);
    }
    h1 { font-size: 1rem; font-weight: 600; margin: 0 0 0.3rem; }
    .hint { font-size: 0.78rem; opacity: 0.75; margin: 0 0 0.5rem; }
    .hint code { font-size: 0.95em; }
    nav { display: flex; gap: 0.3rem; flex-wrap: wrap; }
    nav button {
        font: inherit; font-size: 0.85rem;
        padding: 0.3rem 0.9rem;
        border: 1px solid rgba(128, 128, 128, 0.5);
        border-bottom: none;
        border-radius: 5px 5px 0 0;
        background: transparent; color: inherit; cursor: pointer;
    }
    nav button[aria-selected="true"] {
        background: rgba(128, 128, 128, 0.22); font-weight: 600;
    }
    nav .spacer { flex: 1; }
    nav button.reset { border-radius: 5px; border-bottom: 1px solid rgba(128,128,128,0.5); margin-bottom: 0.2rem; }
    main { flex: 1; position: relative; min-height: 0; }
    perspective-viewer { position: absolute; inset: 0; }
    perspective-viewer[hidden] { display: none; }
    #loading {
        position: absolute; inset: 0;
        display: flex; align-items: center; justify-content: center;
        font-size: 0.9rem; opacity: 0.7; text-align: center; padding: 1rem;
    }
</style>
</head>
<body>
<header>
    <h1>@@TITLE@@</h1>
    <p class="hint">
        Drag a field into <strong>Group By</strong> to roll up, open a group to drill
        down, tick a column to change what is measured. Each row is a bucket of files,
        already counted and summed &mdash; grouping them further only adds buckets
        together, so every total is exact.
        &nbsp;&bull;&nbsp; Sizes are <strong>gigabytes</strong> (1 GB =
        1,000,000,000 bytes) and durations are <strong>hours:minutes</strong>; the
        reports and the database behind them keep the exact bytes and seconds.
        &nbsp;&bull;&nbsp; Bitrate is a rate and cannot be summed, so it is an
        <em>axis</em> here: group by a <code>&hellip;BitrateTier</code> to see how much
        of the library sits in each band.
    </p>
    <nav id="tabs"></nav>
</header>
<main>
    <div id="loading">Loading the Perspective engine&hellip;<br>
        (needed once per browser, from the network; the data is already in this file)</div>
</main>
<script type="module">
import "@@BASE@@/@perspective-dev/viewer@@@VER@@/dist/cdn/perspective-viewer.js";
import "@@BASE@@/@perspective-dev/viewer-datagrid@@@VER@@/dist/cdn/perspective-viewer-datagrid.js";
import "@@BASE@@/@perspective-dev/viewer-charts@@@VER@@/dist/cdn/perspective-viewer-charts.js";
import perspective from "@@BASE@@/@perspective-dev/client@@@VER@@/dist/cdn/perspective.js";

// The census writes UTF-8 and a library is full of accented, CJK and emoji file
// names, so the base64 has to be decoded as BYTES and then decoded as UTF-8.
// atob alone yields one character per byte, which turns every multi-byte name into
// mojibake - the same mistake the reports themselves are careful not to make.
const decodeCsv = (b64) => {
    const binary = atob(b64);
    const bytes = new Uint8Array(binary.length);
    for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
    return new TextDecoder("utf-8").decode(bytes);
};

// --- hh:mm is a rendering, not a column ---------------------------------------
// The duration measure holds HOURS as a number, because a number is the only thing
// that can be rolled up: folding two buckets together has to be an addition, and
// "12:30" + "01:45" is not one. So the hours stay a number in the data, in the
// schema and in every sum the engine does, and are turned into hours:minutes at the
// last possible moment - on the cells that are already on screen.
//
// 1247.5 becomes "1247:30", not a clock time: this is a LENGTH, so the hours run
// past 24 and are never wrapped. The minutes are rounded rather than truncated, so
// a bucket 59.7 minutes long does not read as an hour short of itself.
const hoursToHhMm = (hours) => {
    if (typeof hours !== "number" || !isFinite(hours)) return null;
    const sign = hours < 0 ? "-" : "";
    const total = Math.round(Math.abs(hours) * 60);
    const h = Math.floor(total / 60);
    const m = total % 60;
    return sign + String(h).padStart(2, "0") + ":" + String(m).padStart(2, "0");
};

// The datagrid's own hook for exactly this: a style listener runs again after every
// draw, so scrolling, re-pivoting and re-sorting all keep the formatting, and the
// engine stays the only thing that decides what a cell's VALUE is.
//
// Wrapped in a try/catch and in a pile of "does this exist" tests on purpose. It is
// the one thing in this page that reaches into another library's internals, and it
// is cosmetic: an engine that no longer offers the hook leaves the column reading as
// decimal hours ("1247.51"), which is the honest number rather than a broken page -
// the same "a named gap beats no report" the census itself works by.
const findGrid = (plugin) => {
    if (!plugin) return null;
    if (plugin.regular_table) return plugin.regular_table;
    if (typeof plugin.querySelector === "function") {
        const inside = plugin.querySelector("regular-table");
        if (inside) return inside;
    }
    if (plugin.shadowRoot) {
        const shadowed = plugin.shadowRoot.querySelector("regular-table");
        if (shadowed) return shadowed;
    }
    if (typeof plugin.addStyleListener === "function" &&
        typeof plugin.getMeta === "function") return plugin;
    return null;
};

const columnOf = (meta) => {
    const header = meta && meta.column_header;
    if (Array.isArray(header)) return header[header.length - 1];
    return header;
};

const hookedGrids = new WeakSet();

const renderDurations = async (viewer) => {
    try {
        const plugin = await viewer.getPlugin();
        const grid = findGrid(plugin);
        if (!grid || typeof grid.addStyleListener !== "function" ||
            typeof grid.getMeta !== "function") return;
        // Once per grid: the hook is re-sought whenever the pivot changes (a turn
        // through a chart plugin and back builds a new one), and a grid that is
        // already formatting itself must not be given the same listener twice.
        if (hookedGrids.has(grid)) return;
        hookedGrids.add(grid);
        grid.addStyleListener(() => {
            for (const cell of grid.querySelectorAll("tbody td")) {
                const meta = grid.getMeta(cell);
                if (!meta || columnOf(meta) !== "durationHours") continue;
                const text = hoursToHhMm(meta.value);
                if (text !== null) cell.textContent = text;
            }
        });
    } catch (ignored) {
        /* the column keeps its decimal hours; see above */
    }
};

const DATA = {
"""

_PAGE_TAIL = """};

const main = document.querySelector("main");
const tabs = document.getElementById("tabs");
const loading = document.getElementById("loading");
const worker = await perspective.worker();
const viewers = {};
let active = null;

const show = async (type) => {
    active = type;
    for (const [name, viewer] of Object.entries(viewers)) {
        viewer.hidden = name !== type;
    }
    for (const button of tabs.querySelectorAll("button[data-type]")) {
        button.setAttribute("aria-selected", String(button.dataset.type === type));
    }
};

for (const [type, spec] of Object.entries(DATA)) {
    const button = document.createElement("button");
    button.textContent = spec.title;
    button.dataset.type = type;
    button.addEventListener("click", () => show(type));
    tabs.appendChild(button);

    // The table is created from the DECLARED schema and filled afterwards, rather
    // than inferred from the CSV: a column nobody could fill - a library of epubs
    // has no page counts at all - is empty, and an empty column has no type to
    // guess. Declaring it also keeps the byte totals in doubles rather than in the
    // 32-bit integers a media library overruns immediately.
    const table = await worker.table(spec.schema);
    await table.update(decodeCsv(spec.csv));
    const viewer = document.createElement("perspective-viewer");
    viewer.hidden = true;
    main.appendChild(viewer);
    await viewer.load(table);
    await viewer.restore(spec.config);
    if ("durationHours" in spec.schema) {
        await renderDurations(viewer);
        // Switching the plugin (to a chart and back) builds a new grid, which has
        // no listener of its own yet.
        viewer.addEventListener("perspective-config-update", () => renderDurations(viewer));
    }
    viewers[type] = viewer;
}

const spacer = document.createElement("span");
spacer.className = "spacer";
tabs.appendChild(spacer);

const reset = document.createElement("button");
reset.className = "reset";
reset.textContent = "Reset this tab";
reset.addEventListener("click", async () => {
    if (active) await viewers[active].restore(DATA[active].config);
});
tabs.appendChild(reset);

loading.remove();
await show(Object.keys(DATA)[0]);
</script>
</body>
</html>
"""

def viewer_base64(path):
    """The file as one unwrapped base64 line, or None for a file that cannot be
    read.

    The shell is ``base64 -w0 -- "$1" 2>/dev/null || base64 -- "$1" | tr -d``,
    whose fallback answers the same thing for a base64 that does not know -w0 -
    and whose STATUS is how the two cases are told apart, because an empty file
    and an unreadable one both base64 to nothing.
    """
    try:
        with open(path, "rb") as handle:
            return base64.b64encode(handle.read()).decode("ascii")
    except OSError:
        return None


def viewer_title(content):
    """What the tab for a content type is called - the type's own name for one
    this module does not know."""
    return _TITLES.get(content, content)


def viewer_measures(content):
    """The summable columns the export carries, or None for a type that has
    none."""
    return _MEASURES.get(content)


def viewer_bitrate_tiers(content):
    return _BITRATE_TIERS.get(content, "")


def viewer_aspect_ratios(content):
    return _ASPECT_RATIOS.get(content, "")


def viewer_codec_readings(content):
    return _CODEC_READINGS.get(content, "")


def viewer_dimensions(content):
    """Every axis the page can group by - the cube's own, plus the shape of the
    frame, plus the codec's family and generation, plus the bitrate bands."""
    dimensions = cubes.DIMENSIONS.get(content)
    if dimensions is None:
        return None
    parts = [" ".join(dimensions)]
    for extra in (viewer_aspect_ratios(content), viewer_codec_readings(content),
                  viewer_bitrate_tiers(content)):
        if extra:
            parts.append(extra)
    return " ".join(parts)


def viewer_grain_columns(content):
    """Every column of the export, in order - the axes, then the measures. No
    path: a path is unique per file, so carrying it would make every bucket hold
    exactly one file and there would be no aggregation left to do."""
    dimensions = viewer_dimensions(content)
    measures = viewer_measures(content)
    if dimensions is None or measures is None:
        return None
    return dimensions + " " + measures


def viewer_grain_sql(content):
    """SELECT the buckets - one row per combination of that type's axes, with
    every measure summed into it.

    COUNT(*) and not a literal 1: a row here already stands for however many
    files fell into that combination, and the browser sums those counts onwards.
    """
    dimensions = viewer_dimensions(content)
    measures = _MEASURE_SQL.get(content)
    if dimensions is None or measures is None:
        return None
    names = dimensions.split()
    select = "".join("    %s,\n" % name for name in names)
    grouped = ", ".join(names)
    if content in ("audio", "video"):
        measures = measures % (BYTES_PER_GIGABYTE, SECONDS_PER_HOUR)
    else:
        measures = measures % (BYTES_PER_GIGABYTE,)
    return ("SELECT\n%s    COUNT(*) AS files,\n%s\nFROM %sFacts\nGROUP BY %s"
            % (select, measures, content, grouped))


def viewer_grain_export_sql(content, path):
    """COPY that base grain out as the CSV to embed."""
    body = viewer_grain_sql(content)
    if body is None:
        return None
    return "COPY (%s) TO %s (FORMAT CSV, HEADER);\n" % (
        body, cubes.sql_string(path))


def viewer_opening_axis(content):
    """The axis a tab opens grouped by - the one a person actually asks about
    first. "library" for a type this module does not know."""
    return _OPENING_AXIS.get(content, "library")


def viewer_opening_columns(content):
    return _OPENING_COLUMNS.get(content, "files sizeGigabytes")


def viewer_json_list(words):
    """The words as a JSON array of strings."""
    return "[" + ",".join('"%s"' % word for word in words) + "]"


def viewer_default_config(content):
    """The pivot a tab restores itself to, as JSON, and what "Reset" puts back.
    Everything in it is a starting point the user is meant to drag apart."""
    measures = viewer_measures(content) or ""
    aggregates = ",".join('"%s":"sum"' % column for column in measures.split())
    return ('{"plugin":"Datagrid","group_by":%s,"split_by":[],"columns":%s,'
            '"aggregates":{%s},"sort":[],"filter":[],"expressions":{}}'
            % (viewer_json_list([viewer_opening_axis(content)]),
               viewer_json_list(viewer_opening_columns(content).split()),
               aggregates))


def viewer_column_type(column):
    """The Perspective type a column is given. The raw byte and second names
    answer the same way as the scaled ones, so a caller asking about a column of
    the FACTS gets the type it would need for those too."""
    if column in _INTEGER_COLUMNS:
        return "integer"
    if column in _FLOAT_COLUMNS:
        return "float"
    return "string"


def viewer_schema(content):
    """That export's columns and their types, as JSON.

    A type this module does not know answers "{}" rather than refusing: the shell
    reads its columns through a command substitution, which swallows the refusal,
    so the loop runs over nothing and the empty object is printed. The caller that
    matters - the page - embeds whatever this says, and an empty schema is a tab
    Perspective will reject on its own rather than a page that was never written.
    """
    columns = viewer_grain_columns(content) or ""
    return "{" + ",".join('"%s":"%s"' % (column, viewer_column_type(column))
                          for column in columns.split()) + "}"


def asset_url(asset, base=None, version=None):
    """Where one of the pinned assets is reached, spelled the way the page's own
    href is: the package, then the version, then the path inside it."""
    base = CDN_BASE if base is None else base
    version = PERSPECTIVE_VERSION if version is None else version
    package, _sep, path = asset.partition("/dist/")
    return "%s/%s@%s/dist/%s" % (base, package, version, path)


def sri_attribute(asset, base, version):
    """The integrity attribute for one asset, or nothing.

    Nothing when the run has been pointed at another base or another version
    through the environment: the hash is of one release from one CDN, and a page
    carrying it for a different build would not load at all.
    """
    if base != CDN_BASE or version != PERSPECTIVE_VERSION:
        return ""
    digest = PERSPECTIVE_SRI.get(asset)
    return ' integrity="%s"' % digest if digest else ""


def viewer_html_escape(text):
    """Text safe to drop between HTML tags."""
    text = text.replace("&", "&amp;")
    text = text.replace("<", "&lt;")
    text = text.replace(">", "&gt;")
    text = text.replace('"', "&quot;")
    return text


def viewer_html(title, pairs, base=None, version=None):
    """The whole page as one string.

    One viewer per content type, all built at load and switched between by the
    tab bar, because they are four different tables with four different columns.
    <pairs> is the "<type>:<csv file>" list the caller hands over, in the order
    the tabs appear; the first is the tab the page opens on.
    """
    if base is None:
        base = os.environ.get("viewerCdnBase", CDN_BASE)
    if version is None:
        version = os.environ.get("viewerPerspectiveVersion",
                                 PERSPECTIVE_VERSION)
    head = (_PAGE_HEAD.replace("@@TITLE@@", viewer_html_escape(title))
            .replace("@@CSSHASH@@",
                     sri_attribute(PERSPECTIVE_STYLESHEET, base, version))
            .replace("@@BASE@@", base).replace("@@VER@@", version))
    rows = []
    for pair in pairs:
        content, _sep, path = pair.partition(":")
        rows.append('    "%s": { "title": "%s", "schema": %s, "config": %s, '
                    '"csv": "%s" },\n'
                    % (content, viewer_title(content), viewer_schema(content),
                       viewer_default_config(content),
                       viewer_base64(path) or ""))
    return head + "".join(rows) + _PAGE_TAIL
