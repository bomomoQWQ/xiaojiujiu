"""Static production-entrypoint guards for the v2 cut-over."""

from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).parents[1] / "src" / "companion_runtime"


def _function_source(path: Path, name: str) -> str:
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    node = next(
        item for item in tree.body if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name == name
    )
    return ast.get_source_segment(source, node) or ""


def test_serve_builds_and_mounts_v2_and_never_schedules_legacy_endogenous() -> None:
    source = _function_source(ROOT / "cli.py", "cmd_serve")
    assert "build_v2_composition(" in source
    assert "ConcreteLegacyRuntimeV2Bridge(runtime)" in source
    assert "runtime.v2_coordinator = v2_composition.coordinator" in source
    assert "v2_composition=v2_composition" in source
    assert "round_callback=run_v2_round" in source
    assert "round_callback=runtime.endogenous_round" not in source
    assert "checkpoint(runtime.db" not in source


def test_v2_scheduler_wrapper_generates_id_and_passes_allowed_elapsed_time() -> None:
    source = _function_source(ROOT / "cli.py", "cmd_serve")
    assert 'decision_id=new_id("decision")' in source
    assert "elapsed_allowed_seconds=elapsed" in source
    assert "legacy_user_model_enabled = False" in source
    assert "legacy_endogenous_enabled = False" in source


def test_production_composition_modules_do_not_import_legacy_learning_or_motivation() -> None:
    for filename in ("composition_v2.py", "runtime_repository_v2.py", "runtime_v2.py"):
        source = (ROOT / filename).read_text(encoding="utf-8")
        tree = ast.parse(source)
        imports = {
            node.module or ""
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
        }
        imports.update(
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        )
        assert not any(name.endswith(".user_model") or name == "user_model" for name in imports)
        assert not any(name.endswith(".motivation") or name == "motivation" for name in imports)
