"""Tests for the client configuration file.

The file is the primary way to define a character, so the tests here are mostly
about *refusing* things: a half-applied config, a typo in an axis name, or a
credential pasted into the file are all worse than a loud failure.
"""

from __future__ import annotations

import json

import pytest

from cf.config import (
    DEFAULT_API_KEY_ENV,
    VALUE_AXES,
    ClientConfig,
    ConfigError,
    _looks_like_a_credential,
    example_toml,
    load_client_config,
    write_example,
)


def write(tmp_path, name: str, text: str):
    """Write a config file and return its path."""
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


class TestLoading:
    """Reading the two supported formats."""

    def test_toml_is_read(self, tmp_path) -> None:
        """The primary format."""
        path = write(tmp_path, "cf.toml", '[llm]\nmodel = "m"\n\n[clock]\ntime_scale = 4.0\n')
        config = load_client_config(path)
        assert config.llm.model == "m"
        assert config.clock.time_scale == 4.0

    def test_json_is_read(self, tmp_path) -> None:
        """JSON works too, for generated configs."""
        path = write(tmp_path, "cf.json", json.dumps({"llm": {"model": "m"}}))
        assert load_client_config(path).llm.model == "m"

    def test_the_example_suffix_is_accepted(self, tmp_path) -> None:
        """``cf.toml.example`` is the conventional template name and must load."""
        path = write(tmp_path, "cf.toml.example", '[llm]\nmodel = "m"\n')
        assert load_client_config(path).llm.model == "m"

    def test_missing_file_says_so(self, tmp_path) -> None:
        """A wrong path is a clear message."""
        with pytest.raises(ConfigError, match="not found"):
            load_client_config(tmp_path / "nope.toml")

    def test_bad_extension_is_named(self, tmp_path) -> None:
        """An unsupported extension names itself."""
        path = write(tmp_path, "cf.yaml", "llm: {}\n")
        with pytest.raises(ConfigError, match="unsupported extension"):
            load_client_config(path)

    def test_malformed_toml_is_reported(self, tmp_path) -> None:
        """A syntax error surfaces as a config error, not a traceback."""
        path = write(tmp_path, "cf.toml", "[llm\nmodel = 1\n")
        with pytest.raises(ConfigError, match="not valid TOML"):
            load_client_config(path)

    def test_a_non_table_section_is_refused(self, tmp_path) -> None:
        """``llm = 3`` is an error, not a silently ignored section."""
        path = write(tmp_path, "cf.toml", "llm = 3\n")
        with pytest.raises(ConfigError, match=r"\[llm\]"):
            load_client_config(path)

    def test_defaults_apply_for_absent_sections(self, tmp_path) -> None:
        """An almost-empty file still yields a usable configuration."""
        path = write(tmp_path, "cf.toml", "# nothing here\n")
        config = load_client_config(path)
        assert config.llm.api_key_env == DEFAULT_API_KEY_ENV
        assert config.clock.time_scale == 1.0
        assert config.harness.seed == 20260915


class TestCredentials:
    """A config file is the artefact that ends up in a backup."""

    @pytest.mark.parametrize(
        "key",
        ["api_key", "apikey", "token", "secret", "password", "access_key", "bearer_token"],
    )
    def test_credential_shaped_keys_are_refused(self, tmp_path, key) -> None:
        """Each credential name is rejected."""
        path = write(tmp_path, "cf.toml", f'[llm]\n{key} = "whatever"\n')
        with pytest.raises(ConfigError, match="looks like a credential"):
            load_client_config(path)

    @pytest.mark.parametrize("key", ["max_tokens", "tokens", "api_key_env", "timeout_s", "temperature"])
    def test_ordinary_keys_are_not_mistaken_for_credentials(self, tmp_path, key) -> None:
        """``max_tokens`` is a sampling cap, not a secret.

        A check that refuses to load an ordinary config file is worse than no
        check, so the detection is on word boundaries rather than substrings.
        """
        value = '"x"' if key.endswith("_env") else "1"
        path = write(tmp_path, "cf.toml", f"[llm]\n{key} = {value}\n")
        load_client_config(path)

    def test_the_error_never_echoes_the_value(self, tmp_path) -> None:
        """The message names the key's path, never its contents."""
        path = write(tmp_path, "cf.toml", '[llm]\napi_key = "sk-do-not-print-me"\n')
        with pytest.raises(ConfigError) as excinfo:
            load_client_config(path)
        assert "sk-do-not-print-me" not in str(excinfo.value)
        assert "llm.api_key" in str(excinfo.value)

    def test_the_predicate_itself(self) -> None:
        """The word-boundary rule, stated directly."""
        assert _looks_like_a_credential("api_key") is True
        assert _looks_like_a_credential("max_tokens") is False
        assert _looks_like_a_credential("api_key_env") is False
        assert _looks_like_a_credential("timeout_s") is False


