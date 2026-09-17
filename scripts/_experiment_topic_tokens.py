"""用仓里既有的 topic_tokens 重算阈值（它按连续汉字段取 bigram，不跨标点）。"""
import sys

sys.path.insert(0, "runtime/src")
from companion_runtime.utility import topic_tokens  # noqa: E402

POSITIVE = [
    ("奶茶/去糖 vs 无糖",
     "用户邀请角色尝试奶茶/去糖奶茶，角色是否接受、后续是否真的会一起喝，尚未有结果。",
     "用户邀请角色尝试无糖奶茶，角色是否接受、后续是否真的会一起喝，尚未有结果。"),
    ("奶茶 vs 奶茶邀约尚未落地",
     "用户邀请角色尝试无糖奶茶，角色是否接受、后续是否真的会一起喝，尚未有结果。",
     "奶茶邀约尚未落地：用户邀请角色尝试奶茶/无糖奶茶，是否真的会一起喝还没有结果。"),
    ("我试试 vs 我试试（另一说法）",
     "用户说“我试试什么（）”，具体想试什么未明，可留待后续自然聊到。",
     "用户“我试试什么（）”的具体所指未明，可留待后续自然澄清。"),
    ("当个事办 vs 当个事办（多一句）",
     "“当个事办”具体指什么尚未明确，可在自然语境下轻量确认，不必专门追问。",
     "“当个事办”具体指什么尚未明确，可在自然语境下轻量确认。"),
]

CONSTRUCTED = [
    ("奶茶 -> 咖啡",
     "用户邀请角色尝试咖啡，角色是否接受、后续是否真的会一起喝，尚未有结果。",
     "用户邀请角色尝试无糖奶茶，角色是否接受、后续是否真的会一起喝，尚未有结果。"),
    ("面试 vs 考试（模板）", "等待面试结果", "等待考试结果"),
    ("面试 vs 面试通知（模板）", "等待面试结果", "等待面试结果通知"),
    ("我试试 -> 换个头像",
     "用户说“换个头像”，具体想试什么未明，可留待后续自然聊到。",
     "用户“我试试什么（）”的具体所指未明，可留待后续自然澄清。"),
]


def stats(a, b):
    A, B = topic_tokens(a), topic_tokens(b)
    if not A or not B:
        return 0.0, 0, 0
    jac = len(A & B) / len(A | B)
    changed = len(A - B) + len(B - A)
    length = max(len(A), len(B))
    return jac, changed, changed / length


print("%-32s %-8s %-8s %-8s" % ("对", "jaccard", "changed", "ratio"))
print("-" * 60)
for label, a, b in POSITIVE:
    j, c, r = stats(a, b)
    print("%-32s %-8.3f %-8d %-8.0f%%  [正]" % (label, j, c, r * 100))
for label, a, b in CONSTRUCTED:
    j, c, r = stats(a, b)
    print("%-32s %-8.3f %-8d %-8.0f%%  [负]" % (label, j, c, r * 100))

print()
print("=== 组合判定: jaccard>=J 且 ratio<=R ===")
for J in (0.5, 0.6, 0.65, 0.7):
    for R in (0.20, 0.25, 0.30, 0.35):
        pos = sum(1 for _, a, b in POSITIVE if stats(a, b)[0] >= J and stats(a, b)[2] <= R)
        neg = sum(1 for _, a, b in CONSTRUCTED if stats(a, b)[0] >= J and stats(a, b)[2] <= R)
        flag = "  <-- 全对" if pos == len(POSITIVE) and neg == 0 else ""
        print("  J=%.2f R=%.2f -> 正 %d/%d  负 %d/%d%s" % (
            J, R, pos, len(POSITIVE), neg, len(CONSTRUCTED), flag))
