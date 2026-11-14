"""OTA failures use only temporary files, mocked builds and mocked networking."""
import importlib.util
import json
from pathlib import Path
import socket
import sys
import urllib.error
from unittest.mock import Mock

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
spec = importlib.util.spec_from_file_location("ota", ROOT / "tools/ota.py")
ota = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ota)


@pytest.fixture
def setup_ota(monkeypatch, tmp_path):
    config = tmp_path / "fake-config.json"
    config.write_text(json.dumps({"lump_token": "dummy-test-token", "lump_host": "192.0.2.1"}))
    app, art = tmp_path / "app.bin", tmp_path / "art.bin"
    app.write_bytes(b"app"); art.write_bytes(b"art")
    monkeypatch.setattr(ota, "CONFIG", str(config))
    monkeypatch.setattr(ota, "APP", str(app))
    monkeypatch.setattr(ota, "ART", str(art))
    monkeypatch.setattr(sys, "argv", ["ota.py"])
    build = Mock(return_value=Mock(returncode=0))
    monkeypatch.setattr(ota.subprocess, "run", build)
    monkeypatch.setattr(ota, "resolve", lambda host: "192.0.2.1")
    return build


@pytest.mark.parametrize("error", [ConnectionRefusedError("refused"), TimeoutError("timed out"),
                                   urllib.error.URLError("unreachable"), socket.gaierror("no such host")])
def test_default_env_connection_failure_explains_usb(setup_ota, monkeypatch, error):
    monkeypatch.setattr(ota.urllib.request, "urlopen", Mock(side_effect=error))
    if isinstance(error, socket.gaierror):
        monkeypatch.setattr(ota, "resolve", Mock(side_effect=error))
    with pytest.raises(SystemExit, match="tools/flash.sh esp32-32e"):
        ota.main()


def test_explicit_classic_rejected_before_config_build_or_network(setup_ota, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["ota.py", "--env", "esp32-32e"])
    exists = ota.os.path.exists
    def check_path(path):
        assert path != ota.CONFIG, "read config"
        return exists(path)
    monkeypatch.setattr(ota.os.path, "exists", check_path)
    monkeypatch.setattr(ota, "resolve", Mock(side_effect=AssertionError("network")))
    with pytest.raises(SystemExit, match="tools/flash.sh esp32-32e"):
        ota.main()
    setup_ota.assert_not_called()


def test_http_auth_failure_keeps_pairing_diagnostic(setup_ota, monkeypatch):
    monkeypatch.setattr(ota, "request", lambda *a, **k: (401, "no token"))
    with pytest.raises(SystemExit, match="pairing token"):
        ota.main()
