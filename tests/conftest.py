"""What makes this suite safe to run in parallel, kept as fixtures rather than
as a comment nobody reads.

Two rules:

  * allocate every fixture through a fresh directory, never a fixed name;
  * do not assert on GLOBAL STATE - give the code under test somewhere private
    to work and assert on that.

The second is the one that actually broke, and it broke exactly once it had to:
two files running the full comics conversion both checked that it left no RAM
work directory behind, which meant diffing ``/dev/shm`` - true of the MACHINE,
not of the run - so it failed as soon as there were two of them.

pytest gives the first rule for free: ``tmp_path`` is per-test by construction.
The second it does not, because this package reads where to work from the
ENVIRONMENT, and an inherited ``ramScratchBase`` or a leftover ``PAUSE_DIR``
reaches straight past any fixture. So it is enforced here: every knob that names
a place to work is pointed at this test's own directory before it runs, and the
module-level scratch state is reset with it.

The rule about fixed paths is enforced by a test rather than by a convention -
see ``tests/test_self_containment.py``.

The last section here is about neither: it is the check that the suite pytest
ran is the suite the repository holds.
"""


import locale
import os
import pathlib

import pytest

# --- the locale --------------------------------------------------------------
# PINNED rather than inherited, because two different parts of it change results
# and neither belongs to the code under test:
#
#   * LC_CTYPE must be UTF-8. The name cleaners walk accented and multibyte names
#     character-wise, and a single-byte locale mangles them one byte at a time -
#     so the suite pins a UTF-8 locale rather than recording the mangling as if
#     it were the rule. `normalize_title` asks the host's iconv, whose
#     transliteration table and give-up rule are glibc's and the locale's; under
#     LC_ALL=C it folds nothing and seven of its cases fail.
#   * LC_COLLATE decides the ORDER siblings come out in, which the recorded tree
#     fixtures depend on: in byte order "a16z" sorts after "Die ..."/"Lage ...",
#     in a UTF-8 locale's dictionary order it sorts right after "36".
#
# C.UTF-8 is exactly that combination - UTF-8 character handling, byte collation
# - and is on every glibc system, so the suite answers the same on any host
# whatever LANG it happens to have.
#
# macOS is not a glibc system and has no C.UTF-8 at all, so there the pin is
# assembled from its two halves instead: LC_COLLATE=C for the byte order, and a
# UTF-8 LC_CTYPE beside it. That is the same COMBINATION under a different name
# rather than a second policy - which is what the cases in tests/test_locale.py
# assert, so neither rung can drift into meaning something else. LC_ALL is
# dropped when that rung is taken, because it would override both of them in a
# child. A host with neither keeps its own locale, and results there depend on
# it.
#
# Both the variable and the process's own locale: a child gets the first
# (`iconv` is a subprocess), and `locale.strxfrm` and the ctypes `iswalnum` in
# find_fragment_candidates read the second.

# What LC_CTYPE is set to when the one-name pin is unavailable, best first.
# "UTF-8" with no territory is macOS's own spelling and works where the
# territory-qualified one has not been generated.
_CTYPE_LADDER = ("en_US.UTF-8", "UTF-8")


def _pin_locale() -> str | None:
    """Pin the suite's locale, and say which spelling was used ("" for the
    assembled one, None when the host offers neither)."""
    try:
        locale.setlocale(locale.LC_ALL, "C.UTF-8")
    except locale.Error:                          # pragma: no cover - host data
        pass
    else:
        os.environ["LC_ALL"] = "C.UTF-8"
        return "C.UTF-8"
    if os.name == "nt":                           # pragma: no cover - host data
        # The assembled rung cannot hold here. find_fragment_candidates asks
        # its libc what counts as alphanumeric, and to do that it re-reads the
        # locale from the environment with setlocale(LC_ALL, "") - which on
        # Windows resolves to the SYSTEM locale and ignores LC_CTYPE and
        # LC_COLLATE entirely. The pin would be undone by the first test that
        # imports that module, which is worse than not pinning: the suite would
        # then answer differently depending on what ran before. Windows keeps
        # its own locale, as it did before this ladder existed.
        return None
    for ctype in _CTYPE_LADDER:                   # pragma: no cover - host data
        try:
            locale.setlocale(locale.LC_COLLATE, "C")
            locale.setlocale(locale.LC_CTYPE, ctype)
        except locale.Error:
            continue
        os.environ.pop("LC_ALL", None)
        os.environ["LC_COLLATE"] = "C"
        os.environ["LC_CTYPE"] = ctype
        return ""
    return None                                   # pragma: no cover - host data


