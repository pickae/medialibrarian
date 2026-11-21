"""Tests for medialib.lib.subtitleocr - picture subtitles read by seconv.

seconv, its engines' programs and mkvextract are stubs on PATH that do what a
case tells them. What is pinned: the engines tried in their order, one that
fails or reads nothing giving way to the next, an engine that is not there
or has no data for the language left out, the tracks taken out of the film in
one pass, and the text read back out of what was written.
"""

import contextlib
import json
import os
import shutil
import threading
import time

import pytest

from medialib.lib import (
    languages,
    llmclient,
    llmroster,
    llmtraits,
    subtitleocr,
    subtitletracks,
)

FRENCH = next(row for row in languages.recognised() if row.code2 == "fr")
ENGLISH = next(row for row in languages.recognised() if row.code2 == "en")

_CUES = ("1\n00:00:01,000 --> 00:00:03,000\nBonjour.\n\n"
         "2\n00:01:00,000 --> 00:01:02,000\n- Oui ?\n- Non.\n")

# The fake seconv: records its arguments, then does what the file named after
# its engine says - "ok" writes the cues where it is told and says so in its
# JSON, "empty" exits 0 having written nothing, anything else exits 1.
_SECONV = r"""#!/bin/bash
PATH="$PATH:/usr/bin:/bin"
bin="$(dirname "$0")"
printf '%s\n' "$@" >> "$bin/seconv.argv"
echo "CUDA_VISIBLE_DEVICES=${CUDA_VISIBLE_DEVICES-unset}" >> "$bin/seconv.argv"
echo --- >> "$bin/seconv.argv"
for arg in "$@"; do
  case "$arg" in
    --ocr-engine:*) engine="${arg#--ocr-engine:}" ;;
    --output-folder:*) out="${arg#--output-folder:}" ;;
  esac
done
case "$(cat "$bin/behaviour-$engine" 2>/dev/null)" in
  ok) cp "$bin/cues.srt" "$out/read.srt"
      printf '{"success": true, "files": [{"success": true, "output": "%s"}]}' \
        "$out/read.srt"
      exit 0 ;;
  empty) echo '{"success": true, "files": []}'; exit 0 ;;
  *) echo '{"success": false, "files": []}'; exit 1 ;;
esac
"""


@pytest.fixture()
def bin_dir(tmp_path, monkeypatch):
    real_curl = shutil.which("curl")
    path = tmp_path / "bin"
    path.mkdir()
    if real_curl and os.name != "nt":
        os.symlink(real_curl, path / "curl")
    monkeypatch.setenv("PATH", str(path))
    monkeypatch.delenv("subtitleOcrEngines", raising=False)
    monkeypatch.setattr(subtitleocr, "_said", set())
    monkeypatch.setattr(subtitleocr, "_tesseract_languages", None)
    monkeypatch.setattr(subtitleocr, "paddle_on_gpu", lambda: False)
    monkeypatch.setattr(llmclient, "_servers", {})
    llmroster.forget()
    llmtraits.forget()
    monkeypatch.setattr(llmtraits, "_server_json", lambda *a, **k: None)
    (path / "cues.srt").write_text(_CUES)
    return path


def _toml(value):
    if isinstance(value, dict):
        return "{ %s }" % ", ".join("%s = %s" % (key, _toml(each))
                                    for key, each in value.items())
    return json.dumps(value)


def _vision(tmp_path, url, **fields):
    """The environment of a roster holding one vision model at ``url``;
    ``fields`` are that model's own (jobs, model, options)."""
    lines = ['[models.vision]', 'url = %s' % json.dumps(url),
             'traits = { vision = true }']
    lines += ["%s = %s" % (field, _toml(value))
              for field, value in fields.items()]
    path = tmp_path / "models.toml"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return {"llmModels": str(path)}


def _tool(bin_dir, name, script):
    tool = bin_dir / name
    tool.write_text(script)
    os.chmod(str(tool), 0o755)


def _engines(bin_dir, tesseract_languages=("eng", "fra"), **behaviour):
    """seconv, and each engine's program, with what each engine does."""
    _tool(bin_dir, "seconv", _SECONV)
    _tool(bin_dir, "paddleocr", "#!/bin/bash\n")
    _tool(bin_dir, "tesseract", "#!/bin/bash\necho 'List of languages:'\n"
          + "".join("echo %s\n" % code for code in tesseract_languages))
    for engine, what in behaviour.items():
        (bin_dir / ("behaviour-" + engine)).write_text(what)


def _calls(bin_dir):
    path = bin_dir / "seconv.argv"
    if not path.exists():
        return []
    calls, call = [], []
    for line in path.read_text().splitlines():
        if line == "---":
            calls.append(call)
            call = []
        else:
            call.append(line)
    return calls


