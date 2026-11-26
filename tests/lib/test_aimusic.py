"""Tests for reporting beets skips and asking a web-searching language model
for MusicBrainz release IDs - medialib.lib.aimusic.

The model and beets runner are stand-ins. What is pinned: only lines newly
appended to beets.log are read; skip, duplicate-skip, asis and multi-disc
lines are parsed accurately; the prompt includes folder/file names, track
durations, tags and rip logs (including UTF-16 EAC logs); MusicBrainz IDs are
strictly validated as UUIDs; beets is retried quietly with --search-id; model
usage is metered; and disabled switch, missing model and dry-run leave beets
untouched.
"""

from __future__ import annotations

import os
import pathlib

import pytest

from medialib.lib import aimusic, llmclient, llmroster, modelmeter, runlog

pytestmark = pytest.mark.pure

_VALID_UUID = "12345678-1234-5678-9abc-def012345678"
_UPPER_UUID = "ABCDEF01-2345-6789-ABCD-EF0123456789"


class _Model:
    """A stand-in model that records prompts and returns ``answer`` (or raises
    it)."""

    def __init__(self, answer, *, can_search: bool = True, name: str = "stand-in"):
        self.answer = answer
        self.can_search = can_search
        self.name = name
        self.purpose = ""
        self.asked: list[tuple[str, str]] = []

    def searching(self, purpose: str = ""):
        self.purpose = purpose
        return self if self.can_search else None

    def ask(self, system: str, prompt: str, tally=None):
        self.asked.append((system, prompt))
        if isinstance(self.answer, Exception):
            raise self.answer
        if tally is not None:
            tally.add(1.5, 45, 1100)
        return self.answer


def _album(root: pathlib.Path, name: str = "Artist - Mystery Album (1999)") -> pathlib.Path:
    folder = root / name
    folder.mkdir(parents=True)
    (folder / "01 Opening.flac").write_bytes(b"a" * 100)
    (folder / "02 Middle.flac").write_bytes(b"b" * 200)
    (folder / "03 Finale.flac").write_bytes(b"c" * 150)
    (folder / "EAC.log").write_text(
        "Exact Audio Copy V1.6\nTOC of the extracted CD\nTrack 1 | 0:00.00 | 3:15.00\n",
        encoding="utf-8",
    )
    (folder / "album.cue").write_text(
        'REM DATE 1999\nPERFORMER "The Band"\nTITLE "Mystery Album"\n',
        encoding="utf-8",
    )
    return folder


def _probe(path: str) -> tuple[float, dict[str, str]]:
    name = os.path.basename(path)
    durations = {
        "01 Opening.flac": 195.0,
        "02 Middle.flac": 240.0,
        "03 Finale.flac": 305.0,
    }
    titles = {
        "01 Opening.flac": ("1", "Opening"),
        "02 Middle.flac": ("2", "Middle"),
        "03 Finale.flac": ("3", "Finale"),
    }
    track, title = titles.get(name, ("", ""))
    tags = {
        "album": "Mystery Album",
        "artist": "The Band",
        "date": "1999",
    }
    if track:
        tags["track"] = track
    if title:
        tags["title"] = title
    return durations.get(name, 0.0), tags


@pytest.fixture(autouse=True)
def _on(monkeypatch, tmp_path):
    monkeypatch.setenv(aimusic.SWITCH_VARIABLE, "on")
    # Ensure tests never pick up a real roster file on the machine.
    llmroster.forget()
    monkeypatch.setenv(llmroster.MODELS_VARIABLE, str(tmp_path / "no-models.toml"))
    for name in llmclient.SETTING_FIELDS.values():
        monkeypatch.delenv(name, raising=False)


