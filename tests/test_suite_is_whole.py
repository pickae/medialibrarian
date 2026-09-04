"""The suite pytest ran is the suite the repository holds, asked as a case.

`conftest.py` refuses a short run before it starts, and that is the better
answer wherever it can be had - a run that stops first wastes nothing. Under
`-n` it cannot: the refusal hook runs in the workers, a worker that raises
disagrees with its siblings about what was collected, and xdist reports that as
an INTERNALERROR naming an unrelated case. The controller cannot stand in for
them, because the ids xdist hands it have already been through `-m` and the
media-only files would read as missing.

So the same two questions are asked here as well, where the reporting is the
same reporting as every other case's and holds however the run was started.
Nothing about it is xdist-specific: serially the refusal has already fired long
before this runs, which is the intended order and leaves this a check that
passes rather than a second mechanism to keep in step.

The wording of a failure lives in `conftest.py`, so a report reads the same
whichever of the two got there first.
"""

import pytest

from tests import conftest

pytestmark = pytest.mark.pure


def test_the_suite_is_the_whole_repository(request):
    """Every `test_*.py` under `tests/` handed over at least one case, and the
    total is above the floor.

    Skipped for a run that asked for PART of the suite, which can say nothing
    about what is missing from it - the same distinction `conftest.py` draws,
    read through the same function so the two cannot drift apart.
    """
    if not conftest.whole_suite_run(request.config):
        pytest.skip("a run of part of the suite says nothing about the whole")
    shortfall = conftest.suite_shortfall()
    assert not shortfall, shortfall
