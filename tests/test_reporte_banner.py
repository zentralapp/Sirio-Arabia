"""Aviso del reporte diario en la web (spec H, ADR-8).

No se levanta `backend.app`: importarlo ejecuta create_app() (create_all,
ALTERs, seed) y sin AUTO_MIGRATE=1 el arranque se cuelga (#7). Se prueba el
context processor sobre una app Flask mínima con la carpeta de plantillas
real, el parcial `_reporte_alerta.html` renderizado de verdad, y que
layout.html y app.py lo enganchen donde dice el design.
"""
import re
import subprocess
import sys
from pathlib import Path

import pytest
from flask import Flask, render_template

from backend.views import reporte_estado as vista

REPO = Path(__file__).resolve().parent.parent
TEMPLATES = REPO / "backend" / "templates"

ALERTA_FALLA = {"nivel": "danger", "mensaje": "Falló el reporte del 29/10.", "detalle": "RuntimeError: boom", "fecha": "29/10"}
ALERTA_INCIERTA = {
    "nivel": "warning",
    "mensaje": "Estado incierto del reporte del 29/10: revisar Enviados antes de reenviarlo con --force.",
    "detalle": "",
    "fecha": "29/10",
}


class Usuario:
    def __init__(self, autenticado=True, admin=True):
        self.is_authenticated = autenticado
        self.is_admin = admin


@pytest.fixture
def app():
    app = Flask(__name__, template_folder=str(TEMPLATES))
    # B7: con REPORTE_BOTON=1 el botón exige una SECRET_KEY real (no la default ni ninguna).
    app.config["SECRET_KEY"] = "test-no-default"
    app.register_blueprint(vista.bp)
    return app


@pytest.fixture
def consultas(monkeypatch):
    """Reemplaza el servicio: registra cada llamada y devuelve `valor`."""

    class Falso:
        valor = dict(ALERTA_FALLA)
        llamadas = 0

        def __call__(self):
            type(self).llamadas += 1
            return dict(self.valor)

    falso = Falso()
    Falso.llamadas = 0
    monkeypatch.setattr(vista, "estado_reporte", falso)
    return falso


def _como(monkeypatch, usuario):
    monkeypatch.setattr(vista, "current_user", usuario)


def _render(app):
    with app.test_request_context("/"):
        return render_template("_reporte_alerta.html")


def test_admin_con_banner_prendido_ve_el_aviso(app, consultas, monkeypatch):
    monkeypatch.setenv("REPORTE_BANNER", "1")
    _como(monkeypatch, Usuario(admin=True))
    html = _render(app)
    assert 'role="alert"' in html
    assert "alert-danger" in html
    assert "Falló el reporte del 29/10." in html
    assert "Detalle: RuntimeError: boom" in html


def test_incierto_se_muestra_como_warning(app, consultas, monkeypatch):
    monkeypatch.setenv("REPORTE_BANNER", "1")
    _como(monkeypatch, Usuario())
    consultas.valor = dict(ALERTA_INCIERTA)
    html = _render(app)
    assert "alert-warning" in html and "alert-danger" not in html
    assert "Enviados" in html and "--force" in html
    assert "Detalle:" not in html


def test_no_admin_no_ve_nada_ni_consulta(app, consultas, monkeypatch):
    monkeypatch.setenv("REPORTE_BANNER", "1")
    _como(monkeypatch, Usuario(admin=False))
    assert "alert" not in _render(app)
    assert consultas.llamadas == 0


def test_anonimo_no_ve_nada_ni_consulta(app, consultas, monkeypatch):
    monkeypatch.setenv("REPORTE_BANNER", "1")
    _como(monkeypatch, Usuario(autenticado=False, admin=True))
    assert "alert" not in _render(app)
    assert consultas.llamadas == 0


@pytest.mark.parametrize("valor", [None, "0", "", "true", "si"])
def test_interruptor_apagado_no_muestra_ni_consulta(app, consultas, monkeypatch, valor):
    if valor is None:
        monkeypatch.delenv("REPORTE_BANNER", raising=False)
    else:
        monkeypatch.setenv("REPORTE_BANNER", valor)
    _como(monkeypatch, Usuario(admin=True))
    assert "alert" not in _render(app)
    assert consultas.llamadas == 0


def test_todo_bien_no_muestra_nada(app, consultas, monkeypatch):
    monkeypatch.setenv("REPORTE_BANNER", "1")
    _como(monkeypatch, Usuario())
    consultas.valor = {}
    assert "alert" not in _render(app)


def test_si_el_servicio_lanza_la_pagina_carga_igual(app, monkeypatch):
    monkeypatch.setenv("REPORTE_BANNER", "1")
    _como(monkeypatch, Usuario())

    def roto():
        raise RuntimeError("boom")

    monkeypatch.setattr(vista, "estado_reporte", roto)
    assert "alert" not in _render(app)


def test_si_current_user_lanza_la_pagina_carga_igual(app, consultas, monkeypatch):
    monkeypatch.setenv("REPORTE_BANNER", "1")

    class Explota:
        @property
        def is_authenticated(self):
            raise RuntimeError("sin login manager")

    _como(monkeypatch, Explota())
    assert "alert" not in _render(app)


