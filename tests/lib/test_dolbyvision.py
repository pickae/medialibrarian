"""The white box for medialib/lib/dolbyvision.py.

The reader's jq navigation (the first video track, the field shapes it prints,
and the broken documents it reads as all empty), the frame-rate fractioning,
the three pure gates, and the two pipelines' status, output and log wording.
"""

import json
import os
import subprocess

import pytest

from medialib.lib import dolbyvision

pytestmark = pytest.mark.stubbed


class _Proc:
    def __init__(self, returncode=0, stdout=b"", stderr=b""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class _Run:
    """The command runner stand-in: per-tool canned results, consumed in call
    order, and the argv of every call recorded (with its kwargs)."""

    def __init__(self, results):
        self.results = results
        self.calls = []

    def __call__(self, argv, **kwargs):
        name = argv[0]
        self.calls.append((list(argv), kwargs))
        queue = self.results.get(name, [])
        if queue:
            returncode, out, err = queue.pop(0)
        else:
            returncode, out, err = 0, b"", b""
        return _Proc(returncode, out, err)


def _media_run(document, returncode=0, raw=None):
    """A runner whose mediainfo prints <document> (or <raw> bytes)."""
    if raw is None:
        raw = json.dumps(document, separators=(",", ":")).encode("utf-8")
    return _Run({"mediainfo": [(returncode, raw, b"")]})


def _video(**fields):
    track = {"@type": "Video"}
    track.update(fields)
    return {"media": {"track": [track]}}


_EMPTY = {"PROFILE": "", "SETTINGS": "", "HDR": "", "TRANSFER": "",
          "FPS_SPEC": "", "STREAM_SIZE": ""}


class TestReadVideoInfo:
    def test_happy_path(self):
        run = _media_run(_video(
            HDR_Format_Profile="dvhe.07.06", HDR_Format_Settings="BL+EL+RPU",
            HDR_Format="Dolby Vision / SMPTE ST 2086",
            transfer_characteristics="PQ", FrameRate=23.976,
            FrameRate_Num=24000, FrameRate_Den=1001, StreamSize=123456))
        assert dolbyvision.read_video_info("movie.mkv", run=run) == {
            "PROFILE": "dvhe.07.06", "SETTINGS": "BL+EL+RPU",
            "HDR": "Dolby Vision / SMPTE ST 2086", "TRANSFER": "PQ",
            "FPS_SPEC": "24000/1001fps", "STREAM_SIZE": "123456",
        }

    def test_argv_is_the_one_pass(self):
        run = _media_run(_video())
        dolbyvision.read_video_info("a b.mkv", run=run)
        assert run.calls == [
            (["mediainfo", "--Output=JSON", "a b.mkv"],
             {"stdout": subprocess.PIPE, "stderr": subprocess.DEVNULL}),
        ]

    def test_video_after_an_audio_track(self):
        doc = {"media": {"track": [
            {"@type": "Audio", "Format": "Opus"},
            {"@type": "Video", "HDR_Format_Profile": "dvhe.08.06"},
        ]}}
        info = dolbyvision.read_video_info("m", run=_media_run(doc))
        assert info["PROFILE"] == "dvhe.08.06"

    def test_first_video_wins(self):
        doc = {"media": {"track": [
            {"@type": "Video", "HDR_Format_Profile": "dvhe.07.06"},
            {"@type": "Video", "HDR_Format_Profile": "dvhe.08.06"},
        ]}}
        assert dolbyvision.read_video_info(
            "m", run=_media_run(doc))["PROFILE"] == "dvhe.07.06"

    @pytest.mark.parametrize("entry", ["junk", 42, True])
    def test_a_non_object_track_entry_errors_the_whole_probe(self, entry):
        # the jq dies on the bad entry, so the reader reads every field as
        # empty - even when an earlier entry already named the video
        for doc in ({"media": {"track": [
                    {"@type": "Video", "HDR_Format": "HDR10"}, entry]}},
                    {"media": {"track": [entry,
                                        {"@type": "Video",
                                         "HDR_Format": "HDR10"}]}}):
            assert dolbyvision.read_video_info("m", run=_media_run(doc)) == _EMPTY

    def test_a_null_track_entry_is_harmless(self):
        doc = {"media": {"track": [None,
                                   {"@type": "Video", "HDR_Format": "HDR10"}]}}
        assert dolbyvision.read_video_info(
            "m", run=_media_run(doc))["HDR"] == "HDR10"

    @pytest.mark.parametrize("media", ["flat", [1], 42, True])
    def test_a_non_object_media_reads_as_empty(self, media):
        assert dolbyvision.read_video_info(
            "m", run=_media_run({"media": media})) == _EMPTY

    @pytest.mark.parametrize("doc", [
        {},                                    # no media key at all
        {"media": None},
        {"media": {}},
        {"media": {"track": []}},
        {"media": {"track": "a string"}},      # []? swallows the index error
        {"media": {"track": 42}},
        {"media": {"track": None}},
        {"media": {"track": [
            {"@type": "Audio"}]}},             # no video at all
    ])
    def test_no_video_reads_as_empty(self, doc):
        assert dolbyvision.read_video_info("m", run=_media_run(doc)) == _EMPTY

    def test_a_dict_track_iterates_its_values(self):
        # jq's .[] on an object walks the values, so a dict track still
        # names a video
        doc = {"media": {"track": {
            "head": {"@type": "Video", "HDR_Format": "HDR10"},
            "tail": {"@type": "Audio"}}}}
        assert dolbyvision.read_video_info(
            "m", run=_media_run(doc))["HDR"] == "HDR10"

    @pytest.mark.parametrize("raw", [
        b"[1, 2]", b'"a file"', b"42", b"true",   # not an object at the top
        b"not json {",
    ])
    def test_a_top_level_the_probe_cannot_navigate_reads_as_empty(self, raw):
        assert dolbyvision.read_video_info(
            "m", run=_media_run(None, raw=raw)) == _EMPTY

    def test_a_null_top_level_reads_as_empty_without_erroring(self):
        # .media on null is null, not an error - same answer either way
        assert dolbyvision.read_video_info(
            "m", run=_media_run(None, raw=b"null")) == _EMPTY

    def test_mediainfo_that_fails_reads_as_empty(self):
        assert dolbyvision.read_video_info(
            "m", run=_media_run(None, returncode=7)) == _EMPTY

    def test_mediainfo_that_prints_nothing_reads_as_empty(self):
        assert dolbyvision.read_video_info(
            "m", run=_media_run(None, raw=b"")) == _EMPTY

    def test_blank_stdout_reads_as_empty(self):
        assert dolbyvision.read_video_info(
            "m", run=_media_run(None, raw=b"\n\n")) == _EMPTY

    def test_invalid_utf8_reads_as_empty(self):
        assert dolbyvision.read_video_info(
            "m", run=_media_run(None, raw=b"\xff\xfe{\"media\"")) == _EMPTY

    def test_trailing_newlines_are_stripped_before_parsing(self):
        doc = json.dumps(_video(HDR_Format="HDR10")).encode("utf-8")
        assert dolbyvision.read_video_info(
            "m", run=_media_run(None, raw=doc + b"\n\n"))["HDR"] == "HDR10"

    @pytest.mark.parametrize("value,expected", [
        (None, ""),        # key absent
        ("null", ""),      # explicit null
        ("false", ""),     # the // catches false
        ("true", "true"),
        ("0", "0"),        # zero is not empty
        ("24", "24"),
        ("23.976", "23.976"),
        ("12345678901234567890", "12345678901234567890"),
        ('"PQ"', "PQ"),
    ])
    def test_field_shapes(self, value, expected):
        track = {"@type": "Video"}
        if value is not None:
            track["transfer_characteristics"] = json.loads(value)
        doc = {"media": {"track": [track]}}
        assert dolbyvision.read_video_info(
            "m", run=_media_run(doc))["TRANSFER"] == expected

    @pytest.mark.parametrize("fps,num,den,expected", [
        ("23.976", "24000", "1001", "24000/1001fps"),  # num/den beat the decimal
        ("-", "24000", "1001", "24000/1001fps"),
        ("23.976", "-", "-", "24000/1001fps"),
        ("29.976", "-", "-", "30000/1001fps"),
        ("29.97", "-", "-", "30000/1001fps"),
        ("47.952", "-", "-", "48000/1001fps"),
        ("59.94", "-", "-", "60000/1001fps"),
        ("119.88", "-", "-", "120000/1001fps"),
        ("23.9765", "-", "-", "24000/1001fps"),       # a prefix match
        ("23.97", "-", "-", "23.97fps"),
        ("24", "-", "-", "24fps"),
        ("30.0", "-", "-", "30.0fps"),
        ('"007"', "-", "-", "007fps"),                # a string, all digits
        ('"24.5.1"', "-", "-", ""),
        ('"abc"', "-", "-", ""),
        ("-", "-", "-", ""),
        ("25", "25", "0", "25fps"),                   # a denominator of "0"
        ("25", "25", '"00"', "25/00fps"),             # "00" is not "0"
        ("25", "25.5", "25", "25fps"),                # a non-digit numerator
        ("25", "12.5", "25", "25fps"),
    ])
    def test_fps_spec(self, fps, num, den, expected):
        fields = {}
        if fps != "-":
            fields["FrameRate"] = json.loads(fps)
        if num != "-":
            fields["FrameRate_Num"] = json.loads(num)
        if den != "-":
            fields["FrameRate_Den"] = json.loads(den)
        assert dolbyvision.read_video_info(
            "m", run=_media_run(_video(**fields)))["FPS_SPEC"] == expected

    @pytest.mark.parametrize("size,expected", [
        (123456, "123456"),
        (0, "0"),
        ("123", "123"),
        ("007", "007"),
        (1234.5, ""),     # not all digits
        (-5, ""),         # the minus breaks the digits
        ("12a", ""),
        (None, ""),
    ])
    def test_stream_size(self, size, expected):
        fields = {} if size is None else {"StreamSize": size}
        assert dolbyvision.read_video_info(
            "m", run=_media_run(_video(**fields)))["STREAM_SIZE"] == expected


class TestIsProfile7:
    @pytest.mark.parametrize("profile,settings,expected", [
        ("dvhe.07.06", "", True),
        ("dvhe.07", "BL+EL+RPU", True),
        ("DVHE.07.09", "", True),        # the profile is judged lower-cased
        ("dvhe.07.06", "bl+el+rpu", True),  # the settings are upper-cased
        ("dvhe.07.06", "BL+RPU", True),
        ("", "BL+EL+RPU", True),         # the settings alone are enough
        ("dvhe.04", "BL+EL+RPU", True),
        ("dvhe.09", "BL+EL+RPU", True),
        ("dvhe.05.09", "BL+EL+RPU", False),  # profile 5 is rejected up front
        ("dvh1.05", "BL+EL+RPU", False),
        ("dvav.05", "BL+EL+RPU", False),
        ("dvhe.08.06", "BL+EL+RPU", False),  # already single-layer
        ("dvh1.08", "BL+EL+RPU", False),
        ("hev1.08", "BL+EL+RPU", False),
        ("dvhe.08.06", "", False),
        ("", "", False),
    ])
    def test_gate(self, profile, settings, expected):
        assert dolbyvision.is_profile7(profile, settings) is expected


class TestClaimsDolbyVision:
    @pytest.mark.parametrize("profile,settings,expected", [
        ("dvhe.07.06", "", True),
        ("dvhe.08.06", "", True),
        ("dvh1.09", "", True),
        ("dvav.09.08", "", True),
        ("dva1.09", "", True),
        ("hev1.08.06", "", True),
        ("hvc1.09.08", "", True),
        ("HVC1.09", "", True),          # the profile is judged lower-cased
        ("", "BL+EL+RPU", True),        # the settings alone are enough
        ("", "bl+rpu", True),
        ("", "RPU", True),
        ("mp4v", "", False),
        ("hvc1", "", False),            # a codec string needs the dot
        ("SMPTE ST 2086", "", False),
        ("", "", False),
    ])
    def test_gate(self, profile, settings, expected):
        assert dolbyvision.claims_dolby_vision(profile, settings) is expected


class TestIsHdr:
    @pytest.mark.parametrize("transfer,formats,expected", [
        ("PQ", "", True),
        ("pq", "", True),
        ("SMTE ST 2084", "", True),
        ("HLG", "", True),
        ("BT.2100", "", True),
        ("BT.709", "Dolby Vision / SMPTE ST 2086", True),  # the fallback
        ("BT.709", "dolby vision / HDR10", True),
        ("BT.709", "Dolby Vision / HDR10+", True),
        ("BT.709", "BT.2094", True),
        ("BT.709", "SMPTE ST 2094", True),
        ("BT.709", "HLG", True),
        ("", "SMPTE ST 2086", True),
        ("BT.709", "Dolby Vision", False),  # nothing is left after the cut
        ("BT.709", "Dolby Vision, Version 1.0, dvhe.07.06, BL+EL+RPU", False),
        ("BT.709", "", False),
        ("BT.601", "", False),
        ("", "", False),
    ])
    def test_gate(self, transfer, formats, expected):
        assert dolbyvision.is_hdr(transfer, formats) is expected


class TestIsProfile8:
    @pytest.mark.parametrize("profile,expected", [
        ("dvhe.08.06", True),
        ("DvHe.08.06", True),       # the match is case-insensitive
        ("dvh1.08", True),
        ("hev1.08.06", True),
        ("dvhe.080", True),         # a substring, not a prefix
        ("dvhe.07.06", False),
        ("hev1.07", False),
        ("dvhe.09.08", False),
        ("", False),
    ])
    def test_verdict(self, profile, expected):
        run = _media_run(_video(HDR_Format_Profile=profile))
        ok, seen = dolbyvision.is_profile8("movie.mkv", run=run)
        assert ok is expected
        assert seen == profile

    def test_a_file_it_cannot_read_claims_nothing(self):
        ok, seen = dolbyvision.is_profile8("movie.mkv",
                                           run=_media_run(None, returncode=7))
        assert ok is False
        assert seen == ""

    def test_the_seen_profile_is_what_the_probe_saw(self):
        run = _media_run(_video(HDR_Format_Profile="dvhe.07.06",
                                HDR_Format="Dolby Vision"))
        ok, seen = dolbyvision.is_profile8("movie.mkv", run=run)
        assert ok is False
        assert seen == "dvhe.07.06"


class TestIsDolbyVisionFree:
    def _run(self, profile, settings, transfer, hdr):
        fields = {}
        for name, value in (("HDR_Format_Profile", profile),
                            ("HDR_Format_Settings", settings),
                            ("transfer_characteristics", transfer),
                            ("HDR_Format", hdr)):
            if value is not None:
                fields[name] = value
        return _media_run(_video(**fields))

    def test_free_and_sdr_against_an_sdr_source(self):
        run = self._run(None, None, "BT.709", None)
        ok, seen = dolbyvision.is_dolby_vision_free("movie.mkv", "0", run=run)
        assert ok is True
        assert seen == ("", "", "0")

    def test_free_and_sdr_against_an_hdr_source_fails_the_second_half(self):
        run = self._run(None, None, "BT.709", None)
        ok, seen = dolbyvision.is_dolby_vision_free("movie.mkv", "1", run=run)
        assert ok is False
        assert seen == ("", "", "0")

    def test_free_and_hdr_against_an_hdr_source(self):
        run = self._run(None, None, "PQ", "SMPTE ST 2086")
        ok, seen = dolbyvision.is_dolby_vision_free("movie.mkv", "1", run=run)
        assert ok is True
        assert seen == ("", "", "1")

    def test_a_profile_field_is_not_free(self):
        run = self._run("dvhe.08.06", None, "BT.709", None)
        ok, seen = dolbyvision.is_dolby_vision_free("movie.mkv", "0", run=run)
        assert ok is False
        assert seen == ("dvhe.08.06", "", "0")

    @pytest.mark.parametrize("settings", ["BL+RPU", "bl+rpu", "RPU"])
    def test_an_rpu_in_the_settings_is_not_free(self, settings):
        run = self._run(None, settings, "BT.709", None)
        ok, seen = dolbyvision.is_dolby_vision_free("movie.mkv", "0", run=run)
        assert ok is False
        assert seen[1] == settings

    def test_a_file_it_cannot_read_is_free_against_an_sdr_source(self):
        # no profile and no settings read as no claim; the HDR half reads as
        # "0", so it verifies only against a source that was not HDR
        ok, seen = dolbyvision.is_dolby_vision_free(
            "movie.mkv", "0", run=_media_run(None, returncode=7))
        assert ok is True
        assert seen == ("", "", "0")


class TestStreamHasRpu:
    def _rpu(self, first_rc, second_rc, tmpdir):
        run = _Run({
            "ffmpeg": [(first_rc, b"hevc-bytes", b"")],
            "dovi_tool": [(second_rc, b"", b"")],
        })
        ok = dolbyvision.stream_has_rpu("movie.mkv", run=run, tmpdir=tmpdir)
        return ok, run

    @pytest.mark.parametrize("first,second,expected", [
        (0, 0, True),
        (1, 0, False),   # pipefail: the rightmost non-zero is ffmpeg's
        (0, 3, False),
        (1, 3, False),
    ])
    def test_status(self, first, second, expected, tmp_path):
        ok, _ = self._rpu(first, second, str(tmp_path))
        assert ok is expected

    def test_the_argv_of_both_tools(self, tmp_path):
        _, run = self._rpu(0, 0, str(tmp_path))
        ffmpeg_argv, ffmpeg_kw = run.calls[0]
        dovi_argv, dovi_kw = run.calls[1]
        assert ffmpeg_argv == ["ffmpeg", "-loglevel", "error", "-nostats",
                               "-i", "movie.mkv", "-map", "0:v:0", "-c",
                               "copy", "-frames:v", "48", "-bsf:v",
                               "hevc_mp4toannexb", "-f", "hevc", "-"]
        assert ffmpeg_kw["stdin"] is not None
        assert dovi_argv[:4] == ["dovi_tool", "extract-rpu", "-", "-o"]
        assert dovi_argv[4].startswith(os.path.join(str(tmp_path), "dvRpuProbe."))
        # the pipe: dovi_tool's stdin is the ffmpeg's stdout
        assert dovi_kw["stdin"] == b"hevc-bytes"

    def test_the_probe_is_removed_either_way(self, tmp_path):
        for first, second in ((0, 0), (1, 0)):
            _, run = self._rpu(first, second, str(tmp_path))
            assert not os.path.exists(run.calls[1][0][4])

    def test_the_probe_dir_falls_back_to_tmpdir(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TMPDIR", str(tmp_path))
        run = _Run({
            "ffmpeg": [(0, b"", b"")],
            "dovi_tool": [(0, b"", b"")],
        })
        dolbyvision.stream_has_rpu("movie.mkv", run=run)
        assert run.calls[1][0][4].startswith(os.path.join(str(tmp_path),
                                                          "dvRpuProbe."))


class TestConvertToProfile81:
    def _convert(self, first_rc, second_rc, out, pre_write=b"converted",
                 err=("", ""), tmp_path=None):
        out = str(tmp_path / out)
        if pre_write is not None:
            os.makedirs(os.path.dirname(out), exist_ok=True)
            with open(out, "wb") as handle:
                handle.write(pre_write)
        run = _Run({
            "ffmpeg": [(first_rc, b"", err[0].encode("utf-8"))],
            "dovi_tool": [(second_rc, b"", err[1].encode("utf-8"))],
        })
        logs = []
        status = dolbyvision.convert_to_profile81(
            str(tmp_path / "movie.mkv"), out, log=logs.append, run=run)
        return status, logs, out, run

    def test_success_leaves_the_stream_and_says_nothing(self, tmp_path):
        status, logs, out, run = self._convert(0, 0, "stream.hevc",
                                               tmp_path=tmp_path)
        assert status == 0
        assert logs == []
        assert os.path.getsize(out) > 0
        assert run.calls[0][0][-1] == "-"       # piped, not to a file
        assert run.calls[1][0] == ["dovi_tool", "-m", "2", "--drop-hdr10plus",
                                   "convert", "--discard", "-", "-o", out]

    @pytest.mark.parametrize("first,second,out", [
        (0, 1, b"converted"),    # dovi_tool fails
        (2, 0, None),            # ffmpeg fails (pipefail's rightmost)
        (0, 0, b""),             # the stream is empty
        (0, 0, None),            # the stream is absent
    ])
    def test_failure_leaves_nothing_and_says_why(self, first, second, out,
                                                 tmp_path):
        status, logs, path, _ = self._convert(first, second, "stream.hevc",
                                              pre_write=out, tmp_path=tmp_path)
        assert status == 1
        assert not os.path.exists(path)
        assert logs == [
            "  WARNING: Dolby Vision conversion failed, keeping profile 7: "
            + str(tmp_path / "movie.mkv"),
            "    reason: <no output>",
        ]

    def test_the_reason_is_the_stderr_the_pipeline_captured(self, tmp_path):
        status, logs, _, _ = self._convert(
            0, 1, "stream.hevc", err=("line one\n", "line two\n\n"),
            tmp_path=tmp_path)
        assert status == 1
        # only the trailing newlines are stripped, the way $(...) does
        assert logs[1] == "    reason: line one\nline two"

    def test_the_stream_dest_is_made_for(self, tmp_path):
        status, logs, out, _ = self._convert(0, 0, "stage/converted.hevc",
                                             tmp_path=tmp_path)
        assert status == 0
        assert os.path.exists(out)


class TestExtractVideoStream:
    def _extract(self, returncode, out_content, stdout=b"", tmp_path=None,
                 out_name="stream.hevc"):
        out = str(tmp_path / out_name)
        if out_content is not None:
            with open(out, "wb") as handle:
                handle.write(out_content)
        run = _Run({"ffmpeg": [(returncode, stdout, b"")]})
        logs = []
        status = dolbyvision.extract_video_stream(
            str(tmp_path / "movie.mkv"), out, log=logs.append, run=run)
        return status, logs, out, run

    def test_success_leaves_the_stream_and_says_nothing(self, tmp_path):
        status, logs, out, run = self._extract(0, b"raw-stream",
                                               tmp_path=tmp_path)
        assert status == 0
        assert logs == []
        assert os.path.getsize(out) > 0
        assert run.calls[0][0][-4:] == ["-f", "hevc", "-y", out]
        assert "-" not in run.calls[0][0]    # no pipe: the stream goes to a file

    @pytest.mark.parametrize("returncode,out_content,stdout,reason", [
        (0, b"", b"", "<no output>"),
        (0, None, b"", "<no output>"),
        (0, b"", b"frame data\n\n", "frame data"),
        (1, b"raw-stream", b"error: could not open output",
         "error: could not open output"),
    ])
    def test_failure_leaves_nothing_and_says_why(self, returncode, out_content,
                                                 stdout, reason, tmp_path):
        status, logs, out, _ = self._extract(returncode, out_content,
                                             stdout=stdout, tmp_path=tmp_path)
        assert status == 1
        assert not os.path.exists(out)
        assert logs == [
            "  WARNING: could not copy the video stream out, leaving the "
            "false Dolby Vision claim: " + str(tmp_path / "movie.mkv"),
            "    reason: " + reason,
        ]

# --- the Dolby Vision LEVEL ----------------------------------------------------
# The level is a capability claim, checked by a player against its hardware
# before it decodes anything, so one that overstates the file is refused
# outright - by a device that would have played the same video happily. The
# byte surgery that corrects it is dolbyvisionlevel.py's, pinned in
# test_dolbyvisionlevel.py; what is pinned here is which level a video needs,
# what the record says it declares, and when the correction runs at all.

def _cfg_document(level=10, profile=8, rpu=1, width=3840, height=2160,
                  rate="24/1", side_data=None):
    stream = {"width": width, "height": height, "r_frame_rate": rate}
    if side_data is None:
        record = {"side_data_type": "DOVI configuration record",
                  "dv_profile": profile, "dv_level": level}
        if rpu is not None:
            record["rpu_present_flag"] = rpu
        side_data = [record]
    if side_data != "none":
        stream["side_data_list"] = side_data
    return {"streams": [stream]}


def _cfg_run(*documents, returncode=0):
    """A runner whose successive ffprobe calls print successive documents -
    the correction probes twice, and what the second probe reads is how a case
    says whether the write took."""
    results = []
    for document in documents:
        if isinstance(document, bytes):
            raw = document
        else:
            raw = json.dumps(document, separators=(",", ":")).encode("utf-8")
        results.append((returncode, raw, b""))
    return _Run({"ffprobe": results})


class TestExpectedConfigLevel:
    @pytest.mark.parametrize("geometry,level", [
        ((1280, 720, 24, 1), 1),
        ((1280, 720, 30, 1), 2),
        ((1920, 1080, 24000, 1001), 3),
        ((1920, 1080, 30, 1), 4),
        ((1920, 1080, 60, 1), 5),
        ((3840, 2160, 24000, 1001), 6),
        ((3840, 2160, 30, 1), 7),
        ((3840, 2160, 48, 1), 8),
        ((3840, 2160, 60, 1), 9),
        ((3840, 2160, 120, 1), 10),
        # above the table's last pixel rate the level depends on the bitrate
        # tier rather than the geometry, and nothing here needs them told apart
        ((7680, 4320, 60, 1), 11),
    ])
    def test_each_level_is_named_by_what_it_just_covers(self, geometry, level):
        assert dolbyvision.expected_config_level(*geometry) == level

    @pytest.mark.parametrize("geometry,level", [
        # a scope-cropped 4K frame is SHORTER than 2160 and so needs no more
        # than a full one: the shape the whole correction exists for, found in
        # the wild as a 3840x1608 23.976 fps film declaring level 10
        ((3840, 1608, 24000, 1001), 6),
        ((3840, 2024, 24000, 1001), 6),
    ])
    def test_a_cropped_frame_needs_less_than_a_full_one(self, geometry, level):
        assert dolbyvision.expected_config_level(*geometry) == level

    def test_a_level_covers_its_own_rate_and_one_frame_more_does_not(self):
        assert dolbyvision.expected_config_level(1920, 1080, 30, 1) == 4
        assert dolbyvision.expected_config_level(1920, 1080, 31, 1) == 5

    @pytest.mark.parametrize("geometry", [
        ("", 1080, 24, 1), (1920, 1080, 0, 1), (1920, 1080, "abc", 1),
        (1920, 1080, 24, 0), (0, 1080, 24, 1), (1920, 1080, "1.5", 1),
        (1920, 1080, None, 1),
    ])
    def test_unusable_input_is_no_opinion_never_level_one(self, geometry):
        assert dolbyvision.expected_config_level(*geometry) is None


class TestReadConfigLevel:
    def test_what_the_record_declares_and_what_the_video_needs(self):
        run = _cfg_run(_cfg_document(level=10, width=3840, height=1608,
                                     rate="24000/1001"))
        assert dolbyvision.read_config_level("film.mkv", run=run) == {
            "PROFILE": "8", "LEVEL": "10", "EXPECTED": "6"}
        assert run.calls[0][0][:2] == ["ffprobe", "-v"]
        assert run.calls[0][0][-1] == "film.mkv"

    def test_a_bare_frame_rate_is_its_own_numerator(self):
        run = _cfg_run(_cfg_document(rate="24"))
        assert dolbyvision.read_config_level("film.mkv",
                                             run=run)["EXPECTED"] == "6"

    @pytest.mark.parametrize("document", [
        _cfg_document(rpu=0),           # a claim with nothing behind it
        _cfg_document(rpu=None),        # no flag at all reads as no RPU
        _cfg_document(side_data=[]),
        _cfg_document(side_data="none"),
        _cfg_document(side_data=[{"side_data_type": "Content light level "
                                  "metadata", "max_content": 1000}]),
        _cfg_document(level="high"),    # not a level, so not a record to trust
        _cfg_document(level=10.0),      # nor is one jq prints as a decimal
        {"streams": []},
        {},
    ])
    def test_nothing_to_correct_is_three_empty_values(self, document):
        run = _cfg_run(document)
        assert dolbyvision.read_config_level("film.mkv", run=run) == {
            "PROFILE": "", "LEVEL": "", "EXPECTED": ""}

    @pytest.mark.parametrize("raw", [
        b"", b"not json {", b'"a file"', b'{"streams":"flat"}',
        b'{"streams":[{"side_data_list":"flat"}]}',
        b'{"streams":[{"side_data_list":["flat"]}]}',
    ])
    def test_a_document_the_probe_cannot_navigate_is_empty_too(self, raw):
        run = _cfg_run(raw)
        assert dolbyvision.read_config_level("film.mkv", run=run) == {
            "PROFILE": "", "LEVEL": "", "EXPECTED": ""}

    def test_a_probe_that_fails_says_nothing_about_the_file(self):
        run = _cfg_run(_cfg_document(), returncode=1)
        assert dolbyvision.read_config_level("film.mkv", run=run) == {
            "PROFILE": "", "LEVEL": "", "EXPECTED": ""}

    def test_the_geometry_may_be_unusable_while_the_record_is_not(self):
        run = _cfg_run(_cfg_document(width=0, height=0))
        assert dolbyvision.read_config_level("film.mkv", run=run) == {
            "PROFILE": "8", "LEVEL": "10", "EXPECTED": ""}


class TestNormaliseConfigLevel:
    def _normalise(self, *documents, report_as=None, write_rc=0,
                   monkeypatch=None):
        """The two probes around one correction, with the correction itself
        recorded rather than performed - it is a two-byte edit on a real
        container, and its own white box drives it over one."""
        logs = []
        run = _cfg_run(*documents)
        self.corrections = []

        def correct(path, level):
            self.corrections.append((path, level))
            return write_rc

        monkeypatch.setattr(dolbyvision.dolbyvisionlevel, "correct_level",
                            correct)
        status = dolbyvision.normalise_config_level(
            "film.mkv", report_as=report_as, script_dir="/repo",
            log=logs.append, run=run)
        return status, logs, run

    def test_an_overstated_level_is_corrected_and_verified(self, monkeypatch):
        status, logs, run = self._normalise(
            _cfg_document(level=10, height=1608, rate="24000/1001"),
            _cfg_document(level=6, height=1608, rate="24000/1001"),
            monkeypatch=monkeypatch)
        assert status == 0
        assert logs == [
            "  Corrected the Dolby Vision level, which overstated what the "
            "video needs and",
            "    which players check before they decode anything: 10 -> 6: "
            "film.mkv",
        ]
        # a probe, the correction, and the probe that verifies it
        assert [call[0][0] for call in run.calls] == ["ffprobe", "ffprobe"]
        assert self.corrections == [("film.mkv", "6")]

    def test_the_report_name_stands_in_for_the_path_on_disk(self, monkeypatch):
        _, logs, _ = self._normalise(
            _cfg_document(level=10, height=1608, rate="24000/1001"),
            _cfg_document(level=6, height=1608, rate="24000/1001"),
            report_as="Movies/film.mkv", monkeypatch=monkeypatch)
        assert logs[-1].endswith(": Movies/film.mkv")

    @pytest.mark.parametrize("document", [
        _cfg_document(level=6, height=1608, rate="24000/1001"),   # already right
        _cfg_document(level=3, height=1608, rate="24000/1001"),   # understated
        _cfg_document(rpu=0),                                     # no record
        _cfg_document(width=0, height=0),                         # unmeasurable
    ])
    def test_the_quiet_path_stays_quiet(self, document, monkeypatch):
        status, logs, run = self._normalise(document, monkeypatch=monkeypatch)
        assert (status, logs) == (0, [])
        assert [call[0][0] for call in run.calls] == ["ffprobe"]
        assert self.corrections == []

    def test_a_correction_that_fails_is_a_warning_not_a_rejection(
            self, monkeypatch):
        status, logs, run = self._normalise(
            _cfg_document(level=10, height=1608, rate="24000/1001"),
            write_rc=1, monkeypatch=monkeypatch)
        assert status == 1
        assert logs == [
            "  WARNING: could not correct the overstated Dolby Vision level "
            "(10, should be 6): film.mkv",
        ]
        # the write failed, so there is nothing to verify by re-probing
        assert [call[0][0] for call in run.calls] == ["ffprobe"]

    def test_a_write_that_did_not_take_is_caught_by_the_second_probe(
            self, monkeypatch):
        status, logs, _ = self._normalise(
            _cfg_document(level=10, height=1608, rate="24000/1001"),
            _cfg_document(level=10, height=1608, rate="24000/1001"),
            monkeypatch=monkeypatch)
        assert status == 1
        assert logs == [
            "  WARNING: the Dolby Vision level still reads 10 after "
            "correcting it to 6: film.mkv",
        ]

    def test_a_record_that_vanished_reads_as_nothing(self, monkeypatch):
        status, logs, _ = self._normalise(
            _cfg_document(level=10, height=1608, rate="24000/1001"),
            _cfg_document(rpu=0),
            monkeypatch=monkeypatch)
        assert status == 1
        assert logs == [
            "  WARNING: the Dolby Vision level still reads nothing after "
            "correcting it to 6: film.mkv",
        ]
