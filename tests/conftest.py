"""Fixtures comunes de los tests.

La mayoría de los tests corren SIN base de datos y SIN .env: prueban funciones
puras (fechas, saldos, clasificación, formato).

Los tests marcados `db` usan la base LOCAL de Postgres (la del .env). Se saltean
solos si la base no está disponible o si la URL resuelta NO apunta a localhost:
nunca corren contra otra base. No commitean nada: los datos de prueba viven en
una transacción que se descarta al final de cada test.
"""
import os
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

TZ_ART = ZoneInfo("America/Argentina/Buenos_Aires")

HOSTS_LOCALES = frozenset({"localhost", "127.0.0.1", "::1"})


@pytest.fixture
def ahora_art():
    """Fábrica de datetimes con zona horaria de Argentina.

    Uso: ahora_art(2026, 10, 29, 9, 1) -> jueves 29/10/2026 09:01 ART.
    """

    def _fabrica(anio, mes, dia, hora=0, minuto=0):
        return datetime(anio, mes, dia, hora, minuto, tzinfo=TZ_ART)

    return _fabrica


@pytest.fixture(scope="session")
def app_db():
    """App liviana del job conectada a la base LOCAL (o skip).

    El entorno se restaura después de crear la app para que las variables del
    .env no se filtren al resto de los tests.
    """
    from sqlalchemy import text
    from sqlalchemy.engine import make_url

    from backend.extensions import db
    from backend.jobs.app_job import create_job_app

    entorno = dict(os.environ)
    try:
        try:
            app = create_job_app()
        except RuntimeError as exc:  # DATABASE_URL ausente o inválida
            pytest.skip(f"sin base local: {exc.args[0].splitlines()[0]}")
    finally:
        os.environ.clear()
        os.environ.update(entorno)

    url = make_url(app.config["SQLALCHEMY_DATABASE_URI"])
    if url.host not in HOSTS_LOCALES:
        pytest.skip(f"los tests de DB solo corren contra localhost (resuelto: {url.host})")

    with app.app_context():
        try:
            db.session.execute(text("SELECT 1"))
        except Exception as exc:  # noqa: BLE001 - cualquier falla de conexión = skip
            pytest.skip(f"base local no disponible: {type(exc).__name__}")
        finally:
            db.session.remove()

    yield app

    with app.app_context():
        db.engine.dispose()


@pytest.fixture
def db_ctx(app_db):
    """Contexto de app con `db.session` limpio al terminar."""
    from backend.extensions import db

    with app_db.app_context():
        try:
            yield db
        finally:
            db.session.rollback()
            db.session.remove()


@pytest.fixture
def sesion_tx(db_ctx):
    """Sesión dentro de una transacción externa que SIEMPRE se descarta.

    Sirve para cargar filas de prueba y leerlas sin dejar nada en la base.
    """
    from sqlalchemy.orm import Session

    conn = db_ctx.engine.connect()
    trans = conn.begin()
    sesion = Session(bind=conn, join_transaction_mode="create_savepoint")
    try:
        yield sesion
    finally:
        sesion.close()
        trans.rollback()
        conn.close()
