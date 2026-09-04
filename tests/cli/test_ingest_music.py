"""The white box for medialib/cli/ingest_music.py.

Its five small helpers, and the folder map underneath the resume: which
download folder became which library folder, and where one file's output
therefore goes.
"""

import os
import pathlib

import pytest

from medialib.cli import ingest_music as im
from medialib.lib import cuechapters, safety

pytestmark = pytest.mark.fs


def _touch(*paths) -> None:
    for path in paths:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        open(path, "a").close()


class TestChapterTimeMs:
    """An OGM "HH:MM:SS.mmm" timestamp back to whole milliseconds. The input is
    what the cue library's ``time_row`` produces, so it is always fully padded."""

    @pytest.mark.parametrize("stamp,expected", [
        ("00:00:00.000", 0),
        ("00:00:00.120", 120),
        ("00:00:05.000", 5000),
        ("00:05:00.000", 300000),
        ("01:00:00.000", 3600000),
        ("12:34:56.789", 45296789),
    ])
    def test_each_field_carries_its_own_weight(self, stamp, expected):
        assert im.chapter_time_ms(stamp) == expected

    @pytest.mark.parametrize("stamp,expected", [
        ("00:08:00.000", 480000),
        ("00:00:09.000", 9000),
        ("00:00:00.090", 90),
        ("08:00:00.000", 28800000),
    ])
    def test_a_leading_zero_is_still_base_ten(self, stamp, expected):
        """08 and 09 are what trips the shell: without the ``10#`` they are
        invalid octal and abort the arithmetic under set -e."""
        assert im.chapter_time_ms(stamp) == expected

    @pytest.mark.parametrize("ms", [0, 13, 120, 999, 53120, 3600000, 45296789])
    def test_the_round_trip_with_the_writer_these_come_from(self, ms):
        """``time_row`` formats milliseconds and this parses them back, so the
        pair has to be lossless."""
        written = cuechapters.time_row(1, ms)
        assert im.chapter_time_ms(written[len("CHAPTER01="):]) == ms


class TestMaxChapterMs:
    """The latest chapter START in a chapter list."""

    def test_an_empty_list_is_zero(self):
        assert im.max_chapter_ms([]) == 0

    def test_it_finds_the_last_start(self):
        assert im.max_chapter_ms([
            "CHAPTER01=00:00:00.000", "CHAPTER01NAME=Intro",
            "CHAPTER02=00:02:30.000", "CHAPTER02NAME=Middle",
            "CHAPTER03=00:07:34.500", "CHAPTER03NAME=Outro"]) == 454500

    def test_it_is_a_maximum_and_not_the_final_row(self):
        assert im.max_chapter_ms([
            "CHAPTER01=00:00:00.000", "CHAPTER01NAME=A",
            "CHAPTER02=00:07:34.500", "CHAPTER02NAME=B",
            "CHAPTER03=00:02:30.000", "CHAPTER03NAME=C"]) == 454500

    def test_the_NAME_rows_are_ignored(self):
        """Or a chapter name that looks like a timestamp would be read as
        one."""
        assert im.max_chapter_ms([
            "CHAPTER01=00:00:01.000",
            "CHAPTER01NAME=99:99:99.999"]) == 1000

    def test_a_single_chapter_at_zero_stays_zero(self):
        """The whole-album case, which is what lets the caller's "starts past the
        end" check pass for a one-chapter file."""
        assert im.max_chapter_ms([
            "CHAPTER01=00:00:00.000", "CHAPTER01NAME=Only"]) == 0


