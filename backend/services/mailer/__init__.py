"""Mailer del reporte diario según `REPORTE_MAILER` (gmail_api | archivo).

Solo lee variables REPORTE_*. NUNCA cae en las GMAIL_* del mail viejo por SMTP
(credenciales del ex-dev). Si `REPORTE_MAILER` falta, el modo es `archivo`:
una corrida local jamás manda un mail por accidente.

La configuración se valida entera ANTES de que el job reclame el envío; el
error lista los nombres de las variables que faltan, nunca sus valores.
"""
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

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

REMITENTE_NOMBRE_DEFAULT = "Arabia Representaciones"
ARCHIVO_DIR_DEFAULT = "/tmp/reportes"
# Solo para el modo archivo cuando no se configuró casilla: no sale a ningún lado.
CASILLA_LOCAL_DEFAULT = "reporte-diario@localhost"


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


def leer_config(env: Mapping[str, str] | None = None) -> ConfigMailer:
    """Lee y valida REPORTE_*. Lanza MailerError (sin valores) si algo está mal."""
    env = os.environ if env is None else env

    modo = _valor(env, ENV_MAILER).lower() or MODO_ARCHIVO
    if modo not in MODOS:
        raise MailerError(f"{ENV_MAILER} tiene que ser uno de {', '.join(MODOS)}.")

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


def get_mailer(env: Mapping[str, str] | None = None):
    """Mailer listo para `send(...)`, con `.destinatario` = REPORTE_MAIL_TO."""
    config = leer_config(env)
    if config.modo == MODO_GMAIL_API:
        # Import diferido: el modo archivo no necesita requests.
        from backend.services.mailer.gmail_api import GmailApiMailer

        return GmailApiMailer.desde_config(config)

    from backend.services.mailer.archivo import ArchivoMailer

    return ArchivoMailer(
        directorio=config.archivo_dir,
        remitente_nombre=config.remitente_nombre,
        remitente=config.mail_from,
        destinatario=config.mail_to,
    )
