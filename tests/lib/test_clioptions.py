"""Tests for medialib.lib.clioptions - the option spec, the page, the parse loop.

What is pinned here is the handful of rules a random spec would take thousands of
cases to stumble on, and the ones whose being an accident of bash is the reason
they have to be reproduced at all.
"""

import inspect

import pytest

from medialib.lib import clioptions as cli


def spec(**fields):
    return cli.Spec(**fields)


class TestSplittingASpecLine:
    def test_the_placeholder_and_the_help(self):
        line = "j | <count> | how many jobs"
        assert cli.spec_field(line, 2) == "<count>"
        assert cli.spec_field(line, 3) == "how many jobs"

    def test_an_escaped_pipe_survives_in_either_field(self):
        """convert-video -f's placeholder is <1\\|2>, so the split cannot be a
        plain split on the pipe."""
        line = r"f | <1\|2> | either \| or the other"
        assert cli.spec_field(line, 2) == "<1|2>"
        assert cli.spec_field(line, 3) == r"either | or the other"

    def test_the_help_is_the_whole_remainder_pipes_and_all(self):
        """bash's read puts what is left into the last variable, so an UNescaped
        pipe in the help is help text rather than a fourth field."""
        assert cli.spec_field("j | <n> | a | b | c", 3) == "a | b | c"

    def test_the_help_is_trimmed_at_the_front_only(self):
        """The placeholder is trimmed both ends and the help is not, which is not
        symmetry anybody chose - it is where bash's two trims were written. A page
        whose help ends in spaces prints them, and the port has to as well."""
        assert cli.spec_field("j | <n> |   padded   ", 3) == "padded   "
        assert cli.spec_field("j |   <n>   | x", 2) == "<n>"

    def test_a_field_of_nothing_but_spaces_comes_back_empty(self):
        assert cli.spec_field("j |    | x", 2) == ""
        assert cli.spec_field("j | <n> |    ", 3) == ""

    def test_a_missing_field_is_empty_rather_than_an_error(self):
        assert cli.spec_field("j", 2) == ""
        assert cli.spec_field("j", 3) == ""


class TestWhichOptionsTakeAnArgument:
    def test_a_bracketed_placeholder_declares_one(self):
        tables = cli.flag_tables(spec(options="j | <count> | jobs"))
        assert "j" in tables.has_arg

    def test_a_square_placeholder_does_not(self):
        """[percent] is convert-video -t, whose argument is OPTIONAL - and
        getopts cannot express that, so the flag is declared bare and the word
        after it is claimed by hand."""
        tables = cli.flag_tables(spec(options="t | [percent] | a percentage"))
        assert "t" not in tables.has_arg

    def test_the_arg_flag_declares_one_the_help_never_shows(self):
        tables = cli.flag_tables(spec(options="b | | ", flags="arg:b"))
        assert "b" in tables.has_arg

    def test_the_first_entry_that_declares_an_argument_decides(self):
        """The scan's skip is a memo on "already known to take an argument", not on
        "already seen": an entry that declares nothing is passed over and a later
        one for the same letter still gets its say."""
        tables = cli.flag_tables(spec(options="j | | jobs\nj | <count> | jobs again"))
        assert "j" in tables.has_arg

    def test_and_a_later_entry_cannot_take_the_argument_away(self):
        tables = cli.flag_tables(spec(options="j | <count> | jobs\nj | | jobs again"))
        assert "j" in tables.has_arg

    def test_but_the_last_check_for_a_letter_decides(self):
        """The opposite rule, in the scan right next to it. Neither script writes a
        letter twice; both are kept because which one wins is not this port's
        decision to make."""
        tables = cli.check_tables(spec(checks="j | posInt | first\nj | nonNegInt | second"))
        assert tables.check_kind["j"] == "nonNegInt"
        assert tables.check_label["j"] == "second"


class TestTheOptionString:
    def test_silent_mode_always(self):
        assert cli.build_opt_string(spec(options="j | | x"),
                                    cli.flag_tables(spec(options="j | | x"))).startswith(":")

    def test_h_is_added_when_the_page_does_not_document_it(self):
        s = spec(options="j | <n> | jobs")
        assert cli.build_opt_string(s, cli.flag_tables(s)) == ":hj:"

    def test_h_is_not_added_twice_when_it_is_documented(self):
        s = spec(options="h | | this help\nj | <n> | jobs")
        assert cli.build_opt_string(s, cli.flag_tables(s)) == ":hj:"

    def test_a_letter_written_twice_appears_once(self):
        s = spec(options="j | <n> | jobs\nj | <n> | jobs again")
        assert cli.build_opt_string(s, cli.flag_tables(s)) == ":hj:"


