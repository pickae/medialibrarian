"""The shared RAM-backed scratch helpers.

The shell module keeps three pieces of state per shell: the exported
``ramBase`` (this run's scratch root), the ``EXIT_CLEANUP_PATHS`` list the EXIT
trap releases, and the EXIT trap itself - and ``export -f`` lets a child
process re-enter the same functions against the inherited base. The port keeps
the same three in a per-process ``_State``, which a test resets with
``reset_state``, and the functions read the environment the way the shell
functions read ``$ramScratchBase`` and friends.

Nothing here shells out: the module picks a base, measures free space and
releases its trees itself, because the coreutils the shell reached for are not
on a default Windows PATH and each has a standard-library equivalent. What no
library can supply there is a RAM disk, so on Windows the base lands in the
temporary directory, on disk.

macOS lands there too, and for the same reason: it has no ``/dev/shm`` and no
tmpfs of its own, so the ladder below falls through to ``$TMPDIR`` - which on a
Mac is already a per-user directory, on disk. A RAM disk made by hand
(``hdiutil attach -nomount ram://...`` and ``diskutil erasevolume``) is used by
pointing ``ramScratchBase`` at it, the same as a third-party one on Windows.
What is NOT applied to either is the share cap: that comes from naming the
filesystem tmpfs, and only a mount table can say that (see
:func:`filesystem_type`), so a hand-made RAM disk gets the plain free-space
figure instead.
"""

import os
import re
import shutil
import stat as stat_module
import tempfile

from medialib import commands
from medialib.lib import hostos, safety

_INT = re.compile(r"^[0-9]+$")

# The RAM-backed filesystem a Linux host has. Named rather than written into
# init_ram_base, because a test about WHERE a run lands has to be able to stand
# a directory of its own in its place: the machine's real one is shared with
# every other process on it.
SHM = "/dev/shm"

# The mount table, which is what names a filesystem "tmpfs" rather than a magic
# number - statfs has the answer but Python exposes no f_type.
MOUNTS = "/proc/self/mounts"

# How much of a tmpfs one intermediate may occupy, and the headroom below
# which even a fresh scratch root goes to disk, when the environment does not
# say otherwise (the shell's ${ramScratchMaxPercent:-50} and
# ${ramScratchMinFreeBytes:-1073741824}).
DEFAULT_MAX_PERCENT = 50
DEFAULT_MIN_FREE = 1073741824


class _State:
    def __init__(self, script=""):
        # The exported ramBase: this run's scratch root, or None while the run
        # has not chosen one.
        self.ram_base = None
        # EXIT_CLEANUP_PATHS: what the EXIT trap must hand back. One list per
        # shell is the whole point of the shell design - a sourced script's
        # trap replaces the wrapper's, and a child process starts with none -
        # and the port keeps both halves of that: the list is per-process and
        # a fresh process starts with an empty one.
        self.cleanup = []
        # The EXIT trap's command, or None while nothing has claimed EXIT.
        self.trap = None
        # Every parent this run has made scratch under: the run directory and,
        # when the tmpfs filled up, whichever disk parent it spilled to. The
        # cleanup acts on nothing outside them.
        self.bases = []
        # ${0##*/}: the name the run directory is named after, or "" while
        # nobody has said and the command's own name answers.
        self.script = script


_STATE = _State()


def reset_state(script=""):
    # Mutated rather than replaced: the functions' default state argument
    # binds the object at def time, and a fresh object would leave them
    # working on a list no test can see.
    _STATE.ram_base = None
    _STATE.cleanup = []
    _STATE.bases = []
    _STATE.trap = None
    _STATE.script = script


def _mkdtemp(parent, prefix):
    """A fresh directory under ``parent``, or "" when one cannot be made.

    The shell read mktemp's exit status and so do the callers here, through the
    empty name: which errno it was is not something any of them can act on, and
    a parent that has filled up or gone away is the ordinary case rather than
    an exceptional one.
    """
    try:
        return tempfile.mkdtemp(prefix=prefix, dir=parent)
    except OSError:
        return ""


