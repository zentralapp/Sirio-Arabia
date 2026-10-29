"""Configuración del mailer (REPORTE_*), armado MIME común y modo archivo.

Sin red, sin DB, sin .env: el entorno se pasa como dict a `get_mailer(env)`.
"""
from datetime import date
from email import policy
from email.parser import BytesParser

import pytest

from backend.services.mailer import ENV_MAILER, get_mailer, leer_config
from backend.services.mailer.archivo import ArchivoMailer
from backend.services.mailer.base import (
    CUERPO_REPORTE,
    Adjunto,
    MailerError,
    armar_mensaje,
    asunto_reporte,
    nombre_adjunto,
)

PDF = b"%PDF-1.4\n% muestra\n%%EOF\n"
CASILLA = "negocio@example.com"

ENV_GMAIL_COMPLETO = {
    "REPORTE_MAILER": "gmail_api",
    "REPORTE_GMAIL_CLIENT_ID": "id-de-prueba.apps.googleusercontent.com",
    "REPORTE_GMAIL_CLIENT_SECRET": "secreto-de-prueba",
    "REPORTE_GMAIL_REFRESH_TOKEN": "refresh-de-prueba",
    "REPORTE_MAIL_FROM": CASILLA,
    "REPORTE_MAIL_TO": CASILLA,
}


def leer_eml(ruta):
    return BytesParser(policy=policy.default).parsebytes(ruta.read_bytes())


# ---------------------------------------------------------------- asunto / adjunto


def test_asunto_con_guion_medio_y_dia_abreviado():
    assert asunto_reporte(date(2026, 10, 29)) == "Reporte diario – jue 29/10/2026"
    assert asunto_reporte(date(2026, 10, 31)) == "Reporte diario – sáb 31/10/2026"


def test_asunto_igual_al_titulo_del_pdf():
    from backend.services.reporte_diario.modelo import Reporte
    from backend.services.reporte_diario.pdf import titulo_documento

    hoy = date(2026, 10, 29)
    assert asunto_reporte(hoy) == titulo_documento(Reporte(hoy=hoy, domingo=date(2026, 11, 1)))


def test_nombre_adjunto():
    assert nombre_adjunto(date(2026, 10, 29)) == "reporte-diario-2026-10-29.pdf"


def test_cuerpo():
    assert CUERPO_REPORTE == "Adjunto el reporte del día."


# ---------------------------------------------------------------- configuración


def test_mailer_default_archivo(tmp_path):
    mailer = get_mailer({"REPORTE_ARCHIVO_DIR": str(tmp_path)})

    assert isinstance(mailer, ArchivoMailer)
    assert mailer.directorio == tmp_path


def test_archivo_dir_default_tmp_reportes():
    config = leer_config({})

    assert config.modo == "archivo"
    assert str(config.archivo_dir) == "/tmp/reportes"
    assert config.remitente_nombre == "Arabia Representaciones"


def test_mailer_vacio_es_archivo(tmp_path):
    mailer = get_mailer({ENV_MAILER: "  ", "REPORTE_ARCHIVO_DIR": str(tmp_path)})

    assert isinstance(mailer, ArchivoMailer)


def test_modo_desconocido_falla():
    with pytest.raises(MailerError, match="REPORTE_MAILER"):
        get_mailer({ENV_MAILER: "smtp"})


def test_gmail_api_falta_refresh_token_falla_aunque_haya_gmail_password():
    env = dict(ENV_GMAIL_COMPLETO)
    del env["REPORTE_GMAIL_REFRESH_TOKEN"]
    env.update({"GMAIL_USER": CASILLA, "GMAIL_FROM": CASILLA, "GMAIL_PASSWORD": "clave-vieja"})

    with pytest.raises(MailerError) as exc:
        get_mailer(env)

    mensaje = str(exc.value)
    assert "REPORTE_GMAIL_REFRESH_TOKEN" in mensaje
    assert "clave-vieja" not in mensaje
    assert "secreto-de-prueba" not in mensaje


