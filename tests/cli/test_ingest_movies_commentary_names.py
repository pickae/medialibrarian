"""The commentary naming run: one film's numbered commentary tracks named after
the disc's listing, with their transcripts following them.

The deciding is `tests/lib/test_commentarynames.py`'s. What is pinned here is
what the run does with a decision on disk - which tracks mkvpropedit is asked
to rename, which sidecars follow, and that a dry run touches nothing.
"""

import os

import pytest

from medialib.cli import ingest_movies as im
from medialib.cli import ingest_movies_run as run_module
from medialib.lib.commentarynames import Disc, Release

pytestmark = pytest.mark.fs

FILM = "Nightfall Harbour (1987)"
DIRECTOR = "Audio commentary by director Wenna Castellane"
CAST = "Audio commentary with actors Oriel Pask and Tamsin Voller"


def _tracks(*commentary_names, subtitles=()):
    tracks = [im.Track(id="0", type="video", codec="V_MPEG4/ISO/AVC",
                       dimensions="1920x1080"),
              im.Track(id="1", type="audio", codec="A_DTS", name="Surround")]
    for name in commentary_names:
        tracks.append(im.Track(id=str(len(tracks)), type="audio",
                               codec="A_AC3", name=name))
    for name in subtitles:
        tracks.append(im.Track(id=str(len(tracks)), type="subtitles",
                               codec="S_TEXT/UTF8", name=name,
                               commentary="true"))
    return tracks


def _srt(text):
    return "1\n00:00:10,000 --> 00:00:12,000\n%s\n" % text


@pytest.fixture
def film(tmp_path, monkeypatch):
    """A film folder whose movie reads as the tracks a case gives, with every
    mkvpropedit call it would make recorded rather than run."""
    folder = tmp_path / FILM
    folder.mkdir()
    movie = folder / (FILM + ".mkv")
    movie.write_bytes(b"")
    calls = []
    monkeypatch.setattr(run_module, "log", lambda *a, **k: None)
    monkeypatch.setattr(run_module.rules, "_run_capture",
                        lambda argv: calls.append(argv) or "")

    def given(tracks, releases):
        monkeypatch.setattr(run_module.rules, "_identify", lambda _m: tracks)
        return str(movie), (lambda _title, _year, _kind: releases), calls
    return given


def _name(movie, lookup, write):
    """The naming run over the film's library as the full ingest runs it,
    one film at a time: the list it leaves, "" when it leaves none."""
    root = os.path.dirname(os.path.dirname(movie))
    script = os.path.join(root, "script")
    run_module._commentary_names_in(root, "Films", write, lookup, "", script,
                                    queued=False)
    listing = os.path.join(script, "logs", "ingest-movies",
                           "ingest-movies-commentarynames-Films.txt")
    if not os.path.exists(listing):
        return ""
    with open(listing, encoding="utf-8") as handle:
        return handle.read()


def test_a_single_commentary_is_named_and_its_transcripts_follow(film):
    movie, lookup, calls = film(
        _tracks("Commentary", subtitles=["Commentary"]),
        [Release("Region A", (Disc("bluray", (DIRECTOR,)),))])
    folder = os.path.dirname(movie)
    srt = os.path.join(folder, FILM + " 2 Commentary.en.srt")
    with open(srt, "w", encoding="utf-8") as handle:
        handle.write(_srt("Hello."))

    listing = _name(movie, lookup, True)

    assert listing.startswith("# Named:\n") and "Left alone" not in listing
    assert calls == [["mkvpropedit", movie,
                      "--edit", "track:3", "--set",
                      "name=Commentary by director Wenna Castellane",
                      "--set", "flag-commentary=1",
                      "--edit", "track:4", "--set",
                      "name=Commentary by director Wenna Castellane"]]
    assert sorted(os.listdir(folder)) == sorted([
        FILM + ".mkv",
        FILM + " 2 Commentary by director Wenna Castellane.en.srt"])


def test_a_dry_run_changes_nothing(film):
    movie, lookup, calls = film(
        _tracks("Commentary"),
        [Release("Region A", (Disc("bluray", (DIRECTOR,)),))])
    folder = os.path.dirname(movie)
    srt = os.path.join(folder, FILM + " 2 Commentary.en.srt")
    with open(srt, "w", encoding="utf-8") as handle:
        handle.write(_srt("Hello."))

    listing = _name(movie, lookup, False)

    assert listing.startswith("# Would be named:\n") and calls == []
    assert os.path.isfile(srt)


