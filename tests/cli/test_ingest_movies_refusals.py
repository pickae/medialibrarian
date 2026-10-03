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
