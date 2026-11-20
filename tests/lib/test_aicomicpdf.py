"""Tests for the vision-model fallback for borderline comic PDFs -
medialib.lib.aicomicpdf and its integration with medialib.lib.comicpdf and
medialib.lib.contentcensus.
"""

import contextlib
import json
import os
import pathlib

import pytest

from medialib.lib import (
    aicomicpdf,
    comicpdf,
    contentcensus,
    llmclient,
    llmroster,
    modelmeter,
    runcontext,
)

pytestmark = pytest.mark.pure


class _VisionModel:
    """Stand-in vision model recording every request and answering ``answer``
    (or raising it if it is an Exception)."""

    def __init__(self, answer, *, sees=True, name="vision-stand-in"):
        self.answer = answer
        self.sees = sees
        self.name = name
        self.model = name
        self.calls = []

    @contextlib.contextmanager
    def serving(self, log):
        yield self

    def sees_images(self):
        return self.sees

    def ask(self, system, prompt, tally=None, images=()):
        self.calls.append({
            "system": system,
            "prompt": prompt,
            "images": [pathlib.Path(p).read_bytes() for p in images],
        })
        if isinstance(self.answer, Exception):
            raise self.answer
        if tally is not None:
            tally.add(1.5, 42, 600)
        return self.answer


def _fake_render(calls):
    """Stand-in page renderer that writes tiny fake JPEGs and records pages."""
    def render(pdf, page_numbers, dest):
        calls.append((pdf, list(page_numbers)))
        out = []
        for page in page_numbers:
            target = os.path.join(dest, f"page-{page}.jpg")
            pathlib.Path(target).write_bytes(f"jpeg-page-{page}".encode())
            out.append(target)
        return out
    return render


def _make_pdf(tmp_path, name="digital-comic.pdf", content=b"%PDF-1.7\nbody"):
    pdf = tmp_path / name
    pdf.write_bytes(content)
    return str(pdf)


def test_roster_registers_comic_pdf_judge_task():
    task = llmroster.COMIC_PDF_JUDGE
    assert task.name == "comic-pdf-judge"
    assert task.requires == ("vision",)
    assert task.lean == "light"
    assert llmroster.TASKS["comic-pdf-judge"] is task


@pytest.mark.parametrize("pages,good,min_share,expected", [
    (0, 0, 90, False),       # unreadable
    (24, 24, 90, False),     # already passed deterministically
    (10, 9, 90, False),      # right at threshold: already passed
    (80, 2, 90, False),      # clearly a magazine/textbook with a couple photos
    (10, 4, 90, False),      # below borderline window
    (24, 0, 90, True),       # zero matching pages: digital/vector/tiled comic
    (1, 0, 90, True),        # single-page zero match
    (10, 5, 90, True),       # lower edge of borderline window (50%)
    (10, 8, 90, True),       # just below 90% threshold
    (24, 18, 90, True),      # 75%: borderline
])
def test_should_ask_gating(pages, good, min_share, expected):
    assert aicomicpdf.should_ask(pages, good, min_share) is expected


@pytest.mark.parametrize("pages,expected", [
    (0, []),
    (1, [1]),
    (2, [1, 2]),
    (3, [1, 2, 3]),
    (4, [1, 2, 3]),
    (10, [1, 2, 6]),
    (24, [1, 6, 14]),
    (100, [1, 25, 60]),
])
def test_sample_pages(pages, expected):
    assert aicomicpdf.sample_pages(pages) == expected


