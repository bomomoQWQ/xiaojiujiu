"""用真实的奶茶/我试试标题验证 _same_subject 为什么漏判。"""
import sys

sys.path.insert(0, "runtime/src")
from companion_runtime import unfinished  # noqa: E402

pairs = [
    ("用户邀请角色尝试奶茶/去糖奶茶，角色是否接受、后续是否真的会一起喝，尚未有结果。",
     "用户邀请角色尝试无糖奶茶，角色是否接受、后续是否真的会一起喝，尚未有结果。"),
    ("用户说“我试试什么（）”，具体想试什么未明，可留待后续自然聊到。",
     "用户“我试试什么（）”的具体所指未明，可留待后续自然澄清。"),
    ("用户对回应形式（换行/句号）的偏好尚未被稳定满足",
     "用户对回应形式的偏好从「不要句号」延伸到「不要换行」，尚未被稳定满足"),
    # 对照组：模板标题（regex 路径产出的那种），应当能判成同一主题
    ("等待面试结果", "等待面试结果通知"),
    ("等待考试结果", "等待面试结果"),
]

for left, right in pairs:
    core_l = unfinished._subject_core(left)
    core_r = unfinished._subject_core(right)
    print("A: %s" % left[:52])
    print("B: %s" % right[:52])
    print("   core A = %r" % core_l)
    print("   core B = %r" % core_r)
    print("   _same_subject = %s" % unfinished._same_subject(left, right))
    print()
