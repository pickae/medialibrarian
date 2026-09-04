"""The white box for medialib/lib/ffmpegselect.py.

The shared choice of which ffmpeg build a run uses. Every script here calls
ffmpeg by
name, so what the module decides is not "which path is stored in a variable"
but "what does ``ffmpeg`` mean for the rest of the run" - which is what the
assertions check: after ``select_ffmpeg``, the plain command name reaches the
build that was chosen, in this process and in anything it starts.

The candidates are shell scripts that identify themselves, which is all a
selection test needs - none of them is asked to encode anything. The stubs
announce in the "the ... one" shape the probe below keys on, and the probe
accepts only announcements it was told about, which is what keeps the
host's own builds - the ladder's real absolute rungs - from deciding the
outcome on whichever host this runs on.
"""

import os
import shutil
import subprocess

import pytest

from medialib.lib import ffmpegselect

pytestmark = pytest.mark.stubbed

# The handful of tools the module shells out to (and the scratch's cleanup
# and free-space decision use), reached through the case's own PATH.
_TOOLS = ("mktemp", "rmdir", "ln", "head", "cut", "env", "bash", "rm",
          "chmod", "df", "tail", "stat", "mkdir")


class _World:
    """The machine a selection runs on: PATH directories of announcing stubs,
    a home directory, and the tools the module shells out to."""

    def __init__(self, tmp_path, monkeypatch):
        self.root = tmp_path / "root"
        self.root.mkdir()
        self.home = self.root / "home"
        self.home.mkdir()
        self.scratch = self.root / "scratch"
        self.scratch.mkdir()
        self.tools = self.root / "tools"
        self.tools.mkdir()
        for tool in _TOOLS:
            found = shutil.which(tool)
            if found:
                os.symlink(found, str(self.tools / tool))
        self.path_dirs = []

    def stub(self, directory, name, says):
        """An executable that says which one it is when run."""
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / name
        path.write_text("#!/bin/sh\n" + "printf '%s\\n' '" + says + "'\n")
        path.chmod(0o755)
        return str(path)

    def empty(self, directory, name):
        """A non-executable file wearing the name: found, not usable."""
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / name
        path.write_text("x")
        return str(path)

    def enter(self, monkeypatch, override=None, usage=None):
        monkeypatch.setenv("PATH",
                           os.pathsep.join(self.path_dirs + [str(self.tools)]))
        monkeypatch.setenv("HOME", str(self.home))
        monkeypatch.setenv("ramScratchBase", str(self.scratch))
        monkeypatch.setenv("ffmpegOverride", override or "")
        monkeypatch.setenv("usage", usage or "")
        ffmpegselect.reset_state()


@pytest.fixture
def world(tmp_path, monkeypatch):
    return _World(tmp_path, monkeypatch)


def run(name, *args):
    return subprocess.run([name, *args], stdout=subprocess.PIPE,
                          stderr=subprocess.DEVNULL)


def reaches():
    """What the plain command name ``ffmpeg`` runs now."""
    return run("ffmpeg").stdout.decode().strip()


def takes_everything(candidate):
    """The probe of the caller that has one: accepts only a build that
    announces itself, and not the too-old one - so the host's own builds,
    which announce nothing of the sort, are never the answer."""
    said = subprocess.run([candidate], stdout=subprocess.PIPE,
                          stderr=subprocess.DEVNULL)
    text = said.stdout.decode().strip()
    return text.startswith("the ") and text != "the too-old one"


# --- PATH's own ffmpeg is the choice, and nothing is touched -------------------

class TestPathOwn:
    def test_the_ffmpeg_on_path_is_the_one_used(self, world, monkeypatch):
        chosen = world.stub(world.root / "pathBin", "ffmpeg", "the one on PATH")
        world.path_dirs.append(str(world.root / "pathBin"))
        world.enter(monkeypatch)
        ffmpegselect.select_ffmpeg()
        assert ffmpegselect._STATE.selected == chosen
        assert reaches() == "the one on PATH"

    def test_choosing_what_path_already_offered_changes_nothing(self, world, monkeypatch):
        world.stub(world.root / "pathBin", "ffmpeg", "the one on PATH")
        world.path_dirs.append(str(world.root / "pathBin"))
        world.enter(monkeypatch)
        before = os.environ["PATH"]
        ffmpegselect.select_ffmpeg()
        assert ffmpegselect._STATE.pinned == 0
        assert os.environ["PATH"] == before
        # A caller with no probe gets a selection that lacks nothing.
        assert ffmpegselect._STATE.full == 1

    def test_the_run_says_nothing_about_it(self, world, monkeypatch, capsys):
        world.stub(world.root / "pathBin", "ffmpeg", "the one on PATH")
        world.path_dirs.append(str(world.root / "pathBin"))
        world.enter(monkeypatch)
        ffmpegselect.select_ffmpeg()
        ffmpegselect.report_ffmpeg_selection()
        assert capsys.readouterr().err == ""

    def test_unless_it_is_a_run_that_restates_every_decision(self, world, monkeypatch, capsys):
        chosen = world.stub(world.root / "pathBin", "ffmpeg", "the one on PATH")
        world.path_dirs.append(str(world.root / "pathBin"))
        world.enter(monkeypatch)
        ffmpegselect.select_ffmpeg()
        ffmpegselect.report_ffmpeg_selection(always=True)
        # The stub's own announcement is what the version field reads: the
        # third word of its first line.
        assert capsys.readouterr().err == f"Using ffmpeg: {chosen} (on)\n"


