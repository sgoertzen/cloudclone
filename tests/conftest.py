import os
import tempfile

# store/main read these at import time, so point them somewhere safe before anything imports them.
_root = tempfile.mkdtemp(prefix="cloudclone-test-")
os.environ["CONFIG_DIR"] = os.path.join(_root, "config")
os.environ["DATA_DIR"] = os.path.join(_root, "data")
os.makedirs(os.environ["CONFIG_DIR"], exist_ok=True)

import pytest  # noqa: E402

import store  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_store(tmp_path, monkeypatch):
    """Fresh, empty state and directories for every test."""
    config, data = tmp_path / "config", tmp_path / "data"
    (config / "rclone").mkdir(parents=True)
    data.mkdir()
    monkeypatch.setattr(store, "CONFIG_DIR", str(config))
    monkeypatch.setattr(store, "DATA_DIR", str(data))
    monkeypatch.setattr(store, "STATE_FILE", str(config / "state.json"))
    monkeypatch.setattr(store, "RCLONE_DIR", str(config / "rclone"))
    monkeypatch.setattr(store, "_state", None)
    yield
