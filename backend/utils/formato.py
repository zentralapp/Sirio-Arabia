"""Formato es-AR para plata y fechas (sin depender del locale del sistema).

Lo usan el reporte diario (PDF y asunto del mail). `fmt_plata_es` replica
`_fmt_money_es` de backend/views/main.py pero con Decimal exacto; main.py no
se toca.
"""
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

# Índice = date.weekday() (0 = lunes).
DIAS_ABREV = ("lun", "mar", "mié", "jue", "vie", "sáb", "dom")

_CENTAVO = Decimal("0.01")


def fmt_plata_es(valor) -> str:
    """Decimal -> "1.234.567,50". None cuenta como 0; negativos con "-"."""
    if valor is None:
        valor = Decimal(0)
    if not isinstance(valor, Decimal):
        valor = Decimal(str(valor))
    redondeado = valor.quantize(_CENTAVO, rounding=ROUND_HALF_UP)
    if redondeado == 0:
        redondeado = abs(redondeado)  # evita "-0,00"
    texto = f"{redondeado:,.2f}"
    return texto.replace(",", "X").replace(".", ",").replace("X", ".")


def fmt_fecha_es(fecha: date) -> str:
    """date -> "DD/MM/YYYY"."""
    return fecha.strftime("%d/%m/%Y")


def fmt_dia_mes(fecha: date) -> str:
    """date -> "DD/MM"."""
    return fecha.strftime("%d/%m")
