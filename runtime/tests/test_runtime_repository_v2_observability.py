from companion_runtime.runtime_repository_v2 import PostgresV2RuntimeRepository

class Cursor:
    def __init__(self,rows): self.rows=rows
    def fetchall(self): return self.rows
class Conn:
    def __init__(self): self.calls=[]
    def execute(self,sql,params):
        self.calls.append((sql,params))
        if 'raw_events' in sql: return Cursor([{'event_id':'e','kind':'user_message','scope_key':'s'}])
        if 'interaction_exposures' in sql: return Cursor([{'exposure_id':'x','scope_key':'s'}])
        if 'interaction_target' in sql: return Cursor([{'target_label_id':'l','scope_key':'s'}])
        if 'parameter_snapshots' in sql: return Cursor([{'parameter_snapshot_id':'p','scope_key':'s'}])
        if 'decision_audits' in sql: return Cursor([{'decision_id':'d','scope_key':'s'}])
        return Cursor([])

def repo():
    obj=object.__new__(PostgresV2RuntimeRepository); obj.connection=Conn(); obj.scope_key='s'; return obj

def test_blackbox_reads_are_scope_bound_and_normalized():
    r=repo(); evidence=r.read_blackbox_evidence(scope_key='s'); audits=r.list_decision_audits(scope_key='s')
    assert evidence['events'][0]['event_id']=='e'; assert audits[0]['decision_id']=='d'
    assert all(params==('s',) for _sql,params in r.connection.calls)

def test_blackbox_rejects_cross_scope():
    import pytest
    r=repo()
    with pytest.raises(ValueError): r.read_blackbox_evidence(scope_key='other')
