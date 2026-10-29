"""Claim y registro de envíos en report_send_log contra la base LOCAL (marca `db`).

Casi todo corre dentro de una transacción externa que se descarta (fixture
`sesion_tx`): los `commit()` del job solo liberan savepoints. El test de
concurrencia necesita commits reales: usa una fecha de 1990 y borra sus filas
al terminar (antes y después).
"""
import threading
from datetime import date

import pytest
from sqlalchemy import delete, select, text
from sqlalchemy.orm import Session

import backend.jobs.reporte_diario as rd
from backend.models import ReportSendLog

pytestmark = pytest.mark.db

FECHA = date(1990, 1, 1)  # nunca es una fecha real del reporte
FECHA_CONCURRENCIA = date(1990, 1, 2)
DEST = "negocio@example.com"


@pytest.fixture
def sesion(sesion_tx):
    existe = sesion_tx.execute(text("select to_regclass('public.report_send_log')")).scalar()
    if not existe:
        pytest.skip("report_send_log no existe en la base local (falta la migración)")
    return sesion_tx


def _fila(sesion, fecha=FECHA):
    return sesion.execute(
        select(ReportSendLog.__table__).where(
            ReportSendLog.report_key == rd.REPORT_KEY, ReportSendLog.fecha_negocio == fecha
        )
    ).one()


def test_primer_claim_inserta_pending(sesion):
    r = rd.reclamar_envio(sesion, FECHA, force=False, destinatario=DEST)

    assert r.estado == rd.RECLAMADO
    assert r.attempts == 1
    fila = _fila(sesion)
    assert (fila.id, fila.status, fila.attempts, fila.recipient) == (r.id, "PENDING", 1, DEST)
    assert fila.created_at is not None and fila.updated_at is not None


def test_sent_sin_force_saltea_y_con_force_reclama(sesion):
    r = rd.reclamar_envio(sesion, FECHA, force=False, destinatario=DEST)
    rd.registrar_envio(sesion, r.id, "id-1")
    fila = _fila(sesion)
    assert fila.status == "SENT" and fila.message_id == "id-1" and fila.sent_at is not None

    assert rd.reclamar_envio(sesion, FECHA, force=False, destinatario=DEST).estado == rd.YA_ENVIADO

    r2 = rd.reclamar_envio(sesion, FECHA, force=True, destinatario=DEST)
    assert (r2.estado, r2.id, r2.attempts) == (rd.RECLAMADO, r.id, 2)
    fila = _fila(sesion)
    assert fila.status == "PENDING" and fila.attempts == 2 and fila.error is None


def test_pending_sin_force_queda_en_curso(sesion):
    rd.reclamar_envio(sesion, FECHA, force=False, destinatario=DEST)
    r = rd.reclamar_envio(sesion, FECHA, force=False, destinatario=DEST)
    assert r.estado == rd.EN_CURSO
    assert _fila(sesion).attempts == 1


def test_failed_se_reintenta_sin_force(sesion):
    r = rd.reclamar_envio(sesion, FECHA, force=False, destinatario=DEST)
    rd.registrar_falla(sesion, r.id, "MailerError: 503 persistente")
    fila = _fila(sesion)
    assert fila.status == "FAILED" and fila.error == "MailerError: 503 persistente"

    r2 = rd.reclamar_envio(sesion, FECHA, force=False, destinatario=DEST)
    assert (r2.estado, r2.attempts) == (rd.RECLAMADO, 2)
    fila = _fila(sesion)
    assert fila.status == "PENDING" and fila.error is None


def test_failed_incierto_exige_force(sesion):
    r = rd.reclamar_envio(sesion, FECHA, force=False, destinatario=DEST)
    rd.registrar_falla(sesion, r.id, "estado incierto: revisar Enviados antes de usar --force.")

    assert rd.reclamar_envio(sesion, FECHA, force=False, destinatario=DEST).estado == rd.INCIERTO
    assert _fila(sesion).status == "FAILED"

    r2 = rd.reclamar_envio(sesion, FECHA, force=True, destinatario=DEST)
    assert (r2.estado, r2.attempts) == (rd.RECLAMADO, 2)


