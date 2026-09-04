"""The rename rules: never clobber, never lose a file.

Every rename in the repo goes through :func:`safe_rename`, and the reason it is a
function rather than a call to ``mv`` is a promise that is invisible in a listing
of names: a rename must not change any date. Not the item's own, and not either
folder's - the OS bumps a directory's modification time when an entry is added to
or removed from it, so a file moved between two folders freshens both of them
although neither's contents really changed. The dates are read before anything
moves and written back afterwards.

The other functions here are the ones built on that promise, plus the two
read-only questions their callers ask first (:func:`is_empty_folder`,
:func:`clean_input_path`).

Second-resolution dates, deliberately: bash captures them with ``date -r ...
+%Y%m%d%H%M.%S`` and restores them with ``touch -t``, a format with no room for
anything finer, so a file that survives a rename here comes out rounded down to
the whole second. Keeping the nanoseconds would be an improvement and would also
be a divergence, so it waits for the fixes-after-the-port pass.
"""

import atexit
import os
import random
import shutil
import signal
import sys
from collections.abc import Iterable, Iterator
from typing import Any

from medialib.lib.enums import shell_lower

__all__ = [
    "OutsideTheRun",
    "SkipLog",
    "assert_within",
    "clean_input_path",
    "is_within",
    "is_empty_folder",
    "interrupt_signals",
    "lower_case_ending",
    "lower_case_extensions",
    "safe_rename",
    "unique_suffix_path",
]


class SkipLog:
    """The renames that were refused to avoid an overwrite, in the order refused.

    bash keeps this in a file whose path is exported, because its callers fan out
    into parallel ``xargs -P`` workers and a skip recorded in a child has to reach
    the report printed by the parent. Nothing here forks, so it is a list - but it
    is still a collaborator passed in rather than a global, because "was this
    rename refused?" is the only way a caller can tell a refusal from a rename
    that was never needed.
    """

    def __init__(self) -> None:
        self.skips: list[tuple[str, str]] = []

    def record(self, source: str, destination: str) -> None:
        self.skips.append((source, destination))

    def report(self) -> list[str]:
        """The lines bash prints on stderr, in order, including the count line."""
        lines = [f"Safety: skipped {len(self.skips)} rename(s) to avoid overwrite"]
        if self.skips:
            lines.append("Safety skip details:")
            lines.extend(f"  {source} -> {destination}" for source, destination in self.skips)
        return lines


class RunSkipLog(SkipLog):
    """``recordSafetySkip``: the run-level log, which is a FILE.

    The report a run prints is read back from $SAFETY_LOG, and phases that are
    still bash append to that same file - so a skip recorded only in memory
    would be counted by nobody.
    """

    def record(self, source: str, destination: str) -> None:
        super().record(source, destination)
        path = os.environ.get("SAFETY_LOG", "")
        if not path:
            return
        try:
            with open(path, "a", encoding="utf-8",
                      errors="surrogateescape") as handle:
                handle.write("%s -> %s\n" % (source, destination))
        except OSError:
            pass


def _dirname(path: str) -> str:
    """``dirname(1)``, which is not ``os.path.dirname``.

    They part company on a trailing slash: ``dirname a/`` is ``.`` where
    ``os.path.dirname("a/")`` is ``a``. safe_rename hands whatever the caller
    passed straight to dirname and then restores the date of the result, so
    getting this wrong would freshen the wrong folder.
    """
    stripped = path.rstrip("/")
    if not stripped:
        return "/" if path else "."
    if "/" not in stripped:
        return "."
    head = stripped[: stripped.rindex("/")].rstrip("/")
    return head or "/"


def _mtime(path: str) -> int | None:
    """The whole-second modification time, or None for a path that cannot be read.

    bash's capture is a ``date -r`` whose failure is an empty string, and an empty
    capture simply skips that restore. A path that vanishes between the capture
    and the restore is therefore not an error, here or there.
    """
    try:
        return int(os.stat(path).st_mtime)
    except OSError:
        return None


def _restore_mtime(path: str, when: int | None) -> None:
    """Best-effort ``touch -m -t``. A failure never changes a rename's verdict."""
    if when is None:
        return
    try:
        os.utime(path, (os.stat(path).st_atime, when))
    except OSError:
        pass