def ram_base_usable(directory):
    """True when a directory can actually hold this run's scratch. Probed by
    CREATING something and removing it again, not by asking -d and -w, which
    can both say yes about a directory that then refuses every mkdir.
    """
    probe = _mkdtemp(directory, ".ramProbe.")
    if not probe:
        return False
    try:
        os.rmdir(probe)
    except OSError:
        pass
    return True


def init_ram_base(override="", state=_STATE):
    """Pick the RAM-backed base directory for this run's temporary files: the
    argument, then $ramScratchBase, then /dev/shm, then $TMPDIR or the
    platform's temporary directory, the first that the create probe accepts.
    ONE DIRECTORY PER RUN, named after the script, so a leftover says which
    run abandoned it.

    Already having a usable base is not overridden - a sourced script
    re-initialising or a child process that inherited it works inside the run
    directory that exists instead of making a second one. Only the shell that
    CREATED the directory registers it for cleanup, and the EXIT trap is
    installed only when nothing has claimed it yet, so a wrapper's own trap
    survives.
    """
    if state.ram_base and ram_base_usable(state.ram_base):
        return
    if override and ram_base_usable(override):
        root = override
    else:
        env_base = os.environ.get("ramScratchBase", "")
        if env_base and ram_base_usable(env_base):
            root = env_base
        elif ram_base_usable(SHM):
            root = SHM
        else:
            # /tmp is not a path on Windows, and neither it nor macOS has a
            # second RAM-backed filesystem to try: the temporary directory is
            # the last resort on both.
            root = os.environ.get("TMPDIR", "") or tempfile.gettempdir()
    name = state.script or commands.current_program()
    run_dir = _mkdtemp(root, name + ".run.")
    if not run_dir:
        # The root probed usable a moment ago, so this is a race or a full
        # tmpfs. Working in the root itself loses the isolation, but a run
        # that starts beats a run that refuses to.
        state.ram_base = root
        state.bases.append(root)
        return
    state.ram_base = run_dir
    state.bases.append(run_dir)
    state.cleanup.append(run_dir)
    if state.trap is None:
        state.trap = "runExitCleanup"


def ram_base(state=_STATE):
    """The base this process settled on, for handing to a child that has none."""
    return state.ram_base or ""


def adopt_ram_base(base, state=_STATE):
    """Work inside a base a PARENT settled, the way a child shell inherits the
    exported ``ramBase`` and ``initRamBase`` returns early on it. A worker
    process shares no module state, so without this it settles a base of its own
    and leaves a second run directory behind.

    Registers no cleanup, for the reason the shell does not either: only the
    process that CREATED a directory hands it back.
    """
    if base:
        state.ram_base = base
        state.bases.append(base)


def _unescape_mount(field):
    """One mount-table field with its octal escapes read back.

    A mount point with a space in it is written ``\\040`` there, and a table
    read literally would then not match the directory being asked about.
    """
    if "\\" not in field:
        return field
    out = []
    index = 0
    while index < len(field):
        char = field[index]
        if char == "\\" and field[index + 1:index + 4].isdigit():
            out.append(chr(int(field[index + 1:index + 4], 8)))
            index += 4
        else:
            out.append(char)
            index += 1
    return "".join(out)


def _under(target, point):
    """Whether ``target`` is the mount point ``point`` or sits inside it."""
    if target == point:
        return True
    return target.startswith(point.rstrip(os.sep) + os.sep)


