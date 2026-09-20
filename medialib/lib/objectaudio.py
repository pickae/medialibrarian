"""Which audio tracks are object-based, and which surround track of a language survives.

Both decisions are pure readings of mediainfo fields, centralised so every
script that needs them shares one set of rules instead of each carrying its
own. """

import re

__all__ = ["audio_object_flag", "audio_ex_flag", "audio_ladder_score"]

# The ladder the improved-copy remux deduplicates on, per language: one winner
# per language, the best available tier.
#
# E-AC-3 beats AC-3 on the same bed, an object (Atmos) variant beats the plain
# one, and a 7.1 bed beats 5.1 - EXCEPT that an E-AC-3 5.1 with objects sits
# above a plain AC-3 7.1, because the objects are the only thing the narrower
# track carries that no other tier does.
#
# Dolby Surround EX is a bed of its own between those two widths: a 5.1 track
# carrying a matrixed back surround, so it is worth more than the 5.1 it is
# encoded as and less than a discrete 7.1 - and less than any Atmos, whose
# objects it has none of.
#
# (channels, has objects, is EX) -> score, for E-AC-3.
_EAC3_LADDER = {
    ("8", True, False): 100,    # Dolby Digital Plus Atmos 7.1
    ("8", False, False): 90,
    ("6", True, False): 80,     # Dolby Digital Plus Atmos 5.1
    ("6", False, True): 65,     # Dolby Digital Plus 5.1 EX
    ("6", False, False): 60,
}

# AC-3's rungs, which do NOT consult the object flag: plain AC-3 carries no JOC,
# so a flag on one says nothing and must not move it up or off the ladder.
#
# (channels, is EX) -> score.
_AC3_LADDER = {
    ("8", False): 70,
    ("6", True): 62,            # Dolby Digital 5.1 EX
    ("6", False): 50,
}

# The digits [0-9] matches, which is not what str.isdigit() accepts - that one
# also takes superscripts and other numerals the pattern would refuse.
_DIGITS = re.compile(r"[0-9]+")

# What a track's own name says, when mediainfo parsed no metadata at all.
_NAME_MARKERS = ("atmos", "dts:x", "dtsx")

# The same last resort for Surround EX. Anchored on the word the "EX" hangs off
# - a bare "ex" is a syllable of too many other words to read as a format.
_EX_NAME = re.compile(r"(?:^|[^a-z0-9])(?:dd|ac-?3|e-?ac-?3|digital|surround"
                      r"|5\.1|6\.1)[ \-_]?ex(?![a-z0-9])")


def audio_object_flag(commercial: str, objects: str, name: str) -> str:
    """``audioObjectFlag``: "1" for object-based metadata, "" otherwise.

    Three sources, in order of how much they know. The commercial name is what
    mediainfo writes for the JOC variants ("... with Dolby Atmos"), the object
    count is what the JOC header itself states, and the track's own name is the
    last resort - a track that advertises itself although mediainfo read no
    metadata.

    A plain "DTS-HD Master Audio" or "Dolby TrueHD" is NOT object audio: only the
    JOC variants are.
    """
    if "with dolby atmos" in (commercial or "").lower():
        return "1"
    if _DIGITS.fullmatch(objects or "") and int(objects, 10) > 0:
        return "1"
    lowered = (name or "").lower()
    if any(marker in lowered for marker in _NAME_MARKERS):
        return "1"
    return ""


def audio_ex_flag(mode: str, name: str) -> str:
    """``audioExFlag``: "1" for a Dolby Surround EX track, "" otherwise.

    Two sources, the way the object flag has three. The settings mode is what
    mediainfo writes for the flag in the AC-3 header ("Dolby Surround EX"), and
    the track's own name is the last resort - a track that advertises itself
    although mediainfo read no mode at all.

    A plain "Dolby Surround" is NOT EX: that mode is the stereo downmix flag,
    which the commentary tracks of a film all carry.
    """
    if "surround ex" in (mode or "").lower():
        return "1"
    return "1" if _EX_NAME.search((name or "").lower()) else ""


def audio_ladder_score(codec_id: str, channels: str, object_flag: str,
                       ex_flag: str = "") -> str:
    """``audioLadderScore``: this track's rung, or "" for one not on the ladder.

    The ladder deduplicates the LOSSY compatibility tracks only. Lossless tracks
    are not on it - they are consumed into their opus bed before it runs - and
    neither is opus. Everything else gets nothing back.
    """
    codec = (codec_id or "").upper()
    # EX is a 5.1 bed with a matrixed sixth channel, so the flag is only read on
    # that bed - and it says nothing next to objects, which outrank it anyway.
    objects = object_flag == "1"
    ex = ex_flag == "1" and not objects and channels == "6"
    if codec.startswith("A_EAC3"):
        score = _EAC3_LADDER.get((channels, objects, ex))
    elif codec.startswith("A_AC3"):
        score = _AC3_LADDER.get((channels, ex))
    else:
        return ""
    return str(score) if score is not None else ""
