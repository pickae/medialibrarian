"""ingest-movies' picture subtitles read into text.

Which picture subtitle is read into which sidecar, the forced and SDH sidecars
gathered for the mux, and the remux's reading before it gathers them. seconv
and mkvextract are stood in for: what is pinned is the run's choices.
"""

import os

import pytest

from medialib.cli import ingest_movies as rules
from medialib.cli import ingest_movies_run as run_module
from medialib.lib import languages

ENGLISH, FRENCH = (next(row for row in languages.LANGUAGES
                        if row.code2 == code) for code in ("en", "fr"))


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
            _sub(3, "S_HDMV/PGS", "fre", 2684),
            _sub(4, "S_HDMV/PGS", "eng", 2164),
            _sub(5, "S_HDMV/PGS", "fre", 114)]


def _jobs(base, tracks, wanted=(ENGLISH, FRENCH)):
    return [(job.track.id, job.row.code2, os.path.basename(job.srt))
            for job in rules.image_subtitles_to_read(base, tracks, wanted)]


@pytest.mark.fs
class TestWhatIsRead:
    def test_each_language_s_dialogue_and_the_signs_nobody_flagged(
            self, tmp_path):
        base = str(tmp_path / "Film (2006)")
        assert _jobs(base, _film()) == [
            ("4", "en", "Film (2006).en.srt"),
            ("3", "fr", "Film (2006).fr.srt"),
            ("5", "fr", "Film (2006).fr.forced.srt")]

    def test_a_language_with_a_text_subtitle_is_not_read(self, tmp_path):
        tracks = _film() + [_sub(6, "S_TEXT/UTF8", "eng", 1000)]
        assert ("4", "en", "Film (2006).en.srt") \
            not in _jobs(str(tmp_path / "Film (2006)"), tracks)

    def test_a_text_subtitle_the_remux_drops_does_not_count(self, tmp_path):
        text = _sub(6, "S_TEXT/UTF8", "eng", 1000)
        text.action = "drop"
        assert ("4", "en", "Film (2006).en.srt") \
            in _jobs(str(tmp_path / "Film (2006)"), _film() + [text])

    def test_a_sidecar_already_there_is_not_read_again(self, tmp_path):
        """A download that passed the sync test, or an earlier reading."""
        base = str(tmp_path / "Film (2006)")
        with open(base + ".fr.srt", "w") as handle:
            handle.write("1\n00:00:01,000 --> 00:00:02,000\nOui\n")
        assert [job[0] for job in _jobs(base, _film())] == ["4", "5"]

    def test_only_the_wanted_languages(self, tmp_path):
        assert _jobs(str(tmp_path / "F"), _film(), wanted=(ENGLISH,)) \
            == [("4", "en", "F.en.srt")]

    def test_the_plain_one_falls_back_to_the_sdh_one(self, tmp_path):
        tracks = [_sub(3, "S_HDMV/PGS", "eng", 2000, name="SDH",
                       hearing="true")]
        assert _jobs(str(tmp_path / "F"), tracks) == [("3", "en", "F.en.srt")]

    def test_with_both_each_is_read_into_its_own(self, tmp_path):
        tracks = [_sub(3, "S_HDMV/PGS", "eng", 2000),
                  _sub(4, "S_HDMV/PGS", "eng", 2200, hearing="true")]
        assert _jobs(str(tmp_path / "F"), tracks) == [
            ("3", "en", "F.en.srt"), ("4", "en", "F.en.sdh.srt")]

    def test_an_sdh_text_subtitle_stands_for_the_plain_one(self, tmp_path):
        tracks = [_sub(3, "S_HDMV/PGS", "eng", 2000, hearing="true"),
                  _sub(4, "S_TEXT/UTF8", "eng", 1000, name="English SDH")]
        assert _jobs(str(tmp_path / "F"), tracks) == []

    def test_the_longest_of_a_role_is_the_one(self, tmp_path):
        tracks = [_sub(3, "S_HDMV/PGS", "eng", 1800),
                  _sub(4, "S_HDMV/PGS", "eng", 2200)]
        assert _jobs(str(tmp_path / "F"), tracks) == [("4", "en", "F.en.srt")]

    @pytest.mark.parametrize("track", [
        _sub(3, "S_HDMV/PGS", "eng", 2000, name="Commentary"),
        _sub(3, "S_DVBSUB", "eng", 1000),
    ])
    def test_a_commentary_and_a_broadcast_subtitle_stay_pictures(
            self, tmp_path, track):
        assert _jobs(str(tmp_path / "F"), [track]) == []