def filesystem_type(directory):
    """The name ``stat -f -c %T`` prints for the filesystem holding
    ``directory``, or "" when the host cannot say.

    Read from the mount table by longest matching mount point, over the
    RESOLVED path: ``stat -f`` follows a symlink to the filesystem it lands on,
    and a scratch root reached through one is on that filesystem too.

    A host with no
    mount table - Windows, macOS, a container built without /proc - has no
    tmpfs to find either, and "" is read below as "an ordinary disk".
    """
    target = os.path.realpath(directory)
    best = ""
    found = ""
    try:
        with open(MOUNTS, encoding="utf-8", errors="replace") as handle:
            for line in handle:
                fields = line.split()
                if len(fields) < 3:
                    continue
                point = _unescape_mount(fields[1])
                if _under(target, point) and len(point) >= len(best):
                    best = point
                    found = fields[2]
    except OSError:
        return ""
    return found


def _disk_usage(directory):
    """The total and free bytes of the filesystem holding ``directory``.

    Its own function so the white box can stand a filesystem of a chosen size
    in: what the share calculation below does with a size is the point.
    """
    return shutil.disk_usage(directory)


def ram_dir_free_bytes(directory):
    """How many bytes <directory> may still be given, or None when that cannot
    be worked out (the callers read that as "do not claim it fits").

    A share of the file system rather than its free space when the share
    applies at all: the space is memory, occupied for as long as the
    intermediate lives, and systemd's per-UID quota fails a write that the
    free-space figure is still reporting room for. On a disk base the ordinary
    free space is the whole answer - and so is it on a host that cannot name its
    filesystem at all, which is why a Windows RAM disk gets no share applied.
    """
    if not os.path.isdir(directory):
        return None
    try:
        usage = _disk_usage(directory)
    except OSError:
        return None
    if filesystem_type(directory) in ("tmpfs", "ramfs"):
        raw = os.environ.get("ramScratchMaxPercent", "") or str(DEFAULT_MAX_PERCENT)
        if _INT.match(raw) and 1 <= int(raw) <= 100:
            pct = int(raw)
        else:
            pct = DEFAULT_MAX_PERCENT
        # total/100 first, so the multiplication cannot overflow on a large
        # tmpfs; the rounding it costs is under a hundred bytes.
        capped = usage.total // 100 * pct
        return min(capped, usage.free)
    return usage.free


def _user_cache_home():
    """Where this platform keeps large regenerable per-user data.

    ``$XDG_CACHE_HOME``'s own default on POSIX, ``%LOCALAPPDATA%`` on Windows,
    which has no ``~/.cache``, and ``~/Library/Caches`` on macOS, which is the
    directory the system's own housekeeping knows to look in. Through
    expanduser rather than through ``$HOME``, which a default Windows shell
    does not set.

    ``$XDG_CACHE_HOME`` still wins over all three where it is set - the caller
    reads it first - so a Mac that has been set up the XDG way keeps that.
    """
    if os.name == "nt":
        local = os.environ.get("LOCALAPPDATA", "")
        if local:
            return local
        return os.path.join(os.path.expanduser("~"), ".cache")
    if hostos.is_macos():
        return os.path.join(os.path.expanduser("~"), "Library", "Caches")
    return os.path.join(os.path.expanduser("~"), ".cache")


def ram_disk_base():
    """The disk to spill onto when the tmpfs cannot take something: under the
    user's cache directory, where large regenerable data belongs, and one known
    place to look after a run was killed hard enough to skip its cleanup.
    None when nothing usable can be made, which every caller reads as "stay in
    RAM and let the write fail there"."""
    xdg = os.environ.get("XDG_CACHE_HOME", "")
    root = xdg or _user_cache_home()
    base = os.environ.get("ramScratchDiskBase", "") or os.path.join(
        root, "ramScratchOverflow")
    if not base:
        return None
    try:
        os.makedirs(base, exist_ok=True)
    except OSError:
        return None
    if not ram_base_usable(base):
        return None
    return base


