"""The -v account of which cleaning pass changed a name.

rename_siblings reads a sibling's name back at every stage of the pipeline -
as found, after the individual pass, after the collective strip, after the
recovery pass, after the group passes over the prefixes - and names every
stage that left a different string. These are the stages, on names crafted to
isolate them, and the join _apply uses when it assembles a final name."""

from __future__ import annotations

import pytest

from medialib.cli import clean_folder_structure as cfs
from medialib.lib import prefixes

pytestmark = pytest.mark.fs


def _passes(names, mode="files", final=None):
    """_passes_for over the first sibling.

    ``final`` overrides the group passes over the prefixes: the states a run
    actually reaches, when the wipe and the padding do run.
    """
    split = prefixes.split_siblings(names, mode, None)
    final_prefixes, final_cores = split.prefixes, split.cores
    if final is not None:
        final_prefixes, final_cores = final
    return cfs._passes_for(0, names, mode, split, final_prefixes, final_cores)


def test_the_assembly_is_the_join_apply_makes():
    assert cfs._assemble("01", "Title") == "01 Title"
    assert cfs._assemble("01", "") == "01"
    assert cfs._assemble("", "Title") == "Title"
    assert cfs._assemble("", "") == ""
    # A prefix that is also the whole core is the core saying itself twice.
    assert cfs._assemble("01", "01") == "01"


def test_a_name_only_the_individual_pass_touches():
    assert _passes(["My_Movie.mp4"]) == "individual cleaning"


def test_the_collective_strip_is_the_one_that_changed_it():
    # "Chapter " is the text the group shares. The individual pass changes
    # nothing on these names, and the recovery re-splits the number into a
    # prefix without changing the string, so the strip is the only pass the
    # string owes anything to.
    assert _passes(["Chapter 07.opus", "Chapter 08.opus"]) == \
        "collective stripping"


def test_two_passes_change_a_name_and_both_are_named():
    assert _passes(["Show - Episode 01.opus", "Show - Episode 02.opus"]) == \
        "individual cleaning and collective stripping"


def test_a_string_only_the_recovery_pass_leaves_is_its():
    # The collective strip leaves "01 01": the recovery pass re-splits the
    # prefix and the assembly collapses the doubled core, so the string it
    # leaves is the one no earlier stage had.
    assert _passes(["Show 01 01.opus", "Show 02 02.opus"]) == \
        "collective stripping and hidden-prefix recovery"


def test_the_group_passes_over_the_prefixes_are_the_individual_passes():
    # A gapless run up to two digits repads the narrow prefixes: the string
    # that changes is the prefix' own, and the prefix is what the individual
    # pass split off. The cores share no passage, so the collective strip has
    # nothing of their text to take.
    names = ["%d %s %s.opus" % (i, "Alpha" if i % 2 else "Beta",
                                chr(64 + i)) for i in range(1, 13)]
    split = prefixes.split_siblings(names, "files", None)
    padded, cores = prefixes.normalize_prefix_padding(
        split.prefixes, split.cores, split.group)
    assert cfs._passes_for(0, names, "files", split, padded, cores) == \
        "individual cleaning"
