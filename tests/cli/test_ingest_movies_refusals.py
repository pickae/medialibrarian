"""The refusals ingest-movies makes before a library is touched: a phase asked
beside a phase of its own, the tagging asked without its key or its tool, and
a fragments file that cannot be read.

Each one is exit 1 with the reason, the library byte-identical, and nothing
written - the phases they guard walk every folder given, so a run that started
any of them would have to be undone by hand.
"""

from __future__ import annotations

import pytest

from medialib.cli import ingest_movies_run as run
from medialib.lib import tmdblookup

pytestmark = pytest.mark.fs


def _library(root, name: str, film: str = "The Movie (1999)"):
    """A folder holding one film, which is what makes it worth walking."""
    folder = root / name / film
    folder.mkdir(parents=True)
    (folder / (film + ".mkv")).touch()
    return root / name


class TestANamesBesideAnotherPhase:
    """-n names the commentary tracks by their transcripts, so every other
    phase it would have to share a run with is refused: the order is not
    guessed, and the answer is which of the two runs comes first."""

    @pytest.mark.parametrize("other", [["-a"], ["-c"], ["-s"], ["-t"]])
    def test_a_phase_of_its_own_is_refused(self, other, tmp_path, capsys):
        assert run.main(["-n"] + other + [str(_library(tmp_path, "Films"))]) \
            == 1
        errors = capsys.readouterr().err
        assert "-n names the commentary tracks and nothing else" in errors
        assert "Nothing was changed." in errors

    def test_an_id_list_is_refused(self, tmp_path, capsys):
        assert run.main(["-n", "-i", str(tmp_path / "nope.tsv"),
                         str(_library(tmp_path, "Films"))]) == 1
        errors = capsys.readouterr().err
        assert "-n names the commentary tracks and nothing else" in errors
        assert "Nothing was changed." in errors


class TestTaggingWithoutWhatItNeeds:
    """-t tags by what TMDb answers, and answers need a key: without one there
    is nothing to tag with, and the run says so instead of walking the
    library and naming nothing. The key is read from the environment, so the
    machine's real one is set aside for the test."""

    def test_without_the_api_key(self, monkeypatch, tmp_path, capsys):
        monkeypatch.delenv("tmdbApiKey", raising=False)
        monkeypatch.setenv("CLI_SCRIPT_DIR", str(tmp_path))
        assert run.main(["-t", str(_library(tmp_path, "Films"))]) == 1
        errors = capsys.readouterr().err
        assert "tmdbApiKey is not set, so there is nothing to tag with." \
            in errors

    def test_without_curl(self, monkeypatch, tmp_path, capsys):
        """The key is there, but the tool that carries it to the site is not:
        refused at the door, naming the tool, with the library untouched."""
        empty = tmp_path / "empty"
        empty.mkdir()
        monkeypatch.setenv("tmdbApiKey", "a-key")
        monkeypatch.setenv("CLI_SCRIPT_DIR", str(tmp_path))
        monkeypatch.setenv("PATH", str(empty))
        assert run.main(["-t", str(_library(tmp_path, "Films"))]) == 1
        errors = capsys.readouterr().err
        assert "it needs a tool this machine does not have" in errors
        assert "curl" in errors


class TestAFragmentsFileThatCannotBeRead:
    """-f names the fragments this run removes, and a path that holds none is
    never quietly dropped: the run would clean a whole library without the
    fragments someone asked for, and undo that is by hand. The same answer for
    a file that is there but empty."""

    def test_a_missing_file_is_refused(self, tmp_path, capsys):
        missing = tmp_path / "fragments.txt"
        assert run.main(["-f", str(missing), str(_library(tmp_path, "Films"))]) \
            == 1
        errors = capsys.readouterr().err
        assert 'The fragments file "%s" does not exist or is empty.' \
            % missing in errors

    def test_an_empty_file_is_refused(self, tmp_path, capsys):
        empty = tmp_path / "fragments.txt"
        empty.touch()
        assert run.main(["-f", str(empty), str(_library(tmp_path, "Films"))]) \
            == 1
        errors = capsys.readouterr().err
        assert 'The fragments file "%s" does not exist or is empty.' \
            % empty in errors


class TestUnderQuiet:
    """-q is errors only. A refusal is an error and is said in full; of the file
    name limit's closing report, the names cut to fit are a warning and go,
    and the folders left unrenamed are a failure and stay."""

    def test_a_refusal_is_still_said(self, monkeypatch, tmp_path, capsys):
        # Set here so the -q the run settles is undone after the test.
        monkeypatch.setenv("LOG_VERBOSITY", "")
        monkeypatch.delenv("tmdbApiKey", raising=False)
        monkeypatch.setenv("CLI_SCRIPT_DIR", str(tmp_path))
        assert run.main(["-q", "-t", str(_library(tmp_path, "Films"))]) == 1
        errors = capsys.readouterr().err
        assert "tmdbApiKey is not set, so there is nothing to tag with." \
            in errors

    @pytest.fixture
    def long_names(self):
        names = tmdblookup.LongNames()
        names.crop("/films/A", "A.commentary.srt", "A.comm.srt")
        names.refuse("/films/B", "B.mkv", "too long")
        return names

    def test_the_cut_names_go_and_the_unrenamed_folders_stay(
            self, monkeypatch, capsys, long_names):
        monkeypatch.setenv("LOG_VERBOSITY", "quiet")
        run._report_long_names(long_names)
        errors = capsys.readouterr().err
        assert "WARNING" not in errors
        assert "A.comm.srt" not in errors
        assert errors.startswith("ERROR: 1 folder(s) were left unrenamed")
        assert "    B.mkv (too long)\n" in errors

    def test_and_at_normal_verbosity_both_are_said(self, monkeypatch, capsys,
                                                   long_names):
        monkeypatch.setenv("LOG_VERBOSITY", "")
        run._report_long_names(long_names)
        assert capsys.readouterr().err == "".join(
            line + "\n" for line in long_names.report())


class TestADryRunUnderQuiet:
    """A dry run's account of what it would change is its result, so -q keeps
    it; under -w the same lines are progress, and -q drops them."""

    def test_the_preview_is_said_under_quiet(self, monkeypatch, capsys):
        monkeypatch.setenv("LOG_VERBOSITY", "quiet")
        monkeypatch.delenv("LOG_TIMESTAMPS", raising=False)
        run._preview_log(write=False)('    would rename: "a" -> "b"')
        assert capsys.readouterr().err == '==>     would rename: "a" -> "b"\n'

    def test_but_under_w_it_is_progress(self, monkeypatch, capsys):
        monkeypatch.setenv("LOG_VERBOSITY", "quiet")
        run._preview_log(write=True)("TMDb: tagged")
        assert capsys.readouterr().err == ""
