"""The declarative CLI option spec: option string, help page, checks, parse loop.

The spec is a value and the parse returns one. Not a byte of what reaches a
terminal may change, so every rendering rule below is kept exactly as the
recorded pages have it, including the ones that look like accidents - the help
field trimmed at the front but not the back, the option block that ends without
a line break, the page that leads with credits only sometimes.

``getopts`` has no standard-library equivalent worth the name: ``getopt`` and
``argparse`` both disagree with it about clustering, attached arguments and what
happens at ``--``. So it is reimplemented here, in silent mode.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field

# What bash's [[:space:]] matches in the C locale. Python's str.strip() with no
# argument strips a great deal more than this, and a spec field is allowed to
# hold any of it.
_SPACE = " \t\n\v\f\r"

# The sentinel _cliSpecField swaps in for an escaped pipe while it splits a line.
# Chosen the same way bash chose it: a control character no help text carries.
_PIPE_HOLD = "\x01"

_ENTRY = re.compile(r"[a-zA-Z][ \t\n\v\f\r]*\|")

DEFAULT_CREDITS = "David Ernst"
DEFAULT_COLUMN = 20


class UsageError(Exception):
    """A refusal that ends the run: message, then the page, then the trailer.

    Raised rather than exited so a caller can render it on the stream the script
    used. ``message`` empty is the bare-page form content-census-bi printed.
    """

    def __init__(self, message: str = "") -> None:
        super().__init__(message)
        self.message = message


class HelpRequested(Exception):
    """``-h``: the credits and the page on stdout, exit 0. Not an error."""

@dataclass
class Spec:
    """One script's declaration, the seven globals plus the four settings."""

    head: str = ""
    options: str = ""
    vars: str = ""
    flags: str = ""
    checks: str = ""
    long: str = ""
    tail: str = ""
    column: int = DEFAULT_COLUMN
    credits: str = DEFAULT_CREDITS
    no_args_with_credits: bool = True
    no_args_stream: str = "stdout"


@dataclass
class Tables:
    """The lookup tables the parse loop reads, built once from the spec."""

    arg_flag: dict = field(default_factory=dict)
    repeat: dict = field(default_factory=dict)
    opt_regex: dict = field(default_factory=dict)
    has_arg: dict = field(default_factory=dict)
    check_kind: dict = field(default_factory=dict)
    check_label: dict = field(default_factory=dict)
    long_letter: dict = field(default_factory=dict)


@dataclass
class Result:
    """What a completed parse leaves behind."""

    values: dict = field(default_factory=dict)
    given: list = field(default_factory=list)
    positionals: list = field(default_factory=list)


def _lines(block: str) -> list[str]:
    """The lines bash's ``while read`` sees in a here-string of this value.

    A here-string appends a newline, so an empty block is one empty line and a
    block already ending in one gains a trailing empty line - which is exactly
    what ``split`` produces. The loop's ``|| [[ -n "$line" ]]`` only matters for
    a final line with no newline, and a here-string never leaves one.
    """
    return block.split("\n")


def _entry_letter(line: str) -> str | None:
    """The option letter of a spec line, or None when the line is not an entry.

    The bash test is ``^([a-zA-Z])[[:space:]]*\\|`` - anchored at the front only,
    so anything at all may follow the pipe.
    """
    match = _ENTRY.match(line)
    return line[0] if match else None


def spec_field(line: str, which: int) -> str:
    """Field 2 (the placeholder) or 3 (the help) of a ``letter | arg | help`` line.

    Field 3 is the whole remainder, pipes and all, because bash's ``read`` puts
    what is left into the last variable. It is trimmed at the FRONT only - the
    help of a wrapped entry ends where the spec says it ends, trailing spaces
    included - while field 2 is trimmed at both.
    """
    held = line.replace("\\|", _PIPE_HOLD)
    parts = held.split("|", 2)
    parts += [""] * (3 - len(parts))
    if which == 2:
        return parts[1].replace(_PIPE_HOLD, "|").strip(_SPACE)
    if which == 3:
        return parts[2].replace(_PIPE_HOLD, "|").lstrip(_SPACE)
    return ""


