"""The white box for the shared tool stub - the fake tool every stubbed test
installs under a real tool's name.

A tool-coupled module is verified through a stub of its tools, and what is
compared is the dispatch: which tool, with which arguments, and what the module
made of the answer. The stubbed tests assert on what this stub recorded, so
a knob that quietly stopped working would not fail there - it would make them agree
with a module that never called anything.

Pinned here is the stub's whole surface, one group per knob, plus the precedence
between them: a table line, then ``TOOLSTUB_RC``, then the lists, then the
last-argument rule.
"""

import os
import shutil
import subprocess

import pytest

from tests import blackbox

pytestmark = pytest.mark.stubbed

_TOOLSTUB = blackbox.TOOLSTUB

_US = blackbox.TOOLSTUB_US

# The suite's own writer, not a second reading of the format: what the cases
# below pin is that the stub answers the lines the suite really writes.
_table_line = blackbox.toolstub_table_line


class _Stub:
    def __init__(self, tmp_path):
        self.root = tmp_path
        self.bin = tmp_path / "bin"
        self.out = tmp_path / "out"
        self.state = tmp_path / "state"
        for d in (self.bin, self.out, self.state):
            d.mkdir()
        self.log = tmp_path / "calls"

    def install(self, *names):
        for name in names:
            shutil.copyfile(_TOOLSTUB, str(self.bin / name))
            os.chmod(str(self.bin / name), 0o755)

    def say(self, name, text):
        (self.out / name).write_text(text)

    def rc_list(self, name, codes):
        (self.out / (name + ".rc")).write_text(" ".join(str(c) for c in codes) + "\n")

    def write_list(self, name, paths):
        (self.out / (name + ".write")).write_text(" ".join(paths) + "\n")

    def table(self, name, lines):
        (self.out / (name + ".table")).write_text("\n".join(lines) + "\n")

    def calls(self):
        if not self.log.exists():
            return []
        return [line.split("\t")[1:]
                for line in self.log.read_text().splitlines() if line]

    def run(self, name, *args, out=True, state=True, stdin=None, **env):
        """One call of the stub under `name`, with the knobs the case names."""
        environ = dict(os.environ)
        environ["TOOLSTUB_LOG"] = str(self.log)
        if out:
            environ["TOOLSTUB_OUT"] = str(self.out)
        if state:
            environ["TOOLSTUB_STATE"] = str(self.state)
        for key, value in env.items():
            if value is None:
                environ.pop("TOOLSTUB_" + key.upper(), None)
            else:
                environ["TOOLSTUB_" + key.upper()] = str(value)
        return subprocess.run([str(self.bin / name), *args], env=environ,
                              input=stdin, capture_output=True, text=True)


@pytest.fixture()
def stub(tmp_path):
    s = _Stub(tmp_path)
    s.install("pdftotext", "ebook-convert")
    return s


# --- what it records ---------------------------------------------------------
# The recorded call is what a stubbed test asserts on: one line per call, the
# tool's own name first, then each argument, tab-separated.

class TestRecording:
    def test_the_call_is_recorded_name_first(self, stub):
        stub.run("pdftotext", "-q", "-enc", "UTF-8", "--", "in.PDF", "out-ok.txt")
        assert stub.calls() == [
            ["pdftotext", "-q", "-enc", "UTF-8", "--", "in.PDF", "out-ok.txt"]]

    def test_the_name_is_the_one_it_was_called_by(self, stub):
        stub.run("ebook-convert", "in.epub", "out-ok.txt")
        assert stub.calls() == [["ebook-convert", "in.epub", "out-ok.txt"]]

    def test_a_bare_call_still_records(self, stub):
        stub.run("pdftotext")
        assert stub.calls() == [["pdftotext"]]

    def test_calls_append_rather_than_replace(self, stub):
        stub.run("pdftotext", "first-ok")
        stub.run("ebook-convert", "second-ok")
        assert stub.calls() == [["pdftotext", "first-ok"],
                                ["ebook-convert", "second-ok"]]

    def test_an_argument_with_a_space_stays_one_field(self, stub):
        stub.run("pdftotext", "a b", "out-ok.txt")
        assert stub.calls() == [["pdftotext", "a b", "out-ok.txt"]]

    def test_a_log_that_is_not_named_is_an_error(self, stub):
        env = dict(os.environ)
        env.pop("TOOLSTUB_LOG", None)
        done = subprocess.run([str(stub.bin / "pdftotext"), "a"], env=env,
                              capture_output=True, text=True)
        assert done.returncode == 1

    def test_tail_drains_the_pipeline_feeding_it(self, stub):
        # `df | tail` in ramScratch: a stub that answered while the left side
        # was still running would let that side's log line land after the
        # report had already read the file.
        stub.install("tail")
        done = stub.run("tail", "-1", stdin="one\ntwo\n")
        assert done.returncode == 7
        assert stub.calls() == [["tail", "-1"]]


