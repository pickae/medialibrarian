"""The white box for medialib/lib/bookpublishing.py.

What is pinned here: the exact argv each tool is handed, and the reasons the two
files exist at all - which are the module's own argument.

Two files come out of a read book. One to LISTEN to, and one to KEEP because the
narration is not reproducible: a re-run of the same book with the same voice is
not the same audio, so what is thrown away here is thrown away for good.
"""

import os
import shutil
from types import SimpleNamespace

import pytest

from medialib.lib import bookpublishing as bp
from tests import blackbox

pytestmark = pytest.mark.stubbed

_TOOLSTUB = blackbox.TOOLSTUB

_PLUMBING = ("bash", "awk", "cat", "base64", "head", "mkdir", "dirname",
             "touch", "cut", "find", "sort")

_BP_ENV = ("audiobookLosslessFilters", "audiobookLosslessCompression",
           "CLI_SCRIPT_DIR", "SAFETY_LOG", "ABORT_FLAG")


@pytest.fixture()
def pub(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    out_dir = tmp_path / "stubout"
    state_dir = tmp_path / "stubstate"
    work = tmp_path / "work"
    for directory in (bin_dir, out_dir, state_dir, work):
        directory.mkdir()
    for tool in _PLUMBING:
        (bin_dir / tool).symlink_to(shutil.which(tool))
    record = tmp_path / "calls"

    def install(name):
        target = bin_dir / name
        shutil.copyfile(_TOOLSTUB, str(target))
        os.chmod(str(target), 0o755)

    def says(name, text, rc="0", writes=None):
        install(name)
        (out_dir / name).write_text(text)
        (out_dir / (name + ".rc")).write_text(rc + "\n")
        if writes is not None:
            (out_dir / (name + ".write")).write_text(" ".join(writes) + "\n")

    def calls():
        if not record.exists():
            return []
        return [line.rstrip("\n").split("\t")[1:]
                for line in record.read_text().splitlines() if line]

    def media(name, size=10):
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"\0" * size)
        return str(path)

    for name in _BP_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("PATH", str(bin_dir))
    monkeypatch.setenv("TOOLSTUB_LOG", str(record))
    monkeypatch.setenv("TOOLSTUB_OUT", str(out_dir))
    monkeypatch.setenv("TOOLSTUB_STATE", str(state_dir))
    monkeypatch.setenv("LC_ALL", "C")
    monkeypatch.setenv("CLI_SCRIPT_DIR", str(tmp_path))
    return SimpleNamespace(says=says, calls=calls, media=media,
                           install=install, tmp_path=tmp_path, work=str(work),
                           out_dir=out_dir, bin_dir=bin_dir)


