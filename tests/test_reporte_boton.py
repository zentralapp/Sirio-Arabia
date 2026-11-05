"""Botón "Generar reporte" en la web (spec L, design §6).

Igual que test_reporte_banner: NO se levanta `backend.app` (create_app hace
create_all/ALTERs/seed). Se monta una app Flask mínima con la carpeta real de
plantillas, el blueprint `reporte_estado`, un LoginManager y una SECRET_KEY.
`current_user` se parchea en la vista. La base y el mailer se reemplazan por
falsos: nunca se manda un mail real ni se usan credenciales GMAIL_*.
"""
import logging
from datetime import date, datetime, timezone
from pathlib import Path

import pytest
from flask import Flask
from flask_login import LoginManager
from jinja2 import ChoiceLoader, DictLoader, FileSystemLoader

from backend.services.mailer.base import EnvioIncierto, MailerError, TokenRevocadoError
from backend.services.reporte_diario import envios, generar
from backend.services.reporte_diario.envios import Reclamo
from backend.views import reporte_estado as vista

REPO = Path(__file__).resolve().parent.parent
TEMPLATES = REPO / "backend" / "templates"

HOY = date(2026, 11, 5)  # jueves
# 23:30 ART del 05/11 = 02:30 UTC del 06/11 (spec L "Fecha por defecto").
AHORA = datetime(2026, 11, 6, 2, 30, tzinfo=timezone.utc)
LUNES = "2026-11-09"
DEST = "jefe@negocio.com"
ORIGEN = {"Origin": "http://localhost"}
TOKEN = "token-de-prueba"
SECRETO_REFRESH = "1//0gRefreshSecretoQueNoDebeAparecer1234567890"
SECRETO_CLIENTE = "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cer"
PDF = b"%PDF-1.4 falso"

RUTAS = (("get", "/reporte"), ("get", f"/reporte/pdf?fecha={LUNES}"), ("post", "/reporte/enviar"))


class Usuario:
    def __init__(self, autenticado=True, admin=True, id_=3, username="admin1"):
        self.is_authenticated = autenticado
        self.is_admin = admin
        self.id = id_
        self.username = username


class SesionFalsa:
    """Imita `Session(bind=db.engine)` como context manager. No toca ninguna base."""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class MailerFalso:
    def __init__(self, resultado="gmail-id-123", destinatario=DEST):
        self.destinatario = destinatario
        self.resultado = resultado
        self.envios = []

    def send(self, *, to, subject, body, attachments):
        self.envios.append({"to": to, "subject": subject, "body": body, "attachments": attachments})
        if isinstance(self.resultado, BaseException):
            raise self.resultado
        return self.resultado


@pytest.fixture(autouse=True)
def entorno_limpio(monkeypatch):
    """Sin REPORTE_*/RAILWAY_* heredadas; nunca GMAIL_*."""
    import os

    for nombre in list(os.environ):
        if nombre.startswith(("REPORTE_", "RAILWAY_", "GMAIL_")):
            monkeypatch.delenv(nombre, raising=False)
    monkeypatch.setenv("REPORTE_BOTON", "1")
    monkeypatch.setattr(vista, "_ahora", lambda: AHORA)


@pytest.fixture
def app():
    app = Flask(__name__, template_folder=str(TEMPLATES))
    app.config.update(SECRET_KEY="test", TESTING=True)
    LoginManager().init_app(app)  # sin login_view: unauthorized() -> 401
    app.register_blueprint(vista.bp)
    return app


@pytest.fixture
def cliente(app):
    return app.test_client()


@pytest.fixture
def como(monkeypatch):
    def _como(usuario):
        monkeypatch.setattr(vista, "current_user", usuario)

    _como(Usuario())
    return _como


