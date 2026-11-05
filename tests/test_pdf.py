"""PDF del reporte diario.

El contenido se verifica sobre `armar_secciones` (lo mismo que se dibuja,
ya formateado) y el PDF en sí con chequeos livianos sobre los bytes, sin
agregar dependencias para leer PDFs. Si `pdftotext` (poppler) está instalado
se verifica además el texto real del documento; si no, ese test se saltea.
"""
import re
import shutil
import subprocess
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from backend.services.reporte_diario.clasificar import componer_reporte
from backend.services.reporte_diario.fechas import EXTENDIDO
from backend.services.reporte_diario.modelo import (
    A_COBRAR,
    ATRASADO,
    EN_CAMINO,
    ComprasPar,
    DeudaPar,
    FilaCobranza,
    FilaCumple,
    FilaRegularidad,
    Reporte,
    TotalGeneral,
)
from backend.services.reporte_diario.pdf import (
    SIN_MONTO,
    SIN_NOVEDADES,
    SIN_REGISTROS,
    BloqueTotal,
    Seccion,
    armar_secciones,
    render_pdf,
    subtitulo,
    texto_ritmo,
    texto_total_general,
)
from backend.services.saldos import armar_saldo

D = Decimal
# Semana del lunes 09/11/2026: el lunes es el reporte EXTENDIDO (todas las secciones).
LUNES = date(2026, 11, 9)
MARTES = date(2026, 11, 10)
JUEVES = date(2026, 11, 12)
VIERNES = date(2026, 11, 13)
DOMINGO = date(2026, 11, 15)
# 11:03 UTC = 08:03 ART.
GENERADO = datetime(2026, 11, 9, 11, 3, tzinfo=timezone.utc)

MAS_ADELANTE = "Cobranzas que vencen más adelante (después del domingo 15/11)"
REGULARIDAD = "Regularidad de compra (clientes que se pasaron de su ritmo habitual)"
TITULOS_LUNES = (
    "Cumpleaños de hoy",
    "Cumpleaños de la semana",
    "Cobranzas que vencen hoy",
    "Cobranzas que vencen en la semana",
    "ATRASADAS",
    MAS_ADELANTE,
    "Sin fecha de vencimiento",
    "Pagos parciales",
    "Pagadas sin registrar cobro",
    "Total general de deuda vigente",
    REGULARIDAD,
)
TITULOS_DEL_DIA = (
    "Cumpleaños de hoy",
    "Cobranzas que vencen hoy",
    "ATRASADAS",
    "Pagadas sin registrar cobro",
    "Sin fecha de vencimiento",
)
TITULOS_VIERNES = (
    "Cumpleaños de hoy y del fin de semana",
    "Cobranzas que vencen hoy y el fin de semana",
    "ATRASADAS",
    "Pagadas sin registrar cobro",
    "Sin fecha de vencimiento",
)


def cobranza(order_id=1, venc=LUNES, monto="1000", pagado="0", nc="0", cliente="Cliente", estado=A_COBRAR):
    monto = None if monto is None else D(monto)
    return FilaCobranza(
        order_id=order_id,
        cliente=cliente,
        empresa="Empresa",
        vencimiento=venc,
        estado=estado,
        saldo=armar_saldo(monto, None, None, D(pagado), D(nc)),
    )


def cumple(nombre="Ana", fecha=date(1985, 11, 9)):
    return FilaCumple(nombre=nombre, puesto="Compras", cliente="Cliente", empresa="Empresa", fecha=fecha)


def reporte(cobranzas=(), cumples=(), hoy=LUNES):
    return componer_reporte(hoy, cobranzas, cumples)


def por_titulo(secciones):
    """Solo las tablas (Seccion), por título; el total general va aparte."""
    return {s.titulo: s for s in secciones if isinstance(s, Seccion)}


def bloque_total(secciones) -> BloqueTotal:
    (bloque,) = [s for s in secciones if isinstance(s, BloqueTotal)]
    return bloque


def paginas(pdf: bytes) -> int:
    return len(re.findall(rb"/Type\s*/Page\b(?!s)", pdf))


FILAS_COMPLETAS = (
    cobranza(1, venc=LUNES, monto="1000"),
    cobranza(2, venc=DOMINGO, monto="2500.5", estado=EN_CAMINO),
    cobranza(3, venc=date(2026, 6, 30), monto="1234567.5", estado=ATRASADO, cliente="Muñoz “El Ñandú” — Pérez"),
    cobranza(4, venc=date(2026, 11, 4), monto="1000", pagado="400", estado=ATRASADO),
    cobranza(5, venc=date(2026, 10, 26), monto="1000", pagado="1000", estado=ATRASADO),
    cobranza(6, venc=date(2026, 10, 31), monto=None, estado=ATRASADO),
    cobranza(7, venc=None, monto="300"),
    cobranza(8, venc=date(2026, 11, 30), monto="700"),
)
CUMPLES_COMPLETOS = (cumple("Ana"), cumple("Beto", date(1990, 11, 15)))


