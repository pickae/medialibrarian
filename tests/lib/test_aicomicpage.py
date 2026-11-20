"""Tests for checking whether a very small comic page is truly blank -
medialib.lib.aicomicpage - and its integration with convert-comics.

The model is a stand-in that records what it was shown and answers with JSON
(or raises LlmError). What is pinned:
- confident "blank" returns True (safe to drop);
- "content", unsure, low/missing confidence, malformed JSON, image-prep error,
  and LlmError all return False (keep on any doubt);
- switch off or no vision model returns None quietly (existing size-only rule);
- convert_comics.drop_empty_pages never touches pages at or above MIN_PAGE_SIZE,
  keeps small pages when the check returns False, drops them on True or None,
  and reports vision model usage when any request was made.
"""

from __future__ import annotations

import contextlib
import os
import pathlib
import subprocess

import pytest

from medialib.cli import convert_comics as cc
from medialib.lib import aicomicpage, llmclient, llmroster, llmtraits, modelmeter

pytestmark = pytest.mark.pure

_JPEG = b"\xff\xd8\xff\xe0" + b"\0" * 500
_PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 500
_AVIF = b"\x00\x00\x00\x1cftypavif" + b"\0" * 500


class _VisionModel:
    """A stand-in model that optionally sees images and answers ``answer``
    (or raises it)."""

    def __init__(self, answer, *, sees: bool = True, name: str = "vision-stub",
                 serve_error: Exception | None = None):
        self.answer = answer
        self.sees = sees
        self.name = name
        self.model = name
        self.serve_error = serve_error
        self.shown: list[tuple[str, str, list[bytes]]] = []

    @contextlib.contextmanager
    def serving(self, log):
        if self.serve_error is not None:
            raise self.serve_error
        yield self

    def sees_images(self) -> bool:
        return self.sees

    def ask(self, system, prompt, tally=None, images=()):
        self.shown.append((system, prompt,
                           [pathlib.Path(p).read_bytes() for p in images]))
        if isinstance(self.answer, Exception):
            raise self.answer
        if tally is not None:
            tally.add(0.5, 25, 400)
        return self.answer


def _as_is(path: str, dest: str) -> str:
    return path


def _copy_to_dest(path: str, dest: str) -> str:
    pathlib.Path(dest).write_bytes(_JPEG)
    return dest


@pytest.fixture(autouse=True)
def _on(monkeypatch):
    monkeypatch.setenv(aicomicpage.SWITCH_VARIABLE, "on")


class TestParseVerdict:
    def test_confident_blank_returns_true(self):
        blank, why = aicomicpage.parse_verdict(
            '{"verdict": "blank", "confidence": "high", "why": "uniform white"}')
        assert blank is True
        assert why == "uniform white"

    def test_confident_blank_inside_fenced_text_returns_true(self):
        blank, why = aicomicpage.parse_verdict(
            'Looking closely:\n```json\n'
            '{"verdict": "blank", "confidence": "high", "why": "plain paper"}\n'
            '```')
        assert blank is True
        assert why == "plain paper"

    @pytest.mark.parametrize("score, expected", [
        (0.95, True),
        (0.9, True),
        (0.89, False),
        (0.5, False),
    ])
    def test_numeric_confidence_requires_high_threshold(self, score, expected):
        blank, _ = aicomicpage.parse_verdict(
            '{"verdict": "blank", "confidence": %s, "why": "empty"}' % score)
        assert blank is expected

    def test_content_returns_false(self):
        blank, why = aicomicpage.parse_verdict(
            '{"verdict": "content", "confidence": "high", "why": "title text"}')
        assert blank is False
        assert "title text" in why

    @pytest.mark.parametrize("answer", [
        '{"verdict": "blank", "confidence": "medium", "why": "faint mark"}',
        '{"verdict": "blank", "confidence": "low", "why": "unclear"}',
        '{"verdict": "blank", "confidence": "unsure"}',
        '{"verdict": "blank", "why": "no confidence field"}',
        '{"verdict": "blank", "confidence": true}',
        '{"verdict": "unsure", "confidence": "high", "why": "hard to tell"}',
        '{"verdict": "", "confidence": "high"}',
        'not json at all',
        '["blank", "high"]',
        '',
    ])
    def test_any_doubt_or_malformed_answer_returns_false(self, answer):
        blank, _ = aicomicpage.parse_verdict(answer)
        assert blank is False


