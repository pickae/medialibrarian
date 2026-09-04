"""Shrinking a shared affix so removing it cannot cut a word or a number in half.

When sibling names share leading or trailing text, that text is stripped from all
of them - but only as far as a boundary between character classes. "alpha - show"
and "beta - show" share the suffix "a - show", and removing all of it would leave
"Alph" and "Bet". Retreating to " - show" keeps the words whole. Likewise a shared
leading "1" over "11 title" and "12 title" retreats to nothing, because "11" and
"12" must not become "1" and "2".

Whole runs may still go, and a mixed token may be cut where its class changes:
over "a16z" and "a17z" the prefix "a1" retreats to "a" - the "1" belongs to the
number - while the suffix "z" sits at a digit-to-letter boundary and is removable,
so what survives is "16" and "17".
"""

from collections.abc import Sequence

__all__ = ["align_affix_to_run_boundary", "affix_char_class"]

# Everything bash's ``_affixCharClass`` calls "other": the space, the control
# characters, and every ASCII punctuation mark. Anything not here and not an ASCII
# digit is a letter - which deliberately includes accented and other non-ASCII
# letters, and, less deliberately, every non-ASCII symbol. An en dash is a letter
# to this function. That is the bash behaviour and the separator vocabulary the
# name cleaners run over contains one, so it is reproduced rather than corrected.
_OTHER = (
    frozenset(" ")
    | frozenset(chr(c) for c in range(0x20))
    | frozenset({chr(0x7F)})
    | frozenset(chr(c) for c in range(0x80, 0xA0))
    | frozenset("!\"#$%&'()*+,-./:;<=>?@[\\]^_`{|}~")
)

_ASCII_LETTERS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz")


def affix_char_class(char: str) -> str:
    """``D`` for an ASCII digit, ``O`` for a separator, ``L`` for anything else."""
    if "0" <= char <= "9":
        return "D"
    if char in _OTHER:
        return "O"
    return "L"


def align_affix_to_run_boundary(side: str, affix: str, names: Sequence[str]) -> str:
    """The affix, shrunk until removing it splits no same-class run in any name.

    ``side`` is "prefix" or, for anything else, "suffix" - matching the bash
    original, which tests ``== prefix``.
    """
    if not affix:
        return affix

    is_prefix = side == "prefix"
    # The inner boundary is where the affix meets the divergent middle of the
    # names: the affix's last character for a prefix, its first for a suffix.
    boundary = affix[-1] if is_prefix else affix[0]
    cls = affix_char_class(boundary)
    if cls == "O":
        # Already a clean edge; there is no run here to cut through.
        return affix

    # Retreat only if the cut would land inside a same-class run in SOME name. A
    # name the affix covers entirely has no character past the boundary, so it can
    # neither be split nor prevent a retreat, and is skipped. This is what keeps
    # "die" and "di" from losing their shared "di" and leaving "e" and "".
    span = len(affix)
    splits = False
    for name in names:
        if is_prefix:
            if len(name) <= span:
                continue
            adjacent = name[span]
        else:
            index = len(name) - span - 1
            if index < 0:
                continue
            adjacent = name[index]
        if affix_char_class(adjacent) == cls:
            splits = True
            break
    if not splits:
        return affix

    # Retreat past the whole run, leaving the affix ending at a class change - or
    # empty, which means it lay entirely inside the run and nothing can go.
    #
    # The run is ASCII only, while the class test above is not: an affix ending in
    # an accented letter is class L, finds a split, and then retreats past nothing,
    # because "é" is not in [A-Za-z]. bash does exactly this, so the port does too.
    run = _ASCII_LETTERS if cls == "L" else frozenset("0123456789")
    if is_prefix:
        while affix and affix[-1] in run:
            affix = affix[:-1]
    else:
        while affix and affix[0] in run:
            affix = affix[1:]
    return affix
