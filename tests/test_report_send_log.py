"""Estructura de report_send_log: modelo y migración (sin DB)."""
from pathlib import Path

from alembic.config import Config as AlembicConfig
from alembic.script import ScriptDirectory
from sqlalchemy import CheckConstraint, UniqueConstraint

from backend.models import ReportSendLog

MIGRATIONS = Path(__file__).resolve().parent.parent / "migrations"


def _script_directory() -> ScriptDirectory:
    cfg = AlembicConfig()
    cfg.set_main_option("script_location", str(MIGRATIONS))
    return ScriptDirectory.from_config(cfg)


def test_migracion_es_la_unica_head_y_sigue_a_archived_company():
    script = _script_directory()
    assert script.get_heads() == ["c3e5a9d1f7b2"]
    assert script.get_revision("c3e5a9d1f7b2").down_revision == "a7d3b1e8c4f2"


def test_modelo_columnas_y_nulabilidad():
    cols = ReportSendLog.__table__.columns
    assert set(cols.keys()) == {
        "id", "report_key", "fecha_negocio", "status", "attempts", "recipient",
        "message_id", "error", "created_at", "updated_at", "sent_at",
    }
    obligatorias = {c.name for c in cols if not c.nullable}
    assert obligatorias == {
        "id", "report_key", "fecha_negocio", "status", "attempts", "created_at", "updated_at",
    }
    assert cols["attempts"].server_default.arg == "1"


def test_modelo_unique_por_fecha_y_check_de_estado():
    constraints = ReportSendLog.__table__.constraints
    unique = {c.name: [col.name for col in c.columns] for c in constraints if isinstance(c, UniqueConstraint)}
    assert unique == {"uq_report_send_log_key_fecha": ["report_key", "fecha_negocio"]}

    checks = {c.name: str(c.sqltext) for c in constraints if isinstance(c, CheckConstraint)}
    assert checks == {"ck_report_send_log_status": "status IN ('PENDING','SENT','FAILED')"}
