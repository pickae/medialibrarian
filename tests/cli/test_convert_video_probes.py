"""What convert-video reads out of ffprobe's and ffmpeg's own output.

Fed what the tools really print: ffprobe's JSON for a frame's side data, and
the summary lines idet writes to stderr when its input ends. The probes that
produce them on real files are in `test_convert_video_media.py`.
"""

import json

import pytest

from medialib.cli import convert_video as cv

pytestmark = pytest.mark.pure

MASTERING = {
    "side_data_type": "Mastering display metadata",
    "red_x": "35400/50000", "red_y": "14600/50000",
    "green_x": "8500/50000", "green_y": "39850/50000",
    "blue_x": "6550/50000", "blue_y": "2300/50000",
    "white_point_x": "15635/50000", "white_point_y": "16450/50000",
    "min_luminance": "50/10000", "max_luminance": "40000000/10000",
}
LIGHT = {"side_data_type": "Content light level metadata",
         "max_content": 1000, "max_average": 400}
HDR10PLUS = {"side_data_type": "HDR Dynamic Metadata SMPTE2094-40 (HDR10+)",
             "application version": 1, "num_windows": 1}
MASTER = ("G(8500,39850)B(6550,2300)R(35400,14600)WP(15635,16450)"
          "L(40000000,50)")


class TestTheMasteringDisplayOfASource:

    @pytest.fixture
    def display(self, monkeypatch):
        def run(transfer, side_data):
            frames = json.dumps({"frames": [{"side_data_list": side_data}]},
                                indent=4)
            asked = []

            def probe(argv):
                asked.append(argv)
                return transfer + "\n" if len(asked) == 1 else frames

            monkeypatch.setattr(cv, "_probe", probe)
            return cv.hdr_master_display("film.mkv")
        return run

    def test_a_pq_source_with_both_records(self, display):
        assert display("smpte2084", [HDR10PLUS, MASTERING, LIGHT]) == (
            MASTER + " 1000,400")

    def test_no_light_level_record_is_zero_zero(self, display):
        assert display("smpte2084", [MASTERING]) == MASTER + " 0,0"

    def test_an_hlg_source_that_carries_it_keeps_it(self, display):
        assert display("arib-std-b67", [MASTERING, LIGHT]) == (
            MASTER + " 1000,400")

    def test_an_hlg_source_without_it_has_none(self, display):
        assert display("arib-std-b67", [LIGHT]) == ""

    def test_an_sdr_source_has_none(self, display):
        assert display("bt709", [MASTERING, LIGHT]) == ""

    def test_a_probe_that_printed_nothing_has_none(self, monkeypatch):
        answers = iter(["smpte2084\n", ""])
        monkeypatch.setattr(cv, "_probe", lambda argv: next(answers))
        assert cv.hdr_master_display("film.mkv") == ""


IDET_TAIL = (
    "[Parsed_idet_0 @ 0x5602a1e4c2c0] Repeated Fields: Neither:  1199 Top:"
    "     1 Bottom:     0\n"
    "[Parsed_idet_0 @ 0x5602a1e4c2c0] Single frame detection: TFF:    52 BFF:"
    "     0 Progressive:   917 Undetermined:   231\n"
    "[Parsed_idet_0 @ 0x5602a1e4c2c0] Multi frame detection: TFF:   %d BFF:"
    "     %d Progressive:   %d Undetermined:    19\n"
)


class TestHowTheFieldsOfASourceWereCounted:

    @pytest.fixture
    def counts(self, monkeypatch):
        def run(stderr):
            class Done:
                pass

            Done.stderr = stderr.encode()
            monkeypatch.setattr(cv.subprocess, "run",
                                lambda argv, **kwargs: Done())
            return cv.interlace_counts("film.mkv")
        return run

    def test_the_multi_frame_counts_are_the_ones_taken(self, counts):
        assert counts("frame= 1200 fps=410 q=-0.0 Lsize=N/A\n"
                      + IDET_TAIL % (3, 0, 1178)) == (3, 0, 1178)

    def test_a_telecined_source(self, counts):
        assert counts(IDET_TAIL % (478, 0, 703)) == (478, 0, 703)

    def test_a_source_that_could_not_be_decoded_is_not_measured(self, counts):
        assert counts("film.mkv: Invalid data found when processing input\n"
                      ) is None

    def test_no_ffmpeg_is_not_measured(self, monkeypatch):
        def missing(argv, **kwargs):
            raise FileNotFoundError(2, "No such file or directory: 'ffmpeg'")

        monkeypatch.setattr(cv.subprocess, "run", missing)
        assert cv.interlace_counts("film.mkv") is None