def _read(bin_dir, tmp_path, row=FRENCH, environ=None, usage=None):
    image = tmp_path / "scratch" / "track3.sup"
    image.parent.mkdir(exist_ok=True)
    image.write_bytes(b"PG")
    srt = tmp_path / "Film.fr.srt"
    logs = []
    engine = subtitleocr.recognise(str(image), row, str(srt), logs.append,
                                   environ=environ, usage=usage)
    return engine, srt, logs


@pytest.mark.pure
class TestTheEngines:
    """An engine that reads every language first, where the machine can run
    it at speed; tesseract's few languages next; every language slowly last."""

    @pytest.fixture(autouse=True)
    def _gpu(self, monkeypatch):
        self.gpu = [False]
        llmroster.forget()
        llmtraits.forget()
        monkeypatch.setattr(llmtraits, "_server_json", lambda *a, **k: None)
        monkeypatch.setattr(subtitleocr, "paddle_on_gpu",
                            lambda: self.gpu[0])

    def test_without_a_gpu_tesseract_then_paddle_on_the_cpu(self):
        assert subtitleocr.engines({}) == (("tesseract", ""),
                                           ("paddle", "cpu"))

    def test_with_room_on_the_gpu_paddle_there_first(self):
        self.gpu[0] = True
        assert subtitleocr.engines({}) == (("paddle", ""), ("tesseract", ""),
                                           ("paddle", "cpu"))

    def test_a_roster_vision_model_enables_vision(self, tmp_path):
        models = tmp_path / "models.toml"
        models.write_text(
            '[models.vision-local]\nurl = "http://127.0.0.1:8082/v1"\n'
            'model = "gemma3-12b"\ntraits = { vision = true }\n',
            encoding="utf-8")
        assert subtitleocr.engines(
            {"llmModels": str(models)})[0] == ("vision", "")

    def test_a_text_only_roster_does_not_enable_vision(self, tmp_path):
        models = tmp_path / "models.toml"
        models.write_text(
            '[models.text-only]\nurl = "http://127.0.0.1:8082/v1"\n'
            'model = "qwen3-8b"\ntraits = { vision = false }\n',
            encoding="utf-8")
        assert subtitleocr.engines({"llmModels": str(models)}) == (
            ("tesseract", ""), ("paddle", "cpu"))

    def test_the_setting_names_them_and_their_order(self):
        assert subtitleocr.engines(
            {"subtitleOcrEngines": "tesseract, paddle"}) == (
                ("tesseract", ""), ("paddle", ""))

    def test_a_name_it_does_not_know_is_left_out(self):
        assert subtitleocr.engines(
            {"subtitleOcrEngines": "nocr,tesseract"}) == (("tesseract", ""),)

    def test_vision_only_with_a_server_to_ask(self, tmp_path):
        named = {"subtitleOcrEngines": "vision,tesseract"}
        assert subtitleocr.engines(named) == (("tesseract", ""),)
        assert subtitleocr.engines(
            dict(named, **_vision(tmp_path, "http://127.0.0.1:8080/v1"))) \
            == (("vision", ""), ("tesseract", ""))


@pytest.mark.pure
class TestTheGpu:
    def test_paddle_goes_there_with_cuda_and_room(self, monkeypatch):
        monkeypatch.setattr(subtitleocr, "_paddle_has_cuda", lambda: True)
        monkeypatch.setattr(subtitleocr.modelserver, "free_vram", lambda: 3000)
        assert subtitleocr.paddle_on_gpu()

    def test_not_on_a_card_another_program_fills(self, monkeypatch):
        monkeypatch.setattr(subtitleocr, "_paddle_has_cuda", lambda: True)
        monkeypatch.setattr(subtitleocr.modelserver, "free_vram", lambda: 1500)
        assert not subtitleocr.paddle_on_gpu()

    def test_nor_without_cuda(self, monkeypatch):
        monkeypatch.setattr(subtitleocr, "_paddle_has_cuda", lambda: False)
        monkeypatch.setattr(subtitleocr.modelserver, "free_vram", lambda: 30000)
        assert not subtitleocr.paddle_on_gpu()


