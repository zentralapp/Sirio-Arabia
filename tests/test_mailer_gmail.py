"""Envío por Gmail API con HTTP falso: NUNCA sale a la red ni usa credenciales reales.

`SesionFalsa` reemplaza a `requests.Session`: responde según un guion por URL
y registra cada llamada. El sleep del backoff es inyectado (no se espera).
"""
import base64
import logging
from datetime import date
from email import policy
from email.parser import BytesParser

import pytest
import requests
from urllib3.exceptions import MaxRetryError, NewConnectionError, ProtocolError

from backend.services.mailer import get_mailer
from backend.services.mailer.base import (
    CUERPO_REPORTE,
    Adjunto,
    EnvioIncierto,
    MailerError,
    TokenRevocadoError,
    asunto_reporte,
    nombre_adjunto,
)
from backend.services.mailer.gmail_api import SEND_URL, TOKEN_URL, GmailApiMailer

CASILLA = "negocio@example.com"
CLIENT_ID = "id-falso-123.apps.googleusercontent.com"
CLIENT_SECRET = "SECRETO-FALSO-cliente-xyz"
REFRESH_TOKEN = "1//REFRESH-FALSO-abc"
ACCESS_TOKEN = "ya29.ACCESS-FALSO-def"
SECRETOS = (CLIENT_SECRET, REFRESH_TOKEN, ACCESS_TOKEN)

PDF = b"%PDF-1.4\n% reporte de prueba\n%%EOF\n"
HOY = date(2026, 10, 29)


class RespuestaFalsa:
    def __init__(self, status_code, payload=None):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        if self._payload is None:
            raise ValueError("sin JSON")
        return self._payload


def ok_token():
    return RespuestaFalsa(200, {"access_token": ACCESS_TOKEN, "expires_in": 3599, "token_type": "Bearer"})


def ok_send(mid="18f0000000000001"):
    return RespuestaFalsa(200, {"id": mid, "threadId": mid, "labelIds": ["SENT"]})


def error_token(error, status=400):
    return RespuestaFalsa(status, {"error": error, "error_description": "Bad Request"})


def error_gmail(status, reason="backendError"):
    return RespuestaFalsa(status, {"error": {"code": status, "status": "X", "errors": [{"reason": reason}]}})


def conexion_rechazada():
    causa = NewConnectionError(None, "Failed to establish a new connection: [Errno 61] Connection refused")
    return requests.exceptions.ConnectionError(MaxRetryError(None, "/x", causa))


def conexion_cortada_a_mitad():
    return requests.exceptions.ConnectionError(ProtocolError("Connection aborted.", ConnectionResetError(54)))


class SesionFalsa:
    def __init__(self, token=(), send=()):
        self.guion = {TOKEN_URL: list(token), SEND_URL: list(send)}
        self.llamadas = []

    def post(self, url, **kwargs):
        self.llamadas.append((url, kwargs))
        if not self.guion[url]:
            raise AssertionError(f"llamada no esperada a {url}")
        resultado = self.guion[url].pop(0)
        if isinstance(resultado, BaseException):
            raise resultado
        return resultado

    def a(self, url):
        return [kw for u, kw in self.llamadas if u == url]


class Reloj:
    def __init__(self):
        self.esperas = []

    def __call__(self, segundos):
        self.esperas.append(segundos)


def mailer(sesion, reloj=None, **opciones):
    return GmailApiMailer(
        client_id=CLIENT_ID,
        client_secret=CLIENT_SECRET,
        refresh_token=REFRESH_TOKEN,
        remitente_nombre="Arabia Representaciones",
        remitente=CASILLA,
        destinatario=CASILLA,
        session=sesion,
        dormir=reloj or Reloj(),
        **opciones,
    )


def enviar(m):
    return m.send(
        to=m.destinatario,
        subject=asunto_reporte(HOY),
        body=CUERPO_REPORTE,
        attachments=[Adjunto(nombre_adjunto(HOY), PDF)],
    )


# ---------------------------------------------------------------- éxito y MIME


def test_envio_ok_devuelve_id_y_usa_timeouts():
    sesion = SesionFalsa(token=[ok_token()], send=[ok_send("abc123")])

    assert enviar(mailer(sesion)) == "abc123"

    (refresh,) = sesion.a(TOKEN_URL)
    assert refresh["data"] == {
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "refresh_token": REFRESH_TOKEN,
        "grant_type": "refresh_token",
    }
    assert refresh["timeout"] == (5, 30)
    (envio,) = sesion.a(SEND_URL)
    assert envio["headers"]["Authorization"] == f"Bearer {ACCESS_TOKEN}"
    assert envio["timeout"] == (5, 30)


