"""Tests for medialib.lib.modelserver - a model server started when needed.

A small HTTP server written by the test stands in for a model server: started
by the command a case gives, it answers ``/v1/models``. ``nvidia-smi`` is a
stub saying how much of the GPU is free. What is pinned: a server found
running is used and left; one not running is started only with room for it,
used once it answers, and stopped once idle or at the end; with no room, it
is not to be had.
"""

import os
import socket
import sys
import time

import pytest

from medialib.lib import modelserver

pytestmark = pytest.mark.stubbed

# The stand-in model server: answers /v1/models on the port it is given, after
# the delay it is given; "never" stays silent instead.
_SERVE = r"""
import http.server, json, sys, time
port, delay = int(sys.argv[1]), sys.argv[2]
if delay == "never":
    time.sleep(3600)
time.sleep(float(delay))
class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass
    def do_GET(self):
        payload = json.dumps({"data": [{"id": "vision-35b"}]}).encode()
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


@pytest.fixture()
def gpu(tmp_path, monkeypatch):
    """A PATH with an nvidia-smi that says ``free[0]`` MiB are free."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    free = [30000]
    smi = bin_dir / "nvidia-smi"

    def say(mib):
        free[0] = mib
        smi.write_text("#!/bin/sh\necho %d\n" % mib)
        os.chmod(str(smi), 0o755)
    say(30000)
    monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep
                       + os.environ.get("PATH", ""))
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setattr(modelserver, "_POLL", 0.1)
    return say


@pytest.fixture()
def serve(tmp_path):
    script = tmp_path / "serve.py"
    script.write_text(_SERVE)

    def command(port, delay="0"):
        return "%s -I %s %d %s" % (sys.executable, script, port, delay)
    return command


def _server(url, command="", need=20000, idle=300.0):
    return modelserver.Server(url, command, need, idle)


class TestAServerFoundRunning:
    def test_is_used_and_left_running(self, gpu, serve):
        import subprocess
        port = _port()
        running = subprocess.Popen(serve(port).split())
        try:
            url = "http://127.0.0.1:%d/v1" % port
            for _ in range(50):
                if modelserver.answering(url):
                    break
                time.sleep(0.1)
            server = _server(url, command="false")
            assert server.acquire(print)
            server.release()
            server.stop()
            assert not server.started_here
            assert running.poll() is None
        finally:
            running.kill()
            running.wait()


class TestAServerNotRunning:
    def test_is_started_with_room_on_the_gpu_and_used(self, gpu, serve):
        port = _port()
        server = _server("http://127.0.0.1:%d" % port, serve(port, "0.3"))
        lines = []
        try:
            assert server.acquire(lines.append)
            assert server.started_here
            assert modelserver.served_model(server.url) == ["vision-35b"]
            assert lines[0].startswith("  Starting the model server for ")
        finally:
            server.stop()
        assert not modelserver.answering(server.url)

    def test_is_stopped_once_idle(self, gpu, serve):
        port = _port()
        server = _server("http://127.0.0.1:%d" % port, serve(port),
                         idle=0.2)
        assert server.acquire(print)
        server.release()
        for _ in range(100):
            if not modelserver.answering(server.url):
                break
            time.sleep(0.1)
        assert not modelserver.answering(server.url)
        assert not server.started_here

    def test_is_not_stopped_while_in_use(self, gpu, serve):
        port = _port()
        server = _server("http://127.0.0.1:%d" % port, serve(port),
                         idle=0.2)
        try:
            assert server.acquire(print)
            assert server.acquire(print)
            server.release()
            time.sleep(0.6)
            assert modelserver.answering(server.url)
        finally:
            server.stop()

    def test_with_the_gpu_taken_it_is_not_started(self, gpu, serve):
        gpu(3500)
        port = _port()
        server = _server("http://127.0.0.1:%d" % port, serve(port))
        assert not server.acquire(print)
        assert not server.started_here

    def test_without_a_command_it_is_not_to_be_had(self, gpu):
        assert not _server("http://127.0.0.1:%d" % _port()).acquire(print)

    def test_one_that_never_answers_is_given_up_and_stopped(
            self, gpu, serve, monkeypatch):
        monkeypatch.setattr(modelserver, "START_TIMEOUT", 0.5)
        port = _port()
        server = _server("http://127.0.0.1:%d" % port, serve(port, "never"))
        lines = []
        assert not server.acquire(lines.append)
        assert not server.started_here
        assert lines[-1].startswith("  WARNING: the model server for ")

    def test_the_run_s_end_stops_what_it_started(self, gpu, serve):
        port = _port()
        server = _server("http://127.0.0.1:%d" % port, serve(port))
        assert server.acquire(print)
        modelserver.stop_all()
        assert not modelserver.answering(server.url)


class TestTheAddress:
    @pytest.mark.parametrize("url", ["127.0.0.1:8082",
                                     "http://127.0.0.1:8082/v1",
                                     "http://127.0.0.1:8082/v1/chat/completions/"])
    def test_any_spelling_is_the_server(self, url):
        assert modelserver.root(url) == "http://127.0.0.1:8082"

    def test_a_silent_one_lists_nothing(self):
        assert modelserver.served_model("http://127.0.0.1:%d" % _port()) \
            is None
