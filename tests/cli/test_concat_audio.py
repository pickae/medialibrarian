"""The white box for medialib/cli/concat_audio.py.

The integration files drive the whole pipeline with stubbed tools; what is
pinned here is the cue-sheet choice and the two rules the join is built on.
"""

import os

import pytest

from medialib.cli import concat_audio as ca

pytestmark = pytest.mark.fs


class TestSelectCueSheet:
    """Which cue a folder's chapters are read from, when it holds one audio
    file and more than one cue sheet."""

    def test_a_single_cue_is_always_the_one_used(self, tmp_path):
        """Even buried in a sub-folder, and even though it matches nothing."""
        (tmp_path / "sub").mkdir()
        (tmp_path / "album.flac").write_text("")
        cue = tmp_path / "sub" / "only.cue"
        cue.write_text("only\n")
        assert ca.select_cue_sheet([str(cue)],
                                   str(tmp_path / "album.flac")) == str(cue)

    def test_the_sibling_cue_sharing_the_stem_beats_a_larger_foreign_one(
            self, tmp_path):
        (tmp_path / "sub").mkdir()
        (tmp_path / "My Album.flac").write_text("")
        big = tmp_path / "sub" / "big.cue"
        big.write_text("a" * 28 + "\n")          # larger, wrong place
        match = tmp_path / "My Album.cue"
        match.write_text("match\n")              # same stem and same folder
        assert ca.select_cue_sheet([str(big), str(match)],
                                   str(tmp_path / "My Album.flac")) == str(match)

    def test_the_largest_cue_wins_when_none_matches(self, tmp_path):
        for name in ("sub1", "sub2"):
            (tmp_path / name).mkdir()
        (tmp_path / "track.flac").write_text("")
        small = tmp_path / "sub1" / "one.cue"
        small.write_text("small\n")
        large = tmp_path / "sub2" / "two.cue"
        large.write_text("this cue is considerably larger than the other one\n")
        assert ca.select_cue_sheet([str(small), str(large)],
                                   str(tmp_path / "track.flac")) == str(large)

    def test_a_zero_byte_cue_is_still_a_choice(self, tmp_path):
        """-1 is the "nothing seen yet" mark rather than 0, because a cue file
        can legitimately be empty."""
        (tmp_path / "a.flac").write_text("")
        empty = tmp_path / "empty.cue"
        empty.write_text("")
        other = tmp_path / "other.cue"
        other.write_text("")
        assert ca.select_cue_sheet([str(empty), str(other)],
                                   str(tmp_path / "a.flac")) == str(empty)

    def test_no_cue_at_all_is_no_choice(self, tmp_path):
        assert ca.select_cue_sheet([], str(tmp_path / "a.flac")) == ""


class TestScan:
    """One directory walk, not four."""

    @pytest.fixture
    def folder(self, tmp_path):
        for name in ("a.mp3", "b.mp3", "c.opus", "d.flac", "f.m4a",
                     "notes.txt", "sheet.CUE", "other.cue"):
            (tmp_path / name).write_text("")
        (tmp_path / "deep").mkdir()
        (tmp_path / "deep" / "e.mp3").write_text("")
        return ca.Scan(str(tmp_path))

    def test_it_counts_each_format_at_every_depth(self, folder):
        assert folder.counts == {"mp3": 3, "opus": 1, "aac": 0, "m4a": 1,
                                 "flac": 1}

    def test_a_cue_matches_in_either_case(self, folder):
        """Deliberately unlike the audio extensions, which match exactly - the
        shell used -iname for the cue and -name for the rest."""
        assert sorted(os.path.basename(cue) for cue in folder.cues) == [
            "other.cue", "sheet.CUE"]

    def test_the_audio_total_is_what_decides_the_chapter_source(self, folder):
        assert folder.audio_files == 6


class TestFormats:
    def test_flac_is_re_encoded_and_the_others_are_copied(self):
        """Copying FLAC frames leaves the FIRST file's STREAMINFO in place, so
        the result reports only the first segment's length and players hide
        every chapter past it."""
        assert ca.FORMATS["flac"][3] == "flac"
        assert ca.FORMATS["mp3"][3] == "copy"
        assert ca.FORMATS["opus"][3] == "copy"

    def test_aac_comes_out_as_an_m4b_through_the_raw_remux(self):
        """Raw ADTS has no container to demux."""
        _source, output, strategy, _codec = ca.FORMATS["aac"]
        assert (output, strategy) == ("m4b", "rawRemux")

    def test_m4a_demuxes_into_an_m4b_rather_than_going_through_adts(self):
        """ADTS has no spelling for xHE-AAC, so unwrapping an .m4a to .aac
        fails outright - the MP4 frames are demuxed where they are."""
        _source, output, strategy, codec = ca.FORMATS["m4a"]
        assert (output, strategy, codec) == ("m4b", "mp4Demuxer", "copy")

    def test_every_counted_format_has_a_row_in_the_table(self):
        """The tally on the skip line is built from FORMAT_ORDER, so a name
        there that the table does not carry would be an unjoinable count."""
        assert sorted(fmt for fmt, _label in ca.FORMAT_ORDER) == sorted(
            ca.FORMATS)


