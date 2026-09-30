from pathlib import Path

def test_simulation_endpoint_is_controlled_by_explicit_config_flag():
    root=Path(__file__).parents[1]/'src'/'companion_runtime'
    cli=(root/'cli.py').read_text(encoding='utf-8')
    cfg=(root/'config.py').read_text(encoding='utf-8')
    assert 'v2_simulation_enabled: bool = False' in cfg
    assert 'v2_simulation_ignore_repeat_limits: bool = False' in cfg
    assert 'v2_composition.enable_decision_run = bool(config.v2_simulation_enabled)' in cli
    assert 'simulate_v2_decision if config.v2_simulation_enabled else None' in cli
    assert 'elapsed_allowed_seconds=1_000_000_000.0' in cli
    assert '"utility_threshold", -1_000_000.0' in cli
    assert '"utility_threshold", previous_threshold' in cli
    assert '"repeat", previous_repeat' in cli
    assert 'v2_maintenance.run_due(now=utcnow(), force=True, fit=True)' in cli