@pytest.mark.parametrize("answer,expected", [
    ('{"kind": "comic", "confident": true, "why": "sequential panels"}',
     aicomicpdf.Verdict("comic", True, "sequential panels")),
    ('Sure!\n```json\n{"kind": "comic", "confident": true, "why": "manga"}\n```',
     aicomicpdf.Verdict("comic", True, "manga")),
    ('{"kind": "comic", "confident": false, "why": "maybe an artbook"}',
     aicomicpdf.Verdict("unsure", False, "maybe an artbook")),
    ('{"kind": "comic", "confidence": "low", "why": "hard to tell"}',
     aicomicpdf.Verdict("unsure", False, "hard to tell")),
    ('{"kind": "magazine", "confident": true, "why": "two-column articles"}',
     aicomicpdf.Verdict("magazine", False, "two-column articles")),
    ('{"kind": "text", "confident": true, "why": "novel pages"}',
     aicomicpdf.Verdict("text", False, "novel pages")),
    ('{"kind": "other", "confident": true, "why": "slide deck"}',
     aicomicpdf.Verdict("other", False, "slide deck")),
    ('{"kind": "unsure", "confident": false, "why": "blank"}',
     aicomicpdf.Verdict("unsure", False, "blank")),
    ("not json at all", None),
    ('{"kind": "novel", "confident": true}', None),
    ('{"confident": true}', None),
])
def test_parse_verdict_strictness(answer, expected):
    assert aicomicpdf.parse_verdict(answer) == expected


def test_already_comic_pdf_never_calls_model(tmp_path, monkeypatch):
    pdf = _make_pdf(tmp_path, "scanned-comic.pdf")
    monkeypatch.setenv("comicPdfLlm", "on")
    monkeypatch.setattr(comicpdf, "_stats_line", lambda _p, _m="0": ("24 23 300", 0))

    called = []
    monkeypatch.setattr(aicomicpdf, "judge", lambda *a, **kw: called.append(a))

    comic, pages, good, dpi = comicpdf.comic_pdf_verdict(pdf)
    assert (comic, pages, good, dpi) == (True, 24, 23, 300)
    assert called == []


def test_clearly_non_borderline_pdf_never_calls_model(tmp_path, monkeypatch):
    pdf = _make_pdf(tmp_path, "magazine.pdf")
    monkeypatch.setenv("comicPdfLlm", "on")
    monkeypatch.setattr(comicpdf, "_stats_line", lambda _p, _m="0": ("80 2 300", 0))

    called = []
    monkeypatch.setattr(aicomicpdf, "judge", lambda *a, **kw: called.append(a))

    comic, pages, good, dpi = comicpdf.comic_pdf_verdict(pdf)
    assert (comic, pages, good, dpi) == (False, 80, 2, 300)
    assert called == []


def test_zero_match_and_borderline_pdfs_render_pages_and_accept_comic(tmp_path, monkeypatch):
    pdf = _make_pdf(tmp_path, "Digital Graphic Novel.pdf")
    monkeypatch.setenv("comicPdfLlm", "on")
    cache = str(tmp_path / "verdicts.json")
    render_calls = []
    model = _VisionModel(
        '{"kind": "comic", "confident": true, "why": "vector comic panels"}'
    )

    verdict = aicomicpdf.judge(
        pdf, 24, 0, lookers=[model], cache_file=cache,
        render=_fake_render(render_calls),
    )

    assert verdict == aicomicpdf.Verdict("comic", True, "vector comic panels")
    assert render_calls == [(pdf, [1, 6, 14])]
    assert len(model.calls) == 1
    assert "Digital Graphic Novel.pdf" in model.calls[0]["prompt"]
    assert len(model.calls[0]["images"]) == 3


