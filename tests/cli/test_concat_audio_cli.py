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

    def test_every_strategy_leaves_its_sources_where_it_found_them(self, run):
        """Reading an input is all any of them do to it. Nothing in this
        pipeline writes a .aac either, so one in an input tree is the user's
        own file and not an intermediate to tidy away."""
        concat, _ = run
        remaining = [p.name for p in concat.inputs.rglob("*") if p.is_file()]
        assert len([n for n in remaining if n.endswith(".mp3")]) == 4
        assert len([n for n in remaining if n.endswith(".opus")]) == 4
        assert len([n for n in remaining if n.endswith(".aac")]) == 2

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


class TestASubfolderThatProducesNothing:
    """A folder the run can make no file out of says so on its own progress
    line, in a plain run.

    Without it the heading promises "Processing" and nothing contradicts it, so
    an input in a format the table has no row for reads exactly like a run that
    worked.
    """

    @pytest.fixture
    def run(self, concat):
        _tree(concat.inputs,
              "Nothing Here/cover.jpg", "Nothing Here/liner notes.txt",
              "Mixed/01 - part.mp3", "Mixed/02 - part.opus",
              "Fine/01 - part.mp3", "Fine/02 - part.mp3")
        # No -v: that a plain run says it is the whole point.
        done = concat.run("concat-audio", concat.inputs, concat.outputs)
        assert done.returncode == 0, done.stdout + done.stderr
        return concat, done.stdout + done.stderr

    def _note(self, log, folder):
        """The line printed directly under that folder's heading."""
        lines = log.splitlines()
        for index, line in enumerate(lines):
            if 'Processing "%s"' % folder in line:
                return lines[index + 1] if index + 1 < len(lines) else ""
        raise AssertionError("no heading for %r in %s" % (folder, log))

    def test_a_folder_holding_no_joinable_audio_says_so(self, run):
        _, log = run
        assert "Nothing to concatenate" in self._note(log, "Nothing Here")

    def test_the_reason_names_the_extensions_that_would_have_worked(self, run):
        """Naming them is the point: the usual cause is audio in a format the
        table has no row for, and "nothing found" alone would not say that."""
        _, log = run
        note = self._note(log, "Nothing Here")
        for extension in (".mp3", ".opus", ".aac", ".m4a", ".flac"):
            assert extension in note, note

    def test_a_mixed_folder_says_so_without_v_as_well(self, run):
        _, log = run
        assert "mixed audio formats" in self._note(log, "Mixed")

    def test_the_folder_that_works_carries_no_such_note(self, run):
        concat, log = run
        assert "Nothing to concatenate" not in self._note(log, "Fine")
        assert _names(concat.outputs, ".mp3") == ["Fine.mp3"]

    def test_the_note_is_never_orphaned_from_its_own_heading(self, run):
        """Written inside the progress lock, so a parallel worker's heading
        cannot land between a folder and its reason."""
        _, log = run
        headings = [line for line in log.splitlines() if "Processing" in line]
        assert len(headings) == 3


class TestM4aIsConcatenated:
    """MP4-framed AAC joins like mp3 and opus rather than through raw ADTS -
    which xHE-AAC cannot pass through at all."""

    @pytest.fixture
    def run(self, concat):
        _tree(concat.inputs,
              "Hoerspiel/01 - Kapitel.m4a", "Hoerspiel/02 - Kapitel.m4a",
              "Hoerspiel/Cover.jpg")
        done = concat.run("concat-audio", "-v", concat.inputs, concat.outputs)
        assert done.returncode == 0, done.stdout + done.stderr
        return concat, done.stdout + done.stderr

    def test_it_comes_out_as_one_m4b(self, run):
        concat, _ = run
        assert _names(concat.outputs, ".m4b") == ["Hoerspiel.m4b"]

    def test_the_join_is_announced_rather_than_skipped(self, run):
        _, log = run
        assert re.search(r"Concatenating .*M4A file\(s\)", log), log

    def test_the_sources_are_left_where_they_were(self, run):
        concat, _ = run
        assert (concat.inputs / "Hoerspiel" / "01 - Kapitel.m4a").exists()
        assert (concat.inputs / "Hoerspiel" / "02 - Kapitel.m4a").exists()