# --- the rule it falls back on -----------------------------------------------
# With no knob set, a case chooses success or failure through the destination it
# hands over last, so it need not know which tool the module will reach for.

class TestTheOkRule:
    def test_the_last_argument_containing_ok_is_success(self, stub):
        assert stub.run("pdftotext", "-q", "in.PDF", "out-ok.txt").returncode == 0

    def test_and_anything_else_is_seven(self, stub):
        assert stub.run("ebook-convert", "in.epub", "out.txt").returncode == 7

    def test_only_the_last_argument_decides(self, stub):
        assert stub.run("pdftotext", "ok-dest.txt", "nope-out.txt").returncode == 7

    def test_with_no_arguments_it_refuses(self, stub):
        assert stub.run("pdftotext").returncode == 7

    def test_the_scratch_root_is_not_part_of_the_decision(self, tmp_path):
        # The root is a random name, so a draw containing "ok" would decide the
        # exit on its own. The rule sees the argument with the root removed.
        okroot = tmp_path / "drawn-ok-root"
        okroot.mkdir()
        s = _Stub(okroot)
        s.install("pdftotext")
        assert s.run("pdftotext", str(okroot / "out" / "page.txt")).returncode == 7


# --- what it says ------------------------------------------------------------

class TestPrinting:
    def test_it_prints_the_file_named_for_it(self, stub):
        stub.install("pdfinfo")
        stub.say("pdfinfo", "Pages:    2\n")
        assert stub.run("pdfinfo", "/book.pdf").stdout == "Pages:    2\n"

    def test_a_tool_with_no_file_of_its_own_prints_nothing(self, stub):
        stub.say("pdfinfo", "Pages:    2\n")
        assert stub.run("ebook-convert", "in.epub", "out-ok.txt").stdout == ""

    def test_printing_does_not_stop_it_recording(self, stub):
        stub.install("pdfinfo")
        stub.say("pdfinfo", "Pages:    2\n")
        stub.run("pdfinfo", "/book.pdf")
        assert stub.calls() == [["pdfinfo", "/book.pdf"]]


# --- when it writes its page -------------------------------------------------
# A case's choice is WHICH call finally yields the file, so the counter is per
# tool: one tool's cascade does not start where another's left off.

class TestTouch:
    def test_the_first_skip_calls_write_nothing(self, stub):
        stub.install("pdftoppm")
        page = stub.root / "page-01.jpg"
        stub.run("pdftoppm", "-r", "300", "/book.pdf", touch=page, skip=1)
        assert not page.exists()

    def test_and_the_next_one_writes(self, stub):
        stub.install("pdftoppm")
        page = stub.root / "page-01.jpg"
        stub.run("pdftoppm", "-r", "300", "/book.pdf", touch=page, skip=1)
        stub.run("pdftoppm", "-r", "300", "/book.pdf", touch=page, skip=1)
        assert page.exists()

    def test_the_count_is_per_tool(self, stub):
        stub.install("pdftoppm", "pdfimages")
        page = stub.root / "page-01.jpg"
        for _ in range(3):
            stub.run("pdftoppm", "-r", "300", "/book.pdf", touch=page, skip=1)
        fresh = stub.root / "page-02.jpg"
        stub.run("pdfimages", "-list", "/book.pdf", touch=fresh, skip=2)
        assert not fresh.exists()


# --- the status it exits with ------------------------------------------------

