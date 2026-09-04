"""Tier D for the HDR10 extract -> remux round trip, and for the two readers
that have to recognise a real HDR file.

Three claims here can only be made against real video:

* the extract-and-remux path does not touch the PIXELS - asserted as the md5 of
  the DECODED frames before and after, which no argv comparison can stand in for;
* the HDR10 identity survives it - the colour tags, the mastering-display volume
  and the content-light levels;
* the two readers reassemble from ffprobe's real output, not from a hand-written
  stub. Their unit tests use one, so this is the single place their INPUT SHAPE
  is checked against a file an encoder actually produced.

The remux has to be mkvmerge and cannot be ffmpeg, which is worth recording
because it is the reason the ingest uses mkvmerge and forces --default-duration:
a raw HEVC elementary stream carries no timestamps, so ffmpeg's Matroska muxer
refuses it and writes a corrupt file, with or without -r / -fflags +genpts.
mkvmerge takes the frame rate as an explicit argument instead.
"""

import hashlib
import re
import shutil
import subprocess

import pytest

from medialib.cli import convert_video as cv

pytestmark = [
    pytest.mark.media,
    pytest.mark.skipif(shutil.which("ffmpeg") is None
                       or shutil.which("ffprobe") is None,
                       reason="tier D needs a real ffmpeg and ffprobe"),
]

# The usual P3-D65/BT.2020 set in x265's G()B()R()WP()L() form (chroma /50000,
# luminance /10000), which is exactly the shape the reader reassembles from
# ffprobe's fractions.
MASTER_DISPLAY = ("G(13250,34500)B(7500,3000)R(34000,16000)"
                  "WP(15635,16450)L(10000000,1)")
MAX_CLL = "1000,400"


def _has_encoder(name):
    done = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"],
                          capture_output=True, text=True)
    return " %s " % name in done.stdout


needs_x265 = pytest.mark.skipif(
    shutil.which("ffmpeg") is not None and not _has_encoder("libx265"),
    reason="libx265 is what writes HDR10 static metadata into the stream")


def _encode(path, transfer, pix_fmt="yuv420p10le", static=True, primaries="bt2020",
            matrix="bt2020nc"):
    params = ["repeat-headers=1", "colorprim=" + primaries,
              "transfer=" + transfer, "colormatrix=" + matrix]
    if static:
        # hdr-opt is x265's PQ-specific optimisation, so it belongs only there
        if transfer == "smpte2084":
            params.insert(0, "hdr-opt=1")
        params += ["master-display=" + MASTER_DISPLAY, "max-cll=" + MAX_CLL]
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=24:duration=1",
         "-pix_fmt", pix_fmt, "-c:v", "libx265", "-crf", "30",
         "-x265-params", ":".join(params),
         "-color_primaries", primaries, "-color_trc", transfer,
         "-colorspace", matrix, str(path)],
        check=True, capture_output=True, stdin=subprocess.DEVNULL)
    return path


def _hdr_fields(path):
    """The HDR10 identity of a file, as one comparable block: the colour tags
    plus the mastering-display and content-light side data."""
    tags = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
         "stream=color_primaries,color_transfer,color_space,pix_fmt",
         "-of", "default=nw=1", str(path)],
        capture_output=True, text=True).stdout
    frames = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-read_intervals", "%+#1", "-show_frames", "-show_entries",
         "frame=side_data_list", "-of", "json", str(path)],
        capture_output=True, text=True).stdout
    side = sorted(re.findall(
        r'"(?:red|green|blue|white_point)_[xy]":\s*"[0-9/]+"'
        r'|"(?:min|max)_luminance":\s*"[0-9/]+"'
        r'|"max_(?:content|average)":\s*[0-9]+', frames))
    return tags, [entry.replace(" ", "") for entry in side]


def _video_md5(path):
    """The md5 of the DECODED frames. Equal before and after means the pixels
    were not touched, which is the actual claim."""
    done = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
         "-i", str(path), "-map", "0:v:0", "-f", "framemd5", "-"],
        capture_output=True, stdin=subprocess.DEVNULL)
    body = b"".join(line for line in done.stdout.splitlines(keepends=True)
                    if not line.startswith(b"#"))
    return hashlib.md5(body).hexdigest()


@pytest.fixture(scope="module")
def hdr10(tmp_path_factory):
    # No mark here: a mark on a fixture does nothing. Every class that uses it
    # carries needs_x265 itself.
    return _encode(tmp_path_factory.mktemp("hdr") / "hdr10 source.mkv",
                   "smpte2084")


@needs_x265
class TestTheFixtureReallyCarriesWhatItClaims:
    """A precondition rather than an assumption: the round trip below proves
    nothing if the source was never HDR10 to begin with."""

    def test_the_colour_tags(self, hdr10):
        tags, _side = _hdr_fields(hdr10)
        assert "color_transfer=smpte2084" in tags
        assert "color_primaries=bt2020" in tags
        assert "pix_fmt=yuv420p10le" in tags

    def test_the_static_metadata(self, hdr10):
        _tags, side = _hdr_fields(hdr10)
        assert any("max_luminance" in entry for entry in side)
        assert any("max_content" in entry for entry in side)

    def test_its_frames_hash_to_something(self, hdr10):
        assert _video_md5(hdr10) != hashlib.md5(b"").hexdigest()