def test_urls_de_google():
    assert TOKEN_URL == "https://oauth2.googleapis.com/token"
    assert SEND_URL == "https://gmail.googleapis.com/gmail/v1/users/me/messages/send"


def test_raw_base64url_con_adjunto_y_display_name():
    sesion = SesionFalsa(token=[ok_token()], send=[ok_send()])
    enviar(mailer(sesion))

    raw = sesion.a(SEND_URL)[0]["json"]["raw"]
    assert set(raw) <= set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_=")
    msg = BytesParser(policy=policy.default).parsebytes(base64.urlsafe_b64decode(raw))
    assert msg["From"] == f"Arabia Representaciones <{CASILLA}>"
    assert msg["To"] == CASILLA
    assert msg["Cc"] is None and msg["Bcc"] is None
    assert msg["Subject"] == "Reporte diario – jue 29/10/2026"
    assert msg.get_body(("plain",)).get_content().strip() == CUERPO_REPORTE
    adjuntos = list(msg.iter_attachments())
    assert len(adjuntos) == 1
    assert adjuntos[0].get_content_type() == "application/pdf"
    assert adjuntos[0].get_filename() == "reporte-diario-2026-10-29.pdf"
    assert adjuntos[0].get_content() == PDF


def test_sin_pdf_no_contacta_a_google():
    sesion = SesionFalsa()
    m = mailer(sesion)

    with pytest.raises(MailerError, match="PDF"):
        m.send(to=CASILLA, subject="s", body="b", attachments=[Adjunto("r.pdf", b"no-pdf")])
    assert sesion.llamadas == []


def test_get_mailer_gmail_api_completo():
    m = get_mailer(
        {
            "REPORTE_MAILER": "gmail_api",
            "REPORTE_GMAIL_CLIENT_ID": CLIENT_ID,
            "REPORTE_GMAIL_CLIENT_SECRET": CLIENT_SECRET,
            "REPORTE_GMAIL_REFRESH_TOKEN": REFRESH_TOKEN,
            "REPORTE_MAIL_FROM": CASILLA,
            "REPORTE_MAIL_TO": CASILLA,
        }
    )

    assert isinstance(m, GmailApiMailer)
    assert m.destinatario == CASILLA
    assert all(s not in repr(m) for s in SECRETOS)


# ---------------------------------------------------------------- refresh del token


def test_invalid_grant_token_revocado_sin_reintento():
    sesion = SesionFalsa(token=[error_token("invalid_grant")])
    reloj = Reloj()

    with pytest.raises(TokenRevocadoError) as exc:
        enviar(mailer(sesion, reloj))

    texto = str(exc.value)
    assert "scripts/autorizar_gmail.py" in texto
    assert "REPORTE_GMAIL_REFRESH_TOKEN" in texto
    assert len(sesion.a(TOKEN_URL)) == 1
    assert sesion.a(SEND_URL) == []
    assert reloj.esperas == []


@pytest.mark.parametrize("error", ["invalid_client", "unauthorized_client"])
def test_invalid_client_sin_reintento(error):
    sesion = SesionFalsa(token=[error_token(error, status=401)])
    reloj = Reloj()

    with pytest.raises(MailerError) as exc:
        enviar(mailer(sesion, reloj))

    assert not isinstance(exc.value, TokenRevocadoError)
    assert "REPORTE_GMAIL_CLIENT_ID" in str(exc.value)
    assert len(sesion.a(TOKEN_URL)) == 1
    assert sesion.a(SEND_URL) == []
    assert reloj.esperas == []


def test_refresh_503_luego_ok():
    sesion = SesionFalsa(token=[RespuestaFalsa(503), ok_token()], send=[ok_send()])
    reloj = Reloj()

    enviar(mailer(sesion, reloj))

    assert len(sesion.a(TOKEN_URL)) == 2
    assert len(sesion.a(SEND_URL)) == 1
    assert reloj.esperas == [2]


