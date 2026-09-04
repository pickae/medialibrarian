"""The group passes over a freshly separated (prefix, core) pair per sibling.

The three passes read the same pair list and run in a fixed
order - wipe the doubled prefix, normalise the padding,
then wipe a uniform prefix - and keeping them in one module is what lets a reader
see that order. It is not arbitrary: the wipe runs first so the padding sees the
corrected set, and the uniform wipe runs last so its "identical" test sees
prefixes in their final, uniformly-padded form.

A pass takes the prefixes, the cores, and the indices of the siblings that form the
group under consideration (the plurality filetype, say). Entries outside the group
are returned untouched. Every pass is all-or-nothing across its group: a partial
strip would desynchronise the group's numbering, which is the one thing the whole
name-cleaning stack exists to preserve.
"""

import re
from collections.abc import Sequence

from medialib.lib.enums import DATE_PREFIX_PATTERN

__all__ = ["normalize_prefix_padding", "wipe_doubled_prefixes", "wipe_uniform_prefixes"]

# An eight-digit YYYYMMDD, compiled from the one definition of the shape.
_DATE_PREFIX = re.compile(DATE_PREFIX_PATTERN)


def _is_digits(value: str) -> bool:
    """Whether bash's ``^[0-9]+$`` would match - ASCII digits only.

    ``str.isdigit`` is not that test: it accepts superscripts and other Unicode
    digit forms that bash's bracket expression rejects, and a media filename can
    contain them.
    """
    return value != "" and all("0" <= c <= "9" for c in value)


def wipe_doubled_prefixes(
    prefixes: Sequence[str], cores: Sequence[str], indices: Sequence[int]
) -> list[str]:
    """Clear the outer prefix where the whole group repeats it inside its core.

    A doubled prefix is one that was split off a name and is *also* still the
    leading token of what was left: "1 1 A" separates into prefix "1" + core
    "1 A". The number already lives in the core, so the outer copy says nothing,
    and clearing it makes the name reassemble as "1 A" rather than "1 1 A".

    A purely numeric prefix is compared to the core's leading token by VALUE, not
    as text, so a doubled copy is still recognised when the two are zero-padded to
    different widths ("01" against "1 A"). That matters because our own padding
    pass widens the outer prefix and would otherwise defeat this one. Anything
    non-numeric is matched as text.

    Returns the new prefix list; the inputs are not modified.
    """
    result = list(prefixes)
    if not indices:
        return result

    for i in indices:
        prefix = prefixes[i]
        core = cores[i]
        # Every member must have a non-empty prefix that its core repeats.
        if not prefix:
            return result
        lead = core.split(" ", 1)[0]
        if _is_digits(prefix) and _is_digits(lead):
            if int(prefix) != int(lead):
                return result
        elif core != prefix and not core.startswith(prefix + " "):
            return result

    for i in indices:
        result[i] = ""
    return result


def normalize_prefix_padding(
    prefixes: Sequence[str], cores: Sequence[str], indices: Sequence[int]
) -> tuple[list[str], list[str]]:
    """Re-pad a group's numeric prefixes to the digit width of its largest member.

    "5" becomes "05" when the group runs up to 34, and "007" becomes "7" when it
    only runs up to 9 - one rule, both directions, because the width is always the
    digit count of the largest VALUE rather than of the widest text.

    It is a deliberate no-op unless the group's prefixes are a gapless run of
    distinct non-negative integers. That test is what tells a numbering series
    apart from a handful of unrelated numbers - three folders from 2019, 2021 and
    2024 are not chapters 2019 to 2024, and repadding them would say they were.
    The run need not start at 1, and needs at least two members.

    A core that is nothing but its own prefix is repadded in lockstep, so the two
    stay equal and the caller's later collapse of that pair to a single "01" still
    fires.

    Returns the new prefixes and cores; the inputs are not modified.
    """
    new_prefixes = list(prefixes)
    new_cores = list(cores)

    values: list[int] = []
    for i in indices:
        prefix = prefixes[i]
        if not _is_digits(prefix):
            return new_prefixes, new_cores
        values.append(int(prefix))

    if len(values) < 2 or len(set(values)) != len(values):
        return new_prefixes, new_cores
    # All values distinct, so a gapless run is exactly one whose inclusive span
    # equals its size.
    if max(values) - min(values) + 1 != len(values):
        return new_prefixes, new_cores

    width = len(str(max(values)))
    for i, value in zip(indices, values, strict=True):
        padded = str(value).zfill(width)
        if cores[i] == prefixes[i]:
            new_cores[i] = padded
        new_prefixes[i] = padded
    return new_prefixes, new_cores


def wipe_uniform_prefixes(
    prefixes: Sequence[str], cores: Sequence[str], indices: Sequence[int]
) -> list[str]:
    """Clear a prefix the whole group shares, because it tells them apart from nothing.

    A folder of "2024 A", "2024 B", "2024 C" reads as "A", "B", "C" once the year
    every one of them carries is gone. Three things stop that:

    * **A group of one.** Its prefix may be its only identifying text, and there is
      nothing for it to be uninformative *against*.
    * **A date.** An eight-digit YYYYMMDD says when the item is from - that is
      information about the item, not about how it differs from its siblings, and
      it is what the group sorts by. Two parts of one podcast episode published the
      same day share a date by coincidence, not by redundancy. A uniform plain
      number ("2024") is still wiped; it is the date SHAPE that is protected, so an
      eight-digit run that cannot be a date ("30260728") goes.
    * **A member whose core is empty** - whose whole name is the prefix. Wiping
      would strand that one with no name at all while stripping the prefix from its
      siblings. A non-empty core that merely equals the prefix does not block it:
      that member keeps the core as its name, so "1 1"/"1 2"/"1 3" collapse
      consistently to "1"/"2"/"3".

    Meant to run after the padding pass, so "identical" is tested on prefixes in
    their final width. Returns the new prefix list; the inputs are not modified.
    """
    result = list(prefixes)
    if len(indices) <= 1:
        return result

    first = prefixes[indices[0]]
    if not first or _DATE_PREFIX.match(first):
        return result

    for i in indices:
        if prefixes[i] != first or not cores[i]:
            return result

    for i in indices:
        result[i] = ""
    return result
