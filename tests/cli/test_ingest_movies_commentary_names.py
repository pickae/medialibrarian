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
        FILM + ".2 Commentary by director Wenna Castellane.en.srt"])


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


def test_model_fallback_records_decision_in_output_and_report_file(
        film, tmp_path, monkeypatch):
    from medialib.lib import aicommentarynames

    movie, lookup, calls = film(
        _tracks("Commentary 1", "Commentary 2"),
        [Release("Region A", (Disc("bluray", (DIRECTOR, CAST)),))])
    folder = os.path.dirname(movie)
    for track_id, name, text in (
            ("2", "Commentary 1", "Guten Abend allerseits."),
            ("3", "Commentary 2", "Willkommen zu diesem Film.")):
        with open(os.path.join(folder, "%s %s %s.en.srt"
                               % (FILM, track_id, name)),
                  "w", encoding="utf-8") as handle:
            handle.write(_srt(text))

    lines = []
    monkeypatch.setattr(run_module, "log", lines.append)
    monkeypatch.setattr(
        aicommentarynames, "match_tracks",
        lambda *_a, **_k: (0, (1, 0),
                           "track order chosen by stub-model: swapped German intros"))

    root = os.path.dirname(folder)
    script = os.path.join(root, "script")
    run_module._commentary_names_in(root, "Films", True, lookup, "", script,
                                    queued=False)
    listing = os.path.join(script, "logs", "ingest-movies",
                           "ingest-movies-commentarynames-Films.txt")
    with open(listing, encoding="utf-8") as handle:
        text = handle.read()

    assert "track order chosen by stub-model: swapped German intros" in text
    assert any("stub-model" in line for line in lines)
    assert len(calls) == 1
    assert "name=Commentary with actors Oriel Pask and Tamsin Voller" in calls[0]


def test_model_unsure_keeps_fallback_reason_and_reports_model_note(
        film, monkeypatch):
    from medialib.lib import aicommentarynames

    movie, lookup, calls = film(
        _tracks("Commentary 1", "Commentary 2"),
        [Release("Region A", (Disc("bluray", (DIRECTOR, CAST)),))])
    folder = os.path.dirname(movie)
    for track_id, name, text in (
            ("2", "Commentary 1", "Opening music."),
            ("3", "Commentary 2", "More music.")):
        with open(os.path.join(folder, "%s %s %s.en.srt"
                               % (FILM, track_id, name)),
                  "w", encoding="utf-8") as handle:
            handle.write(_srt(text))

    lines = []
    monkeypatch.setattr(run_module, "log", lines.append)
    monkeypatch.setattr(
        aicommentarynames, "match_tracks",
        lambda *_a, **_k: (None, None, "stub-model unsure: no speakers heard"))

    listing = _name(movie, lookup, True)
    assert listing.startswith("# Left alone:\n")
    assert "stub-model unsure: no speakers heard" in listing
    assert any("stub-model unsure" in line for line in lines)
    assert calls == []


