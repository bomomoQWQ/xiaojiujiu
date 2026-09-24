"""The framework's own configuration file.

Separate from the program's ``runtime.toml`` on purpose. That file configures the
sidecar's internals (storage, emotion dynamics, scheduler); this one configures
*the experiment*: which model acts, who the character is, what it values, where
the clock starts, and which run directory to write to. Mixing the two would make
it impossible to say whether a change belonged to the thing under test or to the
rig testing it.

Why a file at all, when every setting has a flag
------------------------------------------------
Because the settings that matter here are not scalars. A character is a system
prompt *plus* eight value axes that have to agree with it -- a warm, talkative
persona written over a restrained value profile produces a character that says
warm things and then never follows up. Keeping them in one place, under one name,
is the difference between tuning a character and tuning a pile of numbers.

Hence **named personas**::

    [persona]
    active = "gentle"

    [persona.profiles.gentle]
    system_prompt_file = "personas/gentle.md"
    [persona.profiles.gentle.values]
    user_care = 0.95
    boundary_respect = 0.55

    [persona.profiles.guarded]
    system_prompt = "..."
    [persona.profiles.guarded.values]
    boundary_respect = 0.95

Switching between two characters is then ``--persona guarded``, and both halves
move together.

Credentials
-----------
The API key is **never** read from this file. It is read from the environment
variable named by ``[llm].api_key_env``. A file that contains something that
looks like a key is refused rather than tolerated: a config file is exactly the
artefact that ends up in a backup, a screenshot or a repository.
"""

from __future__ import annotations

import json
import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

#: The eight value axes, with the library default and what each one moves.
#: Kept here (rather than in the CLI) because the config file is now the primary
#: way to set them and both need the same names and documentation.
VALUE_AXES: dict[str, tuple[float, str]] = {
    "autonomy": (0.72, "自我推进的意愿：越高越容易自己决定开口"),
    "boundary_respect": (0.88, "对边界的敬畏：越高越不容易越线，也越容易被拒绝压住"),
    "emotional_expression": (0.46, "情绪外露：越高情绪越直接地写在话里"),
    "relationship_maintenance": (0.79, "关系维护：越高越会在长期沉默后主动靠近"),
    "user_care": (0.85, "对用户的在意：越高越会被对方的未结之事推动"),
    "conflict_directness": (0.41, "冲突直率：越高越倾向于把话挑明"),
    "stability_commitment": (0.81, "稳定承诺：越高越不容易被单次波动带偏"),
    "curiosity": (0.76, "好奇：越高越容易想追问、想了解"),
}

DEFAULT_API_KEY_ENV = "CF_MAIN_LLM_API_KEY"

#: Whole words that mark a config value as a credential.
#:
#: Matched on word boundaries rather than as substrings: ``max_tokens`` contains
#: "token" but is a sampling cap, and an over-eager check that refused to load a
#: perfectly ordinary config file would be worse than no check at all.
SECRET_WORDS = frozenset({"secret", "password", "passwd", "token", "credential", "credentials"})

#: Multi-word names that are credentials even though neither word is.
SECRET_PHRASES = ("api_key", "apikey", "access_key", "private_key", "auth_key", "bearer")

#: How strong semantics are fed: reuse the acting model, the built-in mock, or off.
SEMANTIC_SOURCES = ("main_llm", "mock", "disabled")


class ConfigError(Exception):
    """Raised for a configuration file that cannot be honoured."""


# --------------------------------------------------------------------------- model


@dataclass
class LLMSettings:
    """The acting layer's endpoint and sampling."""

    base_url: str = ""
    model: str = ""
    api_key_env: str = DEFAULT_API_KEY_ENV
    system_prompt: str = ""
    temperature: float = 0.8
    max_tokens: int = 800
    timeout_s: float = 60.0

    def api_key(self, env: Mapping[str, str] | None = None) -> str:
        """Return the key from the configured environment variable."""
        source = os.environ if env is None else env
        return source.get(self.api_key_env, "")


@dataclass
class ClockSettings:
    """Where virtual time starts and how fast it runs."""

    start_time: str | None = None
    time_scale: float = 1.0
    step: str | None = None
    heartbeat_interval_s: float = 1.0
    status_interval_s: float = 2.0


