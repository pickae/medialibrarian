"""clean-folder-structure-adb: the name cleaning applied to a phone over adb.

clean-folder-structure delegates here when its target is on a phone whose MTP
mount refuses renames. Where `mv` fails on the gvfs mount there is still a way
in: `adb shell mv` is a metadata-only rename on the phone's own filesystem, so
the file content is never read, rewritten or sent over the cable.

How it stays correct is the mirror trick. The whole cleaning pipeline is
name-only, so this:

1. snapshots the on-device tree once,
2. mirrors it locally, each mirror file's CONTENT being its device-relative path
   - an identity marker,
3. runs the real, unmodified cleaner on that mirror,
4. reads the cleaned mirror back for an exact old -> new mapping, and
5. replays only the real renames on the phone.

All traversal and name logic is therefore local, and there is no MTP/adb cache
incoherence to reason about.
"""

import os
import shutil
import subprocess
import sys
import tempfile

from medialib import commands
from medialib.lib import clioptions, safety
from medialib.lib.runlog import log

USAGE_HEAD = """Usage:
    {program} [options] <inputPath>
Argument:
    <inputPath>   the folder whose names are cleaned on the device.
Options:"""

OPT_SPEC = """
y |  | Sort \"YYYYMMDD ...\" files into YYYY/ subfolders.
d |  | Normalise leading date prefixes to \"YYYYMMDD \".
n |  | Number files by plurality filetype instead of cleaning names.
f | <file> | Read the name fragments to remove from this file.
s |  | Preview only: print the planned renames and DO NOT touch the device.
h |  | Print this help page.
"""

OPT_VARS = "f:fragmentsFile"
OPT_COLUMN = 16
OPT_LONG = ("y:sort-into-years d:fix-dates n:number-files f:fragments s:simulate "
            "h:help")

# The roots a phone's primary storage is usually reachable at, tried in order
# before the ones /storage itself lists.
DEVICE_ROOTS = ("/sdcard", "/storage/emulated/0", "/storage/self/primary")


def spec(program: str) -> clioptions.Spec:
    return clioptions.Spec(
        head=USAGE_HEAD.format(program=program),
        options=OPT_SPEC,
        long=OPT_LONG,
        vars=OPT_VARS,
        column=OPT_COLUMN,
    )


def warn(message: str) -> None:
    sys.stderr.write("!!  %s\n" % message)


class Died(Exception):
    """`die`: a refusal with nothing sensible to carry on with."""


def shell_quote(text: str) -> str:
    """Single-quote a string for the DEVICE's shell, escaping any quotes of its
    own, so arbitrary file names survive the trip intact."""
    return "'" + text.replace("'", "'\\''") + "'"