@pytest.mark.stubbed
@pytest.mark.seconv
class TestReading:
    def test_the_first_engine_that_reads_it_writes_the_sidecar(
            self, bin_dir, tmp_path):
        _engines(bin_dir, tesseract="ok", paddle="ok")
        engine, srt, logs = _read(bin_dir, tmp_path)
        assert engine == "tesseract"
        assert srt.read_text() == _CUES
        assert len(_calls(bin_dir)) == 1
        assert logs == []

    def test_paddle_on_the_cpu_is_kept_off_the_gpu(self, bin_dir, tmp_path):
        _engines(bin_dir, tesseract="fail", paddle="ok")
        engine, _srt, _logs = _read(bin_dir, tmp_path)
        assert engine == "paddle (CPU)"
        tesseract, paddle = _calls(bin_dir)
        assert "CUDA_VISIBLE_DEVICES=" in paddle
        assert "CUDA_VISIBLE_DEVICES=unset" in tesseract

    def test_a_gpu_too_full_after_all_is_one_more_failure(
            self, bin_dir, tmp_path, monkeypatch):
        """paddle on the GPU fails, tesseract has no French, and paddle on
        the CPU reads it."""
        monkeypatch.setattr(subtitleocr, "paddle_on_gpu", lambda: True)
        _engines(bin_dir, tesseract_languages=("eng",), paddle="fail")
        (bin_dir / "seconv").write_text(_SECONV.replace(
            'case "$(cat', '[ "${CUDA_VISIBLE_DEVICES-x}" = "" ] && engine=paddlecpu\ncase "$(cat'))
        (bin_dir / "behaviour-paddlecpu").write_text("ok")
        engine, srt, _logs = _read(bin_dir, tmp_path)
        assert engine == "paddle (CPU)"
        assert srt.read_text() == _CUES

    def test_each_engine_is_told_the_language_its_own_way(self, bin_dir,
                                                          tmp_path):
        _engines(bin_dir, paddle="ok", tesseract="fail")
        _read(bin_dir, tmp_path)
        tesseract, paddle = _calls(bin_dir)
        assert "--ocr-language:fr" in paddle
        assert "--ocr-language:fra" in tesseract
        for call in (paddle, tesseract):
            assert call[1:2] == ["subrip"]
            assert "--json" in call and "--fix-common-errors" in call

    def test_a_failed_engine_gives_way_to_the_next(self, bin_dir, tmp_path,
                                                   monkeypatch):
        """A GPU too full for paddle is a failed paddle, and the CPU's
        tesseract reads the subtitle instead."""
        monkeypatch.setattr(subtitleocr, "paddle_on_gpu", lambda: True)
        _engines(bin_dir, paddle="fail", tesseract="ok")
        engine, srt, _logs = _read(bin_dir, tmp_path)
        assert engine == "tesseract"
        assert srt.read_text() == _CUES

    def test_an_engine_that_exits_0_having_read_nothing_failed(
            self, bin_dir, tmp_path):
        _engines(bin_dir, tesseract="empty", paddle="ok")
        engine, _srt, logs = _read(bin_dir, tmp_path)
        assert engine == "paddle (CPU)"
        assert logs == ["  tesseract OCR read nothing of the French subtitle, "
                        "trying the next engine"]

    def test_none_reading_it_writes_nothing(self, bin_dir, tmp_path):
        _engines(bin_dir, paddle="fail", tesseract="fail")
        engine, srt, _logs = _read(bin_dir, tmp_path)
        assert engine == ""
        assert not srt.exists()

    def test_an_engine_whose_program_is_missing_is_not_tried(
            self, bin_dir, tmp_path):
        _engines(bin_dir, tesseract="fail")
        os.remove(str(bin_dir / "paddleocr"))
        engine, _srt, logs = _read(bin_dir, tmp_path)
        assert engine == ""
        assert len(_calls(bin_dir)) == 1
        assert logs[-1] == "  WARNING: paddleocr not installed, paddle OCR " \
            "is not tried"

    def test_tesseract_without_the_language_is_not_tried_and_said_once(
            self, bin_dir, tmp_path):
        _engines(bin_dir, tesseract_languages=("eng",), paddle="fail",
                 tesseract="ok")
        said = []
        for _ in range(2):
            engine, _srt, logs = _read(bin_dir, tmp_path)
            assert engine == ""
            said += [line for line in logs if "tesseract" in line]
        assert [call for call in _calls(bin_dir)
                if "--ocr-engine:tesseract" in call] == []
        assert said == ["  WARNING: tesseract has no French data (apt "
                        "install tesseract-ocr-fra), it is not tried for "
                        "French"]

    def test_without_seconv_there_is_no_step(self, bin_dir):
        assert not subtitleocr.available()
        _engines(bin_dir)
        assert subtitleocr.available()


@pytest.mark.stubbed
@pytest.mark.seconv
class TestTakingTheTracksOut:
    _MKVEXTRACT = r"""#!/bin/bash
PATH="$PATH:/usr/bin:/bin"
printf '%s\n' "$@" > "$(dirname "$0")/mkvextract.argv"
shift 2
for target in "$@"; do
  path="${target#*:}"
  printf 'x' > "$path"
  case "$path" in *.sub) printf 'x' > "${path%.sub}.idx" ;; esac
done
"""

    def test_every_track_comes_out_in_one_pass(self, bin_dir, tmp_path):
        _tool(bin_dir, "mkvextract", self._MKVEXTRACT)
        scratch = tmp_path / "scratch"
        scratch.mkdir()
        found = subtitleocr.extract(
            str(tmp_path / "Film.mkv"),
            [("3", "S_HDMV/PGS"), ("4", "S_VOBSUB"), ("5", "S_DVBSUB")],
            str(scratch))
        assert found == {"3": str(scratch / "track3.sup"),
                         "4": str(scratch / "track4.idx")}
        argv = (bin_dir / "mkvextract.argv").read_text().splitlines()
        assert argv == [str(tmp_path / "Film.mkv"), "tracks",
                        "3:" + str(scratch / "track3.sup"),
                        "4:" + str(scratch / "track4.sub")]

    def test_a_broadcast_subtitle_is_not_read(self):
        assert not subtitleocr.readable("S_DVBSUB")
        assert subtitleocr.readable("S_HDMV/PGS")


