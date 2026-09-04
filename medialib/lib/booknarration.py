"""Turning one e-book into one narrated audiobook file, driven out of a local
ebook2audiobook checkout.

The command is one line - ``app.py --headless --ebook X --output_dir Y`` - and this
module is the environment that line is handed: which checkout, which interpreter,
which device, a venv the interpreter runs in, and the voice sample the engine
clones from. The interpreter and the tools it reaches for (nvidia-smi, pyenv,
ffmpeg, ffprobe, the venv python) are called through the process boundary, so a
test stands the shared tool stub in for each and asserts on the dispatch: which
tool, with which arguments, and what the module made of the answer.

The state the shell keeps in exported globals (narrationHome, narrationPython,
narrationDevice, narrationEnvReady) lives in the environment here, and the
configuration the shell sets at source time is read from the environment with its
source-time default, so a caller that never initialised anything still gets the
answers the module would have given.
"""

import os
import re
import shutil
import stat
import subprocess
import sys

from medialib.lib.booklanguage import book_language_code
from medialib.lib.enums import lower_extension_of, shell_lower
from medialib.lib.formatting import awk_gt, awk_number, fmt_clock
from medialib.lib.newestfile import newest_file

__all__ = [
    "narration_supports_language",
    "narration_language_list",
    "narration_has_nvidia",
    "narration_free_vram",
    "narration_job_budget",
    "narration_base_python",
    "narration_ensure_venv",
    "narration_package_series",
    "narration_wanted_torchcodec",
    "narration_ensure_torchcodec",
    "narration_install_soundfile_fallback",
    "narration_remove_soundfile_fallback",
    "narration_fix_device_detection",
    "narration_drop_empty_unidic",
    "init_book_narration",
    "narrate_book",
    "narration_progress",
    "narration_lossless_master",
    "narration_session_dir",
    "narration_drop_session",
    "voice_sample_window",
    "prepare_voice_sample",
    "voice_sample_language",
    "prepare_voice_samples",
    "voice_sample_for",
]

# The engine's language table, as one scalar string so the parallel workers inherit
# it the way the shell's export does. One row per engine whose set is known; an
# engine with no row is not second-guessed - app.py owns that answer.
_ENGINE_LANGUAGES = (
    "xtts:ara,ces,deu,eng,fra,hin,hun,ita,jpn,kor,nld,pol,por,rus,spa,tur,zho"
)

# The torchcodec PyTorch pairs, as "<torch series>:<torchcodec series>".
_TORCHCODEC_PAIRS = "2.9:0.9 2.10:0.10 2.11:0.11"

# The mark that recognises this repo's own sitecustomize.py.
_SOUNDFILE_MARK = "_load_with_soundfile_fallback"

# The text the sitecustomize.py is written with, exactly as the shell heredoc
# spells it - the mark above is in it, and the removal searches the whole file for
# it rather than only the head.
_SITECUSTOMIZE = '''# Auto-imported by Python at startup for every script run in this venv.
#
# torchaudio >= 2.9's torchaudio.load() routes unconditionally through
# torchcodec with no fallback (see torchaudio/_torchcodec.py). If the
# installed torchcodec build doesn't support this system's FFmpeg version,
# every torchaudio.load() call raises RuntimeError("Could not load
# libtorchcodec..."). coqui-tts calls torchaudio.load() directly in several
# places (XTTS reference-voice loading, VITS zero-shot voice cloning, VAD,
# dataset loading) with no try/except of its own, so this breaks any
# voice-cloning workflow across TTS engines.
#
# Patch torchaudio.load() to fall back to soundfile (libsndfile, no FFmpeg
# or torchcodec involved) only when torchcodec fails to load. If a future
# torchcodec release covers this system's FFmpeg version, this fallback
# simply never triggers.

def _patch_torchaudio_load():
    try:
        import torchaudio
    except ImportError:
        return

    _orig_load = torchaudio.load

    def _load_with_soundfile_fallback(uri, frame_offset=0, num_frames=-1, normalize=True,
                                       channels_first=True, format=None, buffer_size=4096,
                                       backend=None):
        try:
            return _orig_load(uri, frame_offset=frame_offset, num_frames=num_frames,
                               normalize=normalize, channels_first=channels_first,
                               format=format, buffer_size=buffer_size, backend=backend)
        except RuntimeError as e:
            if 'libtorchcodec' not in str(e):
                raise
            import soundfile as sf
            import torch
            start = frame_offset if frame_offset else 0
            frames = num_frames if num_frames and num_frames > 0 else -1
            data, sr = sf.read(uri, start=start, frames=frames, always_2d=True, dtype='float32')
            tensor = torch.from_numpy(data.T.copy() if channels_first else data.copy())
            return tensor, sr

    torchaudio.load = _load_with_soundfile_fallback


_patch_torchaudio_load()
del _patch_torchaudio_load
'''

# The engine's own version helper, as upstream's fix spells it, indented for the
# scope that both of detect_device()'s branches can see.
_NORMALIZE_VERSION = r"""        def _normalize_version(v:str)->tuple:
            '''Parse version string into (major, minor, patch). Patch defaults to 0.'''
            m = re.search(r'(\d+)\.(\d+)(?:\.(\d+))?', v or '')
            if not m:
                return ()
            major = int(m.group(1))
            minor = int(m.group(2))
            patch = int(m.group(3)) if m.group(3) else 0
            return (major, minor, patch)

"""

# Where that definition goes: the next helper down in the same scope.
_NORMALIZE_ANCHOR = "        def version_classify("

