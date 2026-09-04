"""The "which ffmpeg does this run use" decision.

Every script here calls ffmpeg and ffprobe by name and takes whatever PATH
resolves them to, which is two different answers on one machine: the build the
interactive shell finds, and - on the stripped PATH a cron job, a systemd unit
or a file-manager action runs with - often none at all. This settles it once
per run, and puts the chosen pair where every later call finds it: the
script's own, the shared libraries', and the ones inside the exported
functions the parallel workers run, none of which have to know a choice was
made. The decision travels to every child in the one thing children already
inherit - PATH - so nothing here is exported as a name.

The state the shell keeps per run - ``selectedFfmpeg``, ``selectedFfmpegFull``,
``ffmpegPinnedOnPath`` - is a per-process ``_State``, which a test resets with
:func:`reset_state`. ``ffmpegOverride`` and ``usage``
are read from the environment the way the shell reads the caller's variables,
and the scratch the chosen pair is reached through is the one
:mod:`medialib.lib.ramscratch` claims.
"""

from __future__ import annotations

import os
import subprocess
import sys

from medialib.lib import ramscratch

__all__ = [
    "FfmpegOverrideRefused",
    "reset_state",
    "ffmpeg_candidates",
    "select_ffmpeg",
    "report_ffmpeg_selection",
    "ffmpeg_version",
]


class FfmpegOverrideRefused(Exception):
    """``ffmpegOverride`` names something that is not an executable file.

    An override is an instruction, not a preference: the refusal is the
    answer, and the run does not go ahead on a build it was told not to use.
    """

class _State:
    def __init__(self):
        # The binary the run settled on ("" when there is none), 1 when it
        # satisfied the caller's probe (1 when there was none to satisfy),
        # and 1 when PATH was changed - i.e. the run is NOT using the ffmpeg
        # the surrounding shell would have used.
        self.selected = ""
        self.full = 0
        self.pinned = 0


_STATE = _State()


def reset_state():
    _STATE.selected = ""
    _STATE.full = 0
    _STATE.pinned = 0


def _executable(path: str) -> bool:
    """The shell's ``-x``: the execute bit resolves. A DIRECTORY with the bit
    set passes - which is exactly the shape ``command -v`` itself refuses, so
    the two checks are not the same check."""
    try:
        return os.access(path, os.X_OK)
    except (OSError, ValueError):
        return False


def _command_v(name: str, path: str) -> str | None:
    """The shell's ``command -v <name>``: the first PATH entry holding a file
    by that name - a REGULAR file, a link to one, whether or not it is
    executable; a directory wearing the name and a broken link are not
    commands. An empty entry is the current directory, and its answer is the
    relative spelling the shell prints. ``""`` when nothing is found."""
    for entry in path.split(os.pathsep):
        if entry:
            candidate = entry + "/" + name
            if os.path.isfile(candidate):
                return candidate
        elif os.path.isfile(os.path.join(os.getcwd(), name)):
            return "./" + name
    return ""


def ffmpeg_candidates(path: str | None = None, home: str | None = None) -> list[str]:
    """The ffmpeg binaries worth considering on this machine, best-known
    first.

    PATH comes first deliberately: an ffmpeg somebody put on this run's PATH
    is a deliberate choice - a test's stub, a wrapper, a chosen build - and
    must not be second-guessed while it can do the job. The conventional
    places a hand-installed build goes are looked at only when it cannot. The
    distro's own ``/usr/bin`` comes last, because it is the OLDEST build on
    the machine, which is what makes it worth reaching: a hand-installed
    build can be compiled against an NVENC SDK newer than the installed
    driver and refuse the encoder the packaged one drives. A path reachable
    two ways is offered once - a duplicate costs a whole extra probe encode
    per repeat.

    ``/opt/homebrew/bin`` is in the list for the same reason the others are:
    it is Homebrew's prefix on Apple Silicon, and a run started from a
    launchd job or a Finder action gets a PATH without it. Intel Macs need no
    entry of their own - Homebrew's prefix there is ``/usr/local``, which was
    already on the list. Neither exists on Linux, and a candidate that is not
    there costs nothing: the ladder tests each one before offering it.
    """
    if path is None:
        path = os.environ.get("PATH", "")
    if home is None:
        home = os.environ.get("HOME", "")
    seen = []
    for c in (_command_v("ffmpeg", path),
              home + "/.local/bin/ffmpeg",
              "/opt/homebrew/bin/ffmpeg",
              "/usr/local/bin/ffmpeg",
              "/opt/ffmpeg/bin/ffmpeg",
              "/usr/bin/ffmpeg"):
        if c and _executable(c) and c not in seen:
            seen.append(c)
    return seen


