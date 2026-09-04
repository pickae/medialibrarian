"""The white box for medialib/lib/censusmedia.py.

What is pinned here: the exact argv the probe is handed, and the jq semantics the
reading of a document depends on - which are the whole difficulty of this port and
are worth stating.

Those semantics, each pinned below: ``tonumber?`` over a word produces NO value
and not null; a binding over no value makes the whole filter print nothing; ``//``
is LAZY, so an alternative jq never reaches cannot fail; ``*`` on a string repeats
it instead of failing; and an empty stream inside an ``if`` condition takes the
``if`` with it. Every field the shell then reads out of that silence is empty,
which the gates read as "no audio stream" or "no video track" - not the same
answer as an unreadable file, and the difference is a skip reason.
"""

import json
import os
import shutil
from types import SimpleNamespace

import pytest

from medialib.lib import censusmedia as cm
from tests import blackbox

pytestmark = pytest.mark.stubbed

_TOOLSTUB = blackbox.TOOLSTUB

_PLUMBING = ("bash", "awk", "cat", "base64", "stat")

_CENSUS_ENV = ("CENSUS_SEP", "CENSUS_HAVE_MEDIAINFO")


@pytest.fixture()
def probe(tmp_path, monkeypatch):
    """A PATH holding only an ffprobe stub, whose canned answer is the document
    a test hands it."""
    bin_dir = tmp_path / "bin"
    out_dir = tmp_path / "stubout"
    state_dir = tmp_path / "stubstate"
    for directory in (bin_dir, out_dir, state_dir):
        directory.mkdir()
    for tool in _PLUMBING:
        (bin_dir / tool).symlink_to(shutil.which(tool))
    record = tmp_path / "calls"

    def install(name):
        target = bin_dir / name
        shutil.copyfile(_TOOLSTUB, str(target))
        os.chmod(str(target), 0o755)

    def says(document, name="ffprobe", rc="0"):
        install(name)
        text = document if isinstance(document, str) else json.dumps(document)
        (out_dir / name).write_text(text)
        (out_dir / (name + ".rc")).write_text(rc + "\n")

    def calls():
        if not record.exists():
            return []
        return [line.rstrip("\n").split("\t")[1:]
                for line in record.read_text().splitlines() if line]

    def media(name, size=0):
        path = tmp_path / name
        path.write_bytes(b"\0" * size)
        return str(path)

    for name in _CENSUS_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("PATH", str(bin_dir))
    monkeypatch.setenv("TOOLSTUB_LOG", str(record))
    monkeypatch.setenv("TOOLSTUB_OUT", str(out_dir))
    monkeypatch.setenv("TOOLSTUB_STATE", str(state_dir))
    monkeypatch.setenv("LC_ALL", "C")
    return SimpleNamespace(says=says, calls=calls, media=media,
                           install=install, tmp_path=tmp_path)


# --- one probe per file ---------------------------------------------------------


class TestTheProbe:
    def test_one_call_asks_for_everything_both_rows_need(self, probe):
        path = probe.media("a.mp3")
        probe.says({"streams": [{"codec_type": "audio", "channels": 2}]})
        cm.census_audio_row(path)
        assert probe.calls() == [[
            "ffprobe", "-v", "quiet", "-print_format", "json",
            "-show_format", "-show_streams", "-show_chapters", path]]

    def test_a_probe_that_printed_and_then_failed_is_still_read(self, probe):
        """The shell ends the call with ``|| true``: the status is discarded on
        purpose, so a probe that described the file and then complained about
        something else has still described it."""
        path = probe.media("a.mp3", 10)
        probe.says({"streams": [{"codec_type": "audio", "channels": 2,
                                 "codec_name": "mp3"}]}, rc="7")
        row, reason = cm.census_audio_row(path)
        assert reason is None
        assert row.split(",")[5] == "mp3"

    def test_a_probe_that_says_nothing_is_an_unreadable_file(self, probe):
        path = probe.media("a.mp3")
        probe.says("")
        row, reason = cm.census_audio_row(path)
        assert row is None
        assert reason == "ffprobe could not read it (not audio, or truncated)"

    def test_a_tool_that_is_not_there_is_the_same_answer(self, probe):
        row, reason = cm.census_audio_row(str(probe.tmp_path / "a.mp3"))
        assert row is None
        assert reason.startswith("ffprobe could not read it")


