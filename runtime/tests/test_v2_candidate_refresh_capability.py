from datetime import datetime, timezone
from types import SimpleNamespace
from companion_runtime.legacy_bridge_v2 import ConcreteLegacyRuntimeV2Bridge

class Empty:
    def list_active(self,limit): return []

def test_empty_pool_uses_mechanical_v2_refresh_capability():
    candidate=SimpleNamespace(candidate_id='c',type='contact',intent='hi',goal='',target='',sources=[],internal_need=.1,unfinished_relevance=0,emotion_relevance=0)
    runtime=SimpleNamespace(
        projections=SimpleNamespace(candidates=Empty()),
        config=SimpleNamespace(candidate=SimpleNamespace(max_active=12)),
        refresh_candidates_for_v2=lambda now:[candidate],
        _event_ids_behind=lambda source:[],
    )
    bridge=ConcreteLegacyRuntimeV2Bridge(runtime)
    assert bridge.candidates(scope_key='s',now=datetime.now(timezone.utc))[0].candidate_id=='c'