@pytest.mark.parametrize("raw_answer,expected_kind,cached", [
    ('{"kind": "magazine", "confident": true, "why": "articles"}', "magazine", True),
    ('{"kind": "text", "confident": true, "why": "manual"}', "text", True),
    ('{"kind": "other", "confident": true, "why": "catalogue"}', "other", True),
    ('{"kind": "unsure", "confident": false, "why": "ambiguous"}', "unsure", True),
    ('{"kind": "comic", "confident": false, "why": "unclear"}', "unsure", True),
    ("garbage response", None, False),
    (llmclient.LlmError("server down"), None, False),
])
def test_non_comic_and_errors_rejected(tmp_path, monkeypatch, raw_answer,
                                       expected_kind, cached):
    pdf = _make_pdf(tmp_path, "candidate.pdf")
    monkeypatch.setenv("comicPdfLlm", "on")
    cache = str(tmp_path / "verdicts.json")
    model = _VisionModel(raw_answer)

    verdict = aicomicpdf.judge(
        pdf, 20, 14, lookers=[model], cache_file=cache,
        render=_fake_render([]),
    )

    if expected_kind is None:
        assert verdict is None
    else:
        assert verdict is not None
        assert verdict.comic is False
        assert verdict.classification == expected_kind

    if cached:
        assert aicomicpdf.read_cache(pdf, cache) == verdict
    else:
        assert aicomicpdf.read_cache(pdf, cache) is None


def test_cache_hit_avoids_second_call_and_invalidation_on_size_or_mtime(
    tmp_path, monkeypatch
):
    pdf = _make_pdf(tmp_path, "tiled-scan.pdf", b"%PDF-1.7\noriginal")
    monkeypatch.setenv("comicPdfLlm", "on")
    cache = str(tmp_path / "verdicts.json")

    render_calls = []
    model1 = _VisionModel('{"kind": "comic", "confident": true, "why": "tiled"}')
    first = aicomicpdf.judge(
        pdf, 12, 0, lookers=[model1], cache_file=cache,
        render=_fake_render(render_calls),
    )
    assert first is not None and first.comic is True
    assert len(model1.calls) == 1
    assert len(render_calls) == 1

    # Second call on unchanged file hits cache: neither renderer nor model runs.
    model2 = _VisionModel('{"kind": "text", "confident": true, "why": "unused"}')
    second = aicomicpdf.judge(
        pdf, 12, 0, lookers=[model2], cache_file=cache,
        render=_fake_render(render_calls),
    )
    assert second == first
    assert model2.calls == []
    assert len(render_calls) == 1

    # Changing mtime invalidates cache and calls the model again.
    st = os.stat(pdf)
    os.utime(pdf, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))
    model3 = _VisionModel('{"kind": "magazine", "confident": true, "why": "updated"}')
    third = aicomicpdf.judge(
        pdf, 12, 0, lookers=[model3], cache_file=cache,
        render=_fake_render(render_calls),
    )
    assert third == aicomicpdf.Verdict("magazine", False, "updated")
    assert len(model3.calls) == 1
    assert len(render_calls) == 2

    # Changing size invalidates cache and calls the model again.
    pathlib.Path(pdf).write_bytes(b"%PDF-1.7\noriginal-plus-more-bytes")
    model4 = _VisionModel('{"kind": "comic", "confident": true, "why": "resized"}')
    fourth = aicomicpdf.judge(
        pdf, 12, 0, lookers=[model4], cache_file=cache,
        render=_fake_render(render_calls),
    )
    assert fourth == aicomicpdf.Verdict("comic", True, "resized")
    assert len(model4.calls) == 1
    assert len(render_calls) == 3