def reporte_completo(hoy=LUNES):
    return reporte(cobranzas=FILAS_COMPLETAS, cumples=CUMPLES_COMPLETOS, hoy=hoy)


# ---------------------------------------------------------------- secciones


class TestSecciones:
    def test_orden_y_titulos_con_conteo_lunes(self):
        secciones = armar_secciones(reporte_completo())

        assert [s.titulo for s in secciones] == list(TITULOS_LUNES)
        conteos = {s.titulo: s.conteo for s in secciones if isinstance(s, Seccion)}
        assert conteos == {
            "Cumpleaños de hoy": 1,
            "Cumpleaños de la semana": 1,
            "Cobranzas que vencen hoy": 1,
            "Cobranzas que vencen en la semana": 1,
            "ATRASADAS": 3,
            MAS_ADELANTE: 1,
            "Sin fecha de vencimiento": 1,
            "Pagos parciales": 1,
            "Pagadas sin registrar cobro": 1,
            REGULARIDAD: 0,
        }
        assert secciones[4].encabezado == "ATRASADAS (3)"

    def test_orden_y_titulos_martes(self):
        assert [s.titulo for s in armar_secciones(reporte_completo(MARTES))] == list(TITULOS_DEL_DIA)

    def test_orden_y_titulos_viernes(self):
        assert [s.titulo for s in armar_secciones(reporte_completo(VIERNES))] == list(TITULOS_VIERNES)

    def test_jueves_sin_secciones_del_lunes(self):
        titulos = " | ".join(s.titulo for s in armar_secciones(reporte_completo(JUEVES))).casefold()

        for prohibido in ("de la semana", "pagos parciales", "vencen más adelante", "total general", "regularidad"):
            assert prohibido not in titulos

    def test_pagadas_sin_registrar_siempre_aunque_este_vacia(self):
        for hoy in (LUNES, MARTES, VIERNES):
            secciones = por_titulo(armar_secciones(reporte(hoy=hoy)))

            assert secciones["Pagadas sin registrar cobro"].filas == ()
            assert "Sin fecha de vencimiento" not in secciones

    def test_seccion_vacia_dice_sin_registros(self):
        secciones = por_titulo(armar_secciones(reporte(cumples=[cumple()])))

        assert secciones["Cumpleaños de hoy"].filas
        assert secciones["Cobranzas que vencen hoy"].filas == ()
        assert secciones["Cobranzas que vencen hoy"].vacia_texto == SIN_REGISTROS

    def test_sin_vencimiento_condicional(self):
        sin = [s.titulo for s in armar_secciones(reporte([cobranza(1)]))]
        con = [s.titulo for s in armar_secciones(reporte([cobranza(1, venc=None)]))]
        con_martes = [s.titulo for s in armar_secciones(reporte([cobranza(1, venc=None)], hoy=MARTES))]

        assert "Sin fecha de vencimiento" not in sin
        assert con[con.index(MAS_ADELANTE) + 1] == "Sin fecha de vencimiento"
        assert con_martes[-1] == "Sin fecha de vencimiento"

    def test_totales_solo_en_5_secciones(self):
        secciones = por_titulo(armar_secciones(reporte_completo()))

        con_total = {t for t, s in secciones.items() if s.total is not None}

        assert con_total == {
            "Cobranzas que vencen hoy",
            "Cobranzas que vencen en la semana",
            "ATRASADAS",
            MAS_ADELANTE,
            "Pagos parciales",
        }
        # ATRASADAS: 1.234.567,50 + 600 (parcial); excluye Sin monto y la pagada.
        assert secciones["ATRASADAS"].total == "1.235.167,50"
        assert secciones["Pagos parciales"].total == "600,00"
        assert secciones["Cobranzas que vencen en la semana"].total == "2.500,50"
        assert secciones[MAS_ADELANTE].total == "700,00"

    def test_vencen_mas_adelante_columnas_de_vencen(self):
        secciones = por_titulo(armar_secciones(reporte_completo()))
        fila = dict(zip(secciones[MAS_ADELANTE].columnas, secciones[MAS_ADELANTE].filas[0]))

        assert secciones[MAS_ADELANTE].columnas == secciones["Cobranzas que vencen en la semana"].columnas
        assert (fila["Pedido #"], fila["Vencimiento"], fila["Saldo"]) == ("8", "30/11/2026", "700,00")

    def test_total_general_del_lunes(self):
        # 1.000 + 2.500,50 + 1.235.167,50 + 700 + 300; la Sin monto no suma pero se cuenta.
        bloque = bloque_total(armar_secciones(reporte_completo()))

        assert bloque.texto == (
            "Total general de deuda vigente: $1.239.668,00 (1 pedido sin monto cargado no incluido)"
        )

    def test_texto_total_general_con_y_sin_sin_monto(self):
        assert texto_total_general(TotalGeneral(D("3900"), 0)) == "Total general de deuda vigente: $3.900,00"
        assert texto_total_general(TotalGeneral(D("0"), 2)) == (
            "Total general de deuda vigente: $0,00 (2 pedidos sin monto cargado no incluidos)"
        )

    def test_total_general_solo_el_lunes(self):
        for hoy in (MARTES, JUEVES, VIERNES):
            assert not [s for s in armar_secciones(reporte_completo(hoy)) if isinstance(s, BloqueTotal)]

    def test_seccion_vacia_no_lleva_total(self):
        # Vacía = solo "Sin registros"; un "Total saldo: 0,00" ahí es ruido.
        secciones = por_titulo(armar_secciones(reporte()))

        assert all(s.total is None for s in secciones.values())

    def test_seccion_vacia_no_lleva_total_aunque_otras_si(self):
        secciones = por_titulo(armar_secciones(reporte([cobranza(1, venc=LUNES)])))

        assert secciones["Cobranzas que vencen hoy"].total == "1.000,00"
        assert secciones["ATRASADAS"].total is None
        assert secciones["Cobranzas que vencen en la semana"].total is None
        assert secciones[MAS_ADELANTE].total is None
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
            "Días de atraso": "132",
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

        assert fila == {"Nombre": "Beto", "Puesto": "Compras", "Cliente": "Cliente", "Empresa": "Empresa", "Fecha": "15/11"}

    def test_viernes_cumples_del_fin_de_semana(self):
        r = reporte(cumples=[cumple("Dom", date(1990, 11, 15)), cumple("Vie", date(1990, 11, 13))], hoy=VIERNES)
        seccion = por_titulo(armar_secciones(r))["Cumpleaños de hoy y del fin de semana"]

        assert [f[0] for f in seccion.filas] == ["Vie", "Dom"]
        assert [f[-1] for f in seccion.filas] == ["13/11", "15/11"]

    def test_pagada_muestra_saldo_cero(self):
        secciones = por_titulo(armar_secciones(reporte_completo()))
        pagadas = secciones["Pagadas sin registrar cobro"]
        fila = dict(zip(pagadas.columnas, pagadas.filas[0]))

        assert fila["Saldo"] == "0,00"
        assert pagadas.total is None

    def test_columnas_de_plata_alineadas_a_la_derecha(self):
        for seccion in por_titulo(armar_secciones(reporte_completo())).values():
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
            render_pdf(Reporte(hoy=JUEVES, domingo=DOMINGO), datetime(2026, 11, 12, 8, 0))

    @pytest.mark.parametrize("hoy", [LUNES, MARTES, VIERNES, date(2026, 11, 14)])
    def test_smoke_por_modo(self, hoy):
        pdf = render_pdf(reporte_completo(hoy), GENERADO)

        assert pdf.startswith(b"%PDF-")

    def test_lunes_vacio_sin_novedades_igual_dibuja_total(self):
        rep = reporte()

        assert rep.modo == EXTENDIDO and rep.sin_novedades
        assert render_pdf(rep, GENERADO).startswith(b"%PDF-")


