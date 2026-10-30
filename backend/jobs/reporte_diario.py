"""Job del reporte diario: `python -m backend.jobs.reporte_diario`.

Genera el PDF del día (cumpleaños y cobranzas) y lo manda por mail a la casilla
del negocio. Lo corre el cron de Railway L-V 08:00 ART (`0 11 * * 1-5`).

Opciones:
  --fecha YYYY-MM-DD  fecha de negocio a usar (sin --dry-run exige --force).
  --dry-run           genera el PDF y NO envía: no construye el mailer ni toca
                      report_send_log. Exige --out. Ignora fin de semana.
  --out RUTA.pdf      guarda el PDF ahí (con --dry-run, o como copia al enviar).
  --force             manda aunque sea sáb/dom o ya se haya enviado esa fecha.

Exit codes: 0 = enviado o salteado a propósito (fin de semana, ya SENT,
PENDING en curso, último intento incierto); 1 = falla (config, DB, tabla
inexistente, datos, PDF, envío); 2 = uso incorrecto de las opciones.

Orden (design §6): .env local (sin pisar env vars reales) -> opciones ->
fecha -> [--dry-run: leer, render, --out] -> guard de fin de semana ->
mailer (valida config) -> app liviana -> claim en report_send_log (commit
propio) -> lectura READ ONLY -> render -> envío -> SENT | FAILED.

Idempotencia: UNA fila por (report_key, fecha_negocio). El claim es
`INSERT ... ON CONFLICT DO NOTHING RETURNING id`; si ya existe se toma con
`SELECT ... FOR UPDATE`. FAILED se reintenta; SENT y PENDING no (salvo
--force). Un FAILED en "estado incierto" (el mail pudo haber salido) tampoco
se reintenta solo: hay que revisar Enviados y usar --force. Todo reclamo de
una fila existente limpia error, message_id y sent_at en el mismo commit: la
fila refleja solo el último intento.

NUNCA importa `backend.app` ni `backend.views` (tests/test_aislamiento.py).
Nunca loguea tokens, client secret ni el PDF.
"""
import argparse
import logging
import re
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from backend.extensions import db
from backend.jobs.app_job import create_job_app
from backend.models import ReportSendLog
from backend.services.mailer import get_mailer
from backend.services.mailer.base import (
    CUERPO_REPORTE,
    Adjunto,
    EnvioIncierto,
    MailerError,
    asunto_reporte,
    nombre_adjunto,
)
from backend.services.reporte_diario.datos import armar_reporte
from backend.services.reporte_diario.fechas import es_habil, hoy_negocio
from backend.services.reporte_diario.pdf import render_pdf

log = logging.getLogger("backend.jobs.reporte_diario")

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

MENSAJE_SIN_TABLA = (
    "falta la migración report_send_log: deployar la web primero "
    "(aplica la migración c3e5a9d1f7b2) y después correr el job."
)

SECCIONES = (
    "cumples_hoy",
    "cumples_semana",
    "vencen_hoy",
    "vencen_semana",
    "atrasadas",
    "parciales",
    "pagadas_sin_registrar",
    "sin_vencimiento",
)

_FORMATO_FECHA = re.compile(r"^\d{4}-\d{2}-\d{2}$")


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


# --------------------------------------------------------------------------
#  Base de datos
# --------------------------------------------------------------------------

@contextmanager
def abrir_sesion():
    """App liviana + Session real. Loguea host/base (nunca usuario ni contraseña)."""
    app = create_job_app()
    with app.app_context():
        try:
            url = db.engine.url
            log.info("base de datos: %s/%s", url.host, url.database)
            yield db.session()  # Session real, no el scoped_session
        finally:
            db.session.remove()
            db.engine.dispose()


def _es_tabla_inexistente(exc: BaseException) -> bool:
    return getattr(getattr(exc, "orig", None), "sqlstate", None) == "42P01"


# --------------------------------------------------------------------------
#  Opciones
# --------------------------------------------------------------------------

def _fecha_arg(valor: str) -> date:
    if not _FORMATO_FECHA.match(valor or ""):
        raise argparse.ArgumentTypeError(f"fecha inválida {valor!r}: usar YYYY-MM-DD")
    try:
        return date.fromisoformat(valor)
    except ValueError:
        raise argparse.ArgumentTypeError(f"fecha inválida {valor!r}: usar YYYY-MM-DD") from None


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m backend.jobs.reporte_diario",
        description="Genera el reporte diario en PDF y lo manda por mail.",
        allow_abbrev=False,
    )
    p.add_argument("--fecha", type=_fecha_arg, help="fecha de negocio YYYY-MM-DD (sin --dry-run exige --force)")
    p.add_argument("--dry-run", action="store_true", help="genera el PDF sin enviar ni registrar (exige --out)")
    p.add_argument("--out", help="ruta del PDF a escribir (vista previa o copia del enviado)")
    p.add_argument("--force", action="store_true", help="envía aunque sea fin de semana o ya se haya enviado")
    return p


def _parsear(argv) -> argparse.Namespace:
    p = _parser()
    args = p.parse_args(argv)
    if args.dry_run and not args.out:
        p.error("--dry-run exige --out RUTA.pdf")
    if args.fecha is not None and not (args.dry_run or args.force):
        p.error("--fecha sin --dry-run exige --force (evita mandar por error el reporte de otra fecha)")
    return args


# --------------------------------------------------------------------------
#  Orquestación
# --------------------------------------------------------------------------

