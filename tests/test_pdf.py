"""PDF del reporte diario.

El contenido se verifica sobre `armar_secciones` (lo mismo que se dibuja,
ya formateado) y el PDF en sí con chequeos livianos sobre los bytes, sin
agregar dependencias para leer PDFs. Si `pdftotext` (poppler) está instalado
se verifica además el texto real del documento; si no, ese test se saltea.
"""
import re
import shutil
import subprocess
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from backend.services.reporte_diario.clasificar import componer_reporte
from backend.services.reporte_diario.modelo import (
    A_COBRAR,
    ATRASADO,
    EN_CAMINO,
    FilaCobranza,
    FilaCumple,
    Reporte,
)
from backend.services.reporte_diario.pdf import (
    SIN_MONTO,
    SIN_NOVEDADES,
    SIN_REGISTROS,
    armar_secciones,
    render_pdf,
)
from backend.services.saldos import armar_saldo

D = Decimal
JUEVES = date(2026, 10, 29)
DOMINGO = date(2026, 11, 1)
# 11:03 UTC = 08:03 ART.
GENERADO = datetime(2026, 10, 29, 11, 3, tzinfo=timezone.utc)

TITULOS = (
    "Cumpleaños de hoy",
    "Cumpleaños de la semana",
    "Cobranzas que vencen hoy",
    "Cobranzas que vencen en la semana",
    "ATRASADAS",
    "Pagos parciales",
    "Pagadas sin registrar cobro",
)


def cobranza(order_id=1, venc=JUEVES, monto="1000", pagado="0", nc="0", cliente="Cliente", estado=A_COBRAR):
    monto = None if monto is None else D(monto)
    return FilaCobranza(
        order_id=order_id,
        cliente=cliente,
        empresa="Empresa",
        vencimiento=venc,
        estado=estado,
        saldo=armar_saldo(monto, None, None, D(pagado), D(nc)),
    )


def cumple(nombre="Ana", fecha=date(1985, 10, 29)):
    return FilaCumple(nombre=nombre, puesto="Compras", cliente="Cliente", empresa="Empresa", fecha=fecha)


def reporte(cobranzas=(), cumples=(), hoy=JUEVES):
    return componer_reporte(hoy, cobranzas, cumples)


def por_titulo(secciones):
    return {s.titulo: s for s in secciones}


def paginas(pdf: bytes) -> int:
    return len(re.findall(rb"/Type\s*/Page\b(?!s)", pdf))


def reporte_completo():
    return reporte(
        cobranzas=[
            cobranza(1, venc=JUEVES, monto="1000"),
            cobranza(2, venc=DOMINGO, monto="2500.5", estado=EN_CAMINO),
            cobranza(3, venc=date(2026, 6, 30), monto="1234567.5", estado=ATRASADO, cliente="Muñoz “El Ñandú” — Pérez"),
            cobranza(4, venc=date(2026, 10, 24), monto="1000", pagado="400", estado=ATRASADO),
            cobranza(5, venc=date(2026, 10, 15), monto="1000", pagado="1000", estado=ATRASADO),
            cobranza(6, venc=date(2026, 10, 20), monto=None, estado=ATRASADO),
            cobranza(7, venc=None, monto="300"),
        ],
        cumples=[cumple("Ana"), cumple("Beto", date(1990, 11, 1))],
    )


# ---------------------------------------------------------------- secciones


