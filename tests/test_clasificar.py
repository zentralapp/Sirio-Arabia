from datetime import date, timedelta
from decimal import Decimal

import pytest

from backend.services.reporte_diario.clasificar import (
    clasificar_cobranzas,
    clasificar_cumples,
    componer_reporte,
    total_general,
    total_saldo,
    totales,
)
from backend.services.reporte_diario.fechas import DEL_DIA, EXTENDIDO, fin_de_hoy, fin_de_semana
from backend.services.reporte_diario.modelo import (
    A_COBRAR,
    ATRASADO,
    EN_CAMINO,
    SECCIONES_POR_MODO,
    FilaCobranza,
    FilaCumple,
    Reporte,
    TotalGeneral,
)
from backend.services.saldos import armar_saldo

D = Decimal
JUEVES = date(2026, 10, 29)
VIERNES = date(2026, 10, 30)
MIERCOLES = date(2026, 10, 28)

# Semana del lunes 09/11/2026 (los escenarios de la spec reporte-regularidad).
LUN = date(2026, 11, 9)
MAR = date(2026, 11, 10)
MIE = date(2026, 11, 11)
VIE_ANT = date(2026, 11, 6)  # viernes anterior: cubre sáb 07 y dom 08
DOM = date(2026, 11, 15)

SECCIONES_COBRANZA = (
    "vencen_hoy",
    "vencen_semana",
    "atrasadas",
    "vencen_mas_adelante",
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


POR_VENCIMIENTO = ("vencen_hoy", "vencen_semana", "atrasadas", "vencen_mas_adelante", "sin_vencimiento")


def clasificar(filas, hoy=JUEVES):
    """La partición completa, con el "hoy" real del día (el viernes llega al domingo)."""
    return clasificar_cobranzas(filas, hoy, fin_de_semana(hoy), fin_hoy=fin_de_hoy(hoy))


def ids(filas):
    return [f.order_id for f in filas]


def en_reporte(order_id, reporte):
    """Secciones de cobranzas del Reporte (ya filtrado por modo) donde está el pedido."""
    return {n for n in SECCIONES_COBRANZA if any(f.order_id == order_id for f in getattr(reporte, n))}


def secciones_de(order_id, resultado):
    return {nombre for nombre in SECCIONES_COBRANZA if any(f.order_id == order_id for f in resultado[nombre])}


def cumple(nombre="Ana", fecha=date(1985, 10, 29)):
    return FilaCumple(nombre=nombre, puesto="Compras", cliente="Cliente", empresa="Empresa", fecha=fecha)


def cumples(filas, hoy):
    return clasificar_cumples(filas, hoy, fin_de_semana(hoy), fin_hoy=fin_de_hoy(hoy))


# ---------------------------------------------------------------- cobranzas


class TestBucketsPorVencimiento:
    def test_vence_hoy_solo_en_hoy(self):
        r = clasificar([cobranza(venc=JUEVES)])

        assert secciones_de(1, r) == {"vencen_hoy"}

    def test_vence_domingo_en_semana(self):
        r = clasificar([cobranza(venc=date(2026, 11, 1))])

        assert secciones_de(1, r) == {"vencen_semana"}

    def test_lunes_semana_incluye_sabado_y_domingo(self):
        r = clasificar([cobranza(1, venc=date(2026, 11, 14)), cobranza(2, venc=DOM)], hoy=LUN)

        assert ids(r["vencen_semana"]) == [1, 2]

    def test_despues_del_domingo_va_a_vencen_mas_adelante(self):
        r = clasificar([cobranza(venc=date(2026, 11, 16))], hoy=LUN)

        assert secciones_de(1, r) == {"vencen_mas_adelante"}

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

    def test_parcial_mas_alla_del_domingo_en_mas_adelante_y_parciales(self):
        r = clasificar([cobranza(venc=date(2026, 11, 20), pagado="400")])

        assert secciones_de(1, r) == {"vencen_mas_adelante", "parciales"}

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
            assert len(por_venc) == 1


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

    def test_totales_solo_5_secciones(self):
        # Lunes: el único día con semana, más adelante y parciales.
        filas = [
            cobranza(1, venc=LUN, monto="100"),
            cobranza(2, venc=MAR, monto="200"),
            cobranza(3, venc=VIE_ANT, monto="300", pagado="50"),
            cobranza(4, venc=None, monto="400"),
            cobranza(5, venc=VIE_ANT, monto="500", pagado="500"),
            cobranza(6, venc=VIE_ANT, monto=None),
            cobranza(7, venc=date(2026, 11, 30), monto="700"),
        ]
        reporte = componer_reporte(LUN, filas, [])

        assert totales(reporte) == {
            "vencen_hoy": D("100"),
            "vencen_semana": D("200"),
            "atrasadas": D("250"),
            "vencen_mas_adelante": D("700"),
            "parciales": D("250"),
        }

    def test_totales_del_jueves_sin_secciones_de_la_semana(self):
        filas = [cobranza(1, venc=JUEVES, monto="100"), cobranza(2, venc=VIERNES, monto="200", pagado="50")]
        reporte = componer_reporte(JUEVES, filas, [])

        assert totales(reporte) == {
            "vencen_hoy": D("100"),
            "vencen_semana": D("0"),
            "atrasadas": D("0"),
            "vencen_mas_adelante": D("0"),
            "parciales": D("0"),
        }

    def test_total_saldo_redondeo_umbral_suma_cero(self):
        assert total_saldo([cobranza(venc=MIERCOLES, pagado="999.995")]) == D("0")


# ---------------------------------------------------------------- cumpleaños


class TestCumples:
    def test_cumple_hoy_no_en_semana(self):
        r = cumples([cumple(fecha=date(1985, 10, 29))], JUEVES)

        assert [c.ocurrencia for c in r["cumples_hoy"]] == [JUEVES]
        assert r["cumples_semana"] == ()

    def test_cumple_domingo_reporte_viernes_va_a_hoy(self):
        r = cumples([cumple(fecha=date(1990, 11, 1))], VIERNES)

        assert [c.ocurrencia for c in r["cumples_hoy"]] == [date(2026, 11, 1)]
        assert r["cumples_semana"] == ()

    def test_cumple_domingo_reporte_lunes_en_semana(self):
        r = cumples([cumple(fecha=date(1990, 11, 15))], LUN)

        assert [c.ocurrencia for c in r["cumples_semana"]] == [DOM]

    def test_cumple_cruce_de_anio(self):
        r = cumples([cumple(fecha=date(1980, 1, 2))], date(2026, 12, 28))

        assert [c.ocurrencia for c in r["cumples_semana"]] == [date(2027, 1, 2)]

    def test_cumple_29_feb_2027(self):
        bisiesto = cumple(fecha=date(1992, 2, 29))

        semana = cumples([bisiesto], date(2027, 2, 22))  # lunes
        viernes = cumples([bisiesto], date(2027, 2, 26))  # "hoy" llega al dom 28/02
        hoy = cumples([bisiesto], date(2027, 2, 28))

        assert [c.ocurrencia for c in semana["cumples_semana"]] == [date(2027, 2, 28)]
        assert [c.ocurrencia for c in viernes["cumples_hoy"]] == [date(2027, 2, 28)]
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

        assert [c.nombre for c in r["cumples_hoy"]] == ["Bruno", "ana", "Zoe"]


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


# ---------------------------------------------------------------- por modo (reporte-regularidad)


def ocurrencias(filas):
    return [c.ocurrencia for c in filas]


class TestModoDelReporte:
    def test_modo_y_fin_hoy(self):
        lunes = componer_reporte(LUN, [], [])
        martes = componer_reporte(MAR, [], [])
        viernes = componer_reporte(VIE_ANT, [], [])

        assert (lunes.modo, lunes.fin_hoy, lunes.domingo) == (EXTENDIDO, LUN, DOM)
        assert (martes.modo, martes.fin_hoy) == (DEL_DIA, MAR)
        assert (viernes.modo, viernes.fin_hoy) == (DEL_DIA, date(2026, 11, 8))

    def test_secciones_por_modo(self):
        assert len(Reporte(hoy=MAR, domingo=DOM).secciones) == 5
        assert len(Reporte(hoy=LUN, domingo=DOM, modo=EXTENDIDO).secciones) == 10
        assert SECCIONES_POR_MODO[EXTENDIDO][-1] == "regularidad"

    def test_reporte_sin_modo_explicito_es_del_dia(self):
        # Los constructores de #19 (sin modo) siguen valiendo.
        assert Reporte(hoy=JUEVES, domingo=date(2026, 11, 1)).modo == DEL_DIA


class TestCumplesPorModo:
    def test_martes_no_muestra_la_semana(self):
        r = componer_reporte(MAR, [], [cumple("Hoy", date(1985, 11, 10)), cumple("Jue", date(1985, 11, 12))])

        assert [c.nombre for c in r.cumples_hoy] == ["Hoy"]
        assert r.cumples_semana == ()

    def test_viernes_cuando_el_sabado_o_el_domingo_es_cumpleanos(self):
        filas = [cumple("Dom", date(1980, 11, 8)), cumple("Vie", date(1980, 11, 6)), cumple("Sab", date(1980, 11, 7))]

        r = componer_reporte(VIE_ANT, [], filas)

        assert [c.nombre for c in r.cumples_hoy] == ["Vie", "Sab", "Dom"]
        assert ocurrencias(r.cumples_hoy) == [date(2026, 11, 6), date(2026, 11, 7), date(2026, 11, 8)]
        assert r.cumples_semana == ()

    def test_nada_se_pierde_entre_el_viernes_y_el_lunes(self):
        sabado = [cumple("Sab", date(1980, 11, 7))]

        viernes = componer_reporte(VIE_ANT, [], sabado)
        lunes = componer_reporte(LUN, [], sabado)

        assert [c.nombre for c in viernes.cumples_hoy] == ["Sab"]
        assert lunes.cumples_hoy == () and lunes.cumples_semana == ()

    def test_lunes_con_hoy_y_semana(self):
        r = componer_reporte(LUN, [], [cumple("Lun", date(1980, 11, 9)), cumple("Dom", date(1980, 11, 15))])

        assert [c.nombre for c in r.cumples_hoy] == ["Lun"]
        assert [c.nombre for c in r.cumples_semana] == ["Dom"]

    def test_lunes_primero_de_mes(self):
        filas = [cumple("Ene", date(1980, 1, 31)), cumple("Feb", date(1980, 2, 5))]

        lunes = componer_reporte(date(2027, 2, 1), [], filas)
        viernes = componer_reporte(date(2027, 1, 29), [], filas)

        assert [c.nombre for c in lunes.cumples_semana] == ["Feb"]
        assert lunes.cumples_hoy == ()
        assert [c.nombre for c in viernes.cumples_hoy] == ["Ene"]


@pytest.mark.parametrize("lunes", [LUN, date(2026, 12, 28), date(2027, 2, 22), date(2028, 2, 28)])
def test_cobertura_semanal_completa(lunes):
    """Cada día lun..dom de la semana aparece en "hoy" de algún reporte lun..vie."""
    reportes_habiles = [lunes + timedelta(days=n) for n in range(5)]
    for offset in range(7):
        dia = lunes + timedelta(days=offset)
        filas_cumple = [cumple("X", date(1980, dia.month, dia.day))]
        filas_cobro = [cobranza(1, venc=dia)]
        en_hoy_cumple = [h for h in reportes_habiles if ocurrencias(componer_reporte(h, [], filas_cumple).cumples_hoy)]
        en_hoy_cobro = [h for h in reportes_habiles if ids(componer_reporte(h, filas_cobro, []).vencen_hoy)]

        assert en_hoy_cumple, f"el cumpleaños del {dia} no sale en ningún 'hoy'"
        assert en_hoy_cobro, f"el vencimiento del {dia} no sale en ningún 'hoy'"


class TestCobranzasPorModo:
    def test_martes(self):
        filas = [
            cobranza(1, venc=MAR),
            cobranza(2, venc=date(2026, 11, 5)),
            cobranza(3, venc=date(2026, 11, 12)),
            cobranza(4, venc=None),
        ]

        r = componer_reporte(MAR, filas, [])

        assert ids(r.vencen_hoy) == [1]
        assert [(f.order_id, f.dias_atraso) for f in r.atrasadas] == [(2, 5)]
        assert ids(r.sin_vencimiento) == [4]
        assert en_reporte(3, r) == set()

    def test_viernes_incluye_vencimientos_de_sabado_y_domingo(self):
        filas = [cobranza(i, venc=date(2026, 11, d)) for i, d in ((1, 6), (2, 7), (3, 8), (4, 9))]

        r = componer_reporte(VIE_ANT, filas, [])

        assert ids(r.vencen_hoy) == [1, 2, 3]
        assert en_reporte(4, r) == set()

    def test_dias_de_atraso_del_viernes(self):
        r = componer_reporte(VIE_ANT, [cobranza(1, venc=date(2026, 11, 5))], [])

        assert [(f.order_id, f.dias_atraso) for f in r.atrasadas] == [(1, 1)]

    def test_vencimiento_del_domingo_visto_el_lunes(self):
        domingo = [cobranza(1, venc=date(2026, 11, 8))]

        viernes = componer_reporte(VIE_ANT, domingo, [])
        lunes = componer_reporte(LUN, domingo, [])

        assert ids(viernes.vencen_hoy) == [1]
        assert [(f.order_id, f.dias_atraso) for f in lunes.atrasadas] == [(1, 1)]

    def test_parcial_de_martes_a_viernes(self):
        r = componer_reporte(MIE, [cobranza(1, venc=date(2026, 11, 2), monto="1000", pagado="400")], [])

        assert [f.saldo_efectivo for f in r.atrasadas] == [D("600")]
        assert r.parciales == ()

    @pytest.mark.parametrize("hoy", [MAR, VIE_ANT, LUN])
    def test_pagadas_sin_registrar_todos_los_dias(self, hoy):
        r = componer_reporte(hoy, [cobranza(1, venc=date(2026, 11, 2), pagado="1000")], [])

        assert en_reporte(1, r) == {"pagadas_sin_registrar"}

    def test_particion_del_lunes(self):
        filas = [
            cobranza(1, venc=LUN),
            cobranza(2, venc=date(2026, 11, 12)),
            cobranza(3, venc=date(2026, 11, 1)),
            cobranza(4, venc=date(2026, 11, 30)),
            cobranza(5, venc=None),
            cobranza(6, venc=date(2026, 11, 2), monto=None),
        ]

        r = componer_reporte(LUN, filas, [])

        assert {f.order_id: en_reporte(f.order_id, r) for f in filas} == {
            1: {"vencen_hoy"},
            2: {"vencen_semana"},
            3: {"atrasadas"},
            4: {"vencen_mas_adelante"},
            5: {"sin_vencimiento"},
            6: {"atrasadas"},
        }
        union = [i for n in POR_VENCIMIENTO for i in ids(getattr(r, n))]
        assert sorted(union) == [1, 2, 3, 4, 5, 6]

    def test_particion_del_lunes_es_exhaustiva_y_sin_duplicados(self):
        # Propiedad: un pedido por día de -20..+60 alrededor del lunes, más
        # sin vencimiento, parciales, Sin monto y pagadas sin registrar.
        filas = [cobranza(i, venc=LUN + timedelta(days=i - 20), pagado="10" if i % 3 else "0") for i in range(81)]
        filas += [cobranza(100, venc=None), cobranza(101, venc=LUN, monto=None), cobranza(102, venc=DOM, pagado="1000")]

        r = componer_reporte(LUN, filas, [])

        union = [i for n in (*POR_VENCIMIENTO, "pagadas_sin_registrar") for i in ids(getattr(r, n))]
        assert len(union) == len(set(union))
        assert set(union) == {f.order_id for f in filas}

    def test_parcial_con_vencimiento_lejano_el_lunes(self):
        r = componer_reporte(LUN, [cobranza(1, venc=date(2026, 11, 30), pagado="400")], [])

        assert en_reporte(1, r) == {"vencen_mas_adelante", "parciales"}

    def test_vencimiento_a_fin_de_mes(self):
        r = componer_reporte(LUN, [cobranza(1, venc=date(2026, 11, 30), monto="1000")], [])

        assert [f.saldo_efectivo for f in r.vencen_mas_adelante] == [D("1000")]
        assert totales(r)["vencen_mas_adelante"] == D("1000")

    def test_en_camino_con_vencimiento_derivado_lejano(self):
        r = componer_reporte(LUN, [cobranza(1, venc=date(2026, 12, 20), estado=EN_CAMINO)], [])

        assert [f.estado for f in r.vencen_mas_adelante] == [EN_CAMINO]

    def test_vencen_mas_adelante_ordenado_por_vencimiento_y_cliente(self):
        filas = [
            cobranza(1, venc=date(2026, 12, 1), cliente="Alfa"),
            cobranza(2, venc=date(2026, 11, 20), cliente="Zeta"),
            cobranza(3, venc=date(2026, 11, 20), cliente="beta"),
        ]

        assert ids(componer_reporte(LUN, filas, []).vencen_mas_adelante) == [3, 2, 1]

    @pytest.mark.parametrize("hoy", [MAR, VIE_ANT])
    def test_del_dia_vacia_semana_mas_adelante_y_parciales(self, hoy):
        filas = [
            cobranza(1, venc=hoy + timedelta(days=30), pagado="100"),
            cobranza(2, venc=fin_de_semana(hoy), pagado="100"),
        ]
        cumples_ = [cumple("Dom", fin_de_semana(hoy))]

        r = componer_reporte(hoy, filas, cumples_)

        assert r.vencen_semana == r.vencen_mas_adelante == r.parciales == r.cumples_semana == ()


class TestTotalGeneral:
    def test_total_sin_duplicar_ni_sumar_excluidas(self):
        filas = [
            cobranza(1, venc=LUN, monto="1000"),
            cobranza(2, venc=date(2026, 11, 12), monto="500"),
            cobranza(3, venc=date(2026, 11, 1), monto="1400"),
            cobranza(4, venc=date(2026, 11, 2), monto="1000", pagado="400"),  # parcial, saldo 600
            cobranza(5, venc=date(2026, 11, 30), monto="300"),
            cobranza(6, venc=None, monto="100"),
            cobranza(7, venc=date(2026, 11, 3), monto=None),  # Sin monto atrasada
            cobranza(8, venc=date(2026, 11, 3), monto="500", pagado="500"),  # pagada sin registrar
        ]

        r = componer_reporte(LUN, filas, [])

        assert total_saldo(r.atrasadas) == D("2000")
        assert total_general(r) == TotalGeneral(monto=D("3900"), sin_monto=1)

    def test_solo_sin_monto(self):
        r = componer_reporte(LUN, [cobranza(1, venc=date(2026, 11, 3), monto=None)], [])

        assert total_general(r) == TotalGeneral(monto=D("0"), sin_monto=1)

    def test_invariante_total_general_igual_suma_de_secciones(self):
        filas = [cobranza(i, venc=LUN + timedelta(days=i - 15), monto=str(100 * i), pagado="50" if i % 4 == 0 else "0")
                 for i in range(1, 40)]
        filas += [cobranza(50, venc=None, monto="77.5"), cobranza(51, venc=None, monto=None)]

        r = componer_reporte(LUN, filas, [])
        t = totales(r)

        esperado = (
            t["vencen_hoy"] + t["vencen_semana"] + t["atrasadas"] + t["vencen_mas_adelante"]
            + total_saldo(r.sin_vencimiento)
        )
        assert total_general(r).monto == esperado


class TestSinNovedadesPorModo:
    def test_martes_con_deuda_solo_futura(self):
        r = componer_reporte(MAR, [cobranza(1, venc=date(2026, 11, 13))], [])

        assert r.sin_novedades is True

    def test_lunes_con_solo_vencen_mas_adelante_no_es_sin_novedades(self):
        r = componer_reporte(LUN, [cobranza(1, venc=date(2026, 11, 30))], [])

        assert r.sin_novedades is False

    def test_jueves_con_solo_cumple_de_la_semana_es_sin_novedades(self):
        r = componer_reporte(date(2026, 11, 12), [], [cumple("Sab", date(1980, 11, 14))])

        assert r.sin_novedades is True
