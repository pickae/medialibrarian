"""Tests for vision classification of e-book images vs advertising during
``ingest-books -d`` - medialib.lib.aibookimages.

What is pinned: prompt construction and strict JSON parsing; false-positive
substring rescue when the model sees book content or is unsure; removing
advertising only on high-confidence ``"ad"`` verdicts; always keeping images
on low/medium confidence, ``"unsure"``, malformed JSON, or ``LlmError``;
protecting the book's own cover; clean fallback to ``JUNK_IMAGE_SUBSTRINGS``
when switched off or when no vision model is available; ImageMagick
preparation and cleanup; logging and ``modelmeter`` recording.
"""

from __future__ import annotations

import contextlib
import pathlib

import pytest

from medialib.cli import ingest_books as ib
from medialib.lib import aibookimages, llmclient, llmroster, modelmeter

pytestmark = pytest.mark.pure

_JPEG = b"\xff\xd8\xff\xe0" + b"\0" * 100
_PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 100


class _VisionModel:
    """Stand-in vision model mapping relative paths (or a single default) to
    answers or exceptions, recording every prompt and picture shown."""

    def __init__(self, answers, *, sees: bool = True,
                 serve_error: Exception | None = None, name: str = "vision-stub"):
        self.answers = answers
        self.sees = sees
        self.serve_error = serve_error
        self.name = name
        self.model = name
        self.shown: list[tuple[str, str, list[bytes]]] = []

    @contextlib.contextmanager
    def serving(self, log):
        if self.serve_error is not None:
            raise self.serve_error
        yield self

    def sees_images(self) -> bool:
        return self.sees

    def ask(self, system: str, prompt: str, tally=None, images=()) -> str:
        raw_images = [pathlib.Path(p).read_bytes() for p in images]
        self.shown.append((system, prompt, raw_images))
        if isinstance(self.answers, Exception):
            raise self.answers
        if callable(self.answers):
            reply = self.answers(prompt)
        elif isinstance(self.answers, dict):
            reply = next(
                (val for key, val in self.answers.items() if key in prompt),
                '{"verdict": "unsure", "confidence": "low", "why": "default"}',
            )
        else:
            reply = self.answers
        if isinstance(reply, Exception):
            raise reply
        if tally is not None:
            tally.add(0.5, 25, 400)
        return reply


def _as_is(path: str, _dest: str) -> str:
    return path


def _make_book(tmp_path: pathlib.Path, files: dict[str, bytes | str]) -> list[str]:
    images: list[str] = []
    for rel, content in files.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(content, encoding="utf-8")
        if rel.lower().endswith((".jpg", ".jpeg", ".png", ".svg")):
            images.append(str(path))
    return sorted(images)


@pytest.fixture(autouse=True)
def _enable_switch(monkeypatch):
    monkeypatch.setenv(aibookimages.SWITCH_VARIABLE, "on")


class TestPromptAndParsing:
    def test_prompt_carries_context_and_substring_warning(self):
        text = aibookimages.prompt(
            "OEBPS/images/teaser_dragon.jpg",
            "The Quiet Orchard.epub",
            title="The Quiet Orchard",
            author="Ann Mirelle",
            matched_substring=True,
            size_bytes=24_576,
        )
        assert "The Quiet Orchard.epub" in text
        assert "Book title: The Quiet Orchard" in text
        assert "Author: Ann Mirelle" in text
        assert "OEBPS/images/teaser_dragon.jpg" in text
        assert "false positive" in text
        assert '"verdict"' in text and '"confidence"' in text
        assert "when in doubt" in aibookimages.SYSTEM.lower()

    @pytest.mark.parametrize("raw, expected", [
        ('{"verdict": "ad", "confidence": "high", "why": "other book promo"}',
         aibookimages.Verdict("ad", "high", "other book promo")),
        ('Here is my answer:\n```json\n{"verdict": "BOOK", "confidence": "Medium", '
         '"why": "chapter map"}\n```',
         aibookimages.Verdict("book", "medium", "chapter map")),
        ('{"verdict": "unsure", "confidence": "low"}',
         aibookimages.Verdict("unsure", "low", "")),
    ])
    def test_parse_verdict_accepts_valid_json(self, raw, expected):
        assert aibookimages.parse_verdict(raw) == expected

    @pytest.mark.parametrize("bad", [
        "",
        "not json at all",
        "[]",
        '{"verdict": "ad"}',
        '{"confidence": "high"}',
        '{"verdict": "junk", "confidence": "high"}',
        '{"verdict": "ad", "confidence": "certain"}',
        '{"verdict": "ad", "confidence": 0.99}',
        '{"verdict": null, "confidence": "high"}',
    ])
    def test_parse_verdict_rejects_malformed_or_invalid_values(self, bad):
        verdict = aibookimages.parse_verdict(bad)
        assert verdict is None
        assert not aibookimages.should_drop(verdict)

    @pytest.mark.parametrize("verdict, confidence, drop", [
        ("ad", "high", True),
        ("ad", "medium", False),
        ("ad", "low", False),
        ("book", "high", False),
        ("book", "medium", False),
        ("unsure", "high", False),
        ("unsure", "low", False),
    ])
    def test_only_high_confidence_ad_is_dropped(self, verdict, confidence, drop):
        parsed = aibookimages.Verdict(verdict, confidence)
        assert aibookimages.should_drop(parsed) is drop


