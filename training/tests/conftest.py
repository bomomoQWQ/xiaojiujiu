"""pytest 共享夹具。

关键约束：**全部测试必须在无网络、无大模型、无 GPU 的环境下通过。**

因此这里提供的夹具全部是：
  * 合成的（:mod:`qboss_training.fixtures`）；
  * 本地 HTTP 假服务端（``httpx.MockTransport``，不产生真实连接）；
  * 临时目录（``tmp_path``），测试之间不共享状态。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

import pytest

# 让测试可以直接 import qboss_training（无需先 pip install -e .）
SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from qboss_training.fixtures import build_fixture_records  # noqa: E402
from qboss_training.utils.secrets import forget_secrets  # noqa: E402

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures"


@pytest.fixture(autouse=True)
def _clean_secrets() -> Iterable[None]:
    """每个测试前后清空运行时密钥登记，避免互相污染。"""
    forget_secrets()
    yield
    forget_secrets()


@pytest.fixture
def fixture_records() -> list[dict[str, Any]]:
    """60 条混合任务夹具记录（30 event_eval + 30 emotion_explain）。"""
    return build_fixture_records(event_eval_count=30, emotion_explain_count=30, seed=1234)


@pytest.fixture
def small_fixture_records() -> list[dict[str, Any]]:
    """12 条小夹具，适合快速单测。"""
    return build_fixture_records(event_eval_count=6, emotion_explain_count=6, seed=7)


@pytest.fixture
def fixture_path(tmp_path: Path, fixture_records: Sequence[dict[str, Any]]) -> Path:
    """把夹具写成 JSONL 文件。"""
    from qboss_training.utils.io import write_jsonl

    target = tmp_path / "fixture.jsonl"
    write_jsonl(target, fixture_records, sort_by="id")
    return target


@pytest.fixture
def sample_event_eval() -> dict[str, Any]:
    """一条已知合法的事件评价记录（架构文档 §8 的例子）。"""
    return {
        "id": "ee_example_0001",
        "task": "event_eval",
        "input": {
            "current_event": {"speaker": "user", "text": "不知道，可能没时间。"},
            "context_turns": [{"speaker": "char", "text": "你今晚还会回来吗？"}],
            "background_mood": {"valence": -0.15, "arousal": 0.32},
            "character_values": {
                "autonomy": 0.55,
                "boundary": 0.6,
                "relatedness": 0.7,
                "stability": 0.6,
                "honesty": 0.6,
            },
            "known_facts": ["用户提过明天有个面试"],
        },
        "output": {
            "direction": "-",
            "impact": 0.62,
            "activation": 0.44,
            "uncertainty": 0.71,
            "relation_signal": "slight_distance",
            "responsibility": "unclear",
            "confidence": 0.83,
            "evidence": "不知道，可能没时间",
        },
        "meta": {"scenario_id": "s_ee_1", "event_kind": "user_busy", "source": "test"},
    }


@pytest.fixture
def sample_emotion_explain() -> dict[str, Any]:
    """一条已知合法的情绪解释记录（架构文档 §11 的例子）。"""
    return {
        "id": "ex_example_0001",
        "task": "emotion_explain",
        "input": {
            "event": {"char": "你今晚还会回来吗？", "user": "不知道，可能没时间。"},
            "background_mood": {"valence": -0.15, "arousal": 0.32},
            "active_emotions": [
                {
                    "target": "user",
                    "cause": "用户今晚可能没有时间继续交流",
                    "direction": "-",
                    "intensity": 0.56,
                    "semantic_label": None,
                    "action_tendency": "希望确认之后是否还会回来",
                }
            ],
            "approach_drive": 0.61,
            "restraint": 0.72,
            "conflict_present": True,
        },
        "output": {
            "experience": "有些失落，也有一点不确定。",
            "focus": "比较在意今晚的交流是否会就此中断。",
            "conflict": "想确认之后还会不会继续交流，但又不想显得太依赖。",
            "impulse": "想确认用户之后是否还会回来。",
            "inhibition": "不希望给用户增加压力。",
            "expression": "表达上会稍微显得舍不得，但整体仍然克制。",
        },
        "meta": {"scenario_id": "s_ex_1", "event_kind": "user_busy", "source": "test"},
    }


# --------------------------------------------------------------------------
# 假服务端
# --------------------------------------------------------------------------

class FakeDeepSeekServer:
    """OpenAI 兼容的假服务端（纯内存，无网络）。

    可以通过 ``responder`` 自定义行为；默认返回一个合法的事件评价 JSON。
    """

    def __init__(
        self,
        responder: Callable[[dict[str, Any], int], Any] | None = None,
        *,
        status: int = 200,
    ) -> None:
        self.responder = responder
        self.status = status
        self.requests: list[dict[str, Any]] = []
        self.raw_requests: list[Any] = []

    @property
    def call_count(self) -> int:
        return len(self.requests)

    def default_payload(self, index: int) -> dict[str, Any]:
        return {
            "input": {
                "current_event": {
                    "speaker": "user",
                    "text": f"这是第 {index} 条合成事件，我今晚想自己待着。",
                },
                "context_turns": [],
                "background_mood": {"valence": -0.3, "arousal": 0.4},
                "character_values": {"autonomy": 0.7, "boundary": 0.7},
                "known_facts": [],
            },
            "output": {
                "direction": "-",
                "impact": 0.5,
                "activation": 0.3,
                "uncertainty": 0.6,
                "relation_signal": "slight_distance",
                "responsibility": "other",
                "confidence": 0.7,
                "evidence": "我今晚想自己待着",
            },
        }

    def __call__(self, request: Any) -> Any:
        import httpx

        body = json.loads(request.content)
        self.requests.append(body)
        self.raw_requests.append(request)

        if self.responder is not None:
            outcome = self.responder(body, self.call_count)
            if isinstance(outcome, httpx.Response):
                return outcome
            if isinstance(outcome, tuple):
                status, payload = outcome
                return httpx.Response(status, json=payload)
            payload = outcome
        else:
            payload = self.default_payload(self.call_count)

        content = (
            payload
            if isinstance(payload, str)
            else json.dumps(payload, ensure_ascii=False)
        )
        return httpx.Response(
            self.status,
            json={
                "id": f"chatcmpl-fake-{self.call_count}",
                "object": "chat.completion",
                "model": "deepseek-chat",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": content},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 700,
                    "completion_tokens": 110,
                    "total_tokens": 810,
                },
            },
        )


@pytest.fixture
def fake_server() -> FakeDeepSeekServer:
    return FakeDeepSeekServer()


@pytest.fixture
def mock_httpx_transport(fake_server: FakeDeepSeekServer) -> Any:
    """把假服务端包成 ``httpx.MockTransport``。"""
    import httpx

    return httpx.MockTransport(fake_server)


@pytest.fixture
def fake_api_key() -> str:
    """一个显然不是真实密钥的测试 key。

    刻意不匹配真实的 ``sk-`` 前缀规则以避免被脱敏逻辑改写，
    但保留足够长度以触发密钥登记。
    """
    return "test-key-not-a-real-secret-0123456789"
