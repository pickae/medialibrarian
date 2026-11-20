"""Tests for asking a language model to fill empty audiobook metadata tags -
medialib.lib.aibooktags (plus mutagentags book-tag helpers and aicover meta).
"""

import contextlib
import json
import os
import struct

import pytest

from medialib.lib import (
    aibooktags,
    aicover,
    llmclient,
    llmroster,
    modelmeter,
    mutagentags,
    onlinecover,
    runlog,
)

pytestmark = pytest.mark.pure

# 42-byte valid FLAC (fLaC + STREAMINFO block) that mutagen loads and saves.
_MINI_FLAC = (b"fLaC" + bytes([0x80, 0x00, 0x00, 0x22])
              + struct.pack(">HH", 4092, 4092)
              + b"\x00" * 6
              + struct.pack(">Q", (44100 << 44) | (2 << 41) | (16 << 36))
              + b"\x00" * 16)

# Four consecutive MPEG1 Layer 3 frames (128 kbps, 44.1 kHz) so mutagen's
# MPEGInfo accepts the file without needing ffmpeg.
_MINI_MP3 = (b"\xff\xfb\x90\x00" + b"\x00" * 413) * 4

_JPEG = b"\xff\xd8\xff" + b"\0" * 4000


class _Model:
    """Stand-in web-searching LLM that records prompts and answers."""

    def __init__(self, answer, can_search=True, name="stand-in"):
        self.answer = answer
        self.can_search = can_search
        self.name = name
        self.model = name
        self.asked = []
        self.purpose = ""

    def searching(self, purpose=""):
        self.purpose = purpose
        return self if self.can_search else None

    @contextlib.contextmanager
    def serving(self, log):
        yield self

    def ask(self, system, prompt, tally=None, images=()):
        self.asked.append((system, prompt))
        if isinstance(self.answer, Exception):
            raise self.answer
        if tally is not None:
            tally.add(1.5, 30, 500)
        return self.answer


def _book_folder(tmp_path):
    folder = tmp_path / "Ann Mirelle" / "The Quiet Orchard (Unabridged)"
    (folder / "CD1").mkdir(parents=True)
    (folder / "CD1" / "01 Opening.mp3").write_bytes(b"x" * 10)
    (folder / "CD1" / "02 The Gate.mp3").write_bytes(b"x" * 10)
    (folder / "notes.txt").write_text("read by Clara Vale")
    return folder


def _tracks(folder):
    return sorted(str(p) for p in folder.rglob("*.mp3"))


def _probe_tags(path):
    return {
        "album": "The Quiet Orchard",
        "artist": "Ann Mirelle",
        "title": os.path.basename(path),
        "language": "eng",
    }


def _flac(tmp_path, name="The Quiet Orchard.flac"):
    path = tmp_path / name
    path.write_bytes(_MINI_FLAC)
    return path


def _mp3(tmp_path, name="The Quiet Orchard.mp3"):
    path = tmp_path / name
    path.write_bytes(_MINI_MP3)
    return path


@pytest.fixture(autouse=True)
def _on(monkeypatch):
    monkeypatch.setenv(aibooktags.SWITCH_VARIABLE, "on")


def test_task_traits_in_roster():
    assert llmroster.BOOK_TAGS.name == "book-tags"
    assert llmroster.BOOK_TAGS.requires == ("web",)
    assert llmroster.BOOK_TAGS.prefers == ("reasoning",)
    assert llmroster.BOOK_TAGS.lean == "balanced"
    assert llmroster.TASKS["book-tags"] is llmroster.BOOK_TAGS


class TestPromptBuilding:
    def test_prompt_includes_files_tags_existing_and_cover_hints(self, tmp_path):
        folder = _book_folder(tmp_path)
        subject = onlinecover.subject_for(folder.name, 9 * 3600,
                                          tag_titles=["The Quiet Orchard"],
                                          tag_creators=["Ann Mirelle"])
        text = aibooktags.prompt(
            str(folder), "The Quiet Orchard", _tracks(folder), subject,
            existing_tags={"title": "The Quiet Orchard"},
            cover_hints={"title": "The Quiet Orchard", "language": "English",
                         "edition": "Unabridged audiobook"},
            probe=_probe_tags,
        )
        assert "The Quiet Orchard (Unabridged)/" in text
        assert "CD1/01 Opening.mp3" in text and "notes.txt" in text
        assert "artist: Ann Mirelle" in text
        assert "The Quiet Orchard\n(the folder's name, cleaned)" in text
        assert "Tags already set on the output file" in text
        assert "title=The Quiet Orchard" in text
        assert "Hints from cover search" in text
        assert "edition=Unabridged audiobook" in text
        assert "Empty tags still needed: author, narrator" in text

    def test_prompt_works_without_cover_hints(self, tmp_path):
        folder = _book_folder(tmp_path)
        subject = onlinecover.subject_for(folder.name, 5 * 3600)
        text = aibooktags.prompt(
            str(folder), "The Quiet Orchard", _tracks(folder), subject,
            probe=_probe_tags,
        )
        assert "Hints from cover search" not in text
        assert "Empty tags still needed: title, author" in text


