"""Reporte diario en la web: aviso para admins (spec H, ADR-8) y botón
"Generar reporte" (spec L).

Aviso (sin rutas): un context processor inyecta `reporte_alerta` en todas las
plantillas. Lo muestra `_reporte_alerta.html` (incluido desde layout.html).
- Apagado salvo REPORTE_BANNER=1 (kill switch del servicio web).
- Solo para usuarios autenticados con is_admin.
- Nunca lanza: si algo falla, la página se renderiza sin aviso.

Botón (rutas /reporte, /reporte/pdf y /reporte/enviar), design §6:
- Existe solo con REPORTE_BOTON=1: si no, 404 para todos, admins incluidos.
  El flag se evalúa ANTES que la autorización. Aun con el flag, si la app
  tiene la SECRET_KEY por defecto ("dev-secret-key") o ninguna, el botón
  queda APAGADO (404 y sin links) y se loguea un WARNING una vez: el token
  CSRF vive en la sesión firmada con esa clave.
- Solo admin global: autenticado, is_admin y fuera de modo soporte
  (`impersonate_user_id`). Si no, 403. Sin login: `login_manager.unauthorized()`.
- "Ver PDF" (GET) no construye mailer ni escribe nada.
- "Enviar prueba" (POST): token CSRF de sesión (comparación en tiempo
  constante) + Origin/Referer del mismo host. Valida la config del mailer
  ANTES de reclamar la fila 'prueba' (advisory lock + guardas 5 min / 60 s,
  envios.reclamar_prueba). Va SOLO a REPORTE_MAIL_TO, con asunto [PRUEBA].
  Termina en PRG (303).
- Auditoría: una línea por acción (incluidos los rechazos) en el logger
  `backend.reporte.auditoria`, a stdout. Nunca tokens, secretos ni el PDF.

No importa main.py ni app.py (tests/test_reporte_banner.py).
"""
import hmac
import logging
import os
import re
import secrets
import sys
from datetime import date, datetime, timedelta, timezone
from io import BytesIO
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from flask import (
    Blueprint,
    abort,
    current_app,
    flash,
    make_response,
    redirect,
    render_template,
    request,
    send_file,
    session,
    url_for,
)
from flask_login import current_user
from sqlalchemy import text
from sqlalchemy.orm import Session

from backend.config import SECRET_KEY_POR_DEFECTO
from backend.extensions import db
from backend.services.mailer import OPCIONES_WEB, SERVICIO_WEB, get_mailer
from backend.services.mailer.base import EnvioIncierto, MailerError, TokenRevocadoError, nombre_adjunto
from backend.services.reporte_diario import envios, generar
from backend.services.reporte_diario.fechas import TZ_NEGOCIO, hoy_negocio
from backend.services.reporte_estado import estado_reporte
from backend.utils.formato import fmt_fecha_es

bp = Blueprint("reporte_estado", __name__)

ENV_BOTON = "REPORTE_BOTON"
CLAVE_CSRF = "reporte_csrf"
DIAS_ATRAS = 365  # hoy ART − 1 año (decisión 8 de #3571)
DIAS_ADELANTE = 31
TIMEOUT_CONSULTA = "2s"
MAX_CAMPO_LOG = 64

_FORMATO_FECHA = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_NO_SEGURO_LOG = re.compile(r"[^\w.@:/+\-]")
_TZ = ZoneInfo(TZ_NEGOCIO)


# --------------------------------------------------------------------------
#  Auditoría (H2: la web no configura logging; gunicorn --capture-output)
# --------------------------------------------------------------------------

auditoria = logging.getLogger("backend.reporte.auditoria")
log = logging.getLogger(__name__)


def _configurar_auditoria(logger: logging.Logger) -> None:
    """Idempotente: un solo StreamHandler a stdout, INFO, sin propagar."""
    if logger.handlers:
        return
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False


_configurar_auditoria(auditoria)


def _campo_log(valor) -> str:
    """Un valor de log en una sola "palabra": sin espacios ni saltos (anti log injection)."""
    texto = "-" if valor is None or valor == "" else str(valor)
    return _NO_SEGURO_LOG.sub("_", texto)[:MAX_CAMPO_LOG]