@pytest.mark.pure
class TestTheText:
    def test_each_cue_with_its_lines(self, tmp_path):
        srt = tmp_path / "a.srt"
        srt.write_text("\ufeff" + _CUES.replace("\n", "\r\n"),
                       encoding="utf-8", newline="")
        assert subtitletracks.cue_texts(str(srt)) == ["Bonjour.",
                                                   "- Oui ?\n- Non."]

    def test_an_unreadable_file_has_none(self, tmp_path):
        assert subtitletracks.cue_texts(str(tmp_path / "missing.srt")) == []


# The fake seconv of the vision engine: the times of four lines, and their
# pictures in a BDN export - the third a sliver of no text, the fourth two
# lines tall - and a fix of common errors that changes nothing.
_SECONV_PICTURES = r"""#!/bin/bash
PATH="$PATH:/usr/bin:/bin"
for arg in "$@"; do
  case "$arg" in --output-folder:*) out="${arg#--output-folder:}" ;; esac
done
case "$*" in
  *--time-codes-only*)
    { printf '1\n00:00:01,042 --> 00:00:03,000\n\n\n'
      printf '2\n00:01:00,000 --> 00:01:02,500\n\n\n'
      printf '3\n00:01:03,000 --> 00:01:04,000\n\n\n'
      printf '4\n00:01:05,000 --> 00:01:07,000\n\n'; } > "$out/track3.srt" ;;
  *BDNXml*)
    mkdir -p "$out/track3"
    for name in one two dots tall; do printf '\x89PNG%s' "$name" > "$out/track3/$name.png"; done
    printf '<BDN><Events>'\
'<Event><Graphic Width="9" Height="50">one.png</Graphic></Event>'\
'<Event><Graphic Width="9" Height="48">two.png</Graphic></Event>'\
'<Event><Graphic Width="9" Height="10">dots.png</Graphic></Event>'\
'<Event><Graphic Width="9" Height="110">tall.png</Graphic></Event>'\
'</Events></BDN>' > "$out/track3/index.xml" ;;
esac
exit 0
"""


