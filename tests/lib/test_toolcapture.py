"""The white box for medialib/lib/toolcapture.py - a tool's output kept in RAM
and replayed only when the tool failed.

The tools here are this interpreter, told what to print and how to exit.
"""

import io
import os
import sys

import pytest

from medialib.lib import runlog, statusline, toolcapture
from medialib.lib.toolcapture import ToolOutput


def tool(script: str) -> list:
    return [sys.executable, "-c", script]


@pytest.fixture(autouse=True)
def no_status_row(monkeypatch):
    """No row pinned, whatever an earlier case in this process left settled."""
    monkeypatch.setattr(statusline.state, "row", "")


@pytest.fixture
def logs_home(tmp_path, monkeypatch):
    """Failure logs land under the test's own directory."""
    monkeypatch.setenv("CLI_SCRIPT_DIR", str(tmp_path))
    monkeypatch.delenv("LOG_TIMESTAMPS", raising=False)
    return tmp_path / "logs" / toolcapture.FAILURE_DIR


@pytest.mark.pure
class TestToolOutput:
    def kept(self, *pieces, **limits) -> list:
        output = ToolOutput(**limits)
        for piece in pieces:
            output.feed(piece)
        output.close()
        return output.lines()

    def test_lines_may_arrive_in_any_pieces(self):
        assert self.kept("one\ntw", "o\nthr", "ee") == ["one", "two", "three"]

    def test_a_redrawn_line_keeps_its_last_state(self):
        assert self.kept("frame=1\rframe=2\rframe=3\n") == ["frame=3"]

    def test_a_redraw_split_across_pieces_is_not_glued_together(self):
        assert self.kept("frame=1\r", "frame=2\r", "\ndone\n") == [
            "frame=2", "done"]

    def test_a_counter_that_never_ends_its_line_stays_small(self):
        output = ToolOutput()
        for step in range(10000):
            output.feed("%d%%\r" % (step % 100))
        assert len(output._pending) < 20
        output.close()
        assert output.lines() == ["99%"]

    def test_colour_is_dropped(self):
        assert self.kept("\x1b[31mred\x1b[0m\x1b[K\n") == ["red"]

    def test_bytes_split_inside_a_character_decode_whole(self):
        encoded = "café\n".encode()
        assert self.kept(encoded[:4], encoded[4:]) == ["café"]

    def test_what_falls_between_head_and_tail_is_counted(self):
        lines = self.kept("".join("%d\n" % n for n in range(10)),
                          head=2, tail=3)
        assert lines == ["0", "1", "[... 5 line(s) not kept ...]",
                         "7", "8", "9"]

    def test_the_total_counts_every_line_even_the_dropped(self):
        output = ToolOutput(head=1, tail=1)
        output.feed("a\nb\nc\n")
        assert output.total == 3


@pytest.mark.pure
class TestRedact:
    @pytest.mark.parametrize("text, expected", [
        ("GET /search?api_key=abc123&query=x", "GET /search?api_key=***&query=x"),
        ("Api-Key: abc123", "Api-Key: ***"),
        ("--password hunter2 next", "--password *** next"),
        ("password=hunter2", "password=***"),
        ("Authorization: Bearer abc.def", "Authorization: Bearer ***"),
        ("nothing secret here", "nothing secret here"),
    ])
    def test_values_are_starred(self, text, expected):
        assert toolcapture.redact(text) == expected

    def test_a_key_from_the_environment_is_starred_in_any_form(self,
                                                              monkeypatch):
        monkeypatch.setenv("tmdbApiKey", "0123456789abcdef")
        assert toolcapture.redact("url /movie/1?k=0123456789abcdef") == (
            "url /movie/1?k=***")

    def test_a_short_value_is_left_alone(self, monkeypatch):
        """A two-letter value would star out every word it occurs in."""
        monkeypatch.setenv("SOME_KEY", "ab")
        assert toolcapture.redact("tab") == "tab"


@pytest.mark.pure
class TestToolFlags:
    @pytest.mark.parametrize("word, level", [
        ("quiet", "info"), ("", "info"), ("verbose", "verbose")])
    def test_ffmpeg_is_asked_for_more_only_under_v(self, monkeypatch, word,
                                                   level):
        monkeypatch.setenv("LOG_VERBOSITY", word)
        assert toolcapture.ffmpeg_loglevel() == ["-loglevel", level]