def _auditar(accion: str, resultado: str, *, fecha=None, intento=None, message_id=None, detalle=None) -> None:
    try:
        usuario = getattr(current_user, "username", None) if _autenticado() else None
        uid = getattr(current_user, "id", None) if _autenticado() else None
        partes = [
            f"usuario={_campo_log(usuario)}",
            f"id={_campo_log(uid)}",
            f"fecha={_campo_log(fecha.isoformat() if isinstance(fecha, date) else fecha)}",
            f"accion={_campo_log(accion)}",
            f"resultado={_campo_log(resultado)}",
        ]
        if intento is not None:
            partes.append(f"intento={_campo_log(intento)}")
        if message_id is not None:
            # Sin recorte a 64: los ids de Gmail son cortos y `archivo:<ruta>` es local.
            partes.append(f"message_id={_NO_SEGURO_LOG.sub('_', str(message_id))[:200]}")
        if detalle is not None:
            partes.append(f"detalle={_campo_log(detalle)}")
        auditoria.info("reporte prueba: %s", " ".join(partes))
    except Exception:  # noqa: BLE001 - la auditoría nunca rompe la request
        pass


# --------------------------------------------------------------------------
#  Flags y autorización
# --------------------------------------------------------------------------

def banner_activo() -> bool:
    return os.getenv("REPORTE_BANNER", "").strip() == "1"


MSG_SECRET_KEY_DEFAULT = (
    "REPORTE_BOTON=1 pero la app no tiene una SECRET_KEY real (falta o es la default de "
    "backend/config.py): el botón \"Generar reporte\" queda APAGADO. Configurar SECRET_KEY."
)
_aviso_secret_key_emitido = False


def _secret_key_insegura() -> bool:
    """True si la app no tiene SECRET_KEY o tiene la default de desarrollo.

    Nunca loguea ni devuelve el valor. Sin app context -> insegura (fail closed).
    """
    try:
        clave = current_app.config.get("SECRET_KEY")
    except RuntimeError:
        return True
    return not clave or clave == SECRET_KEY_POR_DEFECTO


def boton_activo() -> bool:
    """REPORTE_BOTON=1 Y una SECRET_KEY real. Con la default, False + WARNING (una vez)."""
    global _aviso_secret_key_emitido
    if os.getenv(ENV_BOTON, "").strip() != "1":
        return False
    if _secret_key_insegura():
        if not _aviso_secret_key_emitido:
            _aviso_secret_key_emitido = True
            log.warning(MSG_SECRET_KEY_DEFAULT)
        return False
    return True


def _autenticado() -> bool:
    return bool(getattr(current_user, "is_authenticated", False))


def _admin_global() -> bool:
    """Espejo de `_has_global_access` (main.py): admin y NO en modo soporte."""
    try:
        if not _autenticado() or not getattr(current_user, "is_admin", False):
            return False
        return not bool(session.get("impersonate_user_id"))
    except Exception:  # noqa: BLE001
        return False


_ACCIONES = {"reporte_estado.generar": "pagina", "reporte_estado.pdf": "pdf", "reporte_estado.enviar": "enviar"}


@bp.before_request
def _guardia():
    """Orden: flag (404) -> login (unauthorized) -> admin global (403)."""
    if not boton_activo():
        abort(404)
    accion = _ACCIONES.get(request.endpoint or "", "otra")
    fecha = request.values.get("fecha")
    if not _autenticado():
        _auditar(accion, "sin_login", fecha=fecha)
        return current_app.login_manager.unauthorized()
    if not _admin_global():
        motivo = "soporte" if getattr(current_user, "is_admin", False) else "no_admin"
        _auditar(accion, f"prohibido_{motivo}", fecha=fecha)
        abort(403)
    return None


@bp.app_context_processor
def inyectar_reporte_alerta():
    try:
        if not banner_activo():
            return {}
        if not (getattr(current_user, "is_authenticated", False) and getattr(current_user, "is_admin", False)):
            return {}
        alerta = estado_reporte()
        return {"reporte_alerta": alerta} if alerta else {}
    except Exception:  # noqa: BLE001 - el aviso nunca rompe una página
        return {}


