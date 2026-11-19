"""losslessAudioTarget: the codec the lossless audio is converted to.

Read once at the run's start: a value the run does not know refuses the
run before a film is touched, and a run set for flac encodes the lossless
tracks to flac - the channels as they are, no layout re-labelling and no
bitrate - up to eight of them, and leaves a track the target is already in
alone.
"""

from __future__ import annotations

import os
import types

import pytest

from medialib.cli import ingest_movies as im
from medialib.cli import ingest_movies_carry as carry
from medialib.cli import ingest_movies_lanes as lanes
from medialib.cli import ingest_movies_run as run
from medialib.lib import clioptions

pytestmark = pytest.mark.fs


def _library(root, name: str, film: str = "The Movie (1999)"):
    """A folder holding one film, which is what makes it worth ingesting."""
    folder = root / name / film
    folder.mkdir(parents=True)
    (folder / (film + ".mkv")).touch()
    return root / name


class TestTheSetting:
    """The closed set, read the way the naming server is read."""

    pytestmark = pytest.mark.pure

    def test_unset_is_opus(self, monkeypatch):
        monkeypatch.delenv("losslessAudioTarget", raising=False)
        assert im.lossless_audio_target() == "opus"

    @pytest.mark.parametrize("value, target", [("opus", "opus"),
                                               ("FLAC", "flac"),
                                               (" flac ", "flac")])
    def test_either_target_is_accepted(self, monkeypatch, value, target):
        monkeypatch.setenv("losslessAudioTarget", value)
        assert im.lossless_audio_target() == target

    @pytest.mark.parametrize("value", ("aac", "mp3", ""))
    def test_a_value_the_run_does_not_know_is_refused(self, monkeypatch,
                                                      value):
        monkeypatch.setenv("losslessAudioTarget", value)
        with pytest.raises(clioptions.UsageError) as error:
            im.lossless_audio_target()
        assert "losslessAudioTarget" in error.value.message
        assert '"%s"' % value in error.value.message
        assert "opus or flac" in error.value.message


class TestTheRefusal:
    """A value the run does not know refuses the run at its start, before a
    film is touched - like a bad option, with the page and the trailer."""

    def test_a_value_the_run_does_not_know_is_refused(
            self, monkeypatch, tmp_path, capsys):
        _library(tmp_path, "Films")
        monkeypatch.setenv("losslessAudioTarget", "aac")
        assert run.main(["-O", "tagging", str(tmp_path / "Films")],
                        script_dir=str(tmp_path / "script")) == 1
        errors = capsys.readouterr().err
        assert "losslessAudioTarget" in errors
        assert '"aac"' in errors
        assert "Nothing was changed." in errors


def _encode_one(tmp_path, monkeypatch, channels, codec="A_TRUEHD"):
    """One lossless track of <channels> through the phase, the target set
    for flac: the encoder calls made, and the probes asked of the source."""
    folder = tmp_path / "Film (2020)"
    folder.mkdir()
    movie = str(folder / "Film (2020).mkv")
    open(movie, "w").close()
    monkeypatch.setenv("losslessAudioTarget", "flac")
    monkeypatch.setattr(im, "_identify", lambda path: [
        im.Track(id="0", type="video", codec="hevc", channels="null"),
        im.Track(id="1", type="audio", codec=codec, channels=channels)])
    probed = []

    def probe(argv):
        probed.append(argv)
        if "stream=channel_layout" in argv:
            return "5.1\n"
        return "7200\n" if os.path.isfile(argv[-1]) else ""
    monkeypatch.setattr(im, "_probe", probe)
    calls = []

    def encode(argv):
        calls.append(argv)
        open(argv[-1], "w").close()
        return 0
    monkeypatch.setattr(im, "_run", encode)
    ram = str(tmp_path / "ram")
    im.check_audio_tracks(movie, str(tmp_path), ram)
    return movie, im.lossless_path(ram, movie, 1), calls, probed