def select_ffmpeg(probe=None) -> int:
    """Settle on the build this run uses, and make every later ffmpeg and
    ffprobe call reach it.

    ``probe`` is a callable taking a candidate binary and answering true when
    that build can do what this run needs of it - the shell's probe
    FUNCTION, by value. Candidates are tried in order and the first that
    satisfies it wins; without one, the first that exists wins, which on any
    machine with an ffmpeg on PATH is that one - the ladder then only
    decides the case where PATH has none. A build that is too old in the
    quiet way - an encoder parameter it logs once and ignores - looks
    exactly like one that applies everything, so the caller that cares asks
    the encoder rather than the version number.

    The chosen pair is reached through a scratch directory holding just
    those two names, at the front of PATH, skipped entirely when the winner
    is already what PATH resolves to. Raises
    :class:`FfmpegOverrideRefused` - having said so on stderr - when
    ``ffmpegOverride`` names nothing executable.
    """
    # A run decides this once: a caller that asks again gets its answer (a
    # probe re-run against the settled build) and nothing else - a second
    # choice would mean a second scratch directory and a second PATH entry.
    if _STATE.selected:
        if probe is not None:
            _STATE.full = 1 if probe(_STATE.selected) else 0
        return 0

    _STATE.selected = ""
    _STATE.full = 0
    _STATE.pinned = 0

    override = os.environ.get("ffmpegOverride", "")
    if override:
        # An override is an instruction, not a preference: the named build
        # is the one the run uses, whether or not it can do everything.
        # Searching past it would make the variable mean "a suggestion", and
        # there would then be no way to pin a build at all.
        if not _executable(override):
            sys.stderr.write(
                f'ffmpegOverride names "{override}", which is not an '
                f"executable file.\n")
            usage = os.environ.get("usage", "")
            if usage:
                sys.stderr.write(f"\n{usage}\n")
            raise FfmpegOverrideRefused(override)
        _STATE.selected = override
        if probe is None or probe(override):
            _STATE.full = 1
    else:
        first_working = ""
        for candidate in ffmpeg_candidates():
            if not first_working:
                first_working = candidate
            if probe is None or probe(candidate):
                _STATE.selected = candidate
                _STATE.full = 1
                break
        # No candidate satisfies the probe - no GPU for a hardware profile,
        # or every build too old for the parameters asked for. The run still
        # goes ahead on the first one that exists, and the caller's summary
        # says so rather than the encoder saying it once per chunk.
        if not _STATE.selected:
            _STATE.selected = first_working

    path = os.environ.get("PATH", "")
    if _pin_on_path(_STATE.selected, path):
        _STATE.pinned = 1
    return 0


def selected_full(state=_STATE) -> int:
    """``selectedFfmpegFull``: 1 when the chosen build satisfied the caller's
    probe - and 1 when there was no probe to satisfy.

    A caller that asks for more than a distribution's package can do wants to say
    so once, at startup: a build that silently drops half a parameter string
    produces a perfectly good encode that is simply not the one that was asked
    for.
    """
    return state.full


def _pin_on_path(selected: str, path: str) -> bool:
    """Put the chosen pair where every later call finds it: a directory of
    this run's own holding just those two names, at the front of PATH - in
    the script, in the shared libraries, in the exported functions the
    workers run, none of which have to know a choice was made, and nothing
    else on PATH is shadowed because the directory holds nothing else.

    Skipped entirely when the winner is already what PATH resolves to - the
    common case - and the environment is left untouched. Answers True when
    PATH was changed."""
    if not selected or selected == _command_v("ffmpeg", path):
        return False
    # The base is claimed here, in this process, not left to the
    # directory call: the cleanup registration belongs to the run, and a
    # child process that claimed it would hand the directory back the
    # moment it ended - taking the symlinks with it.
    ramscratch.init_ram_base()
    tool_dir, _status = ramscratch.ram_scratch_dir("tools")
    ramscratch.add_exit_cleanup([tool_dir])
    os.symlink(selected, tool_dir + "/ffmpeg")
    # ffprobe from the same build, since the two are versioned together;
    # PATH's own is kept when this build did not bring one.
    prefix = selected.rsplit("/", 1)[0] if "/" in selected else selected
    sibling = prefix + "/ffprobe"
    if _executable(sibling):
        os.symlink(sibling, tool_dir + "/ffprobe")
    else:
        found = _command_v("ffprobe", path)
        if found:
            os.symlink(found, tool_dir + "/ffprobe")
    os.environ["PATH"] = tool_dir + os.pathsep + path
    return True


def ffmpeg_version(binary: str) -> str:
    """The build's version field: the third word of the first line of
    ``-hide_banner -version`` - the shell's ``head -n1 | cut -d' ' -f3``,
    which, cut having no ``-s``, hands the whole line back when it holds
    fewer than three words: the tell a stub or a broken build gives."""
    try:
        proc = subprocess.run(
            [binary, "-hide_banner", "-version"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    except OSError:
        return ""
    text = proc.stdout.decode("utf-8", "replace")
    line = text.split("\n", 1)[0] if text else ""
    parts = line.split(" ")
    if len(parts) >= 3:
        return parts[2]
    return line


def report_ffmpeg_selection(always: bool = False) -> int:
    """Say which build the run settled on, on stderr.

    Silent by default unless the choice CHANGED something - the ladder
    overruled what the surrounding shell would have run. That case is worth
    a line because it is invisible otherwise: the person reading the output
    would reproduce the command by hand and get a different binary. The
    ordinary case says nothing, because a line per run stating the obvious
    is how output stops being read. ``always`` prints the line regardless,
    for a script whose startup summary restates every decision it took.
    """
    if not _STATE.selected:
        return 0
    if not always and _STATE.pinned != 1:
        return 0
    # The real binary is named, not the run's symlink to it: the symlink
    # path says nothing about which build this is, and which build it is is
    # the point of the line.
    version = ffmpeg_version(_STATE.selected)
    sys.stderr.write(
        "Using ffmpeg: %s (%s)\n"
        % (_STATE.selected, version or "version not reported"))
    return 0