class TestValueAxes:
    """The eight axes are the character; typos must not pass."""

    def test_overrides_are_kept(self, tmp_path) -> None:
        """Requested axes land on the persona."""
        path = write(tmp_path, "cf.toml", "[persona.values]\nuser_care = 0.95\ncuriosity = 0.2\n")
        persona = load_client_config(path).persona
        assert persona.values == {"user_care": 0.95, "curiosity": 0.2}

    def test_unspecified_axes_keep_their_defaults(self, tmp_path) -> None:
        """The resolved profile is complete, not just the overrides."""
        path = write(tmp_path, "cf.toml", "[persona.values]\nuser_care = 0.95\n")
        resolved = load_client_config(path).persona.resolved_values()
        assert set(resolved) == set(VALUE_AXES)
        assert resolved["user_care"] == 0.95
        assert resolved["boundary_respect"] == pytest.approx(0.88)

    def test_an_unknown_axis_names_the_alternatives(self, tmp_path) -> None:
        """A typo lists the real axes instead of being ignored."""
        path = write(tmp_path, "cf.toml", "[persona.values]\nwarmth = 0.9\n")
        with pytest.raises(ConfigError) as excinfo:
            load_client_config(path)
        message = str(excinfo.value)
        assert "warmth" in message
        assert "user_care" in message and "curiosity" in message

    def test_out_of_range_is_refused(self, tmp_path) -> None:
        """An axis is a 0..1 number; 1.5 would silently distort every downstream gain."""
        path = write(tmp_path, "cf.toml", "[persona.values]\nuser_care = 1.5\n")
        with pytest.raises(ConfigError, match="within 0..1"):
            load_client_config(path)


class TestPersonas:
    """Named profiles bundle a prompt with the axes that agree with it."""

    CONFIG = """
[persona]
active = "warm"
system_prompt = "内联提示词"

[persona.values]
user_care = 0.5

[persona.profiles.warm]
description = "热情"
system_prompt = "你很热情"
[persona.profiles.warm.values]
user_care = 0.99

[persona.profiles.cool]
system_prompt = "你很冷淡"
[persona.profiles.cool.values]
emotional_expression = 0.1
"""

    def test_active_profile_wins(self, tmp_path) -> None:
        """``active`` selects the profile and its prompt and axes both come along."""
        config = load_client_config(write(tmp_path, "cf.toml", self.CONFIG))
        assert config.persona.name == "warm"
        assert config.persona.system_prompt == "你很热情"
        assert config.persona.values == {"user_care": 0.99}

    def test_an_explicit_persona_overrides_active(self, tmp_path) -> None:
        """``--persona`` beats the file's own choice."""
        config = load_client_config(write(tmp_path, "cf.toml", self.CONFIG), persona="cool")
        assert config.persona.name == "cool"
        assert config.persona.system_prompt == "你很冷淡"
        assert config.persona.resolved_values()["emotional_expression"] == 0.1

    def test_without_active_the_inline_block_is_used(self, tmp_path) -> None:
        """The inline ``[persona]`` block is a usable character on its own."""
        text = self.CONFIG.replace('active = "warm"', 'active = ""')
        config = load_client_config(write(tmp_path, "cf.toml", text))
        assert config.persona.name == "default"
        assert config.persona.system_prompt == "内联提示词"
        assert config.persona.values == {"user_care": 0.5}

    def test_an_unknown_persona_lists_the_known_ones(self, tmp_path) -> None:
        """A wrong name is a clear error, not a silent fallback to the default."""
        with pytest.raises(ConfigError) as excinfo:
            load_client_config(write(tmp_path, "cf.toml", self.CONFIG), persona="nope")
        assert "warm" in str(excinfo.value) and "cool" in str(excinfo.value)

    def test_all_profiles_are_reported(self, tmp_path) -> None:
        """``to_dict`` lists what is available, for discoverability."""
        config = load_client_config(write(tmp_path, "cf.toml", self.CONFIG))
        assert config.to_dict()["personas_available"] == ["cool", "warm"]


