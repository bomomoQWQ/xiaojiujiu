"""Move the tests onto the colloquial injection block (2026-09-29).

The block's wording changed for one reason: the injected register leaks into what she types
(measured: her replies read as written prose, and the block was one of the three prompts
feeding that). The section headers stay - the plugin parses them - but the body lines are now
her own voice, so the assertions that pinned the old wording move with it.
"""

import pathlib
import re

RUNTIME = pathlib.Path(".")


def patch(path: str, pairs: list[tuple[str, str]], *, required: bool = False) -> None:
    file = RUNTIME / path
    text = file.read_text(encoding="utf-8")
    hits = 0
    for old, new in pairs:
        if old in text:
            hits += text.count(old)
            text = text.replace(old, new)
        elif required:
            raise SystemExit("missing in %s: %r" % (path, old))
    file.write_text(text, encoding="utf-8")
    print("patched %-46s %d replacement(s)" % (path, hits))


fallback_pairs = [
    ('f"- 距离上次用户消息：{time_context[\'hours_since_last_user_message\']} 小时"',
     'f"他上次开口是 {time_context[\'hours_since_last_user_message\']} 小时前"'),
    ('f"- 距离上次主动联系：{time_context[\'hours_since_last_contact\']} 小时"',
     'f"我上次主动找他是 {time_context[\'hours_since_last_contact\']} 小时前"'),
    ("以上都只是我进来之前的状态，是一轮的临时背景，别照抄，也别写进长期记录。",
     "上面这些是我进这句话之前的样子，是我自己脑子里的事，别照着念，也别写进长期记忆。"),
]
patch("src/companion_runtime/api_v1.py", fallback_pairs, required=True)

test_pairs = [
    ("用户刚说的这句话", "他刚说的那句话"),
    ("眼下确定的事实", "我眼下确定的事"),
    ("用户明确划过的界线", "他明确划过的线"),
    ("我长期以来的心理状态", "我自己那些旧情绪"),
    ("我对眼下心情的解释", "我这份心情是怎么来的"),
    ("我自己的临场发挥", "我这一句的临场发挥"),
    ("我进来之前的状态，是一轮的临时背景", "我进这句话之前的样子，是我自己脑子里的事"),
    ("临时背景", "我自己脑子里的事"),
    ("以上都只是我进来之前的状态", "上面这些是我进这句话之前的样子"),
    ("距离上次用户消息", "他上次开口是"),
    ("听第 2 条", "他刚说的那句，压得过我那些旧情绪"),
]
for name in (
    "tests/test_api.py",
    "tests/test_delivery_scheduler_context.py",
    "tests/test_semantic_config_knobs.py",
):
    patch(name, test_pairs)

# The preamble is prose now, so the "seven numbered levels" check becomes a check that the
# ordering is still stated and that all seven ideas are still named.
file = RUNTIME / "tests/test_semantic_config_knobs.py"
text = file.read_text(encoding="utf-8")
pattern = re.compile(
    r"    def test_all_seven_levels_are_named\(self\) -> None:\n"
    r"(?:.*?\n)*?"
    r"(?=    def test_the_preamble_states_that_the_current_turn_wins)",
)
replacement = (
    "    def test_all_seven_levels_are_named(self) -> None:\n"
    '        """Patch section 7 lists seven levels; the prompt must not compress them.\n\n'
    "        They are prose now rather than a numbered list (2026-09-29: the block is written\n"
    "        in her voice), so this pins the ordering statement plus all seven ideas.\n"
    '        """\n'
    "        preamble = context_module.PRIORITY_PREAMBLE\n"
    '        assert "谁压得过谁是这个顺序" in preamble\n'
    "        for token in (\n"
    '            "我的人设和安全底线",\n'
    '            "他刚说的那句话",\n'
    '            "我眼下确定的事",\n'
    '            "他明确划过的线",\n'
    '            "我自己那些旧情绪",\n'
    '            "我这份心情是怎么来的",\n'
    '            "我这一句的临场发挥",\n'
    "        ):\n"
    "            assert token in preamble, token\n\n"
)
text, count = pattern.subn(replacement, text)
if count != 1:
    raise SystemExit("seven-levels test not replaced (count=%d)" % count)
file.write_text(text, encoding="utf-8")
print("rewrote the seven-levels test")