class TestLossless:
    def _ready(self, pub, rate="48000", channels="2", flac=True, cover=False):
        master = pub.media("master.flac")
        tagged = pub.media("book.m4b")
        pub.install("ffprobe")
        (pub.out_dir / "ffprobe.table").write_text("\n".join([
            blackbox.toolstub_table_line(
                ["-v", "quiet", "-select_streams", "a:0", "-show_entries",
                 "stream=sample_rate", "-of", "default=nk=1:nw=1", tagged],
                0, rate + "\n"),
            blackbox.toolstub_table_line(
                ["-v", "quiet", "-select_streams", "a:0", "-show_entries",
                 "stream=channels", "-of", "default=nk=1:nw=1", tagged],
                0, channels + "\n"),
        ]) + "\n")
        pub.says("ffmpeg", "DATA", "0",
                 ["$LAST" if flac else "-", "$LAST" if cover else "-"])
        return master, tagged

    def test_the_flac_is_named_after_the_master(self, pub):
        master, tagged = self._ready(pub)
        answer = bp.audiobook_lossless(master, tagged, pub.work)
        assert answer == os.path.join(pub.work, "lossless", "master.flac")

    def test_the_rate_and_channels_are_read_off_the_audiobook(self, pub):
        """Read rather than assumed, so the FLAC is that file's audio and not a
        second opinion about it."""
        master, tagged = self._ready(pub, rate="24000", channels="1")
        bp.audiobook_lossless(master, tagged, pub.work)
        finish = [c for c in pub.calls()
                  if c[0] == "ffmpeg" and "-map_metadata" in c][0]
        assert finish[finish.index("-ar") + 1] == "24000"
        assert finish[finish.index("-ac") + 1] == "1"

    def test_a_field_the_audiobook_does_not_state_is_not_asserted(self, pub):
        """ffmpeg then keeps whatever the master has, which is the next best
        thing."""
        master, tagged = self._ready(pub, rate="", channels="abc")
        bp.audiobook_lossless(master, tagged, pub.work)
        finish = [c for c in pub.calls()
                  if c[0] == "ffmpeg" and "-map_metadata" in c][0]
        assert "-ar" not in finish
        assert "-ac" not in finish

    def test_the_filter_pass_is_upstreams_own_chain(self, pub):
        master, tagged = self._ready(pub)
        bp.audiobook_lossless(master, tagged, pub.work)
        finish = [c for c in pub.calls()
                  if c[0] == "ffmpeg" and "-map_metadata" in c][0]
        assert finish[finish.index("-af") + 1] == bp.LOSSLESS_FILTERS

    def test_emptying_the_filters_makes_it_a_plain_re_encode(self, pub,
                                                            monkeypatch):
        monkeypatch.setenv("audiobookLosslessFilters", "")
        master, tagged = self._ready(pub)
        bp.audiobook_lossless(master, tagged, pub.work)
        finish = [c for c in pub.calls()
                  if c[0] == "ffmpeg" and "-map_metadata" in c][0]
        assert "-af" not in finish

    def test_the_source_metadata_is_dropped_and_the_chapters_written_back(
            self, pub):
        """-map_metadata -1: the m4b's tags are not the FLAC's, and the chapters
        go in as Vorbis comments instead."""
        master, tagged = self._ready(pub)
        bp.audiobook_lossless(master, tagged, pub.work)
        finish = [c for c in pub.calls()
                  if c[0] == "ffmpeg" and "-map_metadata" in c][0]
        assert finish[finish.index("-map_metadata") + 1] == "-1"
        assert ["ffprobe", "-v", "quiet", "-show_chapters", "-of", "flat",
                tagged] in pub.calls()

    def test_the_compression_level_is_flacs_default_effort(self, pub):
        """Level 8 buys about 1% at several times the CPU, which on a whole
        library is hours spent on nothing."""
        master, tagged = self._ready(pub)
        bp.audiobook_lossless(master, tagged, pub.work)
        finish = [c for c in pub.calls()
                  if c[0] == "ffmpeg" and "-map_metadata" in c][0]
        assert finish[finish.index("-compression_level") + 1] == "5"
        assert bp.LOSSLESS_COMPRESSION == "5"

    def test_a_master_that_is_missing_or_empty_yields_nothing(self, pub):
        tagged = pub.media("book.m4b")
        assert bp.audiobook_lossless(str(pub.tmp_path / "absent.flac"), tagged,
                                     pub.work) is None
        empty = pub.media("empty.flac", 0)
        assert bp.audiobook_lossless(empty, tagged, pub.work) is None

    def test_a_finishing_pass_that_produced_nothing_yields_nothing(self, pub):
        master, tagged = self._ready(pub, flac=False)
        assert bp.audiobook_lossless(master, tagged, pub.work) is None

    def test_the_cover_is_extracted_and_then_removed(self, pub):
        """It is an intermediate, and the workspace is the RAM one the caller
        publishes out of."""
        master, tagged = self._ready(pub, cover=True)
        answer = bp.audiobook_lossless(master, tagged, pub.work)
        assert answer is not None
        assert not os.path.exists(os.path.join(pub.work, "lossless",
                                               "bookCover.jpg"))
        extract = [c for c in pub.calls()
                   if c[0] == "ffmpeg" and "-an" in c][0]
        assert extract[extract.index("-c:v") + 1] == "copy"

    def test_a_book_with_no_cover_is_still_published(self, pub):
        """Neither a missing cover nor an unwritable tag is worth failing a book
        that has been read for the last four hours."""
        master, tagged = self._ready(pub, cover=False)
        assert bp.audiobook_lossless(master, tagged, pub.work) is not None

    def test_the_workspace_is_cleared_before_it_is_used(self, pub):
        master, tagged = self._ready(pub)
        stale = os.path.join(pub.work, "lossless", "stale.flac")
        os.makedirs(os.path.dirname(stale), exist_ok=True)
        open(stale, "wb").close()
        bp.audiobook_lossless(master, tagged, pub.work)
        assert not os.path.exists(stale)