# The script narrationPackageSeries feeds the venv python on stdin, exactly as the
# shell heredoc does: the package's version from its METADATA, never by importing
# it - torchcodec is asked about precisely because importing it is what fails.
_PACKAGE_SERIES = """import sys
from importlib.metadata import version, PackageNotFoundError

try:
    installed = version(sys.argv[1])
except PackageNotFoundError:
    raise SystemExit(1)
# "0.7.0+cu129" -> "0.7"
print('.'.join(installed.split('+')[0].split('.')[:2]))
"""

# The two progress shapes narrationProgress reads, both anchored at the start of a
# tqdm redraw so a percent sign inside a spoken sentence is not mistaken for one.
_PROGRESS_PERCENT = re.compile(r"^[ \t\n\r\f\v]*[0-9]+(\.[0-9]+)?%:")
_PROGRESS_BAR = "%|"

# The characters the engine uses in a session id - the id ends at the first
# character outside this set.
_SESSION_ID = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_.-")

# The whitespace the shell's [[:space:]] names in the C locale.
_C_SPACE = " \t\n\r\f\v"


def _config(name, default):
    """A configuration value, read from the environment with its source-time
    default - the shell sets these at source time, and a caller that never set
    them gets the module's own answer."""
    return os.environ.get(name, default)


def _noop(*_args):
    return None


# --- the process boundary ------------------------------------------------------


def _command_v(name):
    """The shell's ``command -v <name>``: the path it resolves to, or None.

    A name with a slash is found when it is executable - a directory with the
    bit set passes, the way this shell's command -v answers it. A bare name is
    the first PATH entry holding a file by that name, whether or not executable
    (a directory and a broken link are not commands), an empty entry meaning the
    current directory and its answer the relative spelling.
    """
    if os.sep in name:
        return name if os.access(name, os.X_OK) else None
    path = os.environ.get("PATH", "")
    for entry in path.split(os.pathsep):
        candidate = entry + "/" + name if entry else "./" + name
        if os.path.isfile(candidate):
            return candidate
    return None


def _run(argv, **kwargs):
    """A tool call through the process boundary, with the shell's silence on
    stdin; stdout and stderr go where the caller points them, the way the
    shell's redirects do. An absent tool is the call's own failure, the
    shell's 127."""
    if "input" not in kwargs:
        kwargs.setdefault("stdin", subprocess.DEVNULL)
    try:
        return subprocess.run(list(argv), **kwargs)
    except (OSError, ValueError):
        return None