@pytest.mark.stubbed
@pytest.mark.seconv
class TestTheVisionModel:
    """The lines' pictures sent to a model on a server of its own, several to
    a request and several requests at once: a real HTTP server stands in for
    it, answering a picture alone with its text and several with a JSON
    array."""

    ALONE = {"one": "Bonjour.", "two": "Hmm.</think>- Oui ?\n- Non.",
             "tall": "Line one.\nLine two.", "dots": "Je ne sais pas."}

    @pytest.fixture()
    def server(self, monkeypatch):
        import base64
        import http.server
        import json
        import threading

        state = {"bodies": [], "busy": 0, "refuse": False, "group": None,
                 "usage": None}

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _send(self, status, document):
                payload = json.dumps(document).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def do_GET(self):
                self._send(200, {"data": [{"id": "vision-35b"}]})

            def do_POST(self):
                body = json.loads(self.rfile.read(
                    int(self.headers["Content-Length"])))
                state["bodies"].append(body)
                if state["busy"]:
                    state["busy"] -= 1
                    self._send(503, {"error": "request_queue_timeout"})
                    return
                if state["refuse"]:
                    self._send(400, {"error": "no"})
                    return
                seen = [base64.b64decode(part["image_url"]["url"].split(
                            ",", 1)[1]).removeprefix(b"\x89PNG").decode()
                        for part in body["messages"][0]["content"]
                        if part["type"] == "image_url"]
                if len(seen) == 1:
                    answer = TestTheVisionModel.ALONE[seen[0]]
                else:
                    answer = state["group"](seen)
                reply = {"choices": [{"message": {"content": answer}}]}
                if state["usage"] is not None:
                    reply["usage"] = state["usage"](len(seen))
                self._send(200, reply)

        httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        monkeypatch.setattr(llmclient, "RETRY_WAIT_SECONDS", 0)
        monkeypatch.setattr(llmtraits, "_server_json", lambda *a, **k: None)
        state["url"] = "http://127.0.0.1:%d" % httpd.server_address[1]
        state["group"] = lambda seen: json.dumps(
            [{"one": "Bonjour.", "two": "- Oui ?\n- Non.",
              "tall": "Line one.\nLine two."}[name] for name in seen])
        yield state
        httpd.shutdown()

    def _read(self, bin_dir, tmp_path, server, jobs="2", usage=None):
        _tool(bin_dir, "seconv", _SECONV_PICTURES)
        return _read(bin_dir, tmp_path,
                     environ=dict(
                         _vision(tmp_path, server["url"] + "/v1",
                                 jobs=int(jobs)),
                         subtitleOcrEngines="vision"), usage=usage)

    def _alone(self, server):
        return [body for body in server["bodies"]
                if sum(part["type"] == "image_url"
                       for part in body["messages"][0]["content"]) == 1]

    def test_each_line_is_read_into_its_own_time(self, bin_dir, tmp_path,
                                                 server):
        engine, srt, logs = self._read(bin_dir, tmp_path, server)
        assert engine == "vision"
        assert srt.read_text() == (
            "1\n00:00:01,042 --> 00:00:03,000\nBonjour.\n\n"
            "2\n00:01:00,000 --> 00:01:02,500\n- Oui ?\n- Non.\n\n"
            "3\n00:01:05,000 --> 00:01:07,000\nLine one.\nLine two.\n\n")
        assert logs == []

    def test_the_pictures_go_in_one_request_each_after_its_number(
            self, bin_dir, tmp_path, server):
        """The sliver of no text is not sent: it would make the model lose
        count, and is no line."""
        self._read(bin_dir, tmp_path, server)
        (body,) = server["bodies"]
        content = body["messages"][0]["content"]
        assert "These 3 images" in content[0]["text"]
        assert "in French" in content[0]["text"]
        assert [part["text"] for part in content
                if part["type"] == "text"][1:] == ["Image 1:", "Image 2:",
                                                   "Image 3:"]

    def test_it_is_asked_without_thinking_and_for_little(self, bin_dir,
                                                         tmp_path, server):
        self._read(bin_dir, tmp_path, server)
        body = server["bodies"][0]
        assert body["model"] == "vision-35b"
        assert body["max_tokens"] == 600
        assert body["reasoning_effort"] == "none"
        assert body["chat_template_kwargs"] == {"enable_thinking": False}

    def test_a_stray_backslash_is_a_slip(self, bin_dir, tmp_path, server):
        server["group"] = lambda seen: (
            '```json\n["Bon\\ jour.", "- Oui ?\\\\n- Non.", '
            '"Line one.\\nLine two."]\n```')
        _engine, srt, _logs = self._read(bin_dir, tmp_path, server)
        assert subtitletracks.cue_texts(str(srt))[:2] == ["Bon jour.",
                                                       "- Oui ?\n- Non."]
        assert self._alone(server) == []

    def test_an_answer_that_is_no_array_is_asked_a_picture_at_a_time(
            self, bin_dir, tmp_path, server):
        server["group"] = lambda seen: '["only one"]'
        _engine, srt, _logs = self._read(bin_dir, tmp_path, server)
        assert len(self._alone(server)) == 3
        assert subtitletracks.cue_texts(str(srt)) == [
            "Bonjour.", "- Oui ?\n- Non.", "Line one.\nLine two."]

    def test_a_line_left_out_of_a_tall_picture_is_asked_again(
            self, bin_dir, tmp_path, server):
        import json
        server["group"] = lambda seen: json.dumps(
            ["Bonjour.", "- Oui ?\n- Non.", "Line one."])
        _engine, srt, _logs = self._read(bin_dir, tmp_path, server)
        assert len(self._alone(server)) == 1
        assert subtitletracks.cue_texts(str(srt))[2] == "Line one.\nLine two."

    def test_a_busy_server_is_asked_again(self, bin_dir, tmp_path, server):
        server["busy"] = 3
        engine, _srt, _logs = self._read(bin_dir, tmp_path, server, jobs="1")
        assert engine == "vision"
        assert len(server["bodies"]) == 4

    def test_a_line_it_will_not_read_hands_the_subtitle_on(
            self, bin_dir, tmp_path, server):
        server["refuse"] = True
        engine, srt, logs = self._read(bin_dir, tmp_path, server)
        assert engine == ""
        assert not srt.exists()
        assert logs[0] == ("  vision OCR at %s/v1 could not read 3 of the 4 "
                           "lines of the French subtitle" % server["url"])

    def test_a_server_that_is_not_up_is_not_waited_for(self, bin_dir,
                                                       tmp_path):
        _tool(bin_dir, "seconv", _SECONV_PICTURES)
        engine, _srt, logs = _read(
            bin_dir, tmp_path,
            environ=dict(_vision(tmp_path, "http://127.0.0.1:1/v1"),
                         subtitleOcrEngines="vision"))
        assert engine == ""
        assert any("no vision OCR server is answering" in line for line in logs)

    def test_down_the_ladder_the_next_server_that_answers_reads(
            self, bin_dir, tmp_path, server):
        """The preferred server is not running and has nothing to start it
        with - or the GPU is taken - so the next in the list reads."""
        _tool(bin_dir, "seconv", _SECONV_PICTURES)
        engine, srt, _logs = _read(
            bin_dir, tmp_path,
            environ=dict(
                _vision(tmp_path, ["http://127.0.0.1:1/v1",
                                   server["url"] + "/v1"]),
                subtitleOcrEngines="vision"))
        assert engine == "vision"
        assert "Bonjour." in srt.read_text()

    def _counted(self, monkeypatch, tmp_path, server, jobs="2"):
        """The pictures read straight off the server, seconv's part stood
        in for, into a fresh :class:`subtitleocr.Usage`."""
        folder = tmp_path / "pictures"
        folder.mkdir()
        lines = []
        for at, (name, height) in enumerate((("one", 50), ("two", 48),
                                             ("dots", 10), ("tall", 110))):
            (folder / (name + ".png")).write_bytes(b"\x89PNG" + name.encode())
            lines.append(("00:00:0%d,000" % at, "00:00:0%d,500" % at,
                          str(folder / (name + ".png")), height))
        monkeypatch.setattr(subtitleocr.toolcapture, "watched",
                            lambda *a, **k: None)
        llm = llmclient.Llm("openai", server["url"] + "/v1", "vision-35b",
                            jobs=int(jobs), said={"vision": True})
        usage = subtitleocr.Usage()
        written = subtitleocr._read_with(
            llm, lines, FRENCH, str(tmp_path), lambda line: None, usage)
        assert written
        return usage

    def test_the_tokens_the_server_reports_are_counted(self, monkeypatch,
                                                       tmp_path, server):
        """Every answered request, the group and the one asked again alone,
        with the tokens its reply reports; the wall-clock is the reading's."""
        server["group"] = lambda seen: json.dumps(
            ["Bonjour.", "- Oui ?\n- Non.", "Line one."])
        server["usage"] = lambda pictures: {"prompt_tokens": 100 * pictures,
                                            "completion_tokens": 7}
        usage = self._counted(monkeypatch, tmp_path, server)
        assert (usage.requests, usage.tally.counted) == (2, 2)
        assert (usage.tally.read, usage.tally.tokens) == (400, 14)
        assert len(usage.spans.all()) == 1
        line = usage.line()
        assert line.startswith("ocr:")
        assert "14 token(s) written" in line
        assert "400 read, 414 in all" in line

    def test_a_server_that_reports_none_is_only_timed(self, monkeypatch,
                                                      tmp_path, server):
        usage = self._counted(monkeypatch, tmp_path, server)
        assert (usage.requests, usage.tally.counted) == (1, 0)
        assert usage.line().endswith(", tokens not reported")

    def test_a_refused_request_is_not_counted(self, monkeypatch, tmp_path,
                                              server):
        server["busy"] = 3
        usage = self._counted(monkeypatch, tmp_path, server, jobs="1")
        assert len(server["bodies"]) == 4
        assert usage.requests == 1


