"""Fleet children may share one non-secret TOML without losing per-person isolation."""

import importlib.util
import os
from pathlib import Path


def _load():
    scripts = Path(__file__).parents[2] / "scripts"
    spec = importlib.util.spec_from_file_location("runtime_fleet_config_test", scripts / "runtime_fleet.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_child_command_puts_config_before_serve(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("CR_FLEET_RUNTIME_CONFIG", "/config/runtime.toml")
    module = _load()
    base = tmp_path / "user-a"
    command = module.runtime_command(base, 8788)
    assert command[:4] == ["companion-runtime", "--base-dir", str(base), "--config"]
    assert command[4:] == [
        "/config/runtime.toml",
        "serve",
        "--host",
        "0.0.0.0",
        "--port",
        "8788",
    ]