PINNED_LOCALE = _pin_locale()

# Every environment variable that names somewhere to WORK or somewhere to record
# state. A test that leaves one of these inherited is asserting about the host.
# Not ramScratchBase, ramScratchDiskBase or TMPDIR: those are the RESOLUTION
# INPUTS the scratch picker walks, and which of them wins is itself what
# test_ramscratch exercises - forcing one overrides the logic under test rather
# than isolating it. What is forced here is the per-SCRIPT knobs, which name
# where a run works once the picking is done.
_SCRATCH_KNOBS = (
    "ramBase",
    "censusRamBase",
    "comicsRamBase",
    "musicRamBase",
    "readLibraryRamBase",
)

# State a run records outside its own scratch, which two tests sharing a machine
# would otherwise read from each other.
_STATE_KNOBS = (
    "PAUSE_DIR",
    "PAUSE_JOBS",
    "PAUSE_FLAG",
    "PAUSE_ACCUM",
    "PAUSE_KEY_PID",
    "SAFETY_LOG",
    "ABORT_FLAG",
    "UNCOUNTED_PROGRESS_WARNED",
)


@pytest.fixture(autouse=True)
def private_workspace(tmp_path_factory, monkeypatch):
    """Point everything that names a place to work at this test's own directory.

    Autouse and not opt-in: a test that forgets is exactly the one that reaches
    /dev/shm, and it would pass on its own and fail in a pool - the shape that
    is hardest to attribute and easiest to blame on the code under test.
    """
    # Beside the test's own tmp_path and never inside it: a great many tests
    # assert on what tmp_path HOLDS - that a failed extraction left nothing,
    # that exactly one file moved - and an isolation directory sitting in there
    # would be counted as the code under test's own leavings.
    scratch = tmp_path_factory.mktemp("workspace")
    for name in _SCRATCH_KNOBS:
        monkeypatch.setenv(name, str(scratch))
    for name in _STATE_KNOBS:
        monkeypatch.delenv(name, raising=False)

    # The module keeps the base it settled on in module state, so clearing the
    # environment alone would leave a previous test's directory in force.
    from medialib.lib import ramscratch
    monkeypatch.setattr(ramscratch._STATE, "ram_base", "", raising=False)

    # The other piece of per-process state a test can set and the next test
    # would inherit: what the host's iconv was found to do. A case that stands
    # a fake iconv in front of the fold pins the answer for the whole session
    # otherwise, and the case that suffers is whichever one runs next.
    from medialib.lib import tmdblookup
    tmdblookup.reset_iconv_flavour()
    yield scratch
    tmdblookup.reset_iconv_flavour()
    monkeypatch.setattr(ramscratch._STATE, "ram_base", "", raising=False)


@pytest.fixture
def fake_command(monkeypatch, tmp_path_factory):
    """Point one command name at a throwaway module.

    A command starts another one as a child process (item 5.3), so a test that
    wants to watch what the child was asked to do has two choices: mock the
    starting, or give it something harmless to start. This is the second, and it
    keeps what the first would lose - a real process, the real argv, and the real
    environment the caller decided to hand over, which is what several of these
    tests are actually about.

    The body is a module rather than a script because that is what a command is
    now: ``sys.executable -m <module>``.
    """
    import os

    from medialib import commands

    def install(name, body, module=None):
        module = module or ("fake_" + name.replace("-", "_"))
        directory = tmp_path_factory.mktemp("fakecommand")
        (directory / (module + ".py")).write_text(body)
        existing = os.environ.get("PYTHONPATH", "")
        monkeypatch.setenv("PYTHONPATH", str(directory)
                           + (os.pathsep + existing if existing else ""))
        monkeypatch.setitem(commands.COMMANDS, name, module)
        return directory

    return install


