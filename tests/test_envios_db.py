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
    # La fila refleja solo el intento nuevo: nada del envío anterior.
    assert fila.message_id is None and fila.sent_at is None


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


def test_failed_incierto_no_se_reintenta_y_la_fila_no_cambia(sesion):
    """Un EnvioIncierto sin prefijo igual queda marcado y las corridas siguientes no lo tocan.

    El mail pudo haber salido: reintentarlo solo podría duplicarlo (ADR-7).
    """
    r = rd.reclamar_envio(sesion, FECHA, force=False, destinatario=DEST)
    error = rd._texto_error(rd.EnvioIncierto("Gmail respondió 200 sin id"))
    rd.registrar_falla(sesion, r.id, error)
    antes = _fila(sesion)

    for _ in range(2):
        r2 = rd.reclamar_envio(sesion, FECHA, force=False, destinatario=DEST)
        assert (r2.estado, r2.id, r2.attempts) == (rd.INCIERTO, r.id, 1)

    despues = _fila(sesion)
    assert despues.status == "FAILED"
    assert despues.error == error and despues.error.startswith(rd.PREFIJO_INCIERTO)
    assert (despues.attempts, despues.updated_at) == (antes.attempts, antes.updated_at)
    assert sesion.get_nested_transaction() is None  # el salteo no deja el lock tomado


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


def test_force_sobre_sent_limpia_message_id_en_el_commit_del_claim(engine_limpio):
    """Con commits reales: otra conexión ya ve la fila sin message_id/sent_at apenas
    se reclama, y si el reintento falla queda FAILED sin el message_id viejo."""

    def fila():
        with Session(engine_limpio) as otra:
            return _fila(otra, FECHA_CONCURRENCIA)

    with Session(engine_limpio) as s:
        r = rd.reclamar_envio(s, FECHA_CONCURRENCIA, force=False, destinatario=DEST)
        rd.registrar_envio(s, r.id, "id-viejo")
        assert (fila().status, fila().message_id) == ("SENT", "id-viejo")

        r2 = rd.reclamar_envio(s, FECHA_CONCURRENCIA, force=True, destinatario=DEST)
        assert (r2.estado, r2.attempts) == (rd.RECLAMADO, 2)
        reclamada = fila()
        assert (reclamada.status, reclamada.message_id, reclamada.sent_at) == ("PENDING", None, None)

        rd.registrar_falla(s, r.id, "MailerError: 503 persistente")
        fallida = fila()
        assert (fallida.status, fallida.message_id, fallida.sent_at) == ("FAILED", None, None)
        assert fallida.error == "MailerError: 503 persistente"


# --------------------------------------------------------------------------
#  Envío de prueba del botón (report_key='prueba', spec F y L)
# --------------------------------------------------------------------------
# reclamar_prueba vive en envios.py y llama a envios.reclamar_envio: estos
# tests usan el módulo envios directamente (parchar rd.* no lo afecta).

from datetime import timedelta  # noqa: E402

from backend.services.reporte_diario import envios  # noqa: E402

FECHA_PRUEBA_OTRA = date(1990, 1, 3)


def _fila_key(sesion, key, fecha=FECHA):
    return sesion.execute(
        select(ReportSendLog.__table__).where(
            ReportSendLog.report_key == key, ReportSendLog.fecha_negocio == fecha
        )
    ).one()


@pytest.fixture
def sesion_prueba(sesion):
    """Sin filas 'prueba' de otras fechas (las guardas son globales).

    El DELETE vive en la transacción externa del fixture: se descarta al final.
    El commit solo libera el savepoint, así un rollback del claim no lo deshace.
    """
    sesion.execute(delete(ReportSendLog.__table__).where(ReportSendLog.report_key == envios.REPORT_KEY_PRUEBA))
    sesion.commit()
    return sesion


def _ahora():
    return envios._ahora_utc()


def test_prueba_primera_reclama_pending(sesion_prueba):
    r = envios.reclamar_prueba(sesion_prueba, FECHA, destinatario=DEST)

    assert (r.estado, r.attempts) == (envios.RECLAMADO, 1)
    fila = _fila_key(sesion_prueba, "prueba")
    assert (fila.id, fila.status, fila.attempts, fila.recipient) == (r.id, "PENDING", 1, DEST)
    # No crea fila 'diario'.
    assert sesion_prueba.execute(
        select(ReportSendLog.id).where(ReportSendLog.report_key == "diario", ReportSendLog.fecha_negocio == FECHA)
    ).first() is None


def test_prueba_sent_viejo_se_re_reclama_limpio(sesion_prueba):
    r = envios.reclamar_prueba(sesion_prueba, FECHA, destinatario=DEST)
    envios.registrar_envio(sesion_prueba, r.id, "id-1")

    r2 = envios.reclamar_prueba(sesion_prueba, FECHA, destinatario=DEST, ahora=_ahora() + timedelta(seconds=61))

    assert (r2.estado, r2.id, r2.attempts) == (envios.RECLAMADO, r.id, 2)
    fila = _fila_key(sesion_prueba, "prueba")
    assert (fila.status, fila.message_id, fila.sent_at, fila.error) == ("PENDING", None, None, None)


