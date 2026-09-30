"""Production v2 isolates the user model by conversation origin, not process id."""
from pathlib import Path

def test_cli_composes_v2_with_conversation_scope() -> None:
    source=(Path(__file__).parents[1]/'src'/'companion_runtime'/'cli.py').read_text(encoding='utf-8')
    block=source.split('v2_composition = build_v2_composition(',1)[1].split(')',1)[0]
    assert 'scope_key=config.conversation_id' in block
    assert 'scope_key=config.runtime_id' not in block
