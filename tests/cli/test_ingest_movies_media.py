"""Tier D for a container claiming Dolby Vision over a video that carries no
RPU, and the improved-copy remux dropping that claim without taking the file's
real HDR with it.

The fixture looks impossible to synthesise - no encoder writes a Dolby Vision
configuration record, so there is nothing to make a claim out of - but a FALSE
claim needs no RPU, only the 24 container bytes that lie about one.
`injectDolbyVisionClaim.py` writes those into a generated clip's track header,
which runs the whole strip in a second on 30 KB of testsrc2.

Nothing is stubbed: real ffmpeg, real dovi_tool, real mkvmerge, real mediainfo.
The Tier C file asserts the decisions and the command shapes against stubs; this
asserts the BYTES those commands really produce.

Both shapes are here because the strip has two legitimate outcomes and only a
real file shows which fields decide. Over real HDR10, mediainfo joins the two HDR
formats with " / " and every Dolby Vision field trails that separator; the copy
must come out as HDR10 and nothing else. Over plain BT.709 SDR the fields have no
separator, and the copy must claim no HDR at all - the outcome that catches a
strip mistaking "no Dolby Vision" for "no HDR".
"""

import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from tests import blackbox

pytestmark = [
    pytest.mark.media,
    pytest.mark.skipif(
        any(shutil.which(tool) is None for tool in
            ("ffmpeg", "ffprobe", "mediainfo", "mkvmerge", "dovi_tool")),
        reason="tier D needs the real strip pipeline end to end"),
]

_REPO = blackbox.REPO
_INJECT = blackbox.DATA / "injectDolbyVisionClaim.py"

MASTER_DISPLAY = ("G(13250,34500)B(7500,3000)R(34000,16000)"
                  "WP(15635,16450)L(10000000,500)")
MAX_CLL = "600,200"


def _has_x265():
    done = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"],
                          capture_output=True, text=True)
    return " libx265 " in done.stdout


needs_x265 = pytest.mark.skipif(
    shutil.which("ffmpeg") is not None and not _has_x265(),
    reason="libx265 writes the HDR10 base the claim is injected over")


def _run(argv, **kwargs):
    return subprocess.run(argv, capture_output=True, stdin=subprocess.DEVNULL,
                          **kwargs)


def _make_claim(dest, x265_params, colour_args, work):
    """A one-second 10-bit clip with the colour metadata asked for, the Dolby
    Vision claim written into its track header, then remuxed by mkvmerge - the
    pass that rebuilds the SeekHead and Cues the injector had to void, so the
    fixture is an ordinary Matroska file.

    One second and not half of one: mediainfo only reports FrameRate_Num/Den for
    a clip long enough to be sure of them, and those are what the read turns into
    the exact "24000/1001fps" the remux is given. A shorter fixture would quietly
    exercise the rounded-decimal fallback instead.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    base, claimed = work / "base.mkv", work / "claimed.mkv"
    audio = work / "audio.mka"
    _run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
          "-f", "lavfi",
          "-i", "testsrc2=size=320x180:rate=24000/1001:duration=1",
          "-pix_fmt", "yuv420p10le", "-c:v", "libx265", "-crf", "30",
          "-x265-params", "repeat-headers=1:" + x265_params,
          *colour_args, str(base)], check=True)
    # an Opus track comes along so the remux has a non-video track to carry over
    _run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
          "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
          "-c:a", "libopus", "-b:a", "48k", str(audio)], check=True)
    _run([sys.executable, str(_INJECT), str(base), str(claimed)], check=True)
    _run(["mkvmerge", "--quiet", "-o", str(dest), "--language", "0:eng",
          "--track-name", "0:Feature", str(claimed), "--language", "0:eng",
          str(audio)], check=True)
    for leftover in (base, claimed, audio):
        leftover.unlink()
    return dest


def _dv_fields(path):
    """The Dolby Vision / HDR identity mediainfo reports, as one comparable
    line. Absent fields print as "-", so a lost field is visible rather than
    shifting the others along."""
    done = _run(["mediainfo", "--Output=JSON", str(path)], text=True)
    tracks = json.loads(done.stdout).get("media", {}).get("track", [])
    video = next((t for t in tracks if t.get("@type") == "Video"), {})

    def field(name):
        return video.get(name, "-")

    return (
        "HDR_Format=%s profile=%s settings=%s compat=%s transfer=%s"
        " primaries=%s maxcll=%s md=%s fps=%s/%s" % (
            field("HDR_Format"), field("HDR_Format_Profile"),
            field("HDR_Format_Settings"), field("HDR_Format_Compatibility"),
            field("transfer_characteristics"), field("colour_primaries"),
            field("MaxCLL"), field("MasteringDisplay_Luminance"),
            field("FrameRate_Num"), field("FrameRate_Den")))


def _video_md5(path):
    """md5 of the DECODED frames, to prove nothing re-encoded them."""
    done = _run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
                 "-i", str(path), "-map", "0:v:0", "-f", "framemd5", "-"])
    body = b"".join(line for line in done.stdout.splitlines(keepends=True)
                    if not line.startswith(b"#"))
    return hashlib.md5(body).hexdigest()


def _has_rpu(path, work):
    """Exactly the probe the strip runs before deciding."""
    probe = work / "probe.rpu"
    piped = subprocess.run(
        "ffmpeg -loglevel error -nostats -i %s -map 0:v:0 -c copy "
        "-frames:v 48 -bsf:v hevc_mp4toannexb -f hevc - | "
        "dovi_tool extract-rpu - -o %s" % (repr(str(path)), repr(str(probe))),
        shell=True, capture_output=True)
    probe.unlink(missing_ok=True)
    return piped.returncode == 0


def _dovi_records(path):
    """The dvcC record itself, not mediainfo's rendering of it: ffprobe reports
    it as stream side data, and it has to be gone."""
    done = _run(["ffprobe", "-v", "error", "-select_streams", "v:0",
                 "-show_entries", "stream_side_data", "-of", "json",
                 str(path)], text=True)
    streams = json.loads(done.stdout).get("streams", [{}])
    side = streams[0].get("side_data_list", []) if streams else []
    return sum(1 for entry in side
               if entry.get("side_data_type") == "DOVI configuration record")


def _tree(root):
    return sorted(str(p.relative_to(root)) for p in Path(root).rglob("*"))


def _improve(library, ram_root, log_path):
    """The improve phase as a PROCESS, which is the phase a user gets."""
    done = _run([sys.executable, "-c", """