def ram_scratch_dir(label, state=_STATE, log=print):
    """A fresh scratch DIRECTORY under $ramBase, echoed (returned with its
    status, the way the shell function's stdout and exit are). The label
    becomes the name's prefix, so a leftover names the purpose it belonged to.

    A scratch root asked for while the tmpfs is already out of usable room
    goes to disk instead and says so in the log, having no other channel:
    every caller reads this through a command substitution.
    """
    if not state.ram_base:
        init_ram_base("", state=state)
    free = ram_dir_free_bytes(state.ram_base)
    raw_min = os.environ.get("ramScratchMinFreeBytes", "") or str(DEFAULT_MIN_FREE)
    if free is not None and _INT.match(raw_min) and free <= int(raw_min):
        disk_parent = ram_disk_base()
        if disk_parent is not None:
            spill = _mkdtemp(disk_parent, "." + label + "Scratch.")
            if spill:
                state.bases.append(disk_parent)
                log('  RAM scratch is full, putting "%s" on disk instead: %s'
                    % (label, spill))
                return spill, 0
    made = _mkdtemp(state.ram_base, label + ".")
    if not made:
        return "", 1
    return made, 0


def ram_scratch_file(label, state=_STATE):
    """The same for a single file (queues, counters, progress and plan
    files), created empty."""
    if not state.ram_base:
        init_ram_base("", state=state)
    try:
        handle, path = tempfile.mkstemp(prefix=label + ".", dir=state.ram_base)
    except OSError:
        return "", 1
    os.close(handle)
    return path, 0


def ram_scratch_dir_for(byte_count, label, disk_parent="", state=_STATE):
    """A fresh scratch directory that can actually hold <byte_count> bytes -
    under $ramBase when the tmpfs can take it, otherwise a hidden one under
    <diskParent>, or under the cache spill base when the caller names none.

    Returns (directory, on_disk, status): on_disk is 1 when that is the disk
    fallback, the second half of what a shell function whose answer is only
    its stdout cannot also report (RAM_SCRATCH_DIR / RAM_SCRATCH_ON_DISK).
    An unusable disk parent falls back to RAM rather than failing: a write
    that then runs out of space is a failure the callers already handle by
    leaving their input alone.
    """
    if not state.ram_base:
        init_ram_base("", state=state)
    if _INT.match(byte_count):
        free = ram_dir_free_bytes(state.ram_base)
        if free is not None and int(byte_count) > free:
            if not disk_parent:
                disk_parent = ram_disk_base() or ""
            if disk_parent:
                spill = _mkdtemp(disk_parent, "." + label + "Scratch.")
                if spill:
                    state.bases.append(disk_parent)
                    return spill, 1, 0
    made = _mkdtemp(state.ram_base, label + ".")
    if not made:
        return "", 0, 1
    return made, 0, 0


def _paths(paths):
    """The registry takes a LIST, and a bare string is refused rather than
    iterated.

    The shell's addExitCleanup is varargs, so `addExitCleanup "$dir"` registers
    one path; the same call written against this signature registers one path
    PER CHARACTER, and one of those characters is "/". What follows is
    `chmod -R u+rwX /` and then an attempted `rm -rf /`. A TypeError at the call
    site is the only acceptable answer to that.
    """
    if isinstance(paths, (str, bytes, os.PathLike)):
        raise TypeError(
            "add_exit_cleanup takes a list of paths, not one path: "
            "a string would be registered one character at a time")
    return list(paths)


def _trimmed(path):
    """A path with its trailing separators taken off, for comparing two
    spellings of the same directory."""
    return os.path.normcase(path).rstrip("/" + os.sep) or os.path.normcase(path)


def _is_root(path):
    """Whether this is the top of a filesystem - "/" or "//" on POSIX, "C:\\"
    on Windows - which nothing below it is."""
    _drive, rest = os.path.splitdrive(path)
    return not rest.strip("/" + os.sep)


