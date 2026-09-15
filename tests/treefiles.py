"""The `.tree` fixture format: a folder's whole shape as one comparable value.

A recorded case is two files - what went in and what came out - each the
verbatim output of

    tree -a -n --charset ascii --noreport <folder>

which is the form `clean-folder-structure` writes its own `-s` artifacts in, so
a fixture and the command's own preview are the same bytes and can be diffed
against each other.

The format carries depth in ONE place: the column the branch marker sits at,
four characters per level. Nothing else in a rendering is load-bearing - not the
order the siblings come in, which is `tree`'s own collation, and not the
trailing `/` it would print with `-F`, which these renderings do not ask for. A
node is a folder when something sits one level deeper than it, and that is the
only way the format says so.

Two commands are recorded this way and they want opposite things of the fixture.
`clean-folder-structure` reads names and never contents, so a reconstruction of
`touch`ed files exercises it fully. `ingest-movies` asks a probe how long a film
runs, which an empty file cannot answer - :func:`reconstruct` therefore says
which nodes it made, and a caller that needs bytes in them can put them there.

The parsing and the rendering are kept together here rather than beside either
caller, because a case is only a case while the two agree: a second reader that
drifted by a space would compare two formats and report it as a product change.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

# The flags every rendering here is made with. `-a` so a dotfile is not quietly
# outside the claim, `-n` so colour codes never reach a fixture, ASCII so the
# markers are the same bytes on every host, and `--noreport` so the trailing
# count does not have to be edited whenever a case grows a file.
TREE_FLAGS = ("-a", "-n", "--charset", "ascii", "--noreport")

_MARKERS = ("|-- ", "`-- ")
_INDENT = 4


def snapshot(folder) -> str:
    """The layout as `tree` renders it, rooted at the folder's own basename.

    `tree` and not a walk written here: it is the tool the commands themselves
    write their before/after artifacts with, so rendering the comparison any
    other way would compare two different formats.
    """
    folder = Path(folder)
    done = subprocess.run(
        ["tree", *TREE_FLAGS, folder.name],
        cwd=str(folder.parent), capture_output=True, text=True, check=True)
    return done.stdout


def parse(text: str) -> list[tuple[int, str]]:
    """The (depth, name) pairs of a `tree` rendering, depth 0 being the root."""
    lines = text.splitlines()
    nodes = [(0, lines[0])]
    for line in lines[1:]:
        if not line.strip():
            continue
        columns = [line.find(marker) for marker in _MARKERS]
        found = [column for column in columns if column >= 0]
        if not found:
            continue
        column = min(found)
        nodes.append((column // _INDENT + 1, line[column + _INDENT:]))
    return nodes


def reconstruct(tree_file, destination) -> str:
    """Recreate a recorded layout under ``destination``, and answer the root's
    name.

    A node is a folder when something sits one level deeper than it; everything
    else is a file, and is created empty. A caller that needs a file to have
    contents finds it at the path this returns the root of.
    """
    nodes = parse(Path(tree_file).read_text(encoding="utf-8"))
    deeper = {index for index, (depth, _) in enumerate(nodes)
              if any(other == depth + 1
                     for other, _ in _until_shallower(nodes, index, depth))}
    stack: list[str] = []
    for index, (depth, name) in enumerate(nodes):
        stack = stack[:depth] + [name]
        full = Path(destination).joinpath(*stack)
        if index in deeper:
            full.mkdir(parents=True, exist_ok=True)
        else:
            full.parent.mkdir(parents=True, exist_ok=True)
            full.touch()
    return nodes[0][1]


def _until_shallower(nodes, index, depth):
    """The nodes after ``index`` that are still inside it - its subtree."""
    for other_depth, name in nodes[index + 1:]:
        if other_depth <= depth:
            return
        yield other_depth, name


def paths(tree_file) -> list[str]:
    """A recorded layout as sorted relative paths, the root itself left out.

    The rendering's own comparison is byte for byte, and where that can be had
    it is the better one - it pins the format as well as the contents. It cannot
    always: `tree` orders siblings by the host's collation, so two hosts render
    one layout two ways, and it is a binary besides, which a case comparing a
    layout does not otherwise need.

    Paths carry the whole of what a naming pass did - every rename, every folder
    it made, every file it moved - and carry it in an order no host has an
    opinion about. What they give up is the shape of the rendering, which is
    what :func:`snapshot` is still for.
    """
    return sorted(_joined(parse(Path(tree_file).read_text(encoding="utf-8"))))


def _joined(nodes) -> list[str]:
    stack: list[str] = []
    found = []
    for depth, name in nodes:
        stack = stack[:depth] + [name]
        if depth:
            found.append("/".join(stack[1:]))
    return found


def paths_on_disk(root) -> list[str]:
    """What :func:`paths` answers, read off a real folder instead of a
    recording. `/` on every host, so the two are the same value."""
    root = Path(root)
    return sorted(path.relative_to(root).as_posix()
                  for path in root.rglob("*"))
