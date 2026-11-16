"""The -k parts: what a run is told to keep of what a command would take away.

One letter for every "leave it as it is" mode, so a user who learnt -k in one
command can guess it in the next. A bare -k keeps everything the command would
otherwise discard or alter; where a command has several such things, -k names
the parts it keeps. Everything additive or corrective still happens.

The option is declared with the parser's optional-argument form, the way
``convert-video -t [percent]`` is, so a folder that is not a part stays a
folder. The value itself is settled here, and this is the only place that knows
a command's parts: an empty value - the bare flag - keeps them all, and a part
not among them is refused by naming the ones that are.
"""

from __future__ import annotations

from medialib.lib import clioptions

__all__ = ["parse"]


def parse(value: str, allowed: tuple[str, ...]) -> frozenset[str]:
    """The parts the -k value names, as a set.

    An empty value, the bare flag, keeps every part in ``allowed``. A part not
    in ``allowed`` is a UsageError naming the ones that are, the way the parser
    refuses a value of the wrong kind.
    """
    if not value:
        return frozenset(allowed)
    kept = set()
    for part in value.split(","):
        if part not in allowed:
            raise clioptions.UsageError(
                f'The -k part must be one of: {", ".join(allowed)} '
                f'(got "{part}").')
        kept.add(part)
    return frozenset(kept)