@pytest.mark.pure
class TestGroupSizeAndConcurrency:
    """Batch size scales safely with the model's context window, and
    concurrency stays governed by the roster model's ``jobs``."""

    @pytest.mark.parametrize("context, expected", [
        (32000, 16), (4096, 16), (2048, 8), (1024, 4), (512, 2), (256, 1), (0, 1),
    ])
    def test_group_size_scales_with_context(self, context, expected):
        assert subtitleocr.group_size(context) == expected

    def test_workers_governed_by_llm_jobs_not_32(self):
        local = llmclient.build(
            {"url": "http://127.0.0.1:8082/v1", "model": "gemma3-12b"}, "v")
        assert local.jobs == llmclient.DEFAULT_JOBS
        assert subtitleocr._workers(local, 100) == llmclient.DEFAULT_JOBS
        assert subtitleocr._workers(local, 1) == 1

        custom = llmclient.build(
            {"url": "http://127.0.0.1:8082/v1", "model": "gemma3-12b", "jobs": "4"},
            "v")
        assert subtitleocr._workers(custom, 100) == 4

        cloud = llmclient.build({"api": "anthropic"}, "claude")
        assert cloud.jobs == 0
        assert subtitleocr._workers(cloud, 100) == subtitleocr._CLOUD_JOBS

    def test_small_context_splits_batches_and_holds_concurrency(
            self, monkeypatch, tmp_path):
        lines = []
        for idx in range(10):
            png = tmp_path / ("p%d.png" % idx)
            png.write_bytes(b"\x89PNG" + str(idx).encode())
            lines.append(("00:00:%02d,000" % idx, "00:00:%02d,500" % idx,
                          str(png), 50))
        llm = llmclient.Llm("openai", "http://127.0.0.1:8082/v1", "small",
                            context_tokens=2048, jobs=1, said={"vision": True})
        batches = []
        active = 0
        peak = 0
        lock = threading.Lock()

        def fake_ask_pictures(system, batch_prompt, alone_prompt, images,
                              tally=None, **kwargs):
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
                batches.append(len(images))
            time.sleep(0.01)
            with lock:
                active -= 1
            return ["line %d" % i for i in range(len(images))]

        monkeypatch.setattr(llm, "ask_pictures", fake_ask_pictures)
        monkeypatch.setattr(subtitleocr.toolcapture, "watched",
                            lambda *a, **k: None)
        srt = subtitleocr._read_with(llm, lines, FRENCH, str(tmp_path),
                                     lambda line: None, subtitleocr.Usage())
        assert srt
        assert sorted(batches, reverse=True) == [8, 2]
        assert peak == 1