def test_el_html_escapa_el_texto(app, consultas, monkeypatch):
    monkeypatch.setenv("REPORTE_BANNER", "1")
    _como(monkeypatch, Usuario())
    consultas.valor = {**ALERTA_FALLA, "detalle": "<script>alert(1)</script>"}
    html = _render(app)
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


def test_servicio_real_sin_base_no_rompe(app, monkeypatch):
    """Sin SQLAlchemy inicializado (db.engine lanza) el aviso no aparece."""
    from backend.services.reporte_estado import limpiar_cache

    limpiar_cache()
    monkeypatch.setenv("REPORTE_BANNER", "1")
    _como(monkeypatch, Usuario())
    try:
        assert "alert" not in _render(app)
    finally:
        limpiar_cache()


# --- enganche en layout.html y app.py ----------------------------------------


def test_layout_incluye_el_parcial_entre_soporte_y_contenido():
    layout = (TEMPLATES / "layout.html").read_text(encoding="utf-8")
    inc = layout.index("{% include '_reporte_alerta.html' %}")
    soporte = layout.index("{% if support_mode %}")
    contenido = layout.index("{% block content %}")
    assert soporte < inc < contenido
    assert layout.count("_reporte_alerta.html") == 1


def test_layout_y_parcial_parsean():
    from jinja2 import Environment, FileSystemLoader

    env = Environment(loader=FileSystemLoader(str(TEMPLATES)))
    env.get_template("layout.html")
    env.get_template("_reporte_alerta.html")


def test_app_registra_el_blueprint_tras_main():
    fuente = (REPO / "backend" / "app.py").read_text(encoding="utf-8")
    patron = (
        r"app\.register_blueprint\(main_bp\)\n"
        r"\s+from \.views\.reporte_estado import bp as reporte_estado_bp\n"
        r"\s+app\.register_blueprint\(reporte_estado_bp\)\n"
    )
    assert re.search(patron, fuente)


def test_vista_no_importa_app_ni_main():
    script = (
        "import sys, backend.views.reporte_estado\n"
        "print(','.join(sorted(m for m in sys.modules if m in ('backend.app', 'backend.views.main'))))"
    )
    resultado = subprocess.run([sys.executable, "-c", script], cwd=REPO, capture_output=True, text=True, timeout=60)
    assert resultado.returncode == 0, resultado.stderr
    assert resultado.stdout.strip() == ""


# --- link al botón "Generar reporte" (spec L "Ubicación") ----------------------


@pytest.mark.parametrize("boton,admin,esperado", [("1", True, True), (None, True, False), ("1", False, False)])
def test_parcial_muestra_link_al_boton_solo_con_reporte_boton(app, consultas, monkeypatch, boton, admin, esperado):
    monkeypatch.setenv("REPORTE_BANNER", "1")
    if boton is None:
        monkeypatch.delenv("REPORTE_BOTON", raising=False)
    else:
        monkeypatch.setenv("REPORTE_BOTON", boton)
    usuario = Usuario(admin=admin)
    _como(monkeypatch, usuario)
    with app.test_request_context("/"):
        html = render_template("_reporte_alerta.html", reporte_alerta=dict(ALERTA_FALLA))
    assert ("Generar / reenviar prueba" in html) is esperado
    if esperado:
        assert 'href="/reporte"' in html


def test_parcial_sin_link_con_secret_key_por_defecto(app, consultas, monkeypatch):
    monkeypatch.setenv("REPORTE_BANNER", "1")
    monkeypatch.setenv("REPORTE_BOTON", "1")
    monkeypatch.setattr(vista, "_aviso_secret_key_emitido", False)
    app.config["SECRET_KEY"] = "dev-secret-key"
    _como(monkeypatch, Usuario(admin=True))
    with app.test_request_context("/"):
        html = render_template("_reporte_alerta.html", reporte_alerta=dict(ALERTA_FALLA))
    assert "Generar / reenviar prueba" not in html


def test_layout_tiene_el_link_en_los_dos_navs():
    layout = (TEMPLATES / "layout.html").read_text(encoding="utf-8")
    assert layout.count("url_for('reporte_estado.generar')") == 2
    assert layout.count("{% if reporte_boton %}") == 2
    # Después de "Usuarios" en cada nav.
    for marca in ("url_for('main.usuarios') }}\"><i class=\"bi bi-person-badge me-2\"></i>Usuarios",):
        primero = layout.index(marca)
        segundo = layout.index(marca, primero + 1)
        links = [i for i in range(len(layout)) if layout.startswith("url_for('reporte_estado.generar')", i)]
        assert primero < links[0] < segundo < links[1]


def test_layout_sin_flag_no_muestra_el_link(app, monkeypatch):
    """Render real del bloque del nav: sin reporte_boton no aparece."""
    from jinja2 import Environment

    layout = (TEMPLATES / "layout.html").read_text(encoding="utf-8")
    inicio = layout.index("{% if reporte_boton %}")
    fin = layout.index("{% endif %}", layout.index("</li>", inicio)) + len("{% endif %}")
    bloque = Environment().from_string(layout[inicio:fin])
    url_for = lambda ep: "/reporte"  # noqa: E731
    assert "Generar reporte" not in bloque.render(url_for=url_for)
    assert "Generar reporte" in bloque.render(url_for=url_for, reporte_boton=True)
