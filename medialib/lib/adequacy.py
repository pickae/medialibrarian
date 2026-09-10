"""The one verdict three media types are judged by: starved, adequate, generous.

Video, audio and images each have their own model of what a file NEEDS, and each
of those lives in its own module. What is here is the last step they share:
reading a measured figure against the requirement that model produced, and naming
what it is.

The three words mean the same thing whichever type is being judged, which is the
point of them living here:

* **starved** - below what it would need. Re-encoding cannot improve it, only
  spend another generation of loss on it, so a converter's default is to leave it
  alone.
* **adequate** - at or above the requirement, but without a whole requirement's
  worth to spare. Worth converting only when the output codec is efficient enough
  to stay adequate inside a smaller file.
* **generous** - at :data:`GENEROUS_FACTOR` times the requirement or above. There
  is a whole adequate encode's worth of spare in it, which is the same reading a
  caller demanding a 50% saving applies.
* **unknown** - one of the two figures is missing. Never a guess: a caller decides
  for itself what to do with a file it could not measure, and every one of them in
  this repo converts it rather than skipping it.

The models keep their own tables and their own arithmetic. Nothing type-specific
belongs here.
"""

from __future__ import annotations

from medialib.lib.formatting import awk_number

__all__ = [
    "STARVED",
    "ADEQUATE",
    "GENEROUS",
    "UNKNOWN",
    "VERDICTS",
    "GENEROUS_FACTOR",
    "verdict",
    "at_least_adequate",
    "is_starved",
]

STARVED = "starved"
ADEQUATE = "adequate"
GENEROUS = "generous"

# The answer when either figure is missing. Distinct from every real verdict, so
# nothing unmeasured is silently counted as small - which is the mistake that
# would make a converter skip a file it has not read.
UNKNOWN = "unknown"

# The three real verdicts, worst first. In order, so a report can sort by them and
# a caller can compare two.
VERDICTS = (STARVED, ADEQUATE, GENEROUS)

# Where "generous" starts: twice adequate is a whole adequate encode's worth of
# spare.
GENEROUS_FACTOR = 2


def verdict(measured: object, adequate: object,
            generous_factor: object = GENEROUS_FACTOR) -> str:
    """What ``measured`` IS for a requirement of ``adequate``.

    Both are read the way awk reads a number, because they arrive as text from
    probes and tables alike, and a figure that is not a positive number - missing,
    zero, unreadable - makes the verdict :data:`UNKNOWN` rather than an extreme.

    This is the verdict on ONE file against ONE requirement. Whether a conversion
    follows is a second question, asked by the caller against the OUTPUT's
    requirement.
    """
    have = awk_number(measured)
    need = awk_number(adequate)
    factor = awk_number(generous_factor)
    if need <= 0 or have <= 0:
        return UNKNOWN
    if have < need:
        return STARVED
    if factor > 0 and have >= need * factor:
        return GENEROUS
    return ADEQUATE


def at_least_adequate(name: str) -> str:
    """The verdict, floored at :data:`ADEQUATE`.

    For a source whose format makes "starved" impossible whatever its size says -
    a LOSSLESS one, which holds every pixel or sample it was given. What the
    arithmetic can still say about such a file is whether re-encoding would save
    anything, which is the difference between adequate and generous.
    """
    return ADEQUATE if name == STARVED else name


def is_starved(name: str) -> bool:
    """Whether a verdict is the one that stops a conversion.

    Asked rather than compared, because :data:`UNKNOWN` must not read as starved:
    a file nobody could measure is converted rather than skipped.
    """
    return name == STARVED
