"""The white box for medialib.lib.dolbyvisionlevel - the byte surgery that writes
a dv_level into a Matroska file's Dolby Vision configuration record.

The correction itself only ever hands the file over, so what happens to the bytes
is checked here and nowhere else.

The Matroska headers are synthetic so that the CRC-32 variant exists at all -
whether a real fixture would carry one is the muxer's choice, and both shapes
have to be exercised.
"""

import zlib

import pytest

from medialib.lib import dolbyvisionlevel as helper

pytestmark = pytest.mark.fs


def _size(n):
    """Always the 4-byte VINT form, so one code path builds every element."""
    return bytes([0x10 | (n >> 24) & 0x0F, (n >> 16) & 0xFF,
                  (n >> 8) & 0xFF, n & 0xFF])


def _elem(eid, payload):
    return eid + _size(len(payload)) + payload


def _record(profile, level):
    word = (profile << 9) | (level << 3) | 0b101      # rpu=1, el=0, bl=1
    return bytes([1, 0, word >> 8, word & 0xFF, 0x10])


def _make_ebml(path, profile, level, with_crc=False, count=1):
    blocks = b"".join(
        _elem(b"\x41\xe4",                                 # BlockAdditionMapping
              _elem(b"\x41\xa4", b"dvvC") +                # BlockAddIDName
              _elem(b"\x41\xe7", b"\x01") +                # BlockAddIDType
              _elem(b"\x41\xed", _record(profile, level))) # BlockAddIDExtraData
        for _ in range(count))
    tracks_body = _elem(b"\xae", _elem(b"\xd7", b"\x01") + blocks)
    if with_crc:
        crc = zlib.crc32(tracks_body) & 0xFFFFFFFF
        tracks_body = _elem(b"\xbf", crc.to_bytes(4, "little")) + tracks_body
    segment = _elem(b"\x18\x53\x80\x67",
                    _elem(b"\x16\x54\xae\x6b", tracks_body))
    # A little trailing bulk, so a helper that rewrote "the header" instead of
    # the bytes it changed would be caught by the file's tail changing.
    path.write_bytes(segment + b"\xff" * 4096)
    return path


def _read_record(path):
    """(profile, level, "<rpu><el><bl>") as the file now declares them."""
    buf = path.read_bytes()
    at = buf.index(b"\x41\xed") + 6            # past the id and its 4-byte size
    word = (buf[at + 2] << 8) | buf[at + 3]
    return (word >> 9, (word >> 3) & 0x3F,
            "%d%d%d" % ((word >> 2) & 1, (word >> 1) & 1, word & 1))


def _tracks_crc(path):
    """"yes" / "no" / "none" - whether the Tracks CRC-32 still checks out."""
    buf = path.read_bytes()
    at = buf.index(b"\x16\x54\xae\x6b") + 4
    length = int.from_bytes(buf[at:at + 4], "big") & 0x0FFFFFFF
    body = buf[at + 4:at + 4 + length]
    if body[:1] != b"\xbf":
        return "none"
    stored = int.from_bytes(body[5:9], "little")
    return "yes" if stored == (zlib.crc32(body[9:]) & 0xFFFFFFFF) else "no"


class TestNoCrc:
    """What mkvmerge writes: no CRC-32 over the element."""

    def test_level_lowered_profile_and_flags_untouched(self, tmp_path):
        movie = _make_ebml(tmp_path / "plain.mkv", 8, 10)
        assert _read_record(movie) == (8, 10, "101")
        assert helper.set_level(str(movie), 6) is True
        assert _read_record(movie) == (8, 6, "101")
        assert _tracks_crc(movie) == "none"

    def test_exactly_one_byte_differs(self, tmp_path):
        before = _make_ebml(tmp_path / "before.mkv", 8, 10).read_bytes()
        after = _make_ebml(tmp_path / "after.mkv", 8, 10)
        helper.set_level(str(after), 6)
        now = after.read_bytes()
        assert len(now) == len(before)
        differing = sum(1 for a, b in zip(before, now, strict=True) if a != b)
        assert differing == 1

    def test_another_profiles_level_is_the_same_field(self, tmp_path):
        movie = _make_ebml(tmp_path / "p5.mkv", 5, 9)
        assert helper.set_level(str(movie), 4) is True
        assert _read_record(movie) == (5, 4, "101")