class TestParsingAndValidation:
    def test_parses_full_valid_json_in_fence(self):
        raw = (
            "Here is what I verified:\n```json\n"
            + json.dumps({
                "title": "  The Quiet Orchard  ",
                "author": "Ann Mirelle",
                "narrator": "Clara Vale",
                "series": "Orchard Chronicles",
                "series_index": 2.0,
                "year": 2019,
                "language": "English",
                "abridged": False,
            })
            + "\n```\n"
        )
        tags = aibooktags.parse_tags(raw)
        assert tags == aibooktags.BookTags(
            title="The Quiet Orchard",
            author="Ann Mirelle",
            narrator="Clara Vale",
            series="Orchard Chronicles",
            series_index="2",
            year="2019",
            language="eng",
            abridged=False,
        )
        assert tags.to_tag_map()["abridged"] == "false"

    def test_validates_year_strictly(self):
        for good, expected in [(1984, "1984"), ("2024", "2024"),
                               (" 1999 ", "1999"), (1000, "1000"),
                               (2100, "2100")]:
            parsed = aibooktags.parse_tags(json.dumps({"year": good}))
            assert parsed.year == expected
        for bad in [True, False, 999, 2101, "99", "2020s", "circa 1999",
                    2020.5, None, ["2020"]]:
            parsed = aibooktags.parse_tags(json.dumps({"year": bad}))
            assert parsed.year == ""

    def test_validates_series_index_strictly(self):
        for good, expected in [(1, "1"), (12, "12"), (1.5, "1.5"),
                               (2.0, "2"), ("03", "3"), ("0.5", "0.5"),
                               ("2.25", "2.25")]:
            parsed = aibooktags.parse_tags(json.dumps({"series_index": good}))
            assert parsed.series_index == expected
        for bad in [True, False, 0, -1, 10000, "0", "-2", "Book 2",
                    "1.234", 1.234, None]:
            parsed = aibooktags.parse_tags(json.dumps({"series_index": bad}))
            assert parsed.series_index == ""

    def test_validates_language_codes_and_names(self):
        for good, expected in [("en", "eng"), ("eng", "eng"), ("en-US", "eng"),
                               ("German", "deu"), ("fr_CA", "fra"),
                               ("ja", "jpn")]:
            parsed = aibooktags.parse_tags(json.dumps({"language": good}))
            assert parsed.language == expected
        for bad in ["", "und", "unknown", "klingon-99", "en\nde", 123, None]:
            parsed = aibooktags.parse_tags(json.dumps({"language": bad}))
            assert parsed.language == ""

    def test_validates_abridged_and_text_limits(self):
        assert aibooktags.parse_tags(json.dumps({"abridged": True})).abridged is True
        assert aibooktags.parse_tags(json.dumps({"abridged": False})).abridged is False
        for bad in ["true", "false", 1, 0, None]:
            assert aibooktags.parse_tags(json.dumps({"abridged": bad})).abridged is None

        bad_texts = aibooktags.parse_tags(json.dumps({
            "title": "Line one\nLine two",
            "author": "A" * (aibooktags.MAX_TEXT_LENGTH + 1),
            "narrator": "   ",
            "series": "Bad\x00Null",
        }))
        assert bad_texts == aibooktags.BookTags()

    def test_non_json_or_non_object_returns_empty(self):
        assert aibooktags.parse_tags("not json at all") == aibooktags.BookTags()
        assert aibooktags.parse_tags('["list", "not", "object"]') == aibooktags.BookTags()


class TestMergeHints:
    def test_merges_only_missing_fields_from_cover_hints(self):
        base = aibooktags.BookTags(title="Model Title", author="Ann Mirelle")
        merged = aibooktags.merge_hints(base, {
            "title": "Cover Title",
            "language": "de-DE",
            "edition": "Unabridged audiobook",
        })
        assert merged.title == "Model Title"
        assert merged.author == "Ann Mirelle"
        assert merged.language == "deu"
        assert merged.abridged is False

    def test_detects_abridged_edition_hint(self):
        merged = aibooktags.merge_hints(
            aibooktags.BookTags(),
            {"title": "Shortened", "edition": "Abridged edition"},
        )
        assert merged.title == "Shortened"
        assert merged.abridged is True

    def test_aicover_populates_found_meta(self, tmp_path, monkeypatch):
        monkeypatch.setenv(aicover.SWITCH_VARIABLE, "on")
        folder = _book_folder(tmp_path)
        answer = json.dumps({
            "images": [{
                "url": "https://covers.example/orchard.jpg",
                "title": "The Quiet Orchard",
                "language": "English",
                "edition": "Unabridged audiobook",
            }],
        })
        model = _Model(answer)

        def download(url, dest):
            with open(dest, "wb") as handle:
                handle.write(_JPEG)
            return True

        meta: dict[str, str] = {}
        image = aicover.book_cover(
            str(folder), "The Quiet Orchard", _tracks(folder), 5 * 3600,
            str(tmp_path / "out"),
            searchers=[model], lookers=[], download=download,
            probe=_probe_tags, found_meta=meta,
        )
        assert image
        assert meta == {
            "title": "The Quiet Orchard",
            "language": "English",
            "edition": "Unabridged audiobook",
        }


