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
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import func, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from backend.models import ReportSendLog
from backend.services.mailer.base import EnvioIncierto, MailerError

REPORT_KEY = "diario"
# Envíos de prueba del botón de la web: misma tabla, otra clave (sin migración).
REPORT_KEY_PRUEBA = "prueba"

PENDING = "PENDING"
SENT = "SENT"
FAILED = "FAILED"

# Resultado del claim.
RECLAMADO = "RECLAMADO"  # este proceso manda
YA_ENVIADO = "YA_ENVIADO"  # SENT sin --force
EN_CURSO = "EN_CURSO"  # PENDING sin --force (otro proceso, o uno que murió)
INCIERTO = "INCIERTO"  # FAILED "estado incierto" sin --force
DEMASIADO_PRONTO = "DEMASIADO_PRONTO"  # prueba: el último SENT de 'prueba' fue hace < 60 s

# Botón de la web (reclamar_prueba): advisory lock propio y guardas GLOBALES
# a todas las fechas de 'prueba' (decisión 10 de #3571, design §6).
LOCK_PRUEBA = 7_351_029_118_004_211  # bigint fijo para pg_advisory_xact_lock
PRUEBA_PENDING_VIGENTE = timedelta(minutes=5)
PRUEBA_INTERVALO_MINIMO = timedelta(seconds=60)

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


def reclamar_envio(
    session, fecha: date, *, force: bool, destinatario: str, report_key: str = REPORT_KEY
) -> Reclamo:
    """Reclama el envío de `fecha` y commitea. Concurrencia: UNIQUE + row lock.

    `report_key` es 'diario' (el job) salvo en `reclamar_prueba` ('prueba').

    Dos procesos a la vez: el INSERT del segundo espera al commit del primero
    y no inserta; después lee la fila PENDING y queda EN_CURSO.
    """
    t = ReportSendLog.__table__
    ahora = _ahora_utc()
    try:
        nuevo_id = session.execute(
            pg_insert(t)
            .values(
                report_key=report_key,
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
            .where(t.c.report_key == report_key, t.c.fecha_negocio == fecha)
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


def texto_error(exc: BaseException) -> str:
    """Texto para report_send_log (misma regla que `_texto_error` del job).

    Un EnvioIncierto siempre empieza con PREFIJO_INCIERTO; los errores del
    mailer ya vienen sin secretos; cualquier otra excepción va como
    "Tipo: texto".
    """
    if isinstance(exc, EnvioIncierto):
        texto = str(exc)
        if not _es_incierto(texto):
            texto = f"{PREFIJO_INCIERTO}: {texto}"
    elif isinstance(exc, MailerError):
        texto = str(exc)
    else:
        texto = f"{type(exc).__name__}: {exc}"
    return texto[:MAX_ERROR]


# --------------------------------------------------------------------------
#  Envío de prueba (botón "Generar reporte" de la web)
# --------------------------------------------------------------------------

def reclamar_prueba(session, fecha: date, *, destinatario: str, ahora: datetime | None = None) -> Reclamo:
    """Reclama la fila report_key='prueba' de `fecha` y commitea (spec F y L).

    1. `pg_advisory_xact_lock(LOCK_PRUEBA)`: serializa threads y requests; se
       suelta con el commit/rollback de esta misma transacción.
    2. Si hay alguna fila 'prueba' PENDING actualizada hace < 5 min (de
       CUALQUIER fecha) -> EN_CURSO, sin tocar nada.
    3. Si el último SENT de 'prueba' (de cualquier fecha) fue hace < 60 s ->
       DEMASIADO_PRONTO, sin tocar nada.
    4. Si no, `reclamar_envio(force=True, report_key='prueba')` en la MISMA
       transacción: INSERT o re-reclamo (attempts + 1, limpia message_id,
       sent_at y error) y commit, que suelta el lock con la fila en PENDING.

    Jamás lee ni toca la fila 'diario': todas las sentencias filtran por
    report_key='prueba'.
    """
    t = ReportSendLog.__table__
    if ahora is None:
        ahora = _ahora_utc()
    elif ahora.tzinfo is not None:  # la tabla guarda UTC naive
        ahora = ahora.astimezone(timezone.utc).replace(tzinfo=None)
    try:
        session.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": LOCK_PRUEBA})

        en_curso = session.execute(
            select(t.c.id, t.c.attempts)
            .where(
                t.c.report_key == REPORT_KEY_PRUEBA,
                t.c.status == PENDING,
                t.c.updated_at > ahora - PRUEBA_PENDING_VIGENTE,
            )
            .order_by(t.c.updated_at.desc())
            .limit(1)
        ).first()
        if en_curso is not None:
            session.rollback()
            return Reclamo(EN_CURSO, en_curso.id, en_curso.attempts)

        ultimo_sent = session.execute(
            select(func.max(t.c.sent_at)).where(t.c.report_key == REPORT_KEY_PRUEBA, t.c.status == SENT)
        ).scalar()
        if ultimo_sent is not None and ultimo_sent > ahora - PRUEBA_INTERVALO_MINIMO:
            session.rollback()
            return Reclamo(DEMASIADO_PRONTO, None, None)
    except BaseException:
        session.rollback()
        raise

    return reclamar_envio(
        session, fecha, force=True, destinatario=destinatario, report_key=REPORT_KEY_PRUEBA
    )


@dataclass(frozen=True)
class FilaPrueba:
    fecha_negocio: date
    status: str
    attempts: int | None
    updated_at: datetime | None
    sent_at: datetime | None


def ultimas_pruebas(conn, limite: int = 5) -> list[FilaPrueba]:
    """Últimas filas 'prueba' (solo SELECT), la más reciente primero."""
    t = ReportSendLog.__table__
    filas = conn.execute(
        select(t.c.fecha_negocio, t.c.status, t.c.attempts, t.c.updated_at, t.c.sent_at)
        .where(t.c.report_key == REPORT_KEY_PRUEBA)
        .order_by(t.c.updated_at.desc().nulls_last(), t.c.id.desc())
        .limit(limite)
    ).all()
    return [FilaPrueba(*fila) for fila in filas]
