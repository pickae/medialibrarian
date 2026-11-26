"""ingest-movies' subtitle roles found out from what the tracks hold.

How many lines a track has, which subtitles the film flags wrong for it, the
flags the tag refresh writes to put them right, and a downloaded sidecar that
reads as SDH. mkvmerge, mkvextract, ffprobe and mkvpropedit are stood in for:
what is pinned is the run's choices.
"""

import json
import os

import pytest

from medialib.cli import ingest_movies as rules

MOVIE = "/library/Film (2006)/Film (2006).mkv"

SDH_SRT = ("1\n00:00:01,000 --> 00:00:02,000\n[thunder]\n\n"
           "2\n00:00:03,000 --> 00:00:04,000\nMAN: Run!\n\n"
           "3\n00:00:05,000 --> 00:00:06,000\nFine.\n")
PLAIN_SRT = "1\n00:00:01,000 --> 00:00:02,000\nHello\n"


def _sub(id_, codec, language, frames="", name="", forced="false",
         hearing="false", commentary="false"):
    return rules.Track(id=str(id_), type="subtitles", codec=codec,
                       language=language, name=name, forced=forced,
                       hearing_impaired=hearing, commentary=commentary,
                       frames=str(frames))


def _film():
    """A Blu-ray of a French film: the French dialogue, the English, and the
    French signs on a track nothing marks as forced."""
    return [rules.Track(id="0", type="video", codec="V_MPEGH/ISO/HEVC"),
            rules.Track(id="1", type="audio", codec="A_OPUS", language="fre"),
            _sub(2, "S_HDMV/PGS", "fre", 2684),
            _sub(3, "S_HDMV/PGS", "eng", 2164),
            _sub(4, "S_HDMV/PGS", "fre", 114)]


@pytest.mark.pure
class TestCountingLines:
    def test_a_blu_ray_spends_two_packets_on_a_line(self):
        assert _sub(3, "S_HDMV/PGS", "fre", 2684).cues == 1342

    def test_a_dvd_and_a_text_subtitle_one(self):
        assert _sub(3, "S_VOBSUB", "fre", 700).cues == 700
        assert _sub(3, "S_TEXT/UTF8", "fre", 900).cues == 900

    def test_a_file_without_statistics_says_nothing(self):
        assert _sub(3, "S_HDMV/PGS", "fre").cues == 0

    def test_the_count_is_read_off_mkvmerge_s_report(self, monkeypatch):
        class Done:
            stdout = json.dumps({"tracks": [{
                "id": 3, "type": "subtitles",
                "properties": {"codec_id": "S_HDMV/PGS",
                               "tag_number_of_frames": 114}}]}).encode()
        monkeypatch.setattr(rules.filmrecord, "output", lambda *a: Done())
        (track,) = rules._identify(MOVIE)
        assert track.frames == "114" and track.cues == 57


@pytest.mark.pure
class TestTheRolesTheFileSaysWrong:
    def test_the_signs_nobody_flagged_are_forced(self):
        assert rules.subtitle_role_edits(_film()) == {"4": ("full", "forced")}

    def test_a_whole_dialogue_flagged_forced_is_full(self):
        tracks = [_sub(3, "S_HDMV/PGS", "eng", 2000, forced="true",
                       name="English Forced"),
                  _sub(4, "S_HDMV/PGS", "fre", 2200)]
        assert rules.subtitle_role_edits(tracks) == {"3": ("forced", "full")}

    def test_a_plain_text_track_that_reads_as_sdh(self):
        tracks = [_sub(6, "S_TEXT/UTF8", "eng", 1000)]
        texts = {"6": ["[thunder]", "MAN: Run!", "Fine."]}
        assert rules.subtitle_role_edits(tracks, texts) == {
            "6": ("full", "sdh")}

    def test_a_film_that_says_it_right_needs_nothing(self):
        tracks = [_sub(3, "S_HDMV/PGS", "eng", 2000),
                  _sub(4, "S_HDMV/PGS", "eng", 80, forced="true")]
        assert rules.subtitle_role_edits(tracks) == {}

    def test_a_commentary_is_left_alone(self):
        tracks = [_sub(3, "S_HDMV/PGS", "eng", 2000),
                  _sub(4, "S_HDMV/PGS", "eng", 40, name="Commentary")]
        assert rules.subtitle_role_edits(tracks) == {}

    def test_a_commentary_is_no_reference(self):
        """A short track beside nothing but a long commentary is not
        measured against it."""
        tracks = [_sub(3, "S_HDMV/PGS", "eng", 2000, commentary="true"),
                  _sub(4, "S_HDMV/PGS", "eng", 40)]
        assert rules.subtitle_role_edits(tracks) == {}

    def test_which_text_is_read_for_it(self):
        assert rules.needs_reading(_sub(6, "S_TEXT/UTF8", "eng"))
        assert not rules.needs_reading(_sub(6, "S_HDMV/PGS", "eng"))
        assert not rules.needs_reading(_sub(6, "S_TEXT/UTF8", "eng",
                                            hearing="true"))

    def test_the_flags_that_say_it(self):
        track = _sub(3, "S_HDMV/PGS", "eng", name="English (Forced)")
        assert rules.role_edit_arguments(track, "full", "forced") == [
            "--set", "flag-forced=1"]
        assert rules.role_edit_arguments(track, "full", "sdh") == [
            "--set", "flag-hearing-impaired=1"]
        assert rules.role_edit_arguments(track, "forced", "full") == [
            "--set", "flag-forced=0", "--set", "name=English"]


