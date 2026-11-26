"""The TheTVDB client: built like :mod:`medialib.lib.tmdbclient` - curl, one
request at a time at a polite pace, the secrets handed to curl on stdin, and a
run's answers kept by the question.

TheTVDB's v4 API takes a project key (the ``tvdbApiKey`` setting) and, for a
key a subscriber's account supports, that subscriber's PIN (``tvdbPin``),
exchanged once at ``POST /login`` for a bearer token the service says is good
for a month. Without a key nothing is asked, and ingest-series says so.

What the answers MEAN is :mod:`medialib.lib.seriesidentify`.
"""

from __future__ import annotations

import json
import os
import subprocess
import threading

from medialib.lib import politepacing

__all__ = ["BASE", "available", "remember_answers", "reset", "search_series",
           "series_extended", "series_episodes", "SEASON_TYPES"]

BASE = "https://api4.thetvdb.com/v4"

# The episode orders a series can be read in, the way the API names them -
# the aired order first, which is the one a show is usually catalogued by.
SEASON_TYPES = ("default", "dvd", "absolute")

# How many pages of one series' episodes are read at most: a page holds a few
# hundred, and a daily show runs to thousands.
MAX_PAGES = 40

# TheTVDB publishes no rate; this is TMDb's pace, which is polite to anyone.
_PACER = politepacing.Pacer(0.1)

_ANSWERS: dict | None = None
_LOCK = threading.Lock()
_TOKEN: list[str] = []


def available() -> bool:
    """Whether there is a key to ask TheTVDB with."""
    return bool(os.environ.get("tvdbApiKey", "").strip())


def remember_answers(on: bool = True) -> None:
    """Keep every answer from here on, for the rest of the run - or, with
    ``on`` False, stop and forget them."""
    global _ANSWERS
    with _LOCK:
        _ANSWERS = {} if on else None


def reset() -> None:
    """Forget the token and the pace: for a test."""
    with _LOCK:
        _TOKEN.clear()
    _PACER.reset()


def _config_value(text: str) -> str:
    for character, escape in (("\\", "\\\\"), ('"', '\\"'), ("\t", "\\t"),
                              ("\n", "\\n"), ("\r", "\\r"), ("\v", "\\v")):
        text = text.replace(character, escape)
    return '"' + text + '"'


def _curl(lines: list[str]) -> str | None:
    """One curl call, configured on stdin; the body, or None when it failed."""
    _PACER.wait()
    lines = lines + ["fail", "silent", "show-error", "max-time = 30"]
    try:
        proc = subprocess.run(["curl", "--config", "-"],
                              input="\n".join(lines).encode("utf-8"),
                              stdout=subprocess.PIPE,
                              stderr=subprocess.DEVNULL)
    except OSError:
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.decode("utf-8", "replace")


def _token() -> str:
    """The bearer token, asked for once per run; "" without a key or an
    answer."""
    with _LOCK:
        if _TOKEN:
            return _TOKEN[0]
    key = os.environ.get("tvdbApiKey", "").strip()
    if not key:
        return ""
    body = {"apikey": key}
    pin = os.environ.get("tvdbPin", "").strip()
    if pin:
        body["pin"] = pin
    answer = _curl(["url = " + _config_value(BASE + "/login"),
                    'request = "POST"',
                    'header = "Content-Type: application/json"',
                    "data = " + _config_value(json.dumps(body))])
    token = ""
    try:
        token = (json.loads(answer or "{}").get("data") or {}).get("token", "")
    except (ValueError, AttributeError):
        token = ""
    if token:
        with _LOCK:
            _TOKEN[:] = [token]
    return token


def _get(path: str, params=()) -> dict:
    """``GET`` one path, as a dict - empty when there was no answer."""
    question = (path, tuple(tuple(pair) for pair in params))
    with _LOCK:
        if _ANSWERS is not None and question in _ANSWERS:
            return _ANSWERS[question]
    token = _token()
    if not token:
        return {}
    lines = ["url = " + _config_value(BASE + path), "get",
             "header = " + _config_value("Authorization: Bearer " + token)]
    for key, value in params:
        lines.append("data-urlencode = "
                     + _config_value("{}={}".format(key, value)))
    body = _curl(lines)
    try:
        parsed = json.loads(body) if body else {}
    except ValueError:
        parsed = {}
    answer = parsed if isinstance(parsed, dict) else {}
    if body is not None:
        with _LOCK:
            if _ANSWERS is not None:
                _ANSWERS[question] = answer
    return answer


def search_series(title: str, year: str = "") -> list[dict]:
    """The series TheTVDB's search offers for a title, in its order."""
    params = [("query", title), ("type", "series")]
    if year:
        params.append(("year", year))
    rows = _get("/search", params).get("data")
    return [row for row in rows if isinstance(row, dict)] \
        if isinstance(rows, list) else []


def series_extended(series_id: str) -> dict:
    """One series' record, with its remote ids and original language."""
    data = _get("/series/%s/extended" % series_id, [("short", "true")]) \
        .get("data")
    return data if isinstance(data, dict) else {}


def series_episodes(series_id: str, season_type: str = "default") -> list[dict]:
    """Every episode of a series in one order, page by page."""
    found: list[dict] = []
    for page in range(MAX_PAGES):
        answer = _get("/series/%s/episodes/%s" % (series_id, season_type),
                      [("page", str(page))])
        data = answer.get("data")
        rows = data.get("episodes") if isinstance(data, dict) else None
        if not isinstance(rows, list) or not rows:
            break
        found.extend(row for row in rows if isinstance(row, dict))
        links = answer.get("links")
        if isinstance(links, dict) and not links.get("next"):
            break
    return found
