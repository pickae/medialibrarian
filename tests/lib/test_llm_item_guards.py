"""The guards every command puts around a step that asks a model: the
item's time limit, read from llmItemSeconds, and a step whose failure is said
and goes no further - so the item it is a step of is still written. Each
command's own use is pinned with the command it belongs to."""

import os
import threading
import types

import pytest

from medialib.cli import convert_audio, convert_comics, ingest_books, ytdlp
from medialib.lib import comicpdf, llmclient, llmroster, runcontext

pytestmark = pytest.mark.fs


class TestTheItemSetting:
    @pytest.mark.parametrize("value,seconds", [
        ("", 300.0), ("45", 45.0), ("0", 0.0), ("soon", 300.0)])
    def test_it_is_read_with_the_shipped_default_beneath(
            self, monkeypatch, value, seconds):
        monkeypatch.setenv("llmItemSeconds", value)
        assert llmroster.item_seconds() == seconds

    def test_without_ai_a_run_holds_no_model(self, monkeypatch):
        monkeypatch.setattr(runcontext, "get", lambda name, default="": default)
        assert llmroster.run_models((llmroster.BOOK_TAGS,)) == []


class TestGuarded:
    def test_an_error_is_said_and_goes_no_further(self):
        said: list = []
        with llmclient.guarded("the step", said.append):
            raise ValueError("bad answer")
        assert said == ["    WARNING: the step failed and was left out: bad answer"]

    def test_an_interrupt_still_stops_the_run(self):
        with pytest.raises(KeyboardInterrupt), \
                llmclient.guarded("the step", lambda _line: None):
            raise KeyboardInterrupt


class TestTheTimeLimitIsPerThread:
    def test_another_thread_s_item_is_not_held_to_it(self):
        seen: list = []
        with llmclient.time_limit(5):
            other = threading.Thread(
                target=lambda: seen.append(llmclient._deadline()))
            other.start()
            other.join()
            assert llmclient._deadline() is not None
        assert seen == [None]


def _boom(*_args, **_kwargs):
    raise RuntimeError("the model server went away")


class TestConvertAudio:
    def test_a_cover_search_that_breaks_leaves_the_track(self, monkeypatch, tmp_path):
        monkeypatch.setattr(convert_audio.onlinecover, "enabled", lambda: True)
        monkeypatch.setattr(convert_audio.onlinecover, "track_cover", _boom)
        run = types.SimpleNamespace(input_dir=str(tmp_path), mono=True,
                                    surround=False, model_meter="")
        convert_audio.Run._online_cover(run, "a.flac", str(tmp_path / "a.flac"),
                                        str(tmp_path), seconds=60.0, mono=True,
                                        channels=1)


class TestIngestBooks:
    def test_an_image_check_that_breaks_keeps_every_image(self, monkeypatch, tmp_path):
        image = tmp_path / "OEBPS" / "page1.jpg"
        image.parent.mkdir()
        image.write_text("")
        monkeypatch.setattr(ingest_books.aibookimages, "junk_images", _boom)
        ingest_books.clean_book_folder(str(tmp_path), book_name="Book")
        assert image.exists()


class TestConvertComics:
    def test_a_blank_check_that_breaks_falls_back_to_the_size_rule(self, tmp_path):
        tiny = tmp_path / "001.avif"
        tiny.write_bytes(b"x" * 100)
        middling = tmp_path / "002.avif"
        middling.write_bytes(b"x" * (convert_comics.MIN_PAGE_SIZE + 100))
        dropped, kept = convert_comics.drop_empty_pages(
            str(tmp_path), "Book", check_blank=_boom)
        assert (dropped, kept) == (1, 0)
        assert not tiny.exists() and middling.exists()


class TestTheComicPdfJudge:
    def test_a_judge_that_breaks_leaves_the_rule_s_verdict(self, monkeypatch):
        monkeypatch.setattr(comicpdf, "_stats_line", lambda *_a: ("10 5 150", 0))
        monkeypatch.setattr(comicpdf, "_knob", lambda _name: "80")
        monkeypatch.setattr(comicpdf.aicomicpdf, "should_ask", lambda *_a: True)
        monkeypatch.setattr(comicpdf.aicomicpdf, "judge", _boom)
        assert comicpdf.comic_pdf_verdict("x.pdf") == (False, 10, 5, 150)


class TestYtdlpAdReads:
    def test_an_episode_that_breaks_is_left_for_the_next_run(
            self, monkeypatch, tmp_path):
        first, second = tmp_path / "one.m4a", tmp_path / "two.m4a"
        for path in (first, second):
            path.write_text("")
        asked: list = []

        def process(path, *_args, **_kwargs):
            asked.append(os.path.basename(path))
            if path.endswith("one.m4a"):
                raise RuntimeError("the transcript would not parse")

        monkeypatch.setattr(ytdlp.aipodcastads, "enabled", lambda: True)
        monkeypatch.setattr(ytdlp.aipodcastads, "eligible_profile", lambda _p: True)
        monkeypatch.setattr(ytdlp.aipodcastads, "process_episode", process)
        state = types.SimpleNamespace(status_dir=str(tmp_path))
        ytdlp._process_ad_reads_for_files(state, [str(first), str(second)],
                                          "rssAudio")
        assert asked == ["one.m4a", "two.m4a"]
        assert ytdlp._ad_checked_set(str(tmp_path)) == {str(second)}


class TestHoldingForARun:
    def test_without_a_model_nothing_is_held_or_made(self, tmp_path):
        slots = tmp_path / "slots"
        with llmclient.held_for_run([], str(slots), print):
            assert not slots.exists()
            assert not runcontext.get(llmclient.SLOTS_VARIABLE)

    def test_the_slots_folder_lasts_as_long_as_the_hold(self, tmp_path, monkeypatch):
        from medialib.lib import modelserver
        monkeypatch.setattr(modelserver, "WATCH_SECONDS", 0.05)
        slots = tmp_path / "slots"
        hosted = llmclient.Llm(api="openai", url="https://api.example.com/v1",
                               model="m", local=False)
        with llmclient.held_for_run([hosted], str(slots), print):
            assert slots.is_dir()
            assert runcontext.get(llmclient.SLOTS_VARIABLE) == str(slots)
        assert not slots.exists()
        assert not runcontext.get(llmclient.SLOTS_VARIABLE)
