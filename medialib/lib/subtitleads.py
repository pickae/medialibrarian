"""Advertising taken out of a subtitle, by subcleaner - and by a language model
when subcleaner has no word list for the language or is not installed.

A subtitle from a public catalogue often carries cues of its own that are not
the film's: who ripped or synced it, the site it came from, an invitation to
become a member - usually just before the film starts or after it ends.
subcleaner (https://github.com/KBlixt/subcleaner) knows them by the words they
use, per language, and by where they sit, and removes the cues it is sure of.

Where subcleaner has a profile for a language it always runs first and the
model is never asked. When subcleaner refuses the language for want of a word
list, or is absent, :mod:`medialib.lib.aisubtitleads` may ask a language model
to spot advert cues in the first and last 20 cues. Both are optional: without
either a subtitle keeps whatever it came with.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable

from medialib.lib import aisubtitleads, toolcapture, tooldeps

TOOL = "subcleaner"

# What subcleaner says of a language it has no word list for, when its
# configuration asks for one (it does, unless told otherwise): it then leaves
# the subtitle as it was.
_NO_PROFILE = "have no regex profile"

# The languages already said to go uncleaned, so a run says it once each.
_said_unprofiled: set = set()


def available() -> bool:
    return tooldeps.tool_present(TOOL)


def _cues(srt: str) -> int | None:
    try:
        with open(srt, encoding="utf-8", errors="replace") as text:
            return sum(1 for line in text if "-->" in line)
    except OSError:
        return None


def clean(srt: str, language_code: str, log: Callable[[str], None], *,
          meter_file: str = "") -> int:
    """Take the advertising out of ``srt`` in place, and return how many cues
    went: 0 when there was none, or neither subcleaner nor a language model
    cleaned it.

    Deterministic subcleaner always runs first when available; the language
    model (:mod:`medialib.lib.aisubtitleads`) is asked only when subcleaner is
    not installed or reports that it has no profile for ``language_code``. A
    language subcleaner refuses to clean and no model handles is said once a
    run, through ``log``, which puts the language in front."""
    if not available():
        return aisubtitleads.clean(srt, language_code, log,
                                   meter_file=meter_file)
    before = _cues(srt)
    try:
        done = toolcapture.watched(
            [TOOL, os.path.abspath(srt), "--language", language_code,
             "--silent", "--no-log"], announce=False,
            skipped="the subtitle was not cleaned of adverts",
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, errors="replace")
    except OSError:
        return aisubtitleads.clean(srt, language_code, log,
                                   meter_file=meter_file)
    if _NO_PROFILE in (done.stdout or ""):
        result = aisubtitleads.clean_file(srt, language_code, log,
                                          meter_file=meter_file)
        if result.asked:
            return result.removed
        if language_code not in _said_unprofiled:
            _said_unprofiled.add(language_code)
            log("WARNING: not cleaned of adverts, subcleaner has no word list "
                "for this language - set require_language_profile = false in its "
                "subcleaner.conf to clean it by the words every language shares")
    after = _cues(srt)
    if before is None or after is None:
        return 0
    return max(0, before - after)