class TestIsBlank:
    def test_confident_blank_returns_true_and_cleans_up_and_meters(self, tmp_path):
        page = tmp_path / "03.avif"
        page.write_bytes(_AVIF)
        meter = tmp_path / "meter"
        model = _VisionModel(
            '{"verdict": "blank", "confidence": "high", "why": "solid white"}')

        result = aicomicpage.is_blank(
            str(page), book_name="Book.cbz", page_name="03.avif",
            models=[model], meter_file=str(meter), prepare=_copy_to_dest)

        assert result is True
        assert len(model.shown) == 1
        assert "03.avif" in model.shown[0][1]
        assert "Book.cbz" in model.shown[0][1]
        assert not (tmp_path / "03.avif.look.jpg").exists()
        _spans, tally = modelmeter.gathered(str(meter))
        assert (tally.requests, tally.tokens, tally.read) == (1, 25, 400)

    def test_content_returns_false_to_keep_page(self, tmp_path):
        page = tmp_path / "01.avif"
        page.write_bytes(_AVIF)
        model = _VisionModel(
            '{"verdict": "content", "confidence": "high", "why": "chapter title"}')

        result = aicomicpage.is_blank(
            str(page), book_name="Book.cbz", page_name="01.avif",
            models=[model], prepare=_copy_to_dest)

        assert result is False

    @pytest.mark.parametrize("reply", [
        '{"verdict": "blank", "confidence": "low", "why": "maybe pencil"}',
        '{"verdict": "unsure", "confidence": "low"}',
        'I am not sure whether this is blank.',
    ])
    def test_unsure_low_confidence_or_malformed_json_keeps_page(self, tmp_path, reply):
        page = tmp_path / "02.avif"
        page.write_bytes(_AVIF)
        model = _VisionModel(reply)

        assert aicomicpage.is_blank(
            str(page), models=[model], prepare=_copy_to_dest) is False

    def test_llm_error_keeps_page(self, tmp_path):
        page = tmp_path / "02.avif"
        page.write_bytes(_AVIF)
        model = _VisionModel(llmclient.LlmError("server timed out"))
        meter = tmp_path / "meter"

        assert aicomicpage.is_blank(
            str(page), models=[model], meter_file=str(meter),
            prepare=_copy_to_dest) is False
        assert modelmeter.gathered(str(meter))[1].requests == 0

    def test_serving_error_keeps_page(self, tmp_path):
        page = tmp_path / "02.avif"
        page.write_bytes(_AVIF)
        model = _VisionModel("unused", serve_error=llmclient.LlmError("unreachable"))

        assert aicomicpage.is_blank(
            str(page), models=[model], prepare=_copy_to_dest) is False

    def test_falls_back_to_next_vision_model_after_llm_error(self, tmp_path):
        page = tmp_path / "04.avif"
        page.write_bytes(_AVIF)
        broken = _VisionModel(llmclient.LlmError("500"), name="first")
        working = _VisionModel(
            '{"verdict": "blank", "confidence": "high", "why": "blank"}',
            name="second")

        assert aicomicpage.is_blank(
            str(page), models=[broken, working], prepare=_copy_to_dest) is True
        assert len(working.shown) == 1

    @pytest.mark.parametrize("off_value", ["off", "0", "no", "false", "OFF"])
    def test_switch_off_returns_none_without_asking(self, tmp_path, monkeypatch, off_value):
        monkeypatch.setenv(aicomicpage.SWITCH_VARIABLE, off_value)
        page = tmp_path / "05.avif"
        page.write_bytes(_AVIF)
        model = _VisionModel(
            '{"verdict": "content", "confidence": "high", "why": "text"}')

        assert aicomicpage.is_blank(
            str(page), models=[model], prepare=_copy_to_dest) is None
        assert model.shown == []

    def test_no_vision_model_configured_returns_none_quietly(self, tmp_path, monkeypatch, capsys):
        llmroster.forget()
        llmtraits.forget()
        for name in llmclient.SETTING_FIELDS.values():
            monkeypatch.delenv(name, raising=False)
        monkeypatch.setenv(llmroster.MODELS_VARIABLE, str(tmp_path / "none.toml"))
        page = tmp_path / "06.avif"
        page.write_bytes(_AVIF)

        assert aicomicpage.is_blank(str(page), prepare=_copy_to_dest) is None
        out = capsys.readouterr()
        assert out.out == "" and out.err == ""

    def test_model_that_does_not_see_images_returns_none(self, tmp_path):
        page = tmp_path / "07.avif"
        page.write_bytes(_AVIF)
        model = _VisionModel('{"verdict": "content"}', sees=False)
        prepared = []

        assert aicomicpage.is_blank(
            str(page), models=[model],
            prepare=lambda p, d: prepared.append(p) or d) is None
        assert model.shown == []
        assert prepared == []

    def test_unpreparable_image_keeps_page(self, tmp_path):
        page = tmp_path / "08.avif"
        page.write_bytes(_AVIF)
        model = _VisionModel(
            '{"verdict": "blank", "confidence": "high", "why": "blank"}')

        assert aicomicpage.is_blank(
            str(page), models=[model], prepare=lambda p, d: "") is False
        assert model.shown == []


