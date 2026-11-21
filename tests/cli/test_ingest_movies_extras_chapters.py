"""The extras' chapters (-A): a lane of a full ingest that gives each bonus
video without chapters some a language model works out, written in place.

What is pinned here is the boundary: which files count as extras, that a main
movie is never touched, what is written and what is only counted, and the
options' refusals. How the chapters are worked out is pinned in
tests/cli/test_convert_video_chapters.py.
"""

from __future__ import annotations

import time

import pytest

from medialib.cli import convert_video_chapters as chapterwork
from medialib.cli import ingest_movies_lanes as lanes
from medialib.cli import ingest_movies_run as run
from medialib.lib import llmclient

pytestmark = pytest.mark.fs


def _film(root, name: str = "The Movie (1999)", bonus=("Featurettes",)):
    folder = root / name
    folder.mkdir(parents=True)
    (folder / (name + ".mkv")).touch()
    for sub in bonus:
        (folder / sub).mkdir()
    return folder


class TestWhichFilesAreExtras:

    def test_only_videos_in_bonus_folders_are_extras(self, tmp_path):
        folder = _film(tmp_path, bonus=("Featurettes", "Deleted Scenes"))
        (folder / "Featurettes" / "Making Of.mkv").touch()
        (folder / "Deleted Scenes" / "Cut.mkv").touch()
        (folder / "Featurettes" / "Making Of.en.srt").touch()
        found = run.extras(str(tmp_path))
        assert found == [str(folder / "Deleted Scenes" / "Cut.mkv"),
                         str(folder / "Featurettes" / "Making Of.mkv")]
        assert str(folder / "The Movie (1999).mkv") not in found


@pytest.fixture
def work(monkeypatch, tmp_path):
    folder = _film(tmp_path)
    for name in ("A.mkv", "B.mkv", "C.mkv"):
        (folder / "Featurettes" / name).touch()
    logs: list = []
    monkeypatch.setattr(run, "log", logs.append)
    written: list = []
    monkeypatch.setattr(run.chapterdb, "write_chapters",
                        lambda movie, chosen: written.append((movie, chosen))
                        or True)
    scratch = tmp_path / "ram"
    scratch.mkdir()
    state = run.Run(ram_root=str(scratch), script_dir="script")
    return state, tmp_path, logs, written


def _outcome(*marks):
    return chapterwork.Outcome("", "", "", "", 0, tuple(marks))


def _extra(work_fixture, name="A.mkv", whisper=True):
    state, root, *_ = work_fixture
    movie = str(root / "The Movie (1999)" / "Featurettes" / name)
    hosted = llmclient.Llm(api="openai", url="", model="m")
    return run._chapter_extra(state, str(root), hosted, movie, whisper)


class TestOneExtra:

    def test_chapters_are_written_to_the_extra(self, work, monkeypatch):
        monkeypatch.setattr(chapterwork, "chapter", lambda *a, **k: _outcome(
            (0, "Start"), (60000, "Later")))
        assert _extra(work) == "done"
        [(movie, chosen)] = work[3]
        assert movie.endswith("Featurettes/A.mkv")
        assert chosen.chapters == ((0.0, "Start"), (60.0, "Later"))
        assert chosen.named

    def test_nothing_to_write_leaves_it(self, work, monkeypatch):
        monkeypatch.setattr(chapterwork, "chapter", lambda *a, **k: None)
        assert _extra(work) == "left"
        assert work[3] == []

    def test_one_that_must_be_transcribed_is_put_off(self, work, monkeypatch):
        seen: list = []

        def chapter(*_a, allow_audio, **_k):
            seen.append(allow_audio)
            return chapterwork.LATER

        monkeypatch.setattr(chapterwork, "chapter", chapter)
        assert _extra(work, whisper=False) == "later"
        assert seen == [False]

    def test_a_write_that_fails_leaves_it(self, work, monkeypatch):
        monkeypatch.setattr(chapterwork, "chapter", lambda *a, **k: _outcome(
            (0, "Start"), (60000, "Later")))
        monkeypatch.setattr(run.chapterdb, "write_chapters", lambda *a: False)
        assert _extra(work) == "left"

    def test_the_scratch_is_removed(self, work, monkeypatch):
        monkeypatch.setattr(chapterwork, "chapter", lambda *a, **k: None)
        _extra(work)
        assert list((work[1] / "ram").iterdir()) == []