# --- a PATH with no ffmpeg falls down the ladder -------------------------------

class TestLadderFallthrough:
    def test_a_path_without_an_ffmpeg_falls_through_to_home(self, world, monkeypatch):
        chosen = world.stub(world.home / ".local" / "bin", "ffmpeg",
                            "the hand-installed one")
        world.enter(monkeypatch)
        ffmpegselect.select_ffmpeg()
        assert ffmpegselect._STATE.selected == chosen
        assert reaches() == "the hand-installed one"
        assert ffmpegselect._STATE.pinned == 1

    def test_a_child_process_reaches_it_too_without_being_told(self, world, monkeypatch):
        world.stub(world.home / ".local" / "bin", "ffmpeg",
                   "the hand-installed one")
        world.enter(monkeypatch)
        ffmpegselect.select_ffmpeg()
        assert run("bash", "-c", "ffmpeg").stdout.decode().strip() \
            == "the hand-installed one"

    def test_a_choice_that_overrules_path_is_said_out_loud(self, world, monkeypatch, capsys):
        chosen = world.stub(world.home / ".local" / "bin", "ffmpeg",
                            "the hand-installed one")
        world.enter(monkeypatch)
        ffmpegselect.select_ffmpeg()
        ffmpegselect.report_ffmpeg_selection()
        assert capsys.readouterr().err \
            == f"Using ffmpeg: {chosen} (one)\n"


# --- the ladder itself: order, and no candidate offered twice ------------------

class TestCandidates:
    def test_path_resolves_to_the_file_home_names_and_it_is_offered_once(self, world, monkeypatch):
        rungs_dir = world.home / ".local" / "bin"
        stub = world.stub(rungs_dir, "ffmpeg", "the hand-installed one")
        world.path_dirs.append(str(rungs_dir))
        world.enter(monkeypatch)
        candidates = ffmpegselect.ffmpeg_candidates()
        assert candidates.count(stub) == 1
        assert candidates[0] == stub

    def test_path_own_ffmpeg_is_always_the_first_rung(self, world, monkeypatch):
        stub = world.stub(world.root / "pathBin", "ffmpeg", "the one on PATH")
        world.stub(world.home / ".local" / "bin", "ffmpeg",
                   "the hand-installed one")
        world.path_dirs.append(str(world.root / "pathBin"))
        world.enter(monkeypatch)
        assert ffmpegselect.ffmpeg_candidates()[0] == stub

    def test_no_candidate_is_held_twice(self, world, monkeypatch):
        rungs_dir = world.home / ".local" / "bin"
        world.stub(rungs_dir, "ffmpeg", "the hand-installed one")
        world.stub(world.root / "pathBin", "ffmpeg", "the one on PATH")
        world.path_dirs += [str(rungs_dir), str(world.root / "pathBin")]
        world.enter(monkeypatch)
        candidates = ffmpegselect.ffmpeg_candidates()
        assert len(candidates) == len(set(candidates))

    @pytest.mark.skipif(not os.access("/usr/bin/ffmpeg", os.X_OK),
                        reason="no /usr/bin/ffmpeg on this host")
    def test_the_distro_build_is_the_last_one_considered(self, world, monkeypatch):
        world.stub(world.root / "pathBin", "ffmpeg", "the one on PATH")
        world.path_dirs.append(str(world.root / "pathBin"))
        world.enter(monkeypatch)
        assert ffmpegselect.ffmpeg_candidates()[-1] == "/usr/bin/ffmpeg"


# --- ffmpegOverride is an instruction, not a preference ------------------------

class TestOverride:
    def test_the_override_is_used_even_when_path_offers_one(self, world, monkeypatch):
        pinned = world.stub(world.root / "pinned", "ffmpeg", "the pinned one")
        world.stub(world.root / "pathBin", "ffmpeg", "the one on PATH")
        world.path_dirs.append(str(world.root / "pathBin"))
        world.enter(monkeypatch, override=pinned)
        ffmpegselect.select_ffmpeg()
        assert ffmpegselect._STATE.selected == pinned
        assert reaches() == "the pinned one"

    def test_an_override_naming_nothing_executable_refuses_the_run(
            self, world, monkeypatch, capsys):
        bad = world.empty(world.root / "pinned", "ffmpeg")
        world.stub(world.root / "pathBin", "ffmpeg", "the one on PATH")
        world.path_dirs.append(str(world.root / "pathBin"))
        world.enter(monkeypatch, override=bad, usage="Usage: the script [file]")
        with pytest.raises(ffmpegselect.FfmpegOverrideRefused):
            ffmpegselect.select_ffmpeg()
        err = capsys.readouterr().err
        assert err == ('ffmpegOverride names "%s", which is not an '
                       "executable file.\n\nUsage: the script [file]\n" % bad)

    def test_the_refusal_says_which_value_was_wrong_without_usage(self, world, monkeypatch, capsys):
        bad = world.root / "nowhere" / "ffmpeg"
        world.enter(monkeypatch, override=str(bad))
        with pytest.raises(ffmpegselect.FfmpegOverrideRefused):
            ffmpegselect.select_ffmpeg()
        err = capsys.readouterr().err
        assert err == ('ffmpegOverride names "%s", which is not an '
                       "executable file.\n" % str(bad))