@pytest.mark.parametrize(
    "falla",
    [
        lambda: RespuestaFalsa(429),
        lambda: requests.exceptions.ReadTimeout("lento"),
        lambda: requests.exceptions.ConnectTimeout("sin conexion"),
        conexion_rechazada,
    ],
    ids=["429", "read-timeout", "connect-timeout", "conexion-rechazada"],
)
def test_refresh_transitorio_se_reintenta(falla):
    sesion = SesionFalsa(token=[falla(), ok_token()], send=[ok_send()])

    enviar(mailer(sesion))

    assert len(sesion.a(TOKEN_URL)) == 2


def test_refresh_google_no_responde_corta_tras_3_intentos():
    timeout = requests.exceptions.ConnectTimeout
    sesion = SesionFalsa(token=[timeout("a"), timeout("b"), timeout("c")])
    reloj = Reloj()

    with pytest.raises(MailerError) as exc:
        enviar(mailer(sesion, reloj))

    assert not isinstance(exc.value, (TokenRevocadoError, EnvioIncierto))
    assert len(sesion.a(TOKEN_URL)) == 3
    assert sesion.a(SEND_URL) == []
    assert reloj.esperas == [2, 4]


def test_refresh_400_otro_error_sin_reintento():
    sesion = SesionFalsa(token=[error_token("invalid_request")])

    with pytest.raises(MailerError, match="invalid_request"):
        enviar(mailer(sesion))
    assert len(sesion.a(TOKEN_URL)) == 1


def test_refresh_200_sin_access_token_falla():
    sesion = SesionFalsa(token=[RespuestaFalsa(200, {"token_type": "Bearer"})])

    with pytest.raises(MailerError):
        enviar(mailer(sesion))
    assert sesion.a(SEND_URL) == []


# ---------------------------------------------------------------- envío


def test_send_503_luego_200_un_solo_envio():
    sesion = SesionFalsa(token=[ok_token()], send=[error_gmail(503), ok_send("id-final")])
    reloj = Reloj()

    assert enviar(mailer(sesion, reloj)) == "id-final"
    assert len(sesion.a(SEND_URL)) == 2
    assert reloj.esperas == [2]


def test_send_503_tres_veces_mailer_error():
    sesion = SesionFalsa(token=[ok_token()], send=[error_gmail(503)] * 3)
    reloj = Reloj()

    with pytest.raises(MailerError) as exc:
        enviar(mailer(sesion, reloj))

    assert not isinstance(exc.value, EnvioIncierto)
    assert "503" in str(exc.value)
    assert len(sesion.a(SEND_URL)) == 3
    assert reloj.esperas == [2, 4]


def test_send_429_se_reintenta():
    sesion = SesionFalsa(token=[ok_token()], send=[error_gmail(429, "rateLimitExceeded"), ok_send()])

    enviar(mailer(sesion))

    assert len(sesion.a(SEND_URL)) == 2


@pytest.mark.parametrize("status", [500, 502, 504])
def test_send_500_502_504_es_incierto_sin_reintento(status):
    """Un 500/502/504 al send puede venir DESPUÉS de que Gmail aceptó el mail
    (error interno, gateway que cortó la respuesta): no se reintenta."""
    sesion = SesionFalsa(token=[ok_token()], send=[error_gmail(status)])
    reloj = Reloj()

    with pytest.raises(EnvioIncierto) as exc:
        enviar(mailer(sesion, reloj))

    assert str(exc.value).startswith("estado incierto")
    assert f"HTTP {status}" in str(exc.value)
    assert len(sesion.a(SEND_URL)) == 1
    assert reloj.esperas == []


def test_send_503_y_despues_500_es_incierto():
    sesion = SesionFalsa(token=[ok_token()], send=[error_gmail(503), error_gmail(500)])
    reloj = Reloj()

    with pytest.raises(EnvioIncierto):
        enviar(mailer(sesion, reloj))

    assert len(sesion.a(SEND_URL)) == 2
    assert reloj.esperas == [2]


def test_send_500_sin_body_json_es_incierto():
    sesion = SesionFalsa(token=[ok_token()], send=[RespuestaFalsa(502)])

    with pytest.raises(EnvioIncierto):
        enviar(mailer(sesion))
    assert len(sesion.a(SEND_URL)) == 1


@pytest.mark.parametrize("status", [500, 502, 504])
def test_refresh_500_502_504_se_sigue_reintentando(status):
    """Solo cambia el send: el refresh del token es idempotente."""
    sesion = SesionFalsa(token=[error_token("backend_error", status), ok_token()], send=[ok_send()])

    enviar(mailer(sesion))

    assert len(sesion.a(TOKEN_URL)) == 2
    assert len(sesion.a(SEND_URL)) == 1


