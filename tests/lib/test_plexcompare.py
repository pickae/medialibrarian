"""Tests for medialib.lib.plexcompare - two copies of a film weighed against
each other, told apart as versions, and their soundtracks put in sync.

The profiles are written out here rather than read from files, apart from the
offset, which is measured on real audio ffmpeg makes."""

import shutil
import subprocess

import pytest

from medialib.lib import plexcompare as pc


def _audio(language="eng", channels=6, lossless=False, objects=False, name="",
           index=0):
    return pc.Audio(index, language, channels, lossless, objects, name)


def _profile(seconds=6000.0, width=1920, height=800, hdr="SDR", audio=None,
             commentaries=(), others=(), chapters=0, named=False, edition="",
             path="film.mkv"):
    return pc.Profile(path, seconds, width, height, hdr,
                      tuple(audio if audio is not None else (_audio(),)),
                      tuple(commentaries), tuple(others), chapters, named, edition)


@pytest.mark.pure
class TestTheVerdict:

    def test_two_alike_are_the_same(self):
        assert pc.compare(_profile(), _profile()).verdict == "same"

    def test_better_on_one_count_and_worse_on_none_dominates(self):
        found = pc.compare(_profile(width=3840, height=1600, hdr="Dolby Vision"),
                           _profile())
        assert found.verdict == "new"
        assert found.new_better == ["resolution: 2160p vs 1080p",
                                    "HDR: Dolby Vision vs SDR"]
        assert found.old_better == []

    def test_the_typical_mixed_one(self):
        """A 4K release without the commentary and chapters the old disc
        had."""
        found = pc.compare(
            _profile(width=3840, height=2160),
            _profile(commentaries=[_audio(channels=2, name="Commentary")],
                     chapters=12, named=True))
        assert found.verdict == "mixed"
        assert found.new_better == ["resolution: 2160p vs 1080p"]
        assert found.old_better == [
            "commentaries: 1 (Commentary) vs none",
            "chapters: 12 named vs none"]

    def test_a_language_only_one_has(self):
        found = pc.compare(_profile(), _profile(audio=[
            _audio(), _audio("ger", 2, index=1)]))
        assert found.verdict == "old"
        assert found.old_better == [
            "ger audio (stereo), which the new file lacks"]

    def test_a_language_is_weighed_by_its_best_track(self):
        found = pc.compare(
            _profile(audio=[_audio(channels=8, objects=True), _audio(channels=2)]),
            _profile(audio=[_audio(channels=6, lossless=True)]))
        assert found.new_better == ["eng audio: 7.1 Atmos/DTS:X vs 5.1 lossless"]

    def test_bonus_audio_is_weighed_by_kind_not_by_count(self):
        found = pc.compare(
            _profile(others=[_audio(channels=2, name="Isolated Score")]),
            _profile(others=[_audio(channels=2, name="Interview with the director")]))
        assert found.verdict == "mixed"
        assert found.new_better == ["an isolated score track, which the existing "
                                    "file lacks"]
        assert found.old_better == ["an interview track, which the new file lacks"]

    def test_named_chapters_beat_numbered_ones(self):
        found = pc.compare(_profile(chapters=12, named=True),
                           _profile(chapters=12))
        assert found.new_better == ["chapters: 12 named vs 12 numbered"]


@pytest.mark.pure
class TestVersions:

    def test_a_minute_apart_is_another_cut(self):
        found = pc.compare(_profile(seconds=6000), _profile(seconds=6063))
        assert found.verdict == "different"
        assert found.versions == ["the new file runs 1m03s shorter"]

    def test_a_few_seconds_apart_is_the_same_cut(self):
        assert pc.compare(_profile(seconds=6000),
                          _profile(seconds=6035)).verdict == "same"

    def test_editions_that_differ(self):
        found = pc.compare(_profile(edition="Director's Cut"), _profile())
        assert found.verdict == "different"
        assert found.versions == ["editions differ: Director's Cut vs no edition"]

    def test_audio_that_lines_up_is_one_cut_whatever_the_lengths(self):
        """A copy with a minute of logos and trailers in front still lines up
        with the other: the measurement settles it."""
        found = pc.compare(_profile(seconds=6000), _profile(seconds=6063),
                           pc.Timing(-63.0, 1.0))
        assert found.verdict == "same" and found.versions == []

    def test_lined_up_audio_does_not_unite_two_editions(self):
        found = pc.compare(_profile(edition="Director's Cut"), _profile(),
                           pc.Timing(0.0, 1.0))
        assert found.verdict == "different"

    def test_a_pal_transfer_is_worth_measuring(self):
        assert pc.worth_measuring(_profile(seconds=6000 / pc.PAL_SPEEDUP),
                                  _profile(seconds=6000))
        assert not pc.worth_measuring(_profile(seconds=6000),
                                      _profile(seconds=6600))
        assert not pc.worth_measuring(_profile(edition="Extended"), _profile())

    def test_a_pal_transfer_is_named_as_one(self):
        found = pc.compare(_profile(seconds=6000 / pc.PAL_SPEEDUP),
                           _profile(seconds=6000))
        assert found.verdict == "different"
        assert "PAL speed-up" in found.versions[0]