class TestBeetsLogParsing:
    def test_missing_log_gives_zero_offset_and_no_skips(self, tmp_path):
        missing = str(tmp_path / "beets.log")
        assert aimusic.log_offset(missing) == 0
        assert aimusic.read_skips(missing, 0) == []

    def test_reads_only_lines_newly_appended_during_this_run(self, tmp_path):
        log_file = tmp_path / "beets.log"
        log_file.write_text(
            "import started Mon Jan  1 00:00:00 2026\n"
            "skip /music/Old Artist/Old Album\n",
            encoding="utf-8",
        )
        offset = aimusic.log_offset(str(log_file))
        with log_file.open("a", encoding="utf-8") as handle:
            handle.write(
                "import started Fri Nov 20 15:00:00 2026\n"
                "skip /music/New Artist/New Album\n"
                "duplicate-skip /music/Dup Artist/Dup Album\n"
                "duplicate-keep /music/Kept/Album\n"
                "duplicate-replace /music/Replaced/Album\n"
                "asis /music/AsIs/Album\n"
            )
        skips = aimusic.read_skips(str(log_file), offset)
        assert [(s.verb, s.folder) for s in skips] == [
            ("skip", "/music/New Artist/New Album"),
            ("duplicate-skip", "/music/Dup Artist/Dup Album"),
            ("asis", "/music/AsIs/Album"),
        ]

    def test_truncated_log_falls_back_to_start(self, tmp_path):
        log_file = tmp_path / "beets.log"
        log_file.write_text("x" * 500, encoding="utf-8")
        offset = aimusic.log_offset(str(log_file))
        log_file.write_text("skip /music/Fresh/Album\n", encoding="utf-8")
        skips = aimusic.read_skips(str(log_file), offset)
        assert [s.folder for s in skips] == ["/music/Fresh/Album"]

    def test_multi_disc_paths_collapse_to_common_folder_posix(self):
        text = (
            "skip /music/Beatles/White Album/CD 1; /music/Beatles/White Album/CD 2\n"
        )
        skips = aimusic.parse_beets_log(text)
        assert len(skips) == 1
        assert skips[0].folder == "/music/Beatles/White Album"
        assert skips[0].paths == (
            "/music/Beatles/White Album/CD 1",
            "/music/Beatles/White Album/CD 2",
        )

    def test_multi_disc_paths_collapse_to_common_folder_windows(self):
        text = (
            r"skip C:\music\Beatles\White Album\CD 1; C:\music\Beatles\White Album\CD 2"
            "\n"
        )
        skips = aimusic.parse_beets_log(text)
        assert len(skips) == 1
        assert skips[0].folder == r"C:\music\Beatles\White Album"
        assert len(skips[0].paths) == 2

    def test_semicolon_inside_folder_name_is_not_split(self):
        text = "skip /music/Bach; Handel/Concertos\n"
        skips = aimusic.parse_beets_log(text)
        assert len(skips) == 1
        assert skips[0].folder == "/music/Bach; Handel/Concertos"
        assert skips[0].paths == ("/music/Bach; Handel/Concertos",)

    def test_semicolon_in_folder_name_with_multi_disc(self):
        text = (
            "skip /music/Bach; Handel/Concertos/CD 1; "
            "/music/Bach; Handel/Concertos/CD 2\n"
        )
        skips = aimusic.parse_beets_log(text)
        assert len(skips) == 1
        assert skips[0].folder == "/music/Bach; Handel/Concertos"
        assert skips[0].paths == (
            "/music/Bach; Handel/Concertos/CD 1",
            "/music/Bach; Handel/Concertos/CD 2",
        )

    def test_duplicate_lines_are_deduplicated_in_order(self):
        text = (
            "skip /music/A\n"
            "skip /music/B\n"
            "skip /music/A\n"
        )
        assert [s.folder for s in aimusic.parse_beets_log(text)] == [
            "/music/A",
            "/music/B",
        ]


class TestPromptBuilding:
    def test_prompt_includes_folder_files_durations_tags_and_rip_logs(self, tmp_path):
        folder = _album(tmp_path)
        text = aimusic.prompt(str(folder), probe=_probe)
        assert "Artist - Mystery Album (1999)" in text
        assert "01 Opening.flac" in text
        assert "3:15" in text
        assert "4:00" in text
        assert "5:05" in text
        assert "total 12:20" in text
        assert "Tags shared across all 3 listed tracks:" in text
        assert "album: Mystery Album" in text
        assert "artist: The Band" in text
        assert "title: Opening" in text
        assert "Exact Audio Copy V1.6" in text
        assert 'PERFORMER "The Band"' in text

    def test_utf16_eac_log_is_decoded_cleanly(self, tmp_path):
        folder = tmp_path / "UTF16 Album"
        folder.mkdir()
        (folder / "01.flac").write_bytes(b"x" * 10)
        (folder / "rip.log").write_bytes(
            "Exact Audio Copy V1.3\r\nCatalog: CAT-042\r\n".encode("utf-16")
        )
        text = aimusic.prompt(str(folder), probe=lambda _p: (120.0, {}))
        assert "Exact Audio Copy V1.3" in text
        assert "Catalog: CAT-042" in text
        assert "no tags" in text

    def test_long_listing_and_huge_rip_log_are_truncated(self, tmp_path, monkeypatch):
        monkeypatch.setattr(aimusic, "MAX_LISTED_FILES", 2)
        monkeypatch.setattr(aimusic, "MAX_RIP_LOG_CHARS", 40)
        folder = _album(tmp_path)
        (folder / "big.log").write_text("A" * 200, encoding="utf-8")
        text = aimusic.prompt(str(folder), probe=_probe)
        assert "... and " in text
        assert "... (truncated)" in text