@pytest.fixture
def sandbox(tmp_path):
    """A private working directory, a bin directory ahead of PATH, and a way to
    start a command in both.

    ``bin`` comes FIRST and the host's PATH follows, so a case's stubs stand in
    for the heavy tools while the ordinary ones a run reaches for - `mktemp`,
    `rmdir`, `find` - are still there. ``narrow()`` drops the rest of PATH, which
    is how the "without mkvtoolnix" and "without fdupes" cases make a tool
    genuinely absent.
    """
    import shutil

    from tests import blackbox

    work_dir = tmp_path / "work"
    bin_dir = tmp_path / "bin"
    work_dir.mkdir()
    bin_dir.mkdir()
    blackbox.link_real_python(bin_dir)

    class Sandbox:
        path = os.pathsep.join([str(bin_dir), os.environ.get("PATH", "")])
        bin = bin_dir
        work = work_dir

        def linking(self, *names):
            """Symlink these host tools into the sandbox bin. With `narrow()`
            that makes a PATH holding exactly what a case names - the only way
            to say "this tool is absent" on a host that has it installed."""
            for name in names:
                found = shutil.which(name)
                if found is None:
                    pytest.fail("the host has no %s, so this PATH cannot be "
                                "built" % name)
                (bin_dir / name).symlink_to(found)
            return self

        def narrow(self):
            """Only this sandbox's own bin: every tool it does not hold is
            absent, which is a claim several cases are entirely about."""
            self.path = str(bin_dir)
            return self

        def with_media_stubs(self, exclude=()):
            blackbox.install_media_stubs(bin_dir, exclude=exclude)
            return self

        def with_tools(self, *names, body=":"):
            """Tools that exist and do nothing, for a preflight to find."""
            for name in names:
                self.with_tool(name, body)
            return self

        def with_tool(self, name, body):
            """One tool, scripted. A case that needs a probe to answer something
            in particular writes the answer here rather than mocking a function -
            the point of this tier is that the command really calls out."""
            tool = bin_dir / name
            tool.write_text("#!/usr/bin/env bash\n%s\n" % body,
                            encoding="ascii")
            tool.chmod(0o755)
            return self

        def run(self, command, *args, cwd=None, **kwargs):
            # setdefault, and never `path=path or self.path` at a call site: an
            # explicit None reaching blackbox.run means the HOST's PATH, which is
            # a sandbox that is not one.
            if kwargs.get("path") is None:
                kwargs["path"] = self.path
            if kwargs.get("env") is None:
                kwargs.pop("env", None)
            return blackbox.run(command, *args, cwd=cwd or self.work, **kwargs)

    return Sandbox()


# --- the suite is the whole suite --------------------------------------------
# A runner counts what it DISCOVERS, so "all passed" is not by itself a claim
# that the suite ran: this repository's bash runner once reported 90 files
# passing with 72 of its 98 missing from disk. pytest has the same hole one
# level in - a renamed directory, a `testpaths` that stops matching, a module
# that stops being importable - and the run comes back smaller and still green.
#
# The floor is per FILE rather than a total, because a total is a number
# somebody has to keep in step and this is not: every `test_*.py` under `tests/`
# has to hand over at least one case. It needs no allow-list, it cannot go
# stale, and it names the file that went quiet instead of reporting a shortfall
# somebody then has to hunt for.
#
# Counted as pytest collects rather than at the end, because by the end `-m` has
# deselected the media tier and a media-only file would read as a quiet one.
#
# The count underneath is the coarse backstop for the direction a per-file check
# cannot see - files deleted wholesale, where nothing is left on disk to be
# missing. It sits far below the real total on purpose: a smoke alarm, not a
# scale. Lowering it is a decision about what the suite is, and never the way to
# make a red run green.
_MINIMUM_CASES = 4500

_collected_from: set[str] = set()
_collected_cases = 0


def pytest_itemcollected(item):
    global _collected_cases
    _collected_cases += 1
    _collected_from.add(str(item.path))


def pytest_collection_finish(session):
    """Refuse a run that is quietly smaller than the repository.

    Only a whole-suite run can say anything about what is missing from it, and
    `pytest tests/lib/test_cubes.py` is not one. `config.args` is the configured
    `testpaths` verbatim until a path argument replaces it, which is exactly the
    distinction - `-k` and `-m` narrow what RUNS and leave collection alone.
    """
    config = session.config
    if list(config.args) != list(config.getini("testpaths")):
        return

    root = pathlib.Path(__file__).parent
    quiet = sorted(str(path.relative_to(root))
                   for path in root.rglob("test_*.py")
                   if str(path) not in _collected_from)
    if quiet:
        raise pytest.UsageError(
            "%d test file(s) on disk handed over no cases, so this run is not "
            "the suite: %s" % (len(quiet), ", ".join(quiet)))

    if _collected_cases < _MINIMUM_CASES:
        raise pytest.UsageError(
            "collected %d cases, under the floor of %d - test files have gone "
            "missing from disk, not just cases."
            % (_collected_cases, _MINIMUM_CASES))
