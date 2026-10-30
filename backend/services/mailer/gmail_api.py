"""Envío del reporte por Gmail API (HTTPS) con `requests`, sin librerías de Google.

Flujo:
1. Refresh: POST oauth2.googleapis.com/token (grant_type=refresh_token) ->
   access token. `invalid_grant` = token revocado o vencido -> TokenRevocadoError
   (re-autorizar). `invalid_client`/`unauthorized_client` -> MailerError. Ambos
   sin reintento. 5xx/429/timeout/sin conexión -> reintento (el refresh es
   idempotente).
2. Send: POST gmail/v1/users/me/messages/send {"raw": base64url(MIME)}.
   401 -> refresca UNA vez. 400/403 -> sin reintento. 429/503 (y otros 5xx)
   o "no se pudo conectar" -> reintento. 500/502/504, timeout de lectura o
   conexión cortada después de mandar el pedido -> EnvioIncierto, SIN
   reintento (pudo haber salido; at-most-once): un 500 puede ser un error
   interno después de aceptar el mail, y 502/504 son un gateway que perdió la
   respuesta del backend. 503 (no disponible) y 429 (rate limit) indican que
   el pedido no se procesó.

Como máximo 3 intentos por paso, esperando 2 s y 4 s entre ellos (el sleep es
inyectable para los tests). Timeouts (connect=5 s, read=30 s).

Nunca se loguea ni se pone en un error: refresh token, client secret, access
token, el `raw` ni el PDF. Los errores de requests se resumen por tipo y no se
encadenan (`from None`): su texto podría arrastrar datos del pedido.
"""
import base64
import logging
import re
import time
from typing import Callable, Sequence

import requests
from urllib3.exceptions import MaxRetryError, NewConnectionError

from backend.services.mailer.base import (
    Adjunto,
    EnvioIncierto,
    MailerError,
    TokenRevocadoError,
    armar_mensaje,
)

log = logging.getLogger(__name__)

TOKEN_URL = "https://oauth2.googleapis.com/token"
SEND_URL = "https://gmail.googleapis.com/gmail/v1/users/me/messages/send"

TIMEOUT = (5, 30)  # (connect, read) en segundos
MAX_INTENTOS = 3
ESPERAS = (2, 4, 8)  # backoff entre intentos (con 3 intentos se usan 2 y 4)

MENSAJE_TOKEN_REVOCADO = (
    "El token de Gmail fue revocado o venció (invalid_grant). Hay que volver a correr "
    "scripts/autorizar_gmail.py con la cuenta del negocio y actualizar "
    "REPORTE_GMAIL_REFRESH_TOKEN en Railway."
)
MENSAJE_INCIERTO = (
    "estado incierto: Gmail no confirmó el envío y pudo haber salido. "
    "Revisar Enviados de la cuenta del negocio antes de usar --force."
)

_CODIGO_SEGURO = re.compile(r"[^A-Za-z0-9_.\-]")

# Solo para el send (el refresh del token es idempotente y los reintenta).
STATUS_SEND_INCIERTO = frozenset({500, 502, 504})


def _codigo(valor) -> str:
    """Código de error de Google acotado y sin caracteres raros (nunca el body entero)."""
    return _CODIGO_SEGURO.sub("", str(valor or ""))[:60] or "sin detalle"


def _json(respuesta) -> dict:
    try:
        datos = respuesta.json()
    except ValueError:
        return {}
    return datos if isinstance(datos, dict) else {}


def _error_oauth(respuesta) -> str:
    return _codigo(_json(respuesta).get("error"))


def _error_gmail(respuesta) -> str:
    error = _json(respuesta).get("error")
    if not isinstance(error, dict):
        return "sin detalle"
    errores = error.get("errors") or [{}]
    razon = errores[0].get("reason") if isinstance(errores[0], dict) else None
    return _codigo(razon or error.get("status"))


def _transitorio(status: int) -> bool:
    return status == 429 or status >= 500


def _no_llego_a_conectar(exc: requests.RequestException) -> bool:
    """True solo si hay certeza de que el pedido NO salió (no se pudo conectar)."""
    if isinstance(exc, requests.exceptions.ConnectTimeout):
        return True
    if isinstance(exc, requests.exceptions.ConnectionError) and exc.args:
        causa = exc.args[0]
        if isinstance(causa, MaxRetryError):
            causa = causa.reason
        return isinstance(causa, NewConnectionError)
    return False