@pytest.fixture
def renders(monkeypatch):
    capturas = []

    def falso(nombre, **contexto):
        capturas.append((nombre, contexto))
        return "ok"

    monkeypatch.setattr(vista, "render_template", falso)
    monkeypatch.setattr(vista, "_ultimas_pruebas", lambda: [])
    return capturas


@pytest.fixture
def auditoria(caplog):
    """El logger no propaga (handler propio a stdout): se le cuelga el de caplog."""
    vista.auditoria.addHandler(caplog.handler)
    caplog.set_level(logging.INFO)
    try:
        yield caplog
    finally:
        vista.auditoria.removeHandler(caplog.handler)


def _lineas(caplog):
    return [r.getMessage() for r in caplog.records if r.name == "backend.reporte.auditoria"]


@pytest.fixture
def backend_falso(monkeypatch):
    """Reemplaza DB, claim, registro, generación y mailer. Registra cada llamada."""

    class Estado:
        reclamos = []
        resultados_reclamo = []
        enviados = []
        fallas = []
        pdfs = []
        mailer = MailerFalso()
        mailer_error = None
        get_mailer_llamadas = []
        registrar_envio_error = None

    e = Estado()
    e.reclamos, e.resultados_reclamo, e.enviados, e.fallas, e.pdfs = [], [], [], [], []
    e.get_mailer_llamadas = []

    monkeypatch.setattr(vista, "_nueva_sesion", SesionFalsa)

    def reclamar_prueba(session, fecha, *, destinatario, ahora=None):
        e.reclamos.append((fecha, destinatario))
        if e.resultados_reclamo:
            return e.resultados_reclamo.pop(0)
        return Reclamo(envios.RECLAMADO, 5, 1)

    def registrar_envio(session, id_, message_id):
        if e.registrar_envio_error:
            raise e.registrar_envio_error
        e.enviados.append((id_, message_id))

    def registrar_falla(session, id_, error):
        e.fallas.append((id_, error))

    def armar_reporte(fecha, session):
        e.pdfs.append(fecha)
        return object()

    def get_mailer(env=None, *, opciones_gmail=None, servicio="cron"):
        e.get_mailer_llamadas.append((opciones_gmail, servicio))
        if e.mailer_error:
            raise e.mailer_error
        return e.mailer

    monkeypatch.setattr(envios, "reclamar_prueba", reclamar_prueba)
    monkeypatch.setattr(envios, "registrar_envio", registrar_envio)
    monkeypatch.setattr(envios, "registrar_falla", registrar_falla)
    monkeypatch.setattr(generar, "armar_reporte", armar_reporte)
    monkeypatch.setattr(generar, "render_pdf", lambda reporte, generado_en: PDF)
    monkeypatch.setattr(vista, "get_mailer", get_mailer)
    return e


def _con_token(cliente, **extra):
    with cliente.session_transaction() as s:
        s[vista.CLAVE_CSRF] = TOKEN
        s.update(extra)


def _post(cliente, fecha=LUNES, token=TOKEN, headers=ORIGEN, **form):
    datos = {"fecha": fecha, **form}
    if token is not None:
        datos["csrf_token"] = token
    return cliente.post("/reporte/enviar", data=datos, headers=headers or {})


def _flashes(cliente):
    with cliente.session_transaction() as s:
        return list(s.get("_flashes", []))


# --- flag, autorización y context processor ----------------------------------


@pytest.mark.parametrize("valor", [None, "0", "", "true"])
@pytest.mark.parametrize("metodo,ruta", RUTAS)
def test_flag_apagado_404_incluso_para_admin(cliente, como, backend_falso, monkeypatch, valor, metodo, ruta):
    if valor is None:
        monkeypatch.delenv("REPORTE_BOTON", raising=False)
    else:
        monkeypatch.setenv("REPORTE_BOTON", valor)
    _con_token(cliente)
    resp = getattr(cliente, metodo)(ruta, data={"fecha": LUNES, "csrf_token": TOKEN}, headers=ORIGEN)
    assert resp.status_code == 404
    assert backend_falso.reclamos == [] and backend_falso.pdfs == [] and backend_falso.mailer.envios == []