@pytest.mark.stubbed
class TestRun:
    def test_a_tool_that_succeeds_prints_nothing(self, logs_home, capfd):
        result = toolcapture.run(tool("print('chatter')"), "item")
        assert result.ok and result.returncode == 0
        assert result.output.lines() == ["chatter"]
        assert capfd.readouterr() == ("", "")
        assert not logs_home.exists()

    def test_standard_output_can_be_the_answer(self, logs_home):
        script = "import sys; print('42'); sys.stderr.write('note\\n')"
        result = toolcapture.run(tool(script), stdout="capture")
        assert result.stdout == "42\n"
        assert result.output.lines() == ["note"]

    def test_a_failure_prints_its_last_lines_and_keeps_them(self, logs_home,
                                                            capfd):
        script = "import sys; [print(n) for n in range(100)]; sys.exit(3)"
        result = toolcapture.run(tool(script), "film.mkv")
        err = capfd.readouterr().err
        assert result.failure == "exited with status 3"
        assert err.startswith(
            "==> ERROR: film.mkv: %s exited with status 3\n"
            % os.path.basename(sys.executable))
        shown = [line for line in err.splitlines() if line.startswith("    | ")]
        assert shown == ["    | %d" % n for n in range(60, 100)]
        assert "all 100 line(s) it printed: %s" % result.log_path in err
        kept = open(result.log_path).read()
        assert "exited with status 3" in kept and "\n0\n" in kept

    def test_quiet_prints_only_the_error_and_where_it_is(self, logs_home,
                                                         capfd, monkeypatch):
        monkeypatch.setenv("LOG_VERBOSITY", "quiet")
        result = toolcapture.run(tool("print('x'); raise SystemExit(1)"))
        err = capfd.readouterr().err
        assert err.splitlines() == [
            "==> ERROR: %s exited with status 1"
            % os.path.basename(sys.executable),
            "    its output: %s" % result.log_path]

    def test_verbose_prints_the_command_and_much_more(self, logs_home, capfd,
                                                      monkeypatch):
        monkeypatch.setenv("LOG_VERBOSITY", "verbose")
        script = ("import sys; [print(n) for n in range(1000)]; "
                  "print('api_key=s3cret'); sys.exit(2)")
        toolcapture.run(tool(script))
        err = capfd.readouterr().err
        assert "    command: " in err and "range(1000)" in err
        assert "    | 0\n" in err and "    | 49\n" in err
        assert "    | 50\n" not in err
        assert "    | [...]\n" in err
        assert "    | 999\n" in err and "    | 701\n" in err
        assert "s3cret" not in err

    def test_a_zero_exit_can_still_be_a_failure(self, logs_home, capfd):
        result = toolcapture.run(tool("print('CUDA out of memory')"),
                                 verify=lambda: "no transcript was written")
        assert result.failure == "no transcript was written"
        assert "    | CUDA out of memory" in capfd.readouterr().err

    def test_a_tool_that_is_not_there_is_a_failure_not_a_crash(self,
                                                               logs_home,
                                                               capfd):
        result = toolcapture.run(["no-such-tool-anywhere"], "item")
        assert result.returncode == 127
        assert result.failure.startswith("could not be started")
        assert "ERROR: item: no-such-tool-anywhere" in capfd.readouterr().err

    def test_report_false_leaves_the_replay_to_the_caller(self, logs_home,
                                                          capfd):
        result = toolcapture.run(tool("raise SystemExit(1)"), report=False)
        assert not result.ok
        assert capfd.readouterr().err == ""
        out = io.StringIO()
        toolcapture.report_failure(result, stream=out)
        assert out.getvalue().startswith("==> ERROR: ")

    def test_a_pinned_status_row_is_erased_before_the_block(self, logs_home,
                                                            capfd,
                                                            monkeypatch):
        monkeypatch.setattr(statusline.state, "row", "1")
        toolcapture.run(tool("raise SystemExit(1)"))
        assert capfd.readouterr().err.startswith("\r\x1b[K==> ERROR: ")

    def test_the_replay_is_written_under_the_run_lock(self, logs_home,
                                                      tmp_path, monkeypatch):
        held = []
        real = runlog.take_lock

        def recording(handle):
            held.append(handle.name)
            return real(handle)

        monkeypatch.setattr(runlog, "take_lock", recording)
        lock = str(tmp_path / "run.lock")
        toolcapture.run(tool("raise SystemExit(1)"), lock_file=lock)
        assert held == [lock]

    def test_an_unwritable_log_home_still_prints_the_error(self, tmp_path,
                                                           monkeypatch, capfd):
        blocker = tmp_path / "file"
        blocker.write_text("")
        monkeypatch.setenv("CLI_SCRIPT_DIR", str(blocker))
        result = toolcapture.run(tool("raise SystemExit(1)"))
        assert result.log_path == ""
        assert "ERROR:" in capfd.readouterr().err


@pytest.mark.stubbed
class TestAFailureTheCallerSkips:
    def test_it_opens_with_the_callers_warning(self, logs_home, capfd):
        toolcapture.run(tool("print('no stream'); raise SystemExit(1)"), "x.mkv",
                        tool="ffmpeg", warning="no audio track: x.mkv")
        err = capfd.readouterr().err
        assert err.startswith("==> WARNING: no audio track: x.mkv "
                              "(ffmpeg exited with status 1)\n")
        assert "    | no stream\n" in err
        assert "ERROR" not in err

    def test_quiet_does_not_print_it_but_still_keeps_it(self, logs_home, capfd,
                                                       monkeypatch):
        monkeypatch.setenv("LOG_VERBOSITY", "quiet")
        result = toolcapture.run(tool("raise SystemExit(1)"),
                                 warning="skipped")
        assert capfd.readouterr().err == ""
        assert os.path.isfile(result.log_path)


@pytest.mark.pure
class TestFinished:
    """For a caller that ran the tool itself, feeding its own ToolOutput."""

    def test_a_zero_exit_is_a_success(self):
        assert toolcapture.finished(["ffmpeg"], 0, ToolOutput()).ok

    def test_a_signal_is_named(self):
        result = toolcapture.finished(["ffmpeg"], -9, ToolOutput())
        assert result.failure == "was killed by signal 9"

    def test_the_tool_is_named_as_the_caller_said(self):
        result = toolcapture.finished(["bash", "-c", "x"], 1, ToolOutput(),
                                      tool="vspipe | ffmpeg")
        assert result.name == "vspipe | ffmpeg"