class TestRenderingThePage:
    def test_the_help_starts_at_the_column(self):
        s = spec(options="j | <n> | jobs", column=20)
        assert cli.render_options(s) == "    -j <n>" + " " * 10 + "jobs"

    def test_a_field_past_the_column_puts_its_help_on_the_next_line(self):
        """convert-video -g does this. The help is then indented to the column,
        not to the end of the field."""
        s = spec(options="g | <a very long placeholder> | jobs", column=10)
        assert cli.render_options(s) == "    -g <a very long placeholder>\n" + " " * 10 + "jobs"

    def test_an_entry_with_no_help_renders_nothing(self):
        s = spec(options="j | <n> | jobs\nq | | \nc | | clean", column=12)
        assert "-q" not in cli.render_options(s)

    def test_but_it_still_feeds_the_option_string(self):
        s = spec(options="q | | ")
        assert "q" in cli.build_opt_string(s, cli.flag_tables(s))

    def test_the_block_ends_without_a_line_break(self):
        """What follows the last option line in the page is the tail's business."""
        assert not cli.render_options(spec(options="j | <n> | jobs")).endswith("\n")

    def test_a_note_before_the_first_entry_gets_no_leading_break(self):
        """content-census-bi's "(may be given ...)" line. The breaks go BETWEEN
        things that were emitted, which is the only reason this works."""
        s = spec(options="(a note)\nj | <n> | jobs", column=12)
        assert cli.render_options(s) == "(a note)\n    -j <n>  jobs"

    def test_a_blank_line_in_the_spec_renders_nothing_at_all(self):
        s = spec(options="j | <n> | jobs\n\nc | | clean", column=12)
        assert cli.render_options(s) == "    -j <n>  jobs\n    -c      clean"

    def test_a_wrapped_line_is_the_specs_own_text(self):
        s = spec(options="j | <n> | jobs\n            and more about them", column=12)
        assert cli.render_options(s).endswith("\n            and more about them")


class TestTheWholePage:
    def test_head_then_options(self):
        s = spec(head="Usage: x", options="j | <n> | jobs", column=12)
        assert cli.page(s) == "Usage: x\n    -j <n>  jobs"

    def test_a_page_with_no_options_is_the_head_alone(self):
        """cue-to-chapters sets only the head."""
        assert cli.page(spec(head="Usage: x")) == "Usage: x"

    def test_the_tail_brings_its_own_leading_break(self):
        """transcribe-audio's tail is a lone newline, and that newline is what
        ends its page. A renderer that added one would double it."""
        s = spec(head="Usage: x", options="j | <n> | jobs", column=12, tail="\n")
        assert cli.page(s) == "Usage: x\n    -j <n>  jobs\n"

    def test_a_head_with_no_options_still_takes_its_tail(self):
        assert cli.page(spec(head="Usage: x", tail="\n\nMore.")) == "Usage: x\n\nMore."


class TestTheRefusals:
    def test_a_message_is_followed_by_a_blank_line_then_the_page(self):
        text = cli.usage_error_text(spec(head="Usage: x"), "Unknown option: -q")
        assert text == "Unknown option: -q\n\nUsage: x\n\nNothing was changed.\n"

    def test_no_message_leads_with_a_blank_line_instead(self):
        """Not the same text minus a sentence: content-census-bi printed the page
        led by one blank line for a refused flag, and that shape is the contract."""
        assert cli.usage_error_text(spec(head="Usage: x")) == "\nUsage: x\n\nNothing was changed.\n"

    def test_the_missing_folder_wording(self):
        assert cli.missing_dir_text(spec(head="Usage: x"), "/no/such") == (
            'Directory "/no/such" does not exist.\n\nUsage: x\n')

    def test_the_no_argument_print_leads_with_the_credits(self):
        assert cli.no_args_text(spec(head="Usage: x")) == "David Ernst\n\nUsage: x\n"

    def test_unless_the_script_asked_for_the_page_alone(self):
        s = spec(head="Usage: x", no_args_with_credits=False)
        assert cli.no_args_text(s) == "Usage: x\n"