def safe_rename(source: str, destination: str, log: SkipLog | None = None) -> bool:
    """Rename ``source`` to ``destination``, and report whether one happened.

    False, having done nothing, when the two are the same path (no work) or when
    the destination already exists (a rename would destroy it, so it is refused
    and recorded). True only when the item really moved, which is what lets a
    caller count its changes.
    """
    if source == destination:
        return False
    # lexists, not exists: a BROKEN symlink answers False to os.path.exists, so a
    # destination that is one reads as free. shutil.move then writes to it, and
    # across a filesystem boundary that is a copy which FOLLOWS the link and
    # lands wherever it pointed - outside the tree this run was given. Something
    # is already sitting at that name whatever it resolves to, so it is refused.
    if os.path.lexists(destination):
        if log is not None:
            log.record(source, destination)
        return False

    # Read every date the move is about to disturb, before it does. Both parents,
    # because adding an entry to a folder and removing one from a folder each
    # count as a change to that folder.
    source_parent = _dirname(source)
    destination_parent = _dirname(destination)
    moved_at = _mtime(source)
    source_parent_at = _mtime(source_parent)
    destination_parent_at = _mtime(destination_parent)

    try:
        # shutil.move rather than os.rename: the destination may be on another
        # filesystem, and the network shares and phone mounts this runs over
        # regularly are.
        shutil.move(source, destination)
    except (OSError, shutil.Error):
        pass

    # The outcome decides, not the exit status of the move. On the same mounts,
    # a move can complete and still report failure because it could not carry
    # permissions or ownership across, and a caller's change counter should not
    # depend on which filesystem the user happened to be on.
    if not (os.path.lexists(destination) and not os.path.lexists(source)):
        return False

    # The item first and the folders last: touching a file does not re-bump its
    # parent, so this order is stable. The source's parent is skipped when it is
    # also the destination's, which is every rename within one folder.
    _restore_mtime(destination, moved_at)
    _restore_mtime(destination_parent, destination_parent_at)
    if source_parent != destination_parent:
        _restore_mtime(source_parent, source_parent_at)
    return True


def _split_extension(base: str) -> tuple[str, str]:
    """``base`` as (stem, extension-with-its-dot), by the rule the shell uses.

    A leading dot does not open an extension: ``.gitignore`` is all stem. Any
    later dot does, and it is the last one that counts.
    """
    if "." in base and not base.startswith("."):
        stem, _, extension = base.rpartition(".")
        return stem, f".{extension}"
    return base, ""


def unique_suffix_path(destination: str) -> str:
    """``destination``, or the first " (N)" variant of it that collides with nothing.

    Used where two files that want one name must BOTH survive - flattening a tree,
    converting into a shared folder - as opposed to safe_rename, which keeps the
    one already there and refuses the newcomer.
    """
    # lexists for the reason safe_rename uses it: a name a broken symlink holds
    # is not a free name.
    if not os.path.lexists(destination):
        return destination

    if "/" in destination:
        directory, _, base = destination.rpartition("/")
    else:
        directory, base = ".", destination
    stem, extension = _split_extension(base)

    number = 2
    while True:
        candidate = f"{directory}/{stem} ({number}){extension}"
        if not os.path.lexists(candidate):
            return candidate
        number += 1


def lower_case_ending(path: str, log: SkipLog | None = None) -> None:
    """Normalise one file's extension to lower case, without ever clobbering.

    Files with no extension, and extensions already lower case, are left alone.
    The rename goes through :func:`safe_rename`, so turning ``FILE.JPG`` into
    ``file.jpg`` when a ``file.jpg`` already exists is refused and recorded rather
    than done.
    """
    base = path.rpartition("/")[2]
    if "." not in base or base.startswith("."):
        return
    extension = base.rpartition(".")[2]
    lowered = shell_lower(extension)
    if lowered == extension:
        return
    safe_rename(path, f"{path.rpartition('.')[0]}.{lowered}", log)


def _files_below(directory: str) -> Iterator[str]:
    """Every regular file in the tree, at any depth, symlinks excluded.

    ``find -type f`` is false for a symlink however it resolves, so following one
    here would rename files the shell version never touches.
    """
    for parent, _, names in os.walk(directory):
        for name in names:
            path = os.path.join(parent, name)
            if os.path.isfile(path) and not os.path.islink(path):
                yield path


