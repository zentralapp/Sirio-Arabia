from datetime import date, datetime, time, timezone

import pytest

from backend.services.reporte_diario.fechas import (
    TZ_NEGOCIO,
    es_habil,
    fin_de_semana,
    hoy_negocio,
    proyectar_cumple,
    ultimo_habil_esperado,
)


@pytest.mark.parametrize(
    "hoy,domingo",
    [
        (date(2026, 10, 26), date(2026, 11, 1)),  # lunes
        (date(2026, 10, 28), date(2026, 11, 1)),  # miércoles
        (date(2026, 10, 30), date(2026, 11, 1)),  # viernes: cubre sáb 31/10 y dom 01/11
        (date(2026, 10, 31), date(2026, 11, 1)),  # sábado
        (date(2026, 11, 1), date(2026, 11, 1)),  # domingo: es su propio domingo
        (date(2026, 12, 28), date(2027, 1, 3)),  # cruce de año
    ],
)
def test_fin_de_semana(hoy, domingo):
    assert fin_de_semana(hoy) == domingo


@pytest.mark.parametrize(
    "fecha,habil",
    [
        (date(2026, 10, 26), True),  # lun
        (date(2026, 10, 27), True),  # mar
        (date(2026, 10, 28), True),  # mié
        (date(2026, 10, 29), True),  # jue
        (date(2026, 10, 30), True),  # vie
        (date(2026, 10, 31), False),  # sáb
        (date(2026, 11, 1), False),  # dom
    ],
)
def test_es_habil(fecha, habil):
    assert es_habil(fecha) is habil


class TestHoyNegocio:
    def test_hoy_negocio_utc_medianoche(self):
        ahora_utc = datetime(2026, 10, 30, 1, 30, tzinfo=timezone.utc)

        assert hoy_negocio(ahora_utc) == date(2026, 10, 29)

    def test_hoy_negocio_naive_se_toma_como_utc(self):
        assert hoy_negocio(datetime(2026, 10, 30, 1, 30)) == date(2026, 10, 29)

    def test_hoy_negocio_sin_argumento_devuelve_date(self):
        assert isinstance(hoy_negocio(), date)

    def test_tz_negocio_coincide_con_models(self):
        from backend.models import TZ_NEGOCIO as TZ_MODELS

        assert TZ_NEGOCIO == TZ_MODELS


class TestProyectarCumple:
    def test_proyectar_cumple_mismo_dia_y_mes(self):
        assert proyectar_cumple(date(1985, 10, 29), 2026) == date(2026, 10, 29)

    @pytest.mark.parametrize(
        "anio,esperado",
        [
            (2027, date(2027, 2, 28)),  # no bisiesto
            (2028, date(2028, 2, 29)),  # bisiesto
        ],
    )
    def test_proyectar_cumple_29_feb(self, anio, esperado):
        assert proyectar_cumple(date(1992, 2, 29), anio) == esperado


@pytest.mark.parametrize(
    "momento,esperado",
    [
        ((2026, 10, 29, 8, 59), date(2026, 10, 28)),  # jue antes de 09:00 -> mié
        ((2026, 10, 29, 9, 1), date(2026, 10, 29)),  # jue después de 09:00 -> jue
        ((2026, 10, 29, 9, 0), date(2026, 10, 29)),  # jue 09:00 en punto -> jue
        ((2026, 10, 26, 9, 0), date(2026, 10, 26)),  # lun 09:00 -> lun
        ((2026, 10, 26, 8, 59), date(2026, 10, 23)),  # lun 08:59 -> vie anterior
        ((2026, 10, 31, 11, 0), date(2026, 10, 30)),  # sáb -> vie
        ((2026, 11, 1, 11, 0), date(2026, 10, 30)),  # dom -> vie
        ((2026, 11, 1, 23, 59), date(2026, 10, 30)),  # dom de noche -> vie
    ],
)
def test_ultimo_habil_esperado(ahora_art, momento, esperado):
    assert ultimo_habil_esperado(ahora_art(*momento)) == esperado


def test_ultimo_habil_esperado_hora_limite_configurable(ahora_art):
    ahora = ahora_art(2026, 10, 29, 9, 30)

    assert ultimo_habil_esperado(ahora, hora_limite=time(10, 0)) == date(2026, 10, 28)


def test_ultimo_habil_esperado_convierte_utc_a_art():
    # 12:01 UTC = 09:01 ART del jueves 29/10
    ahora_utc = datetime(2026, 10, 29, 12, 1, tzinfo=timezone.utc)

    assert ultimo_habil_esperado(ahora_utc) == date(2026, 10, 29)