def test_registrar_falla_trunca_a_2000(sesion):
    r = rd.reclamar_envio(sesion, FECHA, force=False, destinatario=DEST)
    rd.registrar_falla(sesion, r.id, "e" * 5000)
    assert len(_fila(sesion).error) == 2000


def test_registrar_envio_message_id_largo_deja_la_cola(sesion):
    """`archivo:<ruta>` puede pasar los 128 caracteres de la columna."""
    r = rd.reclamar_envio(sesion, FECHA, force=False, destinatario=DEST)
    largo = "archivo:/" + "x" * 200 + "/reporte-diario-1990-01-01.eml"
    rd.registrar_envio(sesion, r.id, largo)
    guardado = _fila(sesion).message_id
    assert len(guardado) == 128
    assert guardado.endswith("reporte-diario-1990-01-01.eml")


def test_claim_deja_la_sesion_sin_transaccion_para_la_lectura(sesion):
    """armar_reporte exige arrancar una transacción nueva (SET TRANSACTION READ ONLY)."""
    rd.reclamar_envio(sesion, FECHA, force=False, destinatario=DEST)
    # En modo savepoint la transacción externa sigue; lo que importa es que no
    # quede un savepoint abierto del claim.
    assert sesion.get_nested_transaction() is None


# --------------------------------------------------------------------------
#  Concurrencia real: dos sesiones, commits de verdad, limpieza garantizada.
# --------------------------------------------------------------------------

@pytest.fixture
def engine_limpio(db_ctx):
    engine = db_ctx.engine
    with engine.connect() as conn:
        if not conn.execute(text("select to_regclass('public.report_send_log')")).scalar():
            pytest.skip("report_send_log no existe en la base local (falta la migración)")

    def limpiar():
        with engine.begin() as conn:
            conn.execute(
                delete(ReportSendLog.__table__).where(
                    ReportSendLog.fecha_negocio == FECHA_CONCURRENCIA
                )
            )

    limpiar()
    try:
        yield engine
    finally:
        limpiar()


def test_corridas_concurrentes_solo_una_reclama(engine_limpio):
    barrera = threading.Barrier(2)
    resultados = []
    errores = []

    def correr():
        try:
            with Session(engine_limpio) as s:
                barrera.wait(timeout=10)
                resultados.append(rd.reclamar_envio(s, FECHA_CONCURRENCIA, force=False, destinatario=DEST).estado)
        except Exception as exc:  # noqa: BLE001
            errores.append(exc)

    hilos = [threading.Thread(target=correr) for _ in range(2)]
    for h in hilos:
        h.start()
    for h in hilos:
        h.join(timeout=30)

    assert errores == []
    assert sorted(resultados) == sorted([rd.RECLAMADO, rd.EN_CURSO])


def test_segundo_claim_espera_al_primero_y_pierde(engine_limpio):
    """El INSERT del segundo se bloquea en el UNIQUE hasta que el primero commitea."""
    with Session(engine_limpio) as a, Session(engine_limpio) as b:
        # A inserta su PENDING pero todavía no commitea (simula el claim a mitad).
        a.execute(
            ReportSendLog.__table__.insert().values(
                report_key=rd.REPORT_KEY, fecha_negocio=FECHA_CONCURRENCIA, status="PENDING", attempts=1
            )
        )
        resultado = []
        hilo = threading.Thread(
            target=lambda: resultado.append(
                rd.reclamar_envio(b, FECHA_CONCURRENCIA, force=False, destinatario=DEST).estado
            )
        )
        hilo.start()
        hilo.join(timeout=1)
        assert hilo.is_alive(), "el segundo claim tenía que quedar esperando el lock"
        a.commit()
        hilo.join(timeout=30)
        assert resultado == [rd.EN_CURSO]