class TestTheFlacEncode:
    """The argv is the contract: the stream mapped, no bitrate, no layout,
    the flac encoder and the .flac beside the film's mirror in RAM."""

    def test_a_lossless_track_is_encoded_to_flac(self, tmp_path, monkeypatch):
        movie, converted, calls, probed = _encode_one(tmp_path, monkeypatch,
                                                      "6")
        assert calls == [
            ["ffmpeg", "-y", "-loglevel", "error", "-nostats", "-i", movie,
             "-vn", "-map", "0:a:0", "-map_metadata", "-1", "-c:a", "flac",
             converted]]
        assert os.path.isfile(converted)
        # No layout to match: the source is not asked for one.
        assert not any("stream=channel_layout" in argv for argv in probed)

    def test_the_eighth_channel_is_the_last_encoded(self, tmp_path,
                                                    monkeypatch):
        _movie, converted, calls, _probed = _encode_one(tmp_path, monkeypatch,
                                                        "8")
        assert len(calls) == 1
        assert os.path.isfile(converted)

    def test_a_mono_that_is_not_flac_is_converted(self, tmp_path,
                                                  monkeypatch):
        """The mono-TV quirk is the opus mono's: a mono DTS goes to flac."""
        _movie, converted, calls, _probed = _encode_one(tmp_path, monkeypatch,
                                                        "1", codec="A_DTS")
        assert len(calls) == 1
        assert os.path.isfile(converted)

    @pytest.mark.parametrize("codec", ("A_FLAC",))
    def test_a_flac_track_is_already_the_target(self, tmp_path, monkeypatch,
                                                codec):
        _movie, _converted, calls, probed = _encode_one(
            tmp_path, monkeypatch, "6", codec=codec)
        assert calls == []
        assert probed == []

    def test_a_flac_mono_is_already_the_target(self, tmp_path, monkeypatch):
        _movie, _converted, calls, _probed = _encode_one(
            tmp_path, monkeypatch, "1", codec="A_FLAC")
        assert calls == []

    def test_the_ninth_channel_is_kept_and_said_so(self, tmp_path, monkeypatch,
                                                   capsys):
        _movie, converted, calls, _probed = _encode_one(tmp_path, monkeypatch,
                                                        "9")
        assert calls == []
        assert not os.path.exists(converted)
        captured = capsys.readouterr()
        assert ("audio track 1 kept as it is, its 9-channel audio is past "
                "flac's eight channels") in captured.out + captured.err

    def test_the_encode_is_said(self, tmp_path, monkeypatch, capsys):
        _encode_one(tmp_path, monkeypatch, "6")
        text = capsys.readouterr()
        assert ("Transcoding audio track 1 to flac (6ch): "
                "./Film (2020)/Film (2020).mkv") in (text.out + text.err)


class TestWhatIsNotMuxedIn:
    """A flac that did not come out whole is not left for the remux to swap
    in: the track stays lossless."""

    def _film(self, tmp_path, monkeypatch, status, length):
        folder = tmp_path / "Film (2020)"
        folder.mkdir()
        movie = str(folder / "Film (2020).mkv")
        open(movie, "w").close()
        monkeypatch.setenv("losslessAudioTarget", "flac")
        monkeypatch.setattr(im, "_identify", lambda path: [
            im.Track(id="0", type="video", codec="hevc", channels="null"),
            im.Track(id="1", type="audio", codec="A_DTS", channels="6")])
        monkeypatch.setattr(im, "_probe", lambda argv:
                            "7200\n" if "format=duration" in argv
                            else "")
        monkeypatch.setattr(im.durationcheck, "media_duration",
                            lambda path: length if path.endswith(".flac")
                            else 7200.0)

        def encode(argv):
            with open(argv[-1], "w") as handle:
                handle.write("half a flac")
            return status
        monkeypatch.setattr(im, "_run", encode)
        ram = str(tmp_path / "ram")
        im.check_audio_tracks(movie, str(tmp_path), ram)
        return im.lossless_path(ram, movie, 1)

    def test_a_failed_encode_leaves_nothing(self, tmp_path, monkeypatch):
        assert not os.path.exists(self._film(tmp_path, monkeypatch, 1, 7200.0))

    def test_a_short_one_is_removed(self, tmp_path, monkeypatch):
        monkeypatch.delenv(im.durationcheck.SKIP_VARIABLE, raising=False)
        assert not os.path.exists(self._film(tmp_path, monkeypatch, 0, 10.0))

    def test_a_whole_one_stays_for_the_remux(self, tmp_path, monkeypatch):
        assert os.path.isfile(self._film(tmp_path, monkeypatch, 0, 7200.0))