# --- the jq semantics the two readings share ------------------------------------


class TestTheFilterSemantics:
    """Each of these is a place where jq does something a reader of the program
    would not predict, and where the port had to be written to match rather than
    to be sensible."""

    def test_tonumber_over_a_word_is_no_value_and_not_null(self):
        assert cm._num("abc") is cm._EMPTY
        assert cm._num("") is None
        assert cm._num(None) is None
        assert cm._num("12") == 12
        # never a numeric PREFIX, which is what awk would have taken
        assert cm._num("12abc") is cm._EMPTY

    def test_a_binding_over_no_value_takes_the_whole_filter(self):
        with pytest.raises(cm._NoOutput):
            cm._bind(cm._EMPTY)
        assert cm._bind(None) is None

    def test_the_alternative_is_lazy(self):
        """An alternative jq never reaches cannot fail."""
        def boom():
            raise AssertionError("this alternative must not be reached")

        assert cm._alt_lazy(lambda: 5, boom) == 5
        # ...and it IS reached when the left is null, false or empty
        assert cm._alt_lazy(lambda: None, lambda: 7) == 7
        assert cm._alt_lazy(lambda: False, lambda: 7) == 7
        assert cm._alt_lazy(lambda: cm._EMPTY, lambda: 7) == 7

    def test_the_last_alternative_reached_is_the_answer_whatever_it_is(self):
        assert cm._alt_lazy(lambda: None, lambda: None) is None
        assert cm._alt_lazy(lambda: None, lambda: cm._EMPTY) is cm._EMPTY

    def test_zero_is_not_falsy_in_jq(self):
        """Only null and false are: a stated bitrate of 0 is a stated bitrate."""
        assert cm._alt_lazy(lambda: 0, lambda: 7) == 0
        assert cm._alt("", "x") == ""

    def test_multiplying_a_string_repeats_it(self):
        """Which is how a channels field of "two" becomes a very long string
        rather than an error - and takes the subtraction below down with it."""
        assert cm._mul("ab", 3) == "ababab"
        assert cm._mul("ab", 0) is None
        with pytest.raises(cm._NoOutput):
            cm._sub(10, "ab")

    def test_subtracting_anything_but_two_numbers_is_fatal(self):
        with pytest.raises(cm._NoOutput):
            cm._sub(None, 5)
        with pytest.raises(cm._NoOutput):
            cm._sub(5, True)
        assert cm._sub(10, 4) == 6


class TestWhatTheFilterCannotRead:
    """A document the filter cannot navigate leaves every field empty, which is
    not the same thing as a file the probe could not read - and the gates turn the
    two into different skip reasons."""

    def _audio(self, probe, document, name="a.mp3"):
        path = probe.media(name, 10)
        probe.says(document)
        return cm.census_audio_row(path)

    @pytest.mark.parametrize("document", [
        "not json", "[]", "null", "3", '{"streams":3}', '{"streams":[3]}',
        '{"format":3}', '{"chapters":3}',
    ])
    def test_a_document_the_filter_cannot_navigate_says_nothing(self, probe,
                                                               document):
        row, reason = self._audio(probe, document)
        assert row is None
        # every field empty, so the audio-stream gate is what refuses it
        assert reason == "its suffix says audio but it holds no audio stream"

    def test_and_that_is_a_different_reason_from_an_unreadable_file(self,
                                                                   probe):
        path = probe.media("a.mp3")
        probe.says("")
        _row, reason = cm.census_audio_row(path)
        assert reason == "ffprobe could not read it (not audio, or truncated)"


# --- the audio row --------------------------------------------------------------


