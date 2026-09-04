"""Whether this ffsubsync can REJECT a bad alignment, and who is told.

The flag that asks for the check is a recent ffsubsync addition, so the run
probes for it once and both sync call sites - the downloaded subtitle and the
whisper transcript - are handed the answer. An older ffsubsync given a flag it
does not know fails argparse and exits non-zero, which both call sites read as a
failed sync and answer by discarding a subtitle that was perfectly good.
"""

import pytest

from medialib.cli import ingest_movies_run as run_module

pytestmark = pytest.mark.pure


@pytest.fixture
def probe(monkeypatch):
    """The probe, against an ffsubsync whose help page the case writes."""
    def run(help_text=None):
        logs: list[str] = []
        monkeypatch.setattr(run_module, "log", logs.append)
        monkeypatch.setattr(run_module, "_has_tool",
                            lambda name: help_text is not None)
        monkeypatch.setattr(run_module, "_tool_help", lambda name: help_text)
        return run_module._settle_ffsubsync_quality(), logs
    return run


def test_an_ffsubsync_that_knows_the_flag_is_asked_for_the_check(probe):
    answer, logs = probe("  --skip-sync-on-low-quality  reject bad alignments")
    assert answer == "yes"
    assert logs == []


def test_an_older_ffsubsync_is_not_handed_a_flag_it_would_refuse(probe):
    answer, logs = probe("  --max-offset-seconds SECONDS")
    assert answer == "no"
    assert any("upgrade ffsubsync" in line for line in logs)


def test_no_ffsubsync_at_all_settles_the_same_way_and_says_nothing(probe):
    # The run has already said what a missing ffsubsync costs, once, and the
    # subtitle phases are off anyway.
    answer, logs = probe(None)
    assert answer == "no"
    assert logs == []


@pytest.fixture
def ingested(monkeypatch):
    """One ingest with every phase stubbed out, reporting what the two sync
    call sites were handed."""
    def run(quality="yes", subtitle_work=True):
        seen = {}
        for name in ("cleanup", "mkv_mux", "movies_into_subfolders",
                     "extras_into_subfolders", "update_tags"):
            monkeypatch.setattr(run_module.rules, name, lambda *a, **k: None)
        for name in ("rename_folders", "rename_movies"):
            monkeypatch.setattr(run_module.rules, name, lambda *a, **k: 0)
        monkeypatch.setattr(run_module.safety, "lower_case_extensions",
                            lambda *a, **k: None)
        for name in ("move_subs", "rename_subs"):
            monkeypatch.setattr(run_module.subtitlefiles, name,
                                lambda *a, **k: None)
        monkeypatch.setattr(run_module.tmdblookup, "tag_plex_ids",
                            lambda *a, **k: None)
        for name in ("_transcode_opus", "improve_main_movies",
                     "check_folders"):
            monkeypatch.setattr(run_module, name, lambda *a, **k: None)
        monkeypatch.setattr(run_module, "log", lambda *a, **k: None)

        def download_subs(directory, user, password, max_offset,
                          max_quality_offset, ffsubsync_quality, log):
            seen["download"] = ffsubsync_quality

        def export_commentary(directory, *args):
            seen["commentary"] = args[-1]

        monkeypatch.setattr(run_module.subtitlefiles, "download_subs",
                            download_subs)
        monkeypatch.setattr(run_module.commentarytranscription,
                            "export_commentary", export_commentary)

        state = run_module.Run(script_dir="", ram_root="", skips=None,
                               fragments_file="", whisper={},
                               ffsubsync_quality=quality)
        run_module._ingest(state, "/x", subtitle_work)
        return seen
    return run


def test_both_sync_call_sites_are_handed_the_one_probed_answer(ingested):
    assert ingested(quality="no") == {"download": "no", "commentary": "no"}


def test_the_answer_is_the_probe_s_and_not_a_constant(ingested):
    assert ingested(quality="yes") == {"download": "yes", "commentary": "yes"}
