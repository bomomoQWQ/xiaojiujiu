"""Settings normalization tests: defaults, coercion, and clamping."""

from __future__ import annotations

import os
import unittest
from unittest import mock

from companion_runtime.settings import (
    DEFAULT_BASE_URL,
    HARD_MAX_CONTEXT_TIMEOUT_S,
    OBSERVE_MODE_ALL,
    OBSERVE_MODE_WAKE,
    TOKEN_ENV_VAR,
    Settings,
)


class SettingsDefaultsTests(unittest.TestCase):
    def test_empty_mapping_yields_usable_defaults(self) -> None:
        settings = Settings.from_mapping(None)
        self.assertTrue(settings.usable)
        self.assertEqual(settings.base_url, DEFAULT_BASE_URL)
        self.assertEqual(settings.observe_mode, OBSERVE_MODE_WAKE)
        self.assertEqual(settings.issues, ())
        self.assertTrue(settings.inject_enabled)
        self.assertTrue(settings.outbox_enabled)

    def test_default_base_url_targets_the_runtime_default_port(self) -> None:
        """The Runtime serves 8787 by default; a wrong default fails invisibly."""
        self.assertEqual(DEFAULT_BASE_URL, "http://127.0.0.1:8787")

    def test_bool_disable_is_respected(self) -> None:
        settings = Settings.from_mapping({"enabled": "false"})
        self.assertFalse(settings.enabled)
        self.assertFalse(settings.usable)


class SettingsCoercionTests(unittest.TestCase):
    def test_webui_string_values_are_coerced(self) -> None:
        settings = Settings.from_mapping(
            {
                "runtime_base_url": "http://10.0.0.5:9000/",
                "context_timeout_ms": "250",
                "outbox_max_actions_per_poll": "4",
                "context_prefetch": "yes",
                "debug": "1",
            },
        )
        self.assertEqual(settings.base_url, "http://10.0.0.5:9000")
        self.assertAlmostEqual(settings.context_timeout_s, 0.25)
        self.assertEqual(settings.outbox_batch, 4)
        self.assertTrue(settings.context_prefetch)
        self.assertTrue(settings.debug)

    def test_invalid_numbers_fall_back_to_defaults(self) -> None:
        settings = Settings.from_mapping({"context_timeout_ms": "not-a-number"})
        self.assertAlmostEqual(settings.context_timeout_s, 0.4)
        self.assertNotIn("context_timeout_ms", " ".join(settings.issues))


class SettingsClampTests(unittest.TestCase):
    def test_context_timeout_is_hard_capped(self) -> None:
        settings = Settings.from_mapping({"context_timeout_ms": 60000})
        self.assertAlmostEqual(settings.context_timeout_s, HARD_MAX_CONTEXT_TIMEOUT_S)
        self.assertTrue(any("clamped" in issue for issue in settings.issues))

    def test_context_timeout_floor_is_enforced(self) -> None:
        settings = Settings.from_mapping({"context_timeout_ms": 1})
        self.assertAlmostEqual(settings.context_timeout_s, 0.05)

    def test_poll_interval_floor_is_enforced(self) -> None:
        settings = Settings.from_mapping({"outbox_poll_interval_ms": 5})
        self.assertAlmostEqual(settings.outbox_poll_interval_s, 0.2)

    def test_batch_and_concurrency_are_bounded(self) -> None:
        settings = Settings.from_mapping(
            {"outbox_max_actions_per_poll": 999, "outbox_max_concurrency": 0},
        )
        self.assertEqual(settings.outbox_batch, 16)
        self.assertEqual(settings.outbox_max_concurrency, 1)

    def test_backoff_ceiling_never_below_floor(self) -> None:
        settings = Settings.from_mapping(
            {"queue_base_backoff_ms": 5000, "queue_max_backoff_ms": 100},
        )
        self.assertGreaterEqual(
            settings.queue_max_backoff_s,
            settings.queue_base_backoff_s,
        )


class SettingsValidationTests(unittest.TestCase):
    def test_invalid_base_url_disables_runtime_calls(self) -> None:
        settings = Settings.from_mapping({"runtime_base_url": "127.0.0.1:8787"})
        self.assertEqual(settings.base_url, "")
        self.assertFalse(settings.usable)
        self.assertTrue(any("runtime_base_url" in issue for issue in settings.issues))

    def test_unknown_observe_mode_falls_back_with_issue(self) -> None:
        settings = Settings.from_mapping({"observe_mode": "everything"})
        self.assertEqual(settings.observe_mode, OBSERVE_MODE_WAKE)
        self.assertTrue(any("observe_mode" in issue for issue in settings.issues))

    def test_observe_all_is_accepted(self) -> None:
        settings = Settings.from_mapping({"observe_mode": "ALL"})
        self.assertEqual(settings.observe_mode, OBSERVE_MODE_ALL)

    def test_adapter_id_defaults_when_blank(self) -> None:
        self.assertEqual(Settings.from_mapping({"adapter_id": "   "}).adapter_id, "default")


class SettingsTokenTests(unittest.TestCase):
    def test_token_is_never_in_the_repr(self) -> None:
        settings = Settings.from_mapping({"runtime_token": "super-secret-value"})
        self.assertEqual(settings.token, "super-secret-value")
        self.assertNotIn("super-secret-value", repr(settings))

    def test_environment_variable_is_used_when_config_is_empty(self) -> None:
        with mock.patch.dict(os.environ, {TOKEN_ENV_VAR: "from-env"}):
            settings = Settings.from_mapping({})
        self.assertEqual(settings.token, "from-env")

    def test_config_token_wins_over_environment(self) -> None:
        with mock.patch.dict(os.environ, {TOKEN_ENV_VAR: "from-env"}):
            settings = Settings.from_mapping({"runtime_token": "from-config"})
        self.assertEqual(settings.token, "from-config")


if __name__ == "__main__":
    unittest.main()
