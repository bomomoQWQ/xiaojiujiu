from uuid import uuid4
from companion_runtime.api_v2_observability import _jsonable

def test_uuid_evidence_is_json_safe():
    value=uuid4()
    assert _jsonable({'id':value})=={'id':str(value)}
