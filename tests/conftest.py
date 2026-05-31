"""Shared test fixtures.

``local_media`` resolves real DJI files staged under
``tests/fixtures/media/local/`` (gitignored — privacy + size). Tests that depend
on them ``pytest.skip`` cleanly when the files are absent (CI / fresh clones), so
the committed suite stays green everywhere while still exercising real footage
where it is available.
"""

from __future__ import annotations

from pathlib import Path

import pytest

LOCAL_MEDIA_DIR = Path(__file__).parent / "fixtures" / "media" / "local"


@pytest.fixture
def local_media():
    def _resolve(name: str) -> Path:
        path = LOCAL_MEDIA_DIR / name
        if not path.exists():
            pytest.skip(f"local-only real fixture missing: {path}")
        return path

    return _resolve
