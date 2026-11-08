"""Tests for medialib.lib.commentarydetection.

The measures fed to the judge are shaped on what the probe heard on real discs
(the figures in the module's comments), with invented words: a commentary
talked over end to end in words the film does not have, a downmix that shares
both, a score with no speech, a dub in another language. The probe itself is
held to the argv it builds and the answer it reads, the helper's run stood in
for.
"""

import json
from types import SimpleNamespace

import pytest

from medialib.lib import commentarydetection as cd

pytestmark = pytest.mark.pure

FILM_WORDS = ("where were you tonight harbour captain listen carefully "
              "nobody leaves this island before morning tide")
COMMENTARY_WORDS = ("we shot this sequence over three nights because weather "
                    "kept changing and the harbour captain was played by "
                    "someone whose agent called constantly")


def _audio(id_, channels="2", name="", commentary=False, visual=False,
           index=None):
    return cd.AudioTrack(id_, int(id_) - 1 if index is None else index,
                         channels, name, commentary, visual)


class TestSuspects:
    def test_the_first_audio_track_is_never_one(self):
        assert cd.suspects([_audio("1", "2")]) == []

    def test_a_stereo_or_mono_track_after_it_is(self):
        audio = [_audio("1", "6"), _audio("2", "2"), _audio("3", "1")]
        assert [t.id for t in cd.suspects(audio)] == ["2", "3"]

    def test_a_surround_track_is_not(self):
        audio = [_audio("1", "8"), _audio("2", "6")]
        assert cd.suspects(audio) == []

    def test_a_track_already_a_commentary_is_not(self):
        audio = [_audio("1", "6"), _audio("2", commentary=True)]
        assert cd.suspects(audio) == []

    def test_an_audio_description_is_not_by_flag_or_name(self):
        audio = [_audio("1", "6"), _audio("2", visual=True),
                 _audio("3", name="Audio Description"),
                 _audio("4", name="Descriptive Audio"),
                 _audio("5", name="Audiodeskription")]
        assert cd.suspects(audio) == []

    @pytest.mark.parametrize("name", ["Isolated Score", "Music Only",
                                      "Effects Track", "Musique"])
    def test_a_track_named_as_music_or_effects_is_not(self, name):
        assert cd.suspects([_audio("1", "6"), _audio("2", name=name)]) == []

    @pytest.mark.parametrize("name", ["", "English", "Stereo", "AC3 2.0",
                                      "Mono"])
    def test_a_name_that_says_nothing_of_what_it_is_leaves_it_one(self, name):
        assert len(cd.suspects([_audio("1", "6"), _audio("2", name=name)])) == 1

    def test_an_unreadable_channel_count_is_not(self):
        assert cd.suspects([_audio("1", "6"), _audio("2", "null")]) == []


class TestWords:
    def test_short_words_and_numbers_are_left_out(self):
        assert cd.words("The cat and 1975 harbour") == {"harbour"}

    def test_case_is_folded(self):
        assert cd.words("Harbour HARBOUR harbour") == {"harbour"}

    def test_an_unspaced_script_is_compared_by_pairs_of_characters(self):
        assert cd.words("你好吗") == {"你好",
                                                  "好吗"}


class TestJudge:
    FILM = cd.Measure(0.81, FILM_WORDS, "en")

    def test_more_speech_in_words_of_its_own_is_a_commentary(self):
        verdict = cd.judge(self.FILM, cd.Measure(0.94, COMMENTARY_WORDS, "en"))
        assert verdict.kind == cd.COMMENTARY
        assert "0.94" in verdict.why and "0.81" in verdict.why

    def test_a_downmix_shares_both_and_is_not(self):
        verdict = cd.judge(self.FILM, cd.Measure(0.82, FILM_WORDS, "en"))
        assert verdict.kind == cd.NOT_COMMENTARY

    def test_a_score_has_no_speech_and_is_not(self):
        verdict = cd.judge(self.FILM, cd.Measure(0.0, "", "en"))
        assert verdict.kind == cd.NOT_COMMENTARY

    def test_a_dub_has_words_of_its_own_in_another_language_and_is_not(self):
        dub = cd.Measure(0.80, "wohin gehst heute abend kapitän hafen", "de")
        assert cd.judge(self.FILM, dub).kind == cd.NOT_COMMENTARY

    def test_words_of_its_own_without_more_speech_is_unsure(self):
        same_language = cd.Measure(0.80, COMMENTARY_WORDS, "en")
        assert cd.judge(self.FILM, same_language).kind == cd.UNSURE

    def test_more_speech_in_the_films_words_is_unsure(self):
        narrated = cd.Measure(0.97, FILM_WORDS, "en")
        assert cd.judge(self.FILM, narrated).kind == cd.UNSURE

    def test_a_quiet_film_does_not_make_a_quiet_track_a_commentary(self):
        film = cd.Measure(0.10, FILM_WORDS, "en")
        quiet = cd.Measure(0.30, COMMENTARY_WORDS, "en")
        assert cd.judge(film, quiet).kind == cd.UNSURE


