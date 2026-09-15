"""Packaging and boundary tests.

These guard the parts of the plugin that AstrBot itself validates or relies on:

* ``metadata.yaml`` is well formed and pins the supported AstrBot range,
* ``_conf_schema.json`` and ``Settings`` agree on the config keys,
* no credential is baked into the plugin,
* only public/documented AstrBot modules are imported,
* ``terminate()`` is actually reachable (AstrBot skips it when a plugin defines
  ``__del__`` in its own class body).
"""

from __future__ import annotations

import ast
import json
import re
import unittest
from pathlib import Path

import yaml

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
SOURCE_FILES = sorted(
    path
    for path in PLUGIN_ROOT.rglob("*.py")
    if "tests" not in path.relative_to(PLUGIN_ROOT).parts
)

#: AstrBot modules this plugin is allowed to import. Everything under
#: ``astrbot.api`` is the documented plugin API. The two extra entries are
#: documented in the official plugin docs:
#: ``astrbot.core.agent.message`` for ``TextPart.mark_as_temp`` and
#: ``astrbot.core.star.filter.custom_filter`` as the fallback path behind
#: ``astrbot.api.event.filter.CustomFilter``.
ALLOWED_ASTRBOT_MODULES = (
    "astrbot.api",
    "astrbot.core.agent.message",
    "astrbot.core.star.filter.custom_filter",
)

SECRET_PATTERNS = (
    re.compile(r"\bsk-[A-Za-z0-9]{16,}"),
    re.compile(r"\bBearer\s+[A-Za-z0-9_\-.]{16,}"),
    re.compile(r"\bAIza[0-9A-Za-z_\-]{20,}"),
)

CONFIG_KEY_PATTERN = re.compile(r'(?:data\.get|seconds|count)\(\s*"([^"]+)"')


def _is_astrbot_module(name: str) -> bool:
    """Whether an absolute import targets AstrBot itself."""
    return name == "astrbot" or name.startswith("astrbot.")


def _iter_text_files() -> list[Path]:
    """Return every shipped text file of the plugin."""
    suffixes = {".py", ".json", ".yaml", ".yml", ".md", ".txt"}
    return sorted(
        path
        for path in PLUGIN_ROOT.rglob("*")
        if path.is_file() and path.suffix in suffixes and ".pytest_cache" not in path.parts
    )


class LayoutTests(unittest.TestCase):
    def test_required_plugin_files_exist(self) -> None:
        for name in ("main.py", "metadata.yaml", "_conf_schema.json", "requirements.txt", "README.md"):
            self.assertTrue((PLUGIN_ROOT / name).is_file(), f"{name} is missing")

    def test_plugin_name_matches_directory(self) -> None:
        metadata = yaml.safe_load((PLUGIN_ROOT / "metadata.yaml").read_text(encoding="utf-8"))
        self.assertEqual(metadata["name"], PLUGIN_ROOT.name)
        self.assertTrue(metadata["name"].isidentifier())


class MetadataTests(unittest.TestCase):
    def setUp(self) -> None:
        self.metadata = yaml.safe_load((PLUGIN_ROOT / "metadata.yaml").read_text(encoding="utf-8"))

    def test_astrbot_requires_required_fields(self) -> None:
        for field in ("name", "desc", "version", "author"):
            self.assertIn(field, self.metadata)
            self.assertIsInstance(self.metadata[field], str)
            self.assertTrue(self.metadata[field].strip())

    def test_version_range_matches_the_supported_astrbot_versions(self) -> None:
        from packaging.specifiers import SpecifierSet
        from packaging.version import Version

        specifier = SpecifierSet(self.metadata["astrbot_version"])
        self.assertIn(Version("4.28"), specifier)
        self.assertIn(Version("4.28.1"), specifier)
        self.assertNotIn(Version("4.27.0"), specifier)
        self.assertNotIn(Version("5.0.0"), specifier)

    def test_metadata_contains_no_credential(self) -> None:
        text = (PLUGIN_ROOT / "metadata.yaml").read_text(encoding="utf-8")
        for pattern in SECRET_PATTERNS:
            self.assertIsNone(pattern.search(text), f"credential-like value found: {pattern.pattern}")