def test_site_page_pick_passes_tmdb_info_and_records_decision(
        tmp_path, monkeypatch):
    from medialib.lib import aicommentarynames, dvdcompare, llmroster, tmdbidentify

    tagged = FILM + " {tmdb-55123}"
    folder = tmp_path / tagged
    folder.mkdir()
    movie = folder / (FILM + ".mkv")
    movie.write_bytes(b"")

    monkeypatch.setenv(aicommentarynames.SWITCH_VARIABLE, "on")
    monkeypatch.setattr(run_module.rules, "_identify",
                        lambda _m: _tracks("Commentary"))
    monkeypatch.setattr(run_module.rules, "_run_capture", lambda _argv: "")
    monkeypatch.setattr(llmroster, "optional", lambda _task: ([object()], ""))
    monkeypatch.setattr(
        tmdbidentify, "film_info",
        lambda title, year, tag="": tmdbidentify.FilmInfo(
            tmdb="55123", imdb="tt0093123", aliases=("Nachthafen",)))

    seen_pick = {}

    def fake_pick(fitting, title, year, kind, *, tmdb_id="", imdb_id="",
                  aliases=(), **_kwargs):
        seen_pick["ids"] = [e.fid for e in fitting]
        seen_pick["tmdb_id"] = tmdb_id
        seen_pick["imdb_id"] = imdb_id
        seen_pick["aliases"] = aliases
        return "202", "disc page fid=202 chosen by stub-model: matches TMDb 55123"

    monkeypatch.setattr(aicommentarynames, "pick_page", fake_pick)

    two_search = (
        '<li><a href="film.php?fid=101">Nightfall Harbour (Blu-ray) (1987)</a></li>'
        '<li><a href="film.php?fid=202">Nightfall Harbour (Blu-ray) (1987)</a></li>'
    )
    from tests import blackbox
    film_html = (blackbox.DATA / "dvdcompare" / "film.html").read_text(encoding="utf-8")

    monkeypatch.setattr(
        dvdcompare, "_curl",
        lambda url, _form=None: two_search if url.endswith("search.php") else film_html)

    lines = []
    monkeypatch.setattr(run_module, "log", lines.append)
    script = str(tmp_path / "script")
    usage = aicommentarynames.Usage()
    film_tags: dict[tuple[str, str], str] = {}
    site = run_module._make_dvdcompare_site(script, lines.append, usage, film_tags)

    run_module._commentary_names_in(
        str(tmp_path), "Films", False, site.releases, "", script,
        queued=False, site=site, usage=usage, film_tags=film_tags)

    listing = (tmp_path / "script" / "logs" / "ingest-movies"
               / "ingest-movies-commentarynames-Films.txt").read_text(encoding="utf-8")
    assert seen_pick == {
        "ids": ["101", "202"],
        "tmdb_id": "55123",
        "imdb_id": "tt0093123",
        "aliases": ("Nachthafen",),
    }
    assert "disc page fid=202 chosen by stub-model: matches TMDb 55123" in listing
    assert any("disc page fid=202 chosen by stub-model" in line for line in lines)


def test_intro_naming_analyses_once_and_renames_audio_embedded_and_sidecar_translations(
        film, monkeypatch):
    movie, lookup, calls = film(
        _tracks("Commentary",
                subtitles=["Commentary (EN)", "Commentary (DE)"]),
        None)
    folder = os.path.dirname(movie)
    for filename, text in (
        (FILM + " 2 Commentary.en.srt", "Hi, I'm director Wenna Castellane."),
        (FILM + " 2 Commentary.de.srt", "Hallo, ich bin Regisseurin Wenna Castellane."),
        (FILM + ".Commentary.fr.srt", "Bonjour, ici Wenna Castellane."),
    ):
        with open(os.path.join(folder, filename), "w", encoding="utf-8") as handle:
            handle.write(_srt(text))

    intro_calls = []
    transcribe_calls = []

    def fake_intro(opening, *, title, year, track_name):
        intro_calls.append((opening, title, year, track_name))
        return ("Commentary by director Wenna Castellane",
                "named from opening 5 min by stub-model")

    plan, why = run_module._plan_commentary_names(
        movie, FILM, lookup, "",
        intro_namer=fake_intro,
        transcribe_opening=lambda *a: transcribe_calls.append(a) or ({}, ""))

    assert why == "" and plan is not None
    # Analysed strictly once (prefers English sidecar) and never transcribed audio.
    assert len(intro_calls) == 1
    assert "Wenna Castellane" in intro_calls[0][0]
    assert transcribe_calls == []

    assert run_module._carry_out_commentary_names(plan) == ""
    assert calls == [["mkvpropedit", movie,
                      "--edit", "track:3", "--set",
                      "name=Commentary by director Wenna Castellane",
                      "--set", "flag-commentary=1",
                      "--edit", "track:4", "--set",
                      "name=Commentary by director Wenna Castellane (EN)",
                      "--edit", "track:5", "--set",
                      "name=Commentary by director Wenna Castellane (DE)"]]
    assert sorted(os.listdir(folder)) == sorted([
        FILM + ".mkv",
        FILM + ".2 Commentary by director Wenna Castellane.en.srt",
        FILM + ".2 Commentary by director Wenna Castellane.de.srt",
        FILM + ".Commentary by director Wenna Castellane.fr.srt",
    ])