@needs_x265
@pytest.mark.skipif(shutil.which("mkvmerge") is None,
                    reason="only mkvmerge can mux a raw HEVC elementary stream")
class TestTheRoundTrip:
    @pytest.fixture(scope="class")
    def remuxed(self, hdr10, tmp_path_factory):
        work = tmp_path_factory.mktemp("roundtrip")
        stream = work / "stream.hevc"
        # exactly the command the extraction builds: copy the track out as a raw
        # HEVC elementary stream, no re-encode
        subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostats",
             "-i", str(hdr10), "-map", "0:v:0", "-c", "copy",
             "-bsf:v", "hevc_mp4toannexb", "-f", "hevc", "-y", str(stream)],
            check=True, capture_output=True, stdin=subprocess.DEVNULL)
        assert stream.stat().st_size > 0
        out = work / "remuxed.mkv"
        # the shape the ingest builds: the exact frame-rate fraction, and no
        # chapters from a stream that has none
        subprocess.run(
            ["mkvmerge", "--quiet", "-o", str(out), "--default-duration",
             "0:24000/1001fps", "--no-chapters", str(stream)],
            check=True, capture_output=True)
        return out

    def test_it_did_not_re_encode_the_video(self, hdr10, remuxed):
        assert _video_md5(remuxed) == _video_md5(hdr10)

    def test_it_kept_the_whole_hdr10_identity(self, hdr10, remuxed):
        assert _hdr_fields(remuxed) == _hdr_fields(hdr10)

    @pytest.mark.parametrize("field", ["color_transfer=smpte2084",
                                       "color_primaries=bt2020",
                                       "pix_fmt=yuv420p10le"])
    def test_and_each_field_on_its_own_so_a_failure_says_which(self, remuxed,
                                                               field):
        tags, _side = _hdr_fields(remuxed)
        assert field in tags

    def test_the_mastering_display_and_content_light_survived(self, remuxed):
        _tags, side = _hdr_fields(remuxed)
        assert any("max_luminance" in entry for entry in side)
        assert any("max_content" in entry for entry in side)


@needs_x265
class TestTheReadersAgainstARealFile:
    """Their unit tests use a hand-written ffprobe stub, so this is the one
    place their input SHAPE is checked against a file an encoder produced."""

    def test_the_master_display_is_reassembled_exactly(self, hdr10):
        assert cv.hdr_master_display(str(hdr10)) == \
            "%s %s" % (MASTER_DISPLAY, MAX_CLL)

    def test_the_colour_args_come_back_whole(self, hdr10):
        """Asserted as the full string rather than a prefix, because a silently
        dropped field is exactly what this is here to catch - the colour RANGE
        comes along too, since ffprobe reports it for this stream."""
        assert cv.video_color_args(str(hdr10)) == (
            " -color_primaries bt2020 -color_trc smpte2084"
            " -colorspace bt2020nc -color_range tv")


@needs_x265
class TestAnHlgSourceCarryingTheSameStaticMetadata:
    """HLG does not need it - its transfer tag alone tells a display what to do -
    but BT.2100 permits it and some cameras and graders write it, so a source
    that has it must come through with it. Keying the reader on the TRANSFER
    instead of on the metadata is what silently drops this case."""

    @pytest.fixture(scope="class")
    def hlg(self, tmp_path_factory):
        return _encode(tmp_path_factory.mktemp("hlg") / "hlg source.mkv",
                       "arib-std-b67")

    def test_the_fixture_really_is_hlg_and_really_carries_it(self, hlg):
        tags, side = _hdr_fields(hlg)
        assert "color_transfer=arib-std-b67" in tags
        assert any("max_luminance" in entry for entry in side)

    def test_the_reader_takes_its_static_metadata_too(self, hlg):
        assert cv.hdr_master_display(str(hlg)) == \
            "%s %s" % (MASTER_DISPLAY, MAX_CLL)

    def test_and_reads_back_the_hlg_colour_tags(self, hlg):
        assert cv.video_color_args(str(hlg)) == (
            " -color_primaries bt2020 -color_trc arib-std-b67"
            " -colorspace bt2020nc -color_range tv")


@needs_x265
class TestAnSdrSource:
    def test_it_yields_no_static_metadata(self, tmp_path_factory):
        """It cannot carry any, and the probe decodes a frame to look."""
        sdr = _encode(tmp_path_factory.mktemp("sdr") / "sdr source.mkv",
                      "bt709", pix_fmt="yuv420p", static=False,
                      primaries="bt709", matrix="bt709")
        assert cv.hdr_master_display(str(sdr)) == ""
