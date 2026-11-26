"""The music library's layout: who names ingest-music's folders and files.

``keep``, the default, keeps the layout the download came in: beets only
tags the tracks where they lie, and ``clean-folder-structure`` cleans the names.
Every other layout hands the naming to beets instead. Its shipped config moves
each matched album to the path the config's ``paths:`` describe, and the run then
does not clean the names, so they stay the ones beets gave.

  * ``plex`` - ``Artist/Album/01 - Title``, and ``101 - Title`` on a
    multi-disc album, whose discs all share the album's folder. Compilations go
    under ``Various Artists``.
  * ``jellyfin`` - the same, except that each disc of a multi-disc album gets a
    ``Disc N`` subfolder.
  * ``beets`` - beets' own default paths, ``Artist/Album/01 Title``, with
    compilations under ``Compilations``.

Both servers read the embedded tags first. The names matter less than the
rule of one album per folder, which every layout except ``keep`` follows.
"""

import os
from typing import NamedTuple

__all__ = ["Layout", "LAYOUTS", "DEFAULT", "SETTING_VARIABLE", "chosen",
           "config_path", "beet_options", "import_flags"]

# The setting that picks the layout.
SETTING_VARIABLE = "musicLayout"


class Layout(NamedTuple):
    """One layout: its name, the shipped beets config it runs with, and
    whether beets names the library (it moves the albums, and the run does not
    clean the names afterwards)."""

    name: str
    config_file: str
    beets_names: bool


LAYOUTS = {
    layout.name: layout for layout in (
        Layout("keep", "beets.yaml", False),
        Layout("plex", "beets-plex.yaml", True),
        Layout("jellyfin", "beets-jellyfin.yaml", True),
        Layout("beets", "beets-defaults.yaml", True),
    )
}

DEFAULT = "keep"


def chosen(value: str) -> Layout:
    """The layout ``value`` names. An empty value is the default layout. A
    name that is not a layout raises ValueError, with the names that are."""
    name = (value or DEFAULT).strip().lower()
    if name not in LAYOUTS:
        raise ValueError('"%s" is not a music layout: use %s'
                         % (value, ", ".join(LAYOUTS)))
    return LAYOUTS[name]


def config_path(layout: Layout, config_dir: str) -> str:
    """The shipped beets config the layout runs with."""
    return os.path.join(config_dir, layout.config_file)


def beet_options(layout: Layout, library_dir: str) -> list[str]:
    """The global ``beet`` options that come before ``import``.

    A layout that moves albums names the library folder outright with ``-d``.
    It does not rely on the config's ``directory: .``, because beets may resolve
    a relative ``directory`` against the config file's folder rather than the
    folder it runs in. A move would then empty the library into the package.
    """
    if layout.beets_names:
        return ["-d", os.path.abspath(library_dir)]
    return []


def import_flags(layout: Layout, retry: bool = False) -> str:
    """``beet import``'s short flags: ``-m`` moves the albums where the config
    says, and ``-C`` leaves them where they are. ``-q`` is quiet. A retry also
    adds ``-I``, so beets does not skip the folder it has just recorded."""
    move = "m" if layout.beets_names else "C"
    return "-" + move + ("I" if retry else "i") + "q"