@bp.app_context_processor
def inyectar_reporte_boton():
    """`reporte_boton` = True solo con REPORTE_BOTON=1 y admin global. Nunca lanza."""
    try:
        return {"reporte_boton": bool(boton_activo() and _admin_global())}
    except Exception:  # noqa: BLE001
        return {}


# --------------------------------------------------------------------------
#  Piezas puras
# --------------------------------------------------------------------------

def _ahora() -> datetime:
    return datetime.now(timezone.utc)


def _hoy() -> date:
    return hoy_negocio(_ahora())


def rango_fechas(hoy: date) -> tuple[date, date]:
    return hoy - timedelta(days=DIAS_ATRAS), hoy + timedelta(days=DIAS_ADELANTE)


def _fecha_valida(texto, hoy: date) -> tuple[date | None, str]:
    """(fecha, "") si `texto` es YYYY-MM-DD dentro de [hoy − 365, hoy + 31]; si no (None, error).

    El fin de semana se permite: es una prueba.
    """
    texto = (texto or "").strip()
    if not _FORMATO_FECHA.match(texto):
        return None, "Fecha inválida: usar el formato AAAA-MM-DD."
    try:
        fecha = date.fromisoformat(texto)
    except ValueError:
        return None, "Fecha inválida: usar el formato AAAA-MM-DD."
    desde, hasta = rango_fechas(hoy)
    if not desde <= fecha <= hasta:
        return None, (
            f"Fecha fuera de rango: tiene que estar entre el {fmt_fecha_es(desde)} "
            f"y el {fmt_fecha_es(hasta)}."
        )
    return fecha, ""


def enmascarar(direccion: str) -> str:
    """"jefe@dominio.com" -> "j***@dominio.com"."""
    direccion = (direccion or "").strip()
    if "@" not in direccion:
        return "(sin configurar)"
    usuario, dominio = direccion.split("@", 1)
    return f"{usuario[:1]}***@{dominio}"


MSG_CONFIG = (
    "El servicio web no tiene configurado Gmail (REPORTE_MAILER y REPORTE_GMAIL_*): {texto} "
    "Las variables tienen que estar también en el servicio web (shared variables)."
)
MSG_TOKEN = "Falló el envío: hay que re-autorizar Gmail (scripts/autorizar_gmail.py)."
MSG_INCIERTO = "Gmail no confirmó el envío: puede haber salido. Revisá Enviados antes de volver a probar."
MSG_SIN_REGISTRAR = (
    "Se envió (id {message_id}) pero no se pudo registrar: NO reenviar. "
    "Revisá Enviados; la fila de prueba queda PENDING y bloquea nuevos envíos por 5 minutos."
)
MSG_EN_CURSO = "Ya hay un envío de prueba en curso o reciente: hay uno en curso (hasta 5 minutos). No se envió nada."
MSG_DEMASIADO_PRONTO = (
    "Ya hay un envío de prueba en curso o reciente: el último salió hace menos de 60 segundos. No se envió nada."
)


def mensaje_resultado(resultado, *, destinatario: str = "", etapa: str = "envio") -> tuple[str, str]:
    """(categoría de flash, texto) del resultado del envío de prueba (puro).

    `resultado` es el message_id (str) o la excepción. `etapa`:
    "config" (falló get_mailer, sin fila), "envio" o "registro" (el mail salió
    pero no se pudo marcar SENT). Los textos nunca llevan secretos: los
    MailerError ya vienen limpios y de las demás excepciones solo va el tipo.
    """
    if etapa == "registro":
        return "danger", MSG_SIN_REGISTRAR.format(message_id=resultado)
    if isinstance(resultado, BaseException):
        if etapa == "config" and isinstance(resultado, MailerError):
            return "danger", MSG_CONFIG.format(texto=str(resultado))
        if isinstance(resultado, TokenRevocadoError):
            return "danger", MSG_TOKEN
        if isinstance(resultado, EnvioIncierto):
            return "warning", MSG_INCIERTO
        if isinstance(resultado, MailerError):
            return "danger", f"Falló el envío: {resultado}"
        return "danger", f"Error inesperado ({type(resultado).__name__})."
    message_id = str(resultado)
    if message_id.startswith("archivo:"):
        return "success", f"Guardado en {message_id[len('archivo:'):]} (modo archivo: no sale ningún mail)."
    return "success", f"Enviado a {destinatario} (id {message_id})."


