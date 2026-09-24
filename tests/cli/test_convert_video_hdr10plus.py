"""Which files keep their HDR10+, and what every other one says instead.

The decision is convert-video's; the extraction and injection are
`medialib/lib/hdr10plus.py`'s, stubbed here, so what is pinned is the ORDER
the reasons are asked in, the message each one gives, and that no reason fails
the file: every one of them ends in the plain HDR10 the source carries
alongside.
"""

import json
import os

import pytest

from medialib.cli import convert_video as rules
from medialib.cli import convert_video_run as run_module
from medialib.lib import hdr10plus

pytestmark = pytest.mark.fs


@pytest.fixture
def logged(monkeypatch):
    lines = []
    monkeypatch.setattr(run_module, "log", lines.append)
    return lines


@pytest.fixture
def source(monkeypatch, tmp_path):
    """An HEVC source carrying HDR10+, on a machine with both tools, whose
    metadata extracts to three single-window frames - each of which a test
    takes away in turn."""
    state = {"hdr10plus": True, "codec": "hevc", "tools": {"hdr10plus_tool",
                                                          "mkvmerge"},
             "fps": "24000/1001", "windows": 1, "extracted": 0}
    monkeypatch.setattr(hdr10plus, "stream_has_hdr10plus",
                        lambda path, **kw: state["hdr10plus"])
    monkeypatch.setattr(rules, "_probe", lambda argv: state["codec"] + "\n")
    monkeypatch.setattr(run_module, "_has_tool",
                        lambda name: name in state["tools"])
    monkeypatch.setattr(rules, "video_frame_rate", lambda path: state["fps"])

    def extract(movie, out, log=print, **kw):
        if state["extracted"]:
            return state["extracted"]
        with open(out, "w") as handle:
            json.dump({"SceneInfo": [{"NumberOfWindows": state["windows"]}]
                       * 3}, handle)
        return 0

    monkeypatch.setattr(hdr10plus, "extract_metadata", extract)
    state["directory"] = str(tmp_path)
    return state


def _prepare(state, encoder="libx265", reframed=False):
    settings = rules.Settings(encoder=encoder)
    return run_module.prepare_hdr10plus("film.mkv", "in/film.mkv",
                                        state["directory"], settings, reframed)


class TestWhichFilesKeepIt:
    def test_an_hevc_encode_of_an_hdr10plus_source_keeps_it(self, source,
                                                            logged):
        metadata = _prepare(source)
        assert metadata == os.path.join(source["directory"], "hdr10plus.json")
        assert logged == ["HDR10+: reading the source's dynamic metadata, to "
                          "write it back into the encode: film.mkv"]

    def test_nvenc_keeps_it_too(self, source, logged):
        """Nothing about it is asked of the encoder: it is written back into
        the stream the encoder produced."""
        assert _prepare(source, encoder="hevc_nvenc")

    def test_a_source_without_it_is_not_mentioned(self, source, logged):
        source["hdr10plus"] = False
        assert _prepare(source) == ""
        assert logged == []

    @pytest.mark.parametrize("encoder", ["libsvtav1", "av1_nvenc"])
    def test_an_av1_encode_says_it_cannot_be_written_back_yet(self, encoder,
                                                              source, logged):
        assert _prepare(source, encoder=encoder) == ""
        assert logged == ["HDR10+: the source carries it, but %s writes AV1, "
                          "which it cannot be written back into yet - keeping "
                          "HDR10 only: film.mkv" % encoder]

    def test_a_source_that_is_not_hevc_says_so(self, source, logged):
        source["codec"] = "av1"
        assert _prepare(source) == ""
        assert logged == ["HDR10+: the source carries it in AV1, and it can "
                          "only be read out of HEVC - keeping HDR10 only: "
                          "film.mkv"]


class TestAMissingToolIsAWarningNotAFailure:
    @pytest.mark.parametrize("present,named", [
        ({"mkvmerge"}, "hdr10plus_tool"),
        ({"hdr10plus_tool"}, "mkvmerge (mkvtoolnix)"),
        (set(), "hdr10plus_tool and mkvmerge (mkvtoolnix)"),
    ])
    def test_the_warning_names_what_to_install(self, present, named, source,
                                               logged):
        source["tools"] = present
        assert _prepare(source) == ""
        assert logged == ["WARNING: HDR10+: the source carries it and libx265 "
                          "could keep it, but that needs %s - install it to "
                          "keep HDR10+. Keeping HDR10 only: film.mkv" % named]

    def test_no_warning_where_the_tools_would_not_help(self, source, logged):
        """An AV1 encode could not use them, so their absence is not worth a
        warning there."""
        source["tools"] = set()
        _prepare(source, encoder="libsvtav1")
        assert not any(line.startswith("WARNING") for line in logged)


