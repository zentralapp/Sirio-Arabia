"""Regularidad de compra (puro): ritmo, disparo, orden y texto de deuda.

Escenarios de la spec reporte-regularidad K. Los bordes usan números
literales A PROPÓSITO: si alguien cambia un umbral provisional, estos tests
tienen que romper (además de test_umbrales_provisionales).
"""
from datetime import date, timedelta
from decimal import Decimal

import pytest

from backend.services.reporte_diario import regularidad
from backend.services.reporte_diario.modelo import A_COBRAR, EN_CAMINO, ComprasPar, DeudaPar, FilaCobranza
from backend.services.reporte_diario.regularidad import (
    calcular_regularidad,
    deuda_por_par,
    dispara,
    intervalos,
    mediana_half_up,
    ritmo,
    texto_deuda,
)
from backend.services.saldos import armar_saldo

D = Decimal
LUNES = date(2026, 11, 9)


def dias_con_intervalos(*saltos, ultimo=LUNES - timedelta(days=27)):
    """Días ascendentes que terminan en `ultimo` con esos intervalos (del más viejo al más nuevo)."""
    dias = [ultimo]
    for salto in reversed(saltos):
        dias.insert(0, dias[0] - timedelta(days=salto))
    return tuple(dias)


def par(cliente="Cliente", empresa="Empresa", dias=(), client_id=1, company_id=1):
    return ComprasPar(client_id=client_id, company_id=company_id, cliente=cliente, empresa=empresa, dias=tuple(dias))


def par_con(n, p, cliente="Cliente", empresa="Empresa", client_id=1, company_id=1):
    """Par con intervalos (p, p) cuyo último día de compra fue hace n días."""
    ultimo = LUNES - timedelta(days=n)
    return par(cliente, empresa, dias_con_intervalos(p, p, ultimo=ultimo), client_id, company_id)


def cobranza(order_id=1, monto="1000", pagado="0", client_id=1, company_id=1, estado=A_COBRAR):
    monto = None if monto is None else D(monto)
    return FilaCobranza(
        order_id=order_id,
        cliente="Cliente",
        empresa="Empresa",
        vencimiento=LUNES,
        estado=estado,
        saldo=armar_saldo(monto, None, None, D(pagado), D(0)),
        client_id=client_id,
        company_id=company_id,
    )


# ---------------------------------------------------------------- umbrales


def test_umbrales_provisionales():
    assert regularidad.MIN_DIAS == 3
    assert regularidad.MAX_INTERVALOS == 12
    assert regularidad.FACTOR_TOPE == 3
    assert regularidad.TOPE_DIAS == 365


def test_datos_importa_el_umbral_y_no_lo_repite():
    from pathlib import Path

    fuente = (Path(regularidad.__file__).parent / "datos.py").read_text(encoding="utf-8")

    assert "from backend.services.reporte_diario.regularidad import MAX_INTERVALOS" in fuente
    assert "rn <= MAX_INTERVALOS + 1" in fuente
    assert " 13" not in fuente and "<= 12" not in fuente


# ---------------------------------------------------------------- mediana y P


@pytest.mark.parametrize(
    ("xs", "esperado"),
    [
        ([14, 14, 30], 14),  # impar
        ([10, 11], 11),  # 10,5 -> 11 (half-up)
        ([7, 8, 30, 31], 19),  # par entera
        ([7, 7, 7, 200], 7),  # robusta a un outlier
        ([15, 16], 16),  # 15,5 -> 16 (round() bancario daría 16 también)
        ([2, 3], 3),  # 2,5 -> 3 (round() bancario daría 2)
        ([1, 1, 1], 1),
    ],
)
def test_mediana_half_up(xs, esperado):
    assert mediana_half_up(xs) == esperado


def test_mediana_vacia_falla():
    with pytest.raises(ValueError):
        mediana_half_up([])


def test_intervalos():
    assert intervalos([date(2026, 9, 1), date(2026, 9, 15), date(2026, 10, 1)]) == [14, 16]


@pytest.mark.parametrize(
    ("saltos", "esperado"),
    [
        ((14, 14, 30), 14),
        ((10, 11), 11),
        ((7, 8, 30, 31), 19),
        ((7, 7, 7, 200), 7),
        ((1, 1, 1), 1),
    ],
)
def test_ritmo_escenarios_de_la_spec(saltos, esperado):
    assert ritmo(dias_con_intervalos(*saltos)) == esperado


def test_ritmo_menos_de_3_dias_distintos_es_none():
    assert ritmo([date(2026, 1, 1), date(2026, 2, 1)]) is None
    assert ritmo([date(2026, 1, 1)]) is None
    assert ritmo([]) is None


def test_ritmo_cuenta_dias_distintos():
    # 3 pedidos el 28/10 y 1 el 14/10: son 2 días, no 4; no califica.
    assert ritmo([date(2026, 10, 14), date(2026, 10, 28), date(2026, 10, 28), date(2026, 10, 28)]) is None


def test_ventana_de_12_intervalos_da_30_y_no_16():
    saltos = (2,) * 7 + (30,) * 7  # 15 días, 14 intervalos
    dias = dias_con_intervalos(*saltos)

    assert len(dias) == 15
    assert mediana_half_up(intervalos(dias)) == 16  # sin ventana
    assert ritmo(dias) == 30  # últimos 12: 2,2,2,2,2,30,30,30,30,30,30,30


# ---------------------------------------------------------------- disparo


@pytest.mark.parametrize(
    ("n", "p", "esperado"),
    [
        (27, 14, True),
        (14, 14, True),  # límite inferior inclusivo
        (13, 14, False),
        (42, 14, True),  # 3P inclusivo
        (43, 14, False),
        (365, 200, True),  # techo 365 inclusivo
        (366, 200, False),
        (0, 1, False),  # compró hoy
        (1, 1, True),
        (3, 1, True),
        (4, 1, False),
    ],
)
def test_dispara(n, p, esperado):
    assert dispara(n, p) is esperado