class TestPromptFiles:
    """A long persona prompt belongs in its own file."""

    def test_prompt_file_is_read(self, tmp_path) -> None:
        """The file's contents become the prompt, and its path is recorded."""
        (tmp_path / "persona.md").write_text("来自文件的提示词\n第二行\n", encoding="utf-8")
        path = write(tmp_path, "cf.toml", '[persona]\nsystem_prompt_file = "persona.md"\n')
        config = load_client_config(path)
        assert config.persona.system_prompt == "来自文件的提示词\n第二行"
        assert config.persona.source.endswith("persona.md")

    def test_prompt_file_is_relative_to_the_config(self, tmp_path) -> None:
        """Not to the shell's cwd: a config that only works from one directory breaks in a service."""
        nested = tmp_path / "conf"
        nested.mkdir()
        (nested / "p.md").write_text("嵌套路径", encoding="utf-8")
        path = write(nested, "cf.toml", '[persona]\nsystem_prompt_file = "p.md"\n')
        assert load_client_config(path).persona.system_prompt == "嵌套路径"

    def test_a_missing_prompt_file_is_reported(self, tmp_path) -> None:
        """A dangling path names itself."""
        path = write(tmp_path, "cf.toml", '[persona]\nsystem_prompt_file = "gone.md"\n')
        with pytest.raises(ConfigError, match="not found"):
            load_client_config(path)

    def test_inline_prompt_still_works(self, tmp_path) -> None:
        """Both forms are supported, and the source says which was used."""
        path = write(tmp_path, "cf.toml", '[persona]\nsystem_prompt = "内联"\n')
        config = load_client_config(path)
        assert config.persona.system_prompt == "内联"
        assert config.persona.source == "inline"


class TestHarnessSettings:
    """Rig settings, including the one that has a fixed set of values."""

    def test_semantics_accepts_the_three_sources(self, tmp_path) -> None:
        """main_llm / mock / disabled all load."""
        for source in ("main_llm", "mock", "disabled"):
            path = write(tmp_path, f"{source}.toml", f'[harness]\nsemantics = "{source}"\n')
            assert load_client_config(path).harness.semantics == source

    def test_an_unknown_semantic_source_is_refused(self, tmp_path) -> None:
        """A typo would otherwise silently mean 'no strong semantics at all'."""
        path = write(tmp_path, "cf.toml", '[harness]\nsemantics = "best_model"\n')
        with pytest.raises(ConfigError, match="semantics must be one of"):
            load_client_config(path)

    def test_seed_can_be_disabled(self, tmp_path) -> None:
        """An empty seed means system entropy, which is a legitimate request."""
        path = write(tmp_path, "cf.toml", "[harness]\nseed = 0\n")
        assert load_client_config(path).harness.seed == 0


