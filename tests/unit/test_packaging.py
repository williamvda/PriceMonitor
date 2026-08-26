"""Guards that every source subpackage actually ships in a built distribution."""

from pathlib import Path

import pytest
from setuptools import find_packages

_SRC = Path(__file__).parents[2] / "src"


@pytest.mark.skipif(not _SRC.is_dir(), reason="not running from a checkout")
def test_every_source_directory_is_an_importable_package():
    """A subpackage without __init__.py is silently dropped by packages.find,
    so an installed copy fails to import even though the checkout works."""
    found = set(find_packages(where=str(_SRC)))
    on_disk = {
        str(d.relative_to(_SRC)).replace("/", ".")
        for d in _SRC.rglob("*")
        if d.is_dir() and d.name != "__pycache__" and any(d.glob("*.py"))
    }
    assert on_disk - found == set()
