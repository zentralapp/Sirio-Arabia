from datetime import date
from decimal import Decimal

import pytest

from backend.utils.formato import DIAS_ABREV, fmt_dia_mes, fmt_fecha_es, fmt_plata_es


class TestFmtPlataEs:
    def test_fmt_plata_es_miles(self):
        assert fmt_plata_es(Decimal("1234567.5")) == "1.234.567,50"

    def test_fmt_plata_es_negativo(self):
        assert fmt_plata_es(Decimal("-200")) == "-200,00"

    def test_fmt_plata_es_cero(self):
        assert fmt_plata_es(Decimal("0")) == "0,00"

    @pytest.mark.parametrize(
        "valor,esperado",
        [
            (Decimal("1234.5"), "1.234,50"),
            (Decimal("0.005"), "0,01"),
            (Decimal("-0.001"), "0,00"),
            (Decimal("999.99"), "999,99"),
            (None, "0,00"),
            (1000, "1.000,00"),
        ],
    )
    def test_fmt_plata_es_casos(self, valor, esperado):
        assert fmt_plata_es(valor) == esperado


def test_fmt_fecha_es():
    assert fmt_fecha_es(date(2026, 1, 3)) == "03/01/2026"


def test_fmt_dia_mes():
    assert fmt_dia_mes(date(2026, 11, 1)) == "01/11"


def test_dias_abrev_jueves():
    assert DIAS_ABREV[date(2026, 10, 29).weekday()] == "jue"


def test_dias_abrev_completo():
    assert DIAS_ABREV == ("lun", "mar", "mié", "jue", "vie", "sáb", "dom")