class TestClassificationDecisions:
    def test_false_positive_substring_image_is_rescued_when_book_or_unsure(
            self, tmp_path):
        images = _make_book(tmp_path, {
            "OEBPS/images/teaser_illustration.jpg": _JPEG,
            "OEBPS/images/backadd_map.png": _PNG,
        })
        model = _VisionModel({
            "teaser_illustration.jpg":
                '{"verdict": "book", "confidence": "high", "why": "story illustration"}',
            "backadd_map.png":
                '{"verdict": "unsure", "confidence": "low", "why": "unclear sketch"}',
        })
        dropped = aibookimages.junk_images(
            str(tmp_path), images, ib.JUNK_IMAGE_SUBSTRINGS,
            book_name="Novel.epub", lookers=[model], prepare=_as_is,
        )
        assert dropped == []
        assert len(model.shown) == 2

    def test_high_confidence_ads_dropped_both_named_and_unnamed(self, tmp_path):
        images = _make_book(tmp_path, {
            "OEBPS/content.opf": (
                '<?xml version="1.0"?>'
                '<package xmlns:dc="http://purl.org/dc/elements/1.1/">'
                '<metadata><dc:title>Dune</dc:title>'
                '<dc:creator>Frank Herbert</dc:creator></metadata>'
                '<manifest><item id="c" href="images/title_art.jpg" '
                'properties="cover-image"/></manifest></package>'
            ),
            "OEBPS/images/title_art.jpg": _JPEG,
            "OEBPS/images/cover.png": _PNG,
            "OEBPS/images/page1.jpg": _JPEG,
            "OEBPS/images/img_042.jpg": _JPEG,
            "OEBPS/images/teaser_promo.jpg": _JPEG,
        })
        model = _VisionModel({
            "page1.jpg":
                '{"verdict": "book", "confidence": "high", "why": "map of Arrakis"}',
            "img_042.jpg":
                '{"verdict": "ad", "confidence": "high", "why": "newsletter promo"}',
            "teaser_promo.jpg":
                '{"verdict": "ad", "confidence": "high", "why": "other book teaser"}',
        })
        dropped = aibookimages.junk_images(
            str(tmp_path), images, ib.JUNK_IMAGE_SUBSTRINGS,
            book_name="Dune.epub", lookers=[model], prepare=_as_is,
        )
        assert sorted(pathlib.Path(p).name for p in dropped) == [
            "img_042.jpg", "teaser_promo.jpg"]
        # Both the OPF cover and cover.png were protected without asking the model.
        asked_prompts = "\n".join(p for _s, p, _i in model.shown)
        assert "title_art.jpg" not in asked_prompts
        assert "cover.png" not in asked_prompts
        assert "Book title: Dune" in asked_prompts
        assert "Author: Frank Herbert" in asked_prompts

    @pytest.mark.parametrize("reply", [
        '{"verdict": "ad", "confidence": "low", "why": "maybe an ad"}',
        '{"verdict": "ad", "confidence": "medium", "why": "probably promo"}',
        '{"verdict": "unsure", "confidence": "high", "why": "cannot tell"}',
        "sorry, I cannot classify this image",
        '{"verdict": "ad"}',
        llmclient.LlmError("server timed out"),
    ])
    def test_ambiguous_malformed_or_error_always_keeps_the_image(
            self, tmp_path, reply):
        images = _make_book(tmp_path, {
            "OEBPS/images/teaser_maybe.jpg": _JPEG,
            "OEBPS/images/img01.jpg": _JPEG,
        })
        model = _VisionModel(reply)
        dropped = aibookimages.junk_images(
            str(tmp_path), images, ib.JUNK_IMAGE_SUBSTRINGS,
            lookers=[model], prepare=_as_is,
        )
        assert dropped == []

    def test_serving_error_keeps_images_instead_of_dropping(self, tmp_path):
        images = _make_book(tmp_path, {
            "OEBPS/images/teaser_ad.jpg": _JPEG,
            "OEBPS/images/page1.jpg": _JPEG,
        })
        model = _VisionModel("{}", serve_error=llmclient.LlmError("connection refused"))
        dropped = aibookimages.junk_images(
            str(tmp_path), images, ib.JUNK_IMAGE_SUBSTRINGS,
            lookers=[model], prepare=_as_is,
        )
        assert dropped == []

    def test_switch_off_falls_back_to_junk_substrings(self, tmp_path, monkeypatch):
        monkeypatch.setenv(aibookimages.SWITCH_VARIABLE, "off")
        images = _make_book(tmp_path, {
            "OEBPS/images/teaser_ad.jpg": _JPEG,
            "OEBPS/images/BackAdd.png": _PNG,
            "OEBPS/images/page1.jpg": _JPEG,
        })
        model = _VisionModel('{"verdict": "book", "confidence": "high"}')
        dropped = aibookimages.junk_images(
            str(tmp_path), images, ib.JUNK_IMAGE_SUBSTRINGS,
            lookers=[model], prepare=_as_is,
        )
        assert sorted(pathlib.Path(p).name for p in dropped) == [
            "BackAdd.png", "teaser_ad.jpg"]
        assert model.shown == []

    def test_no_vision_model_falls_back_to_junk_substrings(self, tmp_path, monkeypatch):
        llmroster.forget()
        for name in llmclient.SETTING_FIELDS.values():
            monkeypatch.delenv(name, raising=False)
        images = _make_book(tmp_path, {
            "OEBPS/images/teaser_ad.jpg": _JPEG,
            "OEBPS/images/page1.jpg": _JPEG,
        })
        # Empty roster from environment:
        dropped_none = aibookimages.junk_images(
            str(tmp_path), images, ib.JUNK_IMAGE_SUBSTRINGS, prepare=_as_is,
        )
        assert [pathlib.Path(p).name for p in dropped_none] == ["teaser_ad.jpg"]

        # Looker present but cannot see images:
        blind = _VisionModel('{"verdict": "book", "confidence": "high"}', sees=False)
        dropped_blind = aibookimages.junk_images(
            str(tmp_path), images, ib.JUNK_IMAGE_SUBSTRINGS,
            lookers=[blind], prepare=_as_is,
        )
        assert [pathlib.Path(p).name for p in dropped_blind] == ["teaser_ad.jpg"]
        assert blind.shown == []

    def test_cap_prioritises_substring_matches_and_keeps_overflow(
            self, tmp_path, monkeypatch):
        monkeypatch.setattr(aibookimages, "MAX_IMAGES_PER_BOOK", 2)
        images = _make_book(tmp_path, {
            "OEBPS/images/a.jpg": _JPEG,
            "OEBPS/images/b.jpg": _JPEG,
            "OEBPS/images/c.jpg": _JPEG,
            "OEBPS/images/teaser_hero.jpg": _JPEG,
        })
        model = _VisionModel({
            "teaser_hero.jpg":
                '{"verdict": "book", "confidence": "high", "why": "hero art"}',
            "a.jpg":
                '{"verdict": "ad", "confidence": "high", "why": "promo"}',
        })
        dropped = aibookimages.junk_images(
            str(tmp_path), images, ib.JUNK_IMAGE_SUBSTRINGS,
            lookers=[model], prepare=_as_is,
        )
        assert len(model.shown) == 2
        assert "teaser_hero.jpg" in model.shown[0][1]
        assert [pathlib.Path(p).name for p in dropped] == ["a.jpg"]


