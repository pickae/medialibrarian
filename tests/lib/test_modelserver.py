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


class TestARunHoldingItsServers:
    """A run that fans out to workers holds its servers in its own process:
    a worker neither starts nor stops one, and the run's end stops only what
    it started."""

    @pytest.fixture(autouse=True)
    def fresh_run(self):
        from medialib.lib import runcontext
        runcontext.forget(modelserver.OWNER_VARIABLE)
        yield
        runcontext.forget(modelserver.OWNER_VARIABLE)

    def test_a_server_not_running_is_started_held_and_stopped_at_the_end(
            self, gpu, serve):
        port = _port()
        server = _server("http://127.0.0.1:%d" % port, serve(port), idle=0.1)
        with modelserver.held_for_run([server], print) as held:
            assert held == [server]
            time.sleep(0.4)
            assert modelserver.answering(server.url)
        assert not modelserver.answering(server.url)
        assert not server.started_here

    def test_a_server_found_running_is_left_running(self, gpu, serve):
        import subprocess
        port = _port()
        running = subprocess.Popen(serve(port).split())
        try:
            url = "http://127.0.0.1:%d/v1" % port
            for _ in range(50):
                if modelserver.answering(url):
                    break
                time.sleep(0.1)
            with modelserver.held_for_run([_server(url, "false")], print):
                pass
            assert running.poll() is None
            assert modelserver.answering(url)
        finally:
            running.kill()
            running.wait()

    def test_a_worker_neither_starts_nor_stops_a_server(
            self, gpu, serve, monkeypatch):
        from medialib.lib import runcontext
        parent = subprocess_sleeper()
        try:
            runcontext.settle(modelserver.OWNER_VARIABLE, str(parent.pid))
            monkeypatch.setattr(modelserver, "START_TIMEOUT", 0.5)
            port = _port()
            worker = _server("http://127.0.0.1:%d" % port, serve(port))
            assert not worker.acquire(print)
            assert not worker.started_here
        finally:
            parent.kill()
            parent.wait()

    def test_a_worker_waits_for_the_run_to_bring_its_server_back(
            self, gpu, serve):
        import subprocess

        from medialib.lib import runcontext
        port = _port()
        url = "http://127.0.0.1:%d" % port
        parent = subprocess_sleeper()
        coming = subprocess.Popen(serve(port, "0.5").split())
        try:
            runcontext.settle(modelserver.OWNER_VARIABLE, str(parent.pid))
            worker = _server(url, "false")
            assert worker.acquire(print)
            worker.release()
            assert not worker.started_here
        finally:
            for process in (parent, coming):
                process.kill()
                process.wait()
        assert not modelserver.answering(url)

    def test_a_worker_gives_up_waiting_at_its_deadline(self, gpu):
        from medialib.lib import runcontext
        parent = subprocess_sleeper()
        try:
            runcontext.settle(modelserver.OWNER_VARIABLE, str(parent.pid))
            started = time.monotonic()
            assert not _server("http://127.0.0.1:%d" % _port()).acquire(
                print, until=started + 0.3)
            assert time.monotonic() - started < 5
        finally:
            parent.kill()
            parent.wait()

    def test_a_server_that_went_down_is_started_again(self, gpu, serve):
        import signal
        port = _port()
        server = _server("http://127.0.0.1:%d" % port, serve(port))
        lines: list = []
        with modelserver.held_for_run([server], lines.append, watch=0.1):
            assert server._process is not None
            os.killpg(server._process.pid, signal.SIGKILL)
            server._process.wait()
            for _ in range(100):
                time.sleep(0.1)
                if server._process is not None \
                        and server._process.poll() is None \
                        and modelserver.answering(server.url):
                    break
            assert modelserver.answering(server.url)
        assert any("restarting" in line for line in lines)
        assert not modelserver.answering(server.url)

    def test_inside_a_run_that_holds_them_nothing_more_is_held(self, gpu, serve):
        from medialib.lib import runcontext
        parent = subprocess_sleeper()
        try:
            runcontext.settle(modelserver.OWNER_VARIABLE, str(parent.pid))
            port = _port()
            server = _server("http://127.0.0.1:%d" % port, serve(port))
            with modelserver.held_for_run([server], print) as held:
                assert held == []
                assert not server.started_here
            assert runcontext.get(modelserver.OWNER_VARIABLE) == str(parent.pid)
        finally:
            parent.kill()
            parent.wait()


def subprocess_sleeper():
    """A live process standing in for the run that holds the servers."""
    import subprocess
    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])


class TestTheAddress:
    @pytest.mark.parametrize("url", ["127.0.0.1:8082",
                                     "http://127.0.0.1:8082/v1",
                                     "http://127.0.0.1:8082/v1/chat/completions/"])
    def test_any_spelling_is_the_server(self, url):
        assert modelserver.root(url) == "http://127.0.0.1:8082"

    def test_a_silent_one_lists_nothing(self):
        assert modelserver.served_model("http://127.0.0.1:%d" % _port()) \
            is None


# --- the models a run made a server load -----------------------------------------

# The real one, before the suite's fixture stands it in with a no-op.
_note_request = modelserver.note_request