class TestCrc:
    """What ffmpeg writes: a CRC-32 over the master element holding the
    record. Correcting the level without recomputing it would leave a
    knowingly corrupt checksum behind."""

    def test_crc_is_recomputed(self, tmp_path):
        movie = _make_ebml(tmp_path / "crc.mkv", 8, 10, with_crc=True)
        assert _tracks_crc(movie) == "yes"
        assert helper.set_level(str(movie), 6) is True
        assert _read_record(movie) == (8, 6, "101")
        assert _tracks_crc(movie) == "yes"

    def test_a_stale_crc_would_be_visible(self, tmp_path):
        """The check itself has teeth: a level written without the recompute
        leaves the checksum reading "no"."""
        movie = _make_ebml(tmp_path / "stale.mkv", 8, 10, with_crc=True)
        buf = bytearray(movie.read_bytes())
        at = buf.index(b"\x41\xed") + 6
        word = (buf[at + 2] << 8) | buf[at + 3]
        word = (word & ~(0x3F << 3)) | (6 << 3)
        buf[at + 2], buf[at + 3] = word >> 8, word & 0xFF
        movie.write_bytes(bytes(buf))
        assert _tracks_crc(movie) == "no"


class TestRefusals:
    """Nothing to patch, or no way to be sure WHICH record to patch, is exit 1
    and an untouched file - the caller reads that as "leave this one alone"."""

    def test_a_file_with_no_config_record(self, tmp_path):
        movie = tmp_path / "notmkv.mkv"
        movie.write_bytes(b"not a matroska file at all")
        assert helper.set_level(str(movie), 6) is False
        assert helper.main(["dolbyvisionlevel", str(movie), "6"]) == 1
        assert movie.read_bytes() == b"not a matroska file at all"

    def test_two_records_are_refused_not_guessed_between(self, tmp_path):
        movie = _make_ebml(tmp_path / "two.mkv", 8, 10, count=2)
        before = movie.read_bytes()
        assert helper.set_level(str(movie), 6) is False
        assert helper.main(["dolbyvisionlevel", str(movie), "6"]) == 1
        assert movie.read_bytes() == before

    def test_a_block_addition_that_is_not_a_record_is_not_mistaken_for_one(
            self, tmp_path):
        """The record is recognised by its opening dv_version_major=1,
        dv_version_minor=0, not by the element holding it: other block
        addition types exist."""
        movie = _make_ebml(tmp_path / "other.mkv", 8, 10)
        buf = bytearray(movie.read_bytes())
        at = buf.index(b"\x41\xed") + 6
        buf[at] = 9                                  # not dv_version_major 1
        movie.write_bytes(bytes(buf))
        assert helper.set_level(str(movie), 6) is False


class TestMain:
    """The command line: a level out of range is rejected before anything is
    written - the field is six bits, and a level is at least 1."""

    @pytest.mark.parametrize("bad", ["0", "64", "-1", "notanumber"])
    def test_a_level_out_of_range_writes_nothing(self, tmp_path, bad):
        movie = _make_ebml(tmp_path / "range.mkv", 8, 10)
        before = movie.read_bytes()
        assert helper.main(["dolbyvisionlevel", str(movie), bad]) == 2
        assert movie.read_bytes() == before

    def test_the_usage_itself_is_a_refusal(self, tmp_path):
        assert helper.main(["dolbyvisionlevel"]) == 2

    def test_a_correction_exits_zero(self, tmp_path):
        movie = _make_ebml(tmp_path / "ok.mkv", 8, 10)
        assert helper.main(["dolbyvisionlevel", str(movie), "6"]) == 0
        assert _read_record(movie) == (8, 6, "101")

    def test_a_file_that_is_not_there_is_an_error_not_a_crash(self, tmp_path):
        missing = tmp_path / "gone.mkv"
        assert helper.main(["dolbyvisionlevel", str(missing), "6"]) == 1
