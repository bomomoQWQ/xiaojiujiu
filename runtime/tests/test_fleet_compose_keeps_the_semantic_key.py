"""Regenerating the fleet compose must not drop the semantic provider's key.

``docs/部署与运维.md`` warned for a while that the generator "does not write
``CR_SEMANTIC_API_KEY``" and that a key written into the generated ``fleet-v2.yml`` would be
wiped by the next run. That warning was *false*, and it was the kind of false that costs a
deployment: it told an operator their working stack was fragile in a way it is not, and it
pointed them at a fix (a secrets file) that would itself have broken it.

What is actually true, and what this test pins down:

* the generator copies the base compose's ``runtime`` environment block line by line
  (``base_environment``), so a key that lives in ``astrbot.yml`` -- where it belongs, since
  that is the file a deployment authors -- survives regeneration verbatim. Measured against
  the live stack on 2026-10-07: the value hashes in `astrbot.yml` and in a freshly generated
  file were identical;
* therefore the real precondition is "the key must be in the *base* compose". A key added
  only to the generated file is the case that disappears, which is worth knowing and is why
  the docs now say so;
* ``CR_CONVERSATION_ID`` is the one base variable deliberately *not* copied: each Runtime
  child gets its own session from the supervisor, and inheriting the base value would make
  every child write into one conversation.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

SCRIPTS = Path(__file__).parents[2] / "scripts"

FAKE_KEY = "not-a-real-key"

BASE_COMPOSE = f"""services:
  runtime:
    image: example-runtime:test
    container_name: example-runtime
    environment:
      TZ: Asia/Shanghai
      CR_CONVERSATION_ID: default
      CR_SEMANTIC_PROVIDER: remote_api
      CR_SEMANTIC_API_KEY: {FAKE_KEY}
      CR_STORAGE__DSN: postgresql://runtime:secret@db/runtime
"""


def _load_generator():
    """Import scripts/build_runtime_fleet.py without installing it as a package."""
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    spec = importlib.util.spec_from_file_location(
        "build_runtime_fleet_under_test", SCRIPTS / "build_runtime_fleet.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_base_environment_copies_the_key_and_drops_the_session(tmp_path: Path) -> None:
    generator = _load_generator()
    compose = tmp_path / "astrbot.yml"
    compose.write_text(BASE_COMPOSE, encoding="utf-8")

    image, env = generator.base_environment(compose)

    assert image == "example-runtime:test"
    joined = "\n".join(env)
    assert f"CR_SEMANTIC_API_KEY: {FAKE_KEY}" in joined, "the base key was not copied"
    assert "CR_SEMANTIC_PROVIDER: remote_api" in joined
    assert "CR_CONVERSATION_ID" not in joined, (
        "the base session must not be inherited: every child gets its own from the supervisor"
    )


def test_rendered_fleet_compose_still_carries_the_key(tmp_path: Path) -> None:
    """The regression this exists for: regeneration keeping the key."""
    generator = _load_generator()
    compose = tmp_path / "astrbot.yml"
    compose.write_text(BASE_COMPOSE, encoding="utf-8")
    image, env = generator.base_environment(compose)

    text = generator.render(image, env, compose, tmp_path)

    assert text.count(f"CR_SEMANTIC_API_KEY: {FAKE_KEY}") == 1
    assert "CR_CONVERSATION_ID" not in text
    # The supervisor's own schema/DSN come from elsewhere; the base DSN is copied as-is.
    assert "CR_STORAGE__DSN: postgresql://runtime:secret@db/runtime" in text


def test_rendered_fleet_can_mount_one_common_personal_config(tmp_path: Path) -> None:
    """Every child reads the same safe TOML; its identity/schema still come from env."""
    generator = _load_generator()
    compose = tmp_path / "astrbot.yml"
    compose.write_text(BASE_COMPOSE, encoding="utf-8")
    config = tmp_path / "runtime.toml"
    config.write_text('[langchao]\nshadow_enabled = true\n', encoding="utf-8")
    image, env = generator.base_environment(compose)

    text = generator.render(image, env, compose, tmp_path, runtime_config=config.resolve())

    assert "CR_FLEET_RUNTIME_CONFIG: /config/runtime.toml" in text
    assert f"- {config.resolve()}:/config/runtime.toml:ro" in text
