from pathlib import Path

def test_fleet_children_inherit_postgres_and_do_not_create_sqlite_paths():
    source=(Path(__file__).parents[2]/'scripts'/'runtime_fleet.py').read_text(encoding='utf-8')
    assert 'self.env.pop("CR_STORAGE__DATABASE_PATH", None)' in source
    assert 'self.env["CR_STORAGE__DATABASE_PATH"] =' not in source
    assert 'self.env["CR_CONVERSATION_ID"] = session' in source
    assert 'self.env["CR_RUNTIME_ID"] = f"companion-{self.slug}"' in source
    assert 'self.env["CR_STORAGE__SCHEMA"] = raw_schema' in source
    assert 'len(raw_schema.encode("utf-8")) > 63' in source
