"""`concat-audio` as a process: one audiobook file per input sub-folder.

The heavy tools are stand-ins that succeed and create their output, so the real
pipeline runs end to end with no codecs - which is the only way to reach the
parallel dispatch, the per-format strategy choice and the progress counter.

The input tree is on a tmpfs where one is available, mirroring the RAM input
`convert-and-concat` hands over; nothing here depends on it, the assertions being
about names and outputs rather than about where the bytes live.
"""

from __future__ import annotations

import re

import pytest

pytestmark = pytest.mark.stubbed


def _tree(root, *paths):
    for path in paths:
        full = root / path
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text("")
    return root


def _names(directory, suffix):
    return sorted(p.name for p in directory.iterdir()
                  if p.is_file() and p.name.endswith(suffix))


def _starts(log):
    """The ordered "[n/total] Processing" lines, as (n, total) pairs."""
    return [(int(n), int(total)) for n, total in
            re.findall(r"\[(\d+)/(\d+)\] Processing", log)]


@pytest.fixture
def concat(sandbox, tmp_path):
    sandbox.with_media_stubs()
    sandbox.inputs = tmp_path / "in"
    sandbox.outputs = tmp_path / "out"
    sandbox.inputs.mkdir()
    sandbox.outputs.mkdir()
    return sandbox


class TestParallelConcatenation:
    """Five sub-folders spanning both merge strategies: the ffmpeg concat
    demuxer for mp3 and opus, and the raw-cat-then-remux path for aac."""

    @pytest.fixture
    def run(self, concat):
        _tree(concat.inputs,
              *["%s/%s - part.%s" % (folder, number, extension)
                for folder, extension in (("Book mp3 A", "mp3"),
                                          ("Book mp3 B", "mp3"),
                                          ("Book opus A", "opus"),
                                          ("Book opus B", "opus"),
                                          ("Book aac A", "aac"))
                for number in ("01", "02")])
        done = concat.run("concat-audio", concat.inputs, concat.outputs)
        assert done.returncode == 0, done.stdout + done.stderr
        return concat, done.stdout + done.stderr

    def test_one_output_per_subfolder_in_its_own_container(self, run):
        concat, _ = run
        assert len(_names(concat.outputs, ".mp3")) == 2
        assert len(_names(concat.outputs, ".opus")) == 2
        assert len(_names(concat.outputs, ".m4b")) == 1
        assert len([p for p in concat.outputs.iterdir() if p.is_file()]) == 5

    def test_the_progress_counter_is_a_gapless_set_of_one_to_five(self, run):
        """Atomic across the workers: no lost, duplicated or interleaved
        counter, which is the whole risk of dispatching in parallel."""
        _, log = run
        seen = _starts(log)
        assert sorted(n for n, _ in seen) == [1, 2, 3, 4, 5]
        assert {total for _, total in seen} == {5}

    def test_the_demuxer_leaves_its_sources_and_the_remux_consumes_its_own(
            self, run):
        concat, _ = run
        remaining = [p.name for p in concat.inputs.rglob("*") if p.is_file()]
        assert len([n for n in remaining if n.endswith(".mp3")]) == 4
        assert len([n for n in remaining if n.endswith(".opus")]) == 4
        assert [n for n in remaining if n.endswith(".aac")] == []

    def test_no_output_container_is_written_into_the_input_tree(self, run):
        concat, _ = run
        assert list(concat.inputs.rglob("*.m4b")) == []


class TestAMixedSubfolder:
    """A sub-folder must hold exactly one audio format - mp3, opus and aac
    streams can never be concatenated together - and mixing them is skipped
    gracefully rather than crashing or stopping the run."""

    @pytest.fixture
    def run(self, concat):
        _tree(concat.inputs,
              "Alpha/01 - part.mp3", "Alpha/02 - part.opus",
              "Bravo/01 - part.mp3", "Bravo/02 - part.mp3")
        # -v: the per-step "Skipping" line is what this is about, and quiet mode
        # leaves only the progress line.
        done = concat.run("concat-audio", "-v", concat.inputs, concat.outputs)
        assert done.returncode == 0, done.stdout + done.stderr
        return concat, done.stdout + done.stderr

    def test_only_the_clean_subfolder_produces_output(self, run):
        concat, _ = run
        produced = [p.name for p in concat.outputs.iterdir() if p.is_file()]
        assert len(produced) == 1
        assert produced[0].endswith(".mp3")

    def test_the_skip_is_announced_rather_than_silent(self, run):
        _, log = run
        assert "Skipping: mixed audio formats" in log

    def test_the_mixed_sources_are_left_exactly_as_they_were(self, run):
        concat, _ = run
        assert (concat.inputs / "Alpha" / "01 - part.mp3").exists()
        assert (concat.inputs / "Alpha" / "02 - part.opus").exists()

    def test_both_subfolders_were_still_visited(self, run):
        """The skip did not abort the run before its clean sibling."""
        _, log = run
        assert len(_starts(log)) == 2


