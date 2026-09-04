"""Running a command as a process, which is what the black-box tier is for.

Everything else in this package is tested by calling it. These runs start the
command the way a user does - a child process, its own argv, its own exit status,
its own stderr - because that is the only thing that exercises a CLI's whole
path: the parse, the preflight, the interrupt handler, the footer. A `NameError`
forty lines into a run is invisible to every in-process case and fails this one
immediately.

The external tools are stand-ins (:func:`install_media_stubs`), so a run needs no
codecs and no library on disk. The interpreter is not among them: a command IS
python, so a stub of it stubs the thing under test and not a tool it calls - a
mistake this repo has made eight times.
"""

from __future__ import annotations

import base64
import os
import subprocess
import sys
from pathlib import Path

__all__ = ["REPO", "DATA", "TOOLSTUB", "TOOLSTUB_US", "install_media_stubs",
           "link_real_python", "run", "start", "toolstub_table_line",
           "tree_of"]

# The checkout, for the child processes that need the package on PYTHONPATH.
REPO = Path(__file__).resolve().parent.parent

# What a test reads rather than imports: the recorded pages, the fixtures, and
# the fake tool a case puts on PATH. One definition, so a move is one edit.
DATA = Path(__file__).resolve().parent / "data"
TOOLSTUB = DATA / "toolstub"

# The separator the stub's response table is written with.
TOOLSTUB_US = "\x1f"


def toolstub_table_line(argv, rc, out="") -> str:
    """One line of a toolstub response table: the call's own arguments each
    followed by a unit separator, then ONE MORE separator - which is what ends
    the argv - then the exit code, a separator, and the response's base64
    (absent for a call that prints nothing).

    One definition for the whole suite: a table written one separator short
    still matches, on the line of a longer call, and answers with whatever that
    line's next argument is.
    """
    joined = "".join(a + TOOLSTUB_US for a in argv)
    encoded = base64.b64encode(out.encode()).decode("ascii") if out else ""
    return joined + TOOLSTUB_US + str(rc) + TOOLSTUB_US + encoded


def run(command: str, *args, cwd, path=None, env=None, program=None,
        stdin: str = "", timeout: float = 300) -> subprocess.CompletedProcess:
    """Start one command as a child and wait for it.

    ``path`` replaces PATH entirely, which is how a case makes a tool absent;
    the interpreter is reached by its resolved absolute path either way, so a
    narrow PATH does not stop the command from starting. ``program`` overrides
    what the command calls itself, for the one case that is about those bytes.
    """
    return subprocess.run(
        _argv(command, args), cwd=str(cwd),
        env=_environment(path, env, command, program),
        input=stdin, text=True, capture_output=True, timeout=timeout)


def _argv(command: str, args) -> list[str]:
    from medialib import commands
    return [sys.executable, "-m", commands.module_for(command),
            *[str(a) for a in args]]


def _environment(path, env, command, program) -> dict:
    environment = dict(os.environ if env is None else env)
    environment.setdefault("SKIP_TOOL_PREFLIGHT", "1")
    if path is not None:
        environment["PATH"] = str(path)
    environment["PYTHONPATH"] = os.pathsep.join(
        [str(REPO), environment["PYTHONPATH"]] if environment.get("PYTHONPATH")
        else [str(REPO)])
    environment["CLI_PROGRAM"] = program or command
    return environment


def start(command: str, *args, cwd, path=None, env=None, program=None,
          stdout=None, new_session: bool = False) -> subprocess.Popen:
    """The same launch as :func:`run`, but handed back still running.

    For the cases that STOP a run: a signal has to reach it while it is still
    working, which means the test needs the process rather than its result. By
    default its output is a pipe the caller reads after waiting, so a run that is
    killed still hands back everything it managed to print.

    ``stdout`` takes a file descriptor instead, for a run that is watched WHILE it
    goes: a pipe nobody is draining fills and blocks the run, so the cases that
    read a log as it grows write it to disk. ``new_session`` puts the run in a
    process group of its own, which is how a case signals the group rather than
    the leader.
    """
    return subprocess.Popen(
        _argv(command, args), cwd=str(cwd),
        env=_environment(path, env, command, program), text=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE if stdout is None else stdout,
        stderr=subprocess.STDOUT, start_new_session=new_session)


def tree_of(directory) -> list[str]:
    """Every path under ``directory``, relative and sorted - a folder's shape as
    one comparable value, for the cases that assert a run changed nothing."""
    root = Path(directory)
    return sorted(str(path.relative_to(root)) for path in root.rglob("*"))


def link_real_python(bin_dir) -> None:
    """A working python3 on a sandbox PATH.

    Through ``sys.executable`` rather than a PATH scan: the python3 a scan finds
    on this machine is a pyenv shim, a bash script that re-resolves through a
    pyenv the sandbox does not have.
    """
    (Path(bin_dir) / "python3").symlink_to(sys.executable)


_STUBS = {
    # For every call in these commands that PRODUCES a file, the output path is
    # the last argument. An analysis-only call does not produce one: it ends in
    # the "-" of "-f null -", where "-" is stdout and not a file - and writing to
    # it anyway made a file literally called "-" in the working directory.
    "ffmpeg": 'out="${!#}"; [[ "$out" == "-" ]] || : > "$out"',
    "ffprobe": 'echo "123.456"',
    "mkvmerge": 'prev=""; for a in "$@"; do [[ "$prev" == "-o" ]] && : > "$a"; prev="$a"; done',
    "mkvpropedit": ":",
    "mkvextract": ":",
    "pdftoppm": ":",
    # ImageMagick: the last argument is the output, minus a trailing ">".
    "convert": 'out="${!#}"; out="${out%\\>}"; : > "$out"',
}


def install_media_stubs(bin_dir, exclude=()) -> None:
    """Stand-ins for the heavy tools: each succeeds and, where the real one
    would, creates its output file, so a pipeline's existence checks pass.

    ``exclude`` leaves a tool out, for the cases that are about a tool being
    ABSENT: with `narrow()`, a name left out here is a name nothing on PATH
    answers to. Named rather than deleted afterwards so there is still one
    definition of each stub body.
    """
    directory = Path(bin_dir)
    directory.mkdir(parents=True, exist_ok=True)
    for name, body in _STUBS.items():
        if name in exclude:
            continue
        stub = directory / name
        stub.write_text("#!/usr/bin/env bash\n%s\n" % body, encoding="ascii")
        stub.chmod(0o755)