def flag_tables(spec: Spec, tables: Tables | None = None) -> Tables:
    """``cliOptFlags`` and the spec's placeholders, read into the parse tables."""
    tables = tables or Tables()
    tables.arg_flag = {}
    tables.repeat = {}
    tables.opt_regex = {}
    tables.has_arg = {}
    for flag in spec.flags.split():
        if flag.startswith("arg:"):
            tables.arg_flag[flag[len("arg:"):]] = 1
        elif flag.startswith("repeat:"):
            tables.repeat[flag[len("repeat:"):]] = 1
        elif flag.startswith("optionalArg:"):
            rest = flag[len("optionalArg:"):]
            if rest:
                # rest[1] is the separating colon, skipped without being checked -
                # a malformed entry declares a regex, not an error.
                tables.opt_regex[rest[0]] = rest[2:]
    for line in _lines(spec.options):
        letter = _entry_letter(line)
        # The skip is a memo on "already known to take an argument", not on
        # "already seen": an entry that declares nothing is passed over, and a
        # later entry for the same letter still gets its say.
        if letter is None or letter in tables.has_arg:
            continue
        arg = spec_field(line, 2)
        # getopts gets a colon exactly when the help shows a <placeholder> or the
        # spec hides the argument. The [percent] form of an optionalArg does not
        # count: getopts cannot express an optional argument, so that flag is
        # declared bare and the word after it is claimed by hand.
        if ((len(arg) >= 2 and arg.startswith("<") and arg.endswith(">"))
                or letter in tables.arg_flag):
            tables.has_arg[letter] = 1
    return tables


def check_tables(spec: Spec, tables: Tables | None = None) -> Tables:
    """``cliOptChecks`` read into the kind and label tables.

    A letter named twice keeps the LAST entry, unlike the placeholder scan above
    which keeps the first. Neither script does it; both are preserved because
    which one wins is not this port's decision to make.
    """
    tables = tables or Tables()
    tables.check_kind = {}
    tables.check_label = {}
    for line in _lines(spec.checks):
        letter = _entry_letter(line)
        if letter is None:
            continue
        tables.check_kind[letter] = spec_field(line, 2)
        tables.check_label[letter] = spec_field(line, 3)
    return tables


def long_pairs(spec: Spec) -> list[tuple]:
    """The ``letter:long-name`` pairs of the spec, in the order written."""
    out = []
    for pair in spec.long.split():
        letter, sep, name = pair.partition(":")
        if sep and name:
            out.append((letter, name))
    return out


def long_tables(spec: Spec, tables: Tables | None = None) -> Tables:
    """The long name a word may arrive as, mapped back to its letter.

    ``--help`` is answered whether or not the spec declares it, the way ``-h``
    already is: a script whose page does not document the flag still has it.
    """
    tables = tables or Tables()
    tables.long_letter = {"help": "h"}
    for letter, name in long_pairs(spec):
        tables.long_letter[name] = letter
    return tables


def build_tables(spec: Spec) -> Tables:
    """Both halves, in the order cliParse builds them."""
    tables = Tables()
    flag_tables(spec, tables)
    check_tables(spec, tables)
    long_tables(spec, tables)
    return tables


_POS_INT = re.compile(r"[1-9][0-9]*")
_NON_NEG_INT = re.compile(r"0|[1-9][0-9]*")
_ANY_INT = re.compile(r"0|-?[1-9][0-9]*")