def test_prueba_pending_reciente_queda_en_curso_sin_tocar(sesion_prueba):
    r = envios.reclamar_prueba(sesion_prueba, FECHA, destinatario=DEST)
    antes = _fila_key(sesion_prueba, "prueba")

    r2 = envios.reclamar_prueba(sesion_prueba, FECHA, destinatario=DEST, ahora=_ahora() + timedelta(minutes=4))

    assert (r2.estado, r2.id) == (envios.EN_CURSO, r.id)
    despues = _fila_key(sesion_prueba, "prueba")
    assert (despues.attempts, despues.updated_at, despues.status) == (antes.attempts, antes.updated_at, "PENDING")


def test_prueba_pending_reciente_de_otra_fecha_tambien_bloquea(sesion_prueba):
    envios.reclamar_prueba(sesion_prueba, FECHA_PRUEBA_OTRA, destinatario=DEST)
    r = envios.reclamar_prueba(sesion_prueba, FECHA, destinatario=DEST)
    assert r.estado == envios.EN_CURSO
    assert sesion_prueba.execute(
        select(ReportSendLog.id).where(
            ReportSendLog.report_key == "prueba", ReportSendLog.fecha_negocio == FECHA
        )
    ).first() is None


def test_prueba_pending_de_10_minutos_se_re_reclama(sesion_prueba):
    r = envios.reclamar_prueba(sesion_prueba, FECHA, destinatario=DEST)

    r2 = envios.reclamar_prueba(sesion_prueba, FECHA, destinatario=DEST, ahora=_ahora() + timedelta(minutes=10))

    assert (r2.estado, r2.id, r2.attempts) == (envios.RECLAMADO, r.id, 2)
    assert _fila_key(sesion_prueba, "prueba").status == "PENDING"


def test_prueba_sent_hace_30_s_en_otra_fecha_es_demasiado_pronto(sesion_prueba):
    r = envios.reclamar_prueba(sesion_prueba, FECHA_PRUEBA_OTRA, destinatario=DEST)
    envios.registrar_envio(sesion_prueba, r.id, "id-otra")

    r2 = envios.reclamar_prueba(sesion_prueba, FECHA, destinatario=DEST, ahora=_ahora() + timedelta(seconds=30))

    assert r2.estado == envios.DEMASIADO_PRONTO
    assert sesion_prueba.execute(
        select(ReportSendLog.id).where(
            ReportSendLog.report_key == "prueba", ReportSendLog.fecha_negocio == FECHA
        )
    ).first() is None


def test_prueba_failed_se_re_reclama(sesion_prueba):
    r = envios.reclamar_prueba(sesion_prueba, FECHA, destinatario=DEST)
    envios.registrar_falla(sesion_prueba, r.id, envios.texto_error(envios.EnvioIncierto("timeout")))

    r2 = envios.reclamar_prueba(sesion_prueba, FECHA, destinatario=DEST)
    assert (r2.estado, r2.attempts) == (envios.RECLAMADO, 2)


def test_prueba_no_toca_la_fila_diario_de_la_misma_fecha(sesion_prueba):
    rd_r = rd.reclamar_envio(sesion_prueba, FECHA, force=False, destinatario=DEST)
    rd.registrar_envio(sesion_prueba, rd_r.id, "id-diario")
    antes = _fila_key(sesion_prueba, "diario")

    r = envios.reclamar_prueba(sesion_prueba, FECHA, destinatario="otro@example.com")
    envios.registrar_falla(sesion_prueba, r.id, "MailerError: 503")
    r2 = envios.reclamar_prueba(sesion_prueba, FECHA, destinatario="otro@example.com")
    envios.registrar_envio(sesion_prueba, r2.id, "id-prueba")

    despues = _fila_key(sesion_prueba, "diario")
    assert despues == antes
    assert r.id != rd_r.id
    # Y el job sigue viendo SU fila: SENT sin --force.
    assert rd.reclamar_envio(sesion_prueba, FECHA, force=False, destinatario=DEST).estado == rd.YA_ENVIADO


def test_prueba_pending_no_bloquea_al_job(sesion_prueba):
    envios.reclamar_prueba(sesion_prueba, FECHA, destinatario=DEST)
    r = rd.reclamar_envio(sesion_prueba, FECHA, force=False, destinatario=DEST)
    assert (r.estado, r.attempts) == (rd.RECLAMADO, 1)


def test_diario_pending_no_bloquea_la_prueba(sesion_prueba):
    rd.reclamar_envio(sesion_prueba, FECHA, force=False, destinatario=DEST)
    assert envios.reclamar_prueba(sesion_prueba, FECHA, destinatario=DEST).estado == envios.RECLAMADO


