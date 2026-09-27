"""The subtitles-only run (-s): the subtitle phase and a test of the subtitles
already there, and nothing else.

What is pinned here is the boundary: the check runs per folder in the order
given, as a dry run unless -w is given, -w adds the download the full run does
and adds it after the check, and no other phase of the run touches the library.
The check itself is pinned in tests/lib/test_subtitlefiles.py.
"""

from __future__ import annotations

import pytest

from medialib.cli import ingest_movies_run as run

pytestmark = pytest.mark.fs


def _library(root, name: str, film: str = "The Movie (1999)"):
    folder = root / name / film
    folder.mkdir(parents=True)
    (folder / (film + ".mkv")).touch()
    return root / name


def _never(*_args, **_kwargs):
    raise AssertionError("a phase of the full run ran that -s refuses")


@pytest.fixture
def stubbed(monkeypatch, tmp_path):
    """Everything around the phase stood down, every other phase refused, and
    the two subtitle calls recorded in the order they were made."""
    monkeypatch.setattr(run.ffmpegselect, "select_ffmpeg", lambda: None)
    monkeypatch.setattr(run.ffmpegselect, "report_ffmpeg_selection",
                        lambda: None)
    tools: list = []
    monkeypatch.setattr(run.tooldeps, "require_tools",
                        lambda _program, specs: tools.append(specs) or 0)
    monkeypatch.setattr(run, "_settle_ffsubsync_quality",
                        lambda: "confidence")
    monkeypatch.setenv("openSubtitlesUser", "u")
    monkeypatch.setenv("openSubtitlesPassword", "p")
    logs: list = []
    monkeypatch.setattr(run, "log", logs.append)

    for name in ("cleanup", "mkv_mux", "movies_into_subfolders",
                 "extras_into_subfolders", "update_tags", "rename_folders",
                 "rename_movies", "conform_movie_names"):
        monkeypatch.setattr(run.rules, name, _never)
    for name in ("move_subs", "rename_subs"):
        monkeypatch.setattr(run.subtitlefiles, name, _never)
    monkeypatch.setattr(run.tmdblookup, "tag_plex_ids", _never)
    monkeypatch.setattr(run.commentarytranscription, "export_commentary",
                        _never)
    monkeypatch.setattr(run.ramscratch, "ram_scratch_dir", _never)
    for name in ("_transcode_opus", "improve_main_movies", "check_folders",
                 "_ingest"):
        monkeypatch.setattr(run, name, _never)

    calls: list = []
    verdicts = {"kept": [], "discarded": [], "untested": []}

    def check_subs(directory, max_offset, quality_offset, quality, write,
                   log):
        calls.append(("check", directory, quality, write))
        return {key: list(found) for key, found in verdicts.items()}

    def download_subs(directory, user, password, max_offset, quality_offset,
                      quality, log):
        calls.append(("download", directory, quality))

    monkeypatch.setattr(run.subtitlefiles, "check_subs", check_subs)
    monkeypatch.setattr(run.subtitlefiles, "download_subs", download_subs)
    script = tmp_path / "script"
    return {"calls": calls, "tools": tools, "logs": logs,
            "verdicts": verdicts, "script": str(script)}


def _main(stubbed, *argv):
    return run.main(list(argv), script_dir=stubbed["script"])


