"""Derive the dead-code / dead-knob inventory from the source (item ⑦).

Reports:
  A. functions and methods with no caller anywhere in src/tests/scripts/plugin
  B. ``RuntimeConfig`` (and nested config) fields with no reader
  C. ``EventType`` members never emitted

Exclusions are explicit and printed, because "no caller" is only meaningful once the
framework entry points (FastAPI route handlers, argparse ``func=`` targets, exception
handlers) and the names that share an identifier with a *different* record (a property and a
module function called ``signed_intensity``, say) are taken out. A private name is only
kept alive by a mention in *code*: prose that talks about a dead helper must not hide it. The tool is a *narrowing*
aid, not a proof: it prints what it ignored so a human can audit it.

Section B is a report, not a to-do: those knobs are consciously deferred, because
``GET /config`` serialises every field and deleting one is a response-shape change.

Usage (from ``runtime/``)::

    .venv/bin/python ../scripts/dead_code_inventory.py
"""

from __future__ import annotations

import ast
import collections
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "runtime/src/companion_runtime"
EXTRA_ROOTS = [ROOT / "runtime/tests", ROOT / "scripts", ROOT / "astrbot_plugin_companion_runtime"]


def _sources() -> dict[str, str]:
    return {p.name: p.read_text(encoding="utf-8") for p in sorted(SRC.glob("*.py"))}


def _code_text() -> str:
    """Only Python: tests, scripts and the plugin. No prose."""
    parts = []
    for root in EXTRA_ROOTS:
        if not root.exists():
            continue
        for path in root.rglob("*.py"):
            if "__pycache__" in path.parts:
                continue
            parts.append(path.read_text(encoding="utf-8", errors="replace"))
    return "\n".join(parts)


def _all_other_text() -> str:
    parts = []
    for root in EXTRA_ROOTS:
        if not root.exists():
            continue
        for path in root.rglob("*.py"):
            if "__pycache__" in path.parts:
                continue
            parts.append(path.read_text(encoding="utf-8", errors="replace"))
    # The package's own non-.py surface (docs, README) counts as a mention too: a name
    # documented as public is not dead, even if nothing in Python calls it.
    for pattern in ("*.md", "*.toml", "*.json"):
        for path in ROOT.rglob(pattern):
            if ".venv" in path.parts or "node_modules" in path.parts:
                continue
            parts.append(path.read_text(encoding="utf-8", errors="replace"))
    return "\n".join(parts)


def _framework_targets(trees: dict[str, ast.Module]) -> set[str]:
    """Names the frameworks call by reference, not by name."""
    targets: set[str] = set()
    for tree in trees.values():
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for decorator in node.decorator_list:
                    text = ast.unparse(decorator)
                    if "router." in text or "app." in text or "filter." in text or "command(" in text:
                        targets.add(node.name)
            if isinstance(node, ast.Call):
                text = ast.unparse(node)
                if re.search(r"\bfunc\s*=", text) or "set_defaults" in text:
                    for arg in node.args:
                        if isinstance(arg, ast.Name):
                            targets.add(arg.id)
                    for keyword in node.keywords:
                        if isinstance(keyword.value, ast.Name):
                            targets.add(keyword.value.id)
    return targets


#: Names the heuristic cannot clear, each with the reason. An *unlisted* name appearing in
#: section A is a genuine finding; anything here has been read and cleared by hand.
KNOWN_NON_CANDIDATES: dict[str, str] = {
    "cmd_tick": "argparse target, referenced as a dict value in cli.py",
    "cmd_endogenous": "argparse target, referenced as a dict value in cli.py",
    "cmd_refresh": "argparse target, referenced as a dict value in cli.py",
    "cmd_consolidate": "argparse target, referenced as a dict value in cli.py",
    "cmd_backlog": "argparse target, referenced as a dict value in cli.py",
    "cmd_state": "argparse target, referenced as a dict value in cli.py",
    "cmd_verify": "argparse target, referenced as a dict value in cli.py",
    "cmd_checkpoint": "argparse target, referenced as a dict value in cli.py",
    "cmd_backup": "argparse target, referenced as a dict value in cli.py",
    "cmd_restore": "argparse target, referenced as a dict value in cli.py",
    "cmd_recover": "argparse target, referenced as a dict value in cli.py",
    "cmd_health": "argparse target, referenced as a dict value in cli.py",
    "__repr__": "dunder, called by repr()",
    "_http_transport": "passed as a callable value, never named in a call",
    "_call_round": "passed as a callable value, never named in a call",
    "signed_intensity": "shares its identifier with an unrelated property of the same name",
}