class TestTheValueChecks:
    def tables(self, checks):
        return cli.check_tables(spec(checks=checks))

    @pytest.mark.parametrize("value", ["1", "4", "64", "9999"])
    def test_posint_accepts(self, value):
        cli.validate("j", value, self.tables("j | posInt | job count"))

    @pytest.mark.parametrize("value", ["0", "-1", "07", "1.5", "abc", "", " 4", "4 ", "+3"])
    def test_posint_refuses(self, value):
        with pytest.raises(cli.UsageError):
            cli.validate("j", value, self.tables("j | posInt | job count"))

    def test_the_refusal_names_the_value_and_the_label(self):
        with pytest.raises(cli.UsageError) as caught:
            cli.validate("j", "x", self.tables("j | posInt | job count"))
        assert caught.value.message == ('The -j job count must be a whole '
                                        'number of 1 or more (got "x").')

    def test_a_missing_label_becomes_the_word_value(self):
        with pytest.raises(cli.UsageError) as caught:
            cli.validate("j", "x", self.tables("j | posInt | "))
        assert caught.value.message.startswith("The -j value must be")

    def test_zero_passes_nonnegint_and_nothing_else_changes(self):
        cli.validate("j", "0", self.tables("j | nonNegInt | "))
        with pytest.raises(cli.UsageError):
            cli.validate("j", "-0", self.tables("j | nonNegInt | "))

    @pytest.mark.parametrize("value", ["07", "007", "", " ", "+7"])
    def test_a_whole_number_is_the_one_somebody_typed_on_purpose(self, value):
        """A padded number is a spelling the shell would read as octal, and an
        empty value is an argument that was never given."""
        with pytest.raises(cli.UsageError):
            cli.validate("j", value, self.tables("j | nonNegInt | "))

    @pytest.mark.parametrize("value,ok", [("-5", True), ("5", True), ("0", True),
                                          ("-6", False), ("6", False)])
    def test_a_range_includes_both_ends(self, value, ok):
        tables = self.tables("q | int:-5:5 | level")
        if ok:
            cli.validate("q", value, tables)
        else:
            with pytest.raises(cli.UsageError):
                cli.validate("q", value, tables)

    def test_an_enum_lists_its_choices_with_commas(self):
        """The pipes between the choices are ESCAPED in the spec, because the spec
        line is itself pipe-separated - ytdlp writes enum:windows\\|linux\\|auto
        and transcribe-audio builds its list with sed 's/ /\\\\|/g'."""
        with pytest.raises(cli.UsageError) as caught:
            cli.validate("m", "x", self.tables(r"m | enum:fast\|slow | mode"))
        assert caught.value.message == (
            'The -m mode must be one of: fast, slow (got "x").')

    def test_the_choices_are_matched_as_spelled(self):
        """A user who types Linux is told what the list holds rather than being
        quietly given the lower-cased one."""
        tables = self.tables(r"s | enum:windows\|linux\|auto | system")
        cli.validate("s", "linux", tables)
        with pytest.raises(cli.UsageError):
            cli.validate("s", "Linux", tables)

    def test_an_unescaped_pipe_splits_the_line_instead(self):
        """Which is why they are escaped. Left bare, the second choice becomes part
        of the LABEL and the enum accepts only the first - a spec bug that reads as
        a working line, and the reason this is written down."""
        tables = self.tables("m | enum:fast|slow | mode")
        assert tables.check_kind["m"] == "enum:fast"
        assert tables.check_label["m"] == "slow | mode"
        cli.validate("m", "fast", tables)
        with pytest.raises(cli.UsageError):
            cli.validate("m", "slow", tables)

    def test_an_empty_enum_accepts_nothing_including_the_empty_string(self):
        """bash's walk is a while over a non-empty string, so it never runs once."""
        with pytest.raises(cli.UsageError):
            cli.validate("m", "", self.tables("m | enum: | mode"))

    def test_an_unknown_kind_refuses_every_run_rather_than_passing_it(self):
        with pytest.raises(cli.UsageError) as caught:
            cli.validate("m", "anything", self.tables("m | wat | mode"))
        assert caught.value.message.startswith("Internal: option -m declares an unknown check")

    def test_a_letter_with_no_check_accepts_anything(self):
        cli.validate("z", "nonsense", self.tables("j | posInt | "))