class TestReporting:
    """``cf config show`` is how an operator checks the layering."""

    def test_to_dict_never_contains_a_key(self, tmp_path, monkeypatch) -> None:
        """The description reports presence, never the value."""
        monkeypatch.setenv(DEFAULT_API_KEY_ENV, "sk-should-not-appear")
        path = write(tmp_path, "cf.toml", '[llm]\nmodel = "m"\n')
        rendered = json.dumps(load_client_config(path).to_dict(), ensure_ascii=False)
        assert "sk-should-not-appear" not in rendered
        assert "configured" in rendered

    def test_persona_prompt_is_reported(self, tmp_path) -> None:
        """The prompt lives on the persona; reporting only the llm table made
        every character look promptless."""
        path = write(tmp_path, "cf.toml", '[persona]\nsystem_prompt = "一二三四五"\n')
        persona = load_client_config(path).to_dict()["persona"]
        assert persona["system_prompt_chars"] == 5
        assert persona["system_prompt_source"] == "inline"


class TestExample:
    """The shipped template has to load, or it teaches the wrong thing."""

    def test_a_generated_example_actually_loads(self, tmp_path) -> None:
        """The template must run out of the box, not merely parse.

        Regression: the template's ``active`` persona points at
        ``personas/gentle.md``. Shipping the document without that file made
        ``cf config init`` produce a config that failed on first use -- the worst
        possible introduction to the feature.
        """
        write_example(tmp_path / "cf.toml")
        config = load_client_config(tmp_path / "cf.toml")
        assert config.harness.semantics == "main_llm"
        assert config.persona.name == "gentle"
        assert config.persona.system_prompt.strip()
        assert set(config.personas) == {"gentle", "guarded"}

    def test_scaffolding_keeps_existing_prompts(self, tmp_path) -> None:
        """Editing a persona prompt is not undone by re-running ``init``."""
        (tmp_path / "personas").mkdir()
        (tmp_path / "personas" / "gentle.md").write_text("我自己改过的", encoding="utf-8")
        write_example(tmp_path / "cf.toml")
        assert (tmp_path / "personas" / "gentle.md").read_text(encoding="utf-8") == "我自己改过的"

    def test_the_example_text_parses(self, tmp_path) -> None:
        """The document alone is valid TOML with the documented defaults."""
        path = write(tmp_path, "cf.toml", example_toml())
        # Only the prompt *files* are absent here; clearing ``active`` isolates the
        # document from the scaffolding it expects beside it.
        text = path.read_text(encoding="utf-8").replace('active = "gentle"', 'active = ""')
        path.write_text(text, encoding="utf-8")
        assert load_client_config(path).harness.semantics == "main_llm"

    def test_every_axis_is_documented_in_the_example(self) -> None:
        """All eight axes appear, so nobody has to read the source to find them."""
        text = example_toml()
        for axis in VALUE_AXES:
            assert axis in text, axis

    def test_the_example_contains_no_credential(self) -> None:
        """It says where the key goes; it never suggests putting one inline."""
        assert "api_key_env" in example_toml()
        assert 'api_key = "' not in example_toml()

    def test_write_example_refuses_to_clobber(self, tmp_path) -> None:
        """An existing file is kept unless ``--force``."""
        target = tmp_path / "cf.toml"
        target.write_text("mine\n", encoding="utf-8")
        with pytest.raises(ConfigError, match="already exists"):
            write_example(target)
        assert target.read_text(encoding="utf-8") == "mine\n"

    def test_write_example_creates_parents(self, tmp_path) -> None:
        """A nested destination is created."""
        written = write_example(tmp_path / "conf" / "cf.toml")
        assert written.is_file()

    def test_force_overwrites(self, tmp_path) -> None:
        """``--force`` replaces it."""
        target = tmp_path / "cf.toml"
        target.write_text("mine\n", encoding="utf-8")
        write_example(target, force=True)
        assert "小九九外接框架" in target.read_text(encoding="utf-8")


class TestDefaults:
    """An empty configuration is still a configuration."""

    def test_client_config_is_usable_with_no_file(self) -> None:
        """``ClientConfig()`` needs no arguments, which is what the CLI relies on."""
        config = ClientConfig()
        assert config.harness.semantics == "main_llm"
        assert config.persona.resolved_values()["user_care"] == pytest.approx(0.85)