def test_send_read_timeout_envio_incierto_sin_reintento():
    sesion = SesionFalsa(token=[ok_token()], send=[requests.exceptions.ReadTimeout("lento")])
    reloj = Reloj()

    with pytest.raises(EnvioIncierto) as exc:
        enviar(mailer(sesion, reloj))

    assert "estado incierto" in str(exc.value)
    assert "Enviados" in str(exc.value)
    assert len(sesion.a(SEND_URL)) == 1
    assert reloj.esperas == []


def test_send_conexion_cortada_a_mitad_es_incierto():
    # La conexión se cayó DESPUÉS de mandar el pedido: pudo haber salido.
    sesion = SesionFalsa(token=[ok_token()], send=[conexion_cortada_a_mitad()])

    with pytest.raises(EnvioIncierto):
        enviar(mailer(sesion))
    assert len(sesion.a(SEND_URL)) == 1


@pytest.mark.parametrize(
    "falla",
    [conexion_rechazada, lambda: requests.exceptions.ConnectTimeout("sin conexion")],
    ids=["conexion-rechazada", "connect-timeout"],
)
def test_send_sin_conexion_se_reintenta(falla):
    sesion = SesionFalsa(token=[ok_token()], send=[falla(), ok_send()])

    enviar(mailer(sesion))

    assert len(sesion.a(SEND_URL)) == 2


def test_send_401_refresca_una_vez():
    otro = RespuestaFalsa(200, {"access_token": "ya29.OTRO-ACCESS-FALSO"})
    sesion = SesionFalsa(token=[ok_token(), otro], send=[error_gmail(401, "authError"), ok_send()])

    enviar(mailer(sesion))

    assert len(sesion.a(TOKEN_URL)) == 2
    envios = sesion.a(SEND_URL)
    assert len(envios) == 2
    assert envios[1]["headers"]["Authorization"] == "Bearer ya29.OTRO-ACCESS-FALSO"


def test_send_401_dos_veces_falla_sin_loop():
    sesion = SesionFalsa(token=[ok_token(), ok_token()], send=[error_gmail(401, "authError")] * 2)

    with pytest.raises(MailerError, match="401"):
        enviar(mailer(sesion))
    assert len(sesion.a(TOKEN_URL)) == 2
    assert len(sesion.a(SEND_URL)) == 2


@pytest.mark.parametrize("status", [400, 403])
def test_send_4xx_sin_reintento(status):
    sesion = SesionFalsa(token=[ok_token()], send=[error_gmail(status, "insufficientPermissions")])
    reloj = Reloj()

    with pytest.raises(MailerError) as exc:
        enviar(mailer(sesion, reloj))

    assert str(status) in str(exc.value)
    assert len(sesion.a(SEND_URL)) == 1
    assert reloj.esperas == []


def test_send_200_sin_id_falla():
    sesion = SesionFalsa(token=[ok_token()], send=[RespuestaFalsa(200, {})])

    with pytest.raises(EnvioIncierto):
        enviar(mailer(sesion))


# ---------------------------------------------------------------- secretos


ESCENARIOS_DE_FALLA = {
    "invalid_grant": dict(token=[error_token("invalid_grant")]),
    "invalid_client": dict(token=[error_token("invalid_client", 401)]),
    "refresh_caido": dict(token=[requests.exceptions.ConnectTimeout(f"x {REFRESH_TOKEN}")] * 3),
    "send_503": dict(token=[ok_token()], send=[error_gmail(503)] * 3),
    "send_500": dict(token=[ok_token()], send=[error_gmail(500)]),
    "send_403": dict(token=[ok_token()], send=[error_gmail(403)]),
    "send_read_timeout": dict(token=[ok_token()], send=[requests.exceptions.ReadTimeout(f"x {ACCESS_TOKEN}")]),
    "send_401_x2": dict(token=[ok_token(), ok_token()], send=[error_gmail(401)] * 2),
}


def _cadena(exc):
    """Mensaje, repr y toda la cadena de causas (lo que imprimiría un traceback)."""
    partes = []
    while exc is not None:
        partes += [str(exc), repr(exc)]
        exc = exc.__cause__ or (None if exc.__suppress_context__ else exc.__context__)
    return "\n".join(partes)


