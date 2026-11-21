"""`ytdlp -i`'s library copy, built while the downloads go on: the finished
feeds it is handed, the batches it converts them in, and -A's chapters.

In process, with the two commands it runs recorded rather than run: what is
asserted is which calls the queue makes and when, not what they do - that is
`test_ytdlp_ingest_cli.py`'s.
"""

from __future__ import annotations

import os
import types

import pytest

from medialib.cli import ytdlp

pytestmark = pytest.mark.fs


@pytest.fixture
def library(tmp_path, monkeypatch):
    """A run's scratch and staging, and the calls the queue makes."""
    staging = tmp_path / "Staging" / "Music"
    status, manifests = tmp_path / "status", tmp_path / "manifests"
    for folder in (staging, status, manifests):
        folder.mkdir(parents=True)
    calls: list = []
    failing: set = set()

    def run_command(command, argv, script_dir="", **_kwargs):
        calls.append([command, *argv])
        return types.SimpleNamespace(returncode=1 if command in failing else 0)

    monkeypatch.setattr(ytdlp.commands, "run_command", run_command)
    state = types.SimpleNamespace(
        status_dir=str(status), manifest_dir=str(manifests), script_dir="",
        options={"cleanUp": False, "aiChapters": False},
        ingest={"staging": {"Music": str(staging)}, "root": str(tmp_path / "Ingested"),
                "args": {"Music": "-c -b 65 -A"}},
        transcripts_for=lambda profile: ())
    index = [0]

    def feed(subdir, episodes, profile="youtubeAudio"):
        """A finished feed of ``episodes`` new files, queued as a download
        hands it over."""
        index[0] += 1
        folder = staging / subdir
        folder.mkdir(parents=True, exist_ok=True)
        paths = []
        for number in range(episodes):
            path = folder / ("2026010%d Episode.opus" % (number + 1))
            path.write_bytes(b"x" * 3)
            paths.append(str(path))
        (manifests / ("feed%d" % index[0])).write_text(
            "".join(path + "\n" for path in paths), encoding="utf-8")
        queue = status / ytdlp.LIBRARY_QUEUE
        queue.mkdir(exist_ok=True)
        (queue / ("%06d" % index[0])).write_text("%s\n%s\n" % (profile, subdir))
        return paths

    def drain():
        (status / ytdlp.DOWNLOADS_DONE).write_text("")
        ytdlp.LibraryQueue(state).run()

    return types.SimpleNamespace(state=state, calls=calls, failing=failing,
                                 feed=feed, drain=drain, staging=staging,
                                 status=status, manifests=manifests)


def _conversions(calls):
    return [call for call in calls if call[0] == "convert-audio"]


class TestBatches:
    def test_feeds_wait_for_a_batch_worth_of_episodes(self, library):
        """Three feeds of two: one convert-audio for all three, each feed's
        folder named with -r, so whisper and the model start once for them."""
        for name in ("A", "B", "C"):
            library.feed(name, 2)
        library.drain()
        conversions = _conversions(library.calls)
        assert len(conversions) == 1
        assert conversions[0][-2:] == [str(library.staging),
                                       library.state.ingest["root"]]
        assert conversions[0][1:-2] == ["-c", "-b", "65", "-A",
                                        "-O", "A", "-O", "B", "-O", "C"]

    def test_the_chapter_instructions_go_over_whole(self, library):
        """-P's text is handed to convert-audio as one argument, however many
        spaces it holds."""
        library.state.ingest["prompt"] = ["-P", "Name each speaker and topic."]
        library.feed("A", 1)
        library.drain()
        assert _conversions(library.calls)[0][1:-2] == [
            "-c", "-b", "65", "-A", "-P", "Name each speaker and topic.", "-O", "A"]

    def test_a_batch_short_of_the_size_still_goes_when_the_downloads_end(self, library):
        library.feed("A", 1)
        library.drain()
        assert len(_conversions(library.calls)) == 1

    def test_each_feed_is_named_before_it_is_converted(self, library):
        library.feed("A", 1)
        library.drain()
        assert library.calls[0] == ["clean-folder-structure",
                                    str(library.staging / "A")]
        assert library.calls[1][0] == "convert-audio"

    def test_a_feed_downloaded_twice_is_converted_once(self, library):
        library.feed("A", 1)
        library.feed("A", 1)
        library.drain()
        assert _conversions(library.calls)[0].count("-O") == 1

    def test_a_profile_with_no_conversion_is_not_converted(self, library):
        library.feed("Videos", 6, profile="youtubeVideo")
        library.drain()
        assert library.calls == []

    def test_a_failed_batch_is_left_for_the_summary(self, library):
        library.failing.add("convert-audio")
        library.feed("A", 1)
        library.drain()
        failed = [name for name in os.listdir(library.status)
                  if name.startswith(ytdlp.LIBRARY_FAILED)]
        assert len(failed) == 1
        assert "Music audio (A)" in (library.status / failed[0]).read_text()


