"""The input-folder guards every command that takes one has to keep.

Three promises, and all three are only visible from outside, by running the real
command:

  1. **The input folder is never deleted.** Several commands prune empty folders
     as part of their cleanup, and a plain "delete every empty directory" also
     removes the input itself when it is empty - so a mistyped or not-yet-filled
     path silently disappeared and the next step failed on a missing directory.
     What the cases here pin is that the REFUSAL path leaves it alone; they
     cannot reach the prune, because promise 2 refuses such an input before the
     cleanup runs. The prune's own root-keeping is pinned where it can fail -
     `tests/lib/test_downloadcleanup.py` and
     `TestTheInputFolderSurvivesItsOwnPruning` in
     `test_clean_folder_structure_cli.py`.
  2. **An unusable input is explained, not worked through.** When the folder holds
     nothing the command can use, it says what it looked for and where and exits
     non-zero, instead of reporting "0 files" and going through the motions.
  3. **The output folder may not sit inside the input.** Every command here writes
     the same kind of file it looks for, so an output inside the input hands the
     next run its own output - and the input cleanup reaches into the finished
     library first.

Both cases a user actually hits are covered: the folder is EMPTY, and the folder
HAS content but none of it is relevant. No media and no external tool: every guard
fires before the first heavy call.

Parametrised over commands rather than over rules. One guard applying to fifteen
commands is one claim fifteen times; a case that covered several different rules
at once would only look like this one.
"""

from __future__ import annotations

import pytest

from tests import blackbox

pytestmark = pytest.mark.fs

# (command, extra options, how many folder arguments it takes).
_EMPTY = [
    ("convert-comics", (), 2),
    ("convert-images", (), 2),
    ("convert-images", ("-r",), 2),
    ("convert-audio", (), 2),
    ("convert-audio", ("-a",), 2),
    ("convert-video", (), 2),
    ("concat-audio", (), 2),
    ("ingest-books", (), 2),
    ("read-library", (), 2),
    ("ingest-music", (), 2),
    ("ingest-movies", (), 1),
    ("clean-folder-structure", (), 1),
    ("clean-folder-structure", ("-n",), 1),
    ("clean-folder-structure", ("-s",), 1),
    ("find-fragment-candidates", (), 1),
]

# The same, for a folder that holds something the command cannot use. `junkimg`
# rather than `junk` where a .txt IS the thing the command wants: an e-book to
# `ingest-books`, and a book a TTS engine reads perfectly well to `read-library`.
_IRRELEVANT = [
    ("convert-comics", (), 2, "junk"),
    ("convert-images", (), 2, "junk"),
    ("convert-images", ("-r",), 2, "junk"),
    ("convert-audio", (), 2, "junk"),
    ("convert-audio", ("-a",), 2, "junk"),
    ("convert-video", (), 2, "junk"),
    ("concat-audio", (), 2, "junk"),
    ("ingest-books", (), 2, "junkimg"),
    ("read-library", (), 2, "junkimg"),
    ("ingest-movies", (), 1, "junk"),
]

# Commands taking an input and an output folder, plus `convert-and-concat`, which
# judges the pair itself before it allocates its RAM scratch rather than half a
# run later inside the command it delegates to.
_NESTED = [
    ("convert-comics", ()),
    ("convert-images", ()),
    ("convert-images", ("-r",)),
    ("convert-audio", ()),
    ("convert-video", ()),
    ("concat-audio", ()),
    ("convert-and-concat", ()),
    ("ingest-books", ()),
    ("read-library", ()),
    ("ingest-music", ()),
]


def _id(entry):
    command, options = entry[0], entry[1]
    return " ".join([command, *options])


@pytest.fixture
def guards(sandbox, tmp_path):
    """An input folder of a given kind, and a run against it."""
    base = tmp_path / "inputs"
    base.mkdir()
    counter = iter(range(1, 1000))

    def fixture(kind):
        folder = base / ("in%d" % next(counter))
        folder.mkdir()
        if kind == "junk":
            (folder / "readme.txt").write_text("not relevant")
        elif kind == "junkimg":
            (folder / "artwork.jpg").write_text("not relevant")
        if kind != "empty":
            (folder / "empty sub").mkdir()
        return folder

    sandbox.fixture = fixture
    return sandbox


