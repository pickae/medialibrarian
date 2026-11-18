"""Comma-separated named parts of an option's value, settled once.

-k names the parts a run is told to keep, and -o the phases it is told to
omit; both are one letter for every such thing, so a user who learnt the
form in one command can guess it in the next.

The option is declared with the parser's optional-argument form, the way
``convert-video -t [percent]`` is, so a folder that is not one of the parts
stays a folder. The value itself is settled here, and this is the only place
that knows a command's parts: an empty value - the bare flag - names them
all, and a part not among them is refused by naming the ones that are.
"""

from __future__ import annotations

from medialib.lib import clioptions

__all__ = ["parse"]


def parse(value: str, allowed: tuple[str, ...], flag: str = "-k",
          word: str = "part") -> frozenset[str]:
    """The parts the option's value names, as a set.

    An empty value, the bare flag, names every part in ``allowed``. A part
    not in ``allowed`` is a UsageError naming the ones that are, the way the
    parser refuses a value of the wrong kind. ``flag`` and ``word`` are the
    option's own, so the refusal says "-o phase" of the -o and "-k part" of
    the -k.
    """
    if not value:
        return frozenset(allowed)
    kept = set()
    for part in value.split(","):
        if part not in allowed:
            raise clioptions.UsageError(
                f"The {flag} {word} must be one of: {', '.join(allowed)} "
                f'(got "{part}").')
        kept.add(part)
    return frozenset(kept)