class TestFlacForCue:
    """Which flac a cue describes. A sibling with the SAME STEM wins outright;
    failing that, a folder holding exactly one flac and one cue is unambiguous;
    anything else is undecidable and yields nothing, and the caller then skips the
    cue rather than guessing."""

    def test_a_same_stem_flac_wins_even_with_others_around(self, tmp_path):
        _touch(str(tmp_path / "Album.cue"), str(tmp_path / "Album.flac"),
               str(tmp_path / "Bonus.flac"))
        assert im.flac_for_cue(str(tmp_path / "Album.cue")) == \
            str(tmp_path / "Album.flac")

    def test_the_stem_match_is_case_insensitive(self, tmp_path):
        """Ripped folders often differ, and the shell matches with -iname."""
        _touch(str(tmp_path / "Album.cue"), str(tmp_path / "ALBUM.FLAC"))
        assert im.flac_for_cue(str(tmp_path / "Album.cue")) == \
            str(tmp_path / "ALBUM.FLAC")

    def test_one_flac_and_one_cue_pair_up_whatever_they_are_called(
            self, tmp_path):
        _touch(str(tmp_path / "disc.cue"),
               str(tmp_path / "Some Other Name.flac"))
        assert im.flac_for_cue(str(tmp_path / "disc.cue")) == \
            str(tmp_path / "Some Other Name.flac")

    def test_two_candidate_flacs_are_undecidable(self, tmp_path):
        _touch(str(tmp_path / "disc.cue"), str(tmp_path / "a.flac"),
               str(tmp_path / "b.flac"))
        assert im.flac_for_cue(str(tmp_path / "disc.cue")) == ""

    def test_two_cues_over_one_flac_are_undecidable(self, tmp_path):
        """Which of them owns it?"""
        _touch(str(tmp_path / "one.cue"), str(tmp_path / "two.cue"),
               str(tmp_path / "audio.flac"))
        assert im.flac_for_cue(str(tmp_path / "one.cue")) == ""

    def test_a_cue_with_no_flac_at_all_yields_nothing(self, tmp_path):
        _touch(str(tmp_path / "disc.cue"))
        assert im.flac_for_cue(str(tmp_path / "disc.cue")) == ""

    def test_only_the_cues_own_folder_is_considered(self, tmp_path):
        _touch(str(tmp_path / "disc.cue"), str(tmp_path / "sub" / "audio.flac"))
        assert im.flac_for_cue(str(tmp_path / "disc.cue")) == ""


class TestDeleteUnneededCue:
    """After the encoding and the embedding, a cue is only useful next to a flac
    it describes. Matched by stem, so this runs while the two still share a base
    name."""

    @pytest.fixture
    def tree(self, tmp_path):
        _touch(str(tmp_path / "album" / "Album.cue"),
               str(tmp_path / "album" / "Album.flac"),
               str(tmp_path / "album" / "notes.txt"),
               str(tmp_path / "orphaned" / "Gone.cue"),
               str(tmp_path / "deep" / "inner" / "Deep.cue"),
               str(tmp_path / "deep" / "inner" / "Deep.flac"),
               str(tmp_path / "deep" / "Stray.cue"))
        return tmp_path

    def test_a_cue_next_to_its_flac_is_kept(self, tree):
        im.delete_unneeded_cue(str(tree))
        assert (tree / "album" / "Album.cue").exists()

    def test_a_cue_without_its_flac_is_removed(self, tree):
        im.delete_unneeded_cue(str(tree))
        assert not (tree / "orphaned" / "Gone.cue").exists()

    def test_the_removal_recurses_both_ways(self, tree):
        im.delete_unneeded_cue(str(tree))
        assert (tree / "deep" / "inner" / "Deep.cue").exists()
        assert not (tree / "deep" / "Stray.cue").exists()

    def test_every_drop_is_counted(self, tree):
        assert im.delete_unneeded_cue(str(tree)) == 2

    def test_nothing_that_is_not_an_orphaned_cue_is_touched(self, tree):
        im.delete_unneeded_cue(str(tree))
        assert (tree / "album" / "Album.flac").exists()
        assert (tree / "album" / "notes.txt").exists()
        assert (tree / "orphaned").is_dir()

    def test_it_is_idempotent(self, tree):
        im.delete_unneeded_cue(str(tree))
        before = sorted(str(p) for p in tree.rglob("*"))
        assert im.delete_unneeded_cue(str(tree)) == 0
        assert sorted(str(p) for p in tree.rglob("*")) == before