def validate(letter: str, value: str, tables: Tables) -> None:
    """Refuse a value that does not fit the option's declared kind.

    Every refusal is the same sentence with the kind's own clause in it, so no
    script phrases its own variant of "that is not a number", and the value is
    always quoted back.
    """
    kind = tables.check_kind.get(letter, "")
    if not kind:
        return
    label = tables.check_label.get(letter, "") or "value"

    if kind == "posInt":
        if _POS_INT.fullmatch(value):
            return
        wanted = "a whole number of 1 or more"
    elif kind == "nonNegInt":
        if _NON_NEG_INT.fullmatch(value):
            return
        wanted = "a whole number of 0 or more"
    elif kind.startswith("int:") and ":" in kind[len("int:"):]:
        rest = kind[len("int:"):]
        low, _, high = rest.partition(":")
        # bash's (( )) reads the bounds as arithmetic; the spec writes them, so a
        # bound that is not a number is a typo in the spec rather than input.
        if _ANY_INT.fullmatch(value) and _in_range(value, low, high):
            return
        wanted = f"a whole number between {low} and {high}"
    elif kind.startswith("enum:"):
        choices = kind[len("enum:"):]
        # The arms and the message are built from the same string, so the two
        # cannot come to disagree about what is accepted.
        if value in _enum_choices(choices):
            return
        wanted = "one of: " + choices.replace("|", ", ")
    else:
        # An unknown kind is a typo in the spec, not something a user typed.
        # Refusing every run over it is what makes it impossible to miss.
        raise UsageError(f'Internal: option -{letter} declares an unknown check "{kind}".')

    raise UsageError(f'The -{letter} {label} must be {wanted} (got "{value}").')


def _enum_choices(choices: str) -> list[str]:
    """The accepted words of an ``enum:`` kind.

    An empty kind accepts nothing at all - bash's loop is ``while [[ -n ... ]]``,
    so it never runs - and that is not the same as accepting the empty string.
    """
    if not choices:
        return []
    return choices.split("|")


def _in_range(value: str, low: str, high: str) -> bool:
    try:
        return int(low) <= int(value) <= int(high)
    except ValueError:
        return False


def build_opt_string(spec: Spec, tables: Tables) -> str:
    """The getopts string: silent mode, one letter each, ``h`` always answered."""
    out = ""
    seen = set()
    for line in _lines(spec.options):
        letter = _entry_letter(line)
        if letter is None or letter in seen:
            continue
        seen.add(letter)
        out += letter + ":" if letter in tables.has_arg else letter
    # The scripts whose page does not document -h still answer it: the flag
    # prints the page and exits 0, only the line is missing from it.
    if "h" not in out:
        out = "h" + out
    return ":" + out


def _option_field(letter: str, long_name: str, arg: str) -> str:
    """The left-hand column of one option line: the letter, its long form if it
    has one, then the placeholder."""
    field = f"    -{letter}"
    if long_name:
        field += f", --{long_name}"
    if arg:
        field += " " + arg
    return field


def option_column(spec: Spec) -> int:
    """Where the help text of every option line starts: what the spec declares.

    It is NOT widened to fit the long forms, though that was the first thing
    tried. Every help text in every spec is wrapped to fit 80 columns at that
    script's own column, so moving the column right pushed all seventeen pages
    past 80 - up to 102. A field too wide for the column takes its own line
    instead, which is what the renderer already did for convertVideo's -g and
    what argparse does with a long invocation. Pages get taller; none gets wider,
    and not one authored line of help had to be re-wrapped.
    """
    return spec.column


def render_options(spec: Spec) -> str:
    """The spec's option lines, column-aligned, as one block with no final break.

    Only an entry's FIRST line is aligned; a wrapped line is the spec's own text
    and is emitted where it stands, as is any line that is not an entry at all.
    An entry with an empty help renders nothing while still feeding the option
    string. The line breaks go BETWEEN things that were emitted, which is what
    keeps a leading note line from starting the block with one.
    """
    out = ""
    emitted = False
    column = option_column(spec)
    longs = dict(long_pairs(spec))
    for line in _lines(spec.options):
        if not line:
            continue
        letter = _entry_letter(line)
        if letter is not None:
            help_text = spec_field(line, 3)
            if not help_text:
                continue
            if emitted:
                out += "\n"
            field_text = _option_field(letter, longs.get(letter, ""),
                                       spec_field(line, 2))
            if len(field_text) < column:
                out += field_text + " " * (column - len(field_text)) + help_text
            else:
                out += field_text + "\n" + " " * max(column, 0) + help_text
            emitted = True
        else:
            if emitted:
                out += "\n"
            out += line
            emitted = True
    return out


