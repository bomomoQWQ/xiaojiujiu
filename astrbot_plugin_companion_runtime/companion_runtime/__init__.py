"""AstrBot-free core of the companion Runtime adapter.

The modules in this package never import AstrBot, which keeps the protocol,
settings, retry queue, context bridge, and outbox consumer unit-testable without
a running AstrBot instance:

``protocol``
    Versioned wire types shared with the Runtime.
``settings``
    Coerced and clamped view of the plugin config.
``retry_queue``
    Bounded, fail-open local retry queue for outbound requests.
``bridge``
    Cache + strict-deadline context injection.
``outbox``
    Lease-based execution of Runtime actions.
``http_client``
    aiohttp transport for the Runtime HTTP API.

``astrbot_executor`` and the plugin's ``main.py`` are the only AstrBot-facing
modules, and both live outside this package so the AstrBot-free guarantee is
mechanically checkable (see ``tests/test_packaging.py``).
"""

from .bridge import ContextBridge
from .coerce import as_bool, as_float, as_int, as_mapping, as_str, clamp
from .http_client import AiohttpRuntimeTransport, RuntimeTransportError, action_report_body
from .outbox import ActionExecutor, OutboxConsumer
from .protocol import (
    ACTION_RENDER,
    ACTION_SEND,
    EVENT_ASSISTANT_MESSAGE,
    EVENT_USER_MESSAGE,
    PROTOCOL_VERSION,
    STATUS_FAILED,
    STATUS_OK,
    STATUS_REJECTED,
    STATUS_SKIPPED,
    TRIGGER_LLM_REQUEST,
    TRIGGER_MESSAGE,
    ActionReport,
    AuthorizeDecision,
    AuthorizeRequest,
    ContextRequest,
    ContextSnapshot,
    EventEnvelope,
    EventRecord,
    LeaseHeartbeat,
    LeaseRequest,
    LeasedAction,
    RuntimeTransport,
    new_client_id,
    truncate_error,
    utc_now_iso,
)
from .retry_queue import BoundedRetryQueue, QueueItem, QueueStats
from .settings import OBSERVE_MODE_ALL, OBSERVE_MODE_WAKE, Settings

__all__ = [
    "ACTION_RENDER",
    "ACTION_SEND",
    "EVENT_ASSISTANT_MESSAGE",
    "EVENT_USER_MESSAGE",
    "OBSERVE_MODE_ALL",
    "OBSERVE_MODE_WAKE",
    "PROTOCOL_VERSION",
    "STATUS_FAILED",
    "STATUS_OK",
    "STATUS_REJECTED",
    "STATUS_SKIPPED",
    "TRIGGER_LLM_REQUEST",
    "TRIGGER_MESSAGE",
    "ActionExecutor",
    "ActionReport",
    "AiohttpRuntimeTransport",
    "AuthorizeDecision",
    "AuthorizeRequest",
    "BoundedRetryQueue",
    "ContextBridge",
    "ContextRequest",
    "ContextSnapshot",
    "EventEnvelope",
    "EventRecord",
    "LeaseHeartbeat",
    "LeaseRequest",
    "LeasedAction",
    "OutboxConsumer",
    "QueueItem",
    "QueueStats",
    "RuntimeTransport",
    "RuntimeTransportError",
    "Settings",
    "action_report_body",
    "as_bool",
    "as_float",
    "as_int",
    "as_mapping",
    "as_str",
    "clamp",
    "new_client_id",
    "truncate_error",
    "utc_now_iso",
]
