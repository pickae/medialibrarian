"""The TMDb client: one request at a time, spaced to the service's own limit,
with the key handed to curl on stdin and a run's answers kept by the
question.

Everything that asks TMDb anything goes through :func:`_curl`, so the rate,
the secret's handling and the answer cache are settled in one place; what
the answers MEAN is :mod:`medialib.lib.tmdbidentify`.
"""

import json
import subprocess
import threading

from medialib.lib import politepacing

# The TMDb endpoint everything is relative to.
_BASE = "https://api.themoviedb.org/3"


# What TMDb asks of a caller: about ten requests a second. One film costs
# several - the search, an alternative-titles call per candidate, and the
# external-ids call - so a library goes through them far faster than a person
# would, and the limit is worth keeping to rather than finding out where the
# service draws it.
MAX_REQUESTS_PER_SECOND = 10.0

# Every request to TMDb, spaced to that limit. No jitter: the limit is the
# service's own, and spacing each call evenly at it IS the rate.
_PACER = politepacing.Pacer(1.0 / MAX_REQUESTS_PER_SECOND)


# The answers of a run that asks TMDb the same questions twice - a full ingest
# names its films ahead, on a thread of its own, and again at the end - kept by
# the question, so the second asking costs nothing. None while nothing asked for
# them to be kept, which is every other command.
_ANSWERS: dict | None = None
_ANSWERS_LOCK = threading.Lock()


def remember_answers(on: bool = True) -> None:
    """Keep every answer TMDb gives from here on, for the rest of the run -
    or, with ``on`` False, stop and forget them. An answer that never came is
    not kept, so the question is asked again."""
    global _ANSWERS
    with _ANSWERS_LOCK:
        _ANSWERS = {} if on else None


def reset_rate_limit() -> None:
    """Forget when the last request went out, so the next one goes immediately.
    For a test that would otherwise pay the interval."""
    _PACER.reset()


# The escapes a curl config file's double-quoted value has, and all it has.
_CONFIG_ESCAPES = (("\\", "\\\\"), ('"', '\\"'), ("\t", "\\t"),
                   ("\n", "\\n"), ("\r", "\\r"), ("\v", "\\v"))


def _config_value(text: str) -> str:
    """One value as a curl config file spells it: double-quoted, with the six
    escapes that syntax has applied."""
    for character, escape in _CONFIG_ESCAPES:
        text = text.replace(character, escape)
    return '"' + text + '"'


def _curl(url: str, params) -> str | None:
    """One ``curl -fsSG`` call, the module's only way out to the network.

    ``params`` is an ordered (key, value) pair list, each becoming a
    ``--data-urlencode key=value``. Returns the body on success, or ``None``
    when curl fails (the ``-f`` makes an HTTP error a non-zero exit, which is
    how the caller tells "no answer" from an answer).

    Paced to :data:`MAX_REQUESTS_PER_SECOND` before it goes out, which is where
    the whole module's traffic is metered: every question reaching TMDb is one
    of these calls.

    Handed to curl through ``--config -`` rather than as argv, because one of
    those params is the API key and argv is not private: /proc/<pid>/cmdline is
    readable by every account on the machine, and a run over a library opens
    that window once per candidate folder. On stdin it reaches curl and nothing
    else.

    Given up on after thirty seconds, as the other catalogue lookups are: a
    stalled connection would otherwise hold the whole run, and a curl that
    gave up is no answer like any other failure.
    """
    question = (url, tuple(tuple(pair) for pair in params))
    with _ANSWERS_LOCK:
        if _ANSWERS is not None and question in _ANSWERS:
            return _ANSWERS[question]
    _PACER.wait()
    lines = ["url = " + _config_value(url), "get", "fail", "silent",
             "show-error", "max-time = 30"]
    for key, value in params:
        lines.append("data-urlencode = "
                     + _config_value("{}={}".format(key, value)))
    proc = subprocess.run(["curl", "--config", "-"],
                          input="\n".join(lines).encode("utf-8"),
                          stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    if proc.returncode != 0:
        return None
    answer = proc.stdout.decode("utf-8", "replace")
    with _ANSWERS_LOCK:
        if _ANSWERS is not None:
            _ANSWERS[question] = answer
    return answer


def _as_json(text: str | None):
    """A TMDb body as a dict, or an empty dict when it is not valid JSON.

    A body that is valid JSON but not an object reads as "nothing" too: an
    empty dict.
    """
    if not text:
        return {}
    try:
        parsed = json.loads(text)
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}