class TestMbidValidation:
    @pytest.mark.parametrize("raw, expected", [
        (_VALID_UUID, _VALID_UUID),
        (_UPPER_UUID, _UPPER_UUID.lower()),
        ("  " + _VALID_UUID + " \n", _VALID_UUID),
        ("", ""),
        (None, ""),
        (12345, ""),
        ("not-a-uuid", ""),
        ("https://musicbrainz.org/release/" + _VALID_UUID, ""),
        (_VALID_UUID.replace("-", ""), ""),
        (_VALID_UUID + "0", ""),
        (_VALID_UUID[:-1] + "g", ""),
        ("--delete", ""),
        (_VALID_UUID + "; rm -rf /", ""),
    ])
    def test_valid_mbid_is_strict(self, raw, expected):
        assert aimusic.valid_mbid(raw) == expected

    def test_parse_answer_handles_fenced_json_valid_invalid_and_null(self):
        fenced = (
            'Sure:\n```json\n{"mbid": "%s", "release": "Band - Album (1999)", '
            '"why": "TOC matches"}\n```' % _UPPER_UUID
        )
        decision = aimusic.parse_answer(fenced, model="claude")
        assert decision.mbid == _UPPER_UUID.lower()
        assert decision.raw_mbid == ""
        assert decision.release == "Band - Album (1999)"
        assert decision.why == "TOC matches"
        assert decision.model == "claude"

        null_dec = aimusic.parse_answer(
            '{"mbid": null, "release": "", "why": "bootleg not in MB"}'
        )
        assert null_dec.mbid == ""
        assert null_dec.raw_mbid == ""
        assert null_dec.why == "bootleg not in MB"

        bad_dec = aimusic.parse_answer(
            '{"mbid": "https://musicbrainz.org/release/' + _VALID_UUID + '"}'
        )
        assert bad_dec.mbid == ""
        assert "musicbrainz.org" in bad_dec.raw_mbid

        garbage = aimusic.parse_answer("no json here")
        assert garbage.mbid == ""
        assert garbage.raw_mbid == ""


