"""The improved-copy remux: which tracks survive, and the mkvmerge command built
from that.

Pure decision logic - only the final byte-level remux needs real media, and that
is the opt-in tier - so mkvmerge is stubbed to record the argv it was handed.

The fixture exercises every rule group at once, because the rules interact: the
ladder only sees what the language rule left, and the commentary exemption has to
survive both.
"""

import os

import pytest

from medialib.cli import ingest_movies as rules
from medialib.cli import ingest_movies_run as run_module

pytestmark = pytest.mark.fs

# id, type, codec, channels, language, name, commentary, default
FIXTURE = [
    (0, "audio", "A_TRUEHD", "8", "eng", "TrueHD 7.1", "false", "true"),
    (1, "audio", "A_FLAC", "1", "eng", "FLAC Mono", "false", "false"),
    (2, "audio", "A_AC3", "6", "ger", "AC3 German", "false", "false"),
    (3, "audio", "A_EAC3", "6", "eng", "EAC3 5.1", "false", "false"),
    (4, "audio", "A_AC3", "6", "eng", "AC3 5.1", "false", "false"),
    (5, "subtitles", "S_HDMV/PGS", "null", "ger", "PGS German", "false",
     "false"),
    (6, "subtitles", "S_TEXT/UTF8", "null", "eng", "English", "false",
     "false"),
    (7, "audio", "A_TRUEHD", "6", "eng", "Commentary by Director", "true",
     "false"),
    (8, "audio", "A_AC3", "2", "nld", "Commentary Dutch", "true", "false"),
    (9, "audio", "A_DTS", "5", "zxx", "Isolated Score", "false", "false"),
    (10, "audio", "A_EAC3", "6", "eng", "EAC3 Atmos 5.1", "false", "false"),
    (11, "subtitles", "S_TEXT/UTF8", "null", "eng", "Commentary by Director",
     "false", "false"),
]


@pytest.fixture
def remux(tmp_path, monkeypatch):
    """One film through the improvement, with mkvmerge recording rather than
    running: what comes back is the argv it was handed."""
    folder = tmp_path / "Film (2020)"
    folder.mkdir()
    movie = str(folder / "Film (2020).mkv")
    base = str(folder / "Film (2020)")
    open(movie, "w").close()

    # The sidecars that drive the swap, the FLAC exception and the appends.
    for name in ("_0.opus",                       # track 0 -> swap
                 "_1.opus",                       # track 1 transcoded, FLAC 1.0
                 " 7 Commentary by Director.srt",  # a legacy transcript
                 " 8 Commentary Dutch.nl.srt",     # the native transcript
                 " 8 Commentary Dutch.en.srt"):    # and its translation
        open(base + name, "w").close()

    tracks = [rules.Track(id=str(id), type=kind, codec=codec,
                          channels=channels, language=language, name=name,
                          commentary=commentary, default=default,
                          forced="false")
              for id, kind, codec, channels, language, name, commentary,
              default in FIXTURE]
    monkeypatch.setattr(rules, "_identify", lambda path: tracks)
    # An empty fixture movie: mediainfo reports no JOC metadata, so an object
    # flag can only come from the track NAME - the last-resort path.
    monkeypatch.setattr(rules, "_object_flags",
                        lambda path, found: _name_only(found))
    # No Dolby Vision anywhere in this fixture.
    monkeypatch.setattr(run_module.dolbyvision, "read_video_info",
                        lambda path: {"PROFILE": "", "SETTINGS": "", "HDR": "",
                                      "TRANSFER": "", "FPS_SPEC": "",
                                      "STREAM_SIZE": ""})
    monkeypatch.setattr(run_module.dolbyvision, "normalise_config_level",
                        lambda *a, **k: 0)

    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(run_module.ramscratch, "ram_scratch_dir_for",
                        lambda *a, **k: (str(scratch), False, 0))
    monkeypatch.setattr(run_module.ramscratch, "add_exit_cleanup",
                        lambda paths: None)
    monkeypatch.setattr(run_module.ramscratch, "release_exit_cleanup",
                        lambda paths: None)

    recorded = []

    def stub(argv):
        recorded.append(argv)
        out = argv[argv.index("-o") + 1]
        os.makedirs(os.path.dirname(out), exist_ok=True)
        open(out, "w").close()
        return 0, ""
    monkeypatch.setattr(run_module, "_mkvmerge", stub)

    state = run_module.Run(script_dir="", ram_root=str(tmp_path),
                           skips=None, fragments_file="", whisper={})
    run_module.improve_main_movies(state, str(tmp_path))
    return {"root": str(tmp_path), "movie": movie, "base": base,
            "argv": recorded[0] if recorded else [], "calls": recorded,
            "state": state}


