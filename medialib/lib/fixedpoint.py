"""Repeating a pass until it stops changing anything.

Some work cannot be done in one pass because a pass can create work for the
next: a rename that removes a pair of parentheses leaves a double space behind,
and a chunking decision made with probed files can cut files nobody has probed
yet. Such work is repeated until a pass reports that it changed nothing.

A pass answers what it changed - a count, or a bool - and anything falsy means
it changed nothing. A limit stops a pass that never settles from repeating for
ever, whether because a rule oscillates or because each pass costs too much to
let it run unbounded.
"""

from collections.abc import Callable

__all__ = ["until_stable"]


def until_stable(step: Callable[[], object], limit: int | None = None) -> int:
    """Call <step> until a call changes nothing, or <limit> calls have been
    made; how many of the calls changed something.

    An input that was already stable answers 0, after the one call that found
    nothing to do. When the limit is what stops it, the last call made may
    still have changed something: the answer is then <limit>.
    """
    changed = 0
    while limit is None or changed < limit:
        if not step():
            break
        changed += 1
    return changed