def _resultado_log(exc: BaseException) -> str:
    if isinstance(exc, TokenRevocadoError):
        return "token_revocado"
    if isinstance(exc, EnvioIncierto):
        return "incierto"
    return "error"


# --------------------------------------------------------------------------
#  CSRF (solo este POST; la deuda global va en un issue aparte, decisión 9)
# --------------------------------------------------------------------------

def _token_csrf() -> str:
    token = session.get(CLAVE_CSRF)
    if not token:
        token = secrets.token_urlsafe(32)
        session[CLAVE_CSRF] = token
    return token


def _token_valido() -> bool:
    esperado = session.get(CLAVE_CSRF)
    recibido = request.form.get("csrf_token")
    if not esperado or not recibido:
        return False
    return hmac.compare_digest(str(recibido).encode("utf-8"), str(esperado).encode("utf-8"))


def _mismo_origen() -> bool:
    """Origin (o, si falta, Referer) con el mismo netloc que el host del request.

    Solo netloc: no hay ProxyFix y `request.scheme` puede ser http (H3).
    Sin ninguno de los dos headers -> rechazo.
    """
    origen = request.headers.get("Origin") or request.headers.get("Referer")
    if not origen:
        return False
    netloc = urlsplit(origen).netloc.lower()
    return bool(netloc) and netloc == (request.host or "").lower()


# --------------------------------------------------------------------------
#  Base de datos: Session propia (solo_lectura exige no tener tx abierta)
# --------------------------------------------------------------------------

def _nueva_sesion():
    """Session con conexión propia: no hereda la transacción de `db.session`."""
    return Session(bind=db.engine)


def _ultimas_pruebas() -> list:
    """Últimas 5 filas 'prueba' (solo SELECT, READ ONLY). Nunca lanza."""
    try:
        with db.engine.connect() as conn:
            if conn.dialect.name == "postgresql":
                conn.execute(text("SET TRANSACTION READ ONLY"))
                conn.execute(text(f"SET LOCAL statement_timeout = '{TIMEOUT_CONSULTA}'"))
            return envios.ultimas_pruebas(conn)
    except Exception:  # noqa: BLE001
        return []


def hora_art(momento: datetime | None) -> str:
    if momento is None:
        return ""
    if momento.tzinfo is None:
        momento = momento.replace(tzinfo=timezone.utc)
    return momento.astimezone(_TZ).strftime("%d/%m %H:%M")


# --------------------------------------------------------------------------
#  Rutas
# --------------------------------------------------------------------------

@bp.get("/reporte", endpoint="generar")
def pagina():
    hoy = _hoy()
    desde, hasta = rango_fechas(hoy)
    fecha, error = (hoy, "")
    if request.args.get("fecha"):
        fecha, error = _fecha_valida(request.args.get("fecha"), hoy)
        if fecha is None:
            fecha = hoy
    return render_template(
        "reporte_generar.html",
        active="reporte",
        fecha=fecha,
        fecha_min=desde,
        fecha_max=hasta,
        error_fecha=error,
        destinatario=enmascarar(os.getenv("REPORTE_MAIL_TO", "")),
        csrf_token=_token_csrf(),
        pruebas=_ultimas_pruebas(),
        hora_art=hora_art,
    )


def _texto_plano(texto: str, status: int):
    resp = make_response(texto + "\n", status)
    resp.mimetype = "text/plain"
    resp.headers["Cache-Control"] = "no-store"
    return resp


@bp.get("/reporte/pdf", endpoint="pdf")
def ver_pdf():
    crudo = request.args.get("fecha")
    fecha, error = _fecha_valida(crudo, _hoy())
    if fecha is None:
        _auditar("pdf", "fecha_invalida", fecha=crudo)
        return _texto_plano(error, 400)
    try:
        with _nueva_sesion() as s:
            _, contenido = generar.generar_pdf(s, fecha)
    except Exception as exc:  # noqa: BLE001
        _auditar("pdf", "error", fecha=fecha, detalle=type(exc).__name__)
        return _texto_plano(f"No se pudo generar el PDF ({type(exc).__name__}).", 500)
    _auditar("pdf", "ok", fecha=fecha)
    resp = send_file(
        BytesIO(contenido),
        mimetype="application/pdf",
        download_name=nombre_adjunto(fecha),
        as_attachment=False,
    )
    resp.headers["Cache-Control"] = "no-store"
    return resp


