"""Tests for medialib.lib.chunkdecision - whether a run cuts its long files.

The cases are the shapes a real run takes, each asked the way the run asks it:
file sizes and extensions only. What is asserted is the decision and not the
figures behind it, since the figures are measurements and move when they are
measured again; a decision that flips when they do would be one the census or
the costs got wrong.
"""

import time

import pytest

from medialib.lib import chunkdecision, enums

pytestmark = pytest.mark.pure

GB = 1_000_000_000
MB = 1_000_000


def _decide(tracks, workers=32, threads=None, mono=False, bitrate=46,
            codec="opus"):
    return chunkdecision.decide(
        tracks, workers, threads or workers, mono, bitrate,
        50000 if mono else 90000,
        lambda ext: ext in enums.VIDEO_EXTENSIONS,
        lambda ext: ext in enums.ALWAYS_TRANSCODE_EXTENSIONS, codec)


class TestNothingToDecide:
    def test_an_empty_run_cuts_nothing(self):
        assert _decide([]).chunked == frozenset()

    def test_one_worker_never_cuts(self):
        """One chunk per core is one chunk: there is nothing to cut into."""
        assert _decide([("m4b", 2 * GB)], workers=1).chunked == frozenset()


class TestFewLongFiles:
    def test_a_lone_long_book_is_cut(self):
        """Sixty hours on one core of thirty-two is the whole machine waiting
        on one encode."""
        decision = _decide([("m4b", 2 * GB)])
        assert decision.chunked == {0}
        assert decision.expected < decision.whole / 4

    def test_as_many_long_books_as_cores_are_cut_on_a_big_machine(self):
        tracks = [("m4b", 1 * GB)] * 4
        assert _decide(tracks, workers=32).chunked == {0, 1, 2, 3}

    def test_a_pool_narrower_than_the_machine_still_cuts(self):
        """Four workers on thirty-two threads each run faster than a full
        house would, and the balanced queue that chunking is weighed against
        has to be measured at that pace too."""
        tracks = [("m4b", 5 * GB)] * 2
        assert _decide(tracks, workers=4, threads=32).chunked == {0, 1}

    def test_but_not_on_a_quad_core(self):
        """Four books on four cores already keep every core busy to the end."""
        tracks = [("m4b", 1 * GB)] * 4
        assert _decide(tracks, workers=4).chunked == frozenset()

    def test_a_long_book_among_short_tracks_is_the_one_cut(self):
        tracks = [("m4b", 2 * GB)] + [("m4b", 30 * MB)] * 200
        assert _decide(tracks).chunked == {0}


class TestManyFiles:
    def test_a_backlog_that_dwarfs_its_longest_file_cuts_nothing(self):
        """The longest file ends long before the backlog does."""
        tracks = [("m4b", 300 * MB)] + [("mp3", 40 * MB)] * 20000
        assert _decide(tracks).chunked == frozenset()

    def test_long_books_that_outnumber_the_cores_are_not_cut(self):
        """The longest starts at once and finishes while the eight books that
        found no core at first are still encoding: the end of the run is not
        waiting on it alone."""
        assert _decide([("m4b", 300 * MB)] * 40).chunked == frozenset()

    def test_short_tracks_alone_are_never_cut(self):
        tracks = [("m4a", 20 * MB)] * 500
        assert _decide(tracks).chunked == frozenset()

    def test_a_large_queue_decides_quickly(self):
        """The decision is made before the queue starts, so it has to be
        cheap next to the run, not just correct."""
        tracks = [("mp3", (i % 97 + 1) * MB) for i in range(100_000)]
        tracks += [("m4b", 3 * GB)]
        start = time.monotonic()
        _decide(tracks)
        assert time.monotonic() - start < 5


class TestWhatTheBytesSay:
    def test_a_tree_already_in_opus_is_not_cut(self):
        """Nearly every Opus file is under the threshold and is kept as it is,
        so however long one is, leaving it whole costs next to nothing."""
        tracks = [("opus", 2 * GB)] + [("opus", 30 * MB)] * 100
        assert _decide(tracks).chunked == frozenset()

    def test_the_same_bytes_in_a_format_that_is_always_encoded_are(self):
        tracks = [("m4b", 2 * GB)] + [("m4b", 30 * MB)] * 100
        assert _decide(tracks).chunked == {0}

    def test_an_empty_file_beside_a_long_book_is_no_obstacle(self):
        """An empty file holds nothing a cutoff could fall below, so the
        search that walks the cutoff down has to stop short of it."""
        assert _decide([("m4b", 0), ("m4b", 300 * MB)]).chunked == {1}

    def test_a_run_of_nothing_but_empty_files_cuts_nothing(self):
        assert _decide([("m4b", 0)] * 3).chunked == frozenset()

    def test_the_decision_is_the_same_every_time(self):
        tracks = [("m4b", 900 * MB), ("mp3", 700 * MB)] + [
            ("mp3", 60 * MB)] * 300
        assert _decide(tracks) == _decide(tracks)

    @pytest.mark.parametrize("extension", enums.AUDIO_EXTENSIONS)
    def test_every_audio_extension_has_a_census(self, extension):
        """An extension outside the census is weighed as a guess."""
        assert chunkdecision.census_for(extension)