class TestTheGpu:
    """A model on this machine's card takes its turn on it; a hosted one
    does not."""

    def _llm(self, url):
        return llmclient.Llm(api="openai", url=url, model="m")

    def _run(self, work, monkeypatch, url):
        state, root, *_ = work
        claims: list = []

        class Gate:
            def claim(self, kind, need, abort, say):
                claims.append((kind, need))
                import contextlib
                return contextlib.nullcontext()

        state.gpu_gate = Gate()
        monkeypatch.setenv("llmVramGB", "12")

        def chapter(*_a, gpu, **_k):
            with gpu():
                pass
            return None

        monkeypatch.setattr(chapterwork, "chapter", chapter)
        movie = str(root / "The Movie (1999)" / "Featurettes" / "A.mkv")
        run._chapter_extra(state, str(root), self._llm(url), movie, True)
        return claims

    def test_a_local_model_claims_the_gpu(self, work, monkeypatch):
        assert self._run(work, monkeypatch, "http://localhost:11434/v1") == [
            ("llm", 12 * 1024)]

    def test_a_hosted_model_does_not(self, work, monkeypatch):
        assert self._run(work, monkeypatch, "") == []

    def test_unset_it_needs_the_whole_card(self, monkeypatch):
        monkeypatch.delenv("llmVramGB", raising=False)
        assert run._llm_vram_mib() is None

    def test_whisper_on_the_cpu_holds_no_claim(self, work):
        state = work[0]
        state.gpu_gate = object()
        with run._gpu_claim(state, "whisper", 0):
            pass


class TestTheLane:
    """Subtitle-read extras go at once; transcribed ones wait for whisper."""

    def _lane(self, verdicts, order):
        def work(movie, whisper):
            order.append((movie, whisper))
            first = verdicts[movie]
            return first if not whisper or first != "later" else "done"

        return lanes.ChapterLane(list(verdicts), work, lambda line: None)

    def test_the_ones_that_need_whisper_wait_for_it(self):
        order: list = []
        lane = self._lane({"a": "done", "b": "later", "c": "left"}, order)
        lane.start()
        deadline = time.time() + 5
        while len(order) < 3 and time.time() < deadline:
            time.sleep(0.01)
        assert order == [("a", False), ("b", False), ("c", False)]
        lane.whisper_free()
        lane.finish()
        assert order[-1] == ("b", True)
        assert (lane.given, lane.left) == (2, 1)

    def test_finishing_does_not_wait_for_a_whisper_never_freed(self):
        order: list = []
        lane = self._lane({"a": "later"}, order)
        lane.start()
        lane.finish()
        assert order == [("a", False), ("a", True)]
        assert lane.given == 1

    def test_one_that_raises_is_left_and_the_rest_go_on(self):
        said: list = []

        def work(movie, whisper):
            if movie == "a":
                raise RuntimeError("model down")
            return "done"

        lane = lanes.ChapterLane(["a", "b"], work, said.append)
        lane.start()
        lane.finish()
        assert (lane.given, lane.left) == (1, 1)
        assert any("model down" in line for line in said)


class TestTheOptions:

    def _main(self, tmp_path, *argv):
        (tmp_path / "Films").mkdir(exist_ok=True)
        return run.main([*argv, str(tmp_path / "Films")],
                        script_dir=str(tmp_path / "script"))

    def test_instructions_need_the_option(self, tmp_path, capsys):
        assert self._main(tmp_path, "-P", "a chapter per scene") == 1
        assert ("-P (instructions for the chapter model) needs -A, or -O "
                "extras-chapters.") in capsys.readouterr().err