class TestIsLosslessCodec:
    """Codec membership in the central enum - the one gate on whether a track is
    re-encoded into the library or left behind entirely.

    Asked of ffprobe's codec_name and NOT of the extension, which is the whole
    point: a .wav holding mp3 data and an .m4a holding AAC both reach the encoder,
    and only this answer sends them back. A lossy codec wrongly accepted would
    re-encode lossy into FLAC; a lossless one wrongly rejected would silently drop
    a real track, since the plain copy excludes those extensions.
    """

    @pytest.mark.parametrize("codec", ["flac", "ape", "alac", "pcm_s16le",
                                       "pcm_s16be", "wavpack"])
    def test_the_lossless_codecs_are_accepted(self, codec):
        assert im.is_lossless_codec(codec) is True

    def test_the_answer_is_case_insensitive(self):
        """pcm_s16be is the one an inline chain of comparisons is easiest to
        forget, and a big-endian rip is still a lossless rip."""
        assert im.is_lossless_codec("PCM_S16BE") is True

    @pytest.mark.parametrize("codec", ["mp3", "aac", "opus", "vorbis", "wmav2",
                                       "ac3", "mp2"])
    def test_a_lossy_codec_is_rejected(self, codec):
        assert im.is_lossless_codec(codec) is False

    def test_an_unreadable_codec_is_rejected(self):
        """What ffprobe leaves behind when it cannot read a stream at all: not
        lossless, so the track is skipped and counted rather than encoded."""
        assert im.is_lossless_codec("") is False

    @pytest.mark.parametrize("codec", ["flacx", "xflac", "pcm_s24le", "wav"])
    def test_a_near_miss_is_rejected(self, codec):
        """Membership is whole-word: "flac" must not carry "flacx" in with
        it."""
        assert im.is_lossless_codec(codec) is False

    @pytest.mark.parametrize("extension,codec", [
        ("flac", "flac"), ("ape", "ape"), ("wav", "pcm_s16le"),
        ("wv", "wavpack"),
    ])
    def test_every_queued_extension_can_hold_a_codec_the_encoder_takes(
            self, extension, codec):
        """The two enums this script asks on its two axes have to agree about
        what lossless means: an extension queued for encoding whose codec the
        encoder then rejects is a file that was excluded from the plain copy and
        never ingested at all.

        Not an identity - the two are spelled differently on purpose, wv holding
        wavpack and wav holding pcm_* - so what is pinned is that round trip.
        """
        from medialib.lib import enums
        assert extension in enums.LOSSLESS_AUDIO_EXTENSIONS
        assert im.is_lossless_codec(codec) is True


class TestTheFolderMap:
    """What each download folder became, from the flacs ingested out of it."""

    def _pairs(self, tmp_path, records):
        path = str(tmp_path / "pairs")
        with open(path, "wb") as handle:
            for source, library in records:
                handle.write(source.encode() + b"\0" + library.encode() + b"\0")
        return path

    def test_a_renamed_folder_is_learnt_from_its_tracks(self, tmp_path):
        pairs = self._pairs(tmp_path, [
            ("Album/one.flac", "/lib/Cleaned Album/one.flac"),
            ("Album/two.flac", "/lib/Cleaned Album/two.flac")])
        became, ambiguous = im.build_ingested_folder_map(pairs, "/lib")
        assert became == {"Album": "Cleaned Album"}
        assert ambiguous == set()

    def test_a_folder_whose_name_survived_is_not_recorded(self, tmp_path):
        """Only the folders that resolve somewhere ELSE, so an ordinary first run
        leaves the map empty and every path stays exactly what it was."""
        pairs = self._pairs(tmp_path, [("Album/one.flac",
                                        "/lib/Album/one.flac")])
        became, _ambiguous = im.build_ingested_folder_map(pairs, "/lib")
        assert became == {}

    def test_tracks_spread_over_several_folders_are_ambiguous(self, tmp_path):
        """Someone has reorganised that album by hand, and which folder its rip
        log now belongs to is not this script's guess to make."""
        pairs = self._pairs(tmp_path, [
            ("Album/one.flac", "/lib/Disc One/one.flac"),
            ("Album/two.flac", "/lib/Disc Two/two.flac")])
        _became, ambiguous = im.build_ingested_folder_map(pairs, "/lib")
        assert ambiguous == {"Album"}

    def test_a_track_at_the_library_root_reads_as_the_root(self, tmp_path):
        pairs = self._pairs(tmp_path, [("Album/one.flac", "/lib/one.flac")])
        became, _ambiguous = im.build_ingested_folder_map(pairs, "/lib")
        assert became == {"Album": "."}

    def test_a_library_beside_this_one_is_not_inside_it(self, tmp_path):
        """"/libExtra" starts with "/lib" and is not under it, so the whole
        path is what the album became."""
        pairs = self._pairs(tmp_path, [
            ("Album/one.flac", "/libExtra/Album/one.flac")])
        became, _ambiguous = im.build_ingested_folder_map(pairs, "/lib")
        assert became == {"Album": "libExtra/Album"}