class ConfigSchemaTests(unittest.TestCase):
    def setUp(self) -> None:
        self.schema = json.loads((PLUGIN_ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
        self.settings_source = (PLUGIN_ROOT / "companion_runtime" / "settings.py").read_text(
            encoding="utf-8",
        )

    def test_schema_types_are_supported_by_astrbot(self) -> None:
        supported = {
            "string",
            "text",
            "int",
            "float",
            "bool",
            "object",
            "list",
            "dict",
            "template_list",
            "file",
        }
        for key, field in self.schema.items():
            self.assertIn(field["type"], supported, f"{key} uses an unsupported type")
            self.assertTrue(field.get("description"), f"{key} has no description")

    def test_schema_and_settings_agree_on_keys(self) -> None:
        used = set(CONFIG_KEY_PATTERN.findall(self.settings_source))
        declared = set(self.schema)
        self.assertEqual(used - declared, set(), "Settings reads config keys missing from the schema")
        self.assertEqual(declared - used, set(), "the schema declares config keys Settings never reads")

    def test_token_field_is_secret_and_empty_by_default(self) -> None:
        field = self.schema["runtime_token"]
        self.assertTrue(field["secret"])
        self.assertEqual(field["default"], "")

    def test_default_runtime_url_matches_the_settings_default(self) -> None:
        """A schema default that disagrees with ``Settings`` is a silent outage.

        The WebUI writes the schema default into the config, so the two drifting
        apart means a fresh install talks to the wrong port and nothing anywhere
        says so. 8787 is the Runtime's own default (``RuntimeConfig.port``).
        """
        from companion_runtime.settings import DEFAULT_BASE_URL

        self.assertEqual(self.schema["runtime_base_url"]["default"], DEFAULT_BASE_URL)
        self.assertTrue(
            DEFAULT_BASE_URL.endswith(":8787"),
            f"the Runtime listens on 8787 by default, not {DEFAULT_BASE_URL}",
        )

    def test_token_is_never_logged(self) -> None:
        main_source = (PLUGIN_ROOT / "main.py").read_text(encoding="utf-8")
        for line in main_source.splitlines():
            if "token" in line and ("logger." in line or "self._log" in line):
                self.assertIn("configured", line, f"token may be logged: {line.strip()}")


class SecretScanTests(unittest.TestCase):
    def test_no_credential_is_baked_into_the_plugin(self) -> None:
        offenders: list[str] = []
        for path in _iter_text_files():
            text = path.read_text(encoding="utf-8", errors="replace")
            for pattern in SECRET_PATTERNS:
                if pattern.search(text):
                    offenders.append(f"{path.relative_to(PLUGIN_ROOT)}: {pattern.pattern}")
        self.assertEqual(offenders, [])


class ImportBoundaryTests(unittest.TestCase):
    def _imported_astrbot_modules(self) -> dict[str, set[str]]:
        """Return absolute ``astrbot*`` imports per source file (relative ones skipped)."""
        found: dict[str, set[str]] = {}
        for path in SOURCE_FILES:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                    if _is_astrbot_module(node.module):
                        found.setdefault(str(path.relative_to(PLUGIN_ROOT)), set()).add(node.module)
                elif isinstance(node, ast.Import):
                    for alias in node.names:
                        if _is_astrbot_module(alias.name):
                            found.setdefault(str(path.relative_to(PLUGIN_ROOT)), set()).add(alias.name)
        return found

    def test_only_documented_astrbot_modules_are_imported(self) -> None:
        for filename, modules in self._imported_astrbot_modules().items():
            for module in modules:
                allowed = any(
                    module == candidate or module.startswith(f"{candidate}.")
                    for candidate in ALLOWED_ASTRBOT_MODULES
                )
                self.assertTrue(allowed, f"{filename} imports non-allowlisted {module}")

    def test_pure_core_never_imports_astrbot(self) -> None:
        for filename, modules in self._imported_astrbot_modules().items():
            if filename.startswith("companion_runtime"):
                self.assertEqual(modules, set(), f"{filename} must stay AstrBot-free")

    def test_all_source_files_compile(self) -> None:
        for path in SOURCE_FILES:
            with self.subTest(path=str(path.relative_to(PLUGIN_ROOT))):
                compile(path.read_text(encoding="utf-8"), str(path), "exec")


class LifecycleContractTests(unittest.TestCase):
    def test_main_defines_terminate_but_not_dunder_del(self) -> None:
        tree = ast.parse((PLUGIN_ROOT / "main.py").read_text(encoding="utf-8"))
        plugin_class = next(
            node
            for node in tree.body
            if isinstance(node, ast.ClassDef) and node.name == "CompanionRuntimePlugin"
        )
        methods = {child.name for child in plugin_class.body if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef)}
        self.assertIn("terminate", methods)
        self.assertIn("initialize", methods)
        # AstrBot calls __del__ *instead of* terminate() when the plugin class
        # itself defines __del__, so this plugin must not define one.
        self.assertNotIn("__del__", methods)


if __name__ == "__main__":
    unittest.main()