class TestImagePreparation:
    def test_for_looking_converts_avif_to_small_jpeg(self, tmp_path, monkeypatch):
        page = tmp_path / "01.avif"
        page.write_bytes(_AVIF)
        dest = tmp_path / "01.avif.look.jpg"
        seen_argv: list[list[str]] = []

        def fake_run(argv, **kwargs):
            seen_argv.append(argv)
            # Even a very small blank JPEG (< 2000 bytes) must be accepted.
            dest.write_bytes(_JPEG)
            return subprocess.CompletedProcess(argv, 0)

        monkeypatch.setattr(aicomicpage.subprocess, "run", fake_run)
        result = aicomicpage._for_looking(str(page), str(dest))

        assert result == str(dest)
        assert seen_argv
        assert any(arg.startswith("avif:") and arg.endswith("[0]") for arg in seen_argv[0])
        assert aicomicpage.LOOK_GEOMETRY + ">" in seen_argv[0]

    def test_for_looking_falls_back_to_png_if_imagemagick_fails(self, tmp_path, monkeypatch):
        page = tmp_path / "01.png"
        page.write_bytes(_PNG)
        dest = tmp_path / "01.png.look.jpg"
        monkeypatch.setattr(
            aicomicpage.subprocess, "run",
            lambda *a, **k: (_ for _ in ()).throw(OSError("no convert")))

        assert aicomicpage._for_looking(str(page), str(dest)) == str(page)

    def test_for_looking_returns_empty_for_avif_if_imagemagick_fails(self, tmp_path, monkeypatch):
        page = tmp_path / "01.avif"
        page.write_bytes(_AVIF)
        dest = tmp_path / "01.avif.look.jpg"
        monkeypatch.setattr(
            aicomicpage.subprocess, "run",
            lambda *a, **k: (_ for _ in ()).throw(OSError("no convert")))

        assert aicomicpage._for_looking(str(page), str(dest)) == ""


class TestRosterTask:
    def test_comic_page_check_requires_vision_and_leans_light(self, tmp_path, monkeypatch):
        llmroster.forget()
        llmtraits.forget()
        monkeypatch.setattr(llmtraits, "_server_json", lambda *a, **k: None)
        for name in llmclient.SETTING_FIELDS.values():
            monkeypatch.delenv(name, raising=False)
        roster_file = tmp_path / "models.toml"
        roster_file.write_text('''
[models.text-only]
url = "http://localhost:8080/v1"
model = "qwen3-8b"
traits = { vision = false, params = 8 }

[models.big-vision]
url = "http://localhost:8081/v1"
model = "qwen3-vl-235b-a22b"
traits = { vision = true, params = 235, active = 22, speed = "slow" }

[models.small-vision]
url = "http://localhost:8082/v1"
model = "gemma3-4b"
traits = { vision = true, params = 4 }
''', encoding="utf-8")
        monkeypatch.setenv(llmroster.MODELS_VARIABLE, str(roster_file))

        roster = llmroster.load()
        assert llmroster.COMIC_PAGE_CHECK.name == "comic-page-check"
        assert llmroster.COMIC_PAGE_CHECK.name in llmroster.TASKS
        assert [m.name for m in roster.ladder(llmroster.COMIC_PAGE_CHECK)] == [
            "small-vision", "big-vision"]