def test_reclamar_envio_sin_report_key_usa_diario(sesion_prueba):
    r = envios.reclamar_envio(sesion_prueba, FECHA, force=False, destinatario=DEST)
    assert _fila_key(sesion_prueba, "diario").id == r.id


def test_prueba_deja_la_sesion_sin_transaccion_abierta(sesion_prueba):
    """El lock es de transacción: tras el claim (o el rechazo) no queda nada abierto."""
    envios.reclamar_prueba(sesion_prueba, FECHA, destinatario=DEST)
    assert sesion_prueba.get_nested_transaction() is None
    envios.reclamar_prueba(sesion_prueba, FECHA, destinatario=DEST)  # EN_CURSO
    assert sesion_prueba.get_nested_transaction() is None


def test_ultimas_pruebas_solo_lee_prueba(sesion_prueba):
    rd.reclamar_envio(sesion_prueba, FECHA, force=False, destinatario=DEST)
    r = envios.reclamar_prueba(sesion_prueba, FECHA_PRUEBA_OTRA, destinatario=DEST)
    envios.registrar_envio(sesion_prueba, r.id, "id-x")

    filas = envios.ultimas_pruebas(sesion_prueba.connection())

    assert [(f.fecha_negocio, f.status, f.attempts) for f in filas] == [(FECHA_PRUEBA_OTRA, "SENT", 1)]


# --- concurrencia real de la prueba (commits de verdad, limpieza explícita) --

FECHA_PRUEBA_CONCURRENCIA = date(1990, 1, 4)


@pytest.fixture
def engine_prueba(db_ctx):
    """Base LOCAL (el conftest ya garantiza localhost). Limpia las filas 'prueba'
    de la fecha del test antes y después. Si hay pruebas reales recientes en la
    base local (las guardas son globales) se saltea en vez de borrarlas."""
    engine = db_ctx.engine
    with engine.connect() as conn:
        if not conn.execute(text("select to_regclass('public.report_send_log')")).scalar():
            pytest.skip("report_send_log no existe en la base local (falta la migración)")

    def limpiar():
        with engine.begin() as conn:
            conn.execute(
                delete(ReportSendLog.__table__).where(
                    ReportSendLog.report_key == envios.REPORT_KEY_PRUEBA,
                    ReportSendLog.fecha_negocio == FECHA_PRUEBA_CONCURRENCIA,
                )
            )

    limpiar()
    with Session(engine) as s:
        r = envios.reclamar_prueba(s, FECHA_PRUEBA_CONCURRENCIA, destinatario=DEST)
        if r.estado != envios.RECLAMADO:
            pytest.skip("hay una prueba real reciente en la base local; reintentar en unos minutos")
    limpiar()
    try:
        yield engine
    finally:
        limpiar()


def test_pruebas_concurrentes_solo_una_reclama(engine_prueba):
    barrera = threading.Barrier(2)
    resultados = []
    errores = []

    def correr():
        try:
            with Session(engine_prueba) as s:
                barrera.wait(timeout=10)
                resultados.append(envios.reclamar_prueba(s, FECHA_PRUEBA_CONCURRENCIA, destinatario=DEST).estado)
        except Exception as exc:  # noqa: BLE001
            errores.append(exc)

    hilos = [threading.Thread(target=correr) for _ in range(2)]
    for h in hilos:
        h.start()
    for h in hilos:
        h.join(timeout=30)

    assert errores == []
    assert sorted(resultados) == sorted([envios.RECLAMADO, envios.EN_CURSO])
    with Session(engine_prueba) as s:
        fila = _fila_key(s, "prueba", FECHA_PRUEBA_CONCURRENCIA)
        assert (fila.status, fila.attempts) == ("PENDING", 1)


def test_segunda_prueba_espera_el_advisory_lock(engine_prueba):
    """Mientras A tiene el lock (sin commitear), B espera; cuando A reclama, B queda EN_CURSO."""
    with Session(engine_prueba) as a, Session(engine_prueba) as b:
        a.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": envios.LOCK_PRUEBA})
        resultado = []
        hilo = threading.Thread(
            target=lambda: resultado.append(
                envios.reclamar_prueba(b, FECHA_PRUEBA_CONCURRENCIA, destinatario=DEST).estado
            )
        )
        hilo.start()
        hilo.join(timeout=1)
        assert hilo.is_alive(), "la segunda prueba tenía que esperar el advisory lock"
        # A termina su reclamo en la MISMA transacción (el commit suelta el lock).
        assert envios.reclamar_envio(
            a, FECHA_PRUEBA_CONCURRENCIA, force=True, destinatario=DEST, report_key="prueba"
        ).estado == envios.RECLAMADO
        hilo.join(timeout=30)
        assert resultado == [envios.EN_CURSO]