def test_intro_naming_transcribes_opening_when_no_subtitles_exist(film):
    from medialib.lib import commentarydetection

    movie, lookup, calls = film(_tracks("Commentary 1"), None)
    transcribed = []

    def fake_transcribe(_movie, audio_tracks):
        transcribed.append([t.id for t in audio_tracks])
        return ({
            "2": commentarydetection.Measure(
                0.8, "Welcome, this is actor Oriel Pask.", "en"),
        }, "")

    intro_calls = []
    plan, why = run_module._plan_commentary_names(
        movie, FILM, lookup, "",
        intro_namer=lambda text, **_k: intro_calls.append(text) or (
            "Commentary with actor Oriel Pask", "named from opening 5 min"),
        transcribe_opening=fake_transcribe)

    assert why == "" and plan is not None
    assert transcribed == [["2"]]
    assert len(intro_calls) == 1
    assert run_module._carry_out_commentary_names(plan) == ""
    assert calls == [["mkvpropedit", movie,
                      "--edit", "track:3", "--set",
                      "name=Commentary with actor Oriel Pask",
                      "--set", "flag-commentary=1"]]


def test_intro_naming_extracts_one_embedded_subtitle_and_renames_all_translations(film):
    movie, lookup, calls = film(
        _tracks("Commentary 1",
                subtitles=["Commentary 1 (EN)", "Commentary 1 (DE)"]),
        None)
    extracted = []
    transcribe_calls = []

    def fake_extract(_movie, track_id):
        extracted.append(track_id)
        return _srt("Hello, I'm film historian Maren Halvorsen.")

    plan, why = run_module._plan_commentary_names(
        movie, FILM, lookup, "",
        extract_subtitle=fake_extract,
        transcribe_opening=lambda *a: transcribe_calls.append(a) or ({}, ""),
        intro_namer=lambda *_a, **_k: (
            "Commentary by film historian Maren Halvorsen",
            "named from opening 5 min"))

    assert why == "" and plan is not None
    assert len(extracted) == 1 and transcribe_calls == []
    assert run_module._carry_out_commentary_names(plan) == ""
    assert calls == [["mkvpropedit", movie,
                      "--edit", "track:3", "--set",
                      "name=Commentary by film historian Maren Halvorsen",
                      "--set", "flag-commentary=1",
                      "--edit", "track:4", "--set",
                      "name=Commentary by film historian Maren Halvorsen (EN)",
                      "--edit", "track:5", "--set",
                      "name=Commentary by film historian Maren Halvorsen (DE)"]]


def test_intro_naming_handles_mixed_named_and_generic_tracks(film):
    movie, lookup, calls = film(
        _tracks("Director's Commentary", "Commentary 2",
                subtitles=["Commentary 2 (EN)"]),
        None)
    folder = os.path.dirname(movie)
    with open(os.path.join(folder, FILM + " 3 Commentary 2.en.srt"),
              "w", encoding="utf-8") as handle:
        handle.write(_srt("Hi, we are Oriel Pask and Tamsin Voller."))

    plan, why = run_module._plan_commentary_names(
        movie, FILM, lookup, "",
        intro_namer=lambda *_a, **_k: (
            "Commentary with actors Oriel Pask and Tamsin Voller",
            "named from opening 5 min"))

    assert why == "" and plan is not None
    assert run_module._carry_out_commentary_names(plan) == ""
    assert calls == [["mkvpropedit", movie,
                      "--edit", "track:4", "--set",
                      "name=Commentary with actors Oriel Pask and Tamsin Voller",
                      "--set", "flag-commentary=1",
                      "--edit", "track:5", "--set",
                      "name=Commentary with actors Oriel Pask and Tamsin Voller (EN)"]]