class TestMeterLoggingAndIntegration:
    def test_modelmeter_records_and_decisions_are_logged(
            self, tmp_path, capsys):
        images = _make_book(tmp_path, {
            "OEBPS/images/teaser_art.jpg": _JPEG,
            "OEBPS/images/promo_banner.jpg": _JPEG,
        })
        model = _VisionModel({
            "teaser_art.jpg":
                '{"verdict": "book", "confidence": "high", "why": "chapter art"}',
            "promo_banner.jpg":
                '{"verdict": "ad", "confidence": "high", "why": "buy next book"}',
        })
        meter = tmp_path / "bookImagesModel.meter"
        dropped = aibookimages.junk_images(
            str(tmp_path), images, ib.JUNK_IMAGE_SUBSTRINGS,
            lookers=[model], meter_file=str(meter), prepare=_as_is,
        )
        assert [pathlib.Path(p).name for p in dropped] == ["promo_banner.jpg"]
        spans, tally = modelmeter.gathered(str(meter))
        assert (tally.requests, tally.tokens, tally.read) == (2, 50, 800)
        assert spans.all()
        output = capsys.readouterr().err
        assert "rescued from junk name match" in output
        assert "ad (high confidence) -> drop" in output

    def test_for_looking_invokes_imagemagick_capped_at_look_geometry(
            self, tmp_path, monkeypatch):
        src = tmp_path / "big.png"
        src.write_bytes(_PNG)
        dest = tmp_path / "out.jpg"
        calls: list[list[str]] = []

        def fake_run(argv, **_kwargs):
            calls.append(list(argv))
            dest.write_bytes(_JPEG)

        monkeypatch.setattr(aibookimages.subprocess, "run", fake_run)
        result = aibookimages._for_looking(str(src), str(dest))
        assert result == str(dest)
        assert calls
        assert aibookimages.LOOK_GEOMETRY + ">" in calls[0]

    def test_report_image_model_prints_total_for_multiple_requests(
            self, tmp_path, capsys):
        meter = tmp_path / "bookImagesModel.meter"
        spans, tally = modelmeter.Spans(), modelmeter.Tally()
        spans.add(1.0, 2.5)
        tally.add(0.7, 20, 300)
        tally.add(0.8, 30, 400)
        modelmeter.record(str(meter), spans, tally)
        ib.report_image_model(str(meter))
        err = capsys.readouterr().err
        assert "Book-image model over the run:" in err
        assert "2 request(s)" in err

    def test_prepare_converts_and_cleans_up_temp_image(self, tmp_path):
        images = _make_book(tmp_path, {
            "OEBPS/images/vector.svg": "<svg/>",
        })
        created_dests: list[pathlib.Path] = []

        def fake_prepare(src: str, dest: str) -> str:
            pathlib.Path(dest).write_bytes(_JPEG)
            created_dests.append(pathlib.Path(dest))
            return dest

        model = _VisionModel('{"verdict": "book", "confidence": "high"}')
        dropped = aibookimages.junk_images(
            str(tmp_path), images, ib.JUNK_IMAGE_SUBSTRINGS,
            lookers=[model], prepare=fake_prepare,
        )
        assert dropped == []
        assert created_dests and not created_dests[0].exists()
        assert model.shown[0][2] == [_JPEG]
        # Nothing extra left inside the unpacked book directory.
        assert list((tmp_path / "OEBPS" / "images").iterdir()) == [
            tmp_path / "OEBPS" / "images" / "vector.svg"]

    def test_clean_book_folder_uses_vision_model_when_enabled(self, tmp_path):
        _make_book(tmp_path, {
            "OEBPS/fonts/serif.ttf": b"font",
            "OEBPS/images/cover.png": _PNG,
            "OEBPS/images/teaser_scene.jpg": _JPEG,
            "OEBPS/images/promo_store.jpg": _JPEG,
            "OEBPS/images/diagram.png": _PNG,
            "OEBPS/content.opf": "x",
        })
        model = _VisionModel({
            "teaser_scene.jpg":
                '{"verdict": "book", "confidence": "high", "why": "scene drawing"}',
            "promo_store.jpg":
                '{"verdict": "ad", "confidence": "high", "why": "store advert"}',
            "diagram.png":
                '{"verdict": "book", "confidence": "high", "why": "technical chart"}',
        })
        ib.clean_book_folder(str(tmp_path), book_name="Guide.epub",
                             lookers=[model], prepare=_as_is)
        assert not (tmp_path / "OEBPS/fonts/serif.ttf").exists()
        assert (tmp_path / "OEBPS/images/cover.png").exists()
        assert (tmp_path / "OEBPS/images/teaser_scene.jpg").exists()
        assert (tmp_path / "OEBPS/images/diagram.png").exists()
        assert not (tmp_path / "OEBPS/images/promo_store.jpg").exists()