@pytest.mark.parametrize("metodo,ruta", RUTAS)
def test_no_admin_403(cliente, como, backend_falso, auditoria, metodo, ruta):
    como(Usuario(admin=False, id_=8, username="vendedor"))
    _con_token(cliente)
    resp = getattr(cliente, metodo)(ruta, data={"fecha": LUNES, "csrf_token": TOKEN}, headers=ORIGEN)
    assert resp.status_code == 403
    assert backend_falso.reclamos == [] and backend_falso.pdfs == [] and backend_falso.mailer.envios == []
    assert any("usuario=vendedor id=8" in l and "resultado=prohibido_no_admin" in l for l in _lineas(auditoria))


@pytest.mark.parametrize("metodo,ruta", RUTAS)
def test_admin_en_soporte_403(cliente, como, backend_falso, auditoria, metodo, ruta):
    _con_token(cliente, impersonate_user_id=9)
    resp = getattr(cliente, metodo)(ruta, data={"fecha": LUNES, "csrf_token": TOKEN}, headers=ORIGEN)
    assert resp.status_code == 403
    assert backend_falso.reclamos == [] and backend_falso.mailer.envios == []
    assert any("resultado=prohibido_soporte" in l for l in _lineas(auditoria))


@pytest.mark.parametrize("metodo,ruta", RUTAS)
def test_sin_login_unauthorized(cliente, como, backend_falso, metodo, ruta):
    como(Usuario(autenticado=False, admin=False))
    resp = getattr(cliente, metodo)(ruta, data={"fecha": LUNES}, headers=ORIGEN)
    assert resp.status_code == 401  # con login_view (app real) redirige a auth.login
    assert backend_falso.reclamos == [] and backend_falso.pdfs == []


@pytest.mark.parametrize(
    "boton,usuario,soporte,esperado",
    [
        ("1", Usuario(), None, True),
        ("1", Usuario(), 9, False),
        ("1", Usuario(admin=False), None, False),
        ("1", Usuario(autenticado=False), None, False),
        (None, Usuario(), None, False),
    ],
)
def test_context_processor_reporte_boton(app, monkeypatch, boton, usuario, soporte, esperado):
    if boton is None:
        monkeypatch.delenv("REPORTE_BOTON", raising=False)
    monkeypatch.setattr(vista, "current_user", usuario)
    with app.test_request_context("/"):
        from flask import session

        if soporte:
            session["impersonate_user_id"] = soporte
        assert bool(vista.inyectar_reporte_boton().get("reporte_boton")) is esperado


def test_context_processor_nunca_lanza(app, monkeypatch):
    class Explota:
        @property
        def is_authenticated(self):
            raise RuntimeError("sin login manager")

    monkeypatch.setattr(vista, "current_user", Explota())
    with app.test_request_context("/"):
        assert not vista.inyectar_reporte_boton().get("reporte_boton")


# --- página -------------------------------------------------------------------


def test_pagina_fecha_por_defecto_hoy_art(cliente, como, renders, monkeypatch):
    monkeypatch.setenv("REPORTE_MAIL_TO", DEST)
    resp = cliente.get("/reporte")
    assert resp.status_code == 200
    nombre, ctx = renders[-1]
    assert nombre == "reporte_generar.html"
    assert ctx["fecha"] == HOY  # 02:30 UTC del 06/11 = 23:30 ART del 05/11
    assert (ctx["fecha_min"], ctx["fecha_max"]) == (date(2025, 11, 5), date(2026, 12, 6))
    assert ctx["destinatario"] == "j***@negocio.com"
    assert ctx["csrf_token"]
    with cliente.session_transaction() as s:
        assert s[vista.CLAVE_CSRF] == ctx["csrf_token"]
    assert ctx["active"] == "reporte"