class TestTheRawRemuxLeavesItsSources:
    """It joins a copy in RAM and remuxes that, so the input is only ever read.

    No step in this pipeline writes a .aac, so one reaching here is the user's
    own file rather than an intermediate to tidy away - and the chapter builder
    probes each of them for its duration after the join, which a source taken
    away at the join would leave starting at 0:00.
    """

    def _folder(self, tmp_path):
        folder = tmp_path / "book"
        folder.mkdir()
        for name in ("01 - part.aac", "02 - part.aac"):
            (folder / name).write_bytes(b"\xff\xf1")
        return folder

    def _run(self, argv, **kwargs):
        open(argv[-1], "w").close()
        return None

    def test_the_sources_are_still_there_when_the_join_returns(self, tmp_path):
        folder = self._folder(tmp_path)
        ca.concat_format(str(folder), str(tmp_path / "out"), "aac",
                         str(tmp_path), "ffmpeg", run=self._run)
        assert sorted(p.name for p in folder.iterdir()) == [
            "01 - part.aac", "02 - part.aac"]

    def test_the_joined_copy_it_made_in_ram_is_not(self, tmp_path):
        """The one file it does own, and the one it takes back."""
        folder = self._folder(tmp_path)
        scratch = tmp_path / "scratch"
        scratch.mkdir()
        ca.concat_format(str(folder), str(tmp_path / "out"), "aac",
                         str(scratch), "ffmpeg", run=self._run)
        assert list(scratch.iterdir()) == []

    def test_the_entries_are_the_sources_in_order(self, tmp_path):
        folder = self._folder(tmp_path)
        entries = ca.concat_format(str(folder), str(tmp_path / "out"), "aac",
                                   str(tmp_path), "ffmpeg", run=self._run)
        assert [e.rsplit("/", 1)[-1] for e in entries] == [
            "01 - part.aac'", "02 - part.aac'"]


class TestConcatViaDemuxer:
    """How the concat list gets to ffmpeg."""

    class _Run:
        """A stand-in ffmpeg that reads the list it was handed and creates the
        output it was asked for, the way the real one does - the second half
        being what tells a caller the join worked."""

        def __init__(self):
            self.argv = []
            self.listing = ""
            self.text = ""

        def __call__(self, argv, **kwargs):
            self.argv = list(argv)
            self.listing = argv[argv.index("-i") + 1]
            with open(self.listing) as handle:
                self.text = handle.read()
            open(argv[-1], "w").close()
            return None

    def _folder(self, tmp_path, count):
        for index in range(count):
            (tmp_path / ("track %04d - a chapter of the book.mp3"
                         % index)).write_text("")
        return str(tmp_path)

    def _concat(self, folder, run):
        return ca.concat_via_demuxer(folder, "mp3", folder + "/out.mp3",
                                     "copy", "ffmpeg", run=run)

    def test_ffmpeg_is_handed_a_file_holding_the_whole_list(self, tmp_path):
        run = self._Run()
        entries = self._concat(self._folder(tmp_path, 3), run)
        assert run.text == "\n".join(entries) + "\n"
        assert run.argv[:10] == [
            "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
            "-safe", "0", "-f", "concat", "-i"]
        assert run.argv[11:] == ["-codec", "copy", str(tmp_path) + "/out.mp3"]

    def test_a_list_past_a_pipes_capacity_still_finishes(self, tmp_path):
        """A pipe holds 64 KiB, and a folder this size writes past that
        before ffmpeg would be there to drain it."""
        run = self._Run()
        entries = self._concat(self._folder(tmp_path, 900), run)
        assert len(run.text.encode()) > 64 * 1024
        assert run.text == "\n".join(entries) + "\n"

    def test_the_list_file_does_not_outlive_the_join(self, tmp_path):
        run = self._Run()
        self._concat(self._folder(tmp_path, 3), run)
        assert not os.path.exists(run.listing)

    def test_an_empty_folder_starts_nothing(self, tmp_path):
        run = self._Run()
        assert self._concat(str(tmp_path), run) == []
        assert run.argv == []

    def test_audio_only_maps_the_audio_stream_and_nothing_else(self, tmp_path):
        """An .m4a usually carries its cover as a second stream, and the m4b
        muxer refuses to write one - so the join asks for the audio alone."""
        run = self._Run()
        (tmp_path / "01 - part.m4a").write_text("")
        ca.concat_via_demuxer(str(tmp_path), "m4a", str(tmp_path) + "/out.m4b",
                              "copy", "ffmpeg", run=run, audio_only=True)
        assert run.argv[11:] == ["-map", "0:a", "-codec", "copy",
                                 str(tmp_path) + "/out.m4b"]

    def test_the_map_is_absent_when_it_was_not_asked_for(self, tmp_path):
        run = self._Run()
        self._concat(self._folder(tmp_path, 2), run)
        assert "-map" not in run.argv
