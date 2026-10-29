from datetime import date, timedelta
from decimal import Decimal

import pytest

from backend.services.reporte_diario.clasificar import (
    clasificar_cobranzas,
    clasificar_cumples,
    componer_reporte,
    total_saldo,
    totales,
)
from backend.services.reporte_diario.fechas import fin_de_semana
from backend.services.reporte_diario.modelo import (
    A_COBRAR,
    ATRASADO,
    EN_CAMINO,
    FilaCobranza,
    FilaCumple,
    Reporte,
)
from backend.services.saldos import armar_saldo

D = Decimal
JUEVES = date(2026, 10, 29)
VIERNES = date(2026, 10, 30)
MIERCOLES = date(2026, 10, 28)

SECCIONES_COBRANZA = (
    "vencen_hoy",
    "vencen_semana",
    "atrasadas",
    "parciales",
    "pagadas_sin_registrar",
    "sin_vencimiento",
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


def clasificar(filas, hoy=JUEVES):
    return clasificar_cobranzas(filas, hoy, fin_de_semana(hoy))


def secciones_de(order_id, resultado):
    return {nombre for nombre in SECCIONES_COBRANZA if any(f.order_id == order_id for f in resultado[nombre])}


def cumple(nombre="Ana", fecha=date(1985, 10, 29)):
    return FilaCumple(nombre=nombre, puesto="Compras", cliente="Cliente", empresa="Empresa", fecha=fecha)


def cumples(filas, hoy):
    return clasificar_cumples(filas, hoy, fin_de_semana(hoy))


# ---------------------------------------------------------------- cobranzas


class TestBucketsPorVencimiento:
    def test_vence_hoy_solo_en_hoy(self):
        r = clasificar([cobranza(venc=JUEVES)])

        assert secciones_de(1, r) == {"vencen_hoy"}

    def test_vence_domingo_en_semana(self):
        r = clasificar([cobranza(venc=date(2026, 11, 1))])

        assert secciones_de(1, r) == {"vencen_semana"}

    def test_viernes_incluye_sabado_y_domingo(self):
        r = clasificar([cobranza(1, venc=date(2026, 10, 31)), cobranza(2, venc=date(2026, 11, 1))], hoy=VIERNES)

        assert [f.order_id for f in r["vencen_semana"]] == [1, 2]

    def test_fuera_de_ventana_ninguna_seccion(self):
        r = clasificar([cobranza(venc=date(2026, 11, 2))], hoy=VIERNES)

        assert secciones_de(1, r) == set()

    def test_atrasada_dias_y_orden(self):
        filas = [
            cobranza(1, venc=date(2026, 10, 28), cliente="Beta"),
            cobranza(2, venc=date(2026, 6, 30), cliente="Zeta"),
            cobranza(4, venc=date(2026, 10, 28), cliente="Alfa"),
            cobranza(3, venc=date(2026, 10, 28), cliente="Alfa"),
        ]

        atrasadas = clasificar(filas)["atrasadas"]

        assert [(f.order_id, f.dias_atraso) for f in atrasadas] == [(2, 121), (3, 1), (4, 1), (1, 1)]

    def test_sin_vencimiento(self):
        r = clasificar([cobranza(venc=None)])

        assert secciones_de(1, r) == {"sin_vencimiento"}

    def test_en_camino_en_ventana(self):
        r = clasificar([cobranza(venc=JUEVES, estado=EN_CAMINO), cobranza(2, venc=VIERNES, estado=EN_CAMINO)])

        assert r["vencen_hoy"][0].estado == EN_CAMINO
        assert r["vencen_semana"][0].estado == EN_CAMINO

    def test_solo_atrasadas_tienen_dias_de_atraso(self):
        r = clasificar([cobranza(1, venc=JUEVES), cobranza(2, venc=MIERCOLES, estado=ATRASADO)])

        assert r["vencen_hoy"][0].dias_atraso is None
        assert r["atrasadas"][0].dias_atraso == 1


class TestClasificacionExclusiva:
    def test_pagada_sin_registrar_vencida_no_en_atrasadas(self):
        deuda = cobranza(1, venc=date(2026, 10, 20))
        pagada = cobranza(2, venc=date(2026, 10, 15), pagado="1000")

        con = clasificar([deuda, pagada])
        sin = clasificar([deuda])

        assert secciones_de(2, con) == {"pagadas_sin_registrar"}
        assert total_saldo(con["atrasadas"]) == total_saldo(sin["atrasadas"]) == D("1000")

    def test_pagada_sin_registrar_hoy_y_domingo_solo_pagadas(self):
        hoy = cobranza(1, venc=JUEVES, monto="500", pagado="300", nc="200")
        domingo = cobranza(2, venc=date(2026, 11, 1), monto="500", pagado="300", nc="200")

        r = clasificar([hoy, domingo])

        assert secciones_de(1, r) == {"pagadas_sin_registrar"}
        assert secciones_de(2, r) == {"pagadas_sin_registrar"}

    def test_pagada_sin_registrar_sin_vencimiento_solo_pagadas(self):
        r = clasificar([cobranza(venc=None, pagado="1000")])

        assert secciones_de(1, r) == {"pagadas_sin_registrar"}

    def test_parcial_atrasado_en_atrasadas_y_parciales(self):
        r = clasificar([cobranza(venc=JUEVES - timedelta(days=5), pagado="400")])

        assert secciones_de(1, r) == {"atrasadas", "parciales"}
        assert r["atrasadas"][0].saldo_efectivo == r["parciales"][0].saldo_efectivo == D("600")

    def test_parcial_en_semana_tambien_en_parciales(self):
        r = clasificar([cobranza(venc=VIERNES, pagado="400")])

        assert secciones_de(1, r) == {"vencen_semana", "parciales"}

    def test_parcial_mas_alla_del_domingo_solo_parciales(self):
        r = clasificar([cobranza(venc=date(2026, 11, 20), pagado="400")])

        assert secciones_de(1, r) == {"parciales"}

    def test_solo_nc_es_parcial(self):
        r = clasificar([cobranza(venc=None, nc="200")])

        assert secciones_de(1, r) == {"sin_vencimiento", "parciales"}
        assert r["parciales"][0].saldo_efectivo == D("800")

    def test_sin_monto_en_su_bucket_fuera_de_totales(self):
        deuda = cobranza(1, venc=date(2026, 10, 20))
        sin_monto = cobranza(2, venc=date(2026, 10, 20), monto=None)

        r = clasificar([deuda, sin_monto])

        assert secciones_de(2, r) == {"atrasadas"}
        fila = next(f for f in r["atrasadas"] if f.order_id == 2)
        assert fila.sin_monto is True
        assert fila.saldo_efectivo is None
        assert total_saldo(r["atrasadas"]) == D("1000")

    def test_sin_monto_con_montos_en_cero_vence_hoy(self):
        r = clasificar([cobranza(venc=JUEVES, monto="0")])

        assert secciones_de(1, r) == {"vencen_hoy"}
        assert total_saldo(r["vencen_hoy"]) == D("0")

    def test_sin_monto_con_pago_no_es_pagada_ni_parcial(self):
        r = clasificar([cobranza(venc=VIERNES, monto="0", pagado="100")])

        assert secciones_de(1, r) == {"vencen_semana"}

    def test_cada_pedido_en_una_sola_seccion_por_vencimiento(self):
        filas = [
            cobranza(i, venc=v, pagado=p)
            for i, (v, p) in enumerate(
                [(JUEVES, "0"), (VIERNES, "10"), (MIERCOLES, "0"), (None, "0"), (date(2027, 1, 1), "10")],
                start=1,
            )
        ]

        r = clasificar(filas)

        for f in filas:
            por_venc = secciones_de(f.order_id, r) - {"parciales"}
            assert len(por_venc) <= 1


class TestOrdenYTotales:
    def test_orden_demas_cobranzas_null_al_final(self):
        filas = [
            cobranza(1, venc=None, pagado="10", cliente="Alfa"),
            cobranza(2, venc=date(2026, 11, 20), pagado="10", cliente="Zeta"),
            cobranza(3, venc=date(2026, 11, 20), pagado="10", cliente="Beta"),
            cobranza(4, venc=MIERCOLES, pagado="10", cliente="Zeta"),
        ]

        r = clasificar(filas)

        assert [f.order_id for f in r["parciales"]] == [4, 3, 2, 1]

    def test_orden_semana_por_vencimiento_y_cliente(self):
        filas = [
            cobranza(1, venc=date(2026, 11, 1), cliente="Alfa"),
            cobranza(2, venc=VIERNES, cliente="Zeta"),
            cobranza(3, venc=VIERNES, cliente="beta"),
        ]

        assert [f.order_id for f in clasificar(filas)["vencen_semana"]] == [3, 2, 1]

    def test_totales_solo_4_secciones(self):
        filas = [
            cobranza(1, venc=JUEVES, monto="100"),
            cobranza(2, venc=VIERNES, monto="200"),
            cobranza(3, venc=MIERCOLES, monto="300", pagado="50"),
            cobranza(4, venc=None, monto="400"),
            cobranza(5, venc=MIERCOLES, monto="500", pagado="500"),
            cobranza(6, venc=MIERCOLES, monto=None),
        ]
        reporte = componer_reporte(JUEVES, filas, [])

        assert totales(reporte) == {
            "vencen_hoy": D("100"),
            "vencen_semana": D("200"),
            "atrasadas": D("250"),
            "parciales": D("250"),
        }

    def test_total_saldo_redondeo_umbral_suma_cero(self):
        assert total_saldo([cobranza(venc=MIERCOLES, pagado="999.995")]) == D("0")


# ---------------------------------------------------------------- cumpleaños


class TestCumples:
    def test_cumple_hoy_no_en_semana(self):
        r = cumples([cumple(fecha=date(1985, 10, 29))], JUEVES)

        assert [c.ocurrencia for c in r["cumples_hoy"]] == [JUEVES]
        assert r["cumples_semana"] == ()

    def test_cumple_domingo_reporte_viernes(self):
        r = cumples([cumple(fecha=date(1990, 11, 1))], VIERNES)

        assert [c.ocurrencia for c in r["cumples_semana"]] == [date(2026, 11, 1)]

    def test_cumple_cruce_de_anio(self):
        r = cumples([cumple(fecha=date(1980, 1, 2))], date(2026, 12, 28))

        assert [c.ocurrencia for c in r["cumples_semana"]] == [date(2027, 1, 2)]

    def test_cumple_29_feb_2027(self):
        bisiesto = cumple(fecha=date(1992, 2, 29))

        semana = cumples([bisiesto], date(2027, 2, 26))
        hoy = cumples([bisiesto], date(2027, 2, 28))

        assert [c.ocurrencia for c in semana["cumples_semana"]] == [date(2027, 2, 28)]
        assert [c.ocurrencia for c in hoy["cumples_hoy"]] == [date(2027, 2, 28)]

    def test_cumple_29_feb_2028(self):
        r = cumples([cumple(fecha=date(1992, 2, 29))], date(2028, 2, 28))

        assert [c.ocurrencia for c in r["cumples_semana"]] == [date(2028, 2, 29)]

    def test_cumple_ya_pasado_no_aparece(self):
        r = cumples([cumple(fecha=date(1985, 10, 27))], MIERCOLES)

        assert r == {"cumples_hoy": (), "cumples_semana": ()}

    def test_cumple_sin_fecha_no_aparece(self):
        r = cumples([cumple(fecha=None)], JUEVES)

        assert r == {"cumples_hoy": (), "cumples_semana": ()}

    def test_orden_cumples_fecha_nombre(self):
        filas = [
            cumple("Zoe", date(1980, 11, 1)),
            cumple("Bruno", date(1980, 10, 31)),
            cumple("ana", date(1980, 11, 1)),
        ]

        r = cumples(filas, VIERNES)

        assert [c.nombre for c in r["cumples_semana"]] == ["Bruno", "ana", "Zoe"]


# ---------------------------------------------------------------- reporte


class TestReporte:
    def test_reporte_vacio_sin_novedades(self):
        reporte = componer_reporte(JUEVES, [], [])

        assert reporte.sin_novedades is True
        assert reporte.vacio is True
        assert reporte.domingo == date(2026, 11, 1)

    def test_solo_sin_vencimiento_no_es_sin_novedades(self):
        reporte = componer_reporte(JUEVES, [cobranza(venc=None)], [])

        assert reporte.sin_novedades is False

    def test_componer_reporte_llena_secciones(self):
        reporte = componer_reporte(JUEVES, [cobranza(venc=MIERCOLES)], [cumple(fecha=date(1985, 10, 29))])

        assert isinstance(reporte, Reporte)
        assert len(reporte.atrasadas) == 1
        assert len(reporte.cumples_hoy) == 1

    def test_reporte_es_inmutable(self):
        reporte = componer_reporte(JUEVES, [], [])

        with pytest.raises(AttributeError):
            reporte.hoy = VIERNES