def test_pagina_lunes_dice_extendido_y_token_estable(cliente, como, renders):
    cliente.get(f"/reporte?fecha={LUNES}")
    cliente.get("/reporte")
    _, ctx1 = renders[0]
    _, ctx2 = renders[1]
    assert ctx1["fecha"] == date(2026, 11, 9) and "extendido" in ctx1["modo"]
    assert "del día" in ctx2["modo"]
    assert ctx1["csrf_token"] == ctx2["csrf_token"]


def test_pagina_fecha_invalida_muestra_error_y_usa_hoy(cliente, como, renders):
    cliente.get("/reporte?fecha=2026-13-40")
    _, ctx = renders[-1]
    assert ctx["fecha"] == HOY and "inválida" in ctx["error_fecha"]


def _env_plantillas(app):
    """La página real con un layout stub (layout.html necesita endpoints de main)."""
    return app.jinja_env.overlay(
        loader=ChoiceLoader(
            [DictLoader({"layout.html": "{% block content %}{% endblock %}"}), FileSystemLoader(str(TEMPLATES))]
        )
    )


def test_plantilla_compila_y_lleva_csrf_y_flashes(app):
    app.jinja_env.get_template("reporte_generar.html")  # compila con el layout real
    env = _env_plantillas(app)
    with app.test_request_context("/reporte"):
        from flask import flash

        flash("Enviado a jefe@negocio.com (id abc)", "success")
        html = env.get_template("reporte_generar.html").render(
            fecha=date(2026, 11, 9),
            fecha_min=date(2025, 11, 5),
            fecha_max=date(2026, 12, 6),
            error_fecha="",
            modo="lun 09/11/2026: extendido",
            destinatario="j***@negocio.com",
            csrf_token="TOKEN-XYZ",
            pruebas=[
                envios.FilaPrueba(date(2026, 11, 9), "SENT", 2, datetime(2026, 11, 5, 14, 0), datetime(2026, 11, 5, 14, 0))
            ],
            hora_art=vista.hora_art,
            url_for=lambda ep, **kw: "/" + ep,
            get_flashed_messages=__import__("flask").get_flashed_messages,
        )
    assert 'name="csrf_token" value="TOKEN-XYZ"' in html
    assert 'method="post"' in html and 'target="_blank"' in html
    assert "alert-success" in html and "Enviado a jefe@negocio.com (id abc)" in html
    assert "Enviando…" in html and "disabled = true" in html
    assert "09/11/2026" in html and "05/11 11:00" in html  # 14:00 UTC = 11:00 ART
    assert 'name="to"' not in html


# --- validación de fecha (pura) -------------------------------------------------


@pytest.mark.parametrize(
    "texto,ok",
    [
        ("2025-11-05", True),  # hoy − 365
        ("2025-11-04", False),  # hoy − 366
        ("2026-12-06", True),  # hoy + 31
        ("2026-12-07", False),  # hoy + 32
        ("2026-11-07", True),  # sábado: se permite
        ("2026-13-40", False),
        ("2026-11-5", False),
        ("", False),
        (None, False),
        ("2026-11-09'; drop", False),
    ],
)
def test_fecha_valida_bordes(texto, ok):
    fecha, error = vista._fecha_valida(texto, HOY)
    assert (fecha is not None) is ok
    assert bool(error) is not ok


# --- Ver PDF --------------------------------------------------------------------


def test_ver_pdf_inline_sin_mailer_ni_claim(cliente, como, backend_falso, auditoria, monkeypatch):
    def no_llamar(*a, **k):
        raise AssertionError("Ver PDF no construye mailer")

    monkeypatch.setattr(vista, "get_mailer", no_llamar)
    resp = cliente.get(f"/reporte/pdf?fecha={LUNES}")
    assert resp.status_code == 200
    assert resp.mimetype == "application/pdf"
    assert resp.data == PDF
    assert "inline" in resp.headers["Content-Disposition"]
    assert "reporte-diario-2026-11-09.pdf" in resp.headers["Content-Disposition"]
    assert resp.headers["Cache-Control"] == "no-store"
    assert backend_falso.pdfs == [date(2026, 11, 9)]
    assert backend_falso.reclamos == [] and backend_falso.enviados == [] and backend_falso.fallas == []
    assert any("accion=pdf resultado=ok" in l and "fecha=2026-11-09" in l for l in _lineas(auditoria))