class TestSecciones:
    def test_orden_y_titulos_con_conteo(self):
        secciones = armar_secciones(reporte_completo())

        assert [s.titulo for s in secciones] == [*TITULOS, "Sin fecha de vencimiento"]
        conteos = {s.titulo: s.conteo for s in secciones}
        assert conteos == {
            "Cumpleaños de hoy": 1,
            "Cumpleaños de la semana": 1,
            "Cobranzas que vencen hoy": 1,
            "Cobranzas que vencen en la semana": 1,
            "ATRASADAS": 3,
            "Pagos parciales": 1,
            "Pagadas sin registrar cobro": 1,
            "Sin fecha de vencimiento": 1,
        }
        assert secciones[4].encabezado == "ATRASADAS (3)"

    def test_seccion_vacia_dice_sin_registros(self):
        secciones = por_titulo(armar_secciones(reporte(cumples=[cumple()])))

        assert secciones["Cumpleaños de hoy"].filas
        assert secciones["Cobranzas que vencen hoy"].filas == ()
        assert secciones["Cobranzas que vencen hoy"].vacia_texto == SIN_REGISTROS

    def test_sin_vencimiento_condicional(self):
        sin = [s.titulo for s in armar_secciones(reporte([cobranza(1)]))]
        con = [s.titulo for s in armar_secciones(reporte([cobranza(1, venc=None)]))]

        assert "Sin fecha de vencimiento" not in sin
        assert con[-1] == "Sin fecha de vencimiento"

    def test_totales_solo_en_4_secciones(self):
        secciones = por_titulo(armar_secciones(reporte_completo()))

        con_total = {t for t, s in secciones.items() if s.total is not None}

        assert con_total == {
            "Cobranzas que vencen hoy",
            "Cobranzas que vencen en la semana",
            "ATRASADAS",
            "Pagos parciales",
        }
        # ATRASADAS: 1.234.567,50 + 600 (parcial); excluye Sin monto y la pagada.
        assert secciones["ATRASADAS"].total == "1.235.167,50"
        assert secciones["Pagos parciales"].total == "600,00"
        assert secciones["Cobranzas que vencen en la semana"].total == "2.500,50"

    def test_seccion_vacia_no_lleva_total(self):
        # Vacía = solo "Sin registros"; un "Total saldo: 0,00" ahí es ruido.
        secciones = por_titulo(armar_secciones(reporte()))

        assert all(s.total is None for s in secciones.values())

    def test_seccion_vacia_no_lleva_total_aunque_otras_si(self):
        secciones = por_titulo(armar_secciones(reporte([cobranza(1, venc=JUEVES)])))

        assert secciones["Cobranzas que vencen hoy"].total == "1.000,00"
        assert secciones["ATRASADAS"].total is None
        assert secciones["Cobranzas que vencen en la semana"].total is None
        assert secciones["Pagos parciales"].total is None

    def test_sin_monto_en_monto_y_saldo(self):
        secciones = por_titulo(armar_secciones(reporte([cobranza(6, venc=date(2026, 10, 20), monto=None, pagado="100")])))
        atrasadas = secciones["ATRASADAS"]
        fila = dict(zip(atrasadas.columnas, atrasadas.filas[0]))

        assert fila["Monto"] == SIN_MONTO
        assert fila["Saldo"] == SIN_MONTO
        assert fila["Pagado"] == "100,00"
        assert atrasadas.total == "0,00"

    def test_columnas_por_tipo_de_seccion(self):
        secciones = por_titulo(armar_secciones(reporte_completo()))

        assert secciones["Cumpleaños de hoy"].columnas == ("Nombre", "Puesto", "Cliente", "Empresa", "Fecha")
        base = ("Cliente", "Empresa", "Pedido #", "Vencimiento", "Estado", "Monto", "Pagado", "NC", "Saldo")
        assert secciones["Cobranzas que vencen hoy"].columnas == base
        assert secciones["Sin fecha de vencimiento"].columnas == base
        assert "Días de atraso" in secciones["ATRASADAS"].columnas
        for titulo in ("Pagos parciales", "Pagadas sin registrar cobro"):
            assert "Estado" not in secciones[titulo].columnas
            assert "Saldo" in secciones[titulo].columnas

    def test_fila_atrasada_formateada(self):
        secciones = por_titulo(armar_secciones(reporte_completo()))
        atrasadas = secciones["ATRASADAS"]
        primera = dict(zip(atrasadas.columnas, atrasadas.filas[0]))

        assert primera == {
            "Cliente": "Muñoz “El Ñandú” — Pérez",
            "Empresa": "Empresa",
            "Pedido #": "3",
            "Vencimiento": "30/06/2026",
            "Días de atraso": "121",
            "Estado": "ATRASADO",
            "Monto": "1.234.567,50",
            "Pagado": "0,00",
            "NC": "0,00",
            "Saldo": "1.234.567,50",
        }

    def test_estado_legible_y_vencimiento_vacio(self):
        secciones = por_titulo(armar_secciones(reporte_completo()))
        semana = dict(zip(secciones["Cobranzas que vencen en la semana"].columnas, secciones["Cobranzas que vencen en la semana"].filas[0]))
        sin_venc = dict(zip(secciones["Sin fecha de vencimiento"].columnas, secciones["Sin fecha de vencimiento"].filas[0]))

        assert semana["Estado"] == "EN CAMINO"
        assert sin_venc["Vencimiento"] == ""
        assert sin_venc["Estado"] == "A COBRAR"

    def test_cumple_muestra_ocurrencia_dd_mm(self):
        secciones = por_titulo(armar_secciones(reporte_completo()))
        fila = dict(zip(secciones["Cumpleaños de la semana"].columnas, secciones["Cumpleaños de la semana"].filas[0]))

        assert fila == {"Nombre": "Beto", "Puesto": "Compras", "Cliente": "Cliente", "Empresa": "Empresa", "Fecha": "01/11"}

    def test_pagada_muestra_saldo_cero(self):
        secciones = por_titulo(armar_secciones(reporte_completo()))
        pagadas = secciones["Pagadas sin registrar cobro"]
        fila = dict(zip(pagadas.columnas, pagadas.filas[0]))

        assert fila["Saldo"] == "0,00"
        assert pagadas.total is None

    def test_columnas_de_plata_alineadas_a_la_derecha(self):
        for seccion in armar_secciones(reporte_completo()):
            alineacion = dict(zip(seccion.columnas, seccion.alineacion))
            for col in ("Monto", "Pagado", "NC", "Saldo"):
                if col in alineacion:
                    assert alineacion[col] == "RIGHT"


