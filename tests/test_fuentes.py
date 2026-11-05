"""Las fuentes del PDF están vendorizadas, con licencia y cubren el castellano."""
from pathlib import Path

import pytest
from fontTools.ttLib import TTFont

FUENTES = Path(__file__).resolve().parent.parent / "backend" / "services" / "reporte_diario" / "fuentes"

CARACTERES = "áéíóúÁÉÍÓÚñÑüÜ¿¡°“”‘’—–…$·"


@pytest.mark.parametrize("archivo", ["Poppins-Regular.ttf", "Poppins-SemiBold.ttf"])
def test_poppins_cubre_caracteres_del_castellano(archivo):
    fuente = TTFont(FUENTES / archivo)
    cmap = fuente.getBestCmap()

    faltantes = [c for c in CARACTERES if ord(c) not in cmap]

    assert faltantes == []


def test_licencia_ofl_presente():
    texto = (FUENTES / "OFL.txt").read_text(encoding="utf-8")

    assert "SIL Open Font License, Version 1.1" in texto


def test_fuentes_fuera_de_static():
    assert "static" not in FUENTES.parts