class TestTheIngestDestinationStaysInTheLibrary:
    """Every encode, copy and rename in the ingest lands where ingest_path_for
    says, and the folder half of it comes from a map learnt from a file the last
    run wrote - so it is checked rather than trusted."""

    def test_an_ordinary_path_is_returned(self, tmp_path):
        assert im.ingest_path_for("Album/one.flac", str(tmp_path), {}) == \
            str(tmp_path / "Album" / "one.flac")

    def test_a_map_that_climbs_out_of_the_library_stops_the_run(self, tmp_path):
        with pytest.raises(safety.OutsideTheRun) as raised:
            im.ingest_path_for("Album/one.flac", str(tmp_path),
                               {"Album": "../elsewhere"})
        assert "ingest path is outside" in str(raised.value)

    def test_the_folder_half_is_checked_too(self, tmp_path):
        with pytest.raises(safety.OutsideTheRun):
            im.ingested_dir_path("Album", str(tmp_path),
                                 {"Album": "../../elsewhere"})


class TestResolveThroughAncestors:
    """A folder that holds no flac of its own asks its nearest mapped ancestor."""

    MAP = {"Album": "Cleaned Album"}

    def test_a_mapped_folder_answers_for_itself(self):
        assert im.resolve_through_ancestors("Album", self.MAP, set()) == \
            "Cleaned Album"

    def test_a_subfolder_rides_on_its_parent(self):
        """A "Scans" subfolder of an album that WAS renamed belongs under the
        renamed album, with the path below it carried across unchanged."""
        assert im.resolve_through_ancestors("Album/Scans", self.MAP, set()) == \
            "Cleaned Album/Scans"

    def test_and_so_does_one_several_levels_down(self):
        assert im.resolve_through_ancestors(
            "Album/Scans/Booklet", self.MAP, set()) == \
            "Cleaned Album/Scans/Booklet"

    def test_an_unmapped_folder_keeps_the_path_it_has(self):
        assert im.resolve_through_ancestors("Other", self.MAP, set()) == "Other"

    def test_an_ambiguous_ancestor_decides_nothing(self):
        """Better to leave the path alone than to write a release's sidecars into
        one of the folders its tracks were split between."""
        assert im.resolve_through_ancestors(
            "Album/Scans", self.MAP, {"Album"}) == "Album/Scans"

    def test_a_root_that_moved_carries_the_tree_with_it(self):
        assert im.resolve_through_ancestors("Extras", {".": "Library"},
                                            set()) == "Library/Extras"

    def test_a_folder_mapped_to_the_root_loses_its_own_level(self):
        assert im.resolve_through_ancestors("Album", {"Album": "."},
                                            set()) == "."


class TestIngestPathFor:
    """Where one download file's output belongs in the library."""

    MAP = {"Album": "Cleaned Album"}

    def test_the_folder_is_resolved_and_the_name_kept(self):
        assert im.ingest_path_for("Album/cover.jpg", "/lib", self.MAP) == \
            "/lib/Cleaned Album/cover.jpg"

    def test_an_extension_can_be_replaced(self):
        assert im.ingest_path_for("Album/one.wav", "/lib", self.MAP,
                                  "flac") == "/lib/Cleaned Album/one.flac"

    def test_an_unmapped_folder_is_written_where_it_always_was(self):
        assert im.ingest_path_for("Other/one.wav", "/lib", self.MAP,
                                  "flac") == "/lib/Other/one.flac"

    def test_a_file_at_the_download_root_lands_at_the_library_root(self):
        assert im.ingest_path_for("cover.jpg", "/lib", self.MAP) == \
            "/lib/cover.jpg"

    def test_a_root_that_is_mapped_is_followed_too(self):
        assert im.ingest_path_for("cover.jpg", "/lib", {".": "Library"}) == \
            "/lib/Library/cover.jpg"