class TestAJoinThatFails:
    """A sub-folder whose ffmpeg cannot produce the file.

    Everything downstream of the join - chapters, cover, the closing line and
    the exit status - reads the output file, so a join that produced none has
    to stop the folder rather than let the run report work it did not do.
    """

    @pytest.fixture
    def run(self, concat):
        _tree(concat.inputs,
              "Breaks/01 - part.mp3", "Breaks/02 - part.mp3",
              "Works/01 - part.opus", "Works/02 - part.opus")
        # An ffmpeg that refuses the mp3 output and behaves for everything
        # else, which is the shape of a real per-format failure.
        concat.with_tool(
            "ffmpeg",
            'out="${!#}"; [[ "$out" == *.mp3 ]] && { echo "boom" >&2; exit 1; };'
            ' [[ "$out" == "-" ]] || : > "$out"')
        done = concat.run("concat-audio", concat.inputs, concat.outputs)
        return concat, done

    def test_the_failure_is_announced_by_name(self, run):
        _, done = run
        assert '!!! "Breaks"' in done.stdout + done.stderr

    def test_the_run_ends_non_zero(self, run):
        """It was asked for two books and made one."""
        _, done = run
        assert done.returncode == 1

    def test_the_closing_line_does_not_claim_to_be_done(self, run):
        _, done = run
        log = done.stdout + done.stderr
        assert "Finished with 1 of 2 subfolder(s) unjoined" in log
        assert "==> Done." not in log

    def test_the_recap_names_the_subfolder_that_produced_nothing(self, run):
        _, done = run
        log = done.stdout + done.stderr
        assert "1 subfolder(s) produced no file:" in log
        assert re.search(r"produced no file:\n  Breaks\n", log), log

    def test_the_healthy_subfolder_still_produced_its_file(self, run):
        """One folder's failure costs that folder and nothing else."""
        concat, _ = run
        assert _names(concat.outputs, ".opus") == ["Works.opus"]

    def test_no_half_written_output_is_left_behind(self, run):
        concat, _ = run
        assert _names(concat.outputs, ".mp3") == []


class TestAFileAlreadyAtTheOutputName:
    """ffmpeg declines to overwrite rather than truncating, so the file in the
    way is somebody else's - it is reported, never cleared away."""

    @pytest.fixture
    def run(self, concat):
        _tree(concat.inputs, "Book/01 - part.mp3", "Book/02 - part.mp3")
        (concat.outputs / "Book.mp3").write_text("an earlier run's book")
        concat.with_tool(
            "ffmpeg",
            'out="${!#}"; [[ "$out" != "-" && -e "$out" ]] &&'
            ' { echo "File exists" >&2; exit 1; };'
            ' [[ "$out" == "-" ]] || : > "$out"')
        done = concat.run("concat-audio", concat.inputs, concat.outputs)
        return concat, done

    def test_the_file_that_was_in_the_way_is_untouched(self, run):
        concat, _ = run
        assert (concat.outputs / "Book.mp3").read_text() == (
            "an earlier run's book")

    def test_the_refusal_is_reported_rather_than_passed_over(self, run):
        _, done = run
        assert '!!! "Book"' in done.stdout + done.stderr
        assert done.returncode == 1


class TestAnInputOfNothingButAac:
    """`.aac` is a format this command joins, so a folder of it is work rather
    than an input to refuse - which means it belongs in the extension list the
    door is answered from, raw ADTS though it is rather than a container."""

    def test_it_is_not_turned_away_as_holding_no_audio(self, concat):
        _tree(concat.inputs, "Book/01 - part.aac", "Book/02 - part.aac")
        done = concat.run("concat-audio", concat.inputs, concat.outputs)
        assert done.returncode == 0, done.stdout + done.stderr
        assert "Nothing to do" not in done.stdout + done.stderr
        assert _names(concat.outputs, ".m4b") == ["Book.m4b"]


class TestTheRawRemuxOnAFailedJoin:
    """The aac path makes a joined copy in RAM before it remuxes, and that copy
    is the only thing it may take back - a failure leaves the input to retry
    from and says what went wrong."""

    @pytest.fixture
    def run(self, concat):
        _tree(concat.inputs, "Book/01 - part.aac", "Book/02 - part.aac")
        concat.with_tool(
            "ffmpeg",
            'out="${!#}"; [[ "$out" == *.m4b ]] && { echo "boom" >&2; exit 1; };'
            ' [[ "$out" == "-" ]] || : > "$out"')
        done = concat.run("concat-audio", concat.inputs, concat.outputs)
        return concat, done

    def test_the_sources_are_still_there(self, run):
        concat, _ = run
        assert (concat.inputs / "Book" / "01 - part.aac").exists()
        assert (concat.inputs / "Book" / "02 - part.aac").exists()

    def test_the_failure_is_not_passed_over(self, run):
        _, done = run
        assert '!!! "Book"' in done.stdout + done.stderr
        assert done.returncode == 1
