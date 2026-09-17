"""本地实验：散文套话要剥到什么程度，才能让 bigram@0.6 既合并同话题、又不误合并。

判据：
  正样本 —— 真实语料里**同一话题**的标题对（人工判定，见 POSITIVE）
  负样本 —— ① 跨实例真实标题对（不同话题）
            ② **构造**负样本：把正样本里的主题词换掉，其余照抄（这是纯 bigram 的真正危险区）
"""
import json
import re
import sys

TITLES = json.load(open(r"F:\理解痞老板\.scratch_titles.json", encoding="utf-8"))

# 现有模板词（未改动之前 _TITLE_TEMPLATE_WORDS 的内容）
TEMPLATE_WORDS = ("等待", "用户", "结果", "告知", "关心", "后续", "是否", "顺利", "消息")

# 候选：散文套话词表（要试的核心）
PROSE_WORDS = (
    "角色", "接受", "一起喝", "真的会", "尚未", "还未", "还没有", "可能", "需要",
    "确认", "澄清", "明确", "回应", "满意", "稳定满足", "满足", "自然聊到", "自然澄清",
    "自然", "轻量", "顺带", "相关", "指代", "用途", "说明", "未明", "所指", "具体",
    "展开", "形式", "偏好", "尝试", "邀请", "答应", "尚未", "依然", "仍在", "等一个",
    "态度", "延伸", "得到", "什么", "怎么", "为什么", "什么（）", "（", "）",
)


def clean(text):
    return re.sub(r"[\s，。、？！！：；「」“”（）()\[\]…—\-/]", "", text or "")


def bigrams(text):
    t = clean(text)
    return {t[i:i + 2] for i in range(len(t) - 1)}


def jac(a, b):
    return len(a & b) / len(a | b) if (a and b) else 0.0


def core(title, prose):
    text = title
    for word in TEMPLATE_WORDS + (PROSE_WORDS if prose else ()):
        text = text.replace(word, "")
    return text


POSITIVE = [
    ("1670681411", "用户邀请角色尝试奶茶/去糖奶茶，角色是否接受、后续是否真的会一起喝，尚未有结果。",
     "用户邀请角色尝试无糖奶茶，角色是否接受、后续是否真的会一起喝，尚未有结果。"),
    ("1670681411", "用户邀请角色尝试无糖奶茶，角色是否接受、后续是否真的会一起喝，尚未有结果。",
     "奶茶邀约尚未落地：用户邀请角色尝试奶茶/无糖奶茶，是否真的会一起喝还没有结果。"),
    ("1670681411", "用户说“我试试什么（）”，具体想试什么未明，可留待后续自然聊到。",
     "用户“我试试什么（）”的具体所指未明，可留待后续自然澄清。"),
    ("994959351", "用户对回应形式（换行/句号）的偏好尚未被稳定满足",
     "用户对回应形式的偏好从「不要句号」延伸到「不要换行」，尚未被稳定满足"),
]

# 构造负样本：换掉主题词
SWAPS = [("奶茶/去糖奶茶", "咖啡"), ("奶茶/无糖奶茶", "咖啡"), ("无糖奶茶", "咖啡"),
         ("奶茶/去糖奶茶", "柚子社的作品"), ("我试试什么（）", "换个头像")]


def run(prose: bool, threshold: float):
    pos_hits = sum(1 for _, a, b in POSITIVE
                   if jac(bigrams(core(a, prose)), bigrams(core(b, prose))) >= threshold)
    # 构造负样本
    neg_hits = 0
    for _, a, b in POSITIVE:
        for old, new in SWAPS:
            if old in a:
                neg_hits += 1 if (jac(bigrams(core(a.replace(old, new), prose)),
                                      bigrams(core(b, prose))) >= threshold) else 0
    # 跨实例真实负样本
    tags = sorted(TITLES)
    cross = 0
    for i, t1 in enumerate(tags):
        for t2 in tags[i + 1:]:
            for x in TITLES[t1]:
                for y in TITLES[t2]:
                    if jac(bigrams(core(x, prose)), bigrams(core(y, prose))) >= threshold:
                        cross += 1
    return pos_hits, neg_hits, cross


print("正样本 %d 对；构造负样本最多 %d 个；跨实例对若干" % (
    len(POSITIVE), sum(1 for _, a, _ in POSITIVE for old, _ in SWAPS if old in a)))
print()
print("%-28s %-6s %-10s %-12s %-10s" % ("core 剥离策略", "阈值", "正样本命中", "构造负样本误合", "跨实例误合"))
for prose in (False, True):
    for threshold in (0.5, 0.55, 0.6, 0.65):
        pos, neg, cross = run(prose, threshold)
        print("%-28s %-6.2f %-10d %-12d %-10d" % (
            "模板词" if not prose else "模板词+散文套话", threshold, pos, neg, cross))
print()
print("=== 剥完后各正样本的 core 与相似度（散文套话版）===")
for tag, a, b in POSITIVE:
    ca, cb = core(a, True), core(b, True)
    print("  %.2f  %s" % (jac(bigrams(ca), bigrams(cb)), tag))
    print("        A core = %r" % ca)
    print("        B core = %r" % cb)
print()
print("=== 构造负样本（奶茶->咖啡）在散文套话版下的相似度 ===")
for tag, a, b in POSITIVE:
    for old, new in SWAPS:
        if old in a:
            ca, cb = core(a.replace(old, new), True), core(b, True)
            print("  %.2f  %s -> %s" % (jac(bigrams(ca), bigrams(cb)), old, new))
            print("        A core = %r" % ca)
            print("        B core = %r" % cb)