def _texto_error(exc: BaseException) -> str:
    """Texto para log y report_send_log. Los errores del mailer ya vienen sin secretos."""
    if isinstance(exc, EnvioIncierto):
        texto = str(exc)
        if not _es_incierto(texto):
            texto = f"{PREFIJO_INCIERTO}: {texto}"
    elif isinstance(exc, MailerError):
        texto = str(exc)
    else:
        texto = f"{type(exc).__name__}: {exc}"
    return texto[:MAX_ERROR]


def _log_conteos(reporte) -> None:
    log.info("conteos: %s", " ".join(f"{s}={len(getattr(reporte, s))}" for s in SECCIONES))


def _generar_pdf(session, fecha: date) -> bytes:
    reporte = armar_reporte(fecha, session=session)
    _log_conteos(reporte)
    return render_pdf(reporte, datetime.now(timezone.utc))


def _vista_previa(fecha: date, out: Path) -> int:
    try:
        with abrir_sesion() as session:
            pdf = _generar_pdf(session, fecha)
        out.write_bytes(pdf)
    except Exception as exc:  # noqa: BLE001 - cualquier falla = exit 1
        log.error("falló la vista previa del %s: %s", fecha.isoformat(), _texto_error(exc))
        return 1
    log.info("vista previa (sin envío) guardada en %s (%d bytes)", out, len(pdf))
    return 0


def _guardar_copia(pdf: bytes, out: Path) -> None:
    try:
        out.write_bytes(pdf)
        log.info("copia del PDF guardada en %s", out)
    except OSError as exc:
        log.warning("no se pudo guardar la copia en %s (%s); se envía igual", out, type(exc).__name__)


def _enviar(session, fecha: date, mailer, *, force: bool, out: Path | None) -> int:
    reclamo = reclamar_envio(session, fecha, force=force, destinatario=mailer.destinatario)
    dia = fecha.isoformat()
    if reclamo.estado == YA_ENVIADO:
        log.info("el reporte del %s ya se envió (SENT): no se reenvía. Usar --force para reenviar.", dia)
        return 0
    if reclamo.estado == EN_CURSO:
        log.warning(
            "el reporte del %s está PENDING (otra corrida en curso o una que se cortó): no se envía "
            "para no duplicar. Si no salió (revisar Enviados), usar --force.",
            dia,
        )
        return 0
    if reclamo.estado == INCIERTO:
        log.warning(
            "el último intento del %s quedó en estado incierto (el mail pudo haber salido): no se "
            "reintenta solo. Revisar Enviados de la cuenta del negocio y, si no salió, usar --force.",
            dia,
        )
        return 0

    log.info("envío reclamado: fecha %s, intento %d", dia, reclamo.attempts)
    try:
        pdf = _generar_pdf(session, fecha)
        if out is not None:
            _guardar_copia(pdf, out)
        message_id = mailer.send(
            to=mailer.destinatario,
            subject=asunto_reporte(fecha),
            body=CUERPO_REPORTE,
            attachments=[Adjunto(nombre_adjunto(fecha), pdf)],
        )
    except Exception as exc:  # noqa: BLE001 - toda falla tras el claim queda FAILED
        error = _texto_error(exc)
        log.error("falló el reporte del %s: %s", dia, error)
        try:
            registrar_falla(session, reclamo.id, error)
        except Exception as exc_registro:  # noqa: BLE001
            log.error(
                "no se pudo registrar FAILED (la fila queda PENDING): %s", type(exc_registro).__name__
            )
        return 1

    log.info("reporte del %s enviado: %s", dia, message_id)
    registrar_envio(session, reclamo.id, message_id)
    return 0


def _configurar_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
    )
    # fpdf2 subsetea la fuente con fontTools, que loguea cada glifo en INFO.
    logging.getLogger("fontTools").setLevel(logging.WARNING)


def main(argv=None) -> int:
    # Antes de get_mailer(): REPORTE_* de un .env local valen. override=False:
    # en Railway (y con export) ganan las env vars reales.
    load_dotenv(override=False)
    _configurar_logging()
    try:
        args = _parsear(argv)
    except SystemExit as exc:  # argparse: --help (0) o uso incorrecto (2)
        return exc.code if isinstance(exc.code, int) else 2

    fecha = args.fecha or hoy_negocio()
    out = Path(args.out) if args.out else None
    log.info(
        "reporte diario: inicio, fecha de negocio %s%s%s",
        fecha.isoformat(),
        " --dry-run" if args.dry_run else "",
        " --force" if args.force else "",
    )

    if args.dry_run:
        return _vista_previa(fecha, out)

    if not es_habil(fecha) and not args.force:
        log.info("%s no es día hábil (sáb/dom): no se envía. Con --force se manda igual.", fecha.isoformat())
        return 0

    try:
        mailer = get_mailer()
    except MailerError as exc:
        log.error("configuración de envío inválida: %s", exc)
        return 1

    try:
        with abrir_sesion() as session:
            return _enviar(session, fecha, mailer, force=args.force, out=out)
    except Exception as exc:  # noqa: BLE001 - config de DB, conexión, tabla, registro
        if _es_tabla_inexistente(exc):
            log.error(MENSAJE_SIN_TABLA)
        else:
            log.error("falló el reporte del %s: %s", fecha.isoformat(), _texto_error(exc))
        return 1


if __name__ == "__main__":
    sys.exit(main())