@dataclass
class HarnessSettings:
    """Everything about the rig that is not the character."""

    run_dir: str = ""
    program_src: str = "../runtime/src"
    plugin_root: str = "../astrbot_plugin_companion_runtime"
    seed: int | None = 20260915
    semantics: str = "main_llm"
    session: str = "default"
    echo_logs: bool = False
    #: Where the chat window's durable history lives. Empty means "decide at start-up":
    #: beside the config file when one was loaded (so the history stays with the
    #: experiment it belongs to), otherwise ``runs/chat_history.jsonl``. It is *not*
    #: derived from ``run_dir``, because that is timestamped per run and a history that
    #: dies with the run would not be a history.
    history: str = ""
    #: How many turns of the current session to show when the window opens.
    recap: int = 6


@dataclass
class Persona:
    """A character: one system prompt and the value profile that agrees with it."""

    name: str = "default"
    description: str = ""
    system_prompt: str = ""
    #: Resolved path of the prompt file, if this persona uses one. Kept unread
    #: until the persona is selected: a broken profile you are not using must not
    #: stop you from using a good one.
    prompt_file: str = ""
    #: Where the prompt came from -- ``inline`` or the resolved file path. Shown
    #: by ``cf config show`` so "which file is actually being used" is never a
    #: guess when two personas point at similar names.
    source: str = ""
    values: dict[str, float] = field(default_factory=dict)

    def resolved_values(self) -> dict[str, float]:
        """Return the full profile: library defaults with this persona's overrides."""
        return {**{name: default for name, (default, _) in VALUE_AXES.items()}, **self.values}