class TestEmptyOnlyTagWriting:
    def test_fills_only_empty_tags_on_flac_without_overwriting(self, tmp_path):
        flac = _flac(tmp_path)
        assert mutagentags.set_tag(str(flac), "TITLE", "Original Title") == 0
        assert mutagentags.set_tag(str(flac), "ARTIST", "Original Author") == 0
        assert mutagentags.set_tag(str(flac), "YEAR", "1995") == 0

        written: list[str] = []
        status = mutagentags.fill_book_tags(str(flac), {
            "title": "Model Title",
            "author": "Model Author",
            "narrator": "Clara Vale",
            "series": "Orchard Chronicles",
            "series_index": "1",
            "year": "2020",
            "language": "eng",
            "abridged": "false",
        }, written=written)
        assert status == 0
        assert written == ["narrator", "series", "series_index",
                           "language", "abridged"]
        tags = mutagentags.read_book_tags(str(flac))
        assert tags == {
            "title": "Original Title",
            "author": "Original Author",
            "narrator": "Clara Vale",
            "series": "Orchard Chronicles",
            "series_index": "1",
            "year": "1995",
            "language": "eng",
            "abridged": "false",
        }

    def test_fills_only_empty_tags_on_mp3_without_overwriting(self, tmp_path):
        from mutagen.id3 import TIT2, TPE1
        from mutagen.mp3 import MP3

        mp3_path = _mp3(tmp_path)
        audio = MP3(str(mp3_path))
        audio.add_tags()
        audio.tags.add(TIT2(encoding=3, text=["Existing MP3 Title"]))
        audio.tags.add(TPE1(encoding=3, text=["Existing MP3 Author"]))
        audio.save()

        written: list[str] = []
        status = mutagentags.fill_book_tags(str(mp3_path), {
            "title": "Replaced Title",
            "author": "Replaced Author",
            "narrator": "Clara Vale",
            "series": "Orchard",
            "series_index": "2",
            "year": "2018",
            "language": "eng",
            "abridged": "true",
        }, written=written)
        assert status == 0
        assert written == ["narrator", "series", "series_index",
                           "year", "language", "abridged"]
        tags = mutagentags.read_book_tags(str(mp3_path))
        assert tags["title"] == "Existing MP3 Title"
        assert tags["author"] == "Existing MP3 Author"
        assert tags["narrator"] == "Clara Vale"
        assert tags["series"] == "Orchard"
        assert tags["series_index"] == "2"
        assert tags["year"] == "2018"
        assert tags["language"] == "eng"
        assert tags["abridged"] == "true"


