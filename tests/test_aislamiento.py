"""El job NO puede importar la app web.

`backend.app` ejecuta `create_app()` al importarse (AUTO_MIGRATE, create_all,
ALTERs, seed de usuarios). Si el job lo arrastra, cada corrida del cron haría
DDL contra producción. Se chequea en un subprocess limpio para que los imports
de otros tests no contaminen `sys.modules`.
"""
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent

MODULOS_DEL_JOB = (
    "backend.jobs.app_job",
    "backend.services.reporte_diario.pdf",
    "backend.services.mailer",
    "backend.services.mailer.archivo",
    "backend.services.mailer.gmail_api",
    "backend.jobs.reporte_diario",
)

_SCRIPT = """
import importlib, sys
importlib.import_module({modulo!r})
prohibidos = sorted(m for m in sys.modules if m == "backend.app" or m == "backend.views" or m.startswith("backend.views."))
print(",".join(prohibidos))
"""


@pytest.mark.parametrize("modulo", MODULOS_DEL_JOB)
def test_job_no_importa_backend_app_ni_views(modulo):
    resultado = subprocess.run(
        [sys.executable, "-c", _SCRIPT.format(modulo=modulo)],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert resultado.returncode == 0, resultado.stderr
    assert resultado.stdout.strip() == ""


_SCRIPT_PURO = """
import sys
import backend.services.reporte_diario.pdf
cargados = sorted(
    m for m in sys.modules
    if m.split(".")[0] in ("flask", "flask_sqlalchemy", "sqlalchemy", "psycopg")
    or m in ("backend.models", "backend.extensions")
)
print(",".join(cargados))
"""


def test_render_pdf_es_puro_sin_flask_ni_db():
    resultado = subprocess.run(
        [sys.executable, "-c", _SCRIPT_PURO],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert resultado.returncode == 0, resultado.stderr
    assert resultado.stdout.strip() == ""


def test_create_job_app_no_registra_blueprints_ni_migrate(monkeypatch):
    # URL ficticia: create_job_app no se conecta al construir la app.
    monkeypatch.setenv("DATABASE_URL", "postgresql://nadie@127.0.0.1:1/no_existe")
    from backend.jobs.app_job import create_job_app

    app = create_job_app()

    assert app.blueprints == {}
    assert "migrate" not in app.extensions
    assert "sqlalchemy" in app.extensions
    opciones = app.config["SQLALCHEMY_ENGINE_OPTIONS"]
    assert opciones == {"pool_pre_ping": True, "pool_size": 2, "max_overflow": 0}
    assert app.config["SQLALCHEMY_DATABASE_URI"].startswith("postgresql+psycopg://")
