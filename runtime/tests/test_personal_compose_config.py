"""The documented personal compose is Runtime-only and consumes one safe config file."""

from pathlib import Path

ROOT = Path(__file__).parents[2]


def test_personal_compose_mounts_the_runtime_config_and_does_not_manage_astrbot() -> None:
    text = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert './config/runtime.toml:/config/runtime.toml:ro' in text
    assert 'command: ["--config", "/config/runtime.toml", "serve"]' in text
    assert "CR_STORAGE__DSN" in text
    assert "CR_SEMANTIC_API_KEY" in text
    assert "\n  astrbot:" not in text
    assert "/AstrBot/data/plugins" not in text
    assert "./astrbot_plugin_companion_runtime:" not in text


def test_the_shipped_template_contains_no_secret_and_exposes_common_knobs() -> None:
    text = (ROOT / "config" / "runtime.example.toml").read_text(encoding="utf-8")
    assert "CR_SEMANTIC_API_KEY=" not in text
    assert "postgresql://runtime:" not in text
    for marker in (
        "[prompts]",
        "conversation_context =",
        "proactive_render =",
        "[semantic]",
        'base_url = ""',
        'model = ""',
        "[semantic.prompts]",
        "deep_refresh =",
        "explain_state =",
        "[langchao]",
        "night_send_penalty =",
    ):
        assert marker in text