# A stand-in convert-audio, as a module: a command starts another one as a child
# process, so what these tests watch is a real process with the real argv and the
# real environment the caller decided to hand over.
FAKE_ENCODER = """
import os
import sys

if os.environ.get("FAKE_ARGV"):
    with open(os.environ["FAKE_ARGV"], "w") as handle:
        handle.write(" ".join(sys.argv[1:]) + "\\n")
if os.environ.get("FAKE_SEEN"):
    with open(os.environ["FAKE_SEEN"], "w") as handle:
        handle.write("%s|%s\\n" % (os.environ.get("SAFETY_LOG", "unset"),
                                  os.environ.get("ABORT_FLAG", "unset")))
for entry in [e for e in os.environ.get("FAKE_TREE", "").split(",") if e]:
    path, size, age = entry.rsplit(":", 2)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as handle:
        handle.write(b"\\0" * int(size))
    os.utime(path, (1600000000 - int(age), 1600000000 - int(age)))
sys.exit(int(os.environ.get("FAKE_RC", "0")))
"""

class TestToOpus:
    def _encoder(self, pub, tree, rc="0", *, fake_command, monkeypatch):
        """A stand-in convert-audio that lays out the tree it is given."""
        fake_command("convert-audio", FAKE_ENCODER)
        monkeypatch.setenv("FAKE_RC", str(rc))
        monkeypatch.setenv("FAKE_TREE", ",".join(
            "%s:%s:%s" % (os.path.join(pub.work, rel), size, age)
            for rel, size, age in tree))

    def test_the_encoder_is_pointed_at_a_directory_of_its_own(self, pub, fake_command, monkeypatch):
        book = pub.media("book.m4b")
        self._encoder(pub, [("opus/book.opus", 50, 1)], fake_command=fake_command,
                      monkeypatch=monkeypatch)
        answer = bp.audiobook_to_opus(book, pub.work, "36", "4",
                                      os.path.join(pub.work, "log"))
        assert answer == os.path.join(pub.work, "opus", "book.opus")

    def test_the_input_is_linked_rather_than_copied(self, pub, fake_command, monkeypatch):
        """The source is already in the RAM workspace, so a copy would spend it
        twice."""
        book = pub.media("book.m4b")
        self._encoder(pub, [("opus/book.opus", 50, 1)], fake_command=fake_command,
                      monkeypatch=monkeypatch)
        seen = {}
        real_link = os.link

        def watched(src, dst):
            seen["linked"] = True
            return real_link(src, dst)

        os.link = watched
        try:
            bp.audiobook_to_opus(book, pub.work, "36", "4",
                                 os.path.join(pub.work, "log"))
        finally:
            os.link = real_link
        assert seen.get("linked")

    def test_the_input_directory_is_taken_away_again(self, pub, fake_command, monkeypatch):
        """It holds a hard link to the book, and the workspace is the RAM one."""
        book = pub.media("book.m4b")
        self._encoder(pub, [("opus/book.opus", 50, 1)], fake_command=fake_command,
                      monkeypatch=monkeypatch)
        bp.audiobook_to_opus(book, pub.work, "36", "4",
                             os.path.join(pub.work, "log"))
        assert not os.path.exists(os.path.join(pub.work, "toOpus"))

    def test_the_answer_is_found_and_not_named(self, pub, fake_command, monkeypatch):
        """convert-audio names its own output, and may nest it."""
        book = pub.media("book.m4b")
        self._encoder(pub, [("opus/deep/other.opus", 50, 1)], fake_command=fake_command,
                      monkeypatch=monkeypatch)
        answer = bp.audiobook_to_opus(book, pub.work, "36", "4",
                                      os.path.join(pub.work, "log"))
        assert answer == os.path.join(pub.work, "opus", "deep", "other.opus")

    def test_the_newest_wins_so_an_earlier_attempt_cannot(self, pub, fake_command, monkeypatch):
        book = pub.media("book.m4b")
        self._encoder(pub, [("opus/old.opus", 50, 100),
                            ("opus/new.opus", 50, 1)], fake_command=fake_command,
                      monkeypatch=monkeypatch)
        answer = bp.audiobook_to_opus(book, pub.work, "36", "4",
                                      os.path.join(pub.work, "log"))
        assert answer == os.path.join(pub.work, "opus", "new.opus")

    def test_an_encode_that_produced_nothing_fails_the_book(self, pub, fake_command, monkeypatch):
        """Rather than publishing half of it."""
        book = pub.media("book.m4b")
        self._encoder(pub, [], fake_command=fake_command,
                      monkeypatch=monkeypatch)
        assert bp.audiobook_to_opus(book, pub.work, "36", "4",
                                    os.path.join(pub.work, "log")) is None

    def test_an_empty_opus_is_not_a_book(self, pub, fake_command, monkeypatch):
        book = pub.media("book.m4b")
        self._encoder(pub, [("opus/book.opus", 0, 1)], fake_command=fake_command,
                      monkeypatch=monkeypatch)
        assert bp.audiobook_to_opus(book, pub.work, "36", "4",
                                    os.path.join(pub.work, "log")) is None

    def test_an_encoder_that_failed_but_left_a_file_still_counts(
            self, pub, fake_command, monkeypatch):
        """`|| true`: whatever the encoder said, what decides is the file it
        left."""
        book = pub.media("book.m4b")
        self._encoder(pub, [("opus/book.opus", 50, 1)], rc="7", fake_command=fake_command,
                      monkeypatch=monkeypatch)
        assert bp.audiobook_to_opus(book, pub.work, "36", "4",
                                    os.path.join(pub.work, "log")) is not None

    def test_a_book_that_is_missing_or_empty_yields_nothing(self, pub, fake_command, monkeypatch):
        self._encoder(pub, [("opus/book.opus", 50, 1)], fake_command=fake_command,
                      monkeypatch=monkeypatch)
        assert bp.audiobook_to_opus(str(pub.tmp_path / "absent.m4b"), pub.work,
                                    "36", "4",
                                    os.path.join(pub.work, "log")) is None
        empty = pub.media("empty.m4b", 0)
        assert bp.audiobook_to_opus(empty, pub.work, "36", "4",
                                    os.path.join(pub.work, "log")) is None

    def test_the_childs_environment_loses_the_runs_own_log_and_flag(
            self, pub, fake_command, monkeypatch):
        """Both are inherited by design when one of these commands wraps another
        in one run - but this one is a separate process inside a parallel worker,
        and the tail of convert-audio removes the two files it was given, which
        would take the whole run's skip log and interrupt flag with it."""
        monkeypatch.setenv("SAFETY_LOG", "/run/skips")
        monkeypatch.setenv("ABORT_FLAG", "/run/abort")
        book = pub.media("book.m4b")
        seen = os.path.join(str(pub.tmp_path), "seen")
        self._encoder(pub, [("opus/b.opus", 50, 1)],
                      fake_command=fake_command, monkeypatch=monkeypatch)
        monkeypatch.setenv("FAKE_SEEN", seen)
        bp.audiobook_to_opus(book, pub.work, "36", "4",
                             os.path.join(pub.work, "log"))
        with open(seen, encoding="ascii") as handle:
            assert handle.read().strip() == "unset|unset"

    def test_the_encoder_is_asked_for_mono_at_the_given_bitrate(
            self, pub, fake_command, monkeypatch):
        book = pub.media("book.m4b")
        argv = os.path.join(str(pub.tmp_path), "argv")
        self._encoder(pub, [("opus/b.opus", 50, 1)],
                      fake_command=fake_command, monkeypatch=monkeypatch)
        monkeypatch.setenv("FAKE_ARGV", argv)
        bp.audiobook_to_opus(book, pub.work, "48", "8",
                             os.path.join(pub.work, "log"))
        with open(argv, encoding="ascii") as handle:
            words = handle.read().split()
        assert words[0] == "-m"
        assert words[words.index("-b") + 1] == "48"
        assert words[words.index("-j") + 1] == "8"
