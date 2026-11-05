from decimal import Decimal

import pytest

from backend.services.saldos import (
    UMBRAL_SALDO,
    Saldo,
    armar_saldo,
    calcular_saldo,
    es_pagada_sin_registrar,
    es_parcial,
    es_sin_monto,
    monto_base,
    saldo_efectivo,
)

D = Decimal


def _saldo(monto, pagado="0", nc="0"):
    return armar_saldo(D(monto), None, None, D(pagado), D(nc))


def test_umbral_es_un_centavo():
    assert UMBRAL_SALDO == D("0.01")


@pytest.mark.parametrize(
    "col_monto,precio_final,log_precio,esperado",
    [
        (D("0"), D("1000"), D("1200"), D("1000")),  # el primero > 0 gana
        (None, None, None, D("0")),  # todos nulos -> 0
        (D("0"), None, D("0"), D("0")),  # ninguno > 0 -> primer no nulo
        (D("500"), D("1000"), D("1200"), D("500")),
        (None, None, D("1200"), D("1200")),
        (D("-10"), D("0"), None, D("-10")),  # ninguno > 0 -> primer no nulo
    ],
)
def test_monto_base_precedencia(col_monto, precio_final, log_precio, esperado):
    assert monto_base(col_monto, precio_final, log_precio) == esperado


def test_monto_base_acepta_float_sin_perder_precision():
    assert monto_base(None, 0.1, None) == D("0.1")


def test_saldo_pagos_nc():
    assert calcular_saldo(D("1000"), D("300"), D("100")) == D("600")


def test_armar_saldo_arma_la_dataclass():
    s = armar_saldo(D("0"), D("1000"), None, D("300"), D("100"))

    assert s == Saldo(monto=D("1000"), pagado=D("300"), nc=D("100"), saldo=D("600"))


def test_armar_saldo_pagado_y_nc_nulos_son_cero():
    s = armar_saldo(D("1000"), None, None, None, None)

    assert (s.pagado, s.nc, s.saldo) == (D("0"), D("0"), D("1000"))


def test_umbral_pagada():
    s = _saldo("1000", pagado="999.99")  # saldo 0,01

    assert es_pagada_sin_registrar(s) is True
    assert es_parcial(s) is False


def test_umbral_parcial():
    s = _saldo("1000", pagado="999.98")  # saldo 0,02

    assert es_parcial(s) is True
    assert es_pagada_sin_registrar(s) is False


def test_sobrepago_negativo():
    s = _saldo("1000", pagado="1200")

    assert es_pagada_sin_registrar(s) is True
    assert saldo_efectivo(s) == D("-200")


def test_saldo_0005_se_muestra_cero():
    s = _saldo("1000", pagado="999.995")

    assert saldo_efectivo(s) == D("0")


def test_sin_pagos_no_es_parcial_ni_pagada():
    s = _saldo("1000")

    assert es_parcial(s) is False
    assert es_pagada_sin_registrar(s) is False
    assert saldo_efectivo(s) == D("1000")


def test_sin_monto_nunca_parcial_ni_pagada():
    s = _saldo("0", pagado="100")

    assert es_sin_monto(s.monto) is True
    assert es_parcial(s) is False
    assert es_pagada_sin_registrar(s) is False


def test_solo_nc_es_parcial():
    s = _saldo("1000", nc="200")

    assert es_parcial(s) is True
    assert s.saldo == D("800")


def test_pagada_con_pago_y_nc():
    s = _saldo("500", pagado="300", nc="200")

    assert es_pagada_sin_registrar(s) is True


def test_saldo_efectivo_sin_monto_none():
    assert saldo_efectivo(_saldo("0")) is None
    assert saldo_efectivo(armar_saldo(None, None, None, None, None)) is None