def test_ver_pdf_sin_ninguna_config_de_gmail(cliente, como, backend_falso, monkeypatch):
    monkeypatch.setenv("RAILWAY_ENVIRONMENT_NAME", "production")  # W1 rompería get_mailer
    resp = cliente.get(f"/reporte/pdf?fecha={LUNES}")
    assert resp.status_code == 200 and resp.data == PDF


@pytest.mark.parametrize("fecha", ["2026-13-40", "2026-12-07", "2025-11-04", ""])
def test_ver_pdf_fecha_invalida_400(cliente, como, backend_falso, auditoria, fecha):
    resp = cliente.get(f"/reporte/pdf?fecha={fecha}")
    assert resp.status_code == 400
    assert resp.mimetype == "text/plain"
    assert backend_falso.pdfs == []
    assert any("accion=pdf resultado=fecha_invalida" in l for l in _lineas(auditoria))


def test_ver_pdf_error_de_generacion_500_sin_detalle(cliente, como, backend_falso, monkeypatch):
    def explota(fecha, session):
        raise RuntimeError("postgresql://usuario:clave@host/db")

    monkeypatch.setattr(generar, "armar_reporte", explota)
    resp = cliente.get(f"/reporte/pdf?fecha={LUNES}")
    assert resp.status_code == 500
    assert b"RuntimeError" in resp.data and b"clave" not in resp.data


# --- Enviar prueba: CSRF y origen ------------------------------------------------


@pytest.mark.parametrize(
    "token,headers",
    [
        (None, ORIGEN),  # sin token
        ("otro-token", ORIGEN),  # token malo
        (TOKEN, {"Origin": "https://otro-sitio.com"}),  # origen ajeno
        (TOKEN, {}),  # sin Origin ni Referer
        (TOKEN, {"Referer": "https://otro-sitio.com/reporte"}),
        (TOKEN, {"Origin": "null"}),
    ],
)
def test_csrf_u_origen_invalido_400_sin_efectos(cliente, como, backend_falso, auditoria, token, headers):
    _con_token(cliente)
    resp = _post(cliente, token=token, headers=headers)
    assert resp.status_code == 400
    assert backend_falso.get_mailer_llamadas == []
    assert backend_falso.reclamos == [] and backend_falso.mailer.envios == [] and backend_falso.pdfs == []
    lineas = _lineas(auditoria)
    assert any("accion=enviar" in l and ("csrf_invalido" in l or "origen_invalido" in l) for l in lineas)
    assert all(TOKEN not in l for l in lineas)


def test_sin_token_en_la_sesion_400(cliente, como, backend_falso):
    resp = _post(cliente)  # la sesión nunca pasó por la página
    assert resp.status_code == 400
    assert backend_falso.reclamos == []


def test_referer_del_mismo_host_alcanza(cliente, como, backend_falso):
    _con_token(cliente)
    resp = _post(cliente, headers={"Referer": "http://localhost/reporte?fecha=2026-11-09"})
    assert resp.status_code == 303
    assert len(backend_falso.mailer.envios) == 1


# --- Enviar prueba: flujo -----------------------------------------------------------