def test_compro_hoy_no_aparece():
    dias = dias_con_intervalos(7, 7, ultimo=LUNES)

    assert calcular_regularidad([par(dias=dias)], LUNES, {}) == ()


def test_dia_retrofechado_en_el_futuro_no_cuenta():
    dias = (date(2026, 9, 1), date(2026, 9, 15), date(2026, 9, 29), date(2026, 10, 13), date(2026, 11, 20))

    (fila,) = calcular_regularidad([par(dias=dias)], LUNES, {})

    assert (fila.ultima_compra, fila.dias_sin_comprar, fila.ritmo) == (date(2026, 10, 13), 27, 14)
    assert fila.compras == 4


def test_par_con_menos_de_3_dias_no_aparece_aunque_n_sea_grande():
    dias = (date(2026, 1, 1), date(2026, 2, 1))

    assert calcular_regularidad([par(dias=dias)], LUNES, {}) == ()


# ---------------------------------------------------------------- orden y contenido


def test_orden_por_n_sobre_p():
    pares = [
        par_con(27, 14, cliente="A", client_id=1),
        par_con(60, 30, cliente="B", client_id=2),
        par_con(10, 10, cliente="C", client_id=3),
    ]

    assert [f.cliente for f in calcular_regularidad(pares, LUNES, {})] == ["B", "A", "C"]


def test_empate_de_cociente_por_n_descendente():
    pares = [par_con(20, 10, cliente="D", client_id=1), par_con(40, 20, cliente="E", client_id=2)]

    assert [f.cliente for f in calcular_regularidad(pares, LUNES, {})] == ["E", "D"]


def test_empate_total_por_cliente_y_empresa():
    pares = [
        par_con(20, 10, cliente="beta", empresa="Z", client_id=2),
        par_con(20, 10, cliente="Alfa", empresa="Y", client_id=1, company_id=2),
        par_con(20, 10, cliente="Alfa", empresa="x", client_id=1, company_id=1),
    ]

    filas = calcular_regularidad(pares, LUNES, {})

    assert [(f.cliente, f.empresa) for f in filas] == [("Alfa", "x"), ("Alfa", "Y"), ("beta", "Z")]


def test_fila_trae_la_deuda_de_su_par():
    deudas = {(1, 1): DeudaPar(total=D("500"), abiertas=1)}

    (fila,) = calcular_regularidad([par_con(27, 14)], LUNES, deudas)

    assert fila.deuda == DeudaPar(total=D("500"), abiertas=1)
    assert (fila.dias_sin_comprar, fila.ritmo, fila.compras) == (27, 14, 3)


def test_par_sin_deuda_usa_deuda_vacia():
    (fila,) = calcular_regularidad([par_con(27, 14)], LUNES, {})

    assert fila.deuda == DeudaPar()


# ---------------------------------------------------------------- deuda del par


def test_deuda_con_monto():
    deudas = deuda_por_par([cobranza(1, "1000"), cobranza(2, "234.56")])

    assert deudas[(1, 1)] == DeudaPar(total=D("1234.56"), sin_monto=0, abiertas=2)
    assert texto_deuda(deudas[(1, 1)]) == "(tiene deudas pendientes de $1.234,56)"


def test_deuda_solo_sin_monto():
    deudas = deuda_por_par([cobranza(1, None)])

    assert texto_deuda(deudas[(1, 1)]) == "(tiene deudas pendientes sin monto cargado)"


def test_deuda_mixta():
    deudas = deuda_por_par([cobranza(1, "1000"), cobranza(2, None), cobranza(3, None)])

    assert texto_deuda(deudas[(1, 1)]) == "(tiene deudas pendientes de $1.000,00 + 2 sin monto)"


def test_sin_deuda_pagadas_sin_registrar_no_cuentan():
    deudas = deuda_por_par([cobranza(1, "1000", pagado="1000")])

    assert (1, 1) not in deudas
    assert texto_deuda(deudas.get((1, 1), DeudaPar())) == "(no tiene deudas actuales)"


def test_deuda_en_camino_no_vencida_cuenta():
    deudas = deuda_por_par([cobranza(1, "500", estado=EN_CAMINO)])

    assert texto_deuda(deudas[(1, 1)]) == "(tiene deudas pendientes de $500,00)"


def test_deuda_de_otra_empresa_no_cuenta():
    deudas = deuda_por_par([cobranza(1, "800", company_id=2)])

    assert texto_deuda(deudas.get((1, 1), DeudaPar())) == "(no tiene deudas actuales)"
    assert texto_deuda(deudas[(1, 2)]) == "(tiene deudas pendientes de $800,00)"


def test_parcial_cuenta_su_saldo():
    deudas = deuda_por_par([cobranza(1, "1000", pagado="400")])

    assert deudas[(1, 1)].total == D("600")


@pytest.mark.parametrize(
    ("deuda", "texto"),
    [
        (DeudaPar(), "(no tiene deudas actuales)"),
        (DeudaPar(total=D(0), sin_monto=0, abiertas=1), "(no tiene deudas actuales)"),
        (DeudaPar(total=D("1234.56"), abiertas=2), "(tiene deudas pendientes de $1.234,56)"),
        (DeudaPar(sin_monto=1, abiertas=1), "(tiene deudas pendientes sin monto cargado)"),
        (DeudaPar(total=D("1000"), sin_monto=2, abiertas=3), "(tiene deudas pendientes de $1.000,00 + 2 sin monto)"),
    ],
)
def test_texto_deuda_variantes(deuda, texto):
    assert texto_deuda(deuda) == texto
