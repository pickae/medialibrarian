"""Asking a model through an agent harness: a program that wraps the model in
tools - web search and fetch, a shell - and runs the loop of looking things up
that a bare chat server cannot.

A local server behind the openai protocol only ever sees the transcript, so it
cannot check how a speaker's name is spelled. The same model asked through a
harness can. Which harnesses a user has is the setting
:data:`HARNESS_VARIABLE`, a comma list of :class:`Harness` names, the preferred
first; the model the harness asks is :data:`MODEL_VARIABLE`, named as that
harness names models.

Every request is one throwaway session. A harness keeps its sessions where its
own interface lists them, so each one is removed again the moment its answer
is read - and made hidden when it starts, for the case that the run is killed
before it can be removed - or a user opening the harness for real would find
it buried in hundreds of chapter or cover requests.

What the session did - the prompt, the tools it called, the pages it fetched,
the answer - is what a run's author needs to judge it, so each session is
exported into the run's own folder under `logs/llm/` (:func:`session_dir`)
just before it is deleted.
"""

from __future__ import annotations

import enum
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

from medialib import commands
from medialib.lib import errorlog, runlog

__all__ = ["HARNESS_VARIABLE", "MODEL_VARIABLE", "Harness", "HarnessError",
           "HarnessUnavailable", "parse", "installed", "ask"]

HARNESS_VARIABLE = "llmHarness"
MODEL_VARIABLE = "llmHarnessModel"

# What the sessions are called, so a leftover one is recognisable: each use
# of the model names its own (:func:`session_title`).
SESSION_TITLE = "medialib chapter request"


def session_title(purpose: str) -> str:
    """What a session asked for ``purpose`` - "chapter", "cover" - is called."""
    return "medialib %s request" % (purpose or "model")

# What the harness is told to do with the prompt file. The file holds the
# whole request - system prompt, instructions, transcript - because a
# transcript is longer than one argument can be.
_MESSAGE = ("The attached file is your complete task: read it and do exactly "
            "what it says. You may look things up online with your tools where "
            "its instructions call for it. Reply with the answer the file asks "
            "for, in the form it asks for, and nothing else.")


class HarnessError(Exception):
    """The harness could not be run, or did not answer."""


class HarnessUnavailable(HarnessError):
    """The model's server refused the session: nothing listens there, or its
    queue is full."""


class Harness(enum.Enum):
    """The harnesses a model can be asked through. The value is the name a
    setting gives it, and the program that is started."""

    OPENCODE = "opencode"

    @property
    def program(self) -> str:
        return self.value


def parse(text: str) -> tuple[Harness, ...]:
    """The harnesses a setting lists, in its order. HarnessError on a name that
    is none of :class:`Harness`."""
    found: list[Harness] = []
    for name in (text or "").split(","):
        name = name.strip().lower()
        if not name:
            continue
        try:
            harness = Harness(name)
        except ValueError:
            raise HarnessError("%s names %r; it takes %s" % (
                HARNESS_VARIABLE, name,
                ", ".join(each.value for each in Harness))) from None
        if harness not in found:
            found.append(harness)
    return tuple(found)


def installed(harnesses: tuple[Harness, ...]) -> Harness | None:
    """The first of ``harnesses`` whose program is on the PATH."""
    return next((each for each in harnesses if shutil.which(each.program)), None)


def ask(harness: Harness, model: str, system: str, prompt: str,
        timeout: float, title: str = SESSION_TITLE) -> str:
    """The text ``harness`` answers to ``system`` and ``prompt`` with ``model``,
    in a session called ``title`` that is gone again when this returns.
    HarnessError when it cannot be run, fails, or says nothing."""
    work = tempfile.mkdtemp(prefix="llmharness.")
    session = ""
    try:
        with open(os.path.join(work, "task.md"), "w", encoding="utf-8") as handle:
            handle.write("%s\n\n---\n\n%s\n" % (system, prompt))
        try:
            if harness is Harness.OPENCODE:
                proc = _opencode(work, model, timeout, title)
                session = _opencode_session(proc.stdout)
                if proc.returncode != 0:
                    _raise_session_error(proc.stdout)
                    raise HarnessError("opencode: %s" % (
                        proc.stderr.decode("utf-8", "replace").strip()[-300:]
                        or "exited %d" % proc.returncode))
                return _opencode_answer(proc.stdout)
            raise HarnessError("no way to run %s" % harness.value)
        except subprocess.TimeoutExpired as error:
            session = _opencode_session(error.stdout or b"") or session
            raise HarnessError("%s did not answer within %d seconds"
                               % (harness.program, timeout)) from error
        except OSError as error:
            raise HarnessError("%s could not be started: %s"
                               % (harness.program, error)) from error
    finally:
        if session:
            _keep(harness, work, session)
            _forget(harness, work, session, title)
        shutil.rmtree(work, ignore_errors=True)