class TestAudioRow:
    def _row(self, probe, document, name="a.mp3", size=4096):
        path = probe.media(name, size)
        probe.says(document)
        return cm.census_audio_row(path)

    def test_the_columns_are_path_size_duration_bitrate_channels_codec_chapters(
            self, probe):
        row, reason = self._row(probe, {
            "format": {"duration": "100.5", "bit_rate": "128000"},
            "streams": [{"codec_type": "audio", "channels": 2,
                         "codec_name": "mp3"}],
            "chapters": [{}, {}, {}]})
        assert reason is None
        assert row.split(",")[1:] == ["4096", "100.500", "128000", "2", "mp3",
                                      "3"]

    def test_the_bitrate_is_the_containers_and_not_the_streams(self, probe):
        """It is the number actually stated for every format this list holds, and
        over a spoken-word file it differs from the stream's by the cover art
        alone."""
        row, _reason = self._row(probe, {
            "format": {"bit_rate": "128000"},
            "streams": [{"codec_type": "audio", "channels": 2,
                         "bit_rate": "96000"}]})
        assert row.split(",")[3] == "128000"

    def test_the_streams_own_is_the_fallback(self, probe):
        row, _reason = self._row(probe, {
            "streams": [{"codec_type": "audio", "channels": 2,
                         "bit_rate": "96000"}]})
        assert row.split(",")[3] == "96000"

    def test_and_a_matroska_bps_tag_is_the_fallback_after_that(self, probe):
        """Matroska states no per-stream bitrate; mkvmerge writes a BPS tag
        instead, whose suffix spelling is not fixed."""
        row, _reason = self._row(probe, {
            "streams": [{"codec_type": "audio", "channels": 2,
                         "tags": {"BPS-eng": "80000"}}]}, name="a.mka")
        assert row.split(",")[3] == "80000"

    def test_a_file_with_no_audio_stream_is_not_audio(self, probe):
        row, reason = self._row(probe, {"streams": [
            {"codec_type": "video", "codec_name": "h264"}]})
        assert row is None
        assert reason == "its suffix says audio but it holds no audio stream"

    def test_an_mka_that_holds_video_is_a_video(self, probe):
        """Matroska holds anything, and censusing such a file as audio would
        silently drop its resolution, its codec and its subtitle tracks."""
        row, reason = self._row(probe, {"streams": [
            {"codec_type": "audio", "channels": 2},
            {"codec_type": "video", "codec_name": "h264"}]}, name="a.mka")
        assert row is None
        assert reason == "its suffix says audio but it holds a video track"

    def test_but_an_embedded_cover_is_a_picture_and_not_a_track(self, probe):
        row, reason = self._row(probe, {
            "format": {"bit_rate": "128000"},
            "streams": [{"codec_type": "audio", "channels": 2,
                         "codec_name": "mp3"},
                        {"codec_type": "video", "codec_name": "mjpeg",
                         "disposition": {"attached_pic": 1}}]})
        assert reason is None
        assert row.split(",")[5] == "mp3"

    def test_a_file_that_marks_no_chapter_is_the_one_chapter_it_is(self, probe):
        """Recorded as 0 the column would be unusable in exactly the place a
        census is for: a thousand unmarked audiobooks would total zero."""
        row, _reason = self._row(probe, {
            "streams": [{"codec_type": "audio", "channels": 2}],
            "chapters": []})
        assert row.split(",")[6] == "1"


# --- the video row --------------------------------------------------------------