class GmailApiMailer:
    def __init__(
        self,
        *,
        client_id: str,
        client_secret: str,
        refresh_token: str,
        remitente_nombre: str,
        remitente: str,
        destinatario: str,
        session=None,
        dormir: Callable[[float], None] = time.sleep,
        timeout=TIMEOUT,
    ):
        self._client_id = client_id
        self._client_secret = client_secret
        self._refresh_token = refresh_token
        self.remitente_nombre = remitente_nombre
        self.remitente = remitente
        self.destinatario = destinatario
        self._session = session if session is not None else requests.Session()
        self._dormir = dormir
        self._timeout = timeout

    @classmethod
    def desde_config(cls, config) -> "GmailApiMailer":
        return cls(
            client_id=config.client_id,
            client_secret=config.client_secret,
            refresh_token=config.refresh_token,
            remitente_nombre=config.remitente_nombre,
            remitente=config.mail_from,
            destinatario=config.mail_to,
        )

    def __repr__(self) -> str:
        return f"GmailApiMailer(remitente={self.remitente!r}, destinatario={self.destinatario!r})"

    # ------------------------------------------------------------ API pública

    def send(self, *, to: str, subject: str, body: str, attachments: Sequence[Adjunto]) -> str:
        msg = armar_mensaje(
            remitente_nombre=self.remitente_nombre,
            remitente=self.remitente,
            to=to,
            subject=subject,
            body=body,
            attachments=attachments,
        )
        # CRLF: el formato que espera RFC 5322 (Gmail acepta ambos).
        crudo = msg.as_bytes(policy=msg.policy.clone(linesep="\r\n"))
        raw = base64.urlsafe_b64encode(crudo).decode("ascii")

        access_token = self._refrescar()
        return self._enviar(raw, access_token)

    # ------------------------------------------------------------ internos

    def _esperar(self, intento: int, motivo: str, paso: str) -> None:
        espera = ESPERAS[min(intento - 1, len(ESPERAS) - 1)]
        log.warning("Gmail %s: intento %d/%d falló (%s); reintento en %ss", paso, intento, MAX_INTENTOS, motivo, espera)
        self._dormir(espera)

    def _refrescar(self) -> str:
        datos = {
            "client_id": self._client_id,
            "client_secret": self._client_secret,
            "refresh_token": self._refresh_token,
            "grant_type": "refresh_token",
        }
        motivo = ""
        for intento in range(1, MAX_INTENTOS + 1):
            try:
                respuesta = self._session.post(TOKEN_URL, data=datos, timeout=self._timeout)
            except requests.RequestException as exc:
                motivo = type(exc).__name__
            else:
                if respuesta.status_code == 200:
                    token = _json(respuesta).get("access_token")
                    if not token:
                        raise MailerError("Google devolvió una respuesta sin access_token al renovar el token.")
                    log.info("Gmail: token de acceso renovado")
                    return token
                error = _error_oauth(respuesta)
                if error == "invalid_grant":
                    raise TokenRevocadoError(MENSAJE_TOKEN_REVOCADO)
                if error in ("invalid_client", "unauthorized_client"):
                    raise MailerError(
                        f"Google rechazó el cliente OAuth ({error}): revisar REPORTE_GMAIL_CLIENT_ID y "
                        "REPORTE_GMAIL_CLIENT_SECRET (cliente tipo Desktop app del proyecto de Google Cloud)."
                    )
                motivo = f"HTTP {respuesta.status_code} {error}"
                if not _transitorio(respuesta.status_code):
                    raise MailerError(f"No se pudo renovar el token de Gmail: {motivo}.")
            if intento < MAX_INTENTOS:
                self._esperar(intento, motivo, "token")
        raise MailerError(f"No se pudo renovar el token de Gmail tras {MAX_INTENTOS} intentos (último: {motivo}).")

    def _enviar(self, raw: str, access_token: str) -> str:
        refresco_por_401 = False
        intento = 1
        while True:
            try:
                respuesta = self._session.post(
                    SEND_URL,
                    json={"raw": raw},
                    headers={"Authorization": f"Bearer {access_token}"},
                    timeout=self._timeout,
                )
            except requests.RequestException as exc:
                if not _no_llego_a_conectar(exc):
                    # Timeout de lectura / conexión cortada: el mail pudo haber salido.
                    log.error("Gmail send: %s; no se reintenta (posible envío)", type(exc).__name__)
                    raise EnvioIncierto(f"{MENSAJE_INCIERTO} ({type(exc).__name__})") from None
                motivo = type(exc).__name__
            else:
                status = respuesta.status_code
                if status == 200:
                    mensaje_id = _json(respuesta).get("id")
                    if not mensaje_id:
                        raise EnvioIncierto(f"{MENSAJE_INCIERTO} (respuesta 200 sin id)")
                    log.info("Gmail: mensaje enviado id=%s", mensaje_id)
                    return str(mensaje_id)
                error = _error_gmail(respuesta)
                if status == 401 and not refresco_por_401:
                    log.warning("Gmail send: 401 (%s); renuevo el token una vez", error)
                    refresco_por_401 = True
                    access_token = self._refrescar()
                    continue
                motivo = f"HTTP {status} {error}"
                if status in STATUS_SEND_INCIERTO:
                    log.error("Gmail send: %s; no se reintenta (posible envío)", motivo)
                    raise EnvioIncierto(f"{MENSAJE_INCIERTO} ({motivo})")
                if not _transitorio(status):
                    raise MailerError(f"Gmail rechazó el envío: {motivo}.")
            if intento >= MAX_INTENTOS:
                raise MailerError(f"Gmail no aceptó el envío tras {MAX_INTENTOS} intentos (último: {motivo}).")
            self._esperar(intento, motivo, "send")
            intento += 1
