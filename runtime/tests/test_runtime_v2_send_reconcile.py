from pathlib import Path

def test_send_ack_records_terminal_reconciled_stage():
    src=(Path(__file__).parents[1]/'src'/'companion_runtime'/'runtime_v2.py').read_text(encoding='utf-8')
    block=src.split('def after_legacy_send_ack',1)[1].split('def reconcile',1)[0]
    assert 'DecisionStage.RECONCILED' in block
    assert '"reason": "sent" if sent else "send_failed"' in block