class TestVideoRow:
    FILM = {
        "format": {"duration": "3600.0", "bit_rate": "5000000",
                   "size": "2000000000", "format_name": "matroska,webm"},
        "streams": [
            {"codec_type": "video", "codec_name": "h264", "width": 1920,
             "height": 1080, "avg_frame_rate": "25/1", "r_frame_rate": "50/1",
             "bit_rate": "4000000", "color_transfer": "bt709"},
            {"codec_type": "audio", "codec_name": "eac3", "channels": 6,
             "bit_rate": "640000"},
            {"codec_type": "subtitle"},
            {"codec_type": "subtitle"},
        ],
        "chapters": [{}, {}],
    }

    def _row(self, probe, document=None, name="m.mkv", size=4096):
        path = probe.media(name, size)
        probe.says(self.FILM if document is None else document)
        return cm.census_video_row(path)

    def test_the_columns_in_order(self, probe):
        row, reason = self._row(probe)
        assert reason is None
        assert row.split(",") == [
            os.path.join(str(probe.tmp_path), "m.mkv"), "4096", "3600.000",
            "4000000", "starved", "1920x1080", "25.000", "h264", "matroska",
            "SDR", "1", "6", "eac3", "640000", "2", "2"]

    def test_the_frame_rate_is_the_average_and_not_the_nominal(self, probe):
        """r_frame_rate is doubled for anything field-coded and would report a
        25fps interlaced broadcast as 50fps - which is what the column exists to
        answer."""
        row, _reason = self._row(probe)
        assert row.split(",")[6] == "25.000"

    def test_the_nominal_is_the_fallback_for_a_stream_that_states_no_average(
            self, probe):
        document = json.loads(json.dumps(self.FILM))
        del document["streams"][0]["avg_frame_rate"]
        row, _reason = self._row(probe, document)
        assert row.split(",")[6] == "50.000"

    def test_the_resolution_is_the_coded_size(self, probe):
        row, _reason = self._row(probe)
        assert row.split(",")[5] == "1920x1080"

    def test_a_stream_missing_half_its_size_has_no_resolution(self, probe):
        document = json.loads(json.dumps(self.FILM))
        del document["streams"][0]["height"]
        row, _reason = self._row(probe, document)
        assert row.split(",")[5] == ""

    def test_only_embedded_subtitles_are_counted(self, probe):
        """A sidecar beside the film is a separate file, so a film with external
        subtitles reads 0 - the honest answer to "what does this file hold"."""
        row, _reason = self._row(probe)
        assert row.split(",")[14] == "2"

    def test_a_file_with_no_video_stream_is_not_video(self, probe):
        row, reason = self._row(probe, {"streams": [
            {"codec_type": "audio", "channels": 2}]}, name="m.mp4")
        assert row is None
        assert reason == "its suffix says video but it holds no video track"

    def test_a_cover_is_not_a_video_track_here_either(self, probe):
        row, reason = self._row(probe, {"streams": [
            {"codec_type": "audio", "channels": 2},
            {"codec_type": "video", "codec_name": "mjpeg",
             "disposition": {"attached_pic": 1}}]}, name="m.mp4")
        assert row is None
        assert reason == "its suffix says video but it holds no video track"

    def test_the_first_audio_track_is_the_one_a_player_would_pick(self, probe):
        document = json.loads(json.dumps(self.FILM))
        document["streams"].append({"codec_type": "audio", "codec_name": "aac",
                                    "channels": 2, "bit_rate": "128000"})
        row, _reason = self._row(probe, document)
        fields = row.split(",")
        assert fields[10] == "2"          # both tracks counted
        assert fields[11:14] == ["6", "eac3", "640000"]   # only the first named

    def test_a_bitrate_the_container_does_not_state_is_estimated(self, probe):
        """Which is what keeps the adequacy column from being empty for most of a
        Matroska library."""
        document = json.loads(json.dumps(self.FILM))
        del document["streams"][0]["bit_rate"]
        row, _reason = self._row(probe, document)
        fields = row.split(",")
        # the column itself stays empty - the estimate is not a reading
        assert fields[3] == ""
        # ...but the judgement is made on it
        assert fields[4] in ("starved", "adequate", "generous")

    def test_the_estimate_removes_the_audio_it_can_see(self, probe):
        document = json.loads(json.dumps(self.FILM))
        del document["streams"][0]["bit_rate"]
        document["format"]["bit_rate"] = "6000000"
        document["streams"][1]["bit_rate"] = "2000000"
        row, _reason = self._row(probe, document)
        # 6 Mbit less 2 Mbit of audio, at 98%, is 3.92 - starved for 1080p h264,
        # where the whole 6 Mbit would have been adequate
        assert row.split(",")[4] == "starved"