def test_gmail_api_lista_todas_las_faltantes():
    with pytest.raises(MailerError) as exc:
        get_mailer({ENV_MAILER: "gmail_api"})

    for nombre in (
        "REPORTE_GMAIL_CLIENT_ID",
        "REPORTE_GMAIL_CLIENT_SECRET",
        "REPORTE_GMAIL_REFRESH_TOKEN",
        "REPORTE_MAIL_FROM",
        "REPORTE_MAIL_TO",
    ):
        assert nombre in str(exc.value)


@pytest.mark.parametrize("valor", ["", "   "])
def test_mail_to_vacio_falla(valor):
    env = dict(ENV_GMAIL_COMPLETO, REPORTE_MAIL_TO=valor)

    with pytest.raises(MailerError, match="REPORTE_MAIL_TO"):
        get_mailer(env)


@pytest.mark.parametrize(
    "valor",
    [
        "a@example.com, b@example.com",
        "a@example.com; b@example.com",
        "a@example.com b@example.com",
        "sin-arroba",
        "Nombre <a@example.com>",
    ],
)
def test_mail_to_multiple_o_invalido_falla(valor):
    env = dict(ENV_GMAIL_COMPLETO, REPORTE_MAIL_TO=valor)

    with pytest.raises(MailerError, match="REPORTE_MAIL_TO"):
        get_mailer(env)


def test_mail_to_invalido_tambien_falla_en_modo_archivo(tmp_path):
    with pytest.raises(MailerError, match="REPORTE_MAIL_TO"):
        get_mailer({"REPORTE_MAIL_TO": "a@example.com,b@example.com", "REPORTE_ARCHIVO_DIR": str(tmp_path)})


def test_mail_from_invalido_falla():
    env = dict(ENV_GMAIL_COMPLETO, REPORTE_MAIL_FROM="no es un mail")

    with pytest.raises(MailerError, match="REPORTE_MAIL_FROM"):
        get_mailer(env)


def test_config_no_muestra_secretos_en_repr():
    config = leer_config(ENV_GMAIL_COMPLETO)

    texto = repr(config)
    assert "secreto-de-prueba" not in texto
    assert "refresh-de-prueba" not in texto


def test_config_no_lee_gmail_viejas():
    config = leer_config({"GMAIL_USER": CASILLA, "GMAIL_FROM": CASILLA, "GMAIL_PASSWORD": "x"})

    assert config.mail_from != CASILLA
    assert config.mail_to != CASILLA


# ---------------------------------------------------------------- armado MIME


def test_send_sin_adjunto_falla(tmp_path):
    mailer = get_mailer({"REPORTE_ARCHIVO_DIR": str(tmp_path)})

    with pytest.raises(MailerError, match="adjunto"):
        mailer.send(to=mailer.destinatario, subject="x", body="x", attachments=[])
    assert list(tmp_path.iterdir()) == []


def test_send_sin_pdf_falla(tmp_path):
    mailer = get_mailer({"REPORTE_ARCHIVO_DIR": str(tmp_path)})

    with pytest.raises(MailerError, match="PDF"):
        mailer.send(
            to=mailer.destinatario,
            subject="x",
            body="x",
            attachments=[Adjunto("reporte.pdf", b"<html>no soy un pdf</html>")],
        )
    assert list(tmp_path.iterdir()) == []


def test_mensaje_rechaza_adjunto_que_no_es_pdf_aunque_empiece_con_pdf():
    with pytest.raises(MailerError, match="PDF"):
        armar_mensaje(
            remitente_nombre="Arabia Representaciones",
            remitente=CASILLA,
            to=CASILLA,
            subject="x",
            body="x",
            attachments=[Adjunto("a.txt", PDF, maintype="text", subtype="plain")],
        )


def test_mensaje_rechaza_destinatario_multiple():
    with pytest.raises(MailerError, match="destinatario"):
        armar_mensaje(
            remitente_nombre="Arabia Representaciones",
            remitente=CASILLA,
            to="a@example.com, b@example.com",
            subject="x",
            body="x",
            attachments=[Adjunto("a.pdf", PDF)],
        )