def _refuses(guards, command, options, argc, kind):
    """Run the command over an unusable input and answer (log, folder, before)."""
    folder = guards.fixture(kind)
    before = blackbox.tree_of(folder)
    args = [*options, folder]
    if argc == 2:
        args.append(str(folder) + ".out")
    done = guards.run(command, *args)
    assert done.returncode != 0, \
        "exited 0 over an unusable input:\n" + done.stdout + done.stderr
    return done.stdout + done.stderr, folder, before


class TestAnEmptyInputFolderIsRefused:
    """And survives being refused, which is the point: the folder a user
    mistyped or has not filled yet is still there to look at."""

    @pytest.mark.parametrize("command,options,argc", _EMPTY,
                             ids=[_id(e) for e in _EMPTY])
    def test_it_is_refused_and_explains_itself(self, guards, command, options,
                                               argc):
        """Naming the folder and saying nothing was changed, so a user can tell
        a wrong path from a wrong tool without reading the source."""
        log, folder, _ = _refuses(guards, command, options, argc, "empty")
        assert "nothing" in log.lower(), log
        assert str(folder) in log, log

    @pytest.mark.parametrize("command,options,argc", _EMPTY,
                             ids=[_id(e) for e in _EMPTY])
    def test_the_input_folder_is_still_there_and_untouched(
            self, guards, command, options, argc):
        log, folder, before = _refuses(guards, command, options, argc, "empty")
        assert folder.is_dir(), log
        assert blackbox.tree_of(folder) == before


class TestAnInputWithNothingRelevantIsRefused:
    """The other case a user hits: the folder is not empty, but holds nothing
    this command can use."""

    @pytest.mark.parametrize("command,options,argc,kind", _IRRELEVANT,
                             ids=[_id(e) for e in _IRRELEVANT])
    def test_it_is_refused_and_explains_itself(self, guards, command, options,
                                               argc, kind):
        log, folder, _ = _refuses(guards, command, options, argc, kind)
        assert "nothing" in log.lower(), log
        assert str(folder) in log, log

    @pytest.mark.parametrize("command,options,argc,kind", _IRRELEVANT,
                             ids=[_id(e) for e in _IRRELEVANT])
    def test_the_input_folder_is_still_there_and_untouched(
            self, guards, command, options, argc, kind):
        log, folder, before = _refuses(guards, command, options, argc, kind)
        assert folder.is_dir(), log
        assert blackbox.tree_of(folder) == before


class TestTheNameCleanersStillRun:
    """The counter-case, and not a gap: a command whose work is every name it
    finds has no such thing as an irrelevant input, so for those only "empty"
    means no work. They must not refuse a folder that merely holds junk."""

    @pytest.mark.parametrize("command",
                             ["clean-folder-structure",
                              "find-fragment-candidates"])
    def test_a_folder_holding_junk_is_still_work(self, guards, command):
        folder = guards.fixture("junk")
        done = guards.run(command, folder)
        assert done.returncode == 0, done.stdout + done.stderr
        assert folder.is_dir()


class TestAnOutputInsideTheInputIsRefused:
    """One guard rather than a habit each command has, because every command here
    writes to the output the same kind of file it looks for in the input.

    The input is EMPTY on purpose, which pins the ORDER as well as the refusal:
    the paths are judged before the input is so much as scanned, so the message
    has to be the nested-output one and not "nothing to do" - which is also what
    keeps this from passing for the wrong reason.
    """

    @pytest.mark.parametrize("command,options", _NESTED,
                             ids=[_id(e) for e in _NESTED])
    def test_it_exits_one_on_the_paths_and_creates_nothing(
            self, guards, command, options):
        """Exit 1 and not merely non-zero: a command whose exit trap calls
        something it has not defined yet exits 127, having skipped its own
        cleanup."""
        folder = guards.fixture("empty")
        done = guards.run(command, *options, folder, folder / "out")
        assert done.returncode == 1, done.stdout + done.stderr
        log = done.stdout + done.stderr
        assert "Refusing to write the output inside the input" in log, log
        assert not (folder / "out").exists()

    def test_ingest_musics_optional_third_folder_is_checked_too(self, guards):
        """Its first two folders are the pair every command above has; the
        optional opus copies are checked against both of them, so that one needs
        a run of its own."""
        folder = guards.fixture("empty")
        library = folder.parent / (folder.name + ".lib")
        done = guards.run("ingest-music", folder, library, library / "opus")
        log = done.stdout + done.stderr
        assert done.returncode == 1, log
        assert "Refusing to write the output inside the input" in log, log
        assert not library.exists()
