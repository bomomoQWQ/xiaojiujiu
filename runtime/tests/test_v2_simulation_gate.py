from pathlib import Path

def test_simulation_endpoint_is_controlled_by_explicit_config_flag():
    root=Path(__file__).parents[1]/'src'/'companion_runtime'
    cli=(root/'cli.py').read_text(encoding='utf-8')
    cfg=(root/'config.py').read_text(encoding='utf-8')
    assert 'v2_simulation_enabled: bool = False' in cfg
    assert 'v2_composition.enable_decision_run = bool(config.v2_simulation_enabled)' in cli
    assert 'simulate_v2_decision if config.v2_simulation_enabled else None' in cli
