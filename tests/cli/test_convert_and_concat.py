"""`convert-and-concat`'s own decisions, as function calls.

The phases themselves are child processes and belong in
`test_convert_and_concat_cli.py`; what is asked here is the argv this command
BUILDS for them, which is the whole of its contract with `convert-audio` and is
not worth starting an encoder to read back.
"""

from __future__ import annotations

import os

import pytest

from medialib.cli import convert_and_concat as cac
from medialib.lib import enums

pytestmark = pytest.mark.pure


def _folds_case(directory) -> bool:
    """Whether the filesystem folds case: probe.TMP and probe.tmp then name one
    file, and two names differing only in case cannot both exist."""
    probe = directory / "case-probe.TMP"
    probe.touch()
    try:
        return (directory / "case-probe.tmp").exists()
    finally:
        probe.unlink()


class _Phases:
    """The child commands a run would have started, as (command, argv)."""

    def __init__(self) -> None:
        self.started: list = []

    def __call__(self, command, argv, script_dir="") -> None:
        self.started.append((command, list(argv)))

    @property
    def transcode(self) -> list:
        for command, argv in self.started:
            if command == "convert-audio":
                return argv
        raise AssertionError("no transcoding phase ran: %s" % self.started)


@pytest.fixture
def phases(monkeypatch, tmp_path):
    recorder = _Phases()
    monkeypatch.setattr(cac, "_run_phase", recorder)
    # The phase hands a tree over only when it holds something to transcode,
    # and copies cue sheets afterwards - neither is what this file is about.
    monkeypatch.setattr(cac, "holds_ingestible_audio", lambda *a, **k: True)
    monkeypatch.setattr(cac, "_copy_cue_files", lambda *a, **k: None)
    return recorder


def _transcode(phases, tmp_path, **overrides):
    settings = dict(in_path=str(tmp_path / "in"),
                    temp_path=str(tmp_path / "temp"), stage=None,
                    only_concat=False, mono=False, bitrate="",
                    codec="opus", sub_folders=False, script_dir="")
    settings.update(overrides)
    cac._transcode_phase(settings["in_path"], settings["temp_path"],
                         settings["stage"], settings["only_concat"],
                         settings["mono"], settings["bitrate"],
                         settings["codec"], settings["sub_folders"],
                         settings["script_dir"])
    return phases.transcode


class TestTheCodecReachesTheTranscoder:
    """-o is this command's only say in what the intermediate tree holds, and
    it says it by handing the token to `convert-audio` rather than by acting on
    it here: that command owns the encoders, the containers and the refusals."""

    def test_the_chosen_codec_is_handed_over(self, phases, tmp_path):
        argv = _transcode(phases, tmp_path, codec="xheaac")
        assert argv[argv.index("-e") + 1] == "xheaac"

    def test_the_default_is_handed_over_just_as_explicitly(self, phases,
                                                           tmp_path):
        """Named on the command line rather than left to the child's own
        default: a run that SAYS which codec it is using in its summary must
        not have the actual choice settled somewhere else."""
        argv = _transcode(phases, tmp_path, codec="opus")
        assert argv[argv.index("-e") + 1] == "opus"

    def test_it_rides_alongside_the_other_pass_through_options(self, phases,
                                                              tmp_path):
        argv = _transcode(phases, tmp_path, codec="xheaac", mono=True,
                          bitrate="64")
        assert argv[:6] == ["-m", "-c", "-b", "64", "-e", "xheaac"]

    def test_only_concat_starts_no_transcoder_at_all(self, phases, tmp_path):
        cac._transcode_phase(str(tmp_path / "in"), str(tmp_path / "temp"),
                             None, True, False, "", "xheaac", False, "")
        assert phases.started == []


class TestEachBookIsJudgedWhole:
    """A book converted file by file comes out part converted and part copied
    as it was whenever its bitrates straddle the threshold, and the two halves
    can then never be joined - so the transcoder judges each book whole, at the
    depth this run's books lie at."""

    def test_a_book_is_a_subfolder_of_the_input(self, phases, tmp_path):
        argv = _transcode(phases, tmp_path)
        assert argv[argv.index("-g") + 1] == "1"

    def test_with_s_a_book_is_one_level_further_in(self, phases, tmp_path):
        argv = _transcode(phases, tmp_path, sub_folders=True)
        assert argv[argv.index("-g") + 1] == "2"


class TestTheOptionPage:
    def test_the_codecs_offered_are_the_ones_convert_audio_writes(self):
        """One list, so the two pages cannot come to disagree about what -e
        takes."""
        page = cac.OPT_SPEC
        for codec in enums.AUDIO_CODECS:
            assert codec in page, page

    def test_the_default_is_the_same_one_convert_audio_falls_back_to(self):
        from medialib.cli import convert_audio
        assert cac.DEFAULT_CODEC == convert_audio.DEFAULT_CODEC

    def test_the_token_is_spelled_without_a_hyphen(self):
        """The page names what -e actually takes: `xhe-aac` is the codec's
        name, and a reader who types it gets a refusal."""
        assert "xhe-aac" not in cac.OPT_SPEC


class TestTheCueSheetsATranscodeHandsOver:
    """The cue sheets a tree holds, copied into the destination at the path they
    sit at in the source - and nothing but the cue sheets and the folders on the
    way to them: the audio they describe is what the transcode itself writes."""

    pytestmark = pytest.mark.fs

    def test_cue_sheets_land_at_the_path_they_have_in_the_source(self, tmp_path):
        source = tmp_path / "in"
        (source / "Disc 1").mkdir(parents=True)
        (source / "top.cue").write_text("cue two")
        (source / "Disc 1" / "track.cue").write_text("cue one")
        destination = tmp_path / "out"

        cac._copy_cue_files(str(source), str(destination))

        assert (destination / "top.cue").read_text() == "cue two"
        assert (destination / "Disc 1" / "track.cue").read_text() == "cue one"

    def test_and_nothing_but_the_cue_sheets_is_copied(self, tmp_path):
        source = tmp_path / "in"
        (source / "Disc 1").mkdir(parents=True)
        (source / "Disc 1" / "track.cue").write_text("cue")
        (source / "Disc 1" / "track.mp3").write_text("audio")
        (source / "notes.txt").write_text("not a cue")
        destination = tmp_path / "out"

        cac._copy_cue_files(str(source), str(destination))

        copied = sorted(os.path.relpath(path, str(destination))
                        for path in destination.rglob("*") if path.is_file())
        assert copied == [os.path.join("Disc 1", "track.cue")]

    def test_a_cue_is_found_in_either_case(self, tmp_path):
        if _folds_case(tmp_path):
            pytest.skip("needs a case-sensitive filesystem: sheet.CUE and "
                        "sheet.cue would be one file")
        source = tmp_path / "in"
        source.mkdir()
        (source / "sheet.CUE").write_text("one")
        (source / "sheet.cue").write_text("two")
        destination = tmp_path / "out"

        cac._copy_cue_files(str(source), str(destination))

        assert sorted(p.name for p in destination.iterdir()) == [
            "sheet.CUE", "sheet.cue"]