import sys
from medialib.cli import ingest_movies_run as run
state = run.Run(script_dir=sys.argv[1], ram_root=sys.argv[2], skips=None,
                fragments_file="", whisper={}, ffsubsync_quality="no")
run.improve_main_movies(state, sys.argv[3])
""", str(_REPO), str(ram_root), str(library)],
                env={**__import__("os").environ, "PYTHONPATH": str(_REPO)},
                text=True)
    Path(log_path).write_text(done.stdout + done.stderr)
    return done.returncode


@pytest.fixture(scope="module")
def library(tmp_path_factory):
    """Two film folders, which is the layout the phase walks: the single .mkv
    directly in a folder is that film's main movie. Improved once, with
    everything measured before and after."""
    work = tmp_path_factory.mktemp("dvclaim")
    inbox = work / "library"
    ram_root = work / "ram"
    ram_root.mkdir()

    hdr = _make_claim(
        inbox / "Claimed Hdr10 (2020)" / "Claimed Hdr10 (2020).mkv",
        "hdr-opt=1:colorprim=bt2020:transfer=smpte2084:colormatrix=bt2020nc:"
        "master-display=%s:max-cll=%s" % (MASTER_DISPLAY, MAX_CLL),
        ["-color_primaries", "bt2020", "-color_trc", "smpte2084",
         "-colorspace", "bt2020nc"], work)
    sdr = _make_claim(
        inbox / "Claimed Sdr (1988)" / "Claimed Sdr (1988).mkv",
        "colorprim=bt709:transfer=bt709:colormatrix=bt709",
        ["-color_primaries", "bt709", "-color_trc", "bt709",
         "-colorspace", "bt709"], work)

    before = {
        "hdr_md5": _video_md5(hdr), "sdr_md5": _video_md5(sdr),
        "hdr_fields": _dv_fields(hdr), "sdr_fields": _dv_fields(sdr),
        "hdr_rpu": _has_rpu(hdr, work), "sdr_rpu": _has_rpu(sdr, work),
    }
    log = work / "ingest.log"
    status = _improve(inbox, ram_root, log)
    return {
        "work": work, "inbox": inbox, "ram_root": ram_root, "log": log,
        "status": status, "before": before, "hdr": hdr, "sdr": sdr,
        "hdr_old": hdr.with_name("Claimed Hdr10 (2020) (old).mkv"),
        "sdr_old": sdr.with_name("Claimed Sdr (1988) (old).mkv"),
    }


@needs_x265
class TestTheFixturesAreWhatAnIngestedLibraryIsFullOf:
    """Asserted as the WHOLE mediainfo line, against the exact strings real
    files produce: a hand-written expectation is the only thing that catches the
    injector writing a record that parses but does not say profile 7
    BL+EL+RPU."""

    def test_the_hdr10_one(self, library):
        assert library["before"]["hdr_fields"] == (
            "HDR_Format=Dolby Vision / SMPTE ST 2086 profile=dvhe.07 / "
            " settings=BL+EL+RPU /  compat=Blu-ray / HDR10 transfer=PQ"
            " primaries=BT.2020 maxcll=600"
            " md=min: 0.0500 cd/m2, max: 1000 cd/m2 fps=24000/1001")

    def test_the_sdr_one(self, library):
        assert library["before"]["sdr_fields"] == (
            "HDR_Format=Dolby Vision profile=dvhe.07 settings=BL+EL+RPU"
            " compat=Blu-ray transfer=BT.709 primaries=BT.709 maxcll=-"
            " md=- fps=24000/1001")

    def test_and_the_claim_really_is_false(self, library):
        """Or this tests the CONVERSION path by accident."""
        assert not library["before"]["hdr_rpu"]
        assert not library["before"]["sdr_rpu"]


@needs_x265
class TestTheImprovePhase:
    def test_it_reports_success(self, library):
        """Its status propagates out of the walk and would abort the whole
        ingest."""
        assert library["status"] == 0

    def test_both_films_are_classified_as_false_claims(self, library):
        text = library["log"].read_text()
        assert text.count("is claimed by the container but the video") == 2
        assert "Normalising Dolby Vision profile 7" not in text

    def test_the_two_outcomes_are_named_per_film(self, library):
        """With the exact frame-rate fraction read from mediainfo rather than
        the rounded decimal it also reports."""
        text = library["log"].read_text()
        assert text.count(
            "the copy reports HDR only (PQ, 24000/1001fps)") == 1
        assert text.count(
            "the copy reports no HDR at all (BT.709, 24000/1001fps)") == 1

    def test_both_copies_were_accepted(self, library):
        text = library["log"].read_text()
        assert text.count("Improved copy written") == 2
        assert "left original untouched" not in text

    def test_the_originals_were_kept_and_the_copies_took_their_place(
            self, library):
        assert library["hdr_old"].is_file()
        assert library["sdr_old"].is_file()
        assert library["hdr"].is_file()
        assert library["sdr"].is_file()

    def test_no_scratch_stream_or_remux_survives(self, library):
        assert [p for p in library["ram_root"].rglob("*") if p.is_file()] == []


@needs_x265
class TestWhatTheCopiesReport:
    """The claim being gone is only half of it; the other half is that
    everything the video legitimately had is still there."""

    def test_the_hdr10_copy_is_now_plain_hdr10(self, library):
        """compat drops from "Blu-ray / HDR10" to "HDR10": the first entry was
        the Dolby Vision base-layer compatibility id out of the dvcC and goes
        with it, the second is what the HDR10 the file really has is compatible
        with, and stays."""
        assert _dv_fields(library["hdr"]) == (
            "HDR_Format=SMPTE ST 2086 profile=- settings=- compat=HDR10"
            " transfer=PQ primaries=BT.2020 maxcll=600"
            " md=min: 0.0500 cd/m2, max: 1000 cd/m2 fps=24000/1001")

    def test_the_sdr_copy_claims_nothing_at_all(self, library):
        assert _dv_fields(library["sdr"]) == (
            "HDR_Format=- profile=- settings=- compat=- transfer=BT.709"
            " primaries=BT.709 maxcll=- md=- fps=24000/1001")

    def test_the_configuration_record_itself_is_gone(self, library):
        assert _dovi_records(library["hdr_old"]) == 1
        assert _dovi_records(library["hdr"]) == 0
        assert _dovi_records(library["sdr"]) == 0

    def test_nothing_was_re_encoded(self, library):
        """The claim lived in the container, so dropping it must not have cost a
        single pixel."""
        assert _video_md5(library["hdr"]) == library["before"]["hdr_md5"]
        assert _video_md5(library["sdr"]) == library["before"]["sdr_md5"]

    def test_the_non_video_track_came_along_in_order(self, library):
        for film in (library["hdr"], library["sdr"]):
            done = _run(["mkvmerge", "-J", str(film)], text=True)
            kinds = [t["type"] for t in json.loads(done.stdout)["tracks"]]
            assert kinds == ["video", "audio"]


@needs_x265
class TestTheFilmItReplacedIsStillThere:
    """The promise the remux is built around, asserted on the DECODED FRAMES
    rather than the container bytes. The one edit the phase may make to the
    original is the Dolby Vision LEVEL correction, which runs on the source
    before the remux precisely so a film needing nothing else still gets it -
    two bytes of container metadata, and never a frame."""

    def test_the_frames_are_what_they_were(self, library):
        assert _video_md5(library["hdr_old"]) == library["before"]["hdr_md5"]
        assert _video_md5(library["sdr_old"]) == library["before"]["sdr_md5"]

    def test_and_it_still_reports_the_claim_it_came_in_with(self, library):
        assert _dv_fields(library["hdr_old"]) == library["before"]["hdr_fields"]
        assert _dv_fields(library["sdr_old"]) == library["before"]["sdr_fields"]


@needs_x265
class TestARerunIsANoOp:
    """The "(old)" backup next to a film is what tells the phase this folder is
    done, so a second pass must not remux the cleaned copy again - which would
    leave a "(old) (old)" and a copy of a copy."""

    def test_it_changes_nothing(self, library):
        before = _tree(library["inbox"])
        second = library["work"] / "rerun.log"
        _improve(library["inbox"], library["ram_root"], second)
        assert _tree(library["inbox"]) == before
        assert "Improving main movie" not in second.read_text()
        assert _video_md5(library["hdr"]) == library["before"]["hdr_md5"]
