"""Tests for the chapter model's server ladder - chapterllm.Llm.serving.

A small HTTP server written by the test stands in for a model server: it lists
the model and window a case gives at ``/v1/models``. ``nvidia-smi`` is a stub
saying how much of the GPU is free. What is pinned: the first server that
answers is used and left running; the first, down, is started where the GPU
has room and stopped again; with no room, the next that answers is used with
the model and window it lists; with none, a recording gets no chapters.
"""

import os
import socket
import subprocess
import sys
import time

import pytest

from medialib.lib import aichapters, chapterllm, modelserver

pytestmark = pytest.mark.stubbed

# The stand-in model server: lists one model, with its window when one is
# given, on the port it is given.
_SERVE = r"""
import http.server, json, sys
port, model, window = int(sys.argv[1]), sys.argv[2], int(sys.argv[3])
class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass
    def do_GET(self):
        entry = {"id": model}
        if window:
            entry["max_model_len"] = window
        payload = json.dumps({"data": [entry]}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)
http.server.HTTPServer(("127.0.0.1", port), Handler).serve_forever()
"""


def _port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _url(port: int) -> str:
    return "http://127.0.0.1:%d/v1" % port


@pytest.fixture()
def gpu(tmp_path, monkeypatch):
    """A PATH with an nvidia-smi saying how many MiB are free, 30000 at first;
    and no server left over from another case."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    smi = bin_dir / "nvidia-smi"

    def say(mib):
        smi.write_text("#!/bin/sh\necho %d\n" % mib)
        os.chmod(str(smi), 0o755)
    say(30000)
    monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep
                       + os.environ.get("PATH", ""))
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setattr(modelserver, "_POLL", 0.1)
    monkeypatch.setattr(chapterllm, "_servers", {})
    yield say
    for server in chapterllm._servers.values():
        server.stop()


@pytest.fixture()
def serve(tmp_path):
    """The command that starts a stand-in on ``port`` listing ``model``."""
    script = tmp_path / "serve.py"
    script.write_text(_SERVE)

    def command(port, model="chapters-35b", window=0):
        return "%s -I %s %d %s %d" % (sys.executable, script, port, model,
                                      window)
    return command


@pytest.fixture()
def running(serve):
    """Stand-ins started by the case, as one already running would be."""
    started = []

    def start(port, model="chapters-27b", window=0):
        process = subprocess.Popen(serve(port, model, window).split())
        started.append(process)
        for _ in range(100):
            if modelserver.answering(_url(port)):
                return process
            time.sleep(0.05)
        raise AssertionError("the stand-in did not come up")
    yield start
    for process in started:
        process.kill()
        process.wait()


def _llm(urls, model="chapters-35b", serve="", context=200000,
         context_given=True):
    return chapterllm.Llm("openai", urls[0], model, context_tokens=context,
                          fallbacks=tuple(urls[1:]), serve=serve,
                          serve_mib=20000, context_given=context_given)


class TestTheFirstServer:
    def test_answering_is_used_and_left_running(self, gpu, running):
        port = _port()
        process = running(port, "chapters-35b", 240000)
        llm = _llm([_url(port), _url(_port())], serve="false")
        lines = []
        with llm.serving(lines.append) as served:
            assert (served.url, served.model) == (_url(port), "chapters-35b")
            assert served.context_tokens == 200000
        assert lines == []
        for server in chapterllm._servers.values():
            server.stop()
        assert process.poll() is None

    def test_down_is_started_with_room_on_the_gpu_and_stopped(self, gpu, serve,
                                                              monkeypatch):
        monkeypatch.setattr(chapterllm, "SERVE_IDLE", 0.2)
        port = _port()
        llm = _llm([_url(port)], serve=serve(port, "chapters-35b"))
        lines = []
        with llm.serving(lines.append) as served:
            assert served.url == _url(port)
            assert modelserver.answering(_url(port))
        assert lines[0].startswith("  Starting the model server for ")
        for _ in range(100):
            if not modelserver.answering(_url(port)):
                break
            time.sleep(0.1)
        assert not modelserver.answering(_url(port))

    def test_is_stopped_at_the_end_of_the_run(self, gpu, serve):
        port = _port()
        llm = _llm([_url(port)], serve=serve(port))
        with llm.serving(print):
            pass
        modelserver.stop_all()
        assert not modelserver.answering(_url(port))


class TestTheCardTaken:
    def test_the_next_server_is_asked_for_the_model_it_lists(self, gpu, serve,
                                                             running):
        gpu(2000)
        first, second = _port(), _port()
        running(second, "chapters-27b", 65536)
        llm = _llm([_url(first), _url(second)], serve=serve(first))
        lines = []
        with llm.serving(lines.append) as served:
            assert (served.url, served.model) == (_url(second), "chapters-27b")
            assert served.context_tokens == 65536
            assert not served.fallbacks and not served.serve
        assert lines == ["    asking chapters-27b at %s" % _url(second)]
        assert not modelserver.answering(_url(first))

    def test_a_window_left_unset_is_the_one_the_server_lists(self, gpu, running):
        port = _port()
        running(port, "chapters-27b", 262144)
        llm = _llm([_url(port)], model="", context=chapterllm.DEFAULT_CONTEXT_TOKENS,
                   context_given=False)
        with llm.serving(print) as served:
            assert (served.model, served.context_tokens) == ("chapters-27b", 262144)

    def test_and_the_setting_where_the_server_lists_none(self, gpu, running):
        port = _port()
        running(port, "chapters-27b")
        llm = _llm([_url(port)], model="", context=64000)
        with llm.serving(print) as served:
            assert served.context_tokens == 64000

    def test_the_requests_still_share_one_limit(self, gpu, running):
        port = _port()
        running(port)
        llm = _llm([_url(port), _url(_port())])
        with llm.serving(print) as served:
            assert served._slots is llm._slots


class TestNoServer:
    def test_is_an_error_naming_the_addresses(self, gpu):
        gpu(2000)
        urls = [_url(_port()), _url(_port())]
        with pytest.raises(chapterllm.LlmError, match="no chapter model server"), \
                _llm(urls, serve="false").serving(print):
            pass

    def test_and_a_recording_gets_no_chapters(self, gpu):
        llm = _llm([_url(_port()), _url(_port())])
        cues = [aichapters.Cue(second * 1000, second * 1000 + 900, "line")
                for second in range(0, 3600, 10)]
        lines = []
        assert aichapters.generate(cues, llm, log=lines.append) == []
        assert lines[-1].startswith("    no model to ask: no chapter model server")

    def test_one_address_with_a_model_is_used_unasked(self, gpu):
        llm = _llm([_url(_port())])
        with llm.serving(print) as served:
            assert served is llm


class TestGenerate:
    def test_chapters_on_the_server_it_holds(self, gpu, running, monkeypatch):
        port = _port()
        running(port, "chapters-27b", 65536)
        seen = []
        monkeypatch.setattr(aichapters, "_chapters",
                            lambda cues, found, llm, *rest: seen.append(llm) or [])
        cues = [aichapters.Cue(second * 1000, second * 1000 + 900, "line")
                for second in range(0, 3600, 10)]
        aichapters.generate(cues, _llm([_url(_port()), _url(port)]))
        assert [(llm.url, llm.model, llm.context_tokens) for llm in seen] == [
            (_url(port), "chapters-27b", 65536)]