class TestHasTwinIn:
    """Is this file already in that folder under some other name?

    What identifies a sidecar or a booklet scan is what it IS: it carries no tag
    to say where it came from, so a run that renamed it would copy it again on
    every later run.
    """

    def test_the_same_bytes_under_another_name_are_a_twin(self, tmp_path):
        (tmp_path / "a.txt").write_bytes(b"same")
        (tmp_path / "lib").mkdir()
        (tmp_path / "lib" / "cleaned.txt").write_bytes(b"same")
        assert im.has_twin_in(str(tmp_path / "a.txt"),
                              str(tmp_path / "lib")) is True

    def test_different_bytes_of_the_same_size_are_not(self, tmp_path):
        (tmp_path / "a.txt").write_bytes(b"same")
        (tmp_path / "lib").mkdir()
        (tmp_path / "lib" / "other.txt").write_bytes(b"NOPE")
        assert im.has_twin_in(str(tmp_path / "a.txt"),
                              str(tmp_path / "lib")) is False

    def test_the_file_itself_is_not_its_own_twin(self, tmp_path):
        (tmp_path / "a.txt").write_bytes(b"same")
        assert im.has_twin_in(str(tmp_path / "a.txt"), str(tmp_path)) is False

    def test_only_the_folder_itself_is_searched(self, tmp_path):
        (tmp_path / "a.txt").write_bytes(b"same")
        (tmp_path / "lib" / "deeper").mkdir(parents=True)
        (tmp_path / "lib" / "deeper" / "copy.txt").write_bytes(b"same")
        assert im.has_twin_in(str(tmp_path / "a.txt"),
                              str(tmp_path / "lib")) is False

    def test_a_folder_that_is_not_there_holds_no_twin(self, tmp_path):
        (tmp_path / "a.txt").write_bytes(b"same")
        assert im.has_twin_in(str(tmp_path / "a.txt"),
                              str(tmp_path / "gone")) is False


# --- the cue chapters, and every way one can be declined ----------------------
# Every cue leaves exactly one counted line behind, which is the point: a release
# whose cue names a disc that never got copied must not come out without chapters
# AND without a word about it. The write itself is stubbed here and real in the
# media tier - it stopped being a subprocess at item 5.2, so the stubbed-tool
# suite can no longer observe it by putting a recorder on PATH.

class _Counters:
    def __init__(self):
        self.counts = {}
        self.lines = []
        self.notes = []

    def bump(self, name, amount=1):
        self.counts[name] = self.counts.get(name, 0) + amount

    def progress(self, line):
        self.lines.append(line)

    def note(self, line):
        self.notes.append(line)


CUE = """FILE "one.flac" WAVE
  TRACK 01 AUDIO
    TITLE "First"
    INDEX 01 00:00:00
  TRACK 02 AUDIO
    TITLE "Second"
    INDEX 01 00:02:00
"""


@pytest.fixture
def release(tmp_path, monkeypatch):
    """A folder holding one flac and the cue that describes it, plus the run that
    would embed one into the other."""
    def make(cue_text=CUE, duration_ms=600_000, status=0):
        (tmp_path / "one.flac").write_bytes(b"not really a flac")
        cue = tmp_path / "one.cue"
        cue.write_text(cue_text)
        counters = _Counters()
        run = im.Run(ingest_dir=str(tmp_path), counters=counters)
        monkeypatch.setattr(im, "_duration_ms", lambda path: duration_ms)
        written = []

        def embed(audio, chapter_file, title="", force=False, error=None):
            written.append((audio, title, force,
                            pathlib.Path(chapter_file).read_text()))
            return status

        monkeypatch.setattr(im.mutagentags, "embed_chapters", embed)
        return run, counters, str(cue), written
    return make