class TestConvertComicsIntegration:
    def test_only_small_pages_are_checked_and_model_can_only_keep(self, tmp_path):
        book = tmp_path / "book"
        book.mkdir()
        (book / "01_big.avif").write_bytes(b"a" * cc.MIN_PAGE_SIZE)
        (book / "02_small_blank.avif").write_bytes(b"b" * 100)
        (book / "03_small_title.avif").write_bytes(b"c" * 100)
        (book / "04_small_unsure.avif").write_bytes(b"d" * 100)
        (book / "notes.txt").write_bytes(b"tiny")

        checked: list[str] = []
        verdicts = {
            "02_small_blank.avif": True,
            "03_small_title.avif": False,
            "04_small_unsure.avif": False,
        }

        def stub_check(path, *, book_name="", page_name="", meter_file=""):
            checked.append(page_name)
            return verdicts[page_name]

        dropped, kept = cc.drop_empty_pages(
            str(book), "Comic.cbz", check_blank=stub_check)

        assert (dropped, kept) == (1, 2)
        # Page at MIN_PAGE_SIZE was never shown to the model and never dropped.
        assert checked == [
            "02_small_blank.avif", "03_small_title.avif", "04_small_unsure.avif"]
        assert sorted(os.listdir(book)) == [
            "01_big.avif", "03_small_title.avif", "04_small_unsure.avif", "notes.txt"]

    def test_none_verdict_preserves_size_only_dropping(self, tmp_path):
        book = tmp_path / "book"
        book.mkdir()
        (book / "01_big.avif").write_bytes(b"a" * (cc.MIN_PAGE_SIZE + 50))
        (book / "02_small.avif").write_bytes(b"b" * 50)

        dropped, kept = cc.drop_empty_pages(
            str(book), "Comic.cbz", check_blank=lambda *a, **k: None)

        assert (dropped, kept) == (1, 0)
        assert os.listdir(book) == ["01_big.avif"]

    def test_end_to_end_metering_and_footer_report(self, tmp_path, monkeypatch, capsys):
        book = tmp_path / "book"
        book.mkdir()
        (book / "01_title.avif").write_bytes(_JPEG)
        (book / "02_blank.avif").write_bytes(_JPEG)
        meter = tmp_path / "emptyPageModel.meter"

        replies = iter([
            '{"verdict": "content", "confidence": "high", "why": "title page"}',
            '{"verdict": "blank", "confidence": "high", "why": "blank white"}',
        ])

        class _SeqModel(_VisionModel):
            def ask(self, system, prompt, tally=None, images=()):
                self.answer = next(replies)
                return super().ask(system, prompt, tally=tally, images=images)

        model = _SeqModel("")
        monkeypatch.setattr(
            aicomicpage.llmroster, "optional",
            lambda task: ([model], "") if task is llmroster.COMIC_PAGE_CHECK else ([], "no"))
        monkeypatch.setattr(aicomicpage, "_for_looking", _as_is)

        dropped, kept = cc.drop_empty_pages(
            str(book), "Issue 1.cbz", meter_file=str(meter))

        assert (dropped, kept) == (1, 1)
        assert os.listdir(book) == ["01_title.avif"]
        _spans, tally = modelmeter.gathered(str(meter))
        assert tally.requests == 2

        capsys.readouterr()
        cc.report_empty_page_model(str(meter))
        assert "Empty-page model:" in capsys.readouterr().err

    def test_report_empty_page_model_silent_when_no_requests(self, tmp_path, capsys):
        cc.report_empty_page_model(str(tmp_path / "nonexistent.meter"))
        cc.report_empty_page_model("")
        out = capsys.readouterr()
        assert out.out == "" and out.err == ""