class TestContainerName:
    @pytest.mark.parametrize("formats,extension,expected", [
        # the suffix is one of the family's members, so it is the answer
        ("matroska,webm", "webm", "webm"),
        ("mov,mp4,m4a,3gp,3g2,mj2", "mp4", "mp4"),
        ("mov,mp4,m4a,3gp,3g2,mj2", "m4a", "m4a"),
        # ...otherwise the family is named by the member everyone calls it by
        ("matroska,webm", "mkv", "matroska"),
        ("mov,mp4,m4a,3gp,3g2,mj2", "m4v", "mp4"),
        ("avi", "avi", "avi"),
        ("mpegts", "ts", "mpegts"),
        # a file ffprobe named no format for keeps its suffix
        ("", "mkv", "mkv"),
        ("", "", ""),
    ])
    def test_the_real_container(self, formats, extension, expected):
        assert cm.census_container_name(formats, extension) == expected

    def test_a_whole_entry_has_to_match(self):
        """Both sides are padded with the separator, so "mp4" must not match
        inside "mp4a"."""
        assert cm.census_container_name("mp4a,x", "mp4") == "mp4a"

    def test_it_is_case_blind(self):
        assert cm.census_container_name("MATROSKA,WEBM", "MKV") == "matroska"


class TestDynamicRange:
    """Without mediainfo the enum is derived from what the single ffprobe pass
    already returned - the transfer function, and whether a DOVI configuration
    record is attached. That misses HDR10+ and reports it as HDR10, which is what
    such a file also is."""

    @pytest.mark.parametrize("transfer,records,expected", [
        ("bt709", "0", "SDR"),
        ("", "0", "SDR"),
        ("smpte2084", "0", "HDR10"),
        ("SMPTE2084", "0", "HDR10"),
        ("arib-std-b67", "0", "HLG"),
        ("hlg", "0", "HLG"),
        ("bt709", "1", "DolbyVision"),
        ("smpte2084", "1", "DolbyVision+HDR10"),
        ("arib-std-b67", "1", "DolbyVision+HLG"),
        ("smpte2084", "2", "DolbyVision+HDR10"),
    ])
    def test_the_ffprobe_only_arm(self, probe, transfer, records, expected):
        assert cm.census_dynamic_range(probe.media("m.mkv"), transfer,
                                       records) == expected

    def test_a_records_count_that_is_not_a_number_claims_nothing(self, probe):
        assert cm.census_dynamic_range(probe.media("m.mkv"), "bt709",
                                       "") == "SDR"

    def test_with_mediainfo_the_repos_own_readers_decide(self, probe,
                                                        monkeypatch):
        """The CLAIM is the codec string in HDR_Format_Profile (or an RPU in
        HDR_Format_Settings), not the words in HDR_Format: a file whose
        HDR_Format merely says "Dolby Vision" without naming a profile is read as
        the HDR10 its transfer function makes it."""
        monkeypatch.setenv("CENSUS_HAVE_MEDIAINFO", "1")
        path = probe.media("m.mkv")
        probe.says({"media": {"track": [
            {"@type": "Video", "HDR_Format": "Dolby Vision",
             "HDR_Format_Profile": "dvhe.08",
             "transfer_characteristics": "PQ"}]}}, name="mediainfo")
        assert cm.census_dynamic_range(path, "", "0") == "DolbyVision+HDR10"
        assert probe.calls() == [["mediainfo", "--Output=JSON", path]]

    def test_hdr_format_naming_the_words_alone_is_not_a_claim(self, probe,
                                                             monkeypatch):
        monkeypatch.setenv("CENSUS_HAVE_MEDIAINFO", "1")
        path = probe.media("m.mkv")
        probe.says({"media": {"track": [
            {"@type": "Video", "HDR_Format": "Dolby Vision",
             "transfer_characteristics": "PQ"}]}}, name="mediainfo")
        assert cm.census_dynamic_range(path, "", "0") == "HDR10"

    def test_dynamic_metadata_is_hdr10_plus(self, probe, monkeypatch):
        monkeypatch.setenv("CENSUS_HAVE_MEDIAINFO", "1")
        path = probe.media("m.mkv")
        probe.says({"media": {"track": [
            {"@type": "Video", "HDR_Format": "SMPTE ST 2094 App 4",
             "transfer_characteristics": "PQ"}]}}, name="mediainfo")
        assert cm.census_dynamic_range(path, "", "0") == "HDR10+"

    def test_a_claim_with_no_hdr_base_layer_is_dolby_vision_alone(
            self, probe, monkeypatch):
        """Profile 5 has no HDR10 base layer at all, and reporting it as
        DolbyVision+HDR10 would claim a compatibility it does not have."""
        monkeypatch.setenv("CENSUS_HAVE_MEDIAINFO", "1")
        path = probe.media("m.mkv")
        probe.says({"media": {"track": [
            {"@type": "Video", "HDR_Format": "Dolby Vision",
             "HDR_Format_Profile": "dvhe.05"}]}}, name="mediainfo")
        assert cm.census_dynamic_range(path, "", "0") == "DolbyVision"

    def test_a_mediainfo_answer_that_cannot_be_read_is_sdr(self, probe,
                                                           monkeypatch):
        monkeypatch.setenv("CENSUS_HAVE_MEDIAINFO", "1")
        path = probe.media("m.mkv")
        probe.says("not json", name="mediainfo")
        assert cm.census_dynamic_range(path, "", "0") == "SDR"