class TestEmbedCueChapters:
    def test_a_cue_and_its_flac_get_the_chapters_and_one_counted_line(
            self, release):
        run, counters, cue, written = release()
        run.embed_cue_chapters([cue])
        assert counters.counts == {"chaptersEmbedded": 1}
        assert len(counters.lines) == 1
        audio, title, force, text = written[0]
        assert audio.endswith("one.flac")
        assert force
        assert "CHAPTER01=" in text and "CHAPTER02=" in text

    def test_a_write_that_fails_is_counted_and_named_rather_than_fatal(
            self, release):
        """The status is the whole reason the writer answers with one: a file
        mutagen cannot tag used to be a non-zero exit, and a direct call that
        raised instead would end the run over one track."""
        run, counters, cue, _written = release(status=1)
        run.embed_cue_chapters([cue])
        assert counters.counts == {"chaptersFailed": 1}
        assert len(counters.notes) == 1
        assert "could not be written" in counters.notes[0]

    def test_a_cue_with_no_flac_of_its_own_is_skipped_and_said(self, release,
                                                               tmp_path):
        run, counters, _cue, written = release()
        orphan = tmp_path / "sub" / "other.cue"
        orphan.parent.mkdir()
        orphan.write_text(CUE)
        run.embed_cue_chapters([str(orphan)])
        assert counters.counts == {"chaptersSkipped": 1}
        assert "no flac this cue could belong to" in counters.lines[0]
        assert written == []

    def test_a_cue_describing_no_chapter_is_skipped(self, release):
        run, counters, cue, written = release(cue_text='FILE "one.flac" WAVE\n')
        run.embed_cue_chapters([cue])
        assert counters.counts == {"chaptersSkipped": 1}
        assert "no chapter in the cue" in counters.lines[0]
        assert written == []

    def test_a_flac_reporting_no_duration_is_skipped(self, release):
        run, counters, cue, written = release(duration_ms=0)
        run.embed_cue_chapters([cue])
        assert counters.counts == {"chaptersSkipped": 1}
        assert "reports no duration" in counters.lines[0]
        assert written == []

    def test_a_cue_that_outruns_its_flac_describes_another_disc(self, release):
        """Its last mark lands past the end of the file, so it cannot be this
        one's cue however much the names agree."""
        run, counters, cue, written = release(duration_ms=1_000)
        run.embed_cue_chapters([cue])
        assert counters.counts == {"chaptersSkipped": 1}
        assert written == []

    def test_every_cue_leaves_exactly_one_line_whatever_happens(self, release,
                                                                tmp_path):
        run, counters, cue, _written = release()
        orphan = tmp_path / "sub" / "other.cue"
        orphan.parent.mkdir()
        orphan.write_text(CUE)
        run.embed_cue_chapters([cue, str(orphan)])
        assert len(counters.lines) == 2
        assert sum(counters.counts.values()) == 2


class TestTheTransferNamingFlag:
    """Which spelling of "print what you transferred" this host's rsync takes.

    The copying pass reads that output to find the files a previous run's name
    cleaning renamed, so a flag the rsync on PATH does not know is not a
    cosmetic difference: rsync refuses the whole call, nothing is copied, and
    nothing is de-duplicated.
    """

    pytestmark = pytest.mark.pure

    def test_a_modern_rsync_is_asked_the_modern_way(self, monkeypatch):
        monkeypatch.setattr(im, "_rsync_help",
                            lambda: "--out-format=FORMAT  output updates")
        assert im._transfer_format() == "--out-format=%n"

    def test_and_the_one_macos_ships_the_old_way(self, monkeypatch):
        """rsync 2.6.9, which is what a Mac has without Homebrew, spells it
        --log-format; openrsync on 14 and later carries that spelling too."""
        monkeypatch.setattr(im, "_rsync_help",
                            lambda: "--log-format=FORMAT  log file transfers")
        assert im._transfer_format() == "--log-format=%n"

    def test_an_rsync_that_cannot_be_run_reads_as_the_modern_one(self,
                                                                 monkeypatch):
        """The preflight already asked for rsync, so getting here with none is
        a race rather than a configuration - and the modern spelling is what
        every host this has run on has."""
        def absent(*_a, **_k):
            raise FileNotFoundError(2, "no such file", "rsync")

        monkeypatch.setattr(im.subprocess, "run", absent)
        assert im._transfer_format() == "--out-format=%n"
