"""What ingest-music asks of beets besides the import itself: Discogs as a
second source to match against, and three reports read from the library once
the import is done.

  * ``missing`` - the albums of this run that lack tracks MusicBrainz lists.
  * ``duplicates`` - albums the library holds twice, by MusicBrainz release.
  * ``bad`` - this run's files that fail their format's own check (``flac -t``
    for a flac). It needs the ``flac`` tool and is skipped without it.

The reports only read. They change no file and no tag.

Discogs needs a token. Without one, beets stops the import to ask for a
Discogs login, which a quiet run cannot answer. So the shipped configs leave
the plugin out, and a run with the ``discogsToken`` setting runs with a copy of
its config that adds it.
"""

import os
import shutil
import subprocess
from collections.abc import Callable, Sequence
from typing import NamedTuple

__all__ = ["DISCOGS_VARIABLE", "Reports", "with_discogs", "write_run_config",
           "report_argvs", "run_reports"]

# The setting holding the Discogs user token.
DISCOGS_VARIABLE = "discogsToken"

# Runs one beet command in a folder, and gives back its exit status and output.
Capture = Callable[[str, Sequence[str]], tuple[int, str]]


class Reports(NamedTuple):
    """How many lines each report found."""

    missing: int = 0
    duplicates: int = 0
    bad: int = 0


def with_discogs(config_text: str, token: str) -> str:
    """``config_text`` with the discogs plugin added to its ``plugins:`` line
    and the token in a ``discogs:`` section. Raises ValueError for a config
    without a one-line ``plugins:`` list or with a ``discogs:`` section of its
    own: the run will not guess where else they go."""
    lines = config_text.splitlines()
    plugins = [index for index, line in enumerate(lines)
               if line.startswith("plugins:")]
    if len(plugins) != 1:
        raise ValueError("the config has no single plugins: line")
    if any(line.startswith("discogs:") for line in lines):
        raise ValueError("the config has a discogs: section of its own")
    if "discogs" not in lines[plugins[0]].split()[1:]:
        lines[plugins[0]] += " discogs"
    quoted = "'" + token.replace("'", "''") + "'"
    lines += ["", "discogs:", "    user_token: " + quoted]
    return "\n".join(lines) + "\n"


def write_run_config(config: str, token: str, scratch_dir: str) -> str:
    """The run's copy of ``config`` with Discogs added, written into the
    run's scratch where only its owner can read it, since it holds the token.
    Gives back its path."""
    with open(config, encoding="utf-8") as handle:
        text = with_discogs(handle.read(), token)
    path = os.path.join(scratch_dir, "beets-run.yaml")
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
    return path


def report_argvs(beet: Sequence[str], library_dir: str,
                 since: str) -> dict[str, list[str]]:
    """Each report's command. ``beet`` is everything before the command
    (``beet -c <config>`` and a layout's ``-d``). ``since`` is when this run's
    import began, as beets dates are written: missing tracks and damaged files
    are looked for only among what this run added. Duplicates are looked for
    over the whole library, since a new album's twin is an old one."""
    added = "added:%s.." % since
    return {
        "missing": [*beet, "missing", "-c", added],
        "duplicates": [*beet, "duplicates", "-a",
                       "path:" + os.path.abspath(library_dir)],
        "bad": [*beet, "bad", added],
    }


def _found(name: str, output: str) -> list[str]:
    """The lines of a report that name something, not the ones that only say
    an album misses nothing (``missing -c`` writes ``: 0`` for those)."""
    lines = [line.rstrip() for line in output.splitlines() if line.strip()]
    if name == "missing":
        lines = [line for line in lines if not line.endswith(": 0")]
    return lines


def _counted(name: str, found: Sequence[str]) -> int:
    """How many things a report found. ``bad`` writes one line per file and
    indents the checker's own messages under it, so only the unindented
    lines are files."""
    if name == "bad":
        return sum(1 for line in found if not line[:1].isspace())
    return len(found)


def _capture(directory: str, argv: Sequence[str]) -> tuple[int, str]:
    try:
        done = subprocess.run(list(argv), cwd=directory, capture_output=True,
                              text=True, errors="replace")
    except OSError:
        return 127, ""
    return done.returncode, done.stdout


_HEADINGS = {
    "missing": "Albums of this run that miss tracks",
    "duplicates": "Albums the library holds twice",
    "bad": "Files of this run that fail their check",
}


def run_reports(beet: Sequence[str], library_dir: str, since: str,
                log: Callable[[str], None], *,
                capture: Capture = _capture,
                have_flac: bool | None = None) -> Reports:
    """Runs the three reports, logs what each found, and counts it."""
    if have_flac is None:
        have_flac = shutil.which("flac") is not None
    counts = {}
    for name, argv in report_argvs(beet, library_dir, since).items():
        if name == "bad" and not have_flac:
            log("  Checking this run's files for damage: skipped, no flac "
                "tool on PATH")
            counts[name] = 0
            continue
        status, output = capture(library_dir, argv)
        if status != 0:
            log("  beet %s exited with status %d - no report" % (name, status))
            counts[name] = 0
            continue
        found = _found(name, output)
        counts[name] = _counted(name, found)
        if found:
            log("  %s:" % _HEADINGS[name])
            for line in found:
                log("    " + line)
    return Reports(**counts)