def fila_regularidad(cliente="Cliente", n=27, p=14, deuda=DeudaPar()):
    return FilaRegularidad(
        cliente=cliente,
        empresa="Empresa (Marca)",
        ultima_compra=LUNES - timedelta(days=n),
        dias_sin_comprar=n,
        ritmo=p,
        compras=5,
        deuda=deuda,
    )


def compras_que_disparan(client_id=1, company_id=1, cliente="Cliente"):
    """Par con intervalos 14,14,14 y última compra hace 27 días (P=14, N=27)."""
    ultimo = LUNES - timedelta(days=27)
    dias = tuple(ultimo - timedelta(days=14 * k) for k in (3, 2, 1, 0))
    return ComprasPar(client_id=client_id, company_id=company_id, cliente=cliente, empresa="Empresa", dias=dias)


class TestRegularidad:
    def test_lunes_completo_11_secciones_con_regularidad_al_final(self):
        secciones = armar_secciones(replace(reporte_completo(), regularidad=(fila_regularidad(),)))

        assert len(secciones) == 11
        assert [s.titulo for s in secciones] == list(TITULOS_LUNES)
        assert isinstance(secciones[-2], BloqueTotal)
        assert secciones[-1].titulo == REGULARIDAD

    def test_regularidad_vacia_dice_sin_registros(self):
        regularidad = por_titulo(armar_secciones(reporte()))[REGULARIDAD]

        assert regularidad.filas == ()
        assert regularidad.vacia_texto == "Sin registros"
        assert regularidad.total is None

    def test_columnas_y_fila_con_texto_de_deuda_exacto(self):
        deuda = DeudaPar(total=D("1000"), sin_monto=2, abiertas=3)
        rep = replace(reporte(), regularidad=(fila_regularidad(deuda=deuda),))

        seccion = por_titulo(armar_secciones(rep))[REGULARIDAD]

        assert seccion.columnas == (
            "Cliente", "Empresa", "Última compra", "Días sin comprar", "Ritmo habitual", "Deuda",
        )
        assert seccion.anchos == (52, 38, 22, 18, 18, 72)
        assert seccion.filas == (
            (
                "Cliente",
                "Empresa (Marca)",
                "13/10/2026",
                "27",
                "cada 14 días",
                "(tiene deudas pendientes de $1.000,00 + 2 sin monto)",
            ),
        )

    def test_texto_ritmo(self):
        assert texto_ritmo(14) == "cada 14 días"
        assert texto_ritmo(1) == "cada 1 día"

    def test_componer_lunes_calcula_regularidad_con_deuda_del_par(self):
        deuda = FilaCobranza(
            order_id=9, cliente="Cliente", empresa="Empresa", vencimiento=date(2026, 12, 20), estado=EN_CAMINO,
            saldo=armar_saldo(D("500"), None, None, D(0), D(0)), client_id=1, company_id=1,
        )
        rep = componer_reporte(LUNES, [deuda], [], [compras_que_disparan()])

        (fila,) = por_titulo(armar_secciones(rep))[REGULARIDAD].filas

        assert fila[3:] == ("27", "cada 14 días", "(tiene deudas pendientes de $500,00)")

    @pytest.mark.parametrize("hoy", [MARTES, JUEVES, VIERNES])
    def test_sin_regularidad_de_martes_a_viernes(self, hoy):
        rep = componer_reporte(hoy, [], [], [compras_que_disparan()])

        assert rep.regularidad == ()
        assert REGULARIDAD not in [s.titulo for s in armar_secciones(rep)]

    def test_smoke_lunes_con_200_filas_de_regularidad(self):
        filas = tuple(fila_regularidad(cliente=f"Cliente {i:03d}") for i in range(1, 201))
        rep = replace(reporte(), regularidad=filas)

        pdf = render_pdf(rep, GENERADO)

        assert pdf.startswith(b"%PDF-")
        assert paginas(pdf) > 1
        assert len(por_titulo(armar_secciones(rep))[REGULARIDAD].filas) == 200


