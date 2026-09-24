"""调试：为什么 memory suggestion 的 kind 落成了 episodic。

复现 test_deep_refresh 的那一步，并打印刷新前后所有 pending candidates。
"""
import sys

sys.path.insert(0, "runtime/tests")
sys.path.insert(0, "runtime/src")

from datetime import timedelta  # noqa: E402

import test_deep_refresh as tdr  # noqa: E402
from companion_runtime.providers import DeepRefreshSuggestions  # noqa: E402

runtime = tdr._runtime()
try:
    first = runtime.process_user_message(content="算了，也没什么。", timestamp=tdr.BASE_TIME)
    print("ingest 后的 pending candidates:")
    for item in runtime.projections.memory.pending_candidates():
        print("   %s kind=%s summary=%s" % (item.candidate_id, item.kind, item.summary[:40]))

    runtime.semantic_provider = tdr._StubProvider(
        DeepRefreshSuggestions(
            degraded=False,
            memory_suggestions=[
                {"summary": "他喜欢下雨天", "sources": [first.event.event_id], "kind": "user_preference"}
            ],
        )
    )
    outcome = runtime.deep_refresh(now=tdr.BASE_TIME + timedelta(minutes=5), force=True)
    print("refresh applied=%s violations=%s" % (outcome.applied, (outcome.violations or [])[:2]))
    print("刷新后的 pending candidates:")
    for item in runtime.projections.memory.pending_candidates():
        print("   %s kind=%s summary=%s" % (item.candidate_id, item.kind, item.summary[:40]))
    print("全部 candidates（含各种状态）:")
    for item in runtime.projections.memory.list_candidates(limit=10):
        print("   %s status=%s kind=%s summary=%s" % (
            item.candidate_id, item.status, item.kind, item.summary[:40]))
finally:
    runtime.close()
