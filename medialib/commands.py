"""The eighteen commands: what each is called, and which module is it.

One table, because three separate things need exactly this mapping and must not
disagree about it - the console scripts ``pyproject.toml`` installs, the program
name each help page prints, and :func:`run_command`, which is how one command
starts another.

The names are kebab-case. Two of them run a module whose name is not the
obvious one: ``convert-video`` and ``ingest-movies`` are large enough that their
tables and their run halves are separate modules, and it is the run half that
owns ``main``.
"""

from __future__ import annotations

import os
import subprocess
import sys

__all__ = ["COMMANDS", "MODULES", "exec_command", "module_for", "package_root",
           "program_name", "run_command", "script_dir"]

COMMANDS = {
    "clean-folder-structure": "medialib.cli.clean_folder_structure",
    "clean-folder-structure-adb": "medialib.cli.clean_folder_structure_adb",
    "concat-audio": "medialib.cli.concat_audio",
    "content-census": "medialib.cli.content_census",
    "content-census-bi": "medialib.cli.content_census_bi",
    "convert-and-concat": "medialib.cli.convert_and_concat",
    "convert-audio": "medialib.cli.convert_audio",
    "convert-comics": "medialib.cli.convert_comics",
    "convert-images": "medialib.cli.convert_images",
    "convert-video": "medialib.cli.convert_video_run",
    "cue-to-chapters": "medialib.cli.cue_to_chapters",
    "find-fragment-candidates": "medialib.cli.find_fragment_candidates",
    "ingest-books": "medialib.cli.ingest_books",
    "ingest-movies": "medialib.cli.ingest_movies_run",
    "ingest-music": "medialib.cli.ingest_music",
    "read-library": "medialib.cli.read_library",
    "transcribe-audio": "medialib.cli.transcribe_audio",
    "ytdlp": "medialib.cli.ytdlp",
}

# The reverse, for a module that wants to know what a user calls it.
MODULES = {module: name for name, module in COMMANDS.items()}


def program_name(module: str) -> str:
    """What this module calls itself on its help page and in its refusals.

    ``CLI_PROGRAM`` wins while it is set, which is how a caller that started this
    command tells it what the user typed; the command's own name answers when
    nobody said. A module absent from the table raises rather than inventing a
    name, because a help page is a recorded contract.

    Pass ``__spec__.name`` and not ``__name__``: under ``python -m`` the second
    one is ``"__main__"``.
    """
    return _declared_program() or MODULES[module]


def _declared_program() -> str:
    """``CLI_PROGRAM``, the name a caller that started this command handed down.
    Empty when nobody said."""
    return os.environ.get("CLI_PROGRAM", "")


def current_program() -> str:
    """What this process calls itself, for an asker with no module to name it by.

    The handed-down name when there is one, else ``argv[0]``'s basename - the
    shell's ``${0##*/}``, which for an installed command is the command. The
    package's own name is the last resort, for a process with no argv at all.
    """
    return _declared_program() or os.path.basename(sys.argv[0]) or "medialib"


def package_root() -> str:
    """The directory the ``medialib`` package sits in."""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def script_dir() -> str:
    """What one machine keeps next to the code rather than shipping with it: the
    podcast tables under `data/`, the logs under `logs/`.

    ``CLI_SCRIPT_DIR`` wins while it is set, so a caller that hands one down is
    still obeyed. When nobody said, the checkout answers while there is one -
    an editable install, a run from a clone - detected by the ``pyproject.toml``
    that sits at its root and not in `site-packages`, so a plain
    `pip install .` has no checkout to claim. With no checkout, the user's own
    data directory answers instead: ``XDG_DATA_HOME`` when set, else
    ``~/.local/share``, under a ``medialib`` subfolder either way - a place the
    command can always write to, rather than `site-packages`.
    """
    handed = os.environ.get("CLI_SCRIPT_DIR")
    if handed:
        return handed
    root = package_root()
    if os.path.isfile(os.path.join(root, "pyproject.toml")):
        return root
    home_share = os.path.join(os.path.expanduser("~"), ".local", "share")
    return os.path.join(os.environ.get("XDG_DATA_HOME") or home_share, "medialib")


def config_dir() -> str:
    """The configuration that SHIPS with the package: beets' import config, and
    the sample podcast table.

    Inside the package and not beside the checkout, because an installed command
    has no checkout to sit beside: `package_root()` is `site-packages` there, and
    nothing shipped would be under it. What one machine holds rather than the
    code - the podcast tables, `logs/` - stays with :func:`script_dir`.
    """
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "config")


def module_for(command: str) -> str:
    """The module behind a command name. KeyError names the unknown command,
    which is what a typo in a call site deserves."""
    return COMMANDS[command]


def exec_command(command: str, argv, script_dir: str = "") -> None:
    """REPLACE this process with another command. Never returns.

    For the one caller that hands over rather than delegating:
    `clean-folder-structure` re-execs itself as `clean-folder-structure-adb` when
    the target turns out to be a phone whose mount refuses renames, and the two
    must not both be running - the second one IS the run from there on.
    """
    env = dict(os.environ)
    env["CLI_PROGRAM"] = command
    if script_dir:
        env["CLI_SCRIPT_DIR"] = script_dir
    os.execve(sys.executable,
              [sys.executable, "-m", module_for(command),
               *[str(a) for a in argv]],
              env)


def run_command(command: str, argv, script_dir: str = "", **kwargs):
    """Start another one of these commands as a CHILD PROCESS.

    A child, and not an in-process call, on purpose: the callee installs its own
    interrupt handler, sets its own exit cleanup, and reports its own footer,
    none of which belongs in the caller's process.

    The interpreter is ``sys.executable`` and never the word ``python3``: the
    ``python3`` on this machine's PATH is a pyenv shim that costs ~195 ms a call
    to re-resolve what we already know.

    ``script_dir`` is where the callee looks for what sits beside the checkout -
    the podcast tables under `data/`, the logs under `logs/`. Passed through the
    environment because that is where the callee reads it from, and left out of
    the argv where it never was.
    """
    env = dict(kwargs.pop("env", None) or os.environ)
    env["CLI_PROGRAM"] = command
    if script_dir:
        env["CLI_SCRIPT_DIR"] = script_dir
    return subprocess.run(
        [sys.executable, "-m", module_for(command), *[str(a) for a in argv]],
        env=env, **kwargs)