class TestBeforeTheNamesChange:
    def test_the_download_is_measured_while_its_names_are_its_own(self, library):
        library.feed("A", 2)
        library.drain()
        from medialib.lib import podcastfeeds

        assert podcastfeeds.podcast_download_stats(str(library.manifests)) == (2, 6)

    def test_with_c_the_feed_is_tidied_and_its_figures_kept(self, library):
        library.state.options["cleanUp"] = True
        paths = library.feed("A", 1)
        (library.staging / "A" / "20260101 Episode.jpg").write_bytes(b"x")
        library.drain()
        assert not os.path.exists(paths[0][:-len(".opus")] + ".jpg")
        assert (library.status / (ytdlp.TIDIED + "feed1")).read_text() == "1 0 1\n"

    def test_a_youtube_feeds_transcript_is_chosen_and_kept(self, library):
        library.state.options["cleanUp"] = True
        library.state.transcripts_for = lambda profile: ("de",)
        paths = library.feed("A", 1)
        stem = paths[0][:-len(".opus")]
        with open(stem + ".de-orig.vtt", "w", encoding="utf-8") as handle:
            handle.write("WEBVTT\n\n00:00:01.000 --> 00:00:02.000\nHallo\n\n")
        library.drain()
        assert os.path.isfile(stem + ".srt")
        assert not os.path.exists(stem + ".de-orig.vtt")


class TestTheOption:
    def test_P_with_a_but_without_i_is_refused(self, tmp_path, capsys):
        status = ytdlp.main(["-A", "-P", "Name them.", str(tmp_path / "out"),
                             "archive.log"])
        assert status == 1
        assert "-P is for the chapter model, so it needs -A and -i" in \
            capsys.readouterr().err

    def test_P_without_g_is_refused(self, tmp_path, capsys):
        status = ytdlp.main(["-i", "-P", "Name them.", str(tmp_path / "out"),
                             "archive.log"])
        assert status == 1
        assert "-P is for the chapter model, so it needs -A and -i" in capsys.readouterr().err

    def test_an_unknown_spoken_language_is_refused(self, tmp_path, capsys, monkeypatch):
        monkeypatch.setenv("spokenLanguages", "xx")
        assert ytdlp.main(["-i", "-A", str(tmp_path / "out"), "archive.log"]) == 1
        assert "spokenLanguages" in capsys.readouterr().err

    def test_only_a_youtube_feed_fetches_captions(self):
        state = ytdlp.Run.__new__(ytdlp.Run)
        state.options = {"aiChapters": True}
        state.staging_for_profile = {"youtubeAudio": "/s/Music", "rssAudio": "/s/Speech"}
        state.spoken = ("de",)
        assert state.transcripts_for("youtubeAudio") == ("de",)
        assert state.transcripts_for("rssAudio") == ()
        assert state.transcripts_for("youtubeVideo") == ()
