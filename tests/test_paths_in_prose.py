"""A path named in a comment or a docstring, and whether it is still there.

Item 8.2's grep, kept rather than run once: prose is the part of a move nothing
else checks, and the one-line version of this found 60 dead paths at item 4.1 and
35 more after phases 6 and 7.

Two rules, both without an allow-list to go stale:

  * a path under this repository's own directories has to exist;
  * no prose names a `.sh` or a `.bash`, because the repository holds one of each
    and it is `tests/data/toolstub`, which has neither suffix.

A path that is somebody else's - ebook2audiobook's `app.py`, the
`sitecustomize.py` this package writes into a venv it built - is neither, and is
left alone by both.
"""

import ast
import io
import re
import tokenize

import pytest

from tests import blackbox

pytestmark = pytest.mark.pure

_OURS = re.compile(r"(?<![\w./-])((?:medialib|tests|docs)/[\w./-]*[\w.])")
_SHELL = re.compile(r"(?<![\w./-])[*\w./-]+\.(?:sh|bash)(?![\w])")


def _prose(path) -> str:
    text = path.read_text(encoding="utf-8")
    out = [t.string for t in tokenize.generate_tokens(io.StringIO(text).readline)
           if t.type == tokenize.COMMENT]
    for node in ast.walk(ast.parse(text)):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef
                      | ast.AsyncFunctionDef):
            doc = ast.get_docstring(node)
            if doc:
                out.append(doc)
    return "\n".join(out)


def _modules():
    return sorted(p for root in ("medialib", "tests")
                  for p in (blackbox.REPO / root).rglob("*.py"))


def test_every_path_of_ours_that_prose_names_exists():
    missing = []
    for module in _modules():
        for named in _OURS.findall(_prose(module)):
            if not (blackbox.REPO / named.rstrip(".,;:")).exists():
                missing.append("%s: %s" % (module.name, named))
    assert missing == []


def test_no_prose_names_a_shell_file():
    named = []
    for module in _modules():
        named += ["%s: %s" % (module.name, m) for m in _SHELL.findall(_prose(module))]
    assert named == []
