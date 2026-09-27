"""Scripts another interpreter runs.

Each file here is a whole program for a tool's own Python - the one ``pipx run``
builds from the script's inline metadata, or the one an installed tool already
has - and so imports nothing from medialib. They live inside the package so an
installed command finds them where it actually is.
"""

import os

__all__ = ["path_of"]


def path_of(name: str) -> str:
    """The absolute path of one of the scripts in this folder."""
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), name)