class TestBitrateAdequacy:
    """The one judgement in this census rather than a reading, made against the
    same model convertVideo's -t decides a single file with - so a library counted
    here as starved is the set of files that test refuses to re-encode."""

    def test_a_file_whose_size_nobody_stated_cannot_be_judged(self, probe):
        path = probe.media("m.mkv")
        assert cm.census_bitrate_adequacy(path, "h264", "", "1080", "25",
                                          "5000000") == "unknown"
        assert cm.census_bitrate_adequacy(path, "h264", "1920", "", "25",
                                          "5000000") == "unknown"

    def test_nor_one_whose_bitrate_nobody_stated(self, probe):
        path = probe.media("m.mkv")
        assert cm.census_bitrate_adequacy(path, "h264", "1920", "1080", "25",
                                          "") == "unknown"
        assert cm.census_bitrate_adequacy(path, "h264", "1920", "1080", "25",
                                          "abc") == "unknown"

    def test_unknown_rather_than_empty(self, probe):
        """Every other column here uses the empty string for "nobody stated it",
        but a dimension in a cube must be a value and never a hole - so the two
        things that cannot be judged answer "unknown"."""
        path = probe.media("m.mkv")
        assert cm.census_bitrate_adequacy(path, "h264", "", "", "", "") \
            == "unknown"

    def test_an_unstated_codec_is_still_judged(self, probe):
        """The model has a tuning for a codec it does not know, so what decides
        whether a judgement is possible at all is the frame size and the
        bitrate - not the codec."""
        path = probe.media("m.mkv")
        assert cm.census_bitrate_adequacy(path, "", "1920", "1080", "25",
                                          "5000000") == "adequate"

    @pytest.mark.parametrize("bits,expected", [
        ("500000", "starved"), ("5880000", "adequate"),
        ("20000000", "generous")])
    def test_the_three_verdicts(self, probe, bits, expected):
        path = probe.media("m.mkv")
        assert cm.census_bitrate_adequacy(path, "h264", "1920", "1080", "25",
                                          bits) == expected

    def test_the_grain_axis_is_switched_off(self, probe):
        """Grain has to be measured off the pixels, which is a decode per file -
        exactly what a census does not do. The grainless reading is the model's
        most generous one and never calls a file starved that a grain-aware run
        would not."""
        path = probe.media("m.mkv")
        # no tool is reached for at all
        cm.census_bitrate_adequacy(path, "h264", "1920", "1080", "25",
                                   "5000000")
        assert probe.calls() == []
