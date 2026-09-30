from datetime import datetime,timezone
from companion_runtime.user_model_v2_service_repository import PostgresUserModelV2ServiceRepository
from companion_runtime.user_model_v2_service import PreparedExposureV2
from companion_runtime.user_model_v2_types import InteractionExposureV2,TargetLabelV2,Target,LabelStatus,DeliveryBasis
from companion_runtime.user_model_v2_features import FeatureSnapshotV2
NOW=datetime(2026,1,1,tzinfo=timezone.utc)

def test_legacy_attempt_id_is_mapped_to_uuid_before_postgres_insert():
    exposure=InteractionExposureV2(exposure_id='att_abc',scope_key='s',occurred_at=NOW,window_started_at=NOW,window_ends_at=NOW.replace(hour=1),horizon_seconds=3600,delivery_basis=DeliveryBasis.DELIVERED,created_at=NOW,updated_at=NOW)
    features=FeatureSnapshotV2(scope_key='s',exposure_id='att_abc',action_json={},context_json={},context_cutoff_at=NOW,created_at=NOW)
    labels=tuple(TargetLabelV2(label_id=f'l-{t.value}',exposure_id='att_abc',scope_key='s',target=t,status=LabelStatus.PENDING,value=None,window_started_at=NOW,window_ends_at=NOW.replace(hour=1),horizon_seconds=3600,created_at=NOW,updated_at=NOW) for t in Target)
    prepared=PreparedExposureV2(exposure=exposure,features=features,labels=labels)
    # Test the mapping through a tiny subclass that captures the transformed DTO.
    class Stop(Exception): pass
    class Repo(PostgresUserModelV2ServiceRepository):
        def _transaction(self):
            from contextlib import nullcontext; return nullcontext()
        def get_prepared_exposure(self,**kw): return None
    class Conn:
        def execute(self,*args):
            if args[0].startswith('SELECT pg_'): return self
            raise Stop(args)
    base=type('Base',(),{'insert_exposure':lambda self,**kw: (_ for _ in ()).throw(Stop(kw))})()
    import pytest
    with pytest.raises(Stop) as caught: Repo(Conn(),base).put_prepared_exposure(prepared=prepared,idempotency_key='k')
    import uuid
    uuid.UUID(str(caught.value.args[0]['exposure_id']))