class TestExitCodes:
    def test_a_named_status_outranks_the_ok_rule(self, stub):
        assert stub.run("pdftotext", "in.pdf", "out-ok.txt", rc=3).returncode == 3

    def test_the_rc_list_is_consumed_in_order(self, stub):
        stub.install("ffmpeg")
        stub.rc_list("ffmpeg", [1, 0, 7])
        got = [stub.run("ffmpeg", "-i", "in.wav", "out.wav").returncode
               for _ in range(3)]
        assert got == [1, 0, 7]

    def test_a_list_that_runs_out_falls_through_to_the_rule(self, stub):
        stub.install("ffmpeg")
        stub.rc_list("ffmpeg", [1])
        stub.run("ffmpeg", "-i", "in.wav", "out.wav")
        assert stub.run("ffmpeg", "-i", "in.wav", "out-ok.wav").returncode == 0
        assert stub.run("ffmpeg", "-i", "in.wav", "out-nope.wav").returncode == 7

    def test_the_rc_list_needs_a_state_directory(self, stub):
        # The order lives in the state, not in the call: without one there is no
        # Nth call to speak of, so the rule below answers instead.
        stub.install("ffmpeg")
        stub.rc_list("ffmpeg", [1, 1, 1])
        assert stub.run("ffmpeg", "-i", "in.wav", "out-ok.wav",
                        state=False).returncode == 0

    def test_the_list_and_the_touch_share_one_counter(self, stub):
        stub.install("ffmpeg")
        stub.rc_list("ffmpeg", [1, 0, 7])
        probe = stub.root / "probe.wav"
        for _ in range(6):
            stub.run("ffmpeg", "-i", "in.wav", "out-nope.wav",
                     touch=probe, skip=6)
        assert not probe.exists()
        stub.run("ffmpeg", "-i", "in.wav", "out-nope.wav", touch=probe, skip=6)
        assert probe.exists()

    def test_a_named_status_outranks_the_list(self, stub):
        stub.install("pipx")
        stub.rc_list("pipx", [3])
        assert stub.run("pipx", "run", "whisper", "in.wav", rc=5).returncode == 5


# --- the files it writes -----------------------------------------------------
# The directory a written file lands in is made at runtime by the module under
# test, so an entry names a flag from the call's own argv rather than a path.

class TestWriteList:
    def test_the_nth_call_creates_the_nth_path(self, stub):
        stub.install("ffsubsync")
        logdir = stub.root / "logdir"
        logdir.mkdir()
        stub.say("ffsubsync", "low-quality alignment (score -1.0 < 0.0)\n")
        stub.write_list("ffsubsync",
                        ["${--log-dir-path}/ffsubsync.log", "-",
                         str(stub.root / "plain.srt")])
        stub.run("ffsubsync", "ref.mkv", "-i", "sub.srt",
                 "--log-dir-path", str(logdir))
        assert (logdir / "ffsubsync.log").read_text() == \
            "low-quality alignment (score -1.0 < 0.0)\n"

    def test_a_dash_writes_nothing(self, stub):
        stub.install("ffsubsync")
        logdir = stub.root / "logdir"
        logdir.mkdir()
        plain = stub.root / "plain.srt"
        stub.write_list("ffsubsync", ["${--log-dir-path}/ffsubsync.log", "-",
                                      str(plain)])
        stub.run("ffsubsync", "-i", "sub.srt", "--log-dir-path", str(logdir))
        stub.run("ffsubsync", "-i", "sub.srt", "--log-dir-path", str(logdir))
        assert not plain.exists()
        assert (logdir / "ffsubsync.log").exists()

    def test_a_list_that_runs_out_creates_nothing(self, stub):
        stub.install("ffsubsync")
        stub.write_list("ffsubsync", [str(stub.root / "one.srt")])
        stub.run("ffsubsync", "-i", "sub.srt")
        stub.run("ffsubsync", "-i", "sub.srt")
        assert sorted(p.name for p in stub.root.glob("*.srt")) == ["one.srt"]

    def test_a_printless_tool_writes_the_empty_file(self, stub):
        stub.install("subliminal")
        outdir = stub.root / "subs"
        outdir.mkdir()
        stub.write_list("subliminal", ["${--out-dir}/sub.srt"])
        stub.run("subliminal", "download", "--out-dir", str(outdir))
        assert (outdir / "sub.srt").exists()
        assert (outdir / "sub.srt").read_text() == ""

    def test_a_write_into_a_directory_that_does_not_exist_lands_nowhere(self, stub):
        # The stub, like the real tool, does not create the directory it is told
        # to write into: a missing parent is a missing file, not a made-up folder.
        stub.install("subliminal")
        stub.write_list("subliminal", ["${--out-dir}/sub.srt"])
        stub.run("subliminal", "download", "--out-dir", str(stub.root / "nope"))
        assert not (stub.root / "nope").exists()

    def test_an_entry_naming_a_flag_the_call_never_got_writes_nothing(self, stub):
        stub.install("subliminal")
        stub.write_list("subliminal", ["${--log-dir-path}/never.srt"])
        stub.run("subliminal", "download", "--out-dir", str(stub.root))
        assert not (stub.root / "never.srt").exists()

    def test_last_names_the_call_s_own_final_argument(self, stub):
        stub.install("ebook-convert")
        stub.write_list("ebook-convert", ["$LAST"])
        made = stub.root / "made-at-runtime.txt"
        stub.run("ebook-convert", "in.epub", str(made))
        assert made.exists()

    def test_last_drops_an_attachment_id(self, stub):
        # mkvextract's attachment form N:file writes to file, the number being
        # the attachment's id and not part of the name.
        stub.install("mkvextract")
        stub.write_list("mkvextract", ["$LAST"])
        cover = stub.root / "cover.jpg"
        stub.run("mkvextract", "attachments", "in.mkv", "3:" + str(cover))
        assert cover.exists()

    def test_last_on_a_bare_call_writes_nothing(self, stub):
        stub.install("ebook-convert")
        stub.write_list("ebook-convert", ["$LAST"])
        stub.run("ebook-convert")
        assert list(stub.root.glob("*.txt")) == []