@pytest.mark.pure
@pytest.mark.parametrize("width, height, tier", [
    (3840, 1600, 2160), (1920, 800, 1080), (1440, 1080, 1080),
    (1280, 536, 720), (720, 576, 480)])
def test_the_tier_counts_the_width(width, height, tier):
    assert pc.tier(width, height) == tier


@pytest.mark.pure
class TestTheOffset:

    def test_one_offset_where_every_point_agrees(self):
        offset = pc.Offset((1.0, 2.0, 3.0), (5010, 5000, 5030))
        assert offset.consistent and offset.ms_median == 5010

    def test_offsets_that_drift_are_not_one(self):
        assert not pc.Offset((1.0, 2.0, 3.0), (5000, 5000, 7000)).consistent

    def test_one_point_alone_is_not_enough(self):
        assert not pc.Offset((1.0, 2.0, 3.0), (5000, None, None)).consistent

    def test_a_steady_offset_is_a_line_without_a_slope(self):
        timing = pc.Offset((600.0, 1800.0, 3000.0, 4200.0, 5400.0),
                           (5010, 5000, 5030, 5000, 5020)).timing()
        assert timing is not None and timing.steady(6000)
        assert abs(timing.start - 5.012) < 0.01

    def test_another_frame_rate_is_a_line_with_one(self):
        """24 against 23.976 fps: the offset shrinks a millisecond a second."""
        at = (595.0, 1786.0, 2977.0, 4168.0, 5359.0)
        timing = pc.Offset(at, tuple(round(7917 - 0.999 * t) for t in at)).timing()
        assert timing is not None and not timing.steady(5955)
        assert abs(timing.speed - 1 / 1.000999) < 1e-6
        assert "23.976 fps against the new file's 24" in timing.describe(5955)
        # The old file's moment found where the measurement found it.
        assert abs(timing.to_new(2977 - 4.952) - 2977) < 0.01

    def test_a_cut_inside_the_film_is_no_line(self):
        assert pc.Offset((600.0, 1800.0, 3000.0, 4200.0, 5400.0),
                         (5000, 5000, 5000, 9000, 9000)).timing() is None

    def test_two_points_are_no_line(self):
        assert pc.Offset((1.0, 2.0, 3.0), (5000, 5000, None)).timing() is None

    def test_the_lag_is_found_to_the_sample(self):
        numpy = pytest.importorskip("numpy")
        rng = numpy.random.default_rng(1)
        haystack = rng.standard_normal(4000)
        lag, score = pc.best_lag(haystack[1234:1834], haystack)
        assert lag == 1234 and score > 0.99

    def test_the_pair_shares_a_language_and_a_layout(self):
        new = _profile(audio=[_audio("ger", 2), _audio("eng", 6, index=1)])
        old = _profile(audio=[_audio("eng", 2), _audio("eng", 6, index=1)])
        assert pc.audio_pair(new, old) == (new.audio[1], old.audio[1])


@pytest.mark.media
@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")
def test_a_logo_in_front_is_measured(tmp_path):
    """The same noise twice, one copy with three and a half seconds of silence
    in front of it: the old file's tracks are that much early."""
    pytest.importorskip("numpy")
    old = tmp_path / "old.mka"
    new = tmp_path / "new.mka"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i",
                    "anoisesrc=d=150:c=pink:r=48000:seed=7,"
                    "volume='0.2+0.8*abs(sin(t*1.3))':eval=frame",
                    "-c:a", "flac", str(old)], check=True, stdin=subprocess.DEVNULL)
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(old), "-af",
                    "adelay=3500:all=1", "-c:a", "flac", str(new)],
                   check=True, stdin=subprocess.DEVNULL)
    offset = pc.measure_offset(_profile(seconds=153.5, path=str(new)),
                               _profile(seconds=150, path=str(old)),
                               (_audio(), _audio()))
    assert offset.consistent
    assert abs(offset.ms_median - 3500) <= 2


@pytest.mark.media
@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")
def test_another_speed_is_measured(tmp_path):
    """The same noise played a thousandth faster, the way a 24 fps copy of a
    23.976 fps film is: the line's slope is that thousandth."""
    pytest.importorskip("numpy")
    old = tmp_path / "old.mka"
    new = tmp_path / "new.mka"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i",
                    "anoisesrc=d=600:c=pink:r=48000:seed=7,"
                    "volume='0.2+0.8*abs(sin(t*1.3))':eval=frame",
                    "-c:a", "flac", str(old)], check=True, stdin=subprocess.DEVNULL)
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(old), "-af",
                    "asetrate=48048,aresample=48000,adelay=2000:all=1",
                    "-c:a", "flac", str(new)], check=True, stdin=subprocess.DEVNULL)
    timing = pc.measure_offset(_profile(seconds=601.4, path=str(new)),
                               _profile(seconds=600, path=str(old)),
                               (_audio(), _audio())).timing()
    assert timing is not None
    assert abs(timing.speed - 48000 / 48048) < 2e-5
    assert abs(timing.start - 2.0) < 0.02
