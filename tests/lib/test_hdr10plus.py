"""The white box for medialib/lib/hdr10plus.py.

The detection, the extraction pipeline, the two JSON readers, and the
inject-and-remux chain: its argv, the order it runs in, what it leaves behind,
and the one warning of hdr10plus_tool's that has to be read as a failure.
"""

import json
import os

import pytest

from medialib.lib import hdr10plus

pytestmark = pytest.mark.stubbed


class _Proc:
    def __init__(self, returncode=0, stdout=b"", stderr=b""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class _Pipe:
    """The pipeline runner stand-in: the two statuses and stderrs the real one
    hands back, the argv of the call recorded, and - standing in for what
    hdr10plus_tool writes - <json> put at the -o path."""

    def __init__(self, first=0, second=0, json_text=None, first_err=b"",
                 second_err=b""):
        self.result = (first, second, first_err, second_err)
        self.json_text = json_text
        self.calls = []

    def __call__(self, first_argv, second_argv, **kwargs):
        self.calls.append((list(first_argv), list(second_argv), kwargs))
        if self.json_text is not None:
            out = second_argv[second_argv.index("-o") + 1]
            with open(out, "w") as handle:
                handle.write(self.json_text)
        return self.result


class _Chain:
    """The one-tool runner stand-in for the inject chain: each tool writes its
    output file (the last argv entry for ffmpeg, -o for the other two) unless
    told not to, and answers with its canned status and output."""

    def __init__(self, **answers):
        self.answers = answers
        self.calls = []

    def __call__(self, argv, **kwargs):
        name = argv[0]
        self.calls.append(list(argv))
        returncode, said, writes = self.answers.get(name, (0, b"", True))
        if writes:
            out = argv[-1] if name == "ffmpeg" else argv[argv.index("-o") + 1]
            with open(out, "wb") as handle:
                handle.write(b"stream")
        return _Proc(returncode, said, b"")


def _metadata(tmp_path, frames=3, windows=1):
    path = tmp_path / "hdr10plus.json"
    path.write_text(json.dumps({"SceneInfo": [
        {"NumberOfWindows": windows, "SceneFrameIndex": index}
        for index in range(frames)]}))
    return str(path)


class TestStreamHasHdr10plus:
    def test_the_side_data_of_a_frame_is_what_is_asked(self):
        calls = []

        def run(argv, **kwargs):
            calls.append(argv)
            return _Proc(stdout=b'{"frames": [{"side_data_list": [{'
                                b'"side_data_type": "HDR Dynamic Metadata '
                                b'SMPTE2094-40 (HDR10+)"}]}]}')
        assert hdr10plus.stream_has_hdr10plus("film.mkv", run=run)
        assert "-show_frames" in calls[0] and calls[0][-1] == "film.mkv"

    def test_static_metadata_alone_is_not_hdr10plus(self):
        def run(argv, **kwargs):
            return _Proc(stdout=b'{"frames": [{"side_data_list": [{'
                                b'"side_data_type": "Mastering display '
                                b'metadata"}]}]}')
        assert not hdr10plus.stream_has_hdr10plus("film.mkv", run=run)

    def test_a_probe_that_cannot_start_reads_as_none(self):
        def run(argv, **kwargs):
            raise OSError("no ffprobe")
        assert not hdr10plus.stream_has_hdr10plus("film.mkv", run=run)


class TestExtractMetadata:
    def test_the_video_is_piped_into_the_tool(self, tmp_path):
        out = str(tmp_path / "meta.json")
        pipe = _Pipe(json_text=json.dumps({"SceneInfo": [{}]}))
        logs = []
        assert hdr10plus.extract_metadata("film.mkv", out, log=logs.append,
                                          pipe=pipe) == 0
        ffmpeg_argv, tool_argv, _kw = pipe.calls[0]
        assert ffmpeg_argv[-1] == "-"
        assert "hevc_mp4toannexb" in ffmpeg_argv
        assert tool_argv == ["hdr10plus_tool", "extract", "-o", out, "-"]
        assert logs == []

    @pytest.mark.parametrize("first,second,json_text", [
        (0, 1, json.dumps({"SceneInfo": [{}]})),   # the tool fails
        (1, 0, json.dumps({"SceneInfo": [{}]})),   # ffmpeg fails
        (0, 0, json.dumps({"SceneInfo": []})),     # no frames described
        (0, 0, "not json"),
        (0, 0, None),                              # nothing written
    ])
    def test_failure_leaves_nothing_and_says_why(self, first, second,
                                                 json_text, tmp_path):
        out = str(tmp_path / "meta.json")
        logs = []
        status = hdr10plus.extract_metadata(
            "film.mkv", out, log=logs.append,
            pipe=_Pipe(first, second, json_text, second_err=b"no HDR10+\n"))
        assert status == 1
        assert not os.path.exists(out)
        assert logs == ["    reason: no HDR10+"]


class TestTheJsonReaders:
    def test_frames_are_the_scene_entries(self, tmp_path):
        assert hdr10plus.metadata_frames(_metadata(tmp_path, frames=5)) == 5

    def test_windows_are_the_most_any_frame_uses(self, tmp_path):
        path = tmp_path / "meta.json"
        path.write_text(json.dumps({"SceneInfo": [
            {"NumberOfWindows": 1}, {"NumberOfWindows": 3}, {}]}))
        assert hdr10plus.metadata_windows(str(path)) == 3

    @pytest.mark.parametrize("text", ["", "[]", '{"SceneInfo": 4}', "{"])
    def test_a_json_they_cannot_read_describes_nothing(self, text, tmp_path):
        path = tmp_path / "meta.json"
        path.write_text(text)
        assert hdr10plus.metadata_frames(str(path)) == 0
        assert hdr10plus.metadata_windows(str(path)) == 0


class TestInjectMetadata:
    def _inject(self, tmp_path, **answers):
        scratch = tmp_path / "scratch"
        scratch.mkdir()
        out = str(scratch / "video.mkv")
        run = _Chain(**answers)
        logs = []
        status = hdr10plus.inject_metadata(
            "video.mkv", _metadata(tmp_path), out, "24000/1001", str(scratch),
            log=logs.append, run=run)
        return status, logs, out, run, scratch

    def test_the_chain_runs_in_order_and_leaves_only_the_matroska(self,
                                                                   tmp_path):
        status, logs, out, run, scratch = self._inject(tmp_path)
        assert status == 0 and logs == []
        assert [call[0] for call in run.calls] == [
            "ffmpeg", "hdr10plus_tool", "mkvmerge"]
        raw = str(scratch / "hdr10plus.in.hevc")
        injected = str(scratch / "hdr10plus.out.hevc")
        assert run.calls[0][-1] == raw
        # By name: the tool refuses to read its input from a pipe.
        assert run.calls[1] == ["hdr10plus_tool", "inject", "-i", raw, "-j",
                                str(tmp_path / "hdr10plus.json"), "-o",
                                injected]
        assert run.calls[2] == ["mkvmerge", "--quiet", "-o", out,
                                "--default-duration", "0:24000/1001fps",
                                injected]
        assert sorted(os.listdir(scratch)) == ["video.mkv"]

    def test_a_length_mismatch_is_refused_though_the_tool_exits_0(self,
                                                                   tmp_path):
        said = (b"Parsing JSON file...\n\nWarning: mismatched lengths. video "
                b"12, HDR10+ JSON 24\nMetadata will be skipped at the end to "
                b"match video length\n")
        status, logs, out, run, scratch = self._inject(
            tmp_path, hdr10plus_tool=(0, said, True))
        assert status == 1
        assert logs == ["    reason: the encode has 12 frames and the "
                        "source's HDR10+ metadata describes 24"]
        assert [call[0] for call in run.calls] == ["ffmpeg", "hdr10plus_tool"]
        assert os.listdir(scratch) == []

    @pytest.mark.parametrize("answers,ran", [
        ({"ffmpeg": (1, b"", False)}, ["ffmpeg"]),
        ({"hdr10plus_tool": (1, b"Error: bad JSON\n", False)},
         ["ffmpeg", "hdr10plus_tool"]),
        ({"mkvmerge": (2, b"Error: no space\n", False)},
         ["ffmpeg", "hdr10plus_tool", "mkvmerge"]),
    ])
    def test_a_failing_step_stops_the_chain_and_leaves_nothing(
            self, answers, ran, tmp_path):
        status, logs, _out, run, scratch = self._inject(tmp_path, **answers)
        assert status == 1
        assert [call[0] for call in run.calls] == ran
        assert os.listdir(scratch) == []
        assert len(logs) == 1 and logs[0].startswith("    reason: ")

    def test_a_mkvmerge_warning_still_counts_as_written(self, tmp_path):
        status, _logs, out, _run, _scratch = self._inject(
            tmp_path, mkvmerge=(1, b"Warning: something\n", True))
        assert status == 0 and os.path.exists(out)