def _name_only(tracks):
    """The object flag as it arrives when mediainfo read nothing: from the track
    name alone."""
    from medialib.lib import objectaudio
    for track in tracks:
        if track.is_audio:
            track.objects = objectaudio.audio_object_flag("", "", track.name)


def _after(argv, flag):
    return argv[argv.index(flag) + 1] if flag in argv else ""


def _block_before(argv, path):
    """The per-input option block an appended subtitle was given."""
    at = argv.index(path)
    return argv[at - 8:at]


class TestTheSwap:

    def test_the_original_is_preserved_and_the_copy_took_its_place(self,
                                                                   remux):
        assert os.path.isfile(remux["base"] + " (old).mkv")
        assert os.path.isfile(remux["movie"])

    def test_the_swapped_opus_is_a_remux_input(self, remux):
        assert remux["base"] + "_0.opus" in remux["argv"]


class TestWhatSurvives:
    """The keep-lists, which is where every rule lands."""

    def test_the_audio_keep_list(self, remux):
        """1 is the mono FLAC kept by its exception; 7 and 8 are commentaries -
        8 in Dutch, which the excessive-language rule would drop were
        commentaries not exempt; 9 is an isolated score, which is not a second
        soundtrack; 10 is the ladder winner for English."""
        assert _after(remux["argv"], "-a") == "1,7,8,9,10"

    def test_the_subtitle_keep_list(self, remux):
        """The German image subtitle goes; the English text one stays, and so
        does the commentary transcript a previous improvement appended."""
        assert _after(remux["argv"], "-s") == "6,11"

    def test_the_track_order_puts_the_opus_in_its_losslesss_slot(self, remux):
        """And appends the two new commentary subtitles at the very end."""
        assert _after(remux["argv"], "--track-order") == \
            "1:0,0:1,0:6,0:7,0:8,0:9,0:10,0:11,2:0,3:0"


class TestTheCommentaryTranscripts:

    def test_one_the_file_does_not_carry_is_appended(self, remux):
        assert remux["base"] + " 8 Commentary Dutch.nl.srt" in remux["argv"]

    def test_one_already_a_subtitle_track_is_not_appended_again(self, remux):
        """Track 11 is that transcript, as a previous improvement left it -
        same name, same language. Appending it again would pile an identical
        track on, with one more on every later run."""
        assert remux["base"] + " 7 Commentary by Director.srt" \
            not in remux["argv"]

    def test_each_carries_its_own_language_and_a_title_that_says_which(
            self, remux):
        """A commentary with more than one transcript has the language spelled
        out in the titles, so the two are told apart in the player."""
        assert _block_before(
            remux["argv"], remux["base"] + " 8 Commentary Dutch.nl.srt") == [
            "--language", "0:nl", "--track-name", "0:Commentary Dutch (NL)",
            "--commentary-flag", "0:1", "--default-track-flag", "0:0"]
        assert _block_before(
            remux["argv"], remux["base"] + " 8 Commentary Dutch.en.srt") == [
            "--language", "0:en", "--track-name", "0:Commentary Dutch (EN)",
            "--commentary-flag", "0:1", "--default-track-flag", "0:0"]


