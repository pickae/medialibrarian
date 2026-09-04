"""The library `ytdlp -i` builds out of a run.

`-i` is the only thing in this repo that spans three commands - `ytdlp` downloads,
`clean-folder-structure` renames what came down, `convert-audio` converts it into
the library - so what it promises can only be checked by running all three. The
dry run's routing is `test_ytdlp_cli.py`'s; what is asserted here is the tree that
exists afterwards.

`yt-dlp` is stubbed and so are the heavy media tools, so no codec is involved.
`rsync` and `jq` are not: the copy of an already-Opus file is a real rsync and the
stream probing is real jq over the stub ffprobe's output.
"""

from __future__ import annotations

import os
import shutil

import pytest

from tests import blackbox

pytestmark = pytest.mark.stubbed

# One episode per feed in the folder the output template names, with the jpg
# thumbnail a real run leaves beside it.
_STUB = r"""
marker=""; outTemplate=""; prev=""
for a in "$@"; do
    [[ "$prev" == "--print" ]] && marker="${a#after_move:}"
    [[ "$prev" == "-o" ]] && outTemplate="$a"
    prev="$a"
done
marker="${marker%\%(filepath)s}"
episode="${outTemplate%/*}/20260101 An episode.opus"
mkdir -p "${episode%/*}"
printf x > "$episode"
printf x > "${episode%.opus}.jpg"
printf '%s%s\n' "$marker" "$episode"
exit 0
"""

_EPISODE = "20260101 An episode.opus"


def _table(path, row, profile=""):
    head = ("#!profile %s\n" % profile) if profile else ""
    path.write_text(head + row)
    return path


def _feed(folder, url):
    return "1\t%s\t\t\t\t%s\n" % (folder, url)


@pytest.fixture
def ingest(sandbox, tmp_path):
    """Three tables: one per conversion, so both staging folders are exercised,
    plus a second table of the same conversion as one of them - which must not
    become a third folder."""
    for tool in ("rsync", "jq"):
        if not shutil.which(tool):
            pytest.fail("%s is missing: the copy and the stream probe are real "
                        "here, not stubbed" % tool)
    sandbox.with_media_stubs()
    sandbox.with_tool("yt-dlp", _STUB)

    tables = {
        "music": _table(tmp_path / "music.tsv",
                        _feed("Podcasts/AI/anthropic",
                              "https://example.test/music")),
        "speech": _table(tmp_path / "speech.tsv",
                         _feed("Podcasts/talking",
                               "https://example.test/speech"),
                         profile="rssAudio"),
        "rss": _table(tmp_path / "rss.tsv",
                      _feed("Podcasts/history", "https://example.test/rss"),
                      profile="rssAudio"),
    }
    phone = tmp_path / "phone"
    environment = dict(os.environ, YTDLP="yt-dlp")

    def run(*table_names, expect=0):
        arguments = []
        for name in table_names or tables:
            arguments += ["-t", tables[name]]
        done = sandbox.run("ytdlp", "-s", "linux", "-i", "-c", *arguments,
                           phone, phone / "archive.log", env=environment,
                           timeout=600)
        assert done.returncode == expect, done.stdout + done.stderr
        return done.stdout + done.stderr

    sandbox.phone = phone
    sandbox.ingest = run
    return sandbox


class TestTheTreeARunLeaves:
    @pytest.fixture
    def run(self, ingest):
        return ingest, ingest.ingest()

    def test_the_raw_download_stays_in_its_staging_folder(self, run):
        """It is the only copy of what was published, and the archive says it
        will not be fetched again."""
        ingest, _ = run
        staging = ingest.phone / "Staging"
        assert (staging / "Music/Podcasts/AI/anthropic" / _EPISODE).is_file()
        assert (staging / "Speech/Podcasts/history" / _EPISODE).is_file()

    @pytest.mark.parametrize("episode", [
        "Podcasts/AI/anthropic", "Podcasts/talking", "Podcasts/history"])
    def test_every_conversion_arrives_in_the_one_library(self, run, episode):
        """At the path the feed's own sub-folder names, whichever staging folder
        it came through - which is what makes the split invisible here."""
        ingest, _ = run
        assert (ingest.phone / "Ingested" / episode / _EPISODE).is_file()

    def test_one_staging_folder_per_conversion_and_not_per_table(self, run):
        """Two tables wanting the same conversion share one, and there is no
        third: the folders are keyed on the conversion."""
        ingest, _ = run
        staging = ingest.phone / "Staging"
        assert len(list(staging.iterdir())) == 2
        assert (staging / "Speech/Podcasts/talking" / _EPISODE).is_file()
        assert (staging / "Speech/Podcasts/history" / _EPISODE).is_file()

    def test_the_embedded_thumbnail_is_cleaned_up_before_the_conversion(
            self, run):
        """The ordering is the assertion: `-c` removes it before the conversion
        reads the folder, so it is never copied into the library beside the
        episode it is already inside."""
        ingest, _ = run
        assert list((ingest.phone / "Staging").rglob("*.jpg")) == []
        assert list((ingest.phone / "Ingested").rglob("*.jpg")) == []

    def test_a_re_run_leaves_the_tree_exactly_as_it_was(self, run):
        """A nightly run has nothing new to fetch and walks the same staging tree
        again by design."""
        ingest, _ = run
        before = blackbox.tree_of(ingest.phone)
        ingest.ingest()
        assert blackbox.tree_of(ingest.phone) == before


class TestARunOfOneTableOverAFullStagingTree:
    """What `-i` ingests is the FOLDER and not the run, so a conversion no table
    on this command line downloads into still reaches the library."""

    @pytest.fixture
    def run(self, ingest):
        ingest.ingest()
        # Emptying the library is what makes the assertion about THIS run rather
        # than about the one that filled the staging tree.
        shutil.rmtree(ingest.phone / "Ingested")
        return ingest, ingest.ingest("speech")

    @pytest.mark.parametrize("episode", [
        "Podcasts/talking", "Podcasts/AI/anthropic"])
    def test_it_is_ingested_from_the_folder_it_is_in(self, run, episode):
        ingest, _ = run
        assert (ingest.phone / "Ingested" / episode / _EPISODE).is_file()

    def test_the_carried_over_folders_are_named_rather_than_silently_swept(
            self, run):
        _, log = run
        assert "Also converting Music" in log, log
