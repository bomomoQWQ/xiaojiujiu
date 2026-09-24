"""Mutation evidence for the plugin's assistant-report and whitelist wiring.

A fix is only believable if reverting it turns a *named* test red. This script
reverts exactly one clause at a time in ``astrbot_plugin_companion_runtime/main.py``,
runs the named test, restores the file, and fails loudly if a mutation survives.

It exists because the two failures it guards were both silent: the plugin logged
that it started and polled the Runtime while none of its hooks ever ran.

Usage::

    python scripts/mutation_assistant_report.py
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PLUGIN_DIR = ROOT / "astrbot_plugin_companion_runtime"
TARGETS = {
    "main": PLUGIN_DIR / "main.py",
    "settings": PLUGIN_DIR / "companion_runtime" / "settings.py",
}
TEST_FILE = "tests/test_plugin_integration.py"

#: ``(file key, label, anchor, replacement, test that must go red)``.
MUTATIONS = [
    (
        "main",
        "M1: on_llm_response reports nothing (a streamed turn is lost)",
        """            text = as_str(getattr(response, "completion_text", "")).strip()
            if text and self._report_assistant(event, text):
                self._mark_assistant_reported(event)""",
        """            text = as_str(getattr(response, "completion_text", "")).strip()
            text = ""  # MUTATION M1""",
        "test_streamed_turn_is_reported_from_the_llm_response",
    ),
    (
        "main",
        "M2: after_message_sent ignores the already-reported marker",
        """            if self._assistant_reported(event):
                return
            text = self._result_text(event)""",
        """            text = self._result_text(event)  # MUTATION M2""",
        "test_a_reported_turn_is_not_reported_twice",
    ),
    (
        "main",
        "M3: the whitelist self-check never runs",
        """        self._start()
        self._warn_if_whitelisted_out()""",
        """        self._start()  # MUTATION M3""",
        "test_an_unwhitelisted_plugin_warns_at_startup",
    ),
    (
        "main",
        "M4: the whitelist check does not compare plugin names",
        """        if entries == ["*"] or name in entries:
            return""",
        """        if True:  # MUTATION M4
            return""",
        "test_an_unwhitelisted_plugin_warns_at_startup",
    ),
    (
        "settings",
        "M5: every session resolves to the default Runtime (routing is dead)",
        """        for prefix, url in self.session_routes:
            if session.startswith(prefix):
                return url
        return None""",
        """        return None  # MUTATION M5""",
        "test_sessions_route_to_their_own_runtime",
    ),
    (
        "main",
        "M6: the context bridge ignores the route (reads the default cache)",
        """        target = self._targets.get(self._target_url(session))
        return target.bridge if target is not None else None""",
        """        target = self._targets.get(self._settings.base_url)  # MUTATION M6
        return target.bridge if target is not None else None""",
        "test_sessions_route_to_their_own_runtime",
    ),
    (
        "main",
        "M7: the registry is never consulted (a new person is never routed)",
        """        return self._registry_routes.get(session, self._settings.base_url)""",
        """        return self._settings.base_url  # MUTATION M7""",
        "test_the_registry_adds_a_target_without_a_restart",
    ),
    (
        "main",
        "M8: an unknown session falls back instead of waiting (blending is back)",
        """        return (
            self._settings.registry_configured
            and self._registry_seen
            and not self._route_known(session)
        )""",
        """        return False  # MUTATION M8""",
        "test_a_message_for_an_unprovisioned_person_waits_for_the_registry",
    ),
    (
        "main",
        "M9: auto-provision never asks the fleet (a newcomer never gets an instance)",
        """        if self._registry_transport is None or not self._settings.route_auto_provision:
            return""",
        """        if True:  # MUTATION M9
            return""",
        "test_an_unknown_person_is_provisioned_automatically",
    ),
    (
        "main",
        "M10: empty events are reported (pokes become 'the user said nothing')",
        """            text = as_str(getattr(event, "message_str", "")).strip()
            if not text:""",
        """            text = as_str(getattr(event, "message_str", "")).strip()
            if False:  # MUTATION M10""",
        "test_an_empty_message_event_is_not_reported",
    ),
]


def run_test(name: str) -> tuple[bool, str]:
    """Return whether the named test passed, plus pytest's last line."""
    environment = dict(os.environ)
    environment["PYTHONPATH"] = os.pathsep.join(
        ["tests/stubs", ".", environment.get("PYTHONPATH", "")],
    ).rstrip(os.pathsep)
    process = subprocess.run(
        [sys.executable, "-m", "pytest", TEST_FILE, "-q", "-k", name, "--no-header"],
        cwd=PLUGIN_DIR,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=environment,
    )
    tail = (process.stdout or process.stderr).strip().splitlines()[-1:]
    return process.returncode == 0, tail[0] if tail else ""


def main() -> int:
    """Run every mutation and report survivors."""
    originals = {key: path.read_text(encoding="utf-8") for key, path in TARGETS.items()}
    backups = {key: path.with_suffix(".py.mutation-backup") for key, path in TARGETS.items()}
    for key, path in TARGETS.items():
        shutil.copyfile(path, backups[key])

    survivors: list[str] = []
    try:
        for file_key, label, anchor, replacement, test_name in MUTATIONS:
            target = TARGETS[file_key]
            original = originals[file_key]
            if anchor not in original:
                print(f"{label}: ANCHOR MISSING (the code moved; update this script)")
                survivors.append(label)
                continue
            target.write_text(original.replace(anchor, replacement, 1), encoding="utf-8")
            try:
                passed, tail = run_test(test_name)
            finally:
                target.write_text(original, encoding="utf-8")
            print(f"{label}: {'GREEN (survived!)' if passed else 'RED (killed)'} -> {tail}")
            if passed:
                survivors.append(label)
    finally:
        for key, path in TARGETS.items():
            path.write_text(originals[key], encoding="utf-8")
            if path.read_text(encoding="utf-8") != originals[key]:
                print(f"restore failed for {path.name}; the backup is next to the file")
                return 2
            backups[key].unlink(missing_ok=True)

    if survivors:
        print(f"mutations survived: {survivors}")
        return 1
    print("all mutations killed; source restored byte-identically")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