class TestTheParseLoop:
    def parse(self, options, argv, **fields):
        return cli.parse(spec(options=options, **fields), argv)

    def test_an_option_a_flag_and_two_positionals(self):
        result = self.parse("j | <n> | jobs\nc | | clean",
                            ["-j", "4", "in", "-c", "out"],
                            vars="j:jobs c:clean")
        assert result.values == {"jobs": "4", "clean": "1"}
        assert result.positionals == ["in", "out"]
        assert result.given == ["j", "c"]

    def test_an_option_after_a_positional_is_still_an_option(self):
        """getopts on its own stops at the first non-option, so the loop restarts
        after each positional it collects. Nobody types the flags first when they
        are adding one to a command they already typed once."""
        result = self.parse("j | <n> | jobs", ["in", "out", "-j", "8"], vars="j:jobs")
        assert result.values["jobs"] == "8"
        assert result.positionals == ["in", "out"]

    def test_a_double_dash_ends_the_options_for_good(self):
        result = self.parse("j | <n> | jobs", ["--", "-j", "8"], vars="j:jobs")
        assert result.values.get("jobs", "") == ""
        assert result.positionals == ["-j", "8"]

    def test_a_lone_dash_is_a_positional(self):
        result = self.parse("j | <n> | jobs", ["-", "in"], vars="j:jobs")
        assert result.positionals == ["-", "in"]

    def test_an_attached_value(self):
        result = self.parse("j | <n> | jobs", ["-j4"], vars="j:jobs")
        assert result.values["jobs"] == "4"

    def test_a_cluster_of_flags(self):
        result = self.parse("c | | clean\nv | | verbose\nq | | quiet",
                            ["-cvq"], vars="c:clean v:verbose q:quiet")
        assert result.values == {"clean": "1", "verbose": "1", "quiet": "1"}

    def test_a_cluster_whose_last_letter_takes_the_next_word(self):
        result = self.parse("c | | clean\nj | <n> | jobs", ["-cj", "4"],
                            vars="c:clean j:jobs")
        assert result.values == {"clean": "1", "jobs": "4"}

    def test_an_unknown_flag_is_refused(self):
        with pytest.raises(cli.UsageError) as caught:
            self.parse("j | <n> | jobs", ["-z"])
        assert caught.value.message == "Unknown option: -z"

    def test_a_missing_argument_is_refused(self):
        with pytest.raises(cli.UsageError) as caught:
            self.parse("j | <n> | jobs", ["-j"])
        assert caught.value.message == "Option -j requires an argument"

    def test_h_stops_everything(self):
        with pytest.raises(cli.HelpRequested):
            self.parse("j | <n> | jobs", ["-j", "4", "-h", "in"])

    def test_a_repeated_option_accumulates(self):
        result = self.parse("t | <tag> | a tag", ["-t", "a", "-t", "b"],
                            vars="t:tags", flags="repeat:t")
        assert result.values["tags"] == ["a", "b"]

    def test_a_value_is_checked_before_it_is_assigned(self):
        """A bad value is refused the moment it is parsed, so it never reaches the
        variable and never surfaces an hour into the run as a worker failure."""
        with pytest.raises(cli.UsageError):
            self.parse("j | <n> | jobs", ["-j", "0"], vars="j:jobs",
                       checks="j | posInt | job count")


class TestTheOptionalArgument:
    def parse(self, argv):
        return cli.parse(spec(options="t | [percent] | a percentage",
                              flags="optionalArg:t:^[0-9]+$",
                              vars="t:pct"), argv)

    def test_a_number_after_it_is_claimed(self):
        result = self.parse(["-t", "30", "in"])
        assert result.values["pct"] == "30"
        assert result.positionals == ["in"]

    def test_a_word_that_is_not_one_is_left_where_it_was(self):
        """"-t <inputDir>" has to leave the directory alone, or the flag would eat
        the argument the run is about."""
        result = self.parse(["-t", "in"])
        assert result.values["pct"] == ""
        assert result.positionals == ["in"]

    def test_a_bare_flag_at_the_end_of_the_line(self):
        result = self.parse(["-t"])
        assert result.values["pct"] == ""
        assert result.positionals == []

    def test_an_unclaimed_argument_is_not_checked(self):
        """Empty means the default is standing, not that an empty string was
        typed, so the check has nothing to look at."""
        result = cli.parse(spec(options="t | [percent] | pct",
                                flags="optionalArg:t:^[0-9]+$",
                                vars="t:pct",
                                checks="t | posInt | percentage"), ["-t", "in"])
        assert result.values["pct"] == ""