class TestIdempotency:

    def test_a_folder_that_already_has_an_old_copy_is_skipped(self, remux):
        """Which is what makes a second run a no-op."""
        before = len(remux["calls"])
        run_module.improve_main_movies(remux["state"], remux["root"])
        assert len(remux["calls"]) == before


class TestFromARealIdentificationDocument:
    """The same decisions, driven from a genuine ``mkvmerge -J`` document rather
    than from a canned track list.

    The shell needed a whole second test file for this, because its parsing was a
    jq filter - a second copy of the truth that the decision tests stubbed away
    with a pass-through `cat`, so the filter's field order, separator and
    null-rendering were never actually run. The port parses with `json`, and what
    is worth keeping from that file is the end-to-end property: the document in,
    the mkvmerge command out.
    """

    @pytest.fixture
    def argv(self, tmp_path, monkeypatch):
        import json
        folder = tmp_path / "Film (2020)"
        folder.mkdir()
        movie = str(folder / "Film (2020).mkv")
        base = str(folder / "Film (2020)")
        open(movie, "w").close()
        for name in ("_0.opus", "_1.opus",
                     " 7 Commentary by Director.srt",
                     " 8 Commentary Dutch.nl.srt",
                     " 8 Commentary Dutch.en.srt"):
            open(base + name, "w").close()

        document = {"tracks": [
            {"id": id, "type": kind, "properties": _properties(
                codec, channels, language, name, commentary, default)}
            for id, kind, codec, channels, language, name, commentary, default
            in FIXTURE]}

        class Done:
            returncode = 0
            stdout = json.dumps(document).encode()
        monkeypatch.setattr(rules.subprocess, "run", lambda *a, **k: Done())
        monkeypatch.setattr(rules, "_object_flags",
                            lambda path, found: _name_only(found))
        monkeypatch.setattr(run_module.dolbyvision, "read_video_info",
                            lambda path: {"PROFILE": "", "SETTINGS": "",
                                          "HDR": "", "TRANSFER": "",
                                          "FPS_SPEC": "", "STREAM_SIZE": ""})
        monkeypatch.setattr(run_module.dolbyvision, "normalise_config_level",
                            lambda *a, **k: 0)
        scratch = tmp_path / "scratch"
        scratch.mkdir()
        monkeypatch.setattr(run_module.ramscratch, "ram_scratch_dir_for",
                            lambda *a, **k: (str(scratch), False, 0))
        monkeypatch.setattr(run_module.ramscratch, "add_exit_cleanup",
                            lambda paths: None)
        monkeypatch.setattr(run_module.ramscratch, "release_exit_cleanup",
                            lambda paths: None)

        recorded = []

        def stub(args):
            recorded.append(args)
            out = args[args.index("-o") + 1]
            os.makedirs(os.path.dirname(out), exist_ok=True)
            open(out, "w").close()
            return 0, ""
        monkeypatch.setattr(run_module, "_mkvmerge", stub)

        state = run_module.Run(script_dir="", ram_root=str(tmp_path),
                               skips=None, fragments_file="", whisper={})
        run_module.improve_main_movies(state, str(tmp_path))
        return recorded[0] if recorded else []

    def test_the_same_audio_survives(self, argv):
        assert _after(argv, "-a") == "1,7,8,9,10"

    def test_the_same_subtitles_survive(self, argv):
        assert _after(argv, "-s") == "6,11"

    def test_and_the_same_track_order_comes_out(self, argv):
        assert _after(argv, "--track-order") == \
            "1:0,0:1,0:6,0:7,0:8,0:9,0:10,0:11,2:0,3:0"


def _properties(codec, channels, language, name, commentary, default):
    """One track's properties as mkvmerge writes them - a subtitle carrying no
    channel count at all, rather than a null one."""
    properties = {"codec_id": codec, "language": language, "track_name": name,
                  "flag_commentary": commentary == "true",
                  "default_track": default == "true", "forced_track": False}
    if channels != "null":
        properties["audio_channels"] = int(channels)
    return properties


