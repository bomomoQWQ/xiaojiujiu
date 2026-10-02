from __future__ import annotations

from types import SimpleNamespace

from companion_runtime.config import RuntimeConfig
from companion_runtime.langchao_authority import AuthorityEngine, AuthorityMode
from companion_runtime.langchao_live_wiring import AuthorityRoutedEndogenousRound
from companion_runtime.runtime_v2 import V2RuntimeCoordinator
from test_runtime_v2 import NOW, coordinator


class Authority:
    def __init__(self, engine, mode, may_dispatch):
        self.value = SimpleNamespace(
            engine_key=engine, mode=mode, may_dispatch=may_dispatch, revision=1
        )

    def get_active(self):
        return self.value


def test_live_config_is_default_off_and_scope_allowlisted():
    config = RuntimeConfig()
    assert config.langchao.live_enabled is False
    assert config.langchao.live_scope_allowlist == []
    assert config.langchao.live_allowed("scope") is False
    config.langchao.live_enabled = True
    config.langchao.live_scope_allowlist.append("scope")
    assert config.langchao.live_allowed("scope") is True


def test_v2_assess_has_no_hazard_audit_or_commit_side_effects():
    order = []
    runtime, _legacy, repository = coordinator(order)
    before_rng = runtime.rng.getstate()
    result = runtime.assess_endogenous(
        decision_id="assess:1", now=NOW, elapsed_allowed_seconds=1_000_000.0
    )
    assert result.acted is False and result.reason == "assessment_only"
    assert result.assessments
    assert runtime.rng.getstate() == before_rng
    assert repository.audits == {}
    assert "legacy_outbox_commit" not in order


def test_runtime_v2_live_routes_to_only_v2_commit():
    calls = []
    v2 = SimpleNamespace(
        decide_endogenous=lambda **kw: calls.append(("v2", kw)) or "v2-result",
        assess_endogenous=lambda **kw: calls.append(("assess", kw)) or "assessment",
    )
    live = SimpleNamespace(run=lambda *a, **kw: calls.append(("langchao", kw)))
    router = AuthorityRoutedEndogenousRound(
        scope_key="scope", v2_coordinator=v2,
        authority_reader=Authority(AuthorityEngine.RUNTIME_V2, AuthorityMode.LIVE, True),
        langchao_live_runner=live,
    )
    assert router.run(decision_id="d", now=NOW, elapsed_allowed_seconds=1) == "v2-result"
    assert [item[0] for item in calls] == ["v2"]


def test_langchao_live_assesses_then_commits_only_langchao_once():
    calls = []
    assessment = SimpleNamespace(decision_id="d")
    v2 = SimpleNamespace(
        decide_endogenous=lambda **kw: calls.append(("v2", kw)),
        assess_endogenous=lambda **kw: calls.append(("assess", kw)) or assessment,
    )
    live = SimpleNamespace(run=lambda value, **kw: calls.append(("langchao", kw)) or "live")
    router = AuthorityRoutedEndogenousRound(
        scope_key="scope", v2_coordinator=v2,
        authority_reader=Authority(AuthorityEngine.LANGCHAO, AuthorityMode.LIVE, True),
        langchao_live_runner=live, live_enabled=True,
        live_scope_allowlist=("scope",),
    )
    assert router.run(decision_id="d", now=NOW, elapsed_allowed_seconds=1) == "live"
    assert [item[0] for item in calls] == ["assess", "langchao"]


def test_langchao_shadow_assesses_then_runs_shadow_with_routed_authority_revision():
    calls = []
    assessment = SimpleNamespace(decision_id="d")
    v2 = SimpleNamespace(
        decide_endogenous=lambda **kw: calls.append(("decide", kw)),
        assess_endogenous=lambda **kw: calls.append(("assess", kw)) or assessment,
    )
    shadow = SimpleNamespace(
        run=lambda value, **kw: calls.append(("shadow", value, kw)) or "shadow-result"
    )
    router = AuthorityRoutedEndogenousRound(
        scope_key="scope", v2_coordinator=v2,
        authority_reader=Authority(AuthorityEngine.LANGCHAO, AuthorityMode.SHADOW, False),
        langchao_shadow_runner=shadow,
    )

    assert router.run(decision_id="d", now=NOW, elapsed_allowed_seconds=1) == "shadow-result"
    assert [item[0] for item in calls] == ["assess", "shadow"]
    assert calls[1][1] is assessment
    assert calls[1][2] == {"now": NOW, "authority_revision": 1}


def test_none_or_disabled_authority_only_assesses_without_shadow_writes():
    for engine, mode, may_dispatch in (
        (AuthorityEngine.NONE, AuthorityMode.DISABLED, False),
        (AuthorityEngine.LANGCHAO, AuthorityMode.DISABLED, False),
        (AuthorityEngine.LANGCHAO, AuthorityMode.SHADOW, True),
    ):
        calls = []
        v2 = SimpleNamespace(
            decide_endogenous=lambda **kw: calls.append("decide"),
            assess_endogenous=lambda **kw: calls.append("assess") or "assessment",
        )
        shadow = SimpleNamespace(run=lambda *a, **kw: calls.append("shadow"))
        router = AuthorityRoutedEndogenousRound(
            scope_key="scope", v2_coordinator=v2,
            authority_reader=Authority(engine, mode, may_dispatch),
            langchao_shadow_runner=shadow,
        )

        result = router.run(decision_id="d", now=NOW, elapsed_allowed_seconds=1)
        if hasattr(result, "reason"):
            assert result.reason.value == "permission_denied"
            assert result.stage == "router"
        else:
            assert result == "assessment"
        assert calls == ["assess"]


def test_langchao_shadow_requires_configured_runner_after_pure_assessment():
    calls = []
    router = AuthorityRoutedEndogenousRound(
        scope_key="scope",
        v2_coordinator=SimpleNamespace(
            decide_endogenous=lambda **kw: calls.append("decide"),
            assess_endogenous=lambda **kw: calls.append("assess") or "assessment",
        ),
        authority_reader=Authority(AuthorityEngine.LANGCHAO, AuthorityMode.SHADOW, False),
    )

    import pytest

    with pytest.raises(RuntimeError, match="no configured shadow runner"):
        router.run(decision_id="d", now=NOW, elapsed_allowed_seconds=1)
    assert calls == ["assess"]


def test_disabled_or_unallowlisted_never_calls_live_runner():
    calls = []
    v2 = SimpleNamespace(
        decide_endogenous=lambda **kw: calls.append("v2"),
        assess_endogenous=lambda **kw: calls.append("assess") or "assessment",
    )
    live = SimpleNamespace(run=lambda *a, **kw: calls.append("langchao"))
    router = AuthorityRoutedEndogenousRound(
        scope_key="scope", v2_coordinator=v2,
        authority_reader=Authority(AuthorityEngine.LANGCHAO, AuthorityMode.LIVE, True),
        langchao_live_runner=live, live_enabled=True,
        live_scope_allowlist=("other",),
    )
    assert router.run(decision_id="d", now=NOW, elapsed_allowed_seconds=1) == "assessment"
    assert calls == ["assess"]