# --- the table ---------------------------------------------------------------
# One line per call the case expects, keyed by that call's own argv: the case
# answering THAT call rather than every call, so it decides before anything else.

class TestTable:
    def test_a_matching_line_answers_with_its_own_response(self, stub):
        stub.install("ffprobe")
        stub.table("ffprobe", [
            _table_line(["-i", "a.mkv"], 0, "eac3\n"),
            _table_line(["-i", "b.mkv"], 0, "truehd\n"),
        ])
        assert stub.run("ffprobe", "-i", "b.mkv").stdout == "truehd\n"

    def test_and_with_its_own_exit_code(self, stub):
        stub.install("ffprobe")
        stub.table("ffprobe", [_table_line(["-i", "a-ok.mkv"], 4, "x\n")])
        assert stub.run("ffprobe", "-i", "a-ok.mkv").returncode == 4

    def test_a_line_matches_only_a_call_of_its_own_length(self, stub):
        # The separator ending the join has to be MATCHED, not just written:
        # a prefix match alone reads the line's next argument as an exit code.
        stub.install("ffprobe")
        stub.table("ffprobe", [_table_line(["-i", "a.mkv", "-show"], 0, "long\n")])
        done = stub.run("ffprobe", "-i", "a.mkv")
        assert done.stdout == ""
        assert done.stderr == ""
        assert done.returncode == 7

    def test_nor_does_a_longer_call_match_a_shorter_line(self, stub):
        stub.install("ffprobe")
        stub.table("ffprobe", [_table_line(["-i", "a.mkv"], 0, "short\n")])
        done = stub.run("ffprobe", "-i", "a.mkv", "-show")
        assert done.stdout == ""
        assert done.stderr == ""
        assert done.returncode == 7

    def test_a_call_the_table_has_no_line_for_falls_through(self, stub):
        stub.install("ffprobe")
        stub.say("ffprobe", "canned\n")
        stub.table("ffprobe", [_table_line(["-i", "a.mkv"], 0, "matched\n")])
        done = stub.run("ffprobe", "-i", "other-ok.mkv")
        assert done.stdout == "canned\n"
        assert done.returncode == 0

    def test_a_line_with_no_response_prints_nothing(self, stub):
        stub.install("ffprobe")
        stub.say("ffprobe", "canned\n")
        stub.table("ffprobe", [_table_line(["-i", "a.mkv"], 2)])
        done = stub.run("ffprobe", "-i", "a.mkv")
        assert done.stdout == ""
        assert done.returncode == 2

    def test_a_table_line_outranks_a_named_status(self, stub):
        stub.install("ffprobe")
        stub.table("ffprobe", [_table_line(["-i", "a.mkv"], 0, "x\n")])
        assert stub.run("ffprobe", "-i", "a.mkv", rc=5).returncode == 0

    def test_a_table_hit_is_still_recorded(self, stub):
        stub.install("ffprobe")
        stub.table("ffprobe", [_table_line(["-i", "a.mkv"], 0, "x\n")])
        stub.run("ffprobe", "-i", "a.mkv")
        assert stub.calls() == [["ffprobe", "-i", "a.mkv"]]

    def test_a_table_hit_does_not_consume_the_counter(self, stub):
        # It answers before the counter is bumped, so the cascade the lists
        # describe is not advanced by a call the table settled.
        stub.install("ffprobe")
        stub.rc_list("ffprobe", [1, 1])
        stub.table("ffprobe", [_table_line(["-i", "tabled.mkv"], 0, "x\n")])
        stub.run("ffprobe", "-i", "tabled.mkv")
        assert stub.run("ffprobe", "-i", "plain.mkv").returncode == 1