class Device:
    """Every "device" filesystem touch, so one flow drives both real adb and the
    local backend the tests use."""

    def __init__(self, backend: str, adb: str = "adb"):
        if backend not in ("adb", "local"):
            raise Died("unknown CFS_DEV_BACKEND: %s (expected 'adb' or "
                       "'local')" % backend)
        self.backend = backend
        self.adb = adb

    # --- adb plumbing -----------------------------------------------------
    def _exec_out(self, command: str) -> bytes:
        return subprocess.run([self.adb, "exec-out", command],
                              stdout=subprocess.PIPE).stdout

    def _shell(self, command: str,
               quiet: bool = True) -> subprocess.CompletedProcess:
        return subprocess.run(
            [self.adb, "shell", command],
            stdout=subprocess.DEVNULL if quiet else subprocess.PIPE,
            stderr=subprocess.DEVNULL if quiet else None)

    def _shell_output(self, command: str) -> str:
        proc = subprocess.run([self.adb, "shell", command],
                              stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        return proc.stdout.decode("utf-8", "replace")

    # --- the operations ---------------------------------------------------
    def list_files(self, root: str):
        if self.backend == "local":
            return _walk(root, want_dirs=False)
        payload = self._exec_out("find %s -type f -print0" % shell_quote(root))
        return _nul_split(payload)

    def list_dirs(self, root: str):
        if self.backend == "local":
            return _walk(root, want_dirs=True)
        payload = self._exec_out("find %s -type d -print0" % shell_quote(root))
        return _nul_split(payload)

    def mkdir(self, path: str) -> None:
        if self.backend == "local":
            os.makedirs(path, exist_ok=True)
            return
        self._shell("mkdir -p -- %s" % shell_quote(path))

    def move(self, source: str, destination: str) -> bool:
        if self.backend == "local":
            try:
                shutil.move(source, destination)
                return True
            except OSError:
                return False
        return self._shell("mv -- %s %s" % (shell_quote(source),
                                            shell_quote(destination))
                           ).returncode == 0

    def rmdir(self, path: str) -> None:
        if self.backend == "local":
            try:
                os.rmdir(path)
            except OSError:
                pass
            return
        self._shell("rmdir -- %s" % shell_quote(path))

    def exists(self, path: str) -> bool:
        if self.backend == "local":
            return os.path.exists(path)
        return "Y" in self._shell_output(
            "[ -e %s ] && echo Y" % shell_quote(path))

    def isdir(self, path: str) -> bool:
        if self.backend == "local":
            return os.path.isdir(path)
        return "Y" in self._shell_output(
            "[ -d %s ] && echo Y" % shell_quote(path))


def _walk(root: str, want_dirs: bool):
    """What `find <root> -type d|f -print0` prints, in the same shape."""
    found = []
    if want_dirs:
        found.append(root)
    for parent, dirs, files in os.walk(root):
        if want_dirs:
            found.extend(os.path.join(parent, name) for name in dirs)
        else:
            found.extend(os.path.join(parent, name) for name in files)
    return found


def _nul_split(payload: bytes):
    return [os.fsdecode(entry) for entry in payload.split(b"\0") if entry]


def resolve_device_root(device: Device, fuse_path: str):
    """A gvfs MTP fuse path translated into the equivalent on-device path.

    The part after the storage label is taken and the usual device roots probed
    for it, so no storage-name mapping has to be hard-coded.
    """
    _, marker, tail = fuse_path.partition("/gvfs/mtp:host=")
    if marker:
        tail = tail.partition("/")[2]
    relative = tail.partition("/")[2] if "/" in tail else ""

    candidates = list(DEVICE_ROOTS)
    listing = device._shell_output("ls -1 /storage 2>/dev/null")
    for name in listing.splitlines():
        name = name.rstrip("\r")
        if name and name not in ("self", "emulated"):
            candidates.append("/storage/" + name)

    for candidate in candidates:
        probe = candidate + ("/" + relative if relative else "")
        if device.isdir(probe):
            return probe
    return None


class Plan:
    """The renames, and how much of the plan reached the device.

    Replaying hundreds of renames over adb is slow enough that stopping partway
    is a normal thing to do, and when that happens the device has REAL,
    half-applied changes on it - so the count of what went through is the one
    thing the run must not take with it.
    """

    def __init__(self, dry_run: bool):
        self.dry_run = dry_run
        self.moves = 0
        self.skips = 0
        self.planned = 0
        self._printed = False

    def footer(self) -> None:
        if self._printed:
            return
        self._printed = True
        if self.dry_run:
            log("Preview: %d rename(s) planned on device (nothing changed). "
                "Drop -s to apply." % self.planned)
        else:
            log("Done: %d rename(s) applied on device, %d skipped, of %d "
                "planned" % (self.moves, self.skips, self.planned))


def build_mirror(device: Device, device_root: str, mirror: str):
    """The device tree, mirrored locally with each file's CONTENT being its
    device-relative path - the identity marker the mapping is read back from."""
    directories = device.list_dirs(device_root)
    files = device.list_files(device_root)
    if not files and len(directories) <= 1:
        raise Died('nothing found under "%s" (is the path correct and the '
                   "phone unlocked?)" % device_root)
    log("  %d file(s), %d dir(s)" % (len(files), len(directories)))

    os.makedirs(mirror, exist_ok=True)
    for path in directories:
        relative = _relative_to(path, device_root)
        if relative:
            os.makedirs(os.path.join(mirror, relative), exist_ok=True)
    for path in files:
        relative = _relative_to(path, device_root)
        target = os.path.join(mirror, relative)
        os.makedirs(os.path.dirname(target) or mirror, exist_ok=True)
        with open(target, "w", encoding="utf-8",
                  errors="surrogateescape") as handle:
            handle.write(relative)
    return files, directories


def _relative_to(path: str, root: str) -> str:
    relative = path[len(root):] if path.startswith(root) else path
    return relative.lstrip("/")


def read_mapping(mirror: str):
    """The old -> new mapping, read back out of the cleaned mirror, and the set
    of directories the cleaning left behind."""
    mapping = {}
    for parent, _dirs, names in os.walk(mirror):
        for name in names:
            path = os.path.join(parent, name)
            with open(path, encoding="utf-8", errors="surrogateescape") as fh:
                old = fh.read()
            mapping[old] = os.path.relpath(path, mirror)
    directories = set()
    for parent, dirs, _files in os.walk(mirror):
        for name in dirs:
            directories.add(os.path.relpath(os.path.join(parent, name), mirror))
    return mapping, directories


def replay(device: Device, device_root: str, mapping, plan: Plan) -> None:
    """Print the plan and, unless this is a preview, apply it.

    In SORTED order, not the mapping's own. A bash associative array iterates in
    hash order, so without this the plan came out shuffled and an A -> B, B -> C
    chain resolved differently from run to run. Sorting does not untangle such a
    chain - it makes the outcome the same every time and the plan readable,
    which is what a plan is for.
    """
    ensured = {"."}
    for old in sorted(mapping, key=os.fsencode):
        new = mapping[old]
        if old == new:
            continue
        plan.planned += 1
        sys.stderr.write("  RENAME  %s\n          -> %s\n" % (old, new))
        if plan.dry_run:
            continue

        parent = os.path.dirname(new) or "."
        if parent not in ensured:
            device.mkdir(device_root + "/" + parent)
            ensured.add(parent)
        if device.exists(device_root + "/" + new):
            warn("target already exists, skipping (no overwrite): " + new)
            plan.skips += 1
            continue
        if device.move(device_root + "/" + old, device_root + "/" + new):
            plan.moves += 1
        else:
            warn("move failed, left in place: " + old)
            plan.skips += 1


def prune(device: Device, device_root: str, directories, kept) -> None:
    """Directories the cleaning emptied out, deepest first - best effort, since
    rmdir only removes one that is actually empty, so nothing is ever lost."""
    relatives = sorted(
        (_relative_to(path, device_root) for path in directories),
        key=lambda value: value.count("/"), reverse=True)
    for relative in relatives:
        if not relative or relative in kept:
            continue
        device.rmdir(device_root + "/" + relative)


def main(argv: list, program: str = "clean-folder-structure-adb",
         script_dir: str = "") -> int:
    declaration = spec(program)
    try:
        result = clioptions.parse(declaration, argv)
    except clioptions.HelpRequested:
        sys.stdout.write(clioptions.help_text(declaration))
        return 0
    except clioptions.UsageError as error:
        sys.stderr.write(clioptions.usage_error_text(declaration,
                                                     error.message))
        return 1

    forward = []
    for letter in ("y", "d", "n"):
        if letter in result.given:
            forward.append("-" + letter)
    if result.values["fragmentsFile"]:
        forward += ["-f", result.values["fragmentsFile"]]
    dry_run = "s" in result.given

    script_dir = script_dir or commands.script_dir()

    try:
        if len(result.positionals) != 1:
            raise Died("expected exactly one path; got %d"
                       % len(result.positionals))
        input_path = result.positionals[0].rstrip("/")
        if not input_path:
            raise Died("empty path")

        backend = os.environ.get("CFS_DEV_BACKEND", "adb")
        device = Device(backend, os.environ.get("ADB", "adb"))

        if backend == "adb":
            if shutil.which(device.adb) is None:
                raise Died("this path is on an MTP phone but 'adb' was not "
                           "found. Install adb and enable USB debugging.")
            attached = _attached_devices(device)
            if attached != 1:
                raise Died("need exactly one attached device (found %d). Check "
                           "'adb devices' and enable USB debugging." % attached)

        if backend == "adb" and "/gvfs/mtp:host=" in input_path:
            log("Resolving on-device path for MTP mount ...")
            device_root = resolve_device_root(device, input_path)
            if not device_root:
                raise Died('could not locate "%s" on the device via adb. Pass '
                           "the on-device path (e.g. /sdcard/...) or check the "
                           "mount." % input_path)
            log("  -> " + device_root)
        else:
            device_root = input_path
            if backend == "local" and not os.path.isdir(device_root):
                raise Died("local backend: not a directory: " + device_root)

        plan = Plan(dry_run)
        sandbox = tempfile.mkdtemp()
        try:
            _install_interrupt(plan)
            mirror = os.path.join(sandbox, "root")
            log('Snapshotting device tree under "%s"' % device_root)
            _files, directories = build_mirror(device, device_root, mirror)

            log("Cleaning names on a local mirror%s"
                % (" (flags: %s)" % " ".join(forward) if forward else ""))
            commands.run_command("clean-folder-structure",
                                 forward + [mirror],
                                 script_dir=script_dir)

            mapping, kept = read_mapping(mirror)
            replay(device, device_root, mapping, plan)
            if not dry_run:
                prune(device, device_root, directories, kept)
            plan.footer()
            return 0
        finally:
            shutil.rmtree(sandbox, ignore_errors=True)
    except Died as died:
        sys.stderr.write("error: %s\n" % died)
        return 1


def _attached_devices(device: Device) -> int:
    proc = subprocess.run([device.adb, "devices"], stdout=subprocess.PIPE)
    lines = proc.stdout.decode("utf-8", "replace").splitlines()[1:]
    return sum(1 for line in lines if line.split()[1:2] == ["device"])


def _install_interrupt(plan: Plan) -> None:
    """Report what reached the device, then leave through the ordinary exit so
    the scratch mirror still goes with it.

    The handler is unhooked first so a second Ctrl+C gets out immediately: over
    adb, a device that has been unplugged can make a command hang for a long
    time.
    """
    import signal

    def handler(_number, _frame):
        for number in safety.interrupt_signals():
            try:
                signal.signal(number, signal.SIG_DFL)
            except (OSError, ValueError):
                pass
        sys.stderr.write("\nInterrupted - stopping.\n")
        plan.footer()
        raise SystemExit(130)

    for number in safety.interrupt_signals():
        try:
            signal.signal(number, handler)
        except (OSError, ValueError):
            pass


def cli(argv: list | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    return main(argv, program=commands.program_name(__spec__.name),
                script_dir=commands.script_dir())


if __name__ == "__main__":
    sys.exit(cli())