class TestTheLibraryWalk:
    """Which folders a pass picks a main movie out of.

    Every other case here runs one film, which leaves the walk itself untested:
    the guard that skips bonus folders, the folders there is no telling a feature
    from, and that one film's decision does not leak into the next.
    """

    @pytest.fixture
    def walked(self, tmp_path, monkeypatch):
        """The films the pass actually reached."""
        reached = []
        monkeypatch.setattr(run_module.Run, "improve_main_movie",
                            lambda self, movie: reached.append(movie))
        state = run_module.Run(script_dir="")
        return reached, state, tmp_path

    def test_a_film_folders_single_mkv_is_the_main_movie(self, walked):
        reached, state, root = walked
        (root / "Film (2020)").mkdir()
        open(str(root / "Film (2020)" / "Film (2020).mkv"), "w").close()
        run_module.improve_main_movies(state, str(root))
        assert reached == [str(root / "Film (2020)" / "Film (2020).mkv")]

    @pytest.mark.parametrize("category",
                             [name for name, _k in rules.BONUS_CATEGORIES]
                             + ["Featurettes"])
    def test_a_bonus_folder_is_skipped(self, walked, category):
        """An extra is not a main movie. Asserted against the live table: a
        category added to it that the guard does not cover would have its extras
        remuxed as if each were a feature film."""
        reached, state, root = walked
        folder = root / "Film (2020)" / category
        folder.mkdir(parents=True)
        open(str(folder / "A trailer.mkv"), "w").close()
        run_module.improve_main_movies(state, str(root))
        assert str(folder / "A trailer.mkv") not in reached

    def test_a_folder_with_several_mkvs_is_ambiguous_and_skipped(self, walked):
        """There is no telling which one is the feature."""
        reached, state, root = walked
        folder = root / "Film (2020)"
        folder.mkdir()
        open(str(folder / "Part One.mkv"), "w").close()
        open(str(folder / "Part Two.mkv"), "w").close()
        run_module.improve_main_movies(state, str(root))
        assert reached == []

    def test_a_folder_with_no_mkv_at_all_is_skipped(self, walked):
        reached, state, root = walked
        (root / "Film (2020)").mkdir()
        open(str(root / "Film (2020)" / "notes.txt"), "w").close()
        run_module.improve_main_movies(state, str(root))
        assert reached == []

    def test_an_already_improved_folder_is_skipped(self, walked):
        """The "(old)" sibling is what makes a second pass a no-op."""
        reached, state, root = walked
        folder = root / "Film (2020)"
        folder.mkdir()
        open(str(folder / "Film (2020).mkv"), "w").close()
        open(str(folder / "Film (2020) (old).mkv"), "w").close()
        run_module.improve_main_movies(state, str(root))
        assert reached == []

    def test_the_old_copy_does_not_itself_count_as_a_second_mkv(self, walked):
        """Or a folder holding one would read as ambiguous rather than as
        finished."""
        reached, state, root = walked
        folder = root / "Film (2020)"
        folder.mkdir()
        open(str(folder / "Film (2020).mkv"), "w").close()
        open(str(folder / "Something (old).mkv"), "w").close()
        run_module.improve_main_movies(state, str(root))
        assert reached == [str(folder / "Film (2020).mkv")]

    def test_several_films_are_all_reached_in_one_pass(self, walked):
        reached, state, root = walked
        for title in ("Film A (2019)", "Film B (2020)", "Film C (2021)"):
            (root / title).mkdir()
            open(str(root / title / (title + ".mkv")), "w").close()
        run_module.improve_main_movies(state, str(root))
        assert len(reached) == 3

    def test_a_box_set_is_walked_to_its_depth(self, walked):
        reached, state, root = walked
        folder = root / "Box Set" / "Film (1999)"
        folder.mkdir(parents=True)
        open(str(folder / "Film (1999).mkv"), "w").close()
        run_module.improve_main_movies(state, str(root))
        assert reached == [str(folder / "Film (1999).mkv")]

