"""A transcript inside the audio: where a sidecar is found, and the LRC text
an Opus file is given."""

from __future__ import annotations

import pytest

from medialib.lib import aichapters, transcripttags

pytestmark = pytest.mark.fs


class TestSidecars:
    def test_the_plain_name_first(self, tmp_path):
        for name in ("show.en.srt", "show.srt", "show.lrc"):
            (tmp_path / name).write_text("x")
        assert transcripttags.find_sidecar(str(tmp_path / "show")).endswith("show.srt")

    @pytest.mark.parametrize("name", ["show.en.srt", "show.pt-BR.vtt", "show.deu.lrc"])
    def test_then_one_with_a_language_code(self, tmp_path, name):
        (tmp_path / name).write_text("x")
        assert transcripttags.find_sidecar(str(tmp_path / "show")) == str(tmp_path / name)

    @pytest.mark.parametrize("name", ["show.commentary.srt", "show two.srt",
                                      "showcase.srt", "show.txt"])
    def test_and_nothing_else(self, tmp_path, name):
        (tmp_path / name).write_text("x")
        assert transcripttags.find_sidecar(str(tmp_path / "show")) == ""

    def test_an_empty_one_is_no_transcript(self, tmp_path):
        (tmp_path / "show.srt").write_text("")
        assert transcripttags.find_sidecar(str(tmp_path / "show")) == ""


def test_lrc_counts_minutes_past_the_hour():
    cues = [aichapters.Cue(1234, 2000, "One  two"),
            aichapters.Cue(2 * 3600000 + 5000, 0, "Late")]
    assert transcripttags.lrc_text(cues) == "[00:01.23]One two\n[120:05.00]Late\n"


def test_a_container_that_takes_no_transcript_is_left_alone(tmp_path):
    (tmp_path / "a.srt").write_text("00:00:01,000 --> 00:00:02,000\nHi\n")
    (tmp_path / "a.mp3").write_text("")
    assert transcripttags.embed_transcript(str(tmp_path / "a.mp3"),
                                           str(tmp_path / "a.srt"), str(tmp_path)) == 1
    assert transcripttags.has_transcript(str(tmp_path / "a.mp3"))


@pytest.mark.parametrize("name, stems", [
    ("show.srt", ("show",)),
    ("show.en.srt", ("show.en", "show")),
    ("show.pt-BR.VTT", ("show.pt-BR", "show")),
    ("show.lrc", ("show",)),
    ("show.opus", ()),
    (".srt", ()),
])
def test_the_stems_a_name_would_be_the_sidecar_of(name, stems):
    assert transcripttags.sidecar_stems(name) == stems