def _scope(arguments, position):
    """The ``--set`` arguments of one track's ``--edit`` scope."""
    at = arguments.index("--edit", arguments.index("track:%d" % position) - 1)
    rest = arguments[at + 2:]
    end = rest.index("--edit") if "--edit" in rest else len(rest)
    return rest[:end]


@pytest.mark.pure
class TestTheTagRefreshPutsItRight:
    def test_the_measured_role_goes_in_the_track_s_one_scope(self):
        tracks = _film()
        arguments = rules._tag_arguments(tracks, {"4": ("full", "forced")})
        assert arguments.count("track:5") == 1
        assert _scope(arguments, 5) == ["--set", "flag-default=0",
                                        "--set", "flag-forced=1"]

    def test_a_name_that_says_forced_is_not_flagged_forced_again(self):
        tracks = [_sub(3, "S_HDMV/PGS", "eng", 2000, forced="true",
                       name="English Forced"),
                  _sub(4, "S_HDMV/PGS", "fre", 2200)]
        arguments = rules._tag_arguments(tracks, {"3": ("forced", "full")})
        assert _scope(arguments, 1) == [
            "--set", "flag-default=0", "--set", "flag-forced=0",
            "--set", "name=English", "--set", "language=eng"]

    def test_without_edits_the_name_still_flags(self):
        tracks = [_sub(3, "S_HDMV/PGS", "eng", name="English Forced")]
        assert "flag-forced=1" in rules._tag_arguments(tracks)


@pytest.mark.pure
class TestMeasuringAFilm:
    @pytest.fixture
    def measure(self, monkeypatch):
        calls = {"extract": [], "probe": [], "log": []}

        def extract(movie, tracks):
            calls["extract"].append([track.id for track in tracks])
            return calls.get("texts", {})

        def probe(argv):
            calls["probe"].append(argv)
            return json.dumps({"streams": calls.get("streams", [])})

        monkeypatch.setattr(rules, "_subtitle_texts", extract)
        monkeypatch.setattr(rules, "_probe", probe)
        monkeypatch.setattr(rules, "log", calls["log"].append)

        def run(tracks, **given):
            calls.update(given)
            return rules._measured_role_edits(MOVIE, tracks), calls
        return run

    def test_a_film_with_statistics_is_not_read_through(self, measure):
        edits, calls = measure(_film())
        assert edits == {"4": ("full", "forced")}
        assert calls["probe"] == []
        assert calls["log"] == [
            '  subtitle track 4 "" (fre, 57 lines): the file says full, '
            "measured forced"]

    def test_without_statistics_ffprobe_counts_the_packets(self, measure):
        tracks = [_sub(2, "S_HDMV/PGS", "fre"), _sub(3, "S_HDMV/PGS", "fre")]
        edits, calls = measure(tracks, streams=[
            {"index": 2, "nb_read_packets": "2684"},
            {"index": 3, "nb_read_packets": "114"}])
        assert len(calls["probe"]) == 1
        assert "-count_packets" in calls["probe"][0]
        assert edits == {"3": ("full", "forced")}

    def test_a_lone_subtitle_has_nothing_to_be_counted_against(self,
                                                                measure):
        _edits, calls = measure([_sub(2, "S_HDMV/PGS", "fre")])
        assert calls["probe"] == []

    def test_the_plain_text_tracks_are_read_and_their_lines_counted(
            self, measure):
        """The read text stands for the statistics the file lacks."""
        tracks = [_sub(2, "S_TEXT/UTF8", "eng"),
                  _sub(3, "S_TEXT/UTF8", "eng", name="SDH"),
                  _sub(4, "S_TEXT/UTF8", "eng")]
        texts = {"2": ["[thunder]", "MAN: Run!", "Fine."] * 50,
                 "4": ["Fine."] * 20}
        edits, calls = measure(tracks, texts=texts, streams=[
            {"index": 3, "nb_read_packets": "160"}])
        assert calls["extract"] == [["2", "4"]]
        assert edits == {"2": ("full", "sdh"), "4": ("full", "forced")}


