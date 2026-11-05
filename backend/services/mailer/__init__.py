"""Mailer del reporte diario según `REPORTE_MAILER` (gmail_api | archivo).

Solo lee variables REPORTE_*. NUNCA cae en las GMAIL_* del mail viejo por SMTP
(credenciales del ex-dev). Si `REPORTE_MAILER` falta, el modo es `archivo`:
una corrida local jamás manda un mail por accidente.

En Railway el modo `archivo` está PROHIBIDO (falte `REPORTE_MAILER` o diga
`archivo`): el .eml quedaría en el disco efímero del contenedor, la fila
quedaría SENT y nadie se enteraría de que el jefe no recibió nada. Se falla
con MailerError antes del claim: exit 1, sin fila, y el banner avisa.

La configuración se valida entera ANTES de que el job reclame el envío; el
error lista los nombres de las variables que faltan, nunca sus valores.

El cron llama `get_mailer()` con los defaults (servicio "cron", reintentos
completos). El botón de la web llama
`get_mailer(opciones_gmail=OPCIONES_WEB, servicio="web")`: un solo intento,
timeouts cortos, y el error de W1 nombra el servicio web.
"""
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from backend.services.mailer.base import MailerError, es_direccion_unica

ENV_MAILER = "REPORTE_MAILER"
ENV_CLIENT_ID = "REPORTE_GMAIL_CLIENT_ID"
ENV_CLIENT_SECRET = "REPORTE_GMAIL_CLIENT_SECRET"
ENV_REFRESH_TOKEN = "REPORTE_GMAIL_REFRESH_TOKEN"
ENV_MAIL_FROM = "REPORTE_MAIL_FROM"
ENV_MAIL_TO = "REPORTE_MAIL_TO"
ENV_REMITENTE_NOMBRE = "REPORTE_REMITENTE_NOMBRE"
ENV_ARCHIVO_DIR = "REPORTE_ARCHIVO_DIR"

MODO_GMAIL_API = "gmail_api"
MODO_ARCHIVO = "archivo"
MODOS = (MODO_GMAIL_API, MODO_ARCHIVO)

# Variables que Railway inyecta en todo deploy. Documentadas en
# https://docs.railway.com/reference/variables: RAILWAY_ENVIRONMENT_NAME,
# RAILWAY_ENVIRONMENT_ID, RAILWAY_PROJECT_ID, RAILWAY_SERVICE_ID.
# RAILWAY_ENVIRONMENT es la legacy (ya no figura en la doc): se mira igual.
ENVS_RAILWAY = (
    "RAILWAY_ENVIRONMENT_NAME",
    "RAILWAY_ENVIRONMENT_ID",
    "RAILWAY_ENVIRONMENT",
    "RAILWAY_PROJECT_ID",
    "RAILWAY_SERVICE_ID",
)

REMITENTE_NOMBRE_DEFAULT = "Arabia Representaciones"
ARCHIVO_DIR_DEFAULT = "/tmp/reportes"
# Solo para el modo archivo cuando no se configuró casilla: no sale a ningún lado.
CASILLA_LOCAL_DEFAULT = "reporte-diario@localhost"

SERVICIO_CRON = "cron"
SERVICIO_WEB = "web"

# Opciones del GmailApiMailer para la request síncrona de la web: sin
# reintentos y con read timeout de 20 s (peor caso ~50 s, design §5).
OPCIONES_WEB = {"max_intentos": 1, "timeout": (5, 20)}


@dataclass(frozen=True)
class ConfigMailer:
    modo: str
    mail_from: str
    mail_to: str
    remitente_nombre: str
    archivo_dir: Path
    # Secretos: fuera del repr para que un log/traceback no los imprima.
    client_id: str = field(default="", repr=False)
    client_secret: str = field(default="", repr=False)
    refresh_token: str = field(default="", repr=False)


def _valor(env: Mapping[str, str], nombre: str) -> str:
    return (env.get(nombre) or "").strip()


