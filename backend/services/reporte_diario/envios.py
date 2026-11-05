"""Registro de envíos del reporte en `report_send_log` (claim idempotente).

Movido tal cual desde `backend/jobs/reporte_diario.py`, que lo re-exporta
(`rd.reclamar_envio`, `rd.registrar_envio`, `rd.registrar_falla`, las
constantes de estado y `Reclamo` siguen existiendo en el namespace del job).

Idempotencia: UNA fila por (report_key, fecha_negocio). El claim es
`INSERT ... ON CONFLICT DO NOTHING RETURNING id`; si ya existe se toma con
`SELECT ... FOR UPDATE`. FAILED se reintenta; SENT y PENDING no (salvo
--force). Un FAILED en "estado incierto" (el mail pudo haber salido) tampoco
se reintenta solo: hay que revisar Enviados y usar --force. Todo reclamo de
una fila existente limpia error, message_id y sent_at en el mismo commit: la
fila refleja solo el último intento.

NUNCA importa `backend.app` ni `backend.views` (tests/test_aislamiento.py).
"""
from dataclasses import dataclass
from datetime import date, datetime, timezone

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from backend.models import ReportSendLog

REPORT_KEY = "diario"

PENDING = "PENDING"
SENT = "SENT"
FAILED = "FAILED"

# Resultado del claim.
RECLAMADO = "RECLAMADO"  # este proceso manda
YA_ENVIADO = "YA_ENVIADO"  # SENT sin --force
EN_CURSO = "EN_CURSO"  # PENDING sin --force (otro proceso, o uno que murió)
INCIERTO = "INCIERTO"  # FAILED "estado incierto" sin --force

PREFIJO_INCIERTO = "estado incierto"
MAX_ERROR = 2000
MAX_MESSAGE_ID = 128  # largo de la columna


@dataclass(frozen=True)
class Reclamo:
    estado: str
    id: int | None
    attempts: int | None


# --------------------------------------------------------------------------
#  report_send_log
# --------------------------------------------------------------------------

def _ahora_utc() -> datetime:
    """UTC naive: convención de timestamps del repo."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _es_incierto(error: str | None) -> bool:
    return (error or "").startswith(PREFIJO_INCIERTO)


def reclamar_envio(session, fecha: date, *, force: bool, destinatario: str) -> Reclamo:
    """Reclama el envío de `fecha` y commitea. Concurrencia: UNIQUE + row lock.

    Dos procesos a la vez: el INSERT del segundo espera al commit del primero
    y no inserta; después lee la fila PENDING y queda EN_CURSO.
    """
    t = ReportSendLog.__table__
    ahora = _ahora_utc()
    try:
        nuevo_id = session.execute(
            pg_insert(t)
            .values(
                report_key=REPORT_KEY,
                fecha_negocio=fecha,
                status=PENDING,
                attempts=1,
                recipient=destinatario,
                created_at=ahora,
                updated_at=ahora,
            )
            .on_conflict_do_nothing(constraint="uq_report_send_log_key_fecha")
            .returning(t.c.id)
        ).scalar()
        if nuevo_id is not None:
            session.commit()
            return Reclamo(RECLAMADO, nuevo_id, 1)

        fila = session.execute(
            select(t.c.id, t.c.status, t.c.attempts, t.c.error)
            .where(t.c.report_key == REPORT_KEY, t.c.fecha_negocio == fecha)
            .with_for_update()
        ).one()
        if not force:
            salteo = None
            if fila.status == SENT:
                salteo = YA_ENVIADO
            elif fila.status == PENDING:
                salteo = EN_CURSO
            elif _es_incierto(fila.error):
                salteo = INCIERTO
            if salteo is not None:
                session.rollback()
                return Reclamo(salteo, fila.id, fila.attempts)

        # Reclamo nuevo (FAILED, o --force sobre SENT/PENDING): la fila refleja
        # SOLO este intento. Si falla, queda FAILED sin el message_id viejo.
        attempts = session.execute(
            update(t)
            .where(t.c.id == fila.id)
            .values(
                status=PENDING,
                attempts=t.c.attempts + 1,
                error=None,
                message_id=None,
                sent_at=None,
                recipient=destinatario,
                updated_at=ahora,
            )
            .returning(t.c.attempts)
        ).scalar_one()
        session.commit()
        return Reclamo(RECLAMADO, fila.id, attempts)
    except BaseException:
        session.rollback()
        raise


def _recortar_message_id(message_id: str) -> str:
    """Los ids de Gmail son cortos; `archivo:<ruta>` puede no entrar: se deja la cola."""
    if len(message_id) <= MAX_MESSAGE_ID:
        return message_id
    return "…" + message_id[-(MAX_MESSAGE_ID - 1):]


def registrar_envio(session, id_: int, message_id: str) -> None:
    t = ReportSendLog.__table__
    ahora = _ahora_utc()
    session.rollback()  # arrancar limpio (la lectura ya cerró la suya)
    session.execute(
        update(t)
        .where(t.c.id == id_)
        .values(
            status=SENT,
            message_id=_recortar_message_id(message_id),
            error=None,
            sent_at=ahora,
            updated_at=ahora,
        )
    )
    session.commit()


def registrar_falla(session, id_: int, error: str) -> None:
    t = ReportSendLog.__table__
    session.rollback()  # la falla pudo dejar la transacción abortada
    session.execute(
        update(t)
        .where(t.c.id == id_)
        .values(status=FAILED, error=error[:MAX_ERROR], updated_at=_ahora_utc())
    )
    session.commit()
