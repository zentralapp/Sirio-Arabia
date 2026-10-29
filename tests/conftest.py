"""Fixtures comunes de los tests.

Los tests de esta carpeta corren SIN base de datos y SIN .env: solo prueban
funciones puras (fechas, saldos, clasificación, formato).
"""
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

TZ_ART = ZoneInfo("America/Argentina/Buenos_Aires")


@pytest.fixture
def ahora_art():
    """Fábrica de datetimes con zona horaria de Argentina.

    Uso: ahora_art(2026, 10, 29, 9, 1) -> jueves 29/10/2026 09:01 ART.
    """

    def _fabrica(anio, mes, dia, hora=0, minuto=0):
        return datetime(anio, mes, dia, hora, minuto, tzinfo=TZ_ART)

    return _fabrica