def _silenced(argv):
    """A call the shell runs with ``>/dev/null 2>&1``: only its status is asked
    of it."""
    proc = _run(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return proc is not None and proc.returncode == 0


def _console(argv):
    """A call the shell runs with no redirects at all: its output goes to the
    console as the shell's would, and only its status is asked of it."""
    proc = _run(argv)
    return proc is not None and proc.returncode == 0


def _python_ok(python, args):
    """``"$python" <args...> >/dev/null 2>&1`` - true when it exits 0."""
    return _silenced([python] + list(args))


def _python_stdout(python, args):
    """The call's stdout, or None when it does not exit 0 - the shell's
    ``out="$(... 2>/dev/null)" || ...``.

    Only TRAILING newlines come off, because that is all command substitution
    removes: a leading space would still be part of the answer, and the readers
    of this go on to split what they are handed on its first dot.
    """
    proc = _run([python] + list(args), stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL)
    if proc is None or proc.returncode != 0:
        return None
    return proc.stdout.decode("utf-8", "replace").rstrip("\n")


def _pyenv_root():
    """``pyenv root 2>/dev/null || true``: what it PRINTED, whatever it exited
    with. The shell swallows that status deliberately - a pyenv that names its
    root and then fails has still named it - so a reader that required success
    would walk past every interpreter that root holds."""
    proc = _run(["pyenv", "root"], stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL)
    if proc is None:
        return ""
    return proc.stdout.decode("utf-8", "replace").rstrip("\n")


# --- the language the engine speaks --------------------------------------------


def narration_supports_language(code, engine=None):
    """True when the engine can speak that ISO 639-3 code - and true for an
    engine whose set is not tabulated at all, the same "not this repo's answer"
    the list gives."""
    code = shell_lower(code or "")
    if not code:
        return False
    if engine is None:
        engine = _config("narrationEngine", "xtts")
    engine = shell_lower(engine)
    # The shell splits the table on semicolons and then on whitespace.
    for row in _config("narrationEngineLanguages", _ENGINE_LANGUAGES).replace(";", " ").split():
        name, _sep, codes = row.partition(":")
        if name != engine:
            continue
        return ("," + code + ",") in ("," + codes + ",")
    return True


def narration_language_list(engine=None):
    """The codes an engine speaks, space separated, so a refusal can list them
    instead of merely saying no. Empty for an engine with no row."""
    if engine is None:
        engine = _config("narrationEngine", "xtts")
    engine = shell_lower(engine)
    # The shell splits the table on semicolons and then on whitespace.
    for row in _config("narrationEngineLanguages", _ENGINE_LANGUAGES).replace(";", " ").split():
        name, _sep, codes = row.partition(":")
        if name == engine:
            return codes.replace(",", " ")
    return ""


# --- the nvidia question --------------------------------------------------------


def narration_has_nvidia():
    """True when this host has an NVIDIA GPU that is actually usable. -L is
    asked rather than trusting the binary's presence."""
    proc = _run(["nvidia-smi", "-L"], stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL)
    return proc is not None and proc.returncode == 0


def narration_free_vram():
    """The free VRAM, in MiB, of the first GPU, as a string - or None when it
    cannot be asked."""
    if not narration_has_nvidia():
        return None
    proc = _run(["nvidia-smi", "--query-gpu=memory.free",
                 "--format=csv,noheader,nounits"],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    if proc is None:
        return None
    lines = proc.stdout.decode("utf-8", "replace").splitlines()
    free = lines[0] if lines else ""
    free = re.sub(r"[ \t\n\r\f\v]", "", free)
    if not re.match(r"^[0-9]+$", free):
        return None
    return free


def narration_job_budget(device=None):
    """How many books this machine should read at once. Never 0 and never a
    fraction; falls back to 1 whenever the answer cannot be established."""
    if device is None:
        device = os.environ.get("narrationDevice", "")
    if device != "cuda":
        return "1"
    free = narration_free_vram()
    if free is None:
        return "1"
    per_book = awk_number(_config("narrationVramPerBookGB", "5"))
    divisor = per_book * 1024
    if divisor == 0:
        return ""
    n = int(awk_number(free) / divisor)
    if n < 1:
        n = 1
    return str(n)


# --- the python interpreter the narration runs on -----------------------------


def _version_key(text):
    """A version-sort key: digit runs compared as numbers, text runs as text,
    the way ``sort -V`` orders versions, so 3.12 comes before 3.10."""
    key = []
    for run in re.findall(r"\d+|\D+", text):
        key.append((0, int(run)) if run.isdigit() else (1, run))
    return key


def narration_base_python():
    """A Python interpreter ebook2audiobook can run on (3.10-3.12), newest
    first.

    pyenv's own versions are looked at before PATH, because a machine with pyenv
    usually has its supported interpreters there and only the unsupported system
    one on PATH. Returns the path the first qualifying candidate resolves to, or
    None when nothing qualifies - the shell's ``return 1``.
    """
    min_minor = int(awk_number(_config("narrationPythonMinMinor", "10")))
    max_minor = int(awk_number(_config("narrationPythonMaxMinor", "12")))
    candidates = []

    if _command_v("pyenv"):
        root = _pyenv_root()
        if root:
            kept = []
            for line in _pyenv_versions_bare():
                if re.match(r"^3\.(1[0-2])\.", line):
                    kept.append(line)
            for version in sorted(kept, key=_version_key, reverse=True):
                candidates.append(os.path.join(root, "versions", version,
                                               "bin", "python3"))

    for minor in range(max_minor, min_minor - 1, -1):
        candidates.append("python3.%d" % minor)
    candidates.append("python3")

    probe = 'import sys; print("%d.%d" % sys.version_info[:2])'
    for candidate in candidates:
        path = _command_v(candidate)
        if not path:
            continue
        version = _python_stdout(path, ["-c", probe])
        if not version:
            continue
        major, sep, minor = version.partition(".")
        if not (major == "3" and sep):
            continue
        try:
            minor_number = int(minor)
        except ValueError:
            continue
        if min_minor <= minor_number <= max_minor:
            return path
    return None


def _pyenv_versions_bare():
    proc = _run(["pyenv", "versions", "--bare"], stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL)
    if proc is None:
        return []
    return proc.stdout.decode("utf-8", "replace").split("\n")


def narration_ensure_venv(log=None):
    """The checkout's own interpreter, creating the plain venv when it is not
    there yet. Returns the interpreter path, or None when no base python can be
    found - the refusal the shell prints and the ``return 1``."""
    if log is None:
        log = _noop
    home = os.environ.get("narrationHome", "")
    python_exe = home + "/python_env/bin/python"
    if os.path.isfile(python_exe) and os.access(python_exe, os.X_OK):
        return python_exe

    base_python = narration_base_python()
    if base_python is None:
        min_minor = _config("narrationPythonMinMinor", "10")
        max_minor = _config("narrationPythonMaxMinor", "12")
        sys.stderr.write("\n")
        sys.stderr.write(
            "Cannot narrate: no Python 3.%s-3.%s interpreter found on this "
            "machine.\n\n" % (min_minor, max_minor))
        sys.stderr.write(
            "  python3.%s-3.%s  what ebook2audiobook runs on (not older, not "
            "newer)  apt install python3.12-venv  (or: "
            "https://github.com/pyenv/pyenv)\n" % (min_minor, max_minor))
        sys.stderr.write("\nInstall one and run again. Nothing was changed.\n")
        return None

    log("Creating %s/python_env from %s (first run only)"
        % (home, base_python))
    if not _console([base_python, "-m", "venv", home + "/python_env"]):
        return None
    if not _console([python_exe, "-m", "pip", "install", "--quiet",
                     "--upgrade", "pip", "setuptools", "wheel"]):
        return None
    return python_exe


# --- the torchcodec the torchaudio.load() goes through -------------------------


def narration_package_series(distribution):
    """The "<major>.<minor>" of an installed package, from its METADATA rather
    than by importing it. Returns the series, or None when it is not installed
    - the shell's ``SystemExit(1)``."""
    python = os.environ.get("narrationPython", "")
    if not python:
        return None
    proc = _run([python, "-", distribution], stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                input=_PACKAGE_SERIES.encode("utf-8"))
    if proc is None or proc.returncode != 0:
        return None
    return proc.stdout.decode("utf-8", "replace").strip()


def narration_wanted_torchcodec(torch_series):
    """The torchcodec series that torch is paired with, or None when it is not
    in the table - the same "not this repo's answer" the language table gives
    for an untabulated engine."""
    for pair in _config("narrationTorchcodecPairs", _TORCHCODEC_PAIRS).split():
        torch, sep, torchcodec = pair.partition(":")
        if sep and torch == torch_series:
            return torchcodec
    return None


def narration_ensure_torchcodec(log=None):
    """True when audio can be loaded through torchaudio on this machine, false
    when the caller should install the soundfile shim instead. Corrects a
    torchcodec that does not match the installed torch on the way."""
    if log is None:
        log = _noop
    python = os.environ.get("narrationPython", "")
    if not python:
        return False

    # Importing torchcodec loads its shared libraries, so this probe is the whole
    # question: an import that succeeds is a torchaudio.load() that works.
    if _python_ok(python, ["-c", "import torchcodec"]):
        return True

    # No torchcodec at all - an env app.py has not populated yet.
    installed = narration_package_series("torchcodec")
    if installed is None:
        return False
    torch_series = narration_package_series("torch")
    if torch_series is None:
        return False
    wanted = narration_wanted_torchcodec(torch_series)
    if wanted is None:
        return False
    # Already the paired one and still not loading: not the mismatch this fixes.
    if wanted == installed:
        return False

    log("torchcodec %s cannot load here (typically an FFmpeg newer than the "
        "versions it" % installed)
    log("         shipped libraries for) - installing torchcodec %s, the one "
        "torch %s is paired with." % (wanted, torch_series))
    if not _silenced([python, "-m", "pip", "install", "--quiet",
                      "--no-cache-dir", "--no-deps",
                      "torchcodec==%s.*" % wanted]):
        return False
    return _python_ok(python, ["-c", "import torchcodec"])


def _site_dir():
    """The venv's site directory from the venv python itself, or None when it
    cannot be asked."""
    python = os.environ.get("narrationPython", "")
    if not python:
        return None
    return _python_stdout(python, ["-c",
                                   "import sysconfig; "
                                   "print(sysconfig.get_paths()['purelib'])"])


def narration_install_soundfile_fallback():
    """The last resort: torchaudio.load() -> soundfile, as a sitecustomize.py
    the venv auto-imports. A no-op when the site directory cannot be
    determined."""
    site_dir = _site_dir()
    if site_dir is None or not os.path.isdir(site_dir):
        return 0
    try:
        with open(os.path.join(site_dir, "sitecustomize.py"), "w",
                  encoding="utf-8") as handle:
            handle.write(_SITECUSTOMIZE)
    except OSError:
        pass
    return 0


def narration_remove_soundfile_fallback():
    """Remove the shim once torchcodec works - including one an earlier version
    left behind, which is why the whole file is searched for the mark. A
    sitecustomize.py this repo did not write stays."""
    site_dir = _site_dir()
    if site_dir is None:
        return 0
    target = os.path.join(site_dir, "sitecustomize.py")
    if not os.path.isfile(target):
        return 0
    try:
        with open(target, encoding="utf-8", errors="replace") as handle:
            text = handle.read()
    except OSError:
        return 0
    if _config("narrationSoundfileFallbackMark", _SOUNDFILE_MARK) not in text:
        return 0
    try:
        os.remove(target)
    except OSError:
        pass
    return 0


def narration_fix_device_detection(log=None):
    """Give the engine's device detection the version helper it calls but does
    not define. v26.8.20 puts _normalize_version() inside detect_device()'s ROCm
    branch and calls it from the CUDA one, so on an NVIDIA machine the engine
    dies of an UnboundLocalError before a word is read. Returns 0 whatever
    happened: a checkout without the fault is left exactly as it is.

    The definition is ADDED at the enclosing scope rather than moved out of the
    branch: adding cannot disturb a checkout upstream has meanwhile fixed its
    own way.
    """
    if log is None:
        log = _noop
    home = os.environ.get("narrationHome", "")
    if not home:
        return 0
    target = os.path.join(home, "lib", "classes", "device_installer.py")
    try:
        with open(target, encoding="utf-8") as handle:
            text = handle.read()
    except OSError:
        return 0
    if "def _normalize_version(" not in text:
        return 0
    # One at the enclosing scope already - upstream's fix, or an earlier run's.
    if re.search(r"^ {8}def _normalize_version\(", text, re.MULTILINE):
        return 0
    if _NORMALIZE_ANCHOR not in text:
        return 0
    log("Patching the engine: its device detection calls _normalize_version() "
        "from a branch that does not define it.")
    try:
        with open(target, "w", encoding="utf-8") as handle:
            handle.write(text.replace(_NORMALIZE_ANCHOR,
                                      _NORMALIZE_VERSION + _NORMALIZE_ANCHOR, 1))
    except OSError:
        pass
    return 0


def narration_drop_empty_unidic():
    """Remove an EMPTY unidic/ from the venv, left there by the engine's own
    uninstall of the package. Python imports such a directory as a namespace
    package, so ``import unidic`` succeeds and ``unidic.DICDIR`` then raises
    AttributeError - which the tokeniser behind XTTS never catches, guarding as
    it does against ImportError alone, and the engine loads no model at all.
    Empty is the whole test: a real unidic carries its dictionary."""
    site_dir = _site_dir()
    if site_dir is None:
        return 0
    target = os.path.join(site_dir, "unidic")
    if not os.path.isdir(target):
        return 0
    try:
        if os.listdir(target):
            return 0
        os.rmdir(target)
    except OSError:
        pass
    return 0


# --- settling the run ----------------------------------------------------------


def init_book_narration(checkout=None, device=None, log=None):
    """Settle everything about HOW this run narrates, once, before any book is
    read: which checkout, which interpreter, which device - and apply the
    preparations to that checkout. Returns 0 on success, 1 on a refusal. The
    settled values are left in the environment the way the shell's exports
    leave them."""
    if log is None:
        log = _noop
    checkout = checkout or os.environ.get("narrationHome", "")
    if not checkout:
        checkout = os.path.expanduser("~/ebook2audiobook")

    if not os.path.isfile(os.path.join(checkout, "app.py")):
        sys.stderr.write("\n")
        sys.stderr.write(
            'Cannot narrate: no ebook2audiobook checkout at "%s".\n\n'
            % checkout)
        sys.stderr.write(
            "  ebook2audiobook  the text-to-speech engine that reads the books  "
            "git clone "
            "https://github.com/DrewThomasson/ebook2audiobook\n")
        sys.stderr.write(
            "\nClone it (or name an existing checkout) and run again. Nothing "
            "was changed.\n")
        return 1

    # cd -- dir && pwd settles the path the way the shell does: absolute, the
    # symlinks still standing.
    os.environ["narrationHome"] = os.path.abspath(os.path.expanduser(checkout))

    narration_fix_device_detection(log)

    python = narration_ensure_venv(log)
    if python is None:
        return 1
    os.environ["narrationPython"] = python

    # Keep ~/.local out of this env no matter how it is entered.
    os.environ["PYTHONNOUSERSITE"] = "1"

    if narration_ensure_torchcodec(log):
        narration_remove_soundfile_fallback()
    else:
        narration_install_soundfile_fallback()

    narration_drop_empty_unidic()

    if not device:
        device = "cuda" if narration_has_nvidia() else "cpu"
    os.environ["narrationDevice"] = device

    # Is the environment POPULATED, or merely created? torch stands in for the
    # whole stack: it is the largest thing installed and nothing narrates
    # without it.
    if _python_ok(python, ["-c", "import torch"]):
        os.environ["narrationEnvReady"] = "1"
    else:
        os.environ["narrationEnvReady"] = "0"
    return 0


# --- the narration itself -------------------------------------------------------


def narrate_book(book, out_dir, voice="", language=None, log_file=None):
    """Read ONE book aloud into <outDir> and return the path of the file that
    came out, or None when nothing was produced. <language> falls back to
    $narrationLanguage; the produced file is FOUND rather than named, newest
    first."""
    if language is None or language == "":
        language = os.environ.get("narrationLanguage", "")
    if log_file is None:
        log_file = os.path.join(out_dir, "narration.log")

    try:
        os.makedirs(out_dir, exist_ok=True)
    except OSError:
        return None
    log_dir = os.path.dirname(log_file)
    if log_dir:
        try:
            os.makedirs(log_dir, exist_ok=True)
        except OSError:
            return None

    # app.py is run from inside the checkout, so every path handed to it has to
    # be absolute: a relative one would be resolved against the checkout.
    book = os.path.realpath(book)

    device = os.environ.get("narrationDevice", "")
    args = ["--headless",
            "--device", device,
            "--tts_engine", _config("narrationEngine", "xtts"),
            "--output_format", _config("narrationFormat", "m4b"),
            "--output_channel", _config("narrationChannel", "mono"),
            "--ebook", book,
            "--output_dir", out_dir]
    if voice:
        args += ["--voice", voice]
    if language:
        args += ["--language", language]

    python = os.environ.get("narrationPython", "")
    home = os.environ.get("narrationHome", "")
    if not python or not home:
        return None
    # The venv's own bin first, exactly as activating the env would - the
    # shell's ${narrationPython%/*}, a name without a slash staying itself.
    slash = python.rfind("/")
    head = python[:slash] if slash >= 0 else python
    narration_env = dict(os.environ)
    narration_env["PATH"] = head + os.pathsep + os.environ.get("PATH", "")

    argv = [python, "app.py"] + args
    try:
        if _config("narrationVerbose", "0") != "0":
            # Verbose runs put the engine's output on the console - on stderr,
            # not stdout - and keep it in the log as well.
            status = _tee_run(argv, log_file, home, narration_env)
        else:
            with open(log_file, "wb") as handle:
                proc = subprocess.run(argv, cwd=home, env=narration_env,
                                      stdin=subprocess.DEVNULL, stdout=handle,
                                      stderr=subprocess.STDOUT)
            status = proc.returncode
    except (OSError, ValueError):
        return None
    if status != 0:
        return None

    # Newest first, so a checkout that left something behind from an earlier
    # attempt cannot be mistaken for this run's output.
    produced = newest_file(out_dir, _config("narrationFormat", "m4b"))
    if not produced or os.path.getsize(produced) == 0:
        return None
    return produced


def _tee_run(argv, log_file, cwd, env):
    """The verbose engine call: its output goes to the log and to the console
    stderr at once, the shell's ``{ ...; } | tee -- log >&2``. The console side
    is file descriptor 2 itself, the way ``>&2`` names it, so it survives the
    test's swap of sys.stderr."""
    with open(log_file, "wb") as handle:
        console = os.fdopen(2, "wb", closefd=False)
        proc = subprocess.Popen(list(argv), cwd=cwd, env=env,
                                stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT)
        with proc.stdout:
            for chunk in iter(lambda: proc.stdout.read(65536), b""):
                handle.write(chunk)
                console.write(chunk)
        console.flush()
        proc.wait()
        return proc.returncode


def _media_duration(path):
    """A media file's duration in seconds, as ffprobe reports it - "0" for a
    file ffprobe cannot read a duration from, so arithmetic on the result never
    trips.

    The shell is ``ffprobe ... 2>/dev/null || echo 0``, and the ``|| echo 0``
    APPENDS: a probe that prints something and then fails hands back what it
    printed AND the zero, on the same stream. So a probe that prints "3600.0"
    with no newline of its own and exits 1 answers "3600.00", and one that
    prints a whole line answers two lines - neither of which is the "0" a
    caller reading only the status would expect, and both of which the
    arithmetic downstream reads differently (see awk_gt).
    """
    proc = _run(["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
                 "-of", "default=nk=1:nw=1", path],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    if proc is None:
        return "0"
    out = proc.stdout.decode("utf-8", "replace")
    if proc.returncode != 0:
        out += "0\n"
    return out.rstrip("\n")


def narration_progress(log_file):
    """How far the conversion writing that log has got, as a whole-number
    percentage string, or None when it has not said yet."""
    if not os.path.isfile(log_file) or os.path.getsize(log_file) == 0:
        return None
    try:
        tail = int(_config("narrationProgressTail", "65536"))
    except ValueError:
        return None
    if tail <= 0:
        return None
    try:
        with open(log_file, "rb") as handle:
            data = handle.read()[-tail:]
    except OSError:
        return None
    text = data.decode("utf-8", "replace").replace("\r", "\n")
    found = None
    for line in text.split("\n"):
        if _PROGRESS_PERCENT.match(line):
            value = line.lstrip(_C_SPACE).split("%", 1)[0]
            found = awk_number(value)
        elif _PROGRESS_BAR in line:
            value = line.split(_PROGRESS_BAR, 1)[0]
            cut = -1
            for index, char in enumerate(value):
                if char not in "0123456789.":
                    cut = index
            value = value[cut + 1:] if cut >= 0 else value
            if value != "":
                found = awk_number(value)
    if found is None:
        return None
    return str(int(found))


def narration_lossless_master(log_file, audiobook=None):
    """The path of the lossless FLAC master the engine built for <audiobook>,
    or None when this conversion did not leave one behind."""
    if not os.path.isfile(log_file):
        return None
    reference = "0"
    if audiobook:
        reference = _media_duration(audiobook)

    try:
        with open(log_file, encoding="utf-8", errors="replace") as handle:
            lines = handle.read().split("\n")
    except OSError:
        return None

    # The sed: the leading run of non-slashes must name a Completed, and the
    # candidate is what the line is from its first slash on.
    candidates = []
    for line in lines:
        slash = line.find("/")
        if slash < 0 or "Completed" not in line[:slash]:
            continue
        candidate = line[slash:]
        if re.search(r"\.flac$", candidate, re.IGNORECASE):
            candidates.append(candidate)

    tolerance = awk_number(_config("narrationLosslessTolerance", "0.02"))
    # Newest last in the log, so the candidates are walked from the back.
    for candidate in reversed(candidates):
        if not (os.path.isfile(candidate) and os.path.getsize(candidate) > 0):
            continue
        if "/chapters/" in candidate:
            continue
        # awk -v r="$reference" '... !(r > 0)': a reference that does not look
        # like a number is compared as text, and "N/A" > "0" holds - so an
        # audiobook whose duration could not be read still asks for the check.
        if awk_gt(reference, 0):
            duration = _media_duration(candidate)
            ref_number = awk_number(reference)
            diff = awk_number(duration) - ref_number
            if diff < 0:
                diff = -diff
            # The inner awk is `b > 0 && d / b <= tol` over that SAME reference:
            # the gate holds by the string comparison and the division then
            # divides by zero, which is fatal in awk - and the shell's
            # `|| continue` reads that death as "not this candidate".
            if ref_number == 0:
                continue
            if diff / ref_number > tolerance:
                continue
        return candidate
    return None


def narration_session_dir(log_file):
    """The engine's private working directory for the conversion that wrote
    this log (<checkout>/tmp/proc-<session>), or None when the log names
    none."""
    home = os.environ.get("narrationHome", "")
    if not (os.path.isfile(log_file) and home):
        return None
    prefix = (home[:-1] if home.endswith("/") else home) + "/tmp/proc-"
    try:
        with open(log_file, encoding="utf-8", errors="replace") as handle:
            lines = handle.read().split("\n")
    except OSError:
        return None
    for line in lines:
        pos = line.find(prefix)
        if pos < 0:
            continue
        rest = line[pos + len(prefix):]
        cut = len(rest)
        for index, char in enumerate(rest):
            if char not in _SESSION_ID:
                cut = index
                break
        rest = rest[:cut]
        if rest:
            return prefix + rest
    return None


def narration_drop_session(log_file):
    """Remove everything the engine kept for that book: its work directory, and
    the per-session voice and custom-model folders that live beside it under
    other roots. Always a no-op when the log names none, and refuses anything
    that is not inside the checkout's own tmp.

    All three are named after ONE session id, so the work directory - the only
    one the log spells out - is what finds the other two.
    """
    home = os.environ.get("narrationHome", "")
    if not home:
        return 0
    directory = narration_session_dir(log_file)
    if not directory:
        return 0
    root = home[:-1] if home.endswith("/") else home
    prefix = root + "/tmp/proc-"
    if not directory.startswith(prefix):
        return 0
    session = directory[len(prefix):]
    # A voice sample is written per book and per language, so a run over a
    # library leaves half a megabyte of wav behind for every book it read.
    for path in (directory,
                 root + "/voices/__sessions/voice-" + session,
                 root + "/models/__sessions/model-" + session):
        if os.path.isdir(path):
            shutil.rmtree(path, ignore_errors=True)
    return 0


# --- the voice sample -----------------------------------------------------------


def voice_sample_window(src, duration):
    """Print "<start> <length>" (seconds) of the slice to take out of an
    over-long voice sample: a stretch of speech near the middle, of at most
    voiceSampleSeconds. Returns the string, or "" when nothing could be worked
    out."""
    duration_number = awk_number(duration)
    search_seconds = awk_number(_config("voiceSampleSearchSeconds", "180"))
    # start = d / 2 - s / 2, clamped at 0, as awk's double printf %.3f prints it.
    start = duration_number / 2 - search_seconds / 2
    if start < 0:
        start = 0
    search_start = "%.3f" % start
    # len = s, clamped so the window does not run past the end of the file. The
    # awk is `if (st + len > d)`, and d is the raw duration: a duration that does
    # not look like a number is compared as TEXT against the window's own length,
    # so an unreadable file is not clamped to nothing - the whole search window is
    # probed, which is what the arm above has already decided to do.
    length = search_seconds
    if awk_gt(awk_number(search_start) + length, duration):
        length = duration_number - awk_number(search_start)
    search_length = "%.3f" % length

    noise = _config("voiceSilenceNoise", "-30dB")
    min_dur = _config("voiceSilenceMinDur", "0.3")
    proc = _run(["ffmpeg", "-nostdin", "-hide_banner", "-copyts",
                 "-ss", search_start, "-t", search_length, "-i", src,
                 "-af", "silencedetect=noise=%s:d=%s" % (noise, min_dur),
                 "-f", "null", "-"],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    silence_text = ""
    if proc is not None:
        silence_text = proc.stdout.decode("utf-8", "replace")

    return _select_window(silence_text, search_start, search_length)


def _select_window(silence_text, search_start, search_length):
    """The window selection over the silence lines the silencedetect probe
    printed: the gaps, the stretches between the long ones, the best stretch,
    and the edges nudged out of any short pause - the whole awk END block."""
    win_start = awk_number(search_start)
    win_len = awk_number(search_length)
    want = awk_number(_config("voiceSampleSeconds", "45"))
    min_want = awk_number(_config("voiceSampleMinSeconds", "5"))
    max_gap = awk_number(_config("voiceSampleMaxGap", "2.5"))

    starts = []
    ends = []
    for line in silence_text.split("\n"):
        fields = line.split()
        for index, field in enumerate(fields):
            if field == "silence_start:" and index + 1 < len(fields):
                starts.append(awk_number(fields[index + 1]))
            if field == "silence_end:" and index + 1 < len(fields):
                ends.append(awk_number(fields[index + 1]))

    win_end = win_start + win_len
    middle = win_start + win_len / 2
    pairs = min(len(starts), len(ends))

    # Every gap, in order, and which of them are long enough to end a stretch.
    cursor = win_start
    gap_start = []
    gap_end = []
    for i in range(pairs):
        if starts[i] < cursor:
            continue
        gap_start.append(starts[i])
        gap_end.append(ends[i])
        cursor = ends[i]
    gaps = len(gap_start)

    # The stretches: what lies between the LONG gaps.
    stretch_start = []
    stretch_end = []
    cursor = win_start
    for i in range(gaps):
        if gap_end[i] - gap_start[i] <= max_gap:
            continue
        if gap_start[i] > cursor:
            stretch_start.append(cursor)
            stretch_end.append(gap_start[i])
        cursor = gap_end[i]
    if cursor < win_end:
        stretch_start.append(cursor)
        stretch_end.append(win_end)
    stretches = len(stretch_start)

    best_start = None
    best_len = 0.0
    best_distance = 0.0
    for i in range(stretches):
        length = stretch_end[i] - stretch_start[i]
        if length < min_want:
            continue
        distance = (stretch_start[i] + length / 2) - middle
        if distance < 0:
            distance = -distance
        usable = length >= want
        best_usable = best_len >= want
        if (best_start is None
                or (usable and not best_usable)
                or (usable == best_usable
                    and (distance < best_distance if usable else length > best_len))):
            best_start = stretch_start[i]
            best_len = length
            best_distance = distance

    if best_start is None:
        best_start = win_start
        best_len = win_len
    take = best_len if best_len < want else want
    # Centred in the stretch it is cut from.
    start = best_start + (best_len - take) / 2
    stop = start + take

    # Both edges out of a pause.
    nudged_start = start
    nudged_stop = stop
    for i in range(gaps):
        if gap_start[i] < nudged_start < gap_end[i]:
            nudged_start = gap_end[i]
        if gap_start[i] < nudged_stop < gap_end[i]:
            nudged_stop = gap_start[i]
    if nudged_stop - nudged_start >= min_want:
        start = nudged_start
        take = nudged_stop - nudged_start

    return "%.3f %.3f" % (start, take)


def _probe_codec(src):
    """The file's first audio codec name, or "" when there is none - the
    shell's ``ffprobe -select_streams a:0 ... | head -n1``."""
    proc = _run(["ffprobe", "-v", "quiet", "-select_streams", "a:0",
                 "-show_entries", "stream=codec_name", "-of",
                 "default=nk=1:nw=1", src],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    if proc is None:
        return ""
    lines = proc.stdout.decode("utf-8", "replace").split("\n")
    return lines[0] if lines else ""


def prepare_voice_sample(src, scratch_dir, log=None):
    """The voice sample to hand to the narration: the file itself when it is
    already what cloning wants, or a rewritten one in <scratchDir>. Returns the
    path, or None for a file no audio could be read from at all."""
    if log is None:
        log = _noop
    if not os.path.isfile(src):
        return None

    duration = _media_duration(src)
    codec = _probe_codec(src)
    if not codec:
        return None
    extension = lower_extension_of(src)

    cut_args = []
    # awk -v d="$duration" -v m="$voiceSampleMaxSeconds" '... !(d > m)': both
    # sides are strnums, so a duration that does not LOOK like a number is
    # compared as text - and an unreadable file ("N/A", or a probe that printed a
    # word and failed) reads as OVER-LONG rather than as empty.
    if awk_gt(duration, _config("voiceSampleMaxSeconds", "60")):
        window = voice_sample_window(src, duration)
        parts = window.split()
        if len(parts) == 2 and parts[0] and parts[1]:
            start, take = parts
            log("Voice sample is %s long: taking %s of speech from %s"
                % (fmt_clock(duration), fmt_clock(take), fmt_clock(start)))
            cut_args = ["-ss", start, "-t", take]
    elif extension == "wav" and codec == _config("voiceSampleCodec", "pcm_s16le"):
        return src

    sample = os.path.join(scratch_dir, "voiceSample.wav")
    rate = _config("voiceSampleRate", "24000")
    codec = _config("voiceSampleCodec", "pcm_s16le")
    proc = _run(["ffmpeg", "-y", "-nostdin", "-loglevel", "error"] + cut_args
                + ["-i", src, "-map", "0:a:0", "-ac", "1", "-ar", rate,
                   "-c:a", codec, sample],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if proc is None or proc.returncode != 0:
        return None
    if not os.path.isfile(sample) or os.path.getsize(sample) == 0:
        return None
    return sample


def voice_sample_language(path):
    """The ISO 639-3 code a sample file names, "-" when it names one of the
    fallback names, or "" when it names neither."""
    base = path.rpartition("/")[2]
    dot = base.rfind(".")
    stem = base[:dot] if dot >= 0 else base
    if not stem:
        return ""

    stem_lower = shell_lower(stem)
    for candidate in _config(
            "narrationVoiceFallbackNames", "default any fallback other").split():
        if stem_lower == candidate:
            return "-"

    code = book_language_code(stem)
    if code:
        return code

    # The parts, from the back: "voice sample de" says "de" louder than "voice".
    for token in reversed(re.split(r"[ \t._-]", stem)):
        if not token:
            continue
        code = book_language_code(token)
        if code:
            return code
    return ""


def prepare_voice_samples(given, scratch_dir, log=None):
    """The voice map for what -v was given, having prepared every sample in it
    exactly once. Returns the map (one "<code><TAB><path>" per line, no trailing
    newline), or None when nothing usable came out of it at all."""
    if log is None:
        log = _noop
    voices_dir = os.path.join(scratch_dir, "voices")
    map_lines = []

    if os.path.isfile(given):
        fallback_dir = os.path.join(voices_dir, "fallback")
        try:
            os.makedirs(fallback_dir, exist_ok=True)
        except OSError:
            return None
        prepared = prepare_voice_sample(given, fallback_dir, log)
        if prepared is None:
            return None
        if not prepared.startswith(fallback_dir + os.sep):
            target = os.path.join(fallback_dir, "voiceSample.wav")
            try:
                shutil.copyfile(prepared, target)
            except OSError:
                return None
            prepared = target
        return "-\t" + prepared

    if not os.path.isdir(given):
        return None

    # Every file directly in the directory, in sorted order - the shell's
    # find -maxdepth 1 -type f | sort -z.
    try:
        names = []
        for name in os.listdir(given):
            try:
                info = os.lstat(os.path.join(given, name))
            except OSError:
                continue
            if stat.S_ISREG(info.st_mode):
                names.append(name)
        names.sort()
    except OSError:
        return None
    seen = set()
    for name in names:
        file = os.path.join(given, name)
        language = voice_sample_language(file)
        if not language:
            log('Voice sample "%s" names no language - ignored. Name it after the'
                % name)
            log("         language it speaks (deu.wav, german.m4a, de.mp3), or "
                '"default".')
            continue
        if language in seen:
            log('Voice sample "%s" is a second sample for the same voice - '
                'ignored.' % name)
            continue
        lang_dir = os.path.join(voices_dir, language)
        try:
            os.makedirs(lang_dir, exist_ok=True)
        except OSError:
            continue
        prepared = prepare_voice_sample(file, lang_dir, log)
        if prepared is None:
            log('No audio could be read from the voice sample "%s" - ignored.'
                % name)
            shutil.rmtree(lang_dir, ignore_errors=True)
            continue
        if not prepared.startswith(lang_dir + os.sep):
            target = os.path.join(lang_dir, "voiceSample.wav")
            try:
                shutil.copyfile(prepared, target)
            except OSError:
                continue
            prepared = target
        seen.add(language)
        map_lines.append(language + "\t" + prepared)

    if not map_lines:
        return None
    return "\n".join(map_lines)


def voice_sample_for(language=None):
    """The sample to clone for a book in that language: its own if there is one,
    else the fallback, else nothing at all."""
    voice_map = os.environ.get("narrationVoiceMap", "")
    if not voice_map:
        return ""
    fallback = ""
    want = language or ""
    for line in voice_map.split("\n"):
        parts = line.split("\t", 1)
        if len(parts) < 2:
            continue
        code, path = parts
        if not path:
            continue
        if code == "-":
            fallback = path
        elif want and code == want:
            return path
    return fallback