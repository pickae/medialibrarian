"""The white box for medialib/lib/censusimages.py.

What is pinned: the argv the probe is handed - one ``-ping`` per row and a second,
decoding call ONLY under -a - the row's columns in order, and that a file no image
reader can make sense of is a skip reason rather than a row of empty columns.
"""

import os
import shutil
from types import SimpleNamespace

import pytest

from medialib.lib import censusimages as ci
from tests import blackbox

pytestmark = pytest.mark.stubbed

_PLUMBING = ("bash", "awk", "cat", "base64", "stat")

_CENSUS_ENV = ("CENSUS_SEP", "CENSUS_ADEQUACY")


@pytest.fixture()
def probe(tmp_path, monkeypatch):
    """A PATH holding only an ``identify`` stub, whose canned answer a test
    sets."""
    bin_dir = tmp_path / "bin"
    out_dir = tmp_path / "stubout"
    state_dir = tmp_path / "stubstate"
    for directory in (bin_dir, out_dir, state_dir):
        directory.mkdir()
    for tool in _PLUMBING:
        (bin_dir / tool).symlink_to(shutil.which(tool))
    record = tmp_path / "calls"

    def says(answer, rc="0"):
        target = bin_dir / "identify"
        shutil.copyfile(blackbox.TOOLSTUB, str(target))
        os.chmod(str(target), 0o755)
        (out_dir / "identify").write_text(answer)
        (out_dir / "identify.rc").write_text(rc + "\n")

    def calls():
        if not record.exists():
            return []
        return [line.rstrip("\n").split("\t")[1:]
                for line in record.read_text().splitlines() if line]

    def image(name="page.jpg", size=400000):
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
    return SimpleNamespace(says=says, calls=calls, image=image,
                           tmp_path=tmp_path)


class TestTheRow:
    def test_the_columns_are_path_size_adequacy_resolution_codec(self, probe):
        path = probe.image(size=400000)
        probe.says("JPEG|1988|3056|sRGB\n")
        row, reason = ci.census_image_row(path)
        assert reason is None
        assert row.split(",") == [path, "400000", "", "1988x3056", "jpeg"]

    def test_the_codec_is_the_family_and_not_the_suffix(self, probe):
        """The suffix is the claim this column exists to check: a .jpg holding a
        PNG is a thing a library contains."""
        path = probe.image("scan.jpg")
        probe.says("PNG|800|600|sRGB\n")
        row, _reason = ci.census_image_row(path)
        assert row.split(",")[4] == "png"

    def test_a_multi_frame_file_is_read_by_its_first_frame(self, probe):
        """An animated GIF, a scanned TIFF of pages, a JPEG carrying a thumbnail
        - identify prints one line each and the first is the picture."""
        path = probe.image("anim.gif")
        probe.says("GIF|500|400|sRGB\nGIF|500|400|sRGB\nGIF|500|400|sRGB\n")
        row, _reason = ci.census_image_row(path)
        assert row.split(",")[3] == "500x400"

    @pytest.mark.parametrize("answer,rc", [
        ("", "0"), ("", "1"), ("identify: no decode delegate\n", "1"),
        ("JPEG|abc|3056|sRGB\n", "0")])
    def test_a_file_no_reader_understands_is_a_skip_reason(self, probe, answer,
                                                           rc):
        path = probe.image()
        probe.says(answer, rc=rc)
        row, reason = ci.census_image_row(path)
        assert row is None
        assert "not an image" in reason


class TestTheProbes:
    def test_one_ping_and_nothing_else_without_the_flag(self, probe):
        """The cheap half: a read of the header, no decoding, which is what makes
        a census of forty thousand pages finish."""
        path = probe.image()
        probe.says("JPEG|1988|3056|sRGB\n")
        ci.census_image_row(path)
        assert probe.calls() == [
            ["identify", "-ping", "-format", "%m|%w|%h|%[colorspace]", path]]

    def test_and_a_second_decoding_call_with_it(self, probe, monkeypatch):
        """The expensive half, and the whole reason -a exists: what is IN the
        picture is not in its header, and counting its colours means decoding all
        of it."""
        monkeypatch.setenv("CENSUS_ADEQUACY", "1")
        path = probe.image()
        probe.says("JPEG|1988|3056|sRGB\n")
        ci.census_image_row(path)
        assert probe.calls()[1] == ["identify", "-format", "%[type]|%k", path]


class TestTheAdequacyColumn:
    @pytest.fixture()
    def judging(self, monkeypatch):
        monkeypatch.setenv("CENSUS_ADEQUACY", "1")

    def test_a_thin_page_is_starved(self, probe, judging):
        # 6.1 megapixels in 150 kB is a quarter of a bit per pixel
        path = probe.image(size=150000)
        probe.says("JPEG|1988|3056|sRGB\n")
        row, _reason = ci.census_image_row(path)
        assert row.split(",")[2] == "starved"

    def test_and_a_full_one_is_generous(self, probe, judging):
        path = probe.image(size=2000000)
        probe.says("JPEG|1988|3056|sRGB\n")
        row, _reason = ci.census_image_row(path)
        assert row.split(",")[2] == "generous"

    def test_a_lossless_page_is_never_starved(self, probe, judging):
        path = probe.image("page.png", size=1000)
        probe.says("PNG|1988|3056|sRGB\n")
        row, _reason = ci.census_image_row(path)
        assert row.split(",")[2] == "adequate"
