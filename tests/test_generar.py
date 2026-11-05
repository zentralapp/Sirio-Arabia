"""Generación compartida (job y web): `generar_pdf` y `armar_mail`. Sin DB ni red."""
from datetime import date, datetime, timezone

import backend.services.reporte_diario.generar as generar
from backend.services.mailer.base import CUERPO_PRUEBA, CUERPO_REPORTE, Adjunto
from backend.services.reporte_diario.fechas import fin_de_semana
from backend.services.reporte_diario.modelo import Reporte

LUNES = date(2026, 11, 9)
SABADO = date(2026, 11, 7)
PDF = b"%PDF-1.7 de prueba"


def test_armar_mail_normal_es_el_del_envio_automatico():
    mail = generar.armar_mail(LUNES, PDF)

    assert mail.subject == "Reporte diario – lun 09/11/2026"
    assert mail.body == CUERPO_REPORTE
    assert mail.attachments == (Adjunto("reporte-diario-2026-11-09.pdf", PDF),)


def test_armar_mail_de_prueba_lleva_prefijo_y_cuerpo_de_prueba():
    mail = generar.armar_mail(LUNES, PDF, prueba=True)

    assert mail.subject == "[PRUEBA] Reporte diario – lun 09/11/2026"
    assert mail.body == CUERPO_PRUEBA
    # El nombre del adjunto no cambia con la prueba.
    assert mail.attachments == (Adjunto("reporte-diario-2026-11-09.pdf", PDF),)


def test_armar_mail_de_prueba_en_sabado():
    assert generar.armar_mail(SABADO, PDF, prueba=True).subject == "[PRUEBA] Reporte diario – sáb 07/11/2026"


def _parchear(monkeypatch, eventos):
    reporte = Reporte(hoy=LUNES, domingo=fin_de_semana(LUNES))

    def armar_reporte(hoy, session=None):
        eventos.append(("armar", hoy, session))
        return reporte

    def render_pdf(rep, generado_en):
        eventos.append(("render", rep, generado_en))
        return PDF

    monkeypatch.setattr(generar, "armar_reporte", armar_reporte)
    monkeypatch.setattr(generar, "render_pdf", render_pdf)
    return reporte


def test_generar_pdf_lee_con_la_sesion_dada_y_dibuja(monkeypatch):
    eventos = []
    reporte = _parchear(monkeypatch, eventos)
    cuando = datetime(2026, 11, 9, 11, 0, tzinfo=timezone.utc)

    assert generar.generar_pdf("sesion", LUNES, cuando) == (reporte, PDF)
    assert eventos == [("armar", LUNES, "sesion"), ("render", reporte, cuando)]


def test_generar_pdf_generado_en_por_defecto_es_ahora_con_zona(monkeypatch):
    eventos = []
    _parchear(monkeypatch, eventos)
    antes = datetime.now(timezone.utc)

    generar.generar_pdf("sesion", LUNES)

    generado_en = eventos[-1][2]
    assert generado_en.tzinfo is not None
    assert antes <= generado_en <= datetime.now(timezone.utc)


def test_generar_pdf_al_armar_corre_entre_lectura_y_render(monkeypatch):
    eventos = []
    reporte = _parchear(monkeypatch, eventos)

    generar.generar_pdf("sesion", LUNES, al_armar=lambda r: eventos.append(("al_armar", r)))

    assert [e[0] for e in eventos] == ["armar", "al_armar", "render"]
    assert eventos[1][1] is reporte