def test_switch_off_and_no_vision_model_and_meter_recording(tmp_path, monkeypatch):
    pdf = _make_pdf(tmp_path, "vector.pdf")
    cache = str(tmp_path / "verdicts.json")
    meter = str(tmp_path / "meter.jsonl")

    # Switch off: never asks model.
    monkeypatch.setenv("comicPdfLlm", "off")
    model = _VisionModel('{"kind": "comic", "confident": true, "why": "comic"}')
    render_calls = []
    assert aicomicpdf.judge(
        pdf, 16, 0, lookers=[model], meter_file=meter, cache_file=cache,
        render=_fake_render(render_calls),
    ) is None
    assert model.calls == []
    assert render_calls == []

    # Switch on, but no vision model in roster: returns None cleanly.
    monkeypatch.setenv("comicPdfLlm", "on")
    monkeypatch.setattr(
        llmroster, "optional",
        lambda _task: ([], "no configured model has vision"),
    )
    assert aicomicpdf.judge(
        pdf, 16, 0, meter_file=meter, cache_file=cache,
        render=_fake_render(render_calls),
    ) is None
    assert render_calls == []

    # Model that claims not to see images is skipped without calling ask().
    blind = _VisionModel('{"kind": "comic", "confident": true}', sees=False)
    assert aicomicpdf.judge(
        pdf, 16, 0, lookers=[blind], meter_file=meter, cache_file=cache,
        render=_fake_render(render_calls),
    ) is None
    assert blind.calls == []

    # Working vision model records usage in modelmeter.
    verdict = aicomicpdf.judge(
        pdf, 16, 0, lookers=[model], meter_file=meter, cache_file=cache,
        render=_fake_render(render_calls),
    )
    assert verdict is not None and verdict.comic is True
    spans, tally = modelmeter.gathered(meter)
    assert tally.requests == 1
    assert tally.tokens == 42
    assert tally.read == 600
    assert spans.seconds >= 0.0


def test_convert_comics_and_content_census_agree_via_shared_cache(
    tmp_path, monkeypatch
):
    comic_pdf = _make_pdf(tmp_path, "Digital Issue 01.pdf", b"%PDF-1.7\ncomic")
    mag_pdf = _make_pdf(tmp_path, "Monthly Mag.pdf", b"%PDF-1.7\nmag")
    cache = str(tmp_path / "shared-verdicts.json")
    meter = str(tmp_path / "shared-meter")

    monkeypatch.setenv("comicPdfLlm", "on")
    monkeypatch.setenv("comicPdfCache", cache)
    runcontext.settle(aicomicpdf.METER_VARIABLE, meter)
    contentcensus.census_init()
    runcontext.settle("CENSUS_HAVE_POPPLER", "1")

    # Deterministic check returns 0 matching pages for both (e.g. digital art
    # and a magazine layout).
    monkeypatch.setattr(comicpdf, "_stats_line", lambda _p, _m="0": ("24 0 300", 0))

    render_calls = []
    monkeypatch.setattr(aicomicpdf, "render_sample_pages", _fake_render(render_calls))

    answers = {
        "Digital Issue 01.pdf": '{"kind": "comic", "confident": true, "why": "panels"}',
        "Monthly Mag.pdf": '{"kind": "magazine", "confident": true, "why": "articles"}',
    }
    asked = []

    class _DispatchModel:
        name = "dispatch-vision"
        model = "dispatch-vision"

        @contextlib.contextmanager
        def serving(self, log):
            yield self

        def sees_images(self):
            return True

        def ask(self, system, prompt, tally=None, images=()):
            asked.append(prompt)
            if tally is not None:
                tally.add(1.0, 20, 400)
            for fname, resp in answers.items():
                if fname in prompt:
                    return resp
            raise AssertionError("unexpected prompt: " + prompt)

    monkeypatch.setattr(llmroster, "optional", lambda _task: ([_DispatchModel()], ""))

    # 1) convert-comics path calls comic_pdf_verdict -> asks vision model once each.
    assert comicpdf.comic_pdf_verdict(comic_pdf) == (True, 24, 0, 300)
    assert comicpdf.comic_pdf_verdict(mag_pdf) == (False, 24, 0, 300)
    assert len(asked) == 2

    # 2) content-census path classifies the exact same files -> agrees via cache,
    #    making ZERO extra model calls.
    assert contentcensus.census_classify(comic_pdf) == ("comics", "24 0 300")
    assert contentcensus.census_classify(mag_pdf) == ("books", "24 0 300")
    assert len(asked) == 2

    # Verify the cache file holds both verdicts and modelmeter recorded 2 requests.
    stored = json.loads(pathlib.Path(cache).read_text(encoding="utf-8"))
    assert len(stored) == 2
    _spans, tally = modelmeter.gathered(meter)
    assert tally.requests == 2