def zero_call_functions(sources: dict[str, str]) -> tuple[list[tuple[str, str, int]], set[str]]:
    trees = {name: ast.parse(body, filename=name) for name, body in sources.items()}
    # "Referenced", not "called": a helper passed as a value (``resolvable=self._is_resolvable``,
    # ``clock=clock``, a callback in a mapping) is used, and counting only Call nodes reported
    # it as dead. A FunctionDef's own name is not a Name node, so the definition cannot keep
    # itself alive here.
    referenced: collections.Counter[str] = collections.Counter()
    for name, tree in trees.items():
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
                referenced[node.id] += 1
            elif isinstance(node, ast.Attribute):
                referenced[node.attr] += 1

    class _Visitor(ast.NodeVisitor):
        def __init__(self, module: str) -> None:
            self.module = module
            self.owner: str | None = None
            self.out: list[tuple[str, str, int]] = []

        def visit_ClassDef(self, node: ast.ClassDef) -> None:
            previous, self.owner = self.owner, node.name
            for child in node.body:
                self.visit(child)
            self.owner = previous

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            self.out.append((self.module, node.name, node.lineno))
            previous, self.owner = self.owner, f"{self.owner}.{node.name}"
            for child in node.body:
                self.visit(child)
            self.owner = previous

        visit_AsyncFunctionDef = visit_FunctionDef  # type: ignore[assignment]

    defined: list[tuple[str, str, int]] = []
    for name, tree in trees.items():
        visitor = _Visitor(name)
        visitor.visit(tree)
        defined.extend(visitor.out)

    outside = _all_other_text()
    code = _code_text()
    framework = _framework_targets(trees)
    dead = []
    for module, name, lineno in defined:
        if referenced[name]:
            continue
        if name in framework or name in KNOWN_NON_CANDIDATES:
            continue
        # A mention in prose keeps a *public* name alive (it is documented API), but a
        # private name is only alive if code uses it. Counting docs for private names was
        # a real false negative: this very file's handoff notes named the dead
        # ``_last_proactive_context``, which hid it from the first sweep.
        haystack = code if name.startswith("_") else outside
        if re.search(rf"\b{re.escape(name)}\b", haystack):
            continue
        dead.append((module, name, lineno))
    return sorted(dead, key=lambda item: (item[0], item[2])), framework


def dead_config_fields(sources: dict[str, str]) -> list[tuple[str, str, int, str]]:
    """Config dataclass fields whose name never appears outside their own definition."""
    text_without_config = {name: body for name, body in sources.items() if name != "config.py"}
    outside = "\n".join(text_without_config.values())
    extra = _all_other_text()
    findings = []
    for name, body in sources.items():
        if name != "config.py":
            continue
        tree = ast.parse(body, filename=name)
        for cls in [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]:
            for stmt in cls.body:
                if not isinstance(stmt, ast.AnnAssign) or not isinstance(stmt.target, ast.Name):
                    continue
                field = stmt.target.id
                if field.startswith("_"):
                    continue
                if re.search(rf"\b{re.escape(field)}\b", outside):
                    continue
                # A dataclass field is also read by ``asdict``/``redact``/serialisation.
                if re.search(rf'["\']{re.escape(field)}["\']', extra):
                    continue
                findings.append((cls.name, field, stmt.lineno, ast.unparse(stmt.annotation)))
    return findings


def orphan_event_types(sources: dict[str, str]) -> list[tuple[str, int]]:
    body = sources["typing.py"]
    tree = ast.parse(body, filename="typing.py")
    members: list[tuple[str, str, int]] = []
    for cls in [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef) and n.name == "EventType"]:
        for stmt in cls.body:
            if isinstance(stmt, ast.Assign) and isinstance(stmt.targets[0], ast.Name):
                value = stmt.value
                if isinstance(value, ast.Constant) and isinstance(value.value, str):
                    members.append((stmt.targets[0].id, value.value, stmt.lineno))
    everything = "\n".join(sources.values()) + _all_other_text()
    orphans = []
    for name, literal, lineno in members:
        used_as_member = len(re.findall(rf"EventType\.{name}\b", everything))
        used_as_literal = len(re.findall(rf'["\']{re.escape(literal)}["\']', "\n".join(sources.values())))
        if used_as_member == 0 and used_as_literal <= 1:
            orphans.append((name, lineno))
    return orphans


def main() -> int:
    sources = _sources()
    dead, framework = zero_call_functions(sources)
    print(f"=== A. functions with no caller  ({len(dead)}) ===")
    for module, name, lineno in dead:
        print(f"  {module}:{lineno:5}  {name}")
    if not dead:
        print("  (none - clean)")
    print(
        f"    (excluded: {len(framework)} framework entry points, "
        f"{len(KNOWN_NON_CANDIDATES)} hand-cleared names)"
    )

    fields = dead_config_fields(sources)
    print(f"\n=== B. config fields with no reader  ({len(fields)}) ===")
    print("    (REPORT ONLY - consciously deferred; see HANDOFF, GET /config shape)")
    for cls, field, lineno, annotation in fields:
        print(f"  config.py:{lineno:5}  {cls}.{field}: {annotation}")

    orphans = orphan_event_types(sources)
    print(f"\n=== C. EventType members never emitted  ({len(orphans)}) ===")
    for name, lineno in orphans:
        print(f"  typing.py:{lineno:5}  {name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