def page(spec: Spec) -> str:
    """The whole help page: head, the option block, then the tail verbatim.

    The head and the block are joined by one line break, and only when there is
    a block to join; the tail brings its own leading break, because what follows
    the last option line in the page is the tail's business, not the renderer's.
    """
    options = render_options(spec)
    if options:
        rendered = spec.head + "\n" + options if spec.head else options
    else:
        rendered = spec.head
    return rendered + spec.tail


def usage_error_text(spec: Spec, message: str = "") -> str:
    """What ``cliUsageError`` writes to stderr, byte for byte.

    With no message it is the page led by one blank line - the shape a refused
    flag printed before there was a library to print it.
    """
    lead = f"{message}\n\n" if message else "\n"
    return f"{lead}{page(spec)}\n\nNothing was changed.\n"


def no_args_text(spec: Spec) -> str:
    """The no-argument print: the credits, then the page, unless the script said not."""
    if spec.no_args_with_credits:
        return f"{spec.credits}\n\n{page(spec)}\n"
    return page(spec) + "\n"


def help_text(spec: Spec) -> str:
    """The ``-h`` print, which always leads with the credits."""
    return f"{spec.credits}\n\n{page(spec)}\n"


def missing_dir_text(spec: Spec, directory: str) -> str:
    """The input-folder refusal, worded the way every script worded it."""
    return f'Directory "{directory}" does not exist.\n\n{page(spec)}\n'


def args_out_of_range(count: int, minimum: int, maximum: int | None) -> bool:
    """The post-parse gate: is this positional count outside [min, max]?

    ``maximum`` None is no upper limit, which is how the bash gate reads an empty
    third argument.
    """
    return count < minimum or (maximum is not None and count > maximum)


class _Getopts:
    """bash's ``getopts`` in silent mode, over one list of words.

    Silent mode is the leading colon in the option string: an unknown letter
    comes back as ``?`` and a missing argument as ``:``, both with the offending
    letter in OPTARG, and nothing is printed. The character offset inside a
    cluster is state bash keeps privately and resets whenever OPTIND is assigned,
    so it lives here beside OPTIND rather than being derived from it.
    """

    def __init__(self, optstring: str, args: list[str]) -> None:
        self.args = args
        self.optind = 1
        self._offset = 0
        self._takes_arg = set()
        self._known = set()
        body = optstring[1:] if optstring.startswith(":") else optstring
        index = 0
        while index < len(body):
            letter = body[index]
            index += 1
            if index < len(body) and body[index] == ":":
                self._takes_arg.add(letter)
                index += 1
            self._known.add(letter)

    def _done(self) -> None:
        """What bash does to OPTIND on the call that finds no more options.

        It CLAMPS it to one past the last argument. That is not housekeeping: the
        optional-argument branch advances OPTIND by hand to claim the word it
        took, and when that word was the last one the claim would put OPTIND two
        past the end - where the next getopts pulls it back. The whole parse
        depends on it, because the loop then shifts by OPTIND-1 and a shift
        larger than $# would silently do nothing and leave the option word to be
        collected a second time, as a positional.
        """
        self.optind = min(self.optind, len(self.args) + 1)

    def next(self) -> tuple | None:
        """The next (letter, optarg), or None once the options are used up."""
        if self._offset == 0:
            if self.optind > len(self.args):
                self._done()
                return None
            word = self.args[self.optind - 1]
            if word == "--":
                self.optind += 1
                self._done()
                return None
            if not word.startswith("-") or word == "-":
                self._done()
                return None
            self._offset = 1
        word = self.args[self.optind - 1]
        letter = word[self._offset]
        self._offset += 1
        exhausted = self._offset >= len(word)
        if exhausted:
            self._offset = 0
            self.optind += 1
        if letter == ":" or letter not in self._known:
            return ("?", letter)
        if letter not in self._takes_arg:
            return (letter, "")
        if not exhausted:
            # Attached: "-j4" and "-xj4" both hand over what is left of the word.
            optarg = word[self._offset:]
            self._offset = 0
            self.optind += 1
            return (letter, optarg)
        if self.optind > len(self.args):
            return (":", letter)
        optarg = self.args[self.optind - 1]
        self.optind += 1
        return (letter, optarg)

    def peek(self) -> str:
        """``${!OPTIND}``: the word OPTIND names now, empty when it names none.

        Mid-cluster this is the cluster's own word, because OPTIND has not moved
        off it yet - which is what makes ``-th`` claim ``-th`` as -t's optional
        argument if the regex lets it.
        """
        if 1 <= self.optind <= len(self.args):
            return self.args[self.optind - 1]
        return ""


