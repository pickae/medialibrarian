"""convert-video -n: which track the chapters are worked out from, and how
the chapters and a transcript reach the output in the one mux."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from medialib.cli import convert_video as rules
from medialib.cli import convert_video_chapters as chapterwork
from medialib.cli import convert_video_encode as encode
from medialib.cli import convert_video_run as run_module
from medialib.lib import aichapters, chapterllm, toolcapture


def _stream(kind, codec="", title="", language="", **flags):
    return {"codec_type": kind, "codec_name": codec,
            "tags": {"title": title, "language": language},
            "disposition": {flag: int(on) for flag, on in flags.items()}}


@pytest.mark.pure
class TestCandidates:

    def test_a_dialogue_text_subtitle_comes_before_any_soundtrack(self):
        found = chapterwork.candidates([
            _stream("video", "hevc"),
            _stream("audio", "truehd", "English", "eng"),
            _stream("subtitle", "subrip", "English", "eng"),
        ])
        assert [(c.kind, c.position) for c in found] == [("subtitle", 0),
                                                         ("audio", 0)]

    def test_commentary_sdh_forced_and_picture_subtitles_are_passed_over(self):
        found = chapterwork.candidates([
            _stream("subtitle", "hdmv_pgs_subtitle", "English", "eng"),
            _stream("subtitle", "subrip", "Commentary", "eng"),
            _stream("subtitle", "subrip", "English", "eng", hearing_impaired=1),
            _stream("subtitle", "subrip", "English Forced", "eng"),
            _stream("subtitle", "ass", "English", "eng", comment=1),
            _stream("subtitle", "subrip", "English", "eng"),
        ])
        assert [(c.kind, c.position) for c in found] == [("subtitle", 5)]

    def test_only_soundtracks_and_in_the_files_order(self):
        found = chapterwork.candidates([
            _stream("audio", "ac3", "Director's Commentary", "eng"),
            _stream("audio", "ac3", "English", "eng", visual_impaired=1),
            _stream("audio", "flac", "Isolated Score", "zxx"),
            _stream("audio", "dts", "Deutsch", "ger"),
            _stream("audio", "truehd", "English", "eng"),
        ])
        assert [(c.position, c.language) for c in found] == [(3, "ger"),
                                                             (4, "eng")]

    def test_nothing_to_read_or_hear(self):
        assert chapterwork.candidates([_stream("video", "hevc")]) == []


@pytest.mark.pure
class TestEnoughDialogue:

    def _cues(self, count):
        return [aichapters.Cue(i * 1000, i * 1000 + 500, "line")
                for i in range(count)]

    def test_a_films_dialogue_is(self):
        assert chapterwork.enough_dialogue(self._cues(600), 60 * 60000)

    def test_a_few_signs_are_not(self):
        assert not chapterwork.enough_dialogue(self._cues(40), 60 * 60000)

    def test_nothing_is_not(self):
        assert not chapterwork.enough_dialogue([], 60 * 60000)

    def test_an_unknown_length_asks_only_for_something(self):
        assert chapterwork.enough_dialogue(self._cues(1), 0)


@pytest.fixture
def mux(monkeypatch, tmp_path):
    monkeypatch.setattr(encode, "log", lambda *a, **k: None)
    (tmp_path / "in").mkdir()
    (tmp_path / "in" / "film.mkv").write_text("source")
    (tmp_path / "out").mkdir()
    directory = tmp_path / "chunks"
    directory.mkdir()
    (directory / "video.mkv").write_text("video")
    (directory / "audio0.mka").write_text("audio")
    output = str(tmp_path / "out" / "film.mkv")
    asked: list = []

    def fake_run(argv, *_a, **_k):
        asked.append(argv)
        Path(output).write_text("muxed")
        return toolcapture.ToolRun(argv, 0, toolcapture.ToolOutput())

    monkeypatch.setattr(toolcapture, "run", fake_run)
    outcome = chapterwork.Outcome(
        str(directory / "chapters.ffmetadata"), str(directory / "chapters.txt"),
        str(directory / "transcript.srt"), "eng", 2)
    return tmp_path, str(directory), output, asked, outcome


@pytest.mark.fs
class TestTheMux:

    def _settings(self, tmp_path, **values):
        return rules.Settings(input_dir=str(tmp_path / "in"),
                              output_dir=str(tmp_path / "out"), script_dir="",
                              **values)

    def test_without_n_the_chapters_are_the_sources(self, mux):
        tmp_path, directory, output, asked, _outcome = mux
        assert encode.mux_final("film.mkv", directory,
                                self._settings(tmp_path), output) == 0
        argv = asked[0]
        assert argv[argv.index("-map_chapters") + 1] == "1"

    def test_ffmpeg_takes_the_chapters_and_adds_the_transcript(self, mux):
        tmp_path, directory, output, asked, outcome = mux
        assert encode.mux_final("film.mkv", directory,
                                self._settings(tmp_path), output,
                                outcome) == 0
        argv = asked[0]
        inputs = [argv[i + 1] for i, word in enumerate(argv) if word == "-i"]
        chapters_input = inputs.index(outcome.ffmetadata)
        transcript_input = inputs.index(outcome.transcript)
        assert argv[argv.index("-map_chapters") + 1] == str(chapters_input)
        maps = [argv[i + 1] for i, word in enumerate(argv) if word == "-map"]
        # After the source's own subtitles, before its attachments.
        assert maps.index("%d:s" % transcript_input) == maps.index("1:s?") + 1
        assert maps[-1] == "1:t?"
        assert "-metadata:s:s:2" in argv
        assert "title=" + chapterwork.TRANSCRIPT_TITLE in argv
        assert "language=eng" in argv

    def test_chapters_from_a_subtitle_add_no_track(self, mux):
        tmp_path, directory, output, asked, outcome = mux
        outcome = outcome._replace(transcript="", language="")
        encode.mux_final("film.mkv", directory, self._settings(tmp_path),
                         output, outcome)
        argv = asked[0]
        assert outcome.ffmetadata in argv
        assert not any(word.startswith("-metadata:s:s:") for word in argv)

    def test_mkvmerge_takes_the_chapters_and_adds_the_transcript(self, mux):
        tmp_path, directory, output, asked, outcome = mux
        settings = self._settings(tmp_path, keep_tracks=True,
                                  audio_profile="passthrough")
        assert encode.mux_final("film.mkv", directory, settings, output,
                                outcome) == 0
        argv = asked[0]
        assert argv[0] == "mkvmerge"
        source = argv.index(os.path.join(settings.input_dir, "film.mkv"))
        # The source's chapters are not merged with the new ones.
        assert "--no-chapters" in argv[argv.index(
            os.path.join(directory, "video.mkv")) + 1:source]
        assert argv[argv.index("--chapters") + 1] == outcome.rows
        assert argv[-1] == outcome.transcript
        assert "0:eng" in argv
        assert "0:" + chapterwork.TRANSCRIPT_TITLE in argv


@pytest.mark.fs
class TestTheInstructions:

    def test_p_without_n_is_refused_before_anything_is_written(self, tmp_path,
                                                               capsys):
        (tmp_path / "in").mkdir()
        (tmp_path / "in" / "film.mkv").touch()
        assert run_module.main(["-P", "A chapter per scene.",
                                str(tmp_path / "in"),
                                str(tmp_path / "out")]) == 1
        assert "-P (instructions for the chapter model) needs -n" in \
            capsys.readouterr().err
        assert not (tmp_path / "out").exists()

    def test_the_instructions_reach_the_settings(self, tmp_path, monkeypatch):
        (tmp_path / "in").mkdir()
        (tmp_path / "in" / "film.mkv").touch()
        seen = []
        monkeypatch.setattr(run_module, "_settle_and_run",
                            lambda settings, *_a: seen.append(settings) or 0)
        assert run_module.main(["-n", "--prompt", "  A chapter per scene. ",
                                str(tmp_path / "in"),
                                str(tmp_path / "out")]) == 0
        assert seen[0].ai_chapters
        assert seen[0].chapter_instructions == "A chapter per scene."

    def test_the_model_is_handed_them(self, monkeypatch):
        monkeypatch.setattr(run_module, "log", lambda *a, **k: None)
        monkeypatch.setattr(chapterllm, "require",
                            lambda program: chapterllm.Llm(
                                api="openai", url="http://localhost:1/v1",
                                model="m"))
        settings = rules.Settings(chapter_instructions="A chapter per scene.")
        llm = run_module.chapter_model("convert-video", settings)
        assert llm.instructions == "A chapter per scene."
        assert llm.effort == chapterllm.THOROUGH_EFFORT

    def test_without_them_the_model_is_as_set_up(self, monkeypatch):
        monkeypatch.setattr(run_module, "log", lambda *a, **k: None)
        plain = chapterllm.Llm(api="openai", url="http://localhost:1/v1",
                               model="m")
        monkeypatch.setattr(chapterllm, "require", lambda program: plain)
        assert run_module.chapter_model("convert-video",
                                        rules.Settings()) is plain


class TestChapterBeforeTranscribing:
    """allow_audio=False reads a subtitle but leaves a soundtrack for later."""

    def _source(self, monkeypatch, streams):
        monkeypatch.setattr(chapterwork.chapters, "chapter_count", lambda s: 0)
        monkeypatch.setattr(chapterwork.chapters, "duration_ms",
                            lambda s: 3600000)
        monkeypatch.setattr(chapterwork, "probe_streams", lambda s: streams)

    def test_a_soundtrack_is_put_off(self, monkeypatch, tmp_path):
        self._source(monkeypatch, [_stream("audio", "ac3", language="eng")])
        monkeypatch.setattr(chapterwork, "_transcribe",
                            lambda *a, **k: 1 / 0)
        assert chapterwork.chapter("f.mkv", str(tmp_path), None, "f",
                                   "", lambda line: None,
                                   allow_audio=False) is chapterwork.LATER

    def test_a_film_with_neither_is_not_put_off(self, monkeypatch, tmp_path):
        self._source(monkeypatch, [_stream("video", "h264")])
        assert chapterwork.chapter("f.mkv", str(tmp_path), None, "f", "",
                                   lambda line: None,
                                   allow_audio=False) is None