class TestThePath:
    """The transcode and the remux name it alike: the extension is the
    run's target, and the mirror of the film's path is not."""

    pytestmark = pytest.mark.pure

    def test_the_extension_is_the_target(self, monkeypatch):
        movie = "/films/Film (2020).mkv"
        monkeypatch.delenv("losslessAudioTarget", raising=False)
        assert im.lossless_path("/ram", movie, 3).endswith("_3.opus")
        monkeypatch.setenv("losslessAudioTarget", "flac")
        assert im.lossless_path("/ram", movie, 3).endswith("_3.flac")


class TestForget:
    """Gone from RAM once the remux is done with them: the opus and the flac
    alike, whatever the target the run settled on."""

    def test_both_targets_are_gone(self, tmp_path, monkeypatch):
        movie = str(tmp_path / "Film (2020).mkv")
        open(movie, "w").close()
        ram = str(tmp_path / "ram")
        directory = os.path.dirname(im.lossless_path(ram, movie, 0))
        os.makedirs(directory, exist_ok=True)
        stale_opus = os.path.join(directory, "Film (2020)_0.opus")
        flac = os.path.join(directory, "Film (2020)_1.flac")
        other_film = os.path.join(directory, "Film (2020) trailer_0.opus")
        a_text = os.path.join(directory, "Film (2020)_0.srt")
        for name in (stale_opus, flac, other_film, a_text):
            open(name, "w").close()
        monkeypatch.setenv("losslessAudioTarget", "flac")
        im.forget_lossless(ram, movie)
        assert not os.path.exists(stale_opus)
        assert not os.path.exists(flac)
        assert os.path.isfile(other_film)
        assert os.path.isfile(a_text)


class TestTheSwap:
    """The remux swaps a lossless track for its conversion the way it always
    has: the conversion's presence in the RAM scratch is the decision."""

    def test_the_flac_in_ram_is_swapped(self, tmp_path, monkeypatch):
        monkeypatch.setenv("losslessAudioTarget", "flac")
        tracks = [
            im.Track(id="0", type="video", codec="V_MPEGH/ISO/HEVC"),
            im.Track(id="1", type="audio", codec="A_TRUEHD", channels="6",
                     language="eng", language_ietf="en", default="true")]
        movie = str(tmp_path / "film.mkv")
        converted = im.lossless_path(str(tmp_path / "ram"), movie, "1")
        real = os.path.isfile

        def isfile(path):
            return path == converted or real(path)
        monkeypatch.setattr(im.os.path, "isfile", isfile)
        assert im.decide_actions(tracks, movie, str(tmp_path / "ram"),
                                 "en", ("en",))
        assert tracks[1].action == "swap"
        assert tracks[1].lossless == converted


class TestThePhase:
    """The phase line names the target it is working toward."""

    def test_it_says_what_the_target_is(self, monkeypatch, tmp_path):
        said = []
        monkeypatch.setenv("losslessAudioTarget", "flac")
        monkeypatch.setattr(run, "_transcode_lossless", lambda *a, **k: None)
        monkeypatch.setattr(run, "main_movies", lambda root: [])
        monkeypatch.setattr(run, "improve_main_movies",
                            lambda *a, **k: None)
        monkeypatch.setattr(run, "log", said.append)
        state = run.Run(whisper={"device": "cpu"})
        run._films_in_lanes(state, str(tmp_path), False, lanes.FilmLocks())
        assert any("Phase: transcoding lossless audio to flac" in line
                   for line in said)