def test_envio_ok_303_flash_y_auditoria(cliente, como, backend_falso, auditoria, monkeypatch):
    monkeypatch.setenv("REPORTE_GMAIL_REFRESH_TOKEN", SECRETO_REFRESH)
    monkeypatch.setenv("REPORTE_GMAIL_CLIENT_SECRET", SECRETO_CLIENTE)
    _con_token(cliente)

    resp = _post(cliente)

    assert resp.status_code == 303
    assert resp.headers["Location"].endswith("/reporte?fecha=2026-11-09")
    assert backend_falso.get_mailer_llamadas == [(vista.OPCIONES_WEB, "web")]
    assert backend_falso.reclamos == [(date(2026, 11, 9), DEST)]
    (mail,) = backend_falso.mailer.envios
    assert mail["to"] == DEST
    assert mail["subject"] == "[PRUEBA] Reporte diario – lun 09/11/2026"
    assert mail["body"].startswith("Envío de prueba generado desde la web")
    assert mail["attachments"][0].filename == "reporte-diario-2026-11-09.pdf"
    assert backend_falso.enviados == [(5, "gmail-id-123")]
    assert _flashes(cliente) == [("success", f"Enviado a {DEST} (id gmail-id-123).")]
    (linea,) = [l for l in _lineas(auditoria) if "accion=enviar" in l]
    for campo in ("usuario=admin1", "id=3", "fecha=2026-11-09", "accion=enviar", "resultado=ok",
                  "intento=1", "message_id=gmail-id-123"):
        assert campo in linea
    texto = auditoria.text + "".join(_lineas(auditoria))
    assert SECRETO_REFRESH not in texto and SECRETO_CLIENTE not in texto and TOKEN not in texto


def test_destinatario_inyectado_se_ignora(cliente, como, backend_falso):
    _con_token(cliente)
    _post(cliente, to="otro@ejemplo.com", destinatario="otro@ejemplo.com")
    (mail,) = backend_falso.mailer.envios
    assert mail["to"] == DEST
    assert backend_falso.reclamos == [(date(2026, 11, 9), DEST)]


def test_sabado_se_envia_con_asunto_sab(cliente, como, backend_falso):
    _con_token(cliente)
    _post(cliente, fecha="2026-11-07")
    assert backend_falso.mailer.envios[0]["subject"] == "[PRUEBA] Reporte diario – sáb 07/11/2026"


def test_w1_railway_sin_mailer_flash_danger_sin_claim(cliente, como, monkeypatch, backend_falso, auditoria):
    """get_mailer REAL: en Railway sin REPORTE_MAILER falla antes de reclamar."""
    from backend.services import mailer as mailer_pkg

    monkeypatch.setattr(vista, "get_mailer", mailer_pkg.get_mailer)
    monkeypatch.setenv("RAILWAY_ENVIRONMENT_NAME", "production")
    monkeypatch.setenv("GMAIL_PASSWORD", "no-se-usa")  # nunca se cae en GMAIL_*
    _con_token(cliente)

    resp = _post(cliente)

    assert resp.status_code == 303
    assert backend_falso.reclamos == [] and backend_falso.pdfs == []
    ((categoria, texto),) = _flashes(cliente)
    assert categoria == "danger"
    assert "El servicio web no tiene configurado Gmail (REPORTE_MAILER y REPORTE_GMAIL_*)" in texto
    assert "servicio web" in texto and "enviado" not in texto.lower()
    assert any("resultado=config" in l for l in _lineas(auditoria))


def test_gmail_api_sin_refresh_token_es_error_de_config(cliente, como, monkeypatch, backend_falso):
    from backend.services import mailer as mailer_pkg

    monkeypatch.setattr(vista, "get_mailer", mailer_pkg.get_mailer)
    monkeypatch.setenv("REPORTE_MAILER", "gmail_api")
    monkeypatch.setenv("GMAIL_PASSWORD", "no-se-usa")
    _con_token(cliente)
    _post(cliente)
    assert backend_falso.reclamos == []
    ((categoria, texto),) = _flashes(cliente)
    assert categoria == "danger" and "REPORTE_GMAIL_REFRESH_TOKEN" in texto