class _Holder:
    """A model server in this process that loads what it is asked for: the
    ninfer switcher (/health, /unload) or Ollama (/api/ps, keep_alive 0)."""

    def __init__(self, kind):
        import http.server
        import json
        import threading
        self.kind = kind
        self.held: set = set()
        self.unloaded: list = []
        holder = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _send(self, code, obj):
                payload = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def do_GET(self):
                if kind == "switcher" and self.path == "/health":
                    current = next(iter(holder.held), None)
                    return self._send(200, {"status": "ok", "current": current})
                if kind == "ollama" and self.path == "/api/ps":
                    return self._send(200, {"models": [
                        {"name": name, "model": name} for name in holder.held]})
                if self.path == "/v1/models":
                    return self._send(200, {"data": [{"id": "m"}]})
                self._send(404, {})

            def do_POST(self):
                body = json.loads(self.rfile.read(
                    int(self.headers.get("Content-Length") or 0)) or b"{}")
                if kind == "switcher" and self.path == "/unload" \
                        or kind == "ollama" and self.path == "/api/generate" \
                        and body.get("keep_alive") == 0:
                    holder.held.discard(body["model"])
                    holder.unloaded.append(body["model"])
                    return self._send(200, {"status": "unloaded"})
                self._send(404, {})

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = "http://127.0.0.1:%d/v1" % self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture(params=["switcher", "ollama"])
def holder(request):
    made = _Holder(request.param)
    yield made
    made.close()


class TestWhatARunLoaded:
    def test_a_model_the_server_did_not_hold_is_unloaded_after_an_interrupt(
            self, holder):
        _note_request(holder.url, "big")
        holder.held.add("big:latest" if holder.kind == "ollama" else "big")
        modelserver.eject_loaded()
        assert holder.unloaded == ["big"]

    def test_one_the_server_held_already_is_left(self, holder):
        holder.held.add("big")
        _note_request(holder.url, "big")
        modelserver.eject_loaded()
        assert holder.unloaded == []
        assert holder.held == {"big"}

    def test_one_the_server_has_let_go_since_is_not_asked_again(self, holder):
        _note_request(holder.url, "big")
        modelserver.eject_loaded()
        assert holder.unloaded == []

    def test_a_server_that_cannot_say_what_it_holds_is_left_alone(self):
        _note_request("http://127.0.0.1:%d/v1" % _port(), "big")
        assert modelserver._loaded_here == set()

    def test_the_workers_notes_reach_the_process_that_ends_the_run(
            self, holder, tmp_path):
        from medialib.lib import runcontext, safety
        safety.init_abort_flag(str(tmp_path / "abortRequested"))
        _note_request(holder.url, "big")
        holder.held.add("big")
        # Another process's note: only the shared file carries it.
        modelserver._loaded_here.clear()
        assert os.path.exists(modelserver._shared_file())
        runcontext.request_abort()
        modelserver._eject_at_exit()
        assert holder.unloaded == ["big"]
        assert not os.path.exists(modelserver._shared_file())

    def test_the_workers_notes_outlive_the_scratch_handed_back_first(
            self, holder, tmp_path):
        import shutil

        from medialib.lib import runcontext, safety
        scratch = tmp_path / "scratch"
        scratch.mkdir()
        safety.init_abort_flag(str(scratch / "abortRequested"))
        _note_request(holder.url, "big")
        holder.held.add("big")
        modelserver._loaded_here.clear()
        runcontext.request_abort()
        # The command's own cleanup, before the exit hooks.
        shutil.rmtree(scratch)
        modelserver._eject_at_exit()
        assert holder.unloaded == ["big"]

    def test_a_run_that_finishes_unloads_its_models_too(self, holder, tmp_path):
        # A switching server never lets an idle model go by itself.
        from medialib.lib import safety
        safety.init_abort_flag(str(tmp_path / "abortRequested"))
        _note_request(holder.url, "big")
        holder.held.add("big")
        modelserver._loaded_here.clear()     # a worker's note
        modelserver._eject_at_exit()
        assert holder.unloaded == ["big"]
        assert not os.path.exists(modelserver._shared_file())

    def test_a_command_finishing_inside_another_run_leaves_its_models(
            self, holder, tmp_path, monkeypatch):
        from medialib.lib import runcontext, safety
        # settle() hands the run down through the environment: put it back.
        for name in ("ABORT_FLAG", "ABORT_FLAG_OWNER"):
            monkeypatch.delenv(name, raising=False)
        safety.init_abort_flag(str(tmp_path / "abortRequested"))
        runcontext.settle("ABORT_FLAG_OWNER", str(os.getpid() + 99999))
        _note_request(holder.url, "big")
        holder.held.add("big")
        modelserver._eject_at_exit()
        assert holder.unloaded == []
        # Its note is left for the run that owns it.
        assert os.path.exists(modelserver._shared_file())

    def test_a_process_no_run_was_settled_in_unloads_what_it_loaded(
            self, holder, monkeypatch):
        for name in ("ABORT_FLAG", "ABORT_FLAG_OWNER"):
            monkeypatch.delenv(name, raising=False)
        _note_request(holder.url, "big")
        holder.held.add("big")
        modelserver._eject_at_exit()
        assert holder.unloaded == ["big"]

    def test_the_flag_gone_at_exit_still_counts_as_interrupted(
            self, holder, tmp_path):
        from medialib.lib import runcontext, safety
        flag = safety.init_abort_flag(str(tmp_path / "abortRequested"))
        _note_request(holder.url, "big")
        holder.held.add("big")
        open(flag, "w").close()     # a worker's interrupt, not this process's
        safety.release_abort_flag()
        assert runcontext.interrupted()
        modelserver._eject_at_exit()
        assert holder.unloaded == ["big"]