# --- a caller's probe decides what "can do the job" means ----------------------

class TestProbe:
    def test_a_candidate_the_probe_rejects_is_passed_over(self, world, monkeypatch):
        world.stub(world.root / "tooOld", "ffmpeg", "the too-old one")
        world.stub(world.home / ".local" / "bin", "ffmpeg",
                   "the hand-installed one")
        world.path_dirs.append(str(world.root / "tooOld"))
        world.enter(monkeypatch)
        ffmpegselect.select_ffmpeg(takes_everything)
        assert reaches() == "the hand-installed one"
        assert ffmpegselect._STATE.full == 1

    def test_with_no_candidate_accepted_the_run_still_has_an_ffmpeg(self, world, monkeypatch):
        first = world.stub(world.root / "tooOld", "ffmpeg", "the too-old one")
        world.path_dirs.append(str(world.root / "tooOld"))
        world.enter(monkeypatch)
        def refuses_everything(candidate):
            return False
        ffmpegselect.select_ffmpeg(refuses_everything)
        assert ffmpegselect._STATE.selected == first
        assert ffmpegselect._STATE.full == 0

    def test_a_probe_that_accepts_everything_is_answered_whole(self, world, monkeypatch):
        stub = world.stub(world.root / "tooOld", "ffmpeg", "the too-old one")
        world.path_dirs.append(str(world.root / "tooOld"))
        world.enter(monkeypatch)
        ffmpegselect.select_ffmpeg(lambda candidate: True)
        assert ffmpegselect._STATE.selected == stub
        assert ffmpegselect._STATE.full == 1

    def test_a_probe_overrides_a_whole_selection_too(self, world, monkeypatch):
        pinned = world.stub(world.root / "pinned", "ffmpeg", "the pinned one")
        world.enter(monkeypatch, override=pinned)
        def refuses_everything(candidate):
            return False
        ffmpegselect.select_ffmpeg(refuses_everything)
        assert ffmpegselect._STATE.selected == pinned
        assert ffmpegselect._STATE.full == 0


# --- one decision per run ------------------------------------------------------

class TestOneDecision:
    def test_asking_again_in_the_same_shell_changes_nothing(self, world, monkeypatch):
        world.stub(world.home / ".local" / "bin", "ffmpeg",
                   "the hand-installed one")
        world.enter(monkeypatch)
        ffmpegselect.select_ffmpeg()
        first = os.environ["PATH"]
        ffmpegselect.select_ffmpeg()
        assert os.environ["PATH"] == first
        assert ffmpegselect._STATE.pinned == 1

    def test_a_caller_that_brings_a_probe_still_gets_its_answer(self, world, monkeypatch):
        stub = world.stub(world.root / "pathBin", "ffmpeg", "the one on PATH")
        world.path_dirs.append(str(world.root / "pathBin"))
        world.enter(monkeypatch)
        ffmpegselect.select_ffmpeg()
        ffmpegselect.select_ffmpeg(takes_everything)
        assert ffmpegselect._STATE.full == 1
        ffmpegselect.select_ffmpeg(lambda candidate: False)
        assert ffmpegselect._STATE.full == 0
        assert ffmpegselect._STATE.selected == stub


# --- the line itself ------------------------------------------------------------

class TestReport:
    def test_no_selection_says_nothing(self, world, monkeypatch, capsys):
        world.enter(monkeypatch)
        ffmpegselect.report_ffmpeg_selection(always=True)
        assert capsys.readouterr().err == ""

    def test_a_build_that_reports_no_version_is_named_as_such(self, world, monkeypatch, capsys):
        # One word: cut, having no -s, hands the whole line back when it
        # holds fewer than three words, so the version field is the word.
        chosen = world.stub(world.home / ".local" / "bin", "ffmpeg", "quiet")
        world.enter(monkeypatch)
        ffmpegselect.select_ffmpeg()
        ffmpegselect.report_ffmpeg_selection()
        assert capsys.readouterr().err \
            == f"Using ffmpeg: {chosen} (quiet)\n"

    def test_a_build_that_prints_nothing_at_all_is_named_as_such(self, world, monkeypatch, capsys):
        # The stub that answers nothing is where a broken build lands.
        chosen = world.home / ".local" / "bin" / "ffmpeg"
        chosen.parent.mkdir(parents=True)
        chosen.write_text("#!/bin/sh\nexit 0\n")
        chosen.chmod(0o755)
        world.enter(monkeypatch)
        ffmpegselect.select_ffmpeg()
        ffmpegselect.report_ffmpeg_selection()
        assert capsys.readouterr().err \
            == f"Using ffmpeg: {chosen} (version not reported)\n"