class TestTheClampBashPutsOnOptind:
    """The one piece of getopts that had to be discovered rather than read.

    On the call that finds no more options, bash pulls OPTIND back to one past the
    last argument. The optional-argument branch advances OPTIND by hand, so when
    the word it claims is the last one OPTIND ends up two past the end - and
    without the clamp the loop would shift by more than it has, which bash's shift
    refuses to do, leaving the option word to be collected a second time as a
    positional.
    """

    def test_a_trailing_optional_argument_leaves_no_positional_behind(self):
        result = cli.parse(spec(options="t | [percent] | pct",
                                flags="optionalArg:t:^[0-9]*$",
                                vars="t:pct"), ["-t"])
        assert result.positionals == []

    def test_and_the_claimed_word_is_not_collected_either(self):
        result = cli.parse(spec(options="t | [percent] | pct",
                                flags="optionalArg:t:^[0-9]+$",
                                vars="t:pct"), ["-t", "30"])
        assert result.values["pct"] == "30"
        assert result.positionals == []


class TestTheArgumentCountGate:
    def test_below_the_minimum(self):
        assert cli.args_out_of_range(1, 2, None)

    def test_above_the_maximum(self):
        assert cli.args_out_of_range(4, 1, 3)

    def test_no_maximum_means_no_upper_limit(self):
        assert not cli.args_out_of_range(99, 1, None)

    def test_the_ends_are_inside(self):
        assert not cli.args_out_of_range(1, 1, 3)
        assert not cli.args_out_of_range(3, 1, 3)


class TestTheHelpPathWritesNothingAnybodyCanRead:
    def test_the_help_exception_discards_the_result_it_was_building(self):
        """parse() does not catch HelpRequested, so the Result it had half-built
        when -h arrived is unreachable from every caller. Whether the -h marker is
        recorded before the raise or not at all cannot be observed, and this is
        what makes that a fact about the code rather than about the fixtures."""
        assert "except HelpRequested" not in inspect.getsource(cli.parse)
        with pytest.raises(cli.HelpRequested):
            cli.parse(spec(options="h | | help\nj | <n> | jobs", vars="j:jobs"),
                      ["-j", "4", "-h"])


# --- the long forms ----------------------------------------------------------
# Every option letter may be named in full as well. They are a
# layer on top of getopts rather than a second parser: a --name word is rewritten
# into its letter before the loop sees it, so there is one set of rules about
# clustering, attached arguments and -- rather than two that can disagree.

