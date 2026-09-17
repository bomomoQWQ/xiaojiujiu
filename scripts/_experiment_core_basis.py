"""在 _subject_core 剥完之后的 core 上重新标定阈值（代码实际比较的就是这个）。

上一版实验错了：拿整标题算 token，而实现比较的是剥掉模板词（等待/用户/结果/告知/
关心/后续/是否/顺利/消息）之后的串 —— 分母从 27 掉到 18，同一个 changed=4 就从 15% 变成 22%。
"""
import sys

sys.path.insert(0, "runtime/src")
from companion_runtime import unfinished as u  # noqa: E402


def stats(a, b):
    A, B = u.topic_tokens(u._subject_core(a)), u.topic_tokens(u._subject_core(b))
    if not A or not B:
        return 0.0, 0, 0.0
    jac = len(A & B) / len(A | B)
    changed = len(A - B) + len(B - A)
    return jac, changed, changed / max(len(A), len(B))


POSITIVE = [
    ("奶茶/去糖 vs 无糖",
     "用户邀请角色尝试奶茶/去糖奶茶，角色是否接受、后续是否真的会一起喝，尚未有结果。",
     "用户邀请角色尝试无糖奶茶，角色是否接受、后续是否真的会一起喝，尚未有结果。"),
    ("当个事办 vs 当个事办(多一句)",
     "“当个事办”具体指什么尚未明确，可在自然语境下轻量确认，不必专门追问。",
     "“当个事办”具体指什么尚未明确，可在自然语境下轻量确认。"),
    ("同 qq 昵称（带句号 vs 不带）",
     "“同 qq 昵称”的指代与用途未说明，若后续相关可顺带澄清。",
     "“同 qq 昵称”的指代与用途未说明，若后续相关可顺带澄清"),
    ("奶茶 vs 奶茶邀约尚未落地",
     "用户邀请角色尝试无糖奶茶，角色是否接受、后续是否真的会一起喝，尚未有结果。",
     "奶茶邀约尚未落地：用户邀请角色尝试奶茶/无糖奶茶，是否真的会一起喝还没有结果。"),
    ("我试试 vs 我试试（另一说法）",
     "用户说“我试试什么（）”，具体想试什么未明，可留待后续自然聊到。",
     "用户“我试试什么（）”的具体所指未明，可留待后续自然澄清。"),
]

CONSTRUCTED = [
    ("奶茶 -> 咖啡",
     "用户邀请角色尝试奶茶/去糖奶茶，角色是否接受、后续是否真的会一起喝，尚未有结果。",
     "用户邀请角色尝试咖啡，角色是否接受、后续是否真的会一起喝，尚未有结果。"),
    ("面试 vs 考试（模板）", "等待面试结果", "等待考试结果"),
    ("面试 vs 面试通知（模板）", "等待面试结果", "等待面试结果通知"),
    ("我试试 -> 换个头像",
     "用户说“我试试什么（）”，具体想试什么未明，可留待后续自然聊到。",
     "用户说“换个头像”，具体想试什么未明，可留待后续自然聊到。"),
]

print("%-30s %-9s %-8s %-8s" % ("对", "jaccard", "changed", "ratio"))
print("-" * 58)
for label, a, b in POSITIVE:
    j, c, r = stats(a, b)
    print("%-30s %-9.3f %-8d %-8.1f%% [正]" % (label, j, c, r * 100))
for label, a, b in CONSTRUCTED:
    j, c, r = stats(a, b)
    print("%-30s %-9.3f %-8d %-8.1f%% [负]" % (label, j, c, r * 100))

print()
print("=== jaccard>=J 且 ratio<=R ===")
best = []
for J in (0.6, 0.65, 0.7, 0.75, 0.8):
    for R in (0.15, 0.20, 0.25, 0.30, 0.35, 0.40):
        pos = sum(1 for _, a, b in POSITIVE if stats(a, b)[0] >= J and stats(a, b)[2] <= R)
        neg = sum(1 for _, a, b in CONSTRUCTED if stats(a, b)[0] >= J and stats(a, b)[2] <= R)
        if neg == 0:
            best.append((pos, J, R))
best.sort(reverse=True)
for pos, J, R in best[:8]:
    print("  J=%.2f R=%.2f -> 正 %d/%d  负 0/%d" % (J, R, pos, len(POSITIVE), len(CONSTRUCTED)))
