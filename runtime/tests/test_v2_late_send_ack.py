"""A delivered send report arriving after lease exhaustion is not discarded."""
from companion_runtime.api_v1 import _already_settled, ACTION_SEND, STATUS_OK
from companion_runtime.typing import AttemptState, OutboxStatus
class Attempt: state=AttemptState.FAILED.value

def test_successful_send_report_on_any_terminal_row_reaches_late_report_handler():
    for status in (OutboxStatus.FAILED.value, OutboxStatus.CANCELLED.value, OutboxStatus.DELIVERED.value):
        row=type('Row',(),{'status':status})()
        assert _already_settled(row=row,attempt=Attempt(),action_type=ACTION_SEND,status=STATUS_OK) is False
