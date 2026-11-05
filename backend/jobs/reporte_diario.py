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
from datetime import date
from pathlib import Path

from dotenv import load_dotenv

from backend.extensions import db
from backend.jobs.app_job import create_job_app
from backend.services.mailer import get_mailer
from backend.services.mailer.base import EnvioIncierto, MailerError
from backend.services.reporte_diario import generar
from backend.services.reporte_diario.envios import (  # noqa: F401 - re-export (rd.*)
    EN_CURSO,
    FAILED,
    INCIERTO,
    MAX_ERROR,
    MAX_MESSAGE_ID,
    PENDING,
    PREFIJO_INCIERTO,
    RECLAMADO,
    REPORT_KEY,
    SENT,
    YA_ENVIADO,
    Reclamo,
    _ahora_utc,
    _es_incierto,
    _recortar_message_id,
    reclamar_envio,
    registrar_envio,
    registrar_falla,
)
from backend.services.reporte_diario.fechas import es_habil, hoy_negocio

log = logging.getLogger("backend.jobs.reporte_diario")

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
    "vencen_mas_adelante",
    "parciales",
    "pagadas_sin_registrar",
    "sin_vencimiento",
    "regularidad",
)

_FORMATO_FECHA = re.compile(r"^\d{4}-\d{2}-\d{2}$")


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
    conteos = " ".join(f"{s}={len(getattr(reporte, s))}" for s in SECCIONES)
    log.info("conteos: modo=%s %s", reporte.modo, conteos)


def _generar_pdf(session, fecha: date) -> bytes:
    # Conteos ANTES del render (al_armar): mismo orden de logs que siempre.
    _, pdf = generar.generar_pdf(session, fecha, al_armar=_log_conteos)
    return pdf


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
        mail = generar.armar_mail(fecha, pdf)
        message_id = mailer.send(
            to=mailer.destinatario,
            subject=mail.subject,
            body=mail.body,
            attachments=list(mail.attachments),
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
    try:
        registrar_envio(session, reclamo.id, message_id)
    except Exception as exc_registro:  # noqa: BLE001
        # El mail YA salió. No se marca FAILED: un FAILED común se reintentaría
        # solo (duplicado). La fila queda PENDING: bloquea reintentos sin
        # --force y a los 30 min el banner muestra "Estado incierto".
        # Solo el tipo de la excepción: su texto puede traer la URL de la DB.
        log.error(
            "reporte del %s enviado (id=%s) pero no se pudo registrar SENT: NO reenviar; revisar "
            "Enviados de la cuenta del negocio (la fila queda PENDING). Error: %s",
            dia,
            message_id,
            type(exc_registro).__name__,
        )
        return 1
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