def _short_claims_next(word: str, tables: Tables, following: str) -> bool:
    """Does this short-option word claim the word after it as its argument?

    The cluster is read the way getopts reads it: the first letter that takes an
    argument ends it, and if anything is left in the word that IS the argument.
    An optional-argument letter claims the next word only when its regex matches
    it, which is the same test the parse loop makes.
    """
    for index in range(1, len(word)):
        letter = word[index]
        if letter in tables.has_arg:
            return index + 1 >= len(word)
        regex = tables.opt_regex.get(letter)
        if regex is not None:
            return index + 1 >= len(word) and bool(re.search(regex, following))
    return False


def _expand_long(words: list[str], tables: Tables) -> list[str]:
    """Rewrite every ``--name`` word into the letter it stands for.

    Long options are a layer ON TOP of getopts rather than a second parser: a
    word is translated here and the loop below sees the short form it always saw,
    so there is one set of rules about clustering, attached arguments and ``--``
    rather than two that can disagree.

    Two kinds of word are passed through untouched, because neither is an option
    being named: everything after a literal ``--``, and a word that is some
    option's ARGUMENT. The second is why this walk has to know which options take
    one - ``-p --weird`` hands "--weird" to -p, and always has.
    """
    out: list[str] = []
    index = 0
    while index < len(words):
        word = words[index]
        index += 1
        if word == "--":
            out.append(word)
            out.extend(words[index:])
            return out
        if not word.startswith("-") or word == "-":
            out.append(word)
            continue
        if not word.startswith("--"):
            out.append(word)
            # No word to claim is a MISSING argument, which the loop below
            # reports; inventing an empty one here would hide it.
            if index < len(words) and _short_claims_next(word, tables,
                                                        words[index]):
                out.append(words[index])
                index += 1
            continue
        name, sep, value = word[2:].partition("=")
        letter = tables.long_letter.get(name)
        if letter is None:
            raise UsageError(f"Unknown option: --{name}")
        regex = tables.opt_regex.get(letter)
        out.append("-" + letter)
        if not sep:
            # The word after it, when there is one, is that option's argument and
            # travels with it; when there is none the loop below says so.
            if index < len(words) and (
                    letter in tables.has_arg
                    or (regex is not None
                        and re.search(regex, words[index]))):
                out.append(words[index])
                index += 1
            continue
        # --name=value. An option that takes no argument has nowhere to put one,
        # and saying so is better than dropping it or reading it as a positional.
        if letter not in tables.has_arg and regex is None:
            raise UsageError(f"Option --{name} takes no argument")
        if regex is not None and not re.search(regex, value):
            raise UsageError(f'Option --{name} does not take "{value}"')
        out.append(value)
    return out