@dataclass
class ClientConfig:
    """A fully resolved framework configuration."""

    llm: LLMSettings = field(default_factory=LLMSettings)
    clock: ClockSettings = field(default_factory=ClockSettings)
    harness: HarnessSettings = field(default_factory=HarnessSettings)
    persona: Persona = field(default_factory=Persona)
    personas: dict[str, Persona] = field(default_factory=dict)
    source_path: Path | None = None

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable rendering, for ``cf config show``."""
        return {
            "source": str(self.source_path) if self.source_path else None,
            "llm": {
                "base_url": self.llm.base_url,
                "model": self.llm.model,
                "api_key_env": self.llm.api_key_env,
                "api_key": "configured" if self.llm.api_key() else "not configured",
                "temperature": self.llm.temperature,
                "max_tokens": self.llm.max_tokens,
                "timeout_s": self.llm.timeout_s,
                "system_prompt_chars": len(self.llm.system_prompt),
            },
            "persona": {
                "name": self.persona.name,
                "description": self.persona.description,
                "values": self.persona.resolved_values(),
                "overridden": dict(self.persona.values),
                # The prompt lives on the persona, not on [llm]: a character is a
                # prompt plus the axes that agree with it, and reporting only the
                # llm table's (empty) field made every persona look promptless.
                "system_prompt_chars": len(self.persona.system_prompt),
                "system_prompt_source": self.persona.source,
            },
            "personas_available": sorted(self.personas),
            "clock": {
                "start_time": self.clock.start_time,
                "time_scale": self.clock.time_scale,
                "step": self.clock.step,
                "heartbeat_interval_s": self.clock.heartbeat_interval_s,
                "status_interval_s": self.clock.status_interval_s,
            },
            "harness": {
                "run_dir": self.harness.run_dir,
                "program_src": self.harness.program_src,
                "plugin_root": self.harness.plugin_root,
                "seed": self.harness.seed,
                "semantics": self.harness.semantics,
                "session": self.harness.session,
                "history": self.harness.history,
                "recap": self.harness.recap,
            },
        }


# ------------------------------------------------------------------------ loading


def load_client_config(path: str | Path, *, persona: str = "") -> ClientConfig:
    """Read and validate a framework configuration file.

    Args:
        path: A ``.toml`` or ``.json`` file.
        persona: Name of the persona profile to activate, overriding
            ``[persona].active``.

    Returns:
        The resolved configuration.

    Raises:
        ConfigError: On a missing file, an unknown extension, a malformed
            document, an unknown value axis, a bad semantic source, or a
            credential found in the file. Every message names the offender --
            a config file that is silently half-applied is worse than one that
            refuses to load.
    """
    file_path = Path(path).expanduser()
    if not file_path.is_file():
        raise ConfigError(f"config file not found: {file_path}")
    # ``cf.toml.example`` is the conventional name for a shipped template, so the
    # format is taken from the suffix *before* the marker rather than from the
    # final one. Requiring a rename just to inspect the example would be a strange
    # thing to insist on.
    suffixes = [part.lower() for part in file_path.suffixes]
    while suffixes and suffixes[-1] in {".example", ".sample", ".dist", ".template"}:
        suffixes.pop()
    suffix = suffixes[-1] if suffixes else ""
    if suffix == ".toml":
        with file_path.open("rb") as handle:
            try:
                data = tomllib.load(handle)
            except tomllib.TOMLDecodeError as exc:
                raise ConfigError(f"{file_path}: not valid TOML: {exc}") from exc
    elif suffix == ".json":
        try:
            data = json.loads(file_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ConfigError(f"{file_path}: not valid JSON: {exc}") from exc
    else:
        raise ConfigError(f"{file_path}: unsupported extension {file_path.suffix!r}; use .toml or .json")

    if not isinstance(data, Mapping):
        raise ConfigError(f"{file_path}: the document must be a table/object at the top level")

    _reject_credentials(data, file_path)
    config = _build(data, base=file_path.parent)
    config.source_path = file_path
    if persona:
        config.persona = _select_persona(config, persona)
    return config


def _looks_like_a_credential(name: str) -> bool:
    """Return whether a config key names a credential.

    A key whose name mentions ``env`` is exempt: ``api_key_env`` names *where* the
    key lives, which is the recommended form, not the key itself.
    """
    lowered = name.lower()
    if "env" in lowered:
        return False
    if any(phrase in lowered for phrase in SECRET_PHRASES):
        return True
    words = re.split(r"[^a-z0-9]+", lowered)
    return any(word in SECRET_WORDS for word in words)


def _reject_credentials(data: Any, path: Path, trail: str = "") -> None:
    """Refuse a config file that carries a credential.

    Raises:
        ConfigError: When any key looks like a secret. The key's *path* is named
            but never its value.
    """
    if isinstance(data, Mapping):
        for key, value in data.items():
            name = str(key)
            here = f"{trail}.{name}" if trail else name
            if _looks_like_a_credential(name):
                raise ConfigError(
                    f"{path}: [{here}] looks like a credential. Keys are read from the "
                    f"environment variable named by [llm].api_key_env (default "
                    f"{DEFAULT_API_KEY_ENV}); a config file is the artefact that ends up "
                    "in a backup or a screenshot."
                )
            _reject_credentials(value, path, here)


def _build(data: Mapping[str, Any], *, base: Path) -> ClientConfig:
    """Turn a decoded document into a resolved configuration."""
    llm_table = _table(data, "llm")
    clock_table = _table(data, "clock")
    harness_table = _table(data, "harness")
    persona_table = _table(data, "persona")

    personas = _parse_personas(_table(persona_table, "profiles"), base=base)
    active = str(persona_table.get("active") or "")

    llm = LLMSettings(
        base_url=str(llm_table.get("base_url") or ""),
        model=str(llm_table.get("model") or ""),
        api_key_env=str(llm_table.get("api_key_env") or DEFAULT_API_KEY_ENV),
        temperature=float(llm_table.get("temperature", 0.8)),
        max_tokens=int(llm_table.get("max_tokens", 800)),
        timeout_s=float(llm_table.get("timeout_s", 60.0)),
    )
    clock = ClockSettings(
        start_time=_optional_str(clock_table.get("start_time")),
        time_scale=float(clock_table.get("time_scale", 1.0)),
        step=_optional_str(clock_table.get("step")),
        heartbeat_interval_s=float(clock_table.get("heartbeat_interval_s", 1.0)),
        status_interval_s=float(clock_table.get("status_interval_s", 2.0)),
    )
    harness = HarnessSettings(
        # Every relative path in this file is resolved against the file, never
        # against the shell's cwd. One rule, and it is the only one that survives
        # being run from a different directory -- which is exactly what happens
        # when the config lives beside the experiment it describes.
        run_dir=_resolve_relative(harness_table.get("run_dir"), base),
        program_src=_resolve_relative(
            harness_table.get("program_src") or HarnessSettings.program_src, base
        ),
        plugin_root=_resolve_relative(
            harness_table.get("plugin_root") or HarnessSettings.plugin_root, base
        ),
        seed=_optional_int(harness_table.get("seed"), default=20260915),
        semantics=str(harness_table.get("semantics") or "main_llm").lower(),
        session=str(harness_table.get("session") or "default"),
        echo_logs=bool(harness_table.get("echo_logs", False)),
        history=_resolve_relative(harness_table.get("history"), base),
        recap=int(harness_table.get("recap", HarnessSettings.recap)),
    )
    if harness.semantics not in SEMANTIC_SOURCES:
        raise ConfigError(
            f"[harness].semantics must be one of {', '.join(SEMANTIC_SOURCES)}; got {harness.semantics!r}"
        )

    inline = _parse_persona("default", persona_table, base=base)
    config = ClientConfig(llm=llm, clock=clock, harness=harness, persona=inline, personas=personas)
    config.persona = _select_persona(config, active)
    return config


def _select_persona(config: ClientConfig, name: str) -> Persona:
    """Return the named persona, or the inline one when no name is given.

    Raises:
        ConfigError: When the name is unknown, listing what exists.
    """
    wanted = (name or "").strip()
    if not wanted:
        return _resolve_prompt(config.persona)
    if wanted not in config.personas:
        known = ", ".join(sorted(config.personas)) or "(没有定义任何 profiles)"
        raise ConfigError(f"unknown persona {wanted!r}; available: {known}")
    return _resolve_prompt(config.personas[wanted])


def _parse_personas(table: Mapping[str, Any], *, base: Path) -> dict[str, Persona]:
    """Parse every ``[persona.profiles.*]`` table."""
    personas: dict[str, Persona] = {}
    for name, body in table.items():
        if not isinstance(body, Mapping):
            raise ConfigError(f"[persona.profiles.{name}] must be a table")
        personas[str(name)] = _parse_persona(str(name), body, base=base)
    return personas


def _resolve_relative(value: Any, base: Path) -> str:
    """Resolve a config-supplied path against the config file's directory.

    Absolute paths and the empty string pass through untouched. A path that only
    resolves from one working directory is a path that breaks the moment the
    config is used from anywhere else -- and "the config sits next to the
    experiment" is the normal layout, not the exception.
    """
    text = _optional_str(value)
    if not text:
        return ""
    candidate = Path(text).expanduser()
    if candidate.is_absolute():
        return str(candidate)
    return str((base / candidate).resolve())


def _parse_persona(name: str, body: Mapping[str, Any], *, base: Path) -> Persona:
    """Parse one persona table: axes are validated now, the prompt is deferred.

    Reading the prompt file here would validate every profile in the file, so a
    half-written persona nobody selected would stop the whole configuration from
    loading. The axes are still checked eagerly -- a typo in a name is worth
    catching immediately -- but a missing prompt file is reported when that
    persona is actually asked for.
    """
    prompt = str(body.get("system_prompt") or "").strip()
    prompt_file = _optional_str(body.get("system_prompt_file"))
    resolved_file = ""
    if prompt_file:
        candidate = Path(prompt_file).expanduser()
        if not candidate.is_absolute():
            # Relative to the config file, not to the shell's cwd: a config that
            # only works from one directory is a config that breaks in a service.
            candidate = base / candidate
        resolved_file = str(candidate)

    values_table = _table(body, "values")
    values: dict[str, float] = {}
    for axis, raw in values_table.items():
        if axis not in VALUE_AXES:
            lines = "\n".join(
                f"    {key:<26} 默认 {default:<5} {doc}" for key, (default, doc) in VALUE_AXES.items()
            )
            raise ConfigError(
                f"persona {name!r}: unknown value axis {axis!r}\n  available axes:\n{lines}"
            )
        number = float(raw)
        if not 0.0 <= number <= 1.0:
            raise ConfigError(f"persona {name!r}: value axis {axis!r} must be within 0..1; got {number}")
        values[axis] = number

    return Persona(
        name=name,
        description=str(body.get("description") or ""),
        system_prompt=prompt,
        prompt_file=resolved_file,
        source="inline" if prompt else "",
        values=values,
    )


def _resolve_prompt(persona: Persona) -> Persona:
    """Read the prompt file of the *selected* persona, if it uses one.

    Raises:
        ConfigError: When the file it names does not exist.
    """
    if not persona.prompt_file:
        return persona
    candidate = Path(persona.prompt_file)
    if not candidate.is_file():
        raise ConfigError(
            f"persona {persona.name!r}: system_prompt_file not found: {candidate}"
        )
    persona.system_prompt = candidate.read_text(encoding="utf-8").strip()
    persona.source = str(candidate)
    return persona


def _table(data: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    """Return a sub-table, or an empty mapping when absent.

    Raises:
        ConfigError: When the key exists but is not a table.
    """
    value = data.get(key)
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ConfigError(f"[{key}] must be a table/object")
    return value


def _optional_str(value: Any) -> str | None:
    """Return a stripped string, or ``None`` for blank/absent."""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _optional_int(value: Any, *, default: int | None) -> int | None:
    """Return an int, or ``default`` for blank/absent."""
    if value is None or value == "":
        return default
    return int(value)


# ------------------------------------------------------------------------ example


#: Where the framework's own defaults point, derived from this file's location.
#: ``cf/config.py`` -> ``cf/`` -> the framework root -> the repo root.
FRAMEWORK_ROOT = Path(__file__).resolve().parent.parent
REPO_ROOT = FRAMEWORK_ROOT.parent
DEFAULT_PROGRAM_SRC_ABS = REPO_ROOT / "runtime" / "src"
DEFAULT_PLUGIN_ROOT_ABS = REPO_ROOT / "astrbot_plugin_companion_runtime"


def default_relative_paths(destination: Path) -> tuple[str, str]:
    """Return ``(program_src, plugin_root)`` relative to a config's directory.

    A template can only hard-code a relative path if it knows where it will be
    written. ``../runtime/src`` is right for a config sitting in ``framework/``
    and wrong for one in ``framework/scratch/`` -- and because every relative path
    in the file resolves against the file, "wrong" means the first command the
    new user runs fails. Computing the hop removes the guesswork.
    """
    base = destination.resolve()

    def hop(target: Path) -> str:
        """Return ``target`` relative to ``base``, or the absolute path if that is shorter."""
        try:
            return os.path.relpath(target, base)
        except ValueError:  # pragma: no cover - different drives on Windows
            return str(target)

    return hop(DEFAULT_PROGRAM_SRC_ABS), hop(DEFAULT_PLUGIN_ROOT_ABS)


def example_toml(*, program_src: str = "", plugin_root: str = "") -> str:
    """Return a commented example configuration, ready to edit.

    Written as a literal rather than generated from the dataclasses so the
    comments -- which are most of the value -- survive.

    Args:
        program_src: Value for ``[harness].program_src``; defaults to the path
            that is correct for a config written into the framework directory.
        plugin_root: Likewise for ``[harness].plugin_root``.
    """
    axes = "\n".join(
        f"# {name} = {default}   # {doc}" for name, (default, doc) in VALUE_AXES.items()
    )
    program_src = program_src or os.path.relpath(DEFAULT_PROGRAM_SRC_ABS, FRAMEWORK_ROOT)
    plugin_root = plugin_root or os.path.relpath(DEFAULT_PLUGIN_ROOT_ABS, FRAMEWORK_ROOT)
    return f'''\
# 小九九外接框架 · 客户端配置
#
# 用法：
#   cf chat --config cf.toml
#   cf chat --config cf.toml --persona guarded     # 换一个人格档案
#   cf config show --config cf.toml                # 看解析后的最终结果
#
# 这个文件配的是「实验」，不是 Runtime 内部。Runtime 自己的配置是 runtime.toml。
# API key 绝不写在这里：只从下面 api_key_env 指定的**环境变量**读。

[llm]
base_url = "https://api.deepseek.com/v1"
model = "deepseek-chat"
api_key_env = "{DEFAULT_API_KEY_ENV}"     # key 只从环境变量读，不落盘
temperature = 0.8
max_tokens = 800
timeout_s = 60

[clock]
start_time = "2026-09-15T09:00:00Z"      # 虚拟世界从哪一刻开始
time_scale = 1.0                          # 虚拟秒 / 真实秒；0 = 时间不走
# step = "30m"                            # 每拍自动推进的时长（可选）
heartbeat_interval_s = 1.0                # 心跳间隔（真实秒）；0 = 关掉，手动 /advance
status_interval_s = 2.0                   # 状态栏刷新间隔，与心跳无关

# 这个文件里所有相对路径都相对**本文件所在目录**解析，不是当前工作目录。
[harness]
run_dir = "runs/chat"
program_src = "{program_src}"
plugin_root = "{plugin_root}"
seed = 20260915                           # 固定种子 → 可复现
semantics = "main_llm"                    # main_llm | mock | disabled
session = "default"
# 聊天窗口的持久对话记录。**不要**指向 run_dir：后者带时间戳、每次换一个，
# 记录活不过重启就不叫记录了。留空 = 放在本文件旁边（runs/chat_history.jsonl）。
history = "runs/chat_history.jsonl"
recap = 6                                 # 打开窗口时回显本会话最近几条；0 = 不回显

# ---------------------------------------------------------------------------
# 人格：一个 system prompt + 一套和它相配的价值观参数。
# 两者分开配很容易互相打架（写成话痨，参数却是克制型 → 说暖话但不跟进）。
# ---------------------------------------------------------------------------

[persona]
active = "gentle"                         # 默认启用哪个档案；留空则用下面内联的

# 内联写法（没有 profiles 时用这套）
system_prompt = """
你是一个长期陪伴用户的角色，正在和一个你熟悉的人聊天。
用自然、口语化的中文回复，长度和对方的话相称，不要长篇大论，不要像助手或客服。
如果下面附带了背景信息，那是你此刻心里的状态，不是这一轮的任务，也不要照抄。
"""

[persona.values]
# 八个轴，取值 0..1，不写就用库默认值。
# 想改哪个就把注释去掉。
{axes}

# ---------------------------------------------------------------------------
# 命名档案：cf chat --persona <名字>
# `cf config init` 会连同 personas/*.md 一起生成，所以下面这两个开箱可用。
# ---------------------------------------------------------------------------

[persona.profiles.gentle]
description = "温和、主动、在意对方"
system_prompt_file = "personas/gentle.md"  # 也可以直接写 system_prompt = "..."
[persona.profiles.gentle.values]
user_care = 0.95
emotional_expression = 0.80
relationship_maintenance = 0.92
boundary_respect = 0.60
curiosity = 0.85

[persona.profiles.guarded]
description = "克制、守边界、不轻易开口"
system_prompt = """
你是一个话不多但一直在意对方的角色。你不轻易主动开口，开口时也说得简短。
不要追问，不要催促，对方不想说就不勉强。
"""
[persona.profiles.guarded.values]
user_care = 0.70
emotional_expression = 0.25
boundary_respect = 0.96
conflict_directness = 0.15
relationship_maintenance = 0.55
'''


#: Persona prompt files the example config refers to. Scaffolding them is what
#: makes ``cf config init`` produce something that *runs*: a template whose
#: ``active`` persona points at a file that does not exist fails on first use,
#: which teaches the wrong thing about the feature.
EXAMPLE_PROMPTS: dict[str, str] = {
    "personas/gentle.md": """\