@pytest.mark.fs
class TestTheSidecarsGathered:
    _PLAIN = "1\n00:00:01,000 --> 00:00:02,000\nHello.\n"

    def _write(self, path, text=_PLAIN):
        with open(path, "w") as handle:
            handle.write(text)

    def test_each_role_is_gathered_with_it(self, tmp_path):
        base = str(tmp_path / "F")
        for name in (".fr.srt", ".fr.forced.srt", ".en.sdh.srt"):
            self._write(base + name)
        assert rules.gather_downloaded_subtitles(base, [], say=print) == [
            (base + ".en.sdh.srt", "en", "sdh"),
            (base + ".fr.srt", "fr", "full"),
            (base + ".fr.forced.srt", "fr", "forced")]

    def test_a_role_the_film_has_as_text_is_left_out(self, tmp_path):
        base = str(tmp_path / "F")
        self._write(base + ".en.forced.srt")
        said = []
        assert rules.gather_downloaded_subtitles(
            base, [_sub(6, "S_TEXT/UTF8", "eng", name="Forced")],
            say=said.append) == []
        assert said == ["  English forced subtitle already a text track in "
                        "the file, not muxing the sidecar: F.en.forced.srt"]


@pytest.mark.fs
class TestTheRemuxReadsThem:
    def _read(self, monkeypatch, tmp_path, available=True, omit=frozenset()):
        base = str(tmp_path / "Film (2006)")
        calls, lines = [], []
        monkeypatch.setattr(run_module.subtitleocr, "available",
                            lambda: available)

        def extract(movie, tracks, scratch):
            calls.append(("extract", list(tracks)))
            return {number: os.path.join(scratch, number + ".sup")
                    for number, _codec in tracks if number != "4"}

        def recognise(image, row, srt, log):
            calls.append(("read", os.path.basename(image), row.code2,
                          os.path.basename(srt)))
            with open(srt, "w") as handle:
                handle.write("1\n00:00:01,000 --> 00:00:02,000\nOui\n")
            return "tesseract"

        monkeypatch.setattr(run_module.subtitleocr, "extract", extract)
        monkeypatch.setattr(run_module.subtitleocr, "recognise", recognise)
        state = run_module.Run(subtitle_languages=(ENGLISH, FRENCH),
                               omit_phases=omit)
        state.read_image_subtitles(base + ".mkv", base, _film(),
                                   lines.append)
        return calls, lines

    def test_every_track_comes_out_at_once_and_is_read_into_its_sidecar(
            self, monkeypatch, tmp_path):
        calls, lines = self._read(monkeypatch, tmp_path)
        assert calls == [
            ("extract", [("4", "S_HDMV/PGS"), ("3", "S_HDMV/PGS"),
                         ("5", "S_HDMV/PGS")]),
            ("read", "3.sup", "fr", "Film (2006).fr.srt"),
            ("read", "5.sup", "fr", "Film (2006).fr.forced.srt")]
        assert lines == [
            "  WARNING: English picture subtitle (track 4) could not be "
            "taken out of the film to read",
            "  Read the French picture subtitle (track 3) by tesseract OCR: "
            "1 lines -> Film (2006).fr.srt",
            "  Read the French picture subtitle (track 5) by tesseract OCR: "
            "1 lines -> Film (2006).fr.forced.srt"]

    def test_without_seconv_nothing_is_read(self, monkeypatch, tmp_path):
        assert self._read(monkeypatch, tmp_path, available=False) \
            == ([], [])
