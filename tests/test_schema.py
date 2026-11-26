from sqlalchemy import inspect

from pwa import db, models as m, schema


def _shape(engine):
    insp = inspect(engine)
    return {t: sorted(c["name"] for c in insp.get_columns(t)) for t in insp.get_table_names()}


def test_new_database_gets_every_table_at_the_newest_revision(tmp_path):
    engine = db.make_engine("sqlite:///%s" % (tmp_path / "new.db"))
    before, after = db.create_all(engine)
    assert before is None and after == schema.head()
    assert "site_contacts" in inspect(engine).get_table_names()
    assert db.create_all(engine) == (after, after)                 # running again changes nothing


def test_a_database_from_before_migrations_is_upgraded(tmp_path):
    engine = db.make_engine("sqlite:///%s" % (tmp_path / "old.db"))
    old_tables = [t for name, t in m.Base.metadata.tables.items() if name != "site_contacts"]
    m.Base.metadata.create_all(engine, tables=old_tables)          # what pwa-next 0.2.3 made
    with engine.begin() as conn:
        conn.execute(m.Host.__table__.insert().values(hostname="kept.example.org", source="manual"))
    before, after = db.create_all(engine)
    assert before is None and after == schema.head()
    assert "site_contacts" in inspect(engine).get_table_names()
    with engine.connect() as conn:
        assert conn.execute(m.Host.__table__.select()).fetchone().hostname == "kept.example.org"
    fresh = db.make_engine("sqlite:///%s" % (tmp_path / "fresh.db"))
    db.create_all(fresh)
    assert _shape(engine) == _shape(fresh)                          # models and migrations agree
