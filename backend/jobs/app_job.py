"""App Flask liviana para jobs (cron), SIN la app web.

NUNCA importar `backend.app` ni `backend.views` desde un job: `backend.app`
ejecuta `create_app()` al importarse (AUTO_MIGRATE, create_all, ALTERs, seed de
usuarios). Esta app solo inicializa SQLAlchemy con la misma validación de
DATABASE_URL que la web (Postgres only, driver psycopg3); no registra
blueprints, ni Flask-Migrate, ni Flask-Login. `tests/test_aislamiento.py` lo
controla.

Uso:
    app = create_job_app()
    with app.app_context():
        try:
            ...
        finally:
            db.session.remove()
            db.engine.dispose()
"""
from dotenv import load_dotenv
from flask import Flask

from backend import models  # noqa: F401  registra los mappers
from backend.config import Config
from backend.extensions import db


def create_job_app() -> Flask:
    load_dotenv()  # override=False: en Railway ganan las env vars del servicio
    app = Flask("reporte_diario")
    app.config.from_object(Config())
    app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {
        "pool_pre_ping": True,
        "pool_size": 2,
        "max_overflow": 0,
    }
    db.init_app(app)
    return app