def _opencode(work: str, model: str, timeout: float,
              title: str = SESSION_TITLE):
    """One ``opencode run`` in ``work``, as the finished process.

    ``--auto`` because a run with nobody to ask turns every permission request
    down, and web fetch is one of them. The session is created archived and
    titled, so a run that dies before :func:`_forget` leaves one that is out
    of the way and can be told apart."""
    argv = ["opencode", "run", "--format", "json", "--archived",
            "--title", title, "--auto",
            "--file", os.path.join(work, "task.md")]
    if model:
        argv += ["--model", model]
    argv.append(_MESSAGE)
    return subprocess.run(argv, cwd=work, capture_output=True, timeout=timeout,
                          stdin=subprocess.DEVNULL)


def _events(output: bytes):
    for line in output.decode("utf-8", "replace").splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict):
            yield event


def _opencode_session(output: bytes) -> str:
    return next((str(event["sessionID"]) for event in _events(output)
                 if event.get("sessionID")), "")


# What a session ends on when its model's server refused it: nothing listens
# there, or its queue is full.
_UNAVAILABLE = re.compile(r"ECONNREFUSED|connection refused|queue is full"
                          r"|rate[-_ ]limit|server_overloaded", re.IGNORECASE)


def _session_error(event: dict):
    """The error a session's event carries: its own, or the message's."""
    if event.get("type") == "error":
        return event.get("error")
    for holder in (event, event.get("part"), event.get("info")):
        if isinstance(holder, dict) and isinstance(holder.get("error"), dict):
            return holder["error"]
    return None


def _raise_session_error(output: bytes) -> None:
    """The last error a failed session's events name, raised -
    HarnessUnavailable where its server refused it - and nothing where they
    name none."""
    found = None
    for event in _events(output):
        error = _session_error(event)
        if error is not None:
            found = error
    if found is None:
        return
    text = json.dumps(found, ensure_ascii=False)
    status = found.get("status") if isinstance(found, dict) else None
    kind = HarnessUnavailable if status == 429 or _UNAVAILABLE.search(text) \
        else HarnessError
    raise kind("opencode: %s" % text[:300])


def _opencode_answer(output: bytes) -> str:
    """The text of the last message that has any: the messages before it are
    the harness thinking aloud between its tool calls."""
    last = ""
    parts: list[str] = []
    for event in _events(output):
        if event.get("type") == "error":
            _raise_session_error(json.dumps(event).encode("utf-8"))
            raise HarnessError("opencode: %s" % json.dumps(event)[:300])
        part = event.get("part")
        if event.get("type") != "text" or not isinstance(part, dict):
            continue
        message = str(part.get("messageID"))
        if message != last:
            last, parts = message, []
        parts.append(str(part.get("text") or ""))
    text = "".join(parts).strip()
    if not text:
        raise HarnessError("opencode answered with no text")
    return text


def session_dir() -> str:
    """This run's folder for what its model sessions did: `logs/llm/`, then
    the name the run's error and info logs share, without the suffix."""
    name = os.path.splitext(os.path.basename(errorlog.current_file()))[0]
    return os.path.join(commands.script_dir(), "logs", "llm", name)


def _keep(harness: Harness, work: str, session: str) -> None:
    """Export the session into :func:`session_dir`, as the harness writes it.
    Never raises: the log is a copy, and losing it is no reason to lose the
    answer."""
    try:
        done = subprocess.run([harness.program, "session", "export", session],
                              cwd=work, capture_output=True, timeout=120,
                              stdin=subprocess.DEVNULL)
        if done.returncode != 0 or not done.stdout.strip():
            raise OSError("exit %d" % done.returncode)
        folder = session_dir()
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, "%s-%s-%s.json" % (
            time.strftime("%H%M%S"), harness.program, session))
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(errorlog.redact(done.stdout.decode("utf-8", "replace")))
        runlog.detail("kept the %s session in %s" % (harness.program, path))
    except (OSError, subprocess.TimeoutExpired) as error:
        runlog.warn("could not keep the %s session %s: %s"
                    % (harness.program, session, error))


def _forget(harness: Harness, work: str, session: str,
            title: str = SESSION_TITLE) -> None:
    """Delete the session. A failure is said, once, not raised: the answer is
    already in hand, and the session is hidden."""
    try:
        done = subprocess.run([harness.program, "session", "delete", session],
                              cwd=work, capture_output=True, timeout=60,
                              stdin=subprocess.DEVNULL)
        failed = done.returncode != 0
    except (OSError, subprocess.TimeoutExpired):
        failed = True
    if failed:
        sys.stderr.write("  Could not delete the %s session %s (titled %r); it is "
                         "hidden, and can be removed with `%s session delete`.\n"
                         % (harness.program, session, title,
                            harness.program))
