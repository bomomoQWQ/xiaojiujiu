r"""Preview how a proactive message is cut into bubbles before it is sent.

The Runtime renders one text; the adapter turns it into the bubbles a person would
send. This script runs that decision offline, on the real host settings, so a
change to 分段回复 (or to this split) can be seen without waiting for her to speak.

Usage (from the repository root, with ``.venv-dev`` active):

    python scripts/segment_preview.py                       # built-in beta samples
    python scripts/segment_preview.py "第一句。\n第二句。"     # your own text
    python scripts/segment_preview.py --config path/to/cmd_config.json

``--config`` reads ``platform_settings.segmented_reply`` out of a real AstrBot
config file, which is the same mapping the plugin reads at send time; without it
the beta's live values are used.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from random import Random

REPO_ROOT = Path(__file__).resolve().parent.parent
#: Where the plugin lives: next to this repo in the workspace, or (on the beta
#: host) under AstrBot's plugin directory, which is where the running copy is.
PLUGIN_CANDIDATES = (
    Path(os.environ.get("COMPANION_PLUGIN_ROOT", "")) if os.environ.get("COMPANION_PLUGIN_ROOT") else None,
    REPO_ROOT / "astrbot_plugin_companion_runtime",
    REPO_ROOT.parent / "data" / "plugins" / "astrbot_plugin_companion_runtime",
    Path("/AstrBot/data/plugins/astrbot_plugin_companion_runtime"),
)
PLUGIN_ROOT = next(
    (path for path in PLUGIN_CANDIDATES if path is not None and (path / "companion_runtime").is_dir()),
    None,
)
if PLUGIN_ROOT is None:
    raise SystemExit(
        "找不到插件目录；用 COMPANION_PLUGIN_ROOT=<path> 指定，"
        "或把本脚本放在插件仓库旁边运行",
    )
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

from companion_runtime.segments import SegmentPolicy, host_segment_settings  # noqa: E402

#: The beta's live configuration, as read from the running AstrBot.
BETA_HOST_SETTINGS = {
    "enable": True,
    "only_llm_result": True,
    "interval_method": "random",
    "interval": "0.8,1.6",
    "log_base": 2.6,
    "words_count_threshold": 60,
    "split_mode": "regex",
    "regex": r"[^\n]+",
    "split_words": ["。", "？", "！", "~", "…"],
    "content_cleanup_rule": "[.。]+$",
}

#: Proactive messages the beta actually sent, taken from an archived instance DB.
BETA_SAMPLES = (
    "昨天那句「你先别急」，我到现在还记着。",
    "突然想起你那个“急”字。\n我知道你很急，但你先别急。",
    "你那句“我知道你很急，但你先别急”，我还记着。\n想起来就觉得挺逗的。",
    "我记得你在打以撒。\n急也急不出好道具。",
    "在忙吗",
)


def _load_host_settings(path: str | None) -> dict:
    """Return the host's ``segmented_reply`` mapping from a real config file."""
    if not path:
        return dict(BETA_HOST_SETTINGS)
    raw = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    settings = host_segment_settings(raw)
    if not settings:
        raise SystemExit(f"{path} carries no platform_settings.segmented_reply")
    return dict(settings)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("text", nargs="*", help="text to split (default: beta samples)")
    parser.add_argument("--config", help="AstrBot cmd_config.json to read the rule from")
    parser.add_argument("--mode", default="inherit", choices=("inherit", "on", "off"))
    parser.add_argument("--max-parts", type=int, default=4)
    parser.add_argument("--seed", type=int, default=0, help="pins the pauses")
    args = parser.parse_args()

    settings = _load_host_settings(args.config)
    policy = SegmentPolicy.from_host_settings(
        settings,
        mode=args.mode,
        max_parts=args.max_parts,
    )
    texts = args.text or list(BETA_SAMPLES)

    print(f"宿主规则：enable={settings.get('enable')} mode={policy.mode} "
          f"regex={policy.regex!r} 间隔={policy.interval} 上限={policy.max_parts} 条")
    print(f"分段开关：{args.mode} → {'开' if policy.enabled else '关'}")

    rng = Random(args.seed)
    split = 0
    for text in texts:
        beats = policy.beats(text)
        delays = policy.delays(beats, rng)
        split += 1 if len(beats) > 1 else 0
        print("=" * 68)
        print("渲染：", repr(text))
        print(f"送出：{len(beats)} 条" + ("   ← 以前是 1 条带换行的气泡" if len(beats) > 1 else ""))
        for index, beat in enumerate(beats):
            wait = f"（等 {delays[index - 1]:.2f}s）" if index else ""
            print(f"   {index + 1}. {wait}{beat}")
        assert "".join(beats).replace("\n", "") == text.replace("\n", "").strip(), "丢字了"
    print("=" * 68)
    print(f"{len(texts)} 条样本里 {split} 条会分成多条；文本一字未改，也没有丢字。")


if __name__ == "__main__":
    main()