你是一个长期陪伴用户的角色，正在和一个你熟悉的人聊天。

说话自然、口语化，长度和对方的话相称。不要长篇大论，不要像助手或客服，不要用列表和标题。
你记得你们之间发生过的事，偶尔会自然地带到，但不要刻意提起。

如果这一轮附带了背景信息，那是你此刻心里的状态，不是本轮的任务：
不要复述它，不要提到"背景""状态""系统"，就只是带着它说话。
""",
    "personas/guarded.md": """\
你是一个话不多、但一直在意对方的角色。

你不轻易主动开口；开口时也说得简短，常常只有一句。不追问，不催促，对方不想说就不勉强。
你不说漂亮话，关心放在具体的小事上。

如果这一轮附带了背景信息，那是你此刻心里的状态，不是本轮的任务：不要复述它。
""",
}


def write_example(path: str | Path, *, force: bool = False) -> Path:
    """Write the example configuration, plus the prompt files it refers to.

    Args:
        path: Destination for the config file.
        force: Overwrite existing files.

    Returns:
        The config path written.

    Raises:
        ConfigError: When a target exists and ``force`` is false.
    """
    target = Path(path).expanduser()
    if target.exists() and not force:
        raise ConfigError(f"{target} already exists; pass --force to overwrite")
    target.parent.mkdir(parents=True, exist_ok=True)
    program_src, plugin_root = default_relative_paths(target.parent)
    target.write_text(
        example_toml(program_src=program_src, plugin_root=plugin_root), encoding="utf-8"
    )
    for relative, body in EXAMPLE_PROMPTS.items():
        prompt_path = target.parent / relative
        if prompt_path.exists() and not force:
            continue
        prompt_path.parent.mkdir(parents=True, exist_ok=True)
        prompt_path.write_text(body, encoding="utf-8")
    return target