def en_railway(env: Mapping[str, str]) -> bool:
    return any(_valor(env, nombre) for nombre in ENVS_RAILWAY)


def leer_config(env: Mapping[str, str] | None = None, *, servicio: str = SERVICIO_CRON) -> ConfigMailer:
    """Lee y valida REPORTE_*. Lanza MailerError (sin valores) si algo está mal.

    `servicio` solo cambia el texto del error de Railway (W1): en qué servicio
    hay que configurar REPORTE_MAILER.
    """
    env = os.environ if env is None else env

    explicito = _valor(env, ENV_MAILER).lower()
    modo = explicito or MODO_ARCHIVO
    if modo not in MODOS:
        raise MailerError(f"{ENV_MAILER} tiene que ser uno de {', '.join(MODOS)}.")

    if modo == MODO_ARCHIVO and en_railway(env):
        if explicito:
            raise MailerError(
                f"{ENV_MAILER}=archivo no se permite en Railway (es solo para local): el reporte "
                f"no se envió. Configurar {ENV_MAILER}=gmail_api en el servicio {servicio}."
            )
        raise MailerError(
            f"{ENV_MAILER} no configurado en Railway: el reporte no se envió. Configurar "
            f"{ENV_MAILER}=gmail_api en el servicio {servicio}."
        )

    if modo == MODO_GMAIL_API:
        requeridas = (ENV_CLIENT_ID, ENV_CLIENT_SECRET, ENV_REFRESH_TOKEN, ENV_MAIL_FROM, ENV_MAIL_TO)
        faltan = [n for n in requeridas if not _valor(env, n)]
        if faltan:
            raise MailerError(
                "Configuración del reporte incompleta para gmail_api: faltan " + ", ".join(faltan) + "."
            )

    mail_from = _valor(env, ENV_MAIL_FROM) or CASILLA_LOCAL_DEFAULT
    mail_to = _valor(env, ENV_MAIL_TO) or CASILLA_LOCAL_DEFAULT
    if modo == MODO_GMAIL_API or _valor(env, ENV_MAIL_TO):
        if not es_direccion_unica(mail_to):
            raise MailerError(f"{ENV_MAIL_TO} tiene que ser exactamente UNA dirección de mail.")
    if not es_direccion_unica(mail_from):
        raise MailerError(f"{ENV_MAIL_FROM} tiene que ser exactamente UNA dirección de mail.")

    return ConfigMailer(
        modo=modo,
        mail_from=mail_from,
        mail_to=mail_to,
        remitente_nombre=_valor(env, ENV_REMITENTE_NOMBRE) or REMITENTE_NOMBRE_DEFAULT,
        archivo_dir=Path(_valor(env, ENV_ARCHIVO_DIR) or ARCHIVO_DIR_DEFAULT),
        client_id=_valor(env, ENV_CLIENT_ID),
        client_secret=_valor(env, ENV_CLIENT_SECRET),
        refresh_token=_valor(env, ENV_REFRESH_TOKEN),
    )


def get_mailer(
    env: Mapping[str, str] | None = None,
    *,
    opciones_gmail: Mapping[str, Any] | None = None,
    servicio: str = SERVICIO_CRON,
):
    """Mailer listo para `send(...)`, con `.destinatario` = REPORTE_MAIL_TO.

    `opciones_gmail` (p. ej. OPCIONES_WEB) se pasa solo al GmailApiMailer; en
    modo archivo se ignora.
    """
    config = leer_config(env, servicio=servicio)
    if config.modo == MODO_GMAIL_API:
        # Import diferido: el modo archivo no necesita requests.
        from backend.services.mailer.gmail_api import GmailApiMailer

        return GmailApiMailer.desde_config(config, **dict(opciones_gmail or {}))

    from backend.services.mailer.archivo import ArchivoMailer

    return ArchivoMailer(
        directorio=config.archivo_dir,
        remitente_nombre=config.remitente_nombre,
        remitente=config.mail_from,
        destinatario=config.mail_to,
    )