class TestTagBookWorkflow:
    def test_tag_book_fills_empty_keeps_existing_records_meter_and_never_renames(
        self, tmp_path, capsys,
    ):
        folder = _book_folder(tmp_path)
        joined = _flac(tmp_path, "Custom Folder Name.flac")
        assert mutagentags.set_tag(str(joined), "TITLE", "Custom Folder Name") == 0
        meter = tmp_path / "meter"
        model = _Model(json.dumps({
            "title": "Official Book Title",
            "author": "Ann Mirelle",
            "narrator": "Clara Vale",
            "series": "Orchard Chronicles",
            "series_index": 1,
            "year": 2019,
            "language": "English",
            "abridged": False,
        }), name="claude")

        written = aibooktags.tag_book(
            str(joined), str(folder), "Custom Folder Name",
            _tracks(folder), 9 * 3600,
            searchers=[model], meter_file=str(meter), probe=_probe_tags,
        )

        # File path is never renamed.
        assert joined.is_file()
        assert not (tmp_path / "Official Book Title.flac").exists()
        # Title was already set, so it was not overwritten.
        assert "title" not in written
        assert written == ["author", "narrator", "series", "series_index",
                           "year", "language", "abridged"]
        tags = mutagentags.read_book_tags(str(joined))
        assert tags["title"] == "Custom Folder Name"
        assert tags["author"] == "Ann Mirelle"
        assert tags["narrator"] == "Clara Vale"
        assert tags["series"] == "Orchard Chronicles"
        assert tags["series_index"] == "1"
        assert tags["year"] == "2019"
        assert tags["language"] == "eng"
        assert tags["abridged"] == "false"

        # Usage recorded in modelmeter and decision logged to stderr.
        _spans, tally = modelmeter.gathered(str(meter))
        assert tally.requests == 1
        err = capsys.readouterr().err
        assert "Book tags filled (claude):" in err
        assert "author=Ann Mirelle" in err
        assert aibooktags.PURPOSE in err

    def test_drops_orphan_series_index_when_no_series(self, tmp_path):
        folder = _book_folder(tmp_path)
        joined = _flac(tmp_path)
        model = _Model(json.dumps({
            "author": "Ann Mirelle",
            "series": None,
            "series_index": 3,
        }))
        written = aibooktags.tag_book(
            str(joined), str(folder), "The Quiet Orchard",
            _tracks(folder), 5 * 3600,
            searchers=[model], probe=_probe_tags,
        )
        assert written == ["author"]
        assert "series_index" not in mutagentags.read_book_tags(str(joined))

    def test_skips_model_when_all_tags_already_filled(self, tmp_path):
        folder = _book_folder(tmp_path)
        joined = _flac(tmp_path)
        assert mutagentags.fill_book_tags(str(joined), {
            "title": "T", "author": "A", "narrator": "N", "series": "S",
            "series_index": "1", "year": "2020", "language": "eng",
            "abridged": "false",
        }) == 0
        model = _Model(json.dumps({"author": "Other"}))
        written = aibooktags.tag_book(
            str(joined), str(folder), "T", _tracks(folder), 5 * 3600,
            searchers=[model], probe=_probe_tags,
        )
        assert written == []
        assert model.asked == []

    def test_switch_off_asks_no_model_and_writes_nothing(
        self, tmp_path, monkeypatch, capsys,
    ):
        monkeypatch.setenv(aibooktags.SWITCH_VARIABLE, "off")
        monkeypatch.setattr(runlog, "VERBOSE", True)
        folder = _book_folder(tmp_path)
        joined = _flac(tmp_path)
        model = _Model(json.dumps({"title": "Should Not Be Written"}))
        written = aibooktags.tag_book(
            str(joined), str(folder), "The Quiet Orchard",
            _tracks(folder), 5 * 3600,
            searchers=[model], probe=_probe_tags,
        )
        assert written == []
        assert model.asked == []
        assert mutagentags.read_book_tags(str(joined)) == {}
        assert aibooktags.SWITCH_VARIABLE in capsys.readouterr().err

    def test_no_model_available_skips_cleanly(
        self, tmp_path, monkeypatch, capsys,
    ):
        monkeypatch.setattr(runlog, "VERBOSE", True)
        monkeypatch.setattr(
            llmroster, "optional",
            lambda task: ([], "no model configured for book-tags"),
        )
        folder = _book_folder(tmp_path)
        joined = _flac(tmp_path)
        written = aibooktags.tag_book(
            str(joined), str(folder), "The Quiet Orchard",
            _tracks(folder), 5 * 3600,
            probe=_probe_tags,
        )
        assert written == []
        assert mutagentags.read_book_tags(str(joined)) == {}
        assert "no model configured for book-tags" in capsys.readouterr().err

    def test_no_model_available_still_applies_cover_hints(
        self, tmp_path, monkeypatch,
    ):
        monkeypatch.setattr(
            llmroster, "optional",
            lambda task: ([], "no model configured"),
        )
        folder = _book_folder(tmp_path)
        joined = _flac(tmp_path)
        written = aibooktags.tag_book(
            str(joined), str(folder), "The Quiet Orchard",
            _tracks(folder), 5 * 3600,
            cover_hints={"title": "The Quiet Orchard", "language": "English",
                         "edition": "Unabridged"},
            probe=_probe_tags,
        )
        assert written == ["title", "language", "abridged"]
        assert mutagentags.read_book_tags(str(joined)) == {
            "title": "The Quiet Orchard",
            "language": "eng",
            "abridged": "false",
        }

    def test_falls_back_on_model_error(self, tmp_path, capsys):
        folder = _book_folder(tmp_path)
        joined = _flac(tmp_path)
        broken = _Model(llmclient.LlmError("rate limited"), name="first")
        backup = _Model(json.dumps({"author": "Ann Mirelle", "year": 2019}),
                        name="second")
        written = aibooktags.tag_book(
            str(joined), str(folder), "The Quiet Orchard",
            _tracks(folder), 5 * 3600,
            searchers=[broken, backup], probe=_probe_tags,
        )
        assert written == ["author", "year"]
        err = capsys.readouterr().err
        assert "No book tags from first: rate limited" in err
        assert "Book tags filled (second): author=Ann Mirelle, year=2019" in err
