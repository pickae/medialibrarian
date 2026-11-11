"""How often ingest-movies asks a film what it holds.

Every pass reads the film's tracks, and the remux its picture and sound as
mediainfo reports them; one film is asked once for each until a pass writes
it, and asked again after. The probes are scripts on PATH that note each call,
so what is counted is processes started; the writers are stood in for by stubs
that leave the file as it was, which is the case only forgetting catches.
"""

import json
import os
from types import SimpleNamespace

import pytest

from medialib.cli import ingest_movies as rules
from medialib.cli import ingest_movies_run as run_module
from medialib.lib import chapterdb, filmrecord, plexcompare

pytestmark = pytest.mark.stubbed

TRACKS = {"tracks": [
    {"id": 0, "type": "video",
     "properties": {"codec_id": "V_MPEG4/ISO/AVC",
                    "pixel_dimensions": "1920x1080"}},
    {"id": 1, "type": "audio",
     "properties": {"codec_id": "A_FLAC", "audio_channels": 2,
                    "language": "eng"}},
]}
MEDIAINFO = {"media": {"track": [
    {"@type": "Video", "Format": "AVC"},
    {"@type": "Audio", "Format": "FLAC"},
]}}


@pytest.fixture
def film(tmp_path, monkeypatch):
    """One film, and mkvmerge and mediainfo on PATH answering for it."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    calls = tmp_path / "calls"
    answers = {"mkvmerge": TRACKS, "mediainfo": MEDIAINFO}
    for name, answer in answers.items():
        (tmp_path / (name + ".json")).write_text(json.dumps(answer))
        tool = bin_dir / name
        tool.write_text("#!/bin/sh\necho %s >> '%s'\ncat '%s'\n"
                        % (name, calls, tmp_path / (name + ".json")),
                        encoding="ascii")
        tool.chmod(0o755)
    monkeypatch.setenv("PATH", "%s%s%s" % (bin_dir, os.pathsep,
                                           os.environ["PATH"]))
    folder = tmp_path / "library" / "Film (1970)"
    folder.mkdir(parents=True)
    movie = folder / "Film (1970).mkv"
    movie.write_bytes(b"\x1a\x45\xdf\xa3 a film")

    def asked(name: str) -> int:
        try:
            return calls.read_text().split().count(name)
        except FileNotFoundError:
            return 0

    return SimpleNamespace(path=str(movie), root=str(tmp_path / "library"),
                           asked=asked)


def test_the_tracks_are_read_once_across_passes(film):
    first = rules._identify(film.path)
    second = rules._identify(film.path)
    assert [t.type for t in first] == ["video", "audio"]
    assert [t.codec for t in second] == ["V_MPEG4/ISO/AVC", "A_FLAC"]
    assert film.asked("mkvmerge") == 1


def test_each_read_hands_out_tracks_of_its_own(film):
    """The remux marks what it does with each track on the track itself; the
    next pass must not be handed those marks."""
    rules._identify(film.path)[1].action = "drop"
    assert rules._identify(film.path)[1].action == "keep"


def test_the_remux_asks_mediainfo_once(film):
    """The object-audio flags and the Dolby Vision decision read the same
    mediainfo document."""
    tracks = rules._identify(film.path)
    rules._object_flags(film.path, tracks)
    state = run_module.Run(script_dir="")
    state.decide_dolby_vision_job(film.path, tracks)
    assert film.asked("mediainfo") == 1


def test_the_plex_comparison_reads_what_the_passes_read(film):
    rules._identify(film.path)
    rules._object_flags(film.path, rules._identify(film.path))
    assert plexcompare.read_profile(film.path) is not None
    assert film.asked("mkvmerge") == 1
    assert film.asked("mediainfo") == 1


def test_the_transcode_worker_reads_the_tracks_it_was_handed(film,
                                                             monkeypatch):
    """A worker is handed the film's record with the film: the tracks the run
    read before the pool started are not read again in the worker."""
    rules._identify(film.path)
    handed = (film.path, filmrecord.of(film.path))
    filmrecord.reset()
    seen = []
    monkeypatch.setattr(rules, "check_audio_tracks", lambda movie, root:
                        seen.append(rules._identify(movie)))
    run_module._check_audio(film.root, "", handed)
    assert [t.codec for t in seen[0]] == ["V_MPEG4/ISO/AVC", "A_FLAC"]
    assert film.asked("mkvmerge") == 1


def test_the_tagging_reads_the_tracks_after_its_own_edit(film, monkeypatch):
    """The title edit comes first, and the flags are worked out from what the
    film says after it - not from what an earlier pass read."""
    rules._identify(film.path)
    monkeypatch.setattr(rules, "_run", lambda argv: 0)
    monkeypatch.setattr(rules, "_run_capture", lambda argv: "")
    rules.update_tags(film.root)
    assert film.asked("mkvmerge") == 2
    rules._identify(film.path)
    assert film.asked("mkvmerge") == 3


def test_flagging_a_commentary_forgets_the_tracks(film, monkeypatch):
    tracks = rules._identify(film.path)
    monkeypatch.setattr(rules, "_run_capture", lambda argv: "")
    run_module._flag_commentaries(film.path, tracks, {"1": "Commentary 1"})
    rules._identify(film.path)
    assert film.asked("mkvmerge") == 2


def test_naming_the_commentaries_forgets_the_tracks(film, monkeypatch):
    rules._identify(film.path)
    monkeypatch.setattr(rules, "_run_capture", lambda argv: "")
    run_module._carry_out_commentary_names(
        {"movie": film.path, "arguments": [], "sidecars": []})
    rules._identify(film.path)
    assert film.asked("mkvmerge") == 2


def test_a_worker_writing_a_film_has_this_process_forget_it(film,
                                                            monkeypatch):
    """The names and the chapters are written by a worker; what this process
    read of the film goes once the worker is done, whatever the file system's
    time stamps say."""
    rules._identify(film.path)
    monkeypatch.setattr(run_module.dynamicqueue, "run",
                        lambda items, *a, **k: list(items))
    run_module._apply_as_prepared(iter([(film.path, "work")]),
                                  lambda work: "")
    rules._identify(film.path)
    assert film.asked("mkvmerge") == 2


def test_writing_chapters_forgets_the_tracks(film, monkeypatch):
    rules._identify(film.path)
    done = SimpleNamespace(returncode=0, stdout=b"")
    monkeypatch.setattr(chapterdb.toolcapture, "watched",
                        lambda argv, **k: done)
    monkeypatch.setattr(chapterdb, "existing_chapters", lambda movie: None)
    chapterdb.write_chapters(film.path, chapterdb.ChapterSet(
        "1", "", "", "eng", 0, 0.0, ()))
    rules._identify(film.path)
    assert film.asked("mkvmerge") == 2


def test_the_improved_copy_swapped_in_is_read_afresh(film, tmp_path):
    rules._identify(film.path)
    new = tmp_path / "improved.mkv"
    new.write_bytes(b"\x1a\x45\xdf\xa3 a film")
    old = os.path.splitext(film.path)[0] + " (old).mkv"
    assert run_module._swap_in(str(new), film.path, old) == ""
    rules._identify(film.path)
    assert film.asked("mkvmerge") == 2
