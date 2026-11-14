"""The carry-over from the Plex library as the ingest makes it: settled before
the lanes, waited for by the remux lane, and taken in by the one mkvmerge
call - which tracks it adds, where they go and how they are put in step.

What is carried is pinned in tests/lib/test_plexcarry.py; the server, the
files and the tools are stood in for here."""

from __future__ import annotations

import os

import pytest

from medialib.cli import ingest_movies as rules
from medialib.cli import ingest_movies_carry as carryphase
from medialib.cli import ingest_movies_lanes as lanes
from medialib.cli import ingest_movies_run as run
from medialib.lib import chapterdb, plexcarry, plexcompare, plexserver
from medialib.lib.filmmatch import Film

pytestmark = pytest.mark.fs

LIBRARY = "/library/Film (2006)/Film (2006).mkv"
TIMING = plexcompare.Timing(7.9, 0.999)


def _audio(language="fre", channels=2, name="", track=2, lossless=False):
    return plexcompare.Audio(track - 1, language, channels, lossless, False,
                             name, track, "", 48000)


COMMENTARY = plexcarry.Carried(
    _audio(name="With the director"), "commentary", False, False,
    (("/library/x 2 With the director.en.srt", "en"),))
SOUNDTRACK = plexcarry.Carried(_audio("ger", 6, track=3, lossless=True),
                               "soundtrack", True, True)


def _tracks():
    return [rules.Track(id="0", type="video", codec="V_MPEGH/ISO/HEVC"),
            rules.Track(id="1", type="audio", codec="A_OPUS", channels="6",
                        language="fre", default="true"),
            rules.Track(id="2", type="audio", codec="A_AC3", channels="2",
                        language="fre", name="Commentary 1",
                        commentary="true"),
            rules.Track(id="3", type="subtitles", codec="S_HDMV/PGS",
                        language="eng")]


def _command(carried):
    job = {"wanted": False, "video_index": 0}
    return run.Run()._mkvmerge_command("/in/Film.mkv", _tracks(), [], job, "",
                                       (), carried)


class TestTheRemuxCommand:

    INPUTS = [
        carryphase.Input(LIBRARY, 2, "7900", COMMENTARY),
        carryphase.Input("/in/Film_library_3.opus", 0, "7900", SOUNDTRACK),
        carryphase.Input("/library/x 2 With the director.en.srt", 0,
                         "7900,0.999000000", COMMENTARY, "en")]

    def test_a_track_of_the_librarys_copy_comes_alone_and_in_step(self):
        argv, _order, _slots = _command(self.INPUTS)
        at = argv.index(LIBRARY)
        assert argv[at - 19:at] == [
            "--sync", "2:7900", "--language", "2:fre",
            "--track-name", "2:With the director",
            "--default-track-flag", "2:0", "--commentary-flag", "2:1",
            "-a", "2", "-D", "-S", "-B", "-M", "-T", "--no-global-tags",
            "--no-chapters"]

    def test_an_opus_it_was_encoded_to_is_its_one_track(self):
        argv, _order, _slots = _command(self.INPUTS)
        at = argv.index("/in/Film_library_3.opus")
        assert argv[at - 9:at] == [
            "--sync", "0:7900", "--language", "0:ger", "--track-name", "0:",
            "--default-track-flag", "0:0", "--no-chapters"]

    def test_a_transcript_is_put_to_the_new_files_speed(self):
        argv, _order, _slots = _command(self.INPUTS)
        at = argv.index("/library/x 2 With the director.en.srt")
        assert argv[at - 10:at] == [
            "--sync", "0:7900,0.999000000", "--language", "0:en",
            "--track-name", "0:With the director", "--commentary-flag", "0:1",
            "--default-track-flag", "0:0"]

    def test_carried_audio_goes_below_the_films_own(self):
        """The German soundtrack below the film's French, the carried
        commentary below the film's own, the subtitles after, and the
        carried transcript last."""
        _argv, order, slots = _command(self.INPUTS)
        assert order == ["0:0", "0:1", "2:0", "0:2", "1:2", "0:3", "3:0"]
        assert slots["1:2"].carried is COMMENTARY
        assert slots["3:0"].is_transcript


class TestAfterTheSwap:

    def test_the_transcripts_are_written_beside_the_film_by_its_track(
            self, monkeypatch, tmp_path):
        calls: list = []
        monkeypatch.setattr(rules, "_run_capture",
                            lambda argv: calls.append(argv) or "")
        opus = tmp_path / "Film_library_3.opus"
        opus.write_text("encoded")
        inputs = [carryphase.Input(LIBRARY, 2, "7900", COMMENTARY),
                  carryphase.Input(str(opus), 0, "7900", SOUNDTRACK),
                  carryphase.Input("/library/x.en.srt", 0, "7900,0.999",
                                   COMMENTARY, "en")]
        _argv, order, slots = _command(inputs)
        base = str(tmp_path / "Film")
        run.Run(fragments_file="")._after_carrying(
            base + ".mkv", base, inputs, order, slots)
        assert calls == [["mkvextract", base + ".mkv", "tracks",
                          "6:%s 4 With the director.en.srt" % base]]
        assert not opus.exists()