class TestTheOtherReasons:
    def test_no_frame_rate(self, source, logged):
        source["fps"] = ""
        assert _prepare(source) == ""
        assert "no usable frame rate" in logged[-1]

    def test_a_failed_extraction(self, source, logged):
        source["extracted"] = 1
        assert _prepare(source) == ""
        assert logged[-1] == ("HDR10+: the metadata could not be read out of "
                              "the source, keeping HDR10 only: film.mkv")

    def test_a_single_window_survives_a_crop_or_scale(self, source, logged):
        assert _prepare(source, reframed=True)

    def test_placed_windows_do_not(self, source, logged):
        source["windows"] = 2
        assert _prepare(source, reframed=True) == ""
        assert not os.path.exists(os.path.join(source["directory"],
                                               "hdr10plus.json"))
        assert "processing windows" in logged[-1]

    def test_placed_windows_are_fine_in_the_frame_they_were_placed_in(
            self, source, logged):
        source["windows"] = 2
        assert _prepare(source, reframed=False)


class TestWritingItBack:
    @pytest.fixture
    def encode(self, monkeypatch, tmp_path):
        """A finished video-only intermediate, and a scratch to rewrite it in;
        ``state`` is what the stubbed injection and the two checks answer."""
        directory = tmp_path / "chunks"
        directory.mkdir()
        (directory / "video.mkv").write_bytes(b"plain")
        scratch = tmp_path / "scratch"
        scratch.mkdir()
        state = {"injected": 0, "duration": {"plain": 600.0,
                                             "injected": 600.0},
                 "signals": True}
        monkeypatch.setattr(run_module.ramscratch, "ram_scratch_dir_for",
                            lambda *a: (str(scratch), 0, 0))
        monkeypatch.setattr(run_module.ramscratch, "add_exit_cleanup",
                            lambda targets: None)
        monkeypatch.setattr(run_module.ramscratch, "release_exit_cleanup",
                            lambda targets: None)
        monkeypatch.setattr(run_module.statusline, "clear_status",
                            lambda: None)
        monkeypatch.setattr(rules, "video_frame_rate", lambda path: "24/1")

        def inject(video, metadata, out, fps, scratch_dir, log=print):
            if state["injected"]:
                return state["injected"]
            with open(out, "wb") as handle:
                handle.write(b"injected")
            return 0

        def duration(path):
            with open(path, "rb") as handle:
                return state["duration"][handle.read().decode()]

        monkeypatch.setattr(hdr10plus, "inject_metadata", inject)
        monkeypatch.setattr(run_module, "_media_duration", duration)
        monkeypatch.setattr(hdr10plus, "stream_has_hdr10plus",
                            lambda path, **kw: state["signals"])
        state["directory"] = str(directory)
        return state

    def _restore(self, state):
        settings = rules.Settings(input_dir="in", output_dir="out")
        kept = run_module.restore_hdr10plus(
            "film.mkv", state["directory"], "hdr10plus.json", settings)
        with open(os.path.join(state["directory"], "video.mkv"), "rb") as h:
            return kept, h.read()

    def test_the_rewritten_video_replaces_the_encode(self, encode, logged):
        assert self._restore(encode) == (True, b"injected")
        assert sorted(os.listdir(encode["directory"])) == ["video.mkv"]

    @pytest.mark.parametrize("change,reason", [
        ({"injected": 1}, "it could not be written into the stream"),
        ({"duration": {"plain": 600.0, "injected": 300.0}},
         "the rewritten video came out shorter than the encode"),
        ({"signals": False}, "the rewritten video does not signal it"),
    ])
    def test_every_failure_keeps_the_plain_encode(self, change, reason, encode,
                                                  logged):
        encode.update(change)
        assert self._restore(encode) == (False, b"plain")
        assert logged[-1] == ("HDR10+: %s, keeping HDR10 only: film.mkv"
                              % reason)
