"""The transcript a YouTube episode brings with it: which track is fetched,
which one is kept, and what it looks like once kept."""

from __future__ import annotations

import re

from medialib.lib import aichapters, feedtranscripts

_EPISODE = "20260101 An episode.opus"


def _vtt(*cues):
    """A WebVTT text of (start second, lines) cues, each two seconds long."""
    body = ["WEBVTT", ""]
    for second, lines in cues:
        body.append("00:00:%02d.000 --> 00:00:%02d.000 align:start position:0%%"
                    % (second, second + 2))
        body.extend(lines)
        body.append("")
    return "\n".join(body) + "\n"


def _episode(tmp_path, *languages):
    """An episode with a caption track in each of ``languages``, each one's
    only line naming its language."""
    (tmp_path / _EPISODE).write_bytes(b"x")
    for language in languages:
        (tmp_path / ("20260101 An episode.%s.vtt" % language)).write_text(
            _vtt((1, ["said in %s" % language])), encoding="utf-8")
    return str(tmp_path / _EPISODE)


def _kept(tmp_path):
    return (tmp_path / "20260101 An episode.srt").read_text(encoding="utf-8")


class TestWhatIsAskedFor:
    def test_each_spoken_language_only_as_the_original_and_english_in_full(self):
        args = feedtranscripts.ytdlp_args(["de", "en", "fr"])
        langs = args[args.index("--sub-langs") + 1].split(",")
        assert langs == ["de.*-orig", "fr.*-orig", "en.*"]

    def test_both_the_uploaders_and_the_recognisers_captions(self):
        args = feedtranscripts.ytdlp_args(["en"])
        assert "--write-subs" in args and "--write-auto-subs" in args

    def test_the_patterns_are_whole_matches_as_yt_dlp_reads_them(self):
        """yt-dlp anchors each pattern at both ends: a translation into German
        is not the German original."""
        args = feedtranscripts.ytdlp_args(["de"])
        patterns = args[args.index("--sub-langs") + 1].split(",")

        def fetched(key):
            return any(re.match(pattern + "$", key) for pattern in patterns)

        assert fetched("de-orig") and fetched("en") and fetched("en-orig")
        assert not fetched("de") and not fetched("ja-orig")


class TestWhichOneIsKept:
    def test_the_original_when_it_is_spoken(self, tmp_path):
        choice = feedtranscripts.choose(_episode(tmp_path, "de-orig", "en"), ["en", "de"])
        assert choice.language == "de-orig"
        assert "said in de-orig" in _kept(tmp_path)

    def test_english_when_the_original_is_not_spoken(self, tmp_path):
        """Fetched for a Japanese episode by someone who speaks German: no
        Japanese is asked for, and the English is what is left."""
        choice = feedtranscripts.choose(_episode(tmp_path, "en"), ["de"])
        assert choice.language == "en"

    def test_english_when_the_original_is_unknown(self, tmp_path):
        assert feedtranscripts.choose(_episode(tmp_path, "en-GB"), ["de"]).language == "en-GB"

    def test_written_captions_before_the_recognisers_in_the_same_language(self, tmp_path):
        choice = feedtranscripts.choose(_episode(tmp_path, "en-orig", "en"), ["en"])
        assert choice.language == "en"

    def test_none_when_there_is_neither(self, tmp_path):
        assert feedtranscripts.choose(_episode(tmp_path), ["en"]) is None
        assert not (tmp_path / "20260101 An episode.srt").exists()

    def test_every_track_fetched_is_gone_afterwards(self, tmp_path):
        feedtranscripts.choose(_episode(tmp_path, "de-orig", "en", "en-orig"), ["de"])
        assert sorted(path.name for path in tmp_path.iterdir()) == [
            "20260101 An episode.opus", "20260101 An episode.srt"]

    def test_a_file_of_another_episode_is_left_alone(self, tmp_path):
        episode = _episode(tmp_path, "en")
        (tmp_path / "20260101 An episode two.en.vtt").write_text(_vtt((1, ["x"])))
        feedtranscripts.choose(episode, ["en"])
        assert (tmp_path / "20260101 An episode two.en.vtt").exists()


class TestTheKeptTranscript:
    def test_rolling_captions_say_each_line_once(self, tmp_path):
        """YouTube's own captions: a cue opening with a line of one space, the
        line before repeated above the new one, and a cue of a few
        milliseconds holding only the repeat."""
        (tmp_path / _EPISODE).write_bytes(b"x")
        (tmp_path / "20260101 An episode.en.vtt").write_text(
            "WEBVTT\n\n"
            "00:00:00.160 --> 00:00:02.869 align:start position:0%\n"
            " \n"
            "hello<00:00:00.480><c> there</c>\n\n"
            "00:00:02.869 --> 00:00:02.879 align:start position:0%\n"
            "hello there\n"
            " \n\n"
            "00:00:02.879 --> 00:00:05.000 align:start position:0%\n"
            "hello there\n"
            "how are<00:00:03.100><c> you</c>\n\n",
            encoding="utf-8")
        feedtranscripts.choose(str(tmp_path / _EPISODE), ["en"])
        cues = aichapters.read_transcript(str(tmp_path / "20260101 An episode.srt"))
        assert [(cue.start, cue.text) for cue in cues] == [
            (160, "hello there"), (2879, "how are you")]

    def test_it_is_srt_that_convert_audio_finds_beside_the_episode(self, tmp_path):
        from medialib.lib import transcripttags

        episode = _episode(tmp_path, "en")
        feedtranscripts.choose(episode, ["en"])
        assert transcripttags.find_sidecar(episode[:-len(".opus")]).endswith(
            "20260101 An episode.srt")