# ---------------------------------------------------------------- PDF


class TestRender:
    def test_pdf_vacio_sin_novedades(self):
        vacio = Reporte(hoy=JUEVES, domingo=DOMINGO)

        pdf = render_pdf(vacio, GENERADO)

        assert pdf.startswith(b"%PDF-")
        assert pdf.rstrip().endswith(b"%%EOF")
        assert paginas(pdf) == 1
        assert SIN_NOVEDADES == "Sin novedades"

    def test_pdf_completo_valido(self):
        pdf = render_pdf(reporte_completo(), GENERADO)

        assert pdf.startswith(b"%PDF-")
        assert paginas(pdf) >= 1

    def test_pdf_200_atrasadas_varias_paginas_sin_truncar(self):
        filas = [
            cobranza(i, venc=date(2026, 6, 30), monto="1000", cliente=f"Cliente {i:03d}", estado=ATRASADO)
            for i in range(1, 201)
        ]
        rep = reporte(filas)

        pdf = render_pdf(rep, GENERADO)

        assert len(rep.atrasadas) == 200
        assert len(por_titulo(armar_secciones(rep))["ATRASADAS"].filas) == 200
        assert paginas(pdf) > 3

    def test_pdf_caracteres_especiales_con_poppins_embebida(self):
        rep = reporte([cobranza(1, cliente="Muñoz “El Ñandú” — Pérez ¿¡ü…")])

        pdf = render_pdf(rep, GENERADO)

        assert b"Poppins" in pdf
        assert b"/FontFile2" in pdf  # TTF embebida (subset), no una fuente core

    def test_metadata(self):
        pdf = render_pdf(Reporte(hoy=JUEVES, domingo=DOMINGO), GENERADO)

        assert b"Arabia Representaciones" in pdf
        assert b"Sirio-Arabia CRM" in pdf

    def test_generado_en_naive_falla(self):
        with pytest.raises(ValueError):
            render_pdf(Reporte(hoy=JUEVES, domingo=DOMINGO), datetime(2026, 10, 29, 8, 0))


@pytest.mark.skipif(shutil.which("pdftotext") is None, reason="pdftotext (poppler) no instalado")
class TestTextoExtraido:
    def texto(self, pdf: bytes, tmp_path) -> str:
        ruta = tmp_path / "r.pdf"
        ruta.write_bytes(pdf)
        return subprocess.run(
            ["pdftotext", "-layout", str(ruta), "-"], capture_output=True, text=True, check=True
        ).stdout

    def test_vacio_dice_sin_novedades(self, tmp_path):
        texto = self.texto(render_pdf(Reporte(hoy=JUEVES, domingo=DOMINGO), GENERADO), tmp_path)

        assert "Sin novedades" in texto
        assert "Sin registros" in texto
        assert "Sin fecha de vencimiento" not in texto
        assert "Total saldo" not in texto

    def test_seccion_vacia_sin_linea_de_total(self, tmp_path):
        texto = self.texto(render_pdf(reporte([cobranza(1, venc=JUEVES)]), GENERADO), tmp_path)

        # Solo "vencen hoy" tiene filas: una única línea de total.
        assert texto.count("Total saldo") == 1

    def test_encabezado_y_pie(self, tmp_path):
        texto = self.texto(render_pdf(reporte_completo(), GENERADO), tmp_path)

        assert "jue 29/10/2026" in texto
        assert "dom 01/11/2026" in texto
        assert re.search(r"Página 1 de \d+", texto)
        assert "Generado 29/10/2026 08:03 ART" in texto

    def test_caracteres_especiales_identicos(self, tmp_path):
        texto = self.texto(render_pdf(reporte_completo(), GENERADO), tmp_path)

        assert "Muñoz “El Ñandú” — Pérez" in texto
        assert "Cumpleaños de hoy (1)" in texto
        assert "1.234.567,50" in texto
        assert "Sin monto" in texto

    def test_lista_larga_repite_encabezado_y_total_al_final(self, tmp_path):
        filas = [
            cobranza(i, venc=date(2026, 6, 30), monto="1000", cliente=f"Cliente {i:03d}", estado=ATRASADO)
            for i in range(1, 201)
        ]
        texto = self.texto(render_pdf(reporte(filas), GENERADO), tmp_path)
        paginas_txt = texto.split("\f")

        assert "Cliente 001" in texto and "Cliente 200" in texto
        con_encabezado = [p for p in paginas_txt if "Vencimiento" in p and "Saldo" in p]
        assert len(con_encabezado) > 3
        assert "200.000,00" in paginas_txt[-2] or "200.000,00" in paginas_txt[-1]