@pytest.mark.fs
class TestTheTagPhase:
    def _film_on_disk(self, tmp_path):
        folder = tmp_path / "Film (2006)"
        folder.mkdir()
        movie = folder / "Film (2006).mkv"
        movie.write_bytes(rules.MATROSKA_MAGIC + b"\0" * 16)
        return str(movie)

    def _tag(self, monkeypatch, tmp_path, roles, original_of=None):
        movie = self._film_on_disk(tmp_path)
        argv, measured = [], []
        monkeypatch.setattr(rules, "_run", lambda command: 0)
        monkeypatch.setattr(rules, "_run_capture",
                            lambda command: argv.append(command) or "")
        monkeypatch.setattr(rules, "_identify", lambda path: _film())
        monkeypatch.setattr(rules, "_measured_role_edits",
                            lambda path, tracks: measured.append(path)
                            or {"4": ("full", "forced")})
        rules.update_tags(str(tmp_path), roles=roles, original_of=original_of)
        return movie, argv, measured

    def test_the_original_flags_go_by_the_film_s_original_language(
            self, monkeypatch, tmp_path):
        """The French soundtrack and the French dialogue subtitle are the
        original; the English subtitle is not, nor the French signs track
        measured to be forced."""
        asked = []
        movie, argv, _measured = self._tag(
            monkeypatch, tmp_path, True,
            original_of=lambda path: asked.append(path) or "fr")
        assert asked == [movie]
        (call,) = argv
        flags = {position: [one for one in _scope(call, position)
                            if one.startswith("flag-original")]
                 for position in range(1, 6)}
        assert flags == {1: [], 2: ["flag-original=1"],
                         3: ["flag-original=1"], 4: ["flag-original=0"],
                         5: ["flag-original=0"]}

    def test_without_an_original_language_the_flags_are_left_alone(
            self, monkeypatch, tmp_path):
        _movie, argv, _measured = self._tag(monkeypatch, tmp_path, True,
                                            original_of=lambda path: "")
        assert not any(one.startswith("flag-original") for one in argv[0])

    def test_the_corrections_ride_in_the_flag_call(self, monkeypatch,
                                                  tmp_path):
        movie, argv, measured = self._tag(monkeypatch, tmp_path, True)
        assert measured == [movie]
        (call,) = argv
        assert _scope(call, 5)[-2:] == ["--set", "flag-forced=1"]

    def test_omitted_the_film_is_not_measured(self, monkeypatch, tmp_path):
        _movie, argv, measured = self._tag(monkeypatch, tmp_path, False)
        assert measured == []
        assert "flag-forced=1" not in argv[0]


@pytest.mark.fs
class TestADownloadedSdhSidecar:
    def _sidecar(self, tmp_path, text):
        base = str(tmp_path / "Film (2006)")
        with open(base + ".en.srt", "w") as handle:
            handle.write(text)
        return base

    def test_it_is_muxed_as_sdh_under_its_own_name(self, tmp_path):
        base = self._sidecar(tmp_path, SDH_SRT)
        lines = []
        assert rules.gather_downloaded_subtitles(base, [], say=lines.append) \
            == [(base + ".en.srt", "en", "sdh")]
        assert lines == ["  English sidecar reads as SDH, muxed flagged as "
                         "such: Film (2006).en.srt"]

    def test_a_plain_one_stays_plain(self, tmp_path):
        base = self._sidecar(tmp_path, PLAIN_SRT)
        assert rules.gather_downloaded_subtitles(base, [], say=print) \
            == [(base + ".en.srt", "en", "full")]

    def test_beside_a_plain_text_track_it_is_something_else(self, tmp_path):
        base = self._sidecar(tmp_path, SDH_SRT)
        tracks = [_sub(3, "S_TEXT/UTF8", "eng")]
        assert [role for _srt, _language, role in
                rules.gather_downloaded_subtitles(base, tracks,
                                                  say=print)] == ["sdh"]

    def test_beside_an_sdh_text_track_it_is_the_same_twice(self, tmp_path):
        base = self._sidecar(tmp_path, SDH_SRT)
        tracks = [_sub(3, "S_TEXT/UTF8", "eng", hearing="true")]
        lines = []
        assert rules.gather_downloaded_subtitles(
            base, tracks, say=lines.append) == []
        assert lines == ["  English sdh subtitle already a text track in the "
                         "file, not muxing the sidecar: Film (2006).en.srt"]

    def test_its_name_stays_what_the_download_looks_for(self, tmp_path):
        base = self._sidecar(tmp_path, SDH_SRT)
        rules.gather_downloaded_subtitles(base, [], say=print)
        assert os.listdir(str(tmp_path)) == ["Film (2006).en.srt"]
