from datetime import date, datetime, time, timezone

import pytest

from backend.services.reporte_diario.fechas import (
    DEL_DIA,
    EXTENDIDO,
    TZ_NEGOCIO,
    es_habil,
    fin_de_hoy,
    fin_de_semana,
    hoy_negocio,
    modo_reporte,
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


def test_semana_del_lunes_con_cruce_de_anio():
    # Lunes 28/12/2026: la semana es (29/12/2026, 03/01/2027].
    assert fin_de_semana(date(2026, 12, 28)) == date(2027, 1, 3)
    assert fin_de_hoy(date(2026, 12, 28)) == date(2026, 12, 28)


@pytest.mark.parametrize(
    "fecha,modo",
    [
        (date(2026, 11, 9), EXTENDIDO),  # lun (también si es feriado: no hay calendario)
        (date(2026, 11, 10), DEL_DIA),  # mar
        (date(2026, 11, 11), DEL_DIA),  # mié
        (date(2026, 11, 12), DEL_DIA),  # jue
        (date(2026, 11, 13), DEL_DIA),  # vie
        (date(2026, 11, 14), DEL_DIA),  # sáb forzado
        (date(2026, 11, 15), DEL_DIA),  # dom forzado
    ],
)
def test_modo_reporte(fecha, modo):
    assert modo_reporte(fecha) == modo


def test_modos_son_str_simples():
    assert (EXTENDIDO, DEL_DIA) == ("extendido", "del_dia")


@pytest.mark.parametrize(
    "fecha,fin",
    [
        (date(2026, 11, 9), date(2026, 11, 9)),  # lun
        (date(2026, 11, 10), date(2026, 11, 10)),  # mar
        (date(2026, 11, 11), date(2026, 11, 11)),  # mié
        (date(2026, 11, 12), date(2026, 11, 12)),  # jue
        (date(2026, 11, 6), date(2026, 11, 8)),  # vie -> dom
        (date(2026, 11, 7), date(2026, 11, 8)),  # sáb -> dom
        (date(2026, 11, 8), date(2026, 11, 8)),  # dom
        (date(2026, 12, 25), date(2026, 12, 27)),  # vie feriado: igual cubre el finde
        (date(2027, 1, 1), date(2027, 1, 3)),  # vie 1° de año
    ],
)
def test_fin_de_hoy(fecha, fin):
    assert fin_de_hoy(fecha) == fin


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
