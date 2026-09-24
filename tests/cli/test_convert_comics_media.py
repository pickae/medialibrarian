"""Tier D for convert_comics.same_picture: real ImageMagick, real pixels.

tests/cli/test_convert_comics.py settles which copy is kept with the tools
stubbed. What only the real ones answer is that a lossy export of a picture
measures as that picture, and a different picture of the same size does not.
"""

import shutil
import subprocess

import pytest

from medialib.cli import convert_comics as cc
from medialib.lib import imagemagick

pytestmark = [
    pytest.mark.media,
    pytest.mark.skipif(shutil.which("magick") is None
                       and shutil.which("compare") is None,
                       reason="tier D needs a real ImageMagick"),
]


def _make(path, source):
    subprocess.run(imagemagick.convert_argv([source, "-resize", "400x560!",
                                             str(path)]), check=True)


def test_a_lossy_export_is_the_same_picture(tmp_path):
    _make(tmp_path / "015.png", "rose:")
    _make(tmp_path / "015.jpg", str(tmp_path / "015.png"))
    assert cc.same_picture(str(tmp_path / "015.jpg"), str(tmp_path / "015.png"))


def test_a_different_picture_is_not(tmp_path):
    _make(tmp_path / "015.png", "rose:")
    _make(tmp_path / "015.jpg", "logo:")
    assert not cc.same_picture(str(tmp_path / "015.jpg"),
                               str(tmp_path / "015.png"))
