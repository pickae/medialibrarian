"""The CLI contract of content-census, against the port.

The fixtures under tests/data/cliContract are the oracle: a page changes only
when a decision says so, and this compares the bytes this command prints against
the ones recorded there.

The fixtures carry `<repo>` where the recorded run had the repository path, so
the port is asked for its page under exactly that program name.
"""

import io
from contextlib import redirect_stderr, redirect_stdout

import pytest

from medialib.cli import content_census
from tests import blackbox

pytestmark = pytest.mark.pure

_FIXTURES = blackbox.DATA / "cliContract" / "content-census"

_PROGRAM = "content-census"


def _run(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        status = content_census.main(list(argv), program=_PROGRAM)
    return status, out.getvalue(), err.getvalue()


@pytest.mark.parametrize("scenario,argv", [
    ("h", ["-h"]),
    ("noargs", []),
    ("errUnknown", ["-x"]),
])
def test_the_recorded_scenario_is_reproduced_byte_for_byte(scenario, argv):
    status, out, err = _run(argv)
    assert out == (_FIXTURES / f"{scenario}.out").read_text()
    assert err == (_FIXTURES / f"{scenario}.err").read_text()
    assert status == int((_FIXTURES / f"{scenario}.rc").read_text().strip())


class TestTheOptionsThemselves:
    def test_the_defaults_are_what_the_script_declares(self):
        # -d, -o and -b reach the run as values; -t is a flag the script acts on
        # through cliOnOpt and so has no variable of its own.
        from medialib.lib import clioptions
        result = clioptions.parse(content_census.spec(_PROGRAM), ["lib"])
        assert result.values == {"depth": "", "outDir": "", "runBI": ""}
        assert result.positionals == ["lib"]

    def test_options_are_taken_after_a_positional_too(self):
        from medialib.lib import clioptions
        result = clioptions.parse(content_census.spec(_PROGRAM),
                                  ["lib", "-d", "1", "other", "-t"])
        assert result.values["depth"] == "1"
        assert result.positionals == ["lib", "other"]
        assert "t" in result.given

    # "08" among them: cliOptions' nonNegInt is ^(0|[1-9][0-9]*)$, so a padded
    # value never reaches the script at all - which is what makes the script's
    # own "10#$depth" base-10 guard belt-and-braces rather than the thing that
    # saves "-d 08" from being read as bad octal.
    @pytest.mark.parametrize("bad", ["one", "-1", "1.5", "", "08"])
    def test_a_depth_that_is_not_a_count_is_refused(self, bad):
        status, _, err = _run(["-d", bad, "lib"])
        assert status == 1
        assert err.startswith(
            "The -d depth in levels must be a whole number of 0 or more")

    @pytest.mark.parametrize("good", ["0", "1", "12"])
    def test_a_depth_that_is_a_count_is_taken(self, good):
        from medialib.lib import clioptions
        result = clioptions.parse(content_census.spec(_PROGRAM),
                                  ["-d", good, "lib"])
        assert result.values["depth"] == good


class TestTheSpecIsInternallyConsistent:
    """The two checks the smoke tier makes over every option spec. They are
    properties of the SPEC itself, so they hold wherever the spec lives."""

    def _letters(self, block):
        return [line.split("|")[0].strip()
                for line in block.strip().split("\n") if "|" in line]

    def test_every_option_acted_on_is_declared(self):
        declared = set(self._letters(content_census.OPT_SPEC))
        acting = {pair.split(":")[0]
                  for pair in content_census.OPT_VARS.split()}
        acting |= set(self._letters(content_census.OPT_CHECKS))
        # -t is acted on by the run rather than through a variable, the way the
        # shell acts on it in cliOnOpt.
        acting.add("t")
        assert acting <= declared, "undeclared: %s" % sorted(acting - declared)

    def test_no_option_letter_is_declared_twice(self):
        letters = self._letters(content_census.OPT_SPEC)
        assert len(letters) == len(set(letters))