def _flash(categoria_y_texto: tuple[str, str]) -> None:
    categoria, texto = categoria_y_texto
    flash(texto, categoria)


def _volver(fecha: date | None):
    destino = url_for(".generar", fecha=fecha.isoformat()) if fecha else url_for(".generar")
    return redirect(destino, 303)


@bp.post("/reporte/enviar", endpoint="enviar")
def enviar_prueba():
    crudo = request.form.get("fecha")
    # 1) CSRF y origen: 400 sin generar, enviar ni escribir.
    if not _mismo_origen():
        _auditar("enviar", "origen_invalido", fecha=crudo)
        return _texto_plano("Origen de la solicitud inválido.", 400)
    if not _token_valido():
        _auditar("enviar", "csrf_invalido", fecha=crudo)
        return _texto_plano("Token de seguridad inválido o vencido: recargá la página.", 400)

    # 2) Fecha.
    fecha, error = _fecha_valida(crudo, _hoy())
    if fecha is None:
        _auditar("enviar", "fecha_invalida", fecha=crudo)
        flash(error, "danger")
        return _volver(None)

    # 3) Config del mailer ANTES de reclamar: si falla, no hay fila (W1).
    try:
        mailer = get_mailer(opciones_gmail=OPCIONES_WEB, servicio=SERVICIO_WEB)
    except MailerError as exc:
        _auditar("enviar", "config", fecha=fecha)
        _flash(mensaje_resultado(exc, etapa="config"))
        return _volver(fecha)

    # El destinatario es SIEMPRE el de la config (REPORTE_MAIL_TO): un `to`
    # del form se ignora.
    destinatario = mailer.destinatario
    try:
        with _nueva_sesion() as s:
            reclamo = envios.reclamar_prueba(s, fecha, destinatario=destinatario)
            if reclamo.estado == envios.EN_CURSO:
                _auditar("enviar", "en_curso", fecha=fecha)
                flash(MSG_EN_CURSO, "warning")
                return _volver(fecha)
            if reclamo.estado == envios.DEMASIADO_PRONTO:
                _auditar("enviar", "demasiado_pronto", fecha=fecha)
                flash(MSG_DEMASIADO_PRONTO, "warning")
                return _volver(fecha)

            try:
                _, contenido = generar.generar_pdf(s, fecha)
                mail = generar.armar_mail(fecha, contenido, prueba=True)
                message_id = mailer.send(
                    to=destinatario,
                    subject=mail.subject,
                    body=mail.body,
                    attachments=list(mail.attachments),
                )
            except Exception as exc:  # noqa: BLE001 - toda falla tras el claim queda FAILED
                try:
                    envios.registrar_falla(s, reclamo.id, envios.texto_error(exc))
                except Exception as exc_registro:  # noqa: BLE001
                    _auditar(
                        "enviar", "falla_sin_registrar", fecha=fecha, intento=reclamo.attempts,
                        detalle=type(exc_registro).__name__,
                    )
                _auditar(
                    "enviar", _resultado_log(exc), fecha=fecha, intento=reclamo.attempts,
                    detalle=type(exc).__name__,
                )
                _flash(mensaje_resultado(exc))
                return _volver(fecha)

            try:
                envios.registrar_envio(s, reclamo.id, message_id)
            except Exception as exc_registro:  # noqa: BLE001
                _auditar(
                    "enviar", "enviado_sin_registrar", fecha=fecha, intento=reclamo.attempts,
                    message_id=message_id, detalle=type(exc_registro).__name__,
                )
                _flash(mensaje_resultado(message_id, etapa="registro"))
                return _volver(fecha)
    except Exception as exc:  # noqa: BLE001 - DB caída, tabla inexistente, etc.
        _auditar("enviar", "error", fecha=fecha, detalle=type(exc).__name__)
        _flash(mensaje_resultado(exc))
        return _volver(fecha)

    _auditar("enviar", "ok", fecha=fecha, intento=reclamo.attempts, message_id=message_id)
    _flash(mensaje_resultado(message_id, destinatario=destinatario))
    return _volver(fecha)