class TestThePhaseRunsAndNothingElse:
    def test_the_dry_run_checks_each_folder_in_order_and_downloads_nothing(
            self, stubbed, tmp_path):
        films = _library(tmp_path, "Films")
        docs = _library(tmp_path, "Documentaries")
        assert _main(stubbed, "-s", str(films), str(docs)) == 0
        assert stubbed["calls"] == [
            ("check", str(films), "confidence", False),
            ("check", str(docs), "confidence", False)]
        assert any(line.startswith("Dry run: nothing was changed")
                   for line in stubbed["logs"])

    def test_w_checks_then_downloads_folder_by_folder(self, stubbed,
                                                       tmp_path):
        """The check comes first, so a subtitle it throws out is fetched again
        in the same run."""
        films = _library(tmp_path, "Films")
        docs = _library(tmp_path, "Documentaries")
        assert _main(stubbed, "-sw", str(films), str(docs)) == 0
        assert stubbed["calls"] == [
            ("check", str(films), "confidence", True),
            ("download", str(films), "confidence"),
            ("check", str(docs), "confidence", True),
            ("download", str(docs), "confidence")]

    def test_pipx_is_asked_for_only_when_there_is_a_download(self, stubbed,
                                                             tmp_path):
        films = _library(tmp_path, "Films")
        _main(stubbed, "-s", str(films))
        _main(stubbed, "-sw", str(films))
        dry, real = stubbed["tools"]
        assert "ffsubsync" in dry and "pipx" not in dry
        assert "ffsubsync" in real and "pipx" in real

    def test_without_credentials_w_still_checks_and_says_so_once(
            self, stubbed, tmp_path, monkeypatch):
        monkeypatch.delenv("openSubtitlesPassword")
        films = _library(tmp_path, "Films")
        docs = _library(tmp_path, "Documentaries")
        assert _main(stubbed, "-sw", str(films), str(docs)) == 0
        assert [call[0] for call in stubbed["calls"]] == ["check", "check"]
        assert len([line for line in stubbed["logs"]
                    if "openSubtitlesUser" in line]) == 1

    def test_an_ffsubsync_that_cannot_refuse_has_nothing_to_test_with(
            self, stubbed, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr(run, "_settle_ffsubsync_quality", lambda: "no")
        films = _library(tmp_path, "Films")
        assert _main(stubbed, "-sw", str(films)) == 1
        assert stubbed["calls"] == []
        assert "Nothing was changed." in capsys.readouterr().err

    def test_missing_tools_refuse_the_run(self, stubbed, tmp_path,
                                          monkeypatch):
        monkeypatch.setattr(run.tooldeps, "require_tools", lambda *_a: 1)
        films = _library(tmp_path, "Films")
        assert _main(stubbed, "-s", str(films)) == 1
        assert stubbed["calls"] == []


class TestTheList:
    def _listing(self, stubbed):
        from pathlib import Path
        return Path(stubbed["script"]) / "logs" / \
            "ingest-movies-subtitles-Films.txt"

    def test_the_ones_out_of_step_and_untested_are_listed(self, stubbed,
                                                         tmp_path):
        films = _library(tmp_path, "Films")
        stubbed["verdicts"]["kept"] = ["/Films/a.en.srt"]
        stubbed["verdicts"]["discarded"] = ["/Films/b.de.srt"]
        stubbed["verdicts"]["untested"] = ["/Films/c.fr.srt"]
        _main(stubbed, "-s", str(films))
        assert self._listing(stubbed).read_text() == (
            "# Out of step with their film - would be thrown out:\n"
            "/Films/b.de.srt\n"
            "# Could not be tested - left alone:\n"
            "/Films/c.fr.srt\n")

    def test_under_w_it_says_they_were_thrown_out(self, stubbed, tmp_path):
        films = _library(tmp_path, "Films")
        stubbed["verdicts"]["discarded"] = ["/Films/b.de.srt"]
        _main(stubbed, "-sw", str(films))
        assert self._listing(stubbed).read_text() == (
            "# Out of step with their film - thrown out:\n"
            "/Films/b.de.srt\n")

    def test_nothing_to_list_removes_an_earlier_run_s_list(self, stubbed,
                                                           tmp_path):
        films = _library(tmp_path, "Films")
        listing = self._listing(stubbed)
        listing.parent.mkdir(parents=True)
        listing.write_text("stale\n")
        stubbed["verdicts"]["kept"] = ["/Films/a.en.srt"]
        _main(stubbed, "-s", str(films))
        assert not listing.exists()


class TestTheCombosItRefuses:
    @pytest.mark.parametrize("other", [["-t"], ["-a"], ["-i", "ids.tsv"]])
    def test_s_with_another_phase_is_refused(self, tmp_path, capsys, other):
        films = _library(tmp_path, "Films")
        assert run.main(["-s", *other, str(films)]) == 1
        errors = capsys.readouterr().err
        assert "two runs, not one" in errors
        assert "Nothing was changed." in errors

    def test_w_on_its_own_points_at_sw_too(self, tmp_path, capsys):
        films = _library(tmp_path, "Films")
        assert run.main(["-w", str(films)]) == 1
        assert "-sw" in capsys.readouterr().err