@pytest.mark.parametrize("escenario", sorted(ESCENARIOS_DE_FALLA))
def test_errores_no_contienen_secretos(escenario, caplog):
    caplog.set_level(logging.DEBUG)
    sesion = SesionFalsa(**ESCENARIOS_DE_FALLA[escenario])

    with pytest.raises(MailerError) as exc:
        enviar(mailer(sesion))

    texto = _cadena(exc.value) + caplog.text
    for secreto in SECRETOS:
        assert secreto not in texto


def test_exito_no_loguea_secretos_ni_raw(caplog):
    caplog.set_level(logging.DEBUG)
    sesion = SesionFalsa(token=[ok_token()], send=[ok_send("id-ok")])

    enviar(mailer(sesion))

    raw = sesion.a(SEND_URL)[0]["json"]["raw"]
    for secreto in (*SECRETOS, raw[:40]):
        assert secreto not in caplog.text
    assert "id-ok" in caplog.text


# ---------------------------------------------------------------- opciones de la web (L "Reintentos y timeouts reducidos")


def test_web_un_intento_send_503_falla_sin_dormir():
    sesion = SesionFalsa(token=[ok_token()], send=[error_gmail(503)])
    reloj = Reloj()

    with pytest.raises(MailerError) as exc:
        enviar(mailer(sesion, reloj, max_intentos=1))

    assert not isinstance(exc.value, (TokenRevocadoError, EnvioIncierto))
    assert "tras 1 intento " in str(exc.value)
    assert len(sesion.a(SEND_URL)) == 1
    assert reloj.esperas == []


def test_web_un_intento_refresh_503_falla_sin_reintento():
    sesion = SesionFalsa(token=[RespuestaFalsa(503)])
    reloj = Reloj()

    with pytest.raises(MailerError) as exc:
        enviar(mailer(sesion, reloj, max_intentos=1))

    assert not isinstance(exc.value, (TokenRevocadoError, EnvioIncierto))
    assert len(sesion.a(TOKEN_URL)) == 1
    assert sesion.a(SEND_URL) == []
    assert reloj.esperas == []


def test_web_un_intento_read_timeout_es_incierto_con_una_sola_llamada():
    sesion = SesionFalsa(token=[ok_token()], send=[requests.exceptions.ReadTimeout("lento")])
    reloj = Reloj()

    with pytest.raises(EnvioIncierto) as exc:
        enviar(mailer(sesion, reloj, max_intentos=1))

    assert "estado incierto" in str(exc.value)
    assert len(sesion.a(SEND_URL)) == 1
    assert reloj.esperas == []


def test_timeout_inyectado_llega_a_session_post():
    sesion = SesionFalsa(token=[ok_token()], send=[ok_send()])

    enviar(mailer(sesion, timeout=(5, 20)))

    (refresh,) = sesion.a(TOKEN_URL)
    (envio,) = sesion.a(SEND_URL)
    assert refresh["timeout"] == (5, 20)
    assert envio["timeout"] == (5, 20)


def test_esperas_inyectadas_se_usan_en_el_backoff():
    sesion = SesionFalsa(token=[ok_token()], send=[error_gmail(503)] * 3)
    reloj = Reloj()

    with pytest.raises(MailerError, match="tras 3 intentos"):
        enviar(mailer(sesion, reloj, esperas=(0.5,)))

    assert reloj.esperas == [0.5, 0.5]


@pytest.mark.parametrize("opciones", [{"max_intentos": 0}, {"esperas": ()}])
def test_opciones_invalidas_fallan_al_construir(opciones):
    with pytest.raises(ValueError):
        mailer(SesionFalsa(), **opciones)


def test_desde_config_pasa_las_opciones():
    from backend.services.mailer import OPCIONES_WEB, ConfigMailer
    from pathlib import Path

    config = ConfigMailer(
        modo="gmail_api",
        mail_from=CASILLA,
        mail_to=CASILLA,
        remitente_nombre="Arabia Representaciones",
        archivo_dir=Path("/tmp/no-se-usa"),
        client_id=CLIENT_ID,
        client_secret=CLIENT_SECRET,
        refresh_token=REFRESH_TOKEN,
    )
    sesion = SesionFalsa(token=[RespuestaFalsa(503)])
    reloj = Reloj()
    m = GmailApiMailer.desde_config(config, session=sesion, dormir=reloj, **OPCIONES_WEB)

    with pytest.raises(MailerError):
        enviar(m)

    (refresh,) = sesion.a(TOKEN_URL)
    assert refresh["timeout"] == (5, 20)
    assert reloj.esperas == []
