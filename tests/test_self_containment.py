"""The rule that makes this suite safe to run in parallel, enforced instead of
reviewed.

`conftest.py` states two rules. The second - do not assert on global state - is
structural there: it points every per-command scratch knob at the test's own
directory. This is the first: **allocate every fixture through a fresh
directory, never a fixed name.** Two tests that both write
``/tmp/fixture`` pass alone and fail in a pool, and the failure looks like a
defect in the code under test.

It is asserted about WRITING and not about naming, which is the whole difficulty:
a path is often just a string. `viewer_grain_export_sql(content, "/tmp/x.csv")`
pastes one into generated SQL, `video_source_for(..., "/dev/shm/scratch/dv81.mkv")`
returns one, and `test_ramscratch` is *about* those roots - a dozen or so
literals across the suite that name a shared root and touch nothing. So the check walks
the syntax tree and asks whether a literal reaches a call that CREATES something,
which none of those do.
"""

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.pure

_TESTS = Path(__file__).resolve().parent

# The writable roots every process on the machine shares.
SHARED_ROOTS = ("/tmp/", "/dev/shm", "/var/tmp/")

# Calls that bring something into existence at a path they are given.
_WRITERS = {
    "open", "makedirs", "mkdir", "write_text", "write_bytes", "touch",
    "copyfile", "copy", "copy2", "copytree", "mkdtemp", "mkstemp",
    "NamedTemporaryFile", "TemporaryDirectory", "symlink_to", "symlink",
    "rename", "replace", "link_to", "hardlink_to",
}


def _shared(text):
    return isinstance(text, str) and text.startswith(SHARED_ROOTS)


def _offences(source, name):
    """Every literal under a shared root that reaches a creating call."""
    found = []
    for node in ast.walk(ast.parse(source, filename=name)):
        if not isinstance(node, ast.Call):
            continue
        called = node.func
        label = getattr(called, "attr", None) or getattr(called, "id", None)
        if label not in _WRITERS:
            continue
        args = list(node.args) + [kw.value for kw in node.keywords]
        # Path("/tmp/x").mkdir() hides the literal in the receiver.
        if isinstance(called, ast.Attribute):
            args.append(called.value)
        for arg in args:
            for inner in ast.walk(arg):
                if isinstance(inner, ast.Constant) and _shared(inner.value):
                    found.append((node.lineno, label, inner.value))
    return found


def _test_sources():
    return sorted(p for p in _TESTS.rglob("test_*.py"))


def test_there_are_test_files_to_check():
    """A glob that matched nothing would satisfy the check below by never
    running it."""
    assert len(_test_sources()) >= 50


@pytest.mark.parametrize("path", _test_sources(), ids=lambda p: p.name)
def test_no_fixture_is_built_at_a_fixed_path(path):
    offences = _offences(path.read_text(encoding="utf-8"), path.name)
    assert not offences, "\n".join(
        "%s:%d builds a fixture at the shared path %r with %s()"
        % (path.name, line, value, label) for line, label, value in offences)


class TestTheCheckItself:
    """The verify condition of this item is that a deliberately fixed-path
    fixture FAILS, so the checker is pointed at one."""

    @pytest.mark.parametrize("snippet", [
        'open("/tmp/fixture", "w")',
        'os.makedirs("/tmp/fixture/deep")',
        'Path("/dev/shm/fixture").mkdir()',
        'Path("/tmp/f").write_text("x")',
        'shutil.copyfile(src, "/var/tmp/fixture")',
        'tempfile.mkdtemp(dir="/dev/shm")',
        'os.symlink(target, "/tmp/link")',
    ])
    def test_it_catches_a_fixed_path_fixture(self, snippet):
        assert _offences(snippet, "<case>")

    @pytest.mark.parametrize("snippet", [
        # the shapes the suite really contains: a path that is only ever a
        # string, never a place something is made
        'viewer_grain_export_sql(content, "/tmp/x.csv")',
        'cv.video_source_for("sub/film.mkv", "/lib/in", "/dev/shm/s/dv81.mkv")',
        'assert ramscratch.filesystem_type("/dev/shm") == "tmpfs"',
        'ramscratch.add_exit_cleanup(["/dev/shm/a-command.run.XXXX"])',
        # and the shape that is correct: a directory pytest handed out
        'open(tmp_path / "fixture", "w")',
        'os.makedirs(str(tmp_path / "deep"))',
    ])
    def test_it_passes_a_path_that_is_only_named(self, snippet):
        assert not _offences(snippet, "<case>")

    def test_it_reads_the_receiver_of_a_method_call(self):
        """Path("/tmp/x").mkdir() hides the literal behind the dot, which a
        check reading only the ARGUMENTS would wave through."""
        assert _offences('Path("/tmp/x").mkdir()', "<case>")
        assert not _offences('Path(tmp_path / "x").mkdir()', "<case>")
