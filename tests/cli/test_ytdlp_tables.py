"""Which profile a table's feeds are fetched with when they run in workers.

A forkserver worker keeps the environment the fork server started with, so a
profile handed over through ``os.environ`` reaches the second table's workers
as the first table's. The stand-in for ``_spawn`` below runs each worker under
the environment the first one saw, which is what the fork server does.
"""

import os

import pytest

from medialib.cli import ytdlp

pytestmark = pytest.mark.pure


class _Done:
    exitcode = 0

    def join(self, timeout=None):
        return None

    def is_alive(self):
        return False


@pytest.fixture
def fetched(monkeypatch, tmp_path):
    profiles = []
    server_environment = {}

    def call(*_args, profile="", **_kwargs):
        profiles.append(profile)
        return None

    def spawn(target, args):
        if not server_environment:
            server_environment.update(os.environ)
        current = dict(os.environ)
        os.environ.clear()
        os.environ.update(server_environment)
        try:
            target(*args)
        finally:
            os.environ.clear()
            os.environ.update(current)
        return _Done()

    monkeypatch.setattr(ytdlp.podcastfeeds, "podcast_call", call)
    monkeypatch.setattr(ytdlp, "_spawn", spawn)
    monkeypatch.setattr(ytdlp.workerpool, "reap_one", lambda running: [])
    monkeypatch.setattr(ytdlp.workerpool, "join_all", lambda running: None)
    monkeypatch.setattr(ytdlp.safety, "trap_worker_abort", lambda: None)
    monkeypatch.setattr(ytdlp.safety, "abort_requested", lambda: False)

    run = ytdlp.Run(options={"match": "", "includeInactive": False,
                             "dryRun": False},
                    ytdlp_command=["yt-dlp"], platform="linux",
                    native_output=str(tmp_path), native_archive="",
                    manifest_dir=str(tmp_path), native_manifest_dir="",
                    status_dir="", counter_file="",
                    counters=ytdlp.Counters(""), staging_for_profile={},
                    script_dir="", ffmpeg_location="")
    rows = ["1\tA\t\t\t\thttps://example.invalid/a",
            "1\tB\t\t\t\thttps://example.invalid/b"]
    run.run_table("youtubeAudio", 2, "first", rows)
    run.run_table("youtubeVideo", 2, "second", rows)
    return profiles


def test_each_table_s_workers_use_that_table_s_profile(fetched):
    assert fetched == ["youtubeAudio"] * 2 + ["youtubeVideo"] * 2
