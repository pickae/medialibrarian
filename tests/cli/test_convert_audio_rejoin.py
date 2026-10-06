"""convert-audio's re-join: a split file's chunks put back together as its
finished output.

The join itself is ffmpeg's, and the metadata passes after it are the chapter
and cover libraries', so those are stood in for here. What is under test is what
the re-join does with the files either side of them: the output it publishes,
the timestamp it carries, and when the chunks it was made from are let go.
"""

import os

import pytest

from medialib.cli import convert_audio as ca

pytestmark = pytest.mark.fs

# A day after the epoch, so a timestamp the join failed to carry cannot match
# it by being "now".
SOURCE_MTIME = 86_400


@pytest.fixture
def rejoin(tmp_path, monkeypatch):
    """A two-chunk split of in/book.m4b, and a way to re-join it whose ffmpeg
    concat writes <joined> as the staged output."""
    inputs, outputs = tmp_path / "in", tmp_path / "out"
    inputs.mkdir()
    outputs.mkdir()
    source = inputs / "book.m4b"
    source.write_bytes(b"source")
    os.utime(source, (SOURCE_MTIME, SOURCE_MTIME))

    chunk_root = tmp_path / "chunks"
    chunks = ca.segments.chunk_dir_for(str(chunk_root), "book.m4b")
    os.makedirs(chunks)
    for index in range(2):
        with open(os.path.join(chunks, "%04d.opus" % index), "wb") as handle:
            handle.write(b"chunk")

    staging = []

    def scratch(name):
        made = tmp_path / ("%s.%d" % (name, len(staging)))
        made.mkdir()
        staging.append(made)
        return str(made), 0

    monkeypatch.setattr(ca.ramscratch, "ram_scratch_dir", scratch)
    for module, name in ((ca.chapters, "attach_chapters"),
                         (ca.thumbnails, "extract_source_cover"),
                         (ca.thumbnails, "apply_cover")):
        monkeypatch.setattr(module, name, lambda *args, **kwargs: None)

    def run(joined=b"joined", keep=False):
        def concat(argv, **kwargs):
            with open(argv[-1], "wb") as handle:
                handle.write(joined)

        monkeypatch.setattr(ca.toolcapture, "run", concat)
        state = ca.Run(input_dir=str(inputs), output_dir=str(outputs),
                       chunk_root=str(chunk_root), extension="opus",
                       keep=keep, script_dir="", cover_resolution="")
        state.reconcat("book.m4b\t2")
        return outputs / "book.opus"

    run.chunks = chunks
    run.staging = staging
    return run


class TestTheJoinedOutput:

    def test_the_joined_file_is_published_where_the_track_s_output_goes(
            self, rejoin):
        assert rejoin().read_bytes() == b"joined"

    def test_it_carries_the_source_s_timestamp(self, rejoin):
        """The up-to-date check and every later run read the output against
        its source, and the join is the last write the output gets."""
        assert rejoin().stat().st_mtime == SOURCE_MTIME

    def test_nothing_is_left_in_the_staging_area(self, rejoin):
        rejoin()
        assert rejoin.staging and not [path for path in rejoin.staging
                                       if path.exists()]


class TestTheChunksItWasMadeFrom:
    """Freed as soon as the join holds all the audio, so a long book's chunk
    memory is not held through the slower metadata passes - but only then."""

    def test_they_are_released_once_the_join_holds_the_audio(self, rejoin):
        rejoin()
        assert not os.path.exists(rejoin.chunks)

    def test_k_keeps_them(self, rejoin):
        rejoin(keep=True)
        assert sorted(os.listdir(rejoin.chunks)) == ["0000.opus", "0001.opus"]

    def test_a_join_that_produced_nothing_keeps_them_for_inspection(
            self, rejoin):
        rejoin(joined=b"")
        assert sorted(os.listdir(rejoin.chunks)) == ["0000.opus", "0001.opus"]
