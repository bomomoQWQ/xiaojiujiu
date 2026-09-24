"""提示词里的「格式样例」必须是一份运行时真能照做的样例。

这类毛病的共同形状：样例里**写了**键名，但结构或键名跟读取方对不上。模型照着抄，
运行时默默丢掉，而"刷新运行了、没有报错"的日志一切正常。已经在生产里量到两次：

* ``candidate_intent_operations`` 写成 ``{"operation": "add", "intent": …}``，
  而 reducer 读的是 ``payload.op`` + ``payload.candidate`` → ``candidate_intent:ValueError``；
* ``user_model_evidence_suggestions`` 写成 ``{"trait": …, "weight": …}``，
  而读取方要的是 ``statement`` → ``user_model_evidence:ValueError``。

所以这里**不检查"提示词里有没有这几个键"**——那种断言正是漏掉这两个的原因——而是把提示词
里那整段样例原样当模型回复喂进真实 Runtime，看哪些字段真的落地。

程序侧另有一条便宜的钉子（``runtime/tests/test_providers.py`` 里的
``test_the_prompt_s_candidate_example_is_a_shape_the_runtime_applies``），它只走候选那一个
字段、不需要 boot Runtime；这一条负责其余字段。
"""

from __future__ import annotations

import json

import pytest

from cf.harness import Harness, HarnessConfig
from cf.mock_openai import MockReply, MockScript
from conftest import PROGRAM_SRC, program_available

pytestmark = pytest.mark.skipif(
    not program_available(), reason="the program checkout or uvicorn is not available"
)


@pytest.fixture(scope="module")
def harness(tmp_path_factory) -> Harness:
    """A booted Runtime, kept alive for the module: booting is the expensive part."""
    instance = Harness(
        HarnessConfig(
            run_dir=tmp_path_factory.mktemp("semantic-example"),
            program_src=PROGRAM_SRC,
            start_time="2026-09-15T03:00:00Z",
            time_scale=0.0,
            heartbeat_interval_s=0,
            echo_logs=False,
        )
    )
    instance.start()
    try:
        yield instance
    finally:
        instance.stop()


def test_the_prompt_s_own_example_is_accepted_by_the_reader(harness: Harness) -> None:
    """Feed the example back as if the model had copied it, and count what survives."""
    from companion_runtime.deep_refresh import FIELD_TO_KIND
    from companion_runtime.providers import DEEP_REFRESH_SYSTEM_PROMPT

    example = DEEP_REFRESH_SYSTEM_PROMPT.split("格式样例：", 1)[1].lstrip()
    sample, _ = json.JSONDecoder().raw_decode(example)
    assert isinstance(sample, dict), sample
    assert "candidate_intent_operations" in sample, sample

    # The example cites ``evt_x``; a real refresh must cite a real event id.
    harness.user_turn("我明天下午三点面试，结束了告诉你。")
    events = harness.program.get("/events", event_type="user_message", limit=5)
    event_id = events["events"][-1]["event_id"]
    grounded = json.loads(json.dumps(sample, ensure_ascii=False).replace("evt_x", event_id))

    harness.mock.script = MockScript([MockReply(payload=grounded)], repeat_last=True)
    result = harness.program.refresh(now="2026-09-15T04:00:00Z", major_event=True)

    assert result["ran"] is True
    assert result["violations"] == [], result["violations"]

    # Every field the example shows has to survive the reader. ``unfinished_matter`` is
    # excluded on purpose: the user turn's own refresh already created that matter, so
    # this one is a restatement and is skipped by ``already_spoken_for`` rather than
    # dropped for being malformed.
    expected_fields = [name for name in grounded if name in FIELD_TO_KIND]
    assert expected_fields, grounded
    expected_kinds = {FIELD_TO_KIND[name] for name in expected_fields} - {"unfinished_matter"}
    missing = expected_kinds - set(result["applied"])
    assert not missing, (
        f"the prompt's own example was dropped by the reader: {sorted(missing)}; "
        f"applied={result['applied']}"
    )

    # The expensive half of the old shape: an operation that never applies never settles
    # anything, so the source event stays unresolved and keeps triggering refreshes.
    assert result["settled_events"] >= 1, result
