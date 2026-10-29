"""Regla única de saldo de una cobranza (Decimal exacto, nunca float).

Glosario (spec reporte-diario):
- Monto base: el primer valor > 0 entre [Collection.monto, Order.precio_final,
  LogisticsStatus.precio]; si ninguno es > 0, el primero no nulo; si todos son
  nulos, 0.
- Sin monto: monto base <= 0.
- Saldo = monto base - pagado - NC.
- Un saldo <= UMBRAL_SALDO cuenta como saldado (redondeo).
- Pagada sin registrar cobro: monto > 0, pagado + NC > 0 y saldo <= umbral.
- Parcial: monto > 0, pagado + NC > 0 y saldo > umbral.

Acá van solo las funciones puras; la consulta agregada (`saldos_por_pedido`)
se agrega aparte.
"""
from dataclasses import dataclass
from decimal import Decimal

UMBRAL_SALDO = Decimal("0.01")

_CERO = Decimal(0)


@dataclass(frozen=True)
class Saldo:
    monto: Decimal
    pagado: Decimal
    nc: Decimal
    saldo: Decimal


def _dec(valor) -> Decimal | None:
    if valor is None or isinstance(valor, Decimal):
        return valor
    return Decimal(str(valor))


def monto_base(col_monto, precio_final, log_precio) -> Decimal:
    candidatos = [v for v in (_dec(col_monto), _dec(precio_final), _dec(log_precio)) if v is not None]
    for valor in candidatos:
        if valor > 0:
            return valor
    return candidatos[0] if candidatos else _CERO


def calcular_saldo(monto: Decimal, pagado: Decimal, nc: Decimal) -> Decimal:
    return monto - pagado - nc


def armar_saldo(col_monto, precio_final, log_precio, pagado, nc) -> Saldo:
    """Arma el Saldo de un pedido; pagado/NC nulos (sin pagos) valen 0."""
    monto = monto_base(col_monto, precio_final, log_precio)
    pagado = _dec(pagado) or _CERO
    nc = _dec(nc) or _CERO
    return Saldo(monto=monto, pagado=pagado, nc=nc, saldo=calcular_saldo(monto, pagado, nc))


def es_sin_monto(monto: Decimal) -> bool:
    return monto <= 0


def _tiene_pagos(s: Saldo) -> bool:
    return (s.pagado + s.nc) > 0


def es_pagada_sin_registrar(s: Saldo) -> bool:
    return not es_sin_monto(s.monto) and _tiene_pagos(s) and s.saldo <= UMBRAL_SALDO


def es_parcial(s: Saldo) -> bool:
    return not es_sin_monto(s.monto) and _tiene_pagos(s) and s.saldo > UMBRAL_SALDO


def saldo_efectivo(s: Saldo) -> Decimal | None:
    """Saldo para mostrar y sumar.

    None si es "Sin monto" (se muestra el texto, no suma); 0 si está en
    (0; 0,01]; si no, el saldo tal cual (negativo = sobrepago).
    """
    if es_sin_monto(s.monto):
        return None
    if _CERO < s.saldo <= UMBRAL_SALDO:
        return _CERO
    return s.saldo