def test_modo_archivo_local_guarda_eml(cliente, como, monkeypatch, backend_falso, tmp_path):
    from backend.services import mailer as mailer_pkg

    monkeypatch.setattr(vista, "get_mailer", mailer_pkg.get_mailer)
    monkeypatch.setenv("REPORTE_ARCHIVO_DIR", str(tmp_path))
    _con_token(cliente)

    _post(cliente)

    eml = tmp_path / "reporte-diario-2026-11-09.eml"
    assert eml.exists()
    assert b"PRUEBA" in eml.read_bytes()
    ((categoria, texto),) = _flashes(cliente)
    assert categoria == "success" and texto.startswith(f"Guardado en {eml}")
    assert backend_falso.enviados == [(5, f"archivo:{eml}")]


@pytest.mark.parametrize(
    "estado,texto",
    [(envios.EN_CURSO, "en curso"), (envios.DEMASIADO_PRONTO, "menos de 60 segundos")],
)
def test_reclamo_rechazado_warning_sin_envio(cliente, como, backend_falso, auditoria, estado, texto):
    backend_falso.resultados_reclamo.append(Reclamo(estado, None, None))
    _con_token(cliente)

    resp = _post(cliente)

    assert resp.status_code == 303
    assert backend_falso.mailer.envios == [] and backend_falso.pdfs == []
    ((categoria, mensaje),) = _flashes(cliente)
    assert categoria == "warning"
    assert "ya hay un envío de prueba en curso o reciente" in mensaje.lower() and texto in mensaje
    assert any(f"resultado={estado.lower()}" in l for l in _lineas(auditoria))


def test_doble_click_un_solo_mail(cliente, como, backend_falso):
    """El segundo POST encuentra la fila PENDING (lo resuelve reclamar_prueba, ver test_envios_db)."""
    backend_falso.resultados_reclamo.extend([Reclamo(envios.RECLAMADO, 5, 1), Reclamo(envios.EN_CURSO, 5, 1)])
    _con_token(cliente)
    _post(cliente)
    _post(cliente)
    assert len(backend_falso.mailer.envios) == 1
    assert [c for c, _ in _flashes(cliente)] == ["success", "warning"]


def test_token_revocado_registra_falla_y_pide_reautorizar(cliente, como, backend_falso, auditoria):
    backend_falso.mailer.resultado = TokenRevocadoError("invalid_grant: el token de Gmail fue revocado")
    _con_token(cliente)

    _post(cliente)

    assert backend_falso.fallas == [(5, "invalid_grant: el token de Gmail fue revocado")]
    assert backend_falso.enviados == []
    ((categoria, texto),) = _flashes(cliente)
    assert categoria == "danger" and "re-autorizar" in texto and "scripts/autorizar_gmail.py" in texto
    assert any("resultado=token_revocado" in l for l in _lineas(auditoria))


def test_envio_incierto_registra_falla_incierta_y_dice_revisar_enviados(cliente, como, backend_falso, auditoria):
    backend_falso.mailer.resultado = EnvioIncierto("Gmail no respondió (ReadTimeout)")
    _con_token(cliente)

    _post(cliente)

    ((id_, error),) = backend_falso.fallas
    assert id_ == 5 and error.startswith(envios.PREFIJO_INCIERTO)
    ((categoria, texto),) = _flashes(cliente)
    assert categoria == "warning" and "Revisá Enviados" in texto
    assert any("resultado=incierto" in l for l in _lineas(auditoria))


def test_error_inesperado_no_filtra_el_texto(cliente, como, backend_falso):
    backend_falso.mailer.resultado = RuntimeError("postgresql://u:clave@host/db")
    _con_token(cliente)
    _post(cliente)
    ((categoria, texto),) = _flashes(cliente)
    assert categoria == "danger" and texto == "Error inesperado (RuntimeError)."
    assert backend_falso.fallas and backend_falso.fallas[0][1].startswith("RuntimeError")