class TestResolveSkipped:
    def _capture_logs(self, monkeypatch) -> list[str]:
        lines: list[str] = []
        monkeypatch.setattr(aimusic, "_log", lines.append)
        monkeypatch.setattr(aimusic, "_detail", lines.append)
        monkeypatch.setattr(runlog, "log", lines.append)
        return lines

    def test_a_layout_that_moves_albums_retries_with_its_own_options(
        self, tmp_path, monkeypatch
    ):
        """The retry moves the album where the first pass would have: the
        layout's -d before import, and -m in place of -C."""
        self._capture_logs(monkeypatch)
        ingest = tmp_path / "ingest"
        album = _album(ingest)
        beets_log = tmp_path / "beets.log"
        beets_log.write_text(f"skip {album}\n", encoding="utf-8")
        model = _Model('{"mbid": "%s"}' % _VALID_UUID)
        beet_calls: list[list[str]] = []

        def fake_beet(cwd: str, argv: list[str]) -> int:
            beet_calls.append(argv)
            return 0

        aimusic.resolve_skipped(
            str(ingest), "/cfg/beets-plex.yaml", str(beets_log), 0,
            searchers=[model], meter_file=str(tmp_path / "meter"),
            probe=_probe, run_beet=fake_beet,
            beet_options=["-d", str(ingest)], import_flags="-mIq",
        )

        assert beet_calls[0][:7] == [
            "beet", "-c", "/cfg/beets-plex.yaml", "-d", str(ingest),
            "import", "-mIq",
        ]

    def test_valid_mbid_retries_beets_quietly_and_records_meter(
        self, tmp_path, monkeypatch
    ):
        lines = self._capture_logs(monkeypatch)
        ingest = tmp_path / "ingest"
        album = _album(ingest)
        beets_log = tmp_path / "beets.log"
        beets_log.write_text(f"skip {album}\n", encoding="utf-8")
        meter = tmp_path / "meter"
        model = _Model(
            '{"mbid": "%s", "release": "The Band - Mystery Album (1999)", '
            '"why": "exact track durations"}' % _VALID_UUID
        )
        beet_calls: list[tuple[str, list[str]]] = []

        def fake_beet(cwd: str, argv: list[str]) -> int:
            beet_calls.append((cwd, argv))
            with beets_log.open("a", encoding="utf-8") as handle:
                handle.write("import started Fri Nov 20 15:01:00 2026\n")
            return 0

        summary = aimusic.resolve_skipped(
            str(ingest),
            "/cfg/beets.yaml",
            str(beets_log),
            0,
            searchers=[model],
            meter_file=str(meter),
            probe=_probe,
            run_beet=fake_beet,
        )

        assert summary.skipped == 1
        assert summary.matched == 1
        assert summary.still_skipped == 0
        assert model.purpose == "music"
        assert len(beet_calls) == 1
        cwd, argv = beet_calls[0]
        assert cwd == str(ingest)
        assert argv == [
            "beet",
            "-c",
            "/cfg/beets.yaml",
            "import",
            "-CIq",
            "-l",
            str(beets_log),
            "--search-id",
            _VALID_UUID,
            str(album),
        ]
        spans, tally = modelmeter.gathered(str(meter))
        assert (tally.requests, tally.tokens, tally.read) == (1, 45, 1100)
        assert any("Beets skipped 1 unmatched album(s):" in line for line in lines)
        assert any(_VALID_UUID in line and "tagged by beets" in line for line in lines)

    def test_weak_match_still_skipped_by_beets_is_reported(
        self, tmp_path, monkeypatch
    ):
        lines = self._capture_logs(monkeypatch)
        ingest = tmp_path / "ingest"
        album = _album(ingest)
        beets_log = tmp_path / "beets.log"
        beets_log.write_text(f"skip {album}\n", encoding="utf-8")
        model = _Model('{"mbid": "%s", "release": "Guess"}' % _VALID_UUID)

        def fake_beet(cwd: str, argv: list[str]) -> int:
            with beets_log.open("a", encoding="utf-8") as handle:
                handle.write(f"skip {album}\n")
            return 0

        summary = aimusic.resolve_skipped(
            str(ingest),
            "/cfg/beets.yaml",
            str(beets_log),
            0,
            searchers=[model],
            probe=_probe,
            run_beet=fake_beet,
        )
        assert summary.skipped == 1
        assert summary.matched == 0
        assert summary.still_skipped == 1
        assert any("beets still skipped" in line for line in lines)

    def test_invalid_mbid_from_model_never_calls_beets(self, tmp_path, monkeypatch):
        lines = self._capture_logs(monkeypatch)
        ingest = tmp_path / "ingest"
        album = _album(ingest)
        beets_log = tmp_path / "beets.log"
        beets_log.write_text(f"skip {album}\n", encoding="utf-8")
        model = _Model('{"mbid": "--delete-all", "why": "oops"}')
        beet_calls: list[list[str]] = []

        summary = aimusic.resolve_skipped(
            str(ingest),
            "/cfg/beets.yaml",
            str(beets_log),
            0,
            searchers=[model],
            probe=_probe,
            run_beet=lambda _cwd, argv: (beet_calls.append(argv), 0)[1],
        )
        assert beet_calls == []
        assert summary.still_skipped == 1
        assert any("invalid MusicBrainz ID" in line for line in lines)

    def test_disabled_switch_reports_skips_without_asking_model(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setenv(aimusic.SWITCH_VARIABLE, "off")
        lines = self._capture_logs(monkeypatch)
        ingest = tmp_path / "ingest"
        album = _album(ingest)
        beets_log = tmp_path / "beets.log"
        beets_log.write_text(
            f"skip {album}\nduplicate-skip {ingest / 'Dup'}\n",
            encoding="utf-8",
        )
        model = _Model('{"mbid": "%s"}' % _VALID_UUID)
        beet_calls: list[list[str]] = []

        summary = aimusic.resolve_skipped(
            str(ingest),
            "/cfg/beets.yaml",
            str(beets_log),
            0,
            searchers=[model],
            probe=_probe,
            run_beet=lambda _cwd, argv: (beet_calls.append(argv), 0)[1],
        )
        assert summary.skipped == 1
        assert summary.duplicate_skips == 1
        assert summary.still_skipped == 1
        assert model.asked == []
        assert beet_calls == []
        assert any("musicSearchLlm setting is off" in line for line in lines)

    def test_no_model_configured_reports_skips_and_reason(
        self, tmp_path, monkeypatch
    ):
        lines = self._capture_logs(monkeypatch)
        ingest = tmp_path / "ingest"
        album = _album(ingest)
        beets_log = tmp_path / "beets.log"
        beets_log.write_text(f"skip {album}\n", encoding="utf-8")
        beet_calls: list[list[str]] = []

        summary = aimusic.resolve_skipped(
            str(ingest),
            "/cfg/beets.yaml",
            str(beets_log),
            0,
            probe=_probe,
            run_beet=lambda _cwd, argv: (beet_calls.append(argv), 0)[1],
        )
        assert summary.skipped == 1
        assert summary.still_skipped == 1
        assert beet_calls == []
        assert any("No model asked for unmatched albums:" in line for line in lines)

    def test_dry_run_asks_model_but_never_reruns_beets(self, tmp_path, monkeypatch):
        lines = self._capture_logs(monkeypatch)
        ingest = tmp_path / "ingest"
        album = _album(ingest)
        beets_log = tmp_path / "beets.log"
        beets_log.write_text(f"skip {album}\n", encoding="utf-8")
        model = _Model('{"mbid": "%s", "release": "Album"}' % _VALID_UUID)
        beet_calls: list[list[str]] = []

        summary = aimusic.resolve_skipped(
            str(ingest),
            "/cfg/beets.yaml",
            str(beets_log),
            0,
            searchers=[model],
            probe=_probe,
            run_beet=lambda _cwd, argv: (beet_calls.append(argv), 0)[1],
            dry_run=True,
        )
        assert len(model.asked) == 1
        assert beet_calls == []
        assert summary.matched == 0
        assert summary.still_skipped == 1
        assert any("dry-run" in line and _VALID_UUID in line for line in lines)

    def test_fallback_across_searchers_on_non_searching_or_error(
        self, tmp_path, monkeypatch
    ):
        self._capture_logs(monkeypatch)
        ingest = tmp_path / "ingest"
        album = _album(ingest)
        beets_log = tmp_path / "beets.log"
        beets_log.write_text(f"skip {album}\n", encoding="utf-8")

        no_web = _Model('{"mbid": "%s"}' % _VALID_UUID, can_search=False, name="local")
        broken = _Model(llmclient.LlmError("timeout"), name="flaky")
        good = _Model('{"mbid": "%s", "release": "Found"}' % _VALID_UUID, name="claude")

        summary = aimusic.resolve_skipped(
            str(ingest),
            "/cfg/beets.yaml",
            str(beets_log),
            0,
            searchers=[no_web, broken, good],
            probe=_probe,
            run_beet=lambda _cwd, _argv: 0,
        )
        assert no_web.asked == []
        assert len(broken.asked) == 1
        assert len(good.asked) == 1
        assert summary.matched == 1

    def test_multi_disc_at_ingest_root_passes_disc_dirs_not_ingest_root(
        self, tmp_path, monkeypatch
    ):
        self._capture_logs(monkeypatch)
        ingest = tmp_path / "ingest"
        cd1 = ingest / "CD 1"
        cd2 = ingest / "CD 2"
        other = ingest / "Other Album"
        for d in (cd1, cd2, other):
            d.mkdir(parents=True)
            (d / "01.flac").write_bytes(b"x" * 10)
        beets_log = tmp_path / "beets.log"
        beets_log.write_text(f"skip {cd1}; {cd2}\n", encoding="utf-8")
        model = _Model('{"mbid": "%s"}' % _VALID_UUID)
        beet_calls: list[list[str]] = []

        summary = aimusic.resolve_skipped(
            str(ingest),
            "/cfg/beets.yaml",
            str(beets_log),
            0,
            searchers=[model],
            probe=lambda _p: (180.0, {}),
            run_beet=lambda _cwd, argv: (beet_calls.append(argv), 0)[1],
        )
        assert summary.matched == 1
        assert str(ingest) not in beet_calls[0]
        assert str(cd1) in beet_calls[0]
        assert str(cd2) in beet_calls[0]
        assert "Other Album" not in model.asked[0][1]

    def test_music_search_task_traits_in_roster(self):
        task = llmroster.MUSIC_SEARCH
        assert task.name == "music-search"
        assert task.requires == ("web",)
        assert task.prefers == ("reasoning",)
        assert task.lean == "balanced"
        assert llmroster.TASKS["music-search"] is task
