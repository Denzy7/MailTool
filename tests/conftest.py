import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


class FakeJob:
    def __init__(self):
        self.lines = []
        self.cancelled = False

    def log(self, text, level="info"):
        self.lines.append((level, str(text)))

    def progress(self, *a):
        pass

    def check(self):
        pass

    def track_socket(self, s):
        pass

    def text(self):
        return "\n".join(t for _, t in self.lines)


@pytest.fixture
def job():
    return FakeJob()


@pytest.fixture(autouse=True)
def isolated_dirs(tmp_path, monkeypatch):
    """Keep settings, caches and logs out of the real home folder."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "cfg"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "data"))
    yield
