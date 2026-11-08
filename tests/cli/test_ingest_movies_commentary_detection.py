"""The full ingest's listening for commentaries the files do not flag: which
films and tracks it asks the probe about, what it writes back into a film, and
what it leaves for the end of the run.

The probe and mkvpropedit are stood in for: what is pinned is the run's part -
the tracks handed over, the header edit made of a verdict, and the line a track
it was unsure of leaves behind.
"""

from types import SimpleNamespace

import pytest

from medialib.cli import ingest_movies as rules
from medialib.cli import ingest_movies_run as run_module
from medialib.lib import commentarydetection as cd

pytestmark = pytest.mark.pure

MOVIE = "/library/Film (1970)/Film (1970).mkv"

FILM = cd.Measure(0.81, "where were you tonight harbour captain listen", "en")
TALKED_OVER = cd.Measure(0.94, "we shot this sequence over three nights "
                         "because weather kept changing", "en")
DOWNMIX = FILM._replace(speech=0.82)
SAME_SPEECH_OTHER_WORDS = TALKED_OVER._replace(speech=0.80)


def _track(id_, type_, codec="", channels="", name="", commentary="false",
           visual="false"):
    return rules.Track(id=id_, type=type_, codec=codec, channels=channels,
                       language="eng", name=name, commentary=commentary,
                       visual_impaired=visual)


@pytest.fixture
def ingest(monkeypatch):
    """The run with one film whose tracks the case gives, and the probe's
    answers by track id."""
    edits: list = []
    probed: list = []

    def set_up(tracks, answers, films=(MOVIE,)):
        monkeypatch.setattr(run_module, "main_movies", lambda _root: list(films))
        monkeypatch.setattr(rules, "_identify", lambda _movie: tracks)

        def probe(movie, main, others, whisper):
            probed.append((movie, main, others))
            return {t.id: answers[t.id] for t in [main] + others}, ""

        monkeypatch.setattr(cd, "probe", probe)
        monkeypatch.setattr(rules, "_run_capture",
                            lambda argv: edits.append(argv) or "")
        state = SimpleNamespace(whisper={"modelMulti": "x"}, whisper_said=[],
                                unsure_commentaries=[])
        run_module._detect_commentaries(state, "/library")
        return state

    return SimpleNamespace(run=set_up, edits=edits, probed=probed)


def test_a_talked_over_track_is_flagged_and_numbered_in_place(ingest):
    tracks = [_track("0", "video"),
              _track("1", "audio", "A_EAC3", "6", "Surround 5.1"),
              _track("2", "audio", "A_AC3", "2", "Mono"),
              _track("3", "audio", "A_AC3", "2", "English")]
    ingest.run(tracks, {"1": FILM, "2": DOWNMIX, "3": TALKED_OVER})

    _movie, main, others = ingest.probed[0]
    assert main.id == "1" and main.audio_index == 0
    assert [(t.id, t.audio_index) for t in others] == [("2", 1), ("3", 2)]
    assert ingest.edits == [["mkvpropedit", MOVIE, "--edit", "track:4",
                             "--set", "name=Commentary 1",
                             "--set", "flag-commentary=1"]]


def test_an_unsure_track_is_left_as_it_is_and_listed(ingest):
    tracks = [_track("0", "audio", "A_TRUEHD", "8"),
              _track("1", "audio", "A_AC3", "2", "Stereo")]
    state = ingest.run(tracks, {"0": FILM, "1": SAME_SPEECH_OTHER_WORDS})
    assert ingest.edits == []
    assert len(state.unsure_commentaries) == 1
    assert state.unsure_commentaries[0].startswith(MOVIE + ": audio track 1")


def test_a_film_with_no_suspect_is_not_listened_to(ingest):
    tracks = [_track("0", "audio", "A_TRUEHD", "8"),
              _track("1", "audio", "A_AC3", "2", "Commentary",
                     commentary="true"),
              _track("2", "audio", "A_AC3", "2", visual="true")]
    ingest.run(tracks, {})
    assert ingest.probed == [] and ingest.edits == []


def test_a_probe_that_failed_leaves_the_film_as_it_is(monkeypatch, capsys):
    tracks = [_track("0", "audio", "A_TRUEHD", "8"),
              _track("1", "audio", "A_AC3", "2")]
    monkeypatch.setattr(cd, "probe", lambda *_a: ({}, "the probe failed (1)"))
    monkeypatch.setattr(run_module, "main_movies", lambda _root: [MOVIE])
    monkeypatch.setattr(rules, "_identify", lambda _movie: tracks)
    edits = []
    monkeypatch.setattr(rules, "_run_capture",
                        lambda argv: edits.append(argv) or "")
    state = SimpleNamespace(whisper={}, whisper_said=[],
                            unsure_commentaries=[])
    run_module._detect_commentaries(state, "/library")
    assert edits == []
    assert "the probe failed (1)" in capsys.readouterr().err


def test_the_unsure_tracks_are_named_at_the_end_of_the_run(capsys):
    run_module._report_unsure_commentaries(["film.mkv: audio track 2 - x"])
    assert "  film.mkv: audio track 2 - x" in capsys.readouterr().err


def test_no_unsure_track_says_nothing(capsys):
    run_module._report_unsure_commentaries([])
    assert capsys.readouterr().err == ""