def lower_case_extensions(directory: str, log: SkipLog | None = None) -> None:
    """Lower-case the extension of every file in ``directory``'s tree.

    A path that is not a directory is not an error: callers run this over folders
    that may hold nothing to normalise, under ``set -e``.

    The whole tree is listed before the first rename, rather than renamed while
    walking. Renaming entries in a directory that is still being read leaves what
    the reader sees next unspecified - a file can be visited twice or missed - and
    "which files got normalised" would then depend on the filesystem.
    """
    if not os.path.isdir(directory):
        return
    for path in sorted(_files_below(directory)):
        lower_case_ending(path, log)


def is_empty_folder(path: str) -> bool:
    """True when ``path`` is a folder holding no entry at all.

    Hidden entries and empty sub-folders count: this is the question asked before
    telling a user their input folder is the wrong one, and a folder with a stray
    dotfile in it is not the wrong folder, it is a folder they have to look at.
    """
    if not os.path.isdir(path):
        return False
    try:
        with os.scandir(path) as entries:
            return next(iter(entries), None) is None
    except OSError:
        return False


# The characters the shell splits a name on. Not str.split(), which also splits on
# a carriage return, a form feed and every Unicode space - and a name is renamed to
# whatever this returns.
_BLANKS = " \t\n"

# mktemp draws its X placeholders from this set.
_MKTEMP_ALPHABET = ("0123456789abcdefghijklmnopqrstuvwxyz"
                    "ABCDEFGHIJKLMNOPQRSTUVWXYZ")


def clean_input_path(path: str) -> str:
    """The pretreatment a NAME gets before the media tools see it.

    A closed list: underscores become spaces, apostrophes and exclamation marks
    and backticks are dropped because they need quoting inside the ffmpeg concat
    lists and cue sheets these paths end up in, then runs of whitespace collapse
    and the ends are trimmed. Everything else survives, including quotes and
    backslashes.

    Only the last component is cleaned, and a directory part is handed back
    untouched. The parents are where the input LIVES: a library under
    ``/srv/My_Books`` is not this run's to rename, and cleaning them produced a
    destination directory that did not exist, which `shutil.move` then CREATED -
    copying the tree out from under the run and deleting the original.
    """
    parent, separator, name = path.rpartition(os.sep)
    cleaned = name.replace("_", " ")
    for character in "'!`":
        cleaned = cleaned.replace(character, "")
    return parent + separator + " ".join(_words(cleaned))


def _words(text: str) -> Iterable[str]:
    word: list[str] = []
    for character in text:
        if character in _BLANKS:
            if word:
                yield "".join(word)
                word = []
        else:
            word.append(character)
    if word:
        yield "".join(word)


# --- the interrupt, and the refusal an unusable input gets ---------------------
# The abort flag is a FILE rather than a signal handler's variable: a worker is a
# separate process, and a variable one of them sets is a variable nobody else
# ever sees.

INTERRUPTED_EXIT_STATUS = 130


def init_abort_flag(path: str | None = None) -> str:
    """``initAbortFlag``: settle where this run records an interrupt.

    Inheritance wins: a wrapper that has already started a run owns the flag, and
    the inner script must share it so one Ctrl+C stops every layer. Taking the
    explicit path in preference would give each layer a private flag the others
    cannot see.
    """
    inherited = os.environ.get("ABORT_FLAG", "")
    if inherited:
        return inherited
    if not path:
        directory = os.environ.get("TMPDIR", "/tmp")
        path = os.path.join(
            directory, "abortRequested." + "".join(
                random.choice(_MKTEMP_ALPHABET) for _ in range(8)))
    os.environ["ABORT_FLAG"] = path
    os.environ["ABORT_FLAG_OWNER"] = str(os.getpid())
    atexit.register(release_abort_flag)
    return path


def release_abort_flag() -> None:
    """``releaseAbortFlag``: remove the flag, if this layer is the one that made
    it. An inner script that deleted an inherited one would tell the wrapper
    above it that the run was never interrupted."""
    if os.environ.get("ABORT_FLAG_OWNER", "") != str(os.getpid()):
        return
    try:
        os.remove(os.environ.get("ABORT_FLAG", ""))
    except OSError:
        pass


def abort_requested() -> bool:
    """``abortRequested``: has this run been asked to stop?"""
    flag = os.environ.get("ABORT_FLAG", "")
    return bool(flag) and os.path.exists(flag)


