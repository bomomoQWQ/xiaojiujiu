"""Implicit personal config discovery is convenient; explicit paths stay strict."""

from __future__ import annotations

from pathlib import Path

import pytest

from companion_runtime import cli


def _args(*extra: str):
    return cli.build_parser().parse_args([*extra, "config"])


def test_repo_style_runtime_toml_is_discovered(monkeypatch, tmp_path: Path) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "runtime.toml").write_text('[server]\nport = 9123\n', encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    assert cli._resolve_config(_args()).server.port == 9123


def test_missing_implicit_runtime_toml_uses_defaults(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    assert cli._resolve_config(_args()).server.port == 8787


def test_explicit_config_wins_over_the_discovered_one(monkeypatch, tmp_path: Path) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "runtime.toml").write_text('[server]\nport = 9123\n', encoding="utf-8")
    explicit = tmp_path / "explicit.toml"
    explicit.write_text('[server]\nport = 9456\n', encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    assert cli._resolve_config(_args()).server.port == 9123
    assert cli._resolve_config(_args("--config", str(explicit))).server.port == 9456


def test_missing_explicit_config_is_still_an_error(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        cli._resolve_config(_args("--config", str(tmp_path / "missing.toml")))