class TestSubtitulo:
    def test_martes_a_jueves_solo_la_fecha(self):
        assert subtitulo(reporte(hoy=MARTES)) == "mar 10/11/2026"
        assert subtitulo(reporte(hoy=JUEVES)) == "jue 12/11/2026"

    def test_viernes_incluye_sabado_y_domingo(self):
        assert subtitulo(reporte(hoy=VIERNES)) == "vie 13/11/2026 · incluye sáb y dom hasta dom 15/11/2026"

    def test_lunes_resumen_semanal(self):
        assert subtitulo(reporte(hoy=LUNES)) == "lun 09/11/2026 · resumen semanal hasta dom 15/11/2026"

    def test_reporte_sin_fin_hoy_es_del_dia(self):
        assert subtitulo(Reporte(hoy=JUEVES, domingo=DOMINGO)) == "jue 12/11/2026"


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

        assert "lun 09/11/2026 · resumen semanal hasta dom 15/11/2026" in texto
        assert re.search(r"Página 1 de \d+", texto)
        assert "Generado 09/11/2026 08:03 ART" in texto

    def test_jueves_sin_textos_del_lunes(self, tmp_path):
        texto = self.texto(render_pdf(reporte_completo(JUEVES), GENERADO), tmp_path)

        for prohibido in ("de la semana", "Pagos parciales", "más adelante", "Total general", "Regularidad"):
            assert prohibido not in texto
        assert "jue 12/11/2026" in texto

    def test_lunes_dibuja_total_general(self, tmp_path):
        texto = self.texto(render_pdf(reporte_completo(), GENERADO), tmp_path)

        assert "Total general de deuda vigente: $1.239.668,00" in texto
        assert "Cobranzas que vencen más adelante (después del domingo 15/11) (1)" in texto

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
        # El total de ATRASADAS queda en la misma página que su última fila.
        (ultima,) = [p for p in paginas_txt if "Cliente 200" in p]
        assert "200.000,00" in ultima