class TestProbe:
    WHISPER = {"device": "cuda", "computeType": "float16",
               "model": "distil-large-v3.5", "modelMulti": "large-v3-turbo",
               "threads": "4", "jobs": 1, "batchSlots": 16}

    def _run(self, monkeypatch, returncode=0, stdout=b"", stderr=b""):
        calls = []

        def run(argv, **_kwargs):
            calls.append(argv)
            return SimpleNamespace(returncode=returncode, stdout=stdout,
                                   stderr=stderr)

        monkeypatch.setattr(cd.subprocess, "run", run)
        return calls

    def test_the_argv_names_the_multilingual_model_windows_and_streams(
            self, monkeypatch):
        answer = {"0": {"speech": 0.5, "text": "a", "language": "en"},
                  "3": {"speech": 0.9, "text": "b", "language": "en"}}
        calls = self._run(monkeypatch, stdout=json.dumps(answer).encode())
        measures, why = cd.probe("film.mkv", _audio("1", index=0),
                                 [_audio("5", index=3)], self.WHISPER)
        assert why == ""
        assert measures == {"1": cd.Measure(0.5, "a", "en"),
                            "5": cd.Measure(0.9, "b", "en")}
        argv = calls[0]
        assert argv[:3] == list(cd.PROBE_COMMAND)
        assert argv[argv.index("--model") + 1] == "large-v3-turbo"
        assert argv[argv.index("--device") + 1] == "cuda"
        assert argv[argv.index("--seconds") + 1] == str(cd.PROBE_SECONDS)
        assert [argv[i + 1] for i, a in enumerate(argv) if a == "--at"] == \
            [str(at) for at in cd.PROBE_WINDOWS]
        assert argv[-3:] == ["film.mkv", "0", "3"]

    def test_a_failed_run_is_no_answer_and_says_why(self, monkeypatch):
        self._run(monkeypatch, returncode=1, stderr=b"traceback\nno CUDA\n")
        measures, why = cd.probe("film.mkv", _audio("1"), [_audio("2")],
                                 self.WHISPER)
        assert measures == {}
        assert "no CUDA" in why

    def test_an_answer_missing_a_track_is_no_answer(self, monkeypatch):
        answer = {"0": {"speech": 0.5, "text": "a", "language": "en"}}
        self._run(monkeypatch, stdout=json.dumps(answer).encode())
        measures, why = cd.probe("film.mkv", _audio("1"), [_audio("2")],
                                 self.WHISPER)
        assert measures == {} and why

    def test_an_absent_pipx_is_no_answer(self, monkeypatch):
        def run(*_args, **_kwargs):
            raise FileNotFoundError("pipx")

        monkeypatch.setattr(cd.subprocess, "run", run)
        measures, why = cd.probe("film.mkv", _audio("1"), [_audio("2")],
                                 self.WHISPER)
        assert measures == {} and "pipx" in why


class TestCommentaryNames:
    def test_numbered_by_place_among_the_films_commentaries(self):
        audio = [_audio("1", "6"), _audio("2", commentary=True),
                 _audio("3"), _audio("4")]
        assert cd.commentary_names(audio, ["3", "4"]) == {
            "3": "Commentary 2", "4": "Commentary 3"}

    def test_the_only_commentary_is_number_one(self):
        audio = [_audio("1", "6"), _audio("2", name="English")]
        assert cd.commentary_names(audio, ["2"]) == {"2": "Commentary 1"}