def test_several_are_named_by_their_transcripts_openings(film):
    movie, lookup, calls = film(
        _tracks("Commentary 1", "Commentary 2"),
        [Release("Region A", (Disc("bluray", (DIRECTOR, CAST)),))])
    folder = os.path.dirname(movie)
    for track_id, name, text in (
            ("2", "Commentary 1", "Hi, I'm Wenna Castellane."),
            ("3", "Commentary 2", "This is Oriel Pask, here with Tamsin "
                                  "Voller.")):
        with open(os.path.join(folder, "%s %s %s.en.srt"
                               % (FILM, track_id, name)),
                  "w", encoding="utf-8") as handle:
            handle.write(_srt(text))

    listing = _name(movie, lookup, True)

    assert "Left alone" not in listing
    [argv] = calls
    assert "name=Commentary by director Wenna Castellane" in argv
    assert "name=Commentary with actors Oriel Pask and Tamsin Voller" in argv


def test_several_without_transcripts_are_left_alone(film):
    movie, lookup, calls = film(
        _tracks("Commentary 1", "Commentary 2"),
        [Release("Region A", (Disc("bluray", (DIRECTOR, CAST)),))])

    listing = _name(movie, lookup, True)

    assert listing.startswith("# Left alone:\n")
    assert "no transcript" in listing and calls == []


def test_an_already_named_film_is_not_looked_up_or_listed(film):
    asked = []
    movie, _lookup, calls = film(_tracks("Director's Commentary"), [])

    listing = _name(movie, lambda *a: asked.append(a), True)

    assert (listing, asked, calls) == ("", [], [])


def test_the_film_is_looked_up_by_its_folder_s_title_and_year(film):
    asked = []
    movie, _lookup, _calls = film(_tracks("Commentary"), [])
    _name(movie, lambda *a: asked.append(a) or [], False)
    assert asked == [("Nightfall Harbour", "1987", "bluray")]


def test_a_subtitle_no_commentary_owns_leaves_the_film_alone(film, tmp_path):
    movie, lookup, calls = film(
        _tracks("Commentary 1", "Commentary 2", subtitles=["Commentary 3"]),
        [Release("Region A", (Disc("bluray", (DIRECTOR, CAST)),))])
    folder = os.path.dirname(movie)
    for track_id, name, text in (
            ("2", "Commentary 1", "Hi, I'm Wenna Castellane."),
            ("3", "Commentary 2", "This is Oriel Pask, here with Tamsin "
                                  "Voller.")):
        with open(os.path.join(folder, "%s %s %s.en.srt"
                               % (FILM, track_id, name)),
                  "w", encoding="utf-8") as handle:
            handle.write(_srt(text))
    script = tmp_path / "script"

    run_module._commentary_names_in(str(tmp_path), "Films", True, lookup, "",
                                    str(script), queued=False)

    listing = (script / "logs" / "ingest-movies"
               / "ingest-movies-commentarynames-Films.txt")
    text = listing.read_text(encoding="utf-8")
    assert text.startswith("# Left alone:\n")
    assert './%s/%s.mkv\n' % (FILM, FILM) in text
    assert 'subtitle "Commentary 3" cannot be told which commentary' in text
    assert calls == []


def test_each_film_is_announced_with_the_renames_under_it(film,
                                                          monkeypatch):
    movie, lookup, _calls = film(
        _tracks("Commentary"),
        [Release("Region A", (Disc("bluray", (DIRECTOR,)),))])
    lines = []
    monkeypatch.setattr(run_module, "log", lines.append)
    _name(movie, lookup, False)
    assert lines[0] == "[1/1] " + os.path.join(FILM, FILM + ".mkv")
    assert lines[1] == ('  track 2 "Commentary" -> "Commentary by director '
                        'Wenna Castellane"')


def test_a_film_with_nothing_to_name_says_so_and_stays_off_the_list(
        film, monkeypatch):
    movie, lookup, _calls = film(_tracks(), [])
    lines = []
    monkeypatch.setattr(run_module, "log", lines.append)
    assert _name(movie, lookup, True) == ""
    assert lines == ["[1/1] " + os.path.join(FILM, FILM + ".mkv"),
                     "  left alone: no commentary track"]