class TestTheRemuxLane:

    class Carry:
        def __init__(self, coming):
            self.coming = coming
            self.done = None

        def watch(self, done):
            self.done = done
            return set(self.coming)

    def test_a_film_waits_for_its_carry_over(self):
        carry = self.Carry({lanes.key("/f/A.mkv")})
        improved: list = []
        lane = lanes.RemuxLane(["/f/A.mkv"], improved.append,
                               lanes.FilmLocks(), False, lambda _line: None,
                               carry=carry)
        lane.opus_done("/f/A.mkv")
        assert improved == []
        carry.done("/f/A.mkv")
        lane.finish()
        assert improved == ["/f/A.mkv"]


class TestTheWhisperLanesRecords:

    def test_a_carried_commentarys_record_goes_to_the_carry_over(self):
        told: list = []

        class Tracker:
            def record_queued(self, record):
                told.append(("queued", record))

            def record_done(self, record):
                told.append(("done", record))

            def film_queued(self, movie):
                told.append(("film", movie))

        class Carry:
            def owns(self, record):
                return record == "carried"

            def record_done(self, record):
                told.append(("carry done", record))

        tracker = run._CarryTracker(Tracker(), Carry())
        for record in ("own", "carried"):
            tracker.record_queued(record)
            tracker.record_done(record)
        assert told == [("queued", "own"), ("done", "own"),
                        ("carry done", "carried")]


class TestSettling:
    """The look-up and the settling on a film with a numbered commentary,
    against a library copy that names it and has named chapters."""

    @pytest.fixture
    def film(self, monkeypatch, tmp_path):
        folder = tmp_path / "in" / "Film (2006)"
        folder.mkdir(parents=True)
        movie = folder / "Film (2006).mkv"
        movie.write_bytes(b"new")
        library = tmp_path / "lib" / "Film (2006) {imdb-tt0000001}"
        library.mkdir(parents=True)
        old = library / "Film (2006) {imdb-tt0000001}.mkv"
        old.write_bytes(b"old")
        new_profile = plexcompare.Profile(
            str(movie), 5955.0, 3840, 1600, "SDR", (_audio(channels=6, track=1),),
            (_audio(name="Commentary 1"),), (), 12, False, "")
        old_profile = new_profile._replace(
            path=str(old), width=1920, commentaries=(
                _audio(name="With the director"),), chapters=3, named_chapters=True)

        class Server:
            url = "http://plex"
            token = "t"

            def copies(self):
                return [plexserver.Copy(
                    "1", Film("Film", "2006", imdb="tt0000001"), "Films", "",
                    (str(old),))]

        monkeypatch.setattr(carryphase.plexserver, "local", Server)
        monkeypatch.setattr(carryphase.plexcompare, "read_profile",
                            lambda path, edition="": new_profile
                            if path == str(movie) else old_profile)
        monkeypatch.setattr(carryphase.plexphase, "timing_of",
                            lambda new, old: (TIMING, ""))
        monkeypatch.setattr(carryphase.chapterdb, "existing_chapters",
                            lambda path: chapterdb.Existing(
                                5942.0, ("Prologue", "Credits", "Harbour"),
                                (0.0, 199.2, 288.6)))
        written: list = []
        monkeypatch.setattr(carryphase.chapterdb, "write_chapters",
                            lambda path, chapters: written.append(
                                (path, chapters)) or True)

        class Tags:
            def tag_of(self, path):
                return "{imdb-tt0000001}"

        said: list = []
        carry = carryphase.LibraryCarry(str(tmp_path / "in"),
                                        lambda: [str(movie)], Tags(),
                                        said.append)
        return {"carry": carry, "movie": str(movie), "said": said,
                "written": written}

    def test_names_and_chapters_are_written_before_the_lanes(self, film):
        renamed: list = []
        carry = film["carry"]
        carry.start()
        carry.settle(lambda _movie: (lambda _audio: True),
                     lambda movie, names: renamed.append((movie, names)) or "",
                     whisper=True)
        carry.finish()
        assert renamed == [(film["movie"], [(2, "With the director")])]
        ((path, chapters),) = film["written"]
        assert path == film["movie"]
        assert [name for _start, name in chapters.chapters] == [
            "Prologue", "Credits", "Harbour"]
        assert "Carrying over from the library's copy: " + film["movie"] \
            in film["said"]
        assert carry.inputs_for(film["movie"]) == []
        assert carry.plan_for(film["movie"]).names == (
            (2, "With the director"),)

    def test_the_plan_is_found_again_after_the_folder_is_renamed(self, film):
        carry = film["carry"]
        carry.start()
        carry.settle(lambda _movie: (lambda _audio: True),
                     lambda movie, names: "", whisper=False)
        folder = os.path.dirname(film["movie"])
        renamed = folder + " {imdb-tt0000001}"
        os.rename(folder, renamed)
        moved = os.path.join(renamed, os.path.basename(film["movie"]))
        assert carry.plan_for(moved) is not None