class TestLongOptions:
    OPTIONS = "j | <n> | jobs\nc | | clean\nt | <tag> | a tag"
    LONG = "j:jobs c:clean t:tag"

    def parse(self, argv, **fields):
        fields.setdefault("options", self.OPTIONS)
        fields.setdefault("long", self.LONG)
        fields.setdefault("vars", "j:jobs c:clean t:tag")
        return cli.parse(spec(**fields), argv)

    def test_a_long_option_with_its_value_as_the_next_word(self):
        assert self.parse(["--jobs", "4"]).values["jobs"] == "4"

    def test_a_long_option_with_an_attached_value(self):
        assert self.parse(["--jobs=4"]).values["jobs"] == "4"

    def test_a_long_flag_takes_no_value(self):
        assert self.parse(["--clean"]).values["clean"] == "1"

    def test_the_letter_still_works_beside_it(self):
        result = self.parse(["-j", "4", "--clean"])
        assert result.values == {"jobs": "4", "clean": "1", "tag": ""}

    def test_the_two_forms_of_one_option_are_the_same_option(self):
        assert self.parse(["--jobs", "4", "-j", "9"]).values["jobs"] == "9"

    def test_positionals_still_land_around_them(self):
        result = self.parse(["in", "--jobs=4", "out"])
        assert result.positionals == ["in", "out"]
        assert result.values["jobs"] == "4"

    def test_help_is_answered_whether_the_spec_declares_it_or_not(self):
        with pytest.raises(cli.HelpRequested):
            self.parse(["--help"])
        with pytest.raises(cli.HelpRequested):
            cli.parse(spec(options="j | <n> | jobs"), ["--help"])

    def test_an_unknown_long_option_is_refused_by_its_own_name(self):
        with pytest.raises(cli.UsageError) as caught:
            self.parse(["--jbos", "4"])
        assert caught.value.message == "Unknown option: --jbos"

    def test_no_abbreviation(self):
        """A prefix is not the option. Accepting one makes every new option a
        possible break of a command somebody already types."""
        with pytest.raises(cli.UsageError) as caught:
            self.parse(["--job", "4"])
        assert caught.value.message == "Unknown option: --job"

    def test_a_long_option_with_no_argument_left_is_refused(self):
        with pytest.raises(cli.UsageError) as caught:
            self.parse(["--jobs"])
        assert caught.value.message == "Option -j requires an argument"

    def test_a_value_handed_to_a_flag_is_refused(self):
        with pytest.raises(cli.UsageError) as caught:
            self.parse(["--clean=yes"])
        assert caught.value.message == "Option --clean takes no argument"

    def test_a_checked_value_is_checked_through_either_form(self):
        for argv in (["--jobs", "0"], ["--jobs=0"], ["-j", "0"]):
            with pytest.raises(cli.UsageError):
                self.parse(argv, checks="j | posInt | job count")

    def test_a_repeated_long_option_accumulates(self):
        result = self.parse(["--tag", "a", "--tag=b"],
                            vars="t:tags", flags="repeat:t")
        assert result.values["tags"] == ["a", "b"]

    def test_a_value_that_looks_like_a_long_option_is_still_a_value(self):
        """-p --weird hands "--weird" to -p and always has, so the rewrite has to
        know which options take an argument rather than translating every word
        that starts with two dashes."""
        assert self.parse(["--jobs", "--clean"]).values["jobs"] == "--clean"
        assert self.parse(["-j", "--clean"]).values["jobs"] == "--clean"

    def test_nothing_after_a_double_dash_is_read_as_an_option(self):
        result = self.parse(["--jobs", "4", "--", "--clean", "in"])
        assert result.positionals == ["--clean", "in"]
        assert result.values["clean"] == ""

    def test_an_optional_argument_through_the_long_form(self):
        """convertVideo's -t: the word after it is claimed only when it is one."""
        fields = dict(options="t | [n] | threshold", long="t:threshold",
                      vars="t:demand", flags="optionalArg:t:^[0-9]+$")
        assert self.parse(["--threshold", "5"], **fields).values["demand"] == "5"
        assert self.parse(["--threshold=5"], **fields).values["demand"] == "5"
        result = self.parse(["--threshold", "in"], **fields)
        assert result.values["demand"] == ""
        assert result.positionals == ["in"]
        with pytest.raises(cli.UsageError) as caught:
            self.parse(["--threshold=in"], **fields)
        assert caught.value.message == 'Option --threshold does not take "in"'


class TestRenderingTheLongForms:
    def test_the_letter_and_the_long_form_share_one_field(self):
        s = spec(options="j | <n> | jobs", long="j:jobs", column=20)
        assert cli.render_options(s) == "    -j, --jobs <n>  jobs"

    def test_a_field_too_wide_for_the_column_takes_its_own_line(self):
        """Rather than widening the column, which was tried first: every help
        text in every spec is wrapped to fit 80 columns at its own script's
        column, so moving the column right put all seventeen pages past 80."""
        s = spec(options="m | <n> | how many", long="m:min-prevalence",
                 column=20)
        assert cli.render_options(s) == (
            "    -m, --min-prevalence <n>\n"
            + " " * 20 + "how many")

    def test_the_column_is_what_the_spec_declares_either_way(self):
        s = spec(options="j | <n> | jobs", long="j:jobs", column=20)
        assert cli.option_column(s) == 20
        assert cli.option_column(spec(options="j | <n> | jobs", column=20)) == 20

    def test_a_wrapped_help_line_is_left_exactly_where_it_was_written(self):
        """Which is what let all 180 recorded pages keep every one of their help
        lines through the change: only the option FIELD moved."""
        s = spec(options="j | <n> | jobs\n                    and more",
                 long="j:jobs", column=20)
        assert cli.render_options(s) == (
            "    -j, --jobs <n>  jobs\n"
            "                    and more")

    def test_an_entry_with_no_long_form_renders_as_it_always_did(self):
        s = spec(options="j | <n> | jobs\nc | | clean", long="c:clean",
                 column=20)
        assert cli.render_options(s) == (
            "    -j <n>          jobs\n"
            "    -c, --clean     clean")