def request_abort() -> None:
    """``requestAbort``: record an interrupt for the whole run.

    Never raises, for the reason the shell's always returns 0: it runs inside a
    signal handler, where a failure would take down the run it exists to end
    tidily.
    """
    flag = os.environ.get("ABORT_FLAG", "")
    if not flag:
        return
    try:
        with open(flag, "ab"):
            pass
    except OSError:
        pass


def fail_no_relevant_input(path: str, what: str,
                           stream=None) -> int:
    """``failNoRelevantInput``: the refusal a folder gets when it holds nothing
    this run can work with, and the status to exit with.

    Two different sentences, because they are two different mistakes: an empty
    folder is usually the wrong path, and a full one holding nothing usable is
    usually the wrong expectation.
    """
    out = sys.stderr if stream is None else stream
    out.write("\n")
    if is_empty_folder(path):
        out.write('Nothing to do: "%s" is empty.\n' % path)
        out.write("Expected it to hold %s.\n" % what)
    else:
        out.write('Nothing to do: no %s found in "%s".\n' % (what, path))
        out.write("The folder is not empty, but holds nothing this script "
                  "can work with.\n")
    out.write("Nothing was changed.\n")
    return 1


# The status a worker exits with to STOP the queue rather than merely to fail its
# own item: xargs abandons the rest of the queue on 255, and nothing else does.
XARGS_STOP_EXIT_STATUS = 255

# The closing report of a run, named by the shell that owns it so a run stopped
# halfway still recaps the figures it has. It is per-process on purpose: a worker
# that inherited the name would print the parent's report on its own way out.
_FOOTER: dict[str, Any] = {"report": None, "owner": None, "printed": False}


def set_run_footer(report) -> None:
    """``setRunFooter``: name the closing report of this run."""
    _FOOTER["report"] = report
    _FOOTER["owner"] = os.getpid()
    _FOOTER["printed"] = False


def print_run_footer() -> None:
    """Print the closing report, once, from the process that owns it.

    Best effort rather than another thing that can end the run: a failing line in
    the report - a counter a phase never got to write - still leaves the rest of
    it printed, which is what the shell's ``|| true`` buys.
    """
    report = _FOOTER["report"]
    if report is None or _FOOTER["owner"] != os.getpid() or _FOOTER["printed"]:
        return
    _FOOTER["printed"] = True
    try:
        report()
    except Exception:
        pass


def exit_if_aborted(message: str = "Interrupted - stopping.") -> None:
    """Call right after a parallel dispatch, in place of trusting its status.

    A pool's status cannot tell an interrupt from the ordinary per-item failures
    these runs tolerate, so without this an interrupted run falls straight
    through into its next phase over half-finished work.
    """
    if not abort_requested():
        return
    sys.stderr.write("\n%s\n" % message)
    print_run_footer()
    raise SystemExit(INTERRUPTED_EXIT_STATUS)


def trap_run_abort() -> None:
    """Install in the process that OWNS the run: an interrupt records itself,
    says so, prints the closing report and leaves through the ordinary exit - so
    the cleanup that hands the RAM scratch back still runs."""
    def handler(_number, _frame):
        for number in interrupt_signals():
            try:
                signal.signal(number, signal.SIG_DFL)
            except (OSError, ValueError):
                pass
        request_abort()
        sys.stderr.write("\nInterrupted - stopping.\n")
        print_run_footer()
        raise SystemExit(INTERRUPTED_EXIT_STATUS)

    _install(handler)


def trap_worker_abort() -> None:
    """Call at the top of a queue worker. An interrupt stops the QUEUE rather
    than failing one item, and a worker the pool had already dispatched becomes
    an instant no-op."""
    def handler(_number, _frame):
        request_abort()
        raise SystemExit(XARGS_STOP_EXIT_STATUS)

    _install(handler)
    if abort_requested():
        raise SystemExit(XARGS_STOP_EXIT_STATUS)


def interrupt_signals() -> tuple[int, ...]:
    """The signals that mean "stop the run" on this platform: INT and TERM
    everywhere, HUP where the platform has it. A platform without HUP has no
    closed-terminal case to trap, so the absence is the answer, not an error.
    """
    signals: tuple[int, ...] = (signal.SIGINT, signal.SIGTERM)
    hup = getattr(signal, "SIGHUP", None)
    if hup is not None:
        signals += (hup,)
    return signals