def parse(
    spec: Spec,
    argv: list[str],
    on_opt: Callable[[str, str], None] | None = None,
) -> Result:
    """``cliParse``: the one parse loop, over a copy of the command line.

    getopts on its own stops at the first word that is not an option, so the
    options would have to come first. Nobody types a command that way once they
    have typed it already and now want a flag on the end, so the loop is
    RESTARTED after each positional it collects until the line is used up. A
    literal ``--`` still ends the options for good, which is how a positional
    beginning with a dash stays reachable.
    """
    tables = build_tables(spec)
    optstring = build_opt_string(spec, tables)
    result = Result()
    # Every target exists before the loop runs, empty, the way the shell declares
    # them: a repeat option appends to an array that has to be there already, and a
    # scalar the command line never mentions still has to be readable afterwards.
    # The FIRST pair naming a variable decides whether it is a list, which is the
    # same rule the assignment below follows.
    for letter, name in var_pairs(spec):
        if name not in result.values:
            result.values[name] = [] if letter in tables.repeat else ""

    remaining = _expand_long(list(argv), tables)
    positionals: list[str] = []
    while remaining:
        state = _Getopts(optstring, remaining)
        while True:
            got = state.next()
            if got is None:
                break
            _one_option(spec, tables, result, state, got, on_opt)
        end_of_options = state.optind > 1 and remaining[state.optind - 2] == "--"
        remaining = remaining[state.optind - 1:]
        if end_of_options:
            positionals.extend(remaining)
            break
        if not remaining:
            break
        positionals.append(remaining[0])
        remaining = remaining[1:]
    result.positionals = positionals
    return result


def _one_option(spec, tables, result, state, got, on_opt) -> None:
    """One getopts result: every exit a parse can take is owned here."""
    letter, arg = got
    if letter == "h":
        raise HelpRequested()
    if letter == ":":
        # Silent getopts: a missing argument arrives as ":" with the offending
        # letter in OPTARG, which is all the message needs.
        raise UsageError(f"Option -{arg} requires an argument")
    if letter == "?":
        raise UsageError(f"Unknown option: -{arg}")

    if letter not in result.given:
        result.given.append(letter)
    regex = tables.opt_regex.get(letter)
    if regex is not None:
        # An optional argument getopts cannot express: the word the option is
        # followed by is claimed only when it is one, so a bare "-t" leaves the
        # default standing and "-t <inputDir>" leaves the directory where it was.
        following = state.peek()
        if re.search(regex, following):
            arg = following
            state.optind += 1
        else:
            arg = ""
    elif letter not in tables.has_arg:
        arg = "1"

    # Checked before the assignment and before the dispatch, so neither a
    # variable nor a script's own branch ever sees a value already refused. Only
    # a value somebody TYPED is checked: a flag carries none, and an optional
    # argument left unclaimed is empty, which means the default is standing
    # rather than that an empty string was given.
    if letter in tables.has_arg or regex is not None and arg:
        validate(letter, arg, tables)

    _assign(spec, result, letter, arg)
    if on_opt is not None:
        on_opt(letter, arg)


def var_pairs(spec: Spec) -> list[tuple]:
    """The ``letter:var`` pairs of cliOptVars, in the order they are written."""
    out = []
    for pair in spec.vars.split():
        letter, sep, name = pair.partition(":")
        if sep:
            out.append((letter, name))
    return out


def _assign(spec, result, letter: str, arg: str) -> None:
    """The plain assignment for this letter, if the spec declares one.

    The FIRST pair naming the letter wins and the loop stops there, so a letter
    written twice assigns once.
    """
    for pair_letter, name in var_pairs(spec):
        if pair_letter != letter:
            continue
        if isinstance(result.values.get(name), list):
            result.values[name].append(arg)
        else:
            result.values[name] = arg
        return