class TestFlac:
    """FLAC takes the same concat-demuxer strategy as mp3 and opus. What the tag
    and picture blocks then hold is `tests/lib/test_mutagentags_media.py`'s."""

    @pytest.fixture
    def run(self, concat):
        _tree(concat.inputs,
              "Album/01 - first.flac", "Album/02 - second.flac",
              "Mixed/01 - part.flac", "Mixed/02 - part.mp3")
        done = concat.run("concat-audio", "-v", concat.inputs, concat.outputs)
        assert done.returncode == 0, done.stdout + done.stderr
        return concat, done.stdout + done.stderr

    def test_a_pure_flac_subfolder_produces_one_flac(self, run):
        concat, _ = run
        assert len(_names(concat.outputs, ".flac")) == 1

    def test_the_concatenation_is_announced_rather_than_skipped(self, run):
        _, log = run
        assert re.search(r"Concatenating .*FLAC file\(s\)", log), log

    def test_a_flac_and_mp3_mix_names_flac_in_the_skip_recap(self, run):
        """So a mix involving flac is visible rather than silently mishandled.

        The per-format counts on the skip line itself, not merely somewhere in
        the log - which is what the claim would decay into if the line were
        looked for loosely."""
        _, log = run
        skips = [line for line in log.splitlines()
                 if "Skipping: mixed audio formats" in line]
        assert len(skips) == 1, log
        assert "flac:1" in skips[0] and "mp3:1" in skips[0]

    def test_the_mixed_sources_are_left_in_place(self, run):
        concat, _ = run
        assert (concat.inputs / "Mixed" / "01 - part.flac").exists()
        assert (concat.inputs / "Mixed" / "02 - part.mp3").exists()


class TestTwoWorkersSharingACoverName:
    """Cover art is almost always `folder.jpg`, so two sub-folders running at
    once once computed the same temp thumbnail path and one worker deleted the
    file the other was embedding.

    That the paths differ is pinned in `tests/lib/test_thumbnails.py`, the
    embed being a function call rather than a process to record. What belongs
    here is the run: two workers, in parallel, both finishing.
    """

    def test_both_subfolders_finish(self, concat):
        for name in ("Album One", "Album Two"):
            _tree(concat.inputs, "%s/track.opus" % name, "%s/folder.jpg" % name)
        done = concat.run("concat-audio", concat.inputs, concat.outputs)
        assert done.returncode == 0, done.stdout + done.stderr
        assert len(_names(concat.outputs, ".opus")) == 2


class TestTheOutputFolderIsNotSweptByExtension:
    """What a run removes is what the run made.

    This command writes nothing temporary into the output tree - the join and
    the thumbnail work in the scratch, and the chapter "file" is a stem handed
    to the embedder rather than a file - so there is nothing under the output it
    owns, and a sweep by suffix would be removing somebody else's files. A
    refused input made it worse still: the sweep ran before the check, so a run
    that ended by saying "Nothing was changed" had already deleted them.
    """

    def test_a_refused_input_changes_nothing_in_the_output(self, concat):
        # Loose audio files and no sub-folder: the shape this command refuses.
        _tree(concat.inputs, "01 - part.mp3", "02 - part.mp3")
        kept = concat.outputs / "Some Book.ch"
        kept.write_text("CHAPTER01=00:00:00.000\n")
        (concat.outputs / "cover.jpg").write_text("a picture of a book")

        done = concat.run("concat-audio", concat.inputs, concat.outputs)

        assert done.returncode == 1
        assert "Nothing was changed" in done.stdout + done.stderr
        assert kept.read_text() == "CHAPTER01=00:00:00.000\n"
        assert (concat.outputs / "cover.jpg").exists()

    def test_a_run_that_works_leaves_the_same_files_alone(self, concat):
        _tree(concat.inputs, "Book opus A/01 - part.opus")
        chapters = concat.outputs / "Another Book.ch"
        chapters.write_text("CHAPTER01=00:00:00.000\n")
        (concat.outputs / "cover.jpg").write_text("a picture of a book")

        done = concat.run("concat-audio", concat.inputs, concat.outputs)

        assert done.returncode == 0, done.stdout + done.stderr
        assert _names(concat.outputs, ".opus") == ["Book opus A.opus"]
        assert chapters.read_text() == "CHAPTER01=00:00:00.000\n"
        assert (concat.outputs / "cover.jpg").exists()
