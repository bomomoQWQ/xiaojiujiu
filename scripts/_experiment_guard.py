"""给 0.6 的 bigram 加一道护栏：要求两个标题"几乎只差主题词"。

理由：同一话题被模型换个说法重写时，差异只占标题的一小段；而**不同主题**共用套话时，
差异落在主题词上 —— 如果主题词本身很短（奶茶/咖啡），差异占比仍然小，所以单看占比不够，
必须和 bigram 一起用。这里量两种候选护栏：
   护栏1: 对称差字符数 / 较长标题长度 <= 20%
   护栏2: 对称差字符数 <= 6（绝对值）
并打印真实正样本 / 构造负样本的取值，用数据定阈值。
"""
import json
import re

TITLES = json.load(open(r"F:\理解痞老板\.scratch_titles.json", encoding="utf-8"))
TEMPLATE_WORDS = ("等待", "用户", "结果", "告知", "关心", "后续", "是否", "顺利", "消息")


def clean(text):
    return re.sub(r"[\s，。、？！！：；「」“”（）()\[\]…—\-/]", "", text or "")


def bigrams(text):
    t = clean(text)
    return {t[i:i + 2] for i in range(len(t) - 1)}


def jac(a, b):
    return len(a & b) / len(a | b) if (a and b) else 0.0


def diff_chars(a, b):
    """对称差：A 独有的 bigram + B 独有的 bigram，各折算成字符数（bigram 数 + 1）。"""
    A, B = bigrams(a), bigrams(b)
    return (len(A - B) + len(B - A))


POSITIVE = [
    ("奶茶/去糖奶茶 vs 无糖奶茶",
     "用户邀请角色尝试奶茶/去糖奶茶，角色是否接受、后续是否真的会一起喝，尚未有结果。",
     "用户邀请角色尝试无糖奶茶，角色是否接受、后续是否真的会一起喝，尚未有结果。"),
    ("奶茶 vs 奶茶邀约尚未落地",
     "用户邀请角色尝试无糖奶茶，角色是否接受、后续是否真的会一起喝，尚未有结果。",
     "奶茶邀约尚未落地：用户邀请角色尝试奶茶/无糖奶茶，是否真的会一起喝还没有结果。"),
    ("我试试 vs 我试试（另一说法）",
     "用户说“我试试什么（）”，具体想试什么未明，可留待后续自然聊到。",
     "用户“我试试什么（）”的具体所指未明，可留待后续自然澄清。"),
]

CONSTRUCTED = [
    ("奶茶/去糖奶茶 -> 咖啡",
     "用户邀请角色尝试咖啡，角色是否接受、后续是否真的会一起喝，尚未有结果。",
     "用户邀请角色尝试无糖奶茶，角色是否接受、后续是否真的会一起喝，尚未有结果。"),
    ("无糖奶茶 -> 咖啡（对第二例）",
     "用户邀请角色尝试咖啡，角色是否接受、后续是否真的会一起喝，尚未有结果。",
     "奶茶邀约尚未落地：用户邀请角色尝试奶茶/无糖奶茶，是否真的会一起喝还没有结果。"),
    ("我试试什么（）-> 换个头像",
     "用户说“换个头像”，具体想试什么未明，可留待后续自然聊到。",
     "用户“我试试什么（）”的具体所指未明，可留待后续自然澄清。"),
    ("面试结果 vs 考试结果（模板标题）",
     "等待面试结果",
     "等待考试结果"),
]

print("%-34s %-8s %-10s %-10s" % ("对", "bigram", "对称差", "占比"))
print("-" * 66)
for label, a, b in POSITIVE:
    n = len(clean(a))
    print("%-34s %-8.3f %-10d %.0f%%   [正样本]" % (label, jac(bigrams(a), bigrams(b)),
                                                diff_chars(a, b), 100 * diff_chars(a, b) / n))
for label, a, b in CONSTRUCTED:
    n = len(clean(a))
    print("%-34s %-8.3f %-10d %.0f%%   [负样本]" % (label, jac(bigrams(a), bigrams(b)),
                                                diff_chars(a, b), 100 * diff_chars(a, b) / n))
print()
print("=== 各种组合下的判定（bigram>=0.6 且 占比<=X%）===")
for limit in (0.15, 0.20, 0.25, 0.30, 0.40):
    pos = sum(1 for _, a, b in POSITIVE
              if jac(bigrams(a), bigrams(b)) >= 0.6
              and diff_chars(a, b) / len(clean(a)) <= limit)
    neg = sum(1 for _, a, b in CONSTRUCTED
              if jac(bigrams(a), bigrams(b)) >= 0.6
              and diff_chars(a, b) / len(clean(a)) <= limit)
    print("  占比<=%3.0f%%:  正样本命中 %d/%d   负样本误合 %d/%d" % (
        limit * 100, pos, len(POSITIVE), neg, len(CONSTRUCTED)))