def _refuses(path, state=_STATE) -> bool:
    """Whether this is a path no cleanup may ever act on.

    The filesystem root and the user's home are not scratch, and a run that has
    somehow come to hold one of them is a run with a bug rather than one with
    housekeeping to do. Removal refuses the root itself, but granting the owner
    write access over a whole tree refuses nothing, and it is what runs FIRST.

    Then the rule those two are special cases of: the cleanup may only remove
    what this run MADE, so a path outside every base it has taken scratch under
    is refused whatever it is - a registration that has gone wrong strands a
    temporary directory rather than removing a tree the run never owned.
    """
    if not isinstance(path, str) or not path.strip():
        return True
    resolved = os.path.abspath(path)
    if _is_root(resolved):
        return True
    if _trimmed(resolved) == _trimmed(os.path.expanduser("~")):
        return True
    return not any(safety.is_within(base, path) for base in state.bases)


def add_exit_cleanup(paths, state=_STATE):
    """Release these when this shell exits, however it exits."""
    state.cleanup.extend(_paths(paths))


def _grant_owner_access(path, is_dir):
    """``u+rwX`` on one entry: read and write always, and execute where
    something already has it - which for a directory is always, because a
    directory needs it before its own entries can be reached at all.

    Symlinks are left alone, the way ``chmod -R`` leaves them: POSIX has no
    mode on the link itself, and following one would change a file outside the
    tree being released.
    """
    try:
        mode = os.stat(path, follow_symlinks=False).st_mode
    except OSError:
        return
    want = stat_module.S_IRUSR | stat_module.S_IWUSR
    if is_dir or mode & (stat_module.S_IXUSR | stat_module.S_IXGRP
                         | stat_module.S_IXOTH):
        want |= stat_module.S_IXUSR
    if mode & want == want:
        return
    try:
        os.chmod(path, stat_module.S_IMODE(mode) | want)
    except OSError:
        pass


def _grant_tree_access(path):
    """``chmod -R u+rwX``: enough of the owner's own access back to remove the
    tree. An archive can extract read-only directories, and each one is granted
    before it is LISTED - a directory without u+rx cannot be read to find out
    what is inside it.

    Iterative rather than recursive, because the depth of a tree a run is
    handing back is not something this can assume anything about.
    """
    pending = [path]
    while pending:
        current = pending.pop()
        if os.path.islink(current):
            continue
        if not os.path.isdir(current):
            _grant_owner_access(current, is_dir=False)
            continue
        _grant_owner_access(current, is_dir=True)
        try:
            entries = list(os.scandir(current))
        except OSError:
            continue
        for entry in entries:
            if entry.is_symlink():
                continue
            if entry.is_dir(follow_symlinks=False):
                pending.append(entry.path)
            else:
                _grant_owner_access(entry.path, is_dir=False)


def _release(path):
    """One registered path handed back, whatever it turned out to be.

    Failures are swallowed, because this runs while the process is already
    leaving, and a path on a mount that has gone away must not turn a finished
    run into a failed one.
    """
    _grant_tree_access(path)
    if os.path.isdir(path) and not os.path.islink(path):
        shutil.rmtree(path, ignore_errors=True)
        return
    try:
        os.remove(path)
    except OSError:
        pass


def run_exit_cleanup(state=_STATE):
    """Release everything registered, once. Belongs in the EXIT trap."""
    if not state.cleanup:
        return 0
    for path in state.cleanup:
        if _refuses(path, state):
            continue
        if path and os.path.exists(path):
            _release(path)
    state.cleanup = []
    return 0


def release_exit_cleanup(targets, state=_STATE):
    """Hand back just these, now, and take them off the list so the EXIT trap
    does not go looking for them again. For the scripts a wrapper SOURCES:
    releasing the whole list at the end of each of them would take the
    wrapper's own scratch with it - and that scratch is the very tree the next
    sub-folder is read from."""
    for target in _paths(targets):
        if _refuses(target, state):
            continue
        if target and os.path.exists(target):
            _release(target)
    gone = set(targets)
    state.cleanup = [p for p in state.cleanup if p not in gone]
    return 0