class TestTheCarriedEncode:
    """The library's lossless tracks go to the same target on the way in,
    put to the new file's speed where the two run at different ones."""

    def _encoded(self, tmp_path, monkeypatch, channels, stretch=False):
        movie = str(tmp_path / "Film (2020).mkv")
        open(movie, "w").close()
        library = str(tmp_path / "The library's copy.mkv")
        open(library, "w").close()
        from medialib.lib import plexcompare
        audio = plexcompare.Audio(stream=0, language="eng",
                                  channels=channels, lossless=True,
                                  objects=False, name="French", track=1,
                                  rate=48000)
        called = []

        def watched(argv, *args, **kwargs):
            called.append(argv)
            open(argv[-1], "w").close()
            return types.SimpleNamespace(returncode=0)
        monkeypatch.setattr(carry.toolcapture, "watched", watched)
        monkeypatch.setattr(im, "_probe", lambda argv: "5.1\n")
        carry_over = carry.LibraryCarry("", [], None,
                                        lambda _line: None,
                                        ram_root=str(tmp_path / "ram"))
        converted = carry_over.lossless_for(str(tmp_path / "ram"), movie,
                                            audio)
        done = carry_over._encode(library, audio, converted, stretch, 1.25)
        return done, converted, called

    def test_a_lossless_track_goes_to_flac(self, tmp_path, monkeypatch):
        monkeypatch.setenv("losslessAudioTarget", "flac")
        done, converted, called = self._encoded(tmp_path, monkeypatch, 6)
        assert done and os.path.isfile(converted)
        assert called[0][-3:] == ["-f", "flac", converted + ".part"]
        assert called[0][called[0].index("-map_metadata") + 2:] == \
            ["-c:a", "flac", "-f", "flac", converted + ".part"]

    def test_and_put_to_the_new_files_speed(self, tmp_path, monkeypatch):
        monkeypatch.setenv("losslessAudioTarget", "flac")
        done, _converted, called = self._encoded(tmp_path, monkeypatch, 6,
                                                 stretch=True)
        assert done
        assert "-af" in called[0]
        assert "asetrate=38400.000000" in called[0][called[0].index("-af") + 1]

    def test_the_ninth_channel_is_not_carried_encoded(self, tmp_path,
                                                      monkeypatch):
        monkeypatch.setenv("losslessAudioTarget", "flac")
        done, converted, called = self._encoded(tmp_path, monkeypatch, 9)
        assert not done
        assert called == []
        assert not os.path.exists(converted)

    def test_the_default_still_goes_to_opus(self, tmp_path, monkeypatch):
        monkeypatch.delenv("losslessAudioTarget", raising=False)
        done, converted, called = self._encoded(tmp_path, monkeypatch, 6)
        assert done and os.path.isfile(converted)
        tail = called[0][called[0].index("-c:a"):]
        assert tail == ["-c:a", "libopus", "-b:a", "250k", "-ac", "6",
                        "-f", "opus", converted + ".part"]


class TestTheCarriedWording:
    """The carry-over says what the track goes to, the way it is done."""

    pytestmark = pytest.mark.pure

    def _plan(self, **kwargs):
        from medialib.lib import plexcarry as pcy
        from medialib.lib import plexcompare as pc
        old = pc.Profile("old.mkv", 5955.0, 1920, 1600, "SDR",
                         (),
                         (pc.Audio(0, "fre", 2, True, False,
                                   "With the director", 2, "", 48000),),
                         (), 0, False, "")
        new = pc.Profile("new.mkv", 5955.0, 3840, 1600, "SDR",
                         (pc.Audio(0, "fre", 2, False, False, "", 1, "",
                                   48000),),
                         (), (), 0, False, "")
        return pcy.plan(new, old, pc.Timing(7.9, 1.0), "", lambda _a: True,
                        lambda _a: (), None, False, False, **kwargs)

    def test_it_names_the_target(self):
        found = self._plan(lossless_target="flac")
        assert found.carried and "to flac" in found.carried[0]

    def test_and_the_default_says_opus(self):
        found = self._plan()
        assert found.carried and "to opus" in found.carried[0]
