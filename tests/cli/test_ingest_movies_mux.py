"""The remux into Matroska: which files it picks up, what it leaves behind when
the mux does not work, and the tagging pass declining what is still not one.

The container is read from the file's first bytes rather than believed from its
extension, because a release that ships an MP4 under a ".mkv" name is otherwise
carried through the whole ingest as a Matroska.
"""

import os

import pytest

from medialib.cli import ingest_movies as im

pytestmark = pytest.mark.stubbed

MATROSKA = im.MATROSKA_MAGIC + b"the rest of a film"
MP4 = b"\x00\x00\x00\x20ftypisom" + b"the rest of a film"


def _write(path, content):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as handle:
        handle.write(content)


@pytest.fixture
def muxer(monkeypatch, tmp_path):
    """One ingest tree, a scratch of its own, and a stubbed mkvmerge that
    reports the calls it was given."""
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    root = tmp_path / "movies"
    root.mkdir()
    calls = []
    released = []

    monkeypatch.setattr(im.ramscratch, "ram_scratch_dir_for",
                        lambda size, label, parent="": (str(scratch), 0, 0))
    monkeypatch.setattr(im.ramscratch, "add_exit_cleanup", lambda paths: 0)
    monkeypatch.setattr(im.ramscratch, "release_exit_cleanup",
                        lambda paths: released.extend(paths))

    class Muxer:
        status = 0
        writes = MATROSKA

        def __init__(self):
            self.root = str(root)
            self.calls = calls
            self.released = released

        def film(self, name, content):
            path = os.path.join(self.root, "A Film (2020)", name)
            _write(path, content)
            return path

        def names(self):
            return sorted(os.listdir(os.path.join(self.root, "A Film (2020)")))

        def run(self):
            im.mkv_mux(self.root)

    state = Muxer()

    def fake_run(argv):
        calls.append(argv)
        if state.writes is not None:
            _write(argv[argv.index("-o") + 1], state.writes)
        return state.status

    monkeypatch.setattr(im, "_run", fake_run)
    return state


class TestWhichFilesAreMuxed:
    def test_an_mp4_named_mkv_is_remuxed(self, muxer):
        """The extension says Matroska and the bytes say MP4."""
        muxer.film("A Film (2020).mkv", MP4)
        muxer.run()
        assert len(muxer.calls) == 1

    def test_a_real_matroska_is_left_alone(self, muxer):
        muxer.film("A Film (2020).mkv", MATROSKA)
        muxer.run()
        assert muxer.calls == []

    def test_an_mp4_is_remuxed(self, muxer):
        muxer.film("A Film (2020).mp4", MP4)
        muxer.run()
        assert len(muxer.calls) == 1

    def test_a_non_video_is_not_opened(self, muxer):
        muxer.film("A Film (2020).en.srt", b"1\n")
        muxer.run()
        assert muxer.calls == []


class TestTheResult:
    def test_a_muxed_mp4_becomes_the_mkv(self, muxer):
        muxer.film("A Film (2020).mp4", MP4)
        muxer.run()
        assert muxer.names() == ["A Film (2020).mkv"]

    def test_the_muxed_file_is_the_matroska_mkvmerge_wrote(self, muxer):
        muxer.film("A Film (2020).mp4", MP4)
        muxer.run()
        with open(os.path.join(muxer.root, "A Film (2020)",
                               "A Film (2020).mkv"), "rb") as handle:
            assert handle.read() == MATROSKA

    def test_an_mp4_named_mkv_keeps_its_name(self, muxer):
        muxer.film("A Film (2020).mkv", MP4)
        muxer.run()
        assert muxer.names() == ["A Film (2020).mkv"]

    def test_a_warning_still_counts_as_a_mux(self, muxer):
        """mkvmerge exits 1 having written the file."""
        muxer.status = 1
        muxer.film("A Film (2020).mp4", MP4)
        muxer.run()
        assert muxer.names() == ["A Film (2020).mkv"]

    def test_the_scratch_is_handed_back(self, muxer):
        muxer.film("A Film (2020).mp4", MP4)
        muxer.run()
        assert muxer.released


class TestAFailedMux:
    def test_the_source_keeps_its_own_extension(self, muxer):
        """A source renamed ahead of the mux would leave an MP4 called ".mkv"
        behind, which is the state this pass exists to clear."""
        muxer.status = 2
        muxer.writes = None
        muxer.film("A Film (2020).mp4", MP4)
        muxer.run()
        assert muxer.names() == ["A Film (2020).mp4"]

    def test_an_output_that_is_not_matroska_is_refused(self, muxer):
        muxer.writes = MP4
        muxer.film("A Film (2020).mp4", MP4)
        muxer.run()
        assert muxer.names() == ["A Film (2020).mp4"]

    def test_the_scratch_is_handed_back_anyway(self, muxer):
        muxer.status = 2
        muxer.writes = None
        muxer.film("A Film (2020).mp4", MP4)
        muxer.run()
        assert muxer.released


class TestTheScratch:
    def test_it_is_sized_against_the_film(self, muxer, monkeypatch):
        """A remux writes the whole film out again, so the scratch is asked for
        that many bytes rather than taken on trust."""
        asked = []
        monkeypatch.setattr(im.ramscratch, "ram_scratch_dir_for",
                            lambda size, label, parent="":
                            (asked.append((size, parent)) or
                             (str(muxer.root), 0, 0)))
        path = muxer.film("A Film (2020).mp4", MP4)
        muxer.run()
        assert asked == [(str(len(MP4)), os.path.dirname(path))]

    def test_a_scratch_that_cannot_be_made_leaves_the_film_alone(self, muxer,
                                                                 monkeypatch):
        monkeypatch.setattr(im.ramscratch, "ram_scratch_dir_for",
                            lambda size, label, parent="": ("", 0, 1))
        muxer.film("A Film (2020).mp4", MP4)
        muxer.run()
        assert muxer.calls == []
        assert muxer.names() == ["A Film (2020).mp4"]


class TestTaggingWhatIsNotMatroska:
    """A mux that failed leaves the film under its own name, which for such a
    release is already ".mkv"."""

    @pytest.fixture
    def tagged(self, monkeypatch, tmp_path):
        calls = []
        monkeypatch.setattr(im, "_run", lambda argv: calls.append(argv) or 0)
        monkeypatch.setattr(im, "_run_capture", lambda argv: "")
        monkeypatch.setattr(im, "_identify", lambda path: [])

        def run(content):
            _write(str(tmp_path / "A Film (2020)" / "A Film (2020).mkv"),
                   content)
            im.update_tags(str(tmp_path))
            return calls

        return run

    def test_an_mp4_named_mkv_is_not_handed_to_mkvpropedit(self, tagged):
        assert tagged(MP4) == []

    def test_a_real_matroska_still_is(self, tagged):
        assert tagged(MATROSKA)
