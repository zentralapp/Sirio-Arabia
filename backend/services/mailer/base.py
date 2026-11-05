"""Piezas comunes de los mailers del reporte diario (puro: sin red, sin DB).

- `Adjunto`, `Mailer` (Protocol) y la jerarquía de errores.
- `armar_mensaje(...)`: el MIME que mandan TODOS los modos (archivo y Gmail
  API), así lo que se ve en el `.eml` local es exactamente lo que sale.
- Asunto, nombre de adjunto y cuerpo del reporte.

Ningún mensaje de error incluye valores de credenciales: solo nombres de
variables y descripciones.
"""
import re
from dataclasses import dataclass
from datetime import date
from email.message import EmailMessage
from email.utils import formataddr
from typing import Protocol, Sequence

from backend.utils.formato import DIAS_ABREV, fmt_fecha_es

CUERPO_REPORTE = "Adjunto el reporte del día."
# Botón "Generar reporte" de la web (envío de prueba, nunca el automático).
CUERPO_PRUEBA = "Envío de prueba generado desde la web (no es el envío automático). Adjunto el reporte del día."
PREFIJO_PRUEBA = "[PRUEBA] "

# Una sola dirección "pelada" (sin nombre, sin listas): S7.
_DIRECCION = re.compile(r"^[^@\s<>,;\"']+@[^@\s<>,;\"']+$")


class MailerError(RuntimeError):
    """Falla de configuración o de envío. El texto nunca lleva secretos."""


class TokenRevocadoError(MailerError):
    """Google rechazó el refresh token (invalid_grant): hay que re-autorizar."""


class EnvioIncierto(MailerError):
    """El envío pudo haber salido (timeout de lectura): NO se reintenta."""


@dataclass(frozen=True)
class Adjunto:
    filename: str
    content: bytes
    maintype: str = "application"
    subtype: str = "pdf"


class Mailer(Protocol):
    destinatario: str

    def send(self, *, to: str, subject: str, body: str, attachments: Sequence[Adjunto]) -> str:
        """Envía y devuelve un identificador (id de Gmail o `archivo:<ruta>`)."""
        ...


def es_direccion_unica(valor: str) -> bool:
    return bool(_DIRECCION.match(valor or ""))


def asunto_reporte(fecha: date, *, prueba: bool = False) -> str:
    """"Reporte diario – jue 29/10/2026" (guion medio, día en minúscula).

    Con `prueba=True` (botón de la web) antepone "[PRUEBA] ".
    """
    asunto = f"Reporte diario – {DIAS_ABREV[fecha.weekday()]} {fmt_fecha_es(fecha)}"
    return PREFIJO_PRUEBA + asunto if prueba else asunto


def nombre_adjunto(fecha: date) -> str:
    return f"reporte-diario-{fecha.isoformat()}.pdf"


def _validar_adjuntos(attachments: Sequence[Adjunto]) -> None:
    if not attachments:
        raise MailerError("El mail del reporte necesita el adjunto PDF y no vino ninguno.")
    for adj in attachments:
        es_pdf = (adj.maintype, adj.subtype) == ("application", "pdf")
        if not es_pdf or not bytes(adj.content).startswith(b"%PDF"):
            raise MailerError(f"El adjunto {adj.filename!r} no es un PDF válido: no se envía.")


def armar_mensaje(
    *,
    remitente_nombre: str,
    remitente: str,
    to: str,
    subject: str,
    body: str,
    attachments: Sequence[Adjunto],
) -> EmailMessage:
    """MIME del reporte: From con nombre visible, UN destinatario, sin CC/BCC.

    Falla fuerte (MailerError) si no hay adjunto o si no es un PDF: nada de
    mandar un mail vacío "porque sí".
    """
    if not es_direccion_unica(to):
        raise MailerError("El mail del reporte va a exactamente un destinatario (dirección única).")
    if not es_direccion_unica(remitente):
        raise MailerError("El remitente del reporte tiene que ser una única dirección.")
    _validar_adjuntos(attachments)

    msg = EmailMessage()
    msg["From"] = formataddr((remitente_nombre, remitente))
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)
    for adj in attachments:
        msg.add_attachment(
            bytes(adj.content), maintype=adj.maintype, subtype=adj.subtype, filename=adj.filename
        )
    return msg