def test_mensaje_completo():
    msg = armar_mensaje(
        remitente_nombre="Arabia Representaciones",
        remitente=CASILLA,
        to=CASILLA,
        subject=asunto_reporte(date(2026, 10, 29)),
        body=CUERPO_REPORTE,
        attachments=[Adjunto(nombre_adjunto(date(2026, 10, 29)), PDF)],
    )
    parseado = BytesParser(policy=policy.default).parsebytes(msg.as_bytes())

    assert parseado["From"] == f"Arabia Representaciones <{CASILLA}>"
    assert parseado["To"] == CASILLA
    assert parseado["Subject"] == "Reporte diario – jue 29/10/2026"
    assert parseado["Cc"] is None and parseado["Bcc"] is None
    assert parseado.get_body(("plain",)).get_content().strip() == CUERPO_REPORTE
    adjuntos = list(parseado.iter_attachments())
    assert len(adjuntos) == 1
    assert adjuntos[0].get_content_type() == "application/pdf"
    assert adjuntos[0].get_filename() == "reporte-diario-2026-10-29.pdf"
    assert adjuntos[0].get_content() == PDF


# ---------------------------------------------------------------- modo archivo


def test_archivo_escribe_eml_con_adjunto_y_from(tmp_path):
    destino = tmp_path / "reportes"  # no existe: se crea
    mailer = get_mailer(
        {
            "REPORTE_MAILER": "archivo",
            "REPORTE_ARCHIVO_DIR": str(destino),
            "REPORTE_MAIL_FROM": CASILLA,
            "REPORTE_MAIL_TO": CASILLA,
        }
    )
    hoy = date(2026, 10, 29)

    resultado = mailer.send(
        to=mailer.destinatario,
        subject=asunto_reporte(hoy),
        body=CUERPO_REPORTE,
        attachments=[Adjunto(nombre_adjunto(hoy), PDF)],
    )

    ruta = destino / "reporte-diario-2026-10-29.eml"
    assert resultado == f"archivo:{ruta}"
    msg = leer_eml(ruta)
    assert msg["From"] == f"Arabia Representaciones <{CASILLA}>"
    assert msg["To"] == CASILLA
    assert msg["Cc"] is None and msg["Bcc"] is None
    assert msg["Subject"] == "Reporte diario – jue 29/10/2026"
    adjuntos = list(msg.iter_attachments())
    assert [a.get_filename() for a in adjuntos] == ["reporte-diario-2026-10-29.pdf"]
    assert adjuntos[0].get_content_type() == "application/pdf"
    assert adjuntos[0].get_content() == PDF


def test_archivo_remitente_nombre_configurable(tmp_path):
    mailer = get_mailer({"REPORTE_ARCHIVO_DIR": str(tmp_path), "REPORTE_REMITENTE_NOMBRE": "Otra Firma"})

    mailer.send(to=mailer.destinatario, subject="s", body="b", attachments=[Adjunto("r.pdf", PDF)])

    assert leer_eml(tmp_path / "r.eml")["From"].startswith("Otra Firma <")


def test_archivo_reescribe_si_ya_existe(tmp_path):
    mailer = get_mailer({"REPORTE_ARCHIVO_DIR": str(tmp_path)})
    for asunto in ("primero", "segundo"):
        mailer.send(to=mailer.destinatario, subject=asunto, body="b", attachments=[Adjunto("r.pdf", PDF)])

    assert leer_eml(tmp_path / "r.eml")["Subject"] == "segundo"


def test_archivo_no_permite_salir_del_directorio(tmp_path):
    mailer = get_mailer({"REPORTE_ARCHIVO_DIR": str(tmp_path / "dir")})

    mailer.send(to=mailer.destinatario, subject="s", body="b", attachments=[Adjunto("../../afuera.pdf", PDF)])

    assert (tmp_path / "dir" / "afuera.eml").exists()
    assert not (tmp_path / "afuera.eml").exists()