def test_enviado_pero_no_registrado_dice_no_reenviar(cliente, como, backend_falso, auditoria):
    backend_falso.registrar_envio_error = RuntimeError("db caida")
    _con_token(cliente)
    _post(cliente)
    ((categoria, texto),) = _flashes(cliente)
    assert categoria == "danger" and "NO reenviar" in texto and "gmail-id-123" in texto
    assert backend_falso.fallas == []  # no se marca FAILED: el mail salió
    assert any("resultado=enviado_sin_registrar" in l for l in _lineas(auditoria))


def test_claim_que_explota_no_da_500(cliente, como, backend_falso, monkeypatch):
    def explota(*a, **k):
        raise RuntimeError("tabla inexistente")

    monkeypatch.setattr(envios, "reclamar_prueba", explota)
    _con_token(cliente)
    resp = _post(cliente)
    assert resp.status_code == 303
    assert backend_falso.mailer.envios == []
    assert _flashes(cliente)[0][0] == "danger"


@pytest.mark.parametrize("fecha", ["2026-12-07", "2025-11-04", "2026-13-40"])
def test_fecha_fuera_de_rango_error_sin_efectos(cliente, como, backend_falso, auditoria, fecha):
    _con_token(cliente)
    resp = _post(cliente, fecha=fecha)
    assert resp.status_code == 303
    assert backend_falso.get_mailer_llamadas == [] and backend_falso.reclamos == []
    ((categoria, _),) = _flashes(cliente)
    assert categoria == "danger"
    assert any("resultado=fecha_invalida" in l for l in _lineas(auditoria))


# --- mensaje_resultado y auditoría (puros) --------------------------------------------


def test_mensaje_resultado_mapeo():
    assert vista.mensaje_resultado("abc", destinatario=DEST) == ("success", f"Enviado a {DEST} (id abc).")
    cat, txt = vista.mensaje_resultado("archivo:/tmp/x.eml")
    assert cat == "success" and txt.startswith("Guardado en /tmp/x.eml")
    assert vista.mensaje_resultado(MailerError("faltan X"), etapa="config")[1].startswith(
        "El servicio web no tiene configurado Gmail (REPORTE_MAILER y REPORTE_GMAIL_*): faltan X"
    )
    assert vista.mensaje_resultado(TokenRevocadoError("x")) == ("danger", vista.MSG_TOKEN)
    assert vista.mensaje_resultado(EnvioIncierto("x")) == ("warning", vista.MSG_INCIERTO)
    assert vista.mensaje_resultado(MailerError("Gmail 503")) == ("danger", "Falló el envío: Gmail 503")
    assert vista.mensaje_resultado(ValueError("secreto")) == ("danger", "Error inesperado (ValueError).")
    cat, txt = vista.mensaje_resultado("id-9", etapa="registro")
    assert cat == "danger" and "NO reenviar" in txt and "id-9" in txt


def test_auditoria_no_permite_inyectar_lineas(app, monkeypatch, auditoria):
    monkeypatch.setattr(vista, "current_user", Usuario(username="malo\nusuario=admin resultado=ok"))
    with app.test_request_context("/"):
        vista._auditar("enviar", "csrf_invalido", fecha="x y\nz")
    (linea,) = _lineas(auditoria)
    assert "\n" not in linea and linea.count("usuario=") == 1


def test_auditoria_logger_idempotente_y_a_stdout():
    antes = list(vista.auditoria.handlers)
    vista._configurar_auditoria(vista.auditoria)
    assert vista.auditoria.handlers == antes
    assert vista.auditoria.propagate is False and vista.auditoria.level == logging.INFO
    assert any(isinstance(h, logging.StreamHandler) for h in antes)


def test_enmascarar():
    assert vista.enmascarar("jefe@negocio.com") == "j***@negocio.com"
    assert vista.enmascarar("") == "(sin configurar)"