@pytest.mark.pure
class TestVisionRosterAndFallback:
    """Roster ladder walk, modelmeter accounting, short-array/tall-line
    re-asking, and fallback to paddle/tesseract when vision cannot read."""

    @pytest.fixture(autouse=True)
    def _clean(self, monkeypatch):
        llmroster.forget()
        llmtraits.forget()
        monkeypatch.setattr(llmtraits, "_server_json", lambda *a, **k: None)
        monkeypatch.setattr(subtitleocr, "_said", set())
        monkeypatch.setattr(subtitleocr, "paddle_on_gpu", lambda: False)

    def _make_lines(self, tmp_path):
        lines = []
        for at, (name, height) in enumerate((("one", 50), ("two", 48),
                                             ("dots", 10), ("tall", 110))):
            png = tmp_path / (name + ".png")
            png.write_bytes(b"\x89PNG" + name.encode())
            lines.append(("00:00:0%d,000" % at, "00:00:0%d,500" % at,
                          str(png), height))
        return lines

    def test_short_array_reasks_alone_and_tall_line_reasks_with_meter(
            self, monkeypatch, tmp_path):
        lines = self._make_lines(tmp_path)
        llm = llmclient.Llm("openai", "http://127.0.0.1:8082/v1", "vision-12b",
                            said={"vision": True})
        calls = []

        def fake_post(model, system, prompt, tried, pictures=(), **kwargs):
            calls.append((len(pictures), kwargs.get("numbered"),
                          kwargs.get("options"), kwargs.get("max_tokens")))
            if len(pictures) == 3:
                return json.dumps(["Bonjour.", "- Oui ?\n- Non.", "Line one."]), 10, 300
            return "Line one.\nLine two.", 6, 100

        monkeypatch.setattr(llmclient, "_post_openai", fake_post)
        monkeypatch.setattr(subtitleocr.toolcapture, "watched",
                            lambda *a, **k: None)
        usage = subtitleocr.Usage()
        srt = subtitleocr._read_with(llm, lines, FRENCH, str(tmp_path),
                                     lambda line: None, usage)
        assert srt
        assert subtitletracks.cue_texts(srt) == [
            "Bonjour.", "- Oui ?\n- Non.", "Line one.\nLine two."]
        assert len(calls) == 2
        assert calls[0][0] == 3 and calls[0][1] is True and calls[0][3] == 600
        assert calls[1][0] == 1 and calls[1][1] is True and calls[1][3] == 200
        assert (usage.requests, usage.tally.read, usage.tally.tokens) == (2, 400, 16)
        assert "16 token(s) written" in usage.line()
        assert "400 read, 416 in all" in usage.line()

    def test_responses_api_token_names_are_reported(self):
        assert llmclient._reported_tokens(
            {"input_tokens": 9, "output_tokens": 3}) == (3, 9)
        assert llmclient._reported_tokens(None) == (None, None)
        assert llmclient._reported_tokens({}) == (None, None)

    def test_ladder_falls_back_to_next_vision_model_and_skips_non_vision(
            self, monkeypatch, tmp_path):
        lines = self._make_lines(tmp_path)
        monkeypatch.setattr(subtitleocr, "_pictures", lambda image, out: lines)
        monkeypatch.setattr(subtitleocr.toolcapture, "watched",
                            lambda *a, **k: None)

        down = llmclient.Llm("openai", "http://127.0.0.1:8081/v1", "down",
                             said={"vision": True}, name="down")
        text_only = llmclient.Llm("openai", "http://127.0.0.1:8082/v1", "text",
                                  said={"vision": False}, name="text")
        good = llmclient.Llm("openai", "http://127.0.0.1:8083/v1", "good",
                             said={"vision": True}, name="good")

        @contextlib.contextmanager
        def fail_serving(log, **_kwargs):
            raise llmclient.LlmError("down")
            yield down  # pragma: no cover

        @contextlib.contextmanager
        def serve_as(target):
            yield target

        monkeypatch.setattr(down, "serving", fail_serving)
        monkeypatch.setattr(text_only, "serving",
                            lambda log, **_kwargs: serve_as(text_only))
        monkeypatch.setattr(good, "serving",
                            lambda log, **_kwargs: serve_as(good))
        monkeypatch.setattr(
            good, "ask_pictures",
            lambda *a, **k: ["Bonjour.", "- Oui ?\n- Non.", "Line one.\nLine two."])

        logs = []
        srt = subtitleocr._read_by_vision(
            "t.sup", FRENCH, str(tmp_path), {}, logs.append,
            models=[down, text_only, good])
        assert srt
        assert subtitletracks.cue_texts(srt) == [
            "Bonjour.", "- Oui ?\n- Non.", "Line one.\nLine two."]
        assert any("down could not start" in line for line in logs)

    def test_recognise_falls_back_to_tesseract_when_vision_fails(
            self, monkeypatch, tmp_path):
        image = tmp_path / "track3.sup"
        image.write_bytes(b"PG")
        srt = tmp_path / "Film.fr.srt"
        models_file = tmp_path / "models.toml"
        models_file.write_text(
            '[models.v]\nurl = "http://127.0.0.1:8082/v1"\n'
            'model = "gemma3-4b"\ntraits = { vision = true }\n',
            encoding="utf-8")

        monkeypatch.setattr(subtitleocr, "_read_by_vision",
                            lambda *a, **k: "")
        monkeypatch.setattr(subtitleocr, "_ready", lambda *a, **k: True)

        def fake_watched(cmd, **kwargs):
            out_dir = next(arg.split(":", 1)[1] for arg in cmd
                           if arg.startswith("--output-folder:"))
            out_srt = os.path.join(out_dir, "read.srt")
            with open(out_srt, "w", encoding="utf-8") as handle:
                handle.write(_CUES)
            return type("Done", (), {
                "returncode": 0,
                "stdout": json.dumps({"files": [{"success": True, "output": out_srt}]}),
            })()

        monkeypatch.setattr(subtitleocr.toolcapture, "watched", fake_watched)
        logs = []
        engine = subtitleocr.recognise(
            str(image), FRENCH, str(srt), logs.append,
            environ={"llmModels": str(models_file)})
        assert engine == "tesseract"
        assert srt.read_text(encoding="utf-8") == _CUES
        assert any("vision OCR read nothing" in line for line in logs)