def _install(handler) -> None:
    for number in interrupt_signals():
        try:
            signal.signal(number, handler)
        except (OSError, ValueError):      # pragma: no cover - not the main thread
            pass


# --- is this path inside that tree? -------------------------------------------
# The one question every write and every delete in the repo is really asking, and
# it was being answered three different ways: a lexical prefix here, a prefix with
# a separator on it there, a bare startswith somewhere else. One answer, so a site
# that gets it wrong gets it wrong everywhere and is fixed once.


class OutsideTheRun(Exception):
    """A destination the run computed that is not inside the tree it was given.

    Raised rather than returned, for the reason ramscratch refuses a bare string:
    a run that has worked out a path outside its own trees has a bug, and there is
    nothing sensible for the caller to carry on with.
    """


def is_within(root: str, path: str) -> bool:
    """Whether ``path`` is ``root`` itself or sits inside it.

    realpath on both sides, because the question is about where a write LANDS.
    abspath resolves ".." and nothing else, so an output folder that is a SYMLINK
    into the input reads as separate to a lexical comparison and is the same
    directory to the filesystem. normcase because the comparison is a string one
    and Windows' filesystem is not case-sensitive.
    """
    root_real = os.path.normcase(os.path.realpath(root))
    try:
        return os.path.commonpath(
            [root_real, os.path.normcase(os.path.realpath(path))]) == root_real
    except ValueError:
        # commonpath will not mix an absolute path with a relative one, nor two
        # Windows drives. Neither answer is "inside".
        return False


def assert_within(root: str, path: str, what: str = "destination") -> str:
    """``path`` back, or OutsideTheRun. For a destination worked out from data -
    a map lookup, a relative path, a name read out of a file - rather than from
    the walk that found it."""
    if is_within(root, path):
        return path
    raise OutsideTheRun(
        "%s is outside the tree this run was given:\n  tree: %s\n  %s: %s"
        % (what, root, what, path))


# --- the output must not be inside the input ----------------------------------

def output_inside_input(input_dir: str, output_dir: str) -> bool:
    """Whether the output folder is the input, or sits inside it."""
    return is_within(input_dir, output_dir)


def require_separate_output(input_dir: str, output_dir: str,
                            stream=None) -> int:
    """0 when the two folders are separate, and the refusal when they are not.

    What these scripts write to the output is what they look for in the input, so
    a later run would convert its own output - and the cleanup of the input tree
    would reach into the finished output.
    """
    if not output_inside_input(input_dir, output_dir):
        return 0
    out = sys.stderr if stream is None else stream
    out.write("\nRefusing to write the output inside the input:\n")
    out.write("  input:  %s\n" % input_dir)
    out.write("  output: %s\n" % output_dir)
    out.write("What this script writes to the output is what it looks for in "
              "the input, so\na later run would convert its own output - and "
              "the cleanup of the input tree\nwould reach into the finished "
              "output. Give an output folder beside it, not\ninside it.\n")
    out.write("Nothing was changed.\n")
    return 1


# --- the safety log -----------------------------------------------------------
# Where a rename that would have overwritten something is recorded, so the run can
# say at the end what it declined to do. A wrapper that has already opened one
# owns it, and the inner run appends to the same file rather than truncating it.

def init_safety_log(path: str = "") -> str:
    if path:
        os.environ["SAFETY_LOG"] = path
    else:
        inherited = os.environ.get("SAFETY_LOG", "")
        if inherited and os.path.isfile(inherited):
            return inherited
        directory = os.environ.get("TMPDIR", "/tmp")
        path = os.path.join(directory, "safetySkips." + "".join(
            random.choice(_MKTEMP_ALPHABET) for _ in range(8)))
        os.environ["SAFETY_LOG"] = path
    open(path, "w").close()
    return path


def report_safety_skips(stream=None) -> None:
    out = sys.stderr if stream is None else stream
    log_path = os.environ.get("SAFETY_LOG", "")
    lines = []
    if log_path and os.path.isfile(log_path):
        with open(log_path, encoding="utf-8", errors="surrogateescape") as fh:
            # wc -l counts NEWLINES, so a final line with none is not counted -
            # and the report walks the same lines it counted.
            lines = fh.read().split("\n")[:-1]
    out.write("Safety: skipped %d rename(s) to avoid overwrite\n" % len(lines))
    if lines:
        out.write("Safety skip details:\n")
        for line in lines:
            out.write("  %s\n" % line)