@pytest.mark.pure
class TestTheVisionSettings:
    @pytest.fixture()
    def asked(self, monkeypatch, tmp_path):
        llmroster.forget()
        llmtraits.forget()
        monkeypatch.setattr(llmtraits, "_server_json", lambda *a, **k: None)
        png = tmp_path / "1.png"
        png.write_bytes(b"\x89PNGone")
        calls = []
        monkeypatch.setattr(subtitleocr, "_pictures", lambda image, out: [
            ("00:00:01,000", "00:00:02,000", str(png), 50)])
        monkeypatch.setattr(llmclient, "_listing",
                            lambda url, key="": {"listed": 4096, "vision-35b": 4096})
        monkeypatch.setattr(llmclient, "_refused", lambda url: False)

        def fake_post(llm, system, prompt, tried, pictures=(), **kwargs):
            calls.append((llm.model, kwargs.get("options")))
            return "Oui", 2, 10

        monkeypatch.setattr(llmclient, "_post_openai", fake_post)
        monkeypatch.setattr(subtitleocr.toolcapture, "watched",
                            lambda *a, **k: None)
        return calls

    def _read(self, tmp_path, **settings):
        environ = _vision(tmp_path, "http://127.0.0.1:8082/v1", **settings)
        logs = []
        written = subtitleocr._read_by_vision("t.sup", FRENCH, str(tmp_path),
                                              environ, logs.append)
        return written, logs

    def test_the_model_named_is_the_one_asked_for(self, asked, tmp_path):
        self._read(tmp_path, model="vision-35b")
        assert len(asked) == 1
        assert asked[0][0] == "vision-35b"

    def test_without_one_the_server_s_first(self, asked, tmp_path):
        written, logs = self._read(tmp_path)
        assert written
        assert logs == []
        assert len(asked) == 1
        assert asked[0][0] == "listed"

    def test_the_options_go_with_every_request(self, asked, tmp_path):
        self._read(tmp_path, options={"top_k": 1})
        assert len(asked) == 1
        assert asked[0][0] == "listed"
        assert asked[0][1]["top_k"] == 1
        assert asked[0][1]["temperature"] == 0

    def test_options_that_are_not_an_object_ask_nothing(self, asked,
                                                         tmp_path,
                                                         monkeypatch):
        monkeypatch.setattr(subtitleocr, "_said", set())
        written, logs = self._read(tmp_path, options=[1])
        assert written == "" and asked == []
        assert len(logs) == 1
        assert "vision's options" in logs[0]
        assert "the vision model is not asked" in logs[0]
