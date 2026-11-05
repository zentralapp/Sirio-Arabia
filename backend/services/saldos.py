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

Las funciones puras no tocan DB ni Flask; `saldos_por_pedido` (la consulta
agregada) importa SQLAlchemy y los modelos adentro de la función.
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


def saldos_por_pedido(order_ids, session=None) -> dict[int, Saldo]:
    """Saldo de cada pedido en UNA query agregada: {order_id: Saldo}.

    Pagado = sum(abs(amount)) de kind PAYMENT; NC = ídem kind CREDIT_NOTE;
    se excluyen DRAFT y anulados (voided_at). Se clasifica por `kind`, no por
    `method`. Si `order_ids` está vacío no consulta.

    Los imports de SQLAlchemy/modelos van acá adentro para que las funciones
    puras de este módulo se puedan testear sin Flask ni DB.
    """
    ids = sorted({int(i) for i in order_ids})
    if not ids:
        return {}

    from sqlalchemy import func, select

    from backend.extensions import db
    from backend.models import Collection, CollectionPayment, LogisticsStatus, Order

    session = session if session is not None else db.session
    importe = func.abs(CollectionPayment.amount)
    pagos = (
        select(
            CollectionPayment.order_id,
            func.coalesce(func.sum(importe).filter(CollectionPayment.kind == "PAYMENT"), 0).label("pagado"),
            func.coalesce(func.sum(importe).filter(CollectionPayment.kind == "CREDIT_NOTE"), 0).label("nc"),
        )
        .where(
            CollectionPayment.kind != "DRAFT",
            CollectionPayment.voided_at.is_(None),
            CollectionPayment.order_id.in_(ids),
        )
        .group_by(CollectionPayment.order_id)
        .subquery()
    )
    q = (
        select(Order.id, Collection.monto, Order.precio_final, LogisticsStatus.precio, pagos.c.pagado, pagos.c.nc)
        .select_from(Order)
        .outerjoin(Collection, Collection.order_id == Order.id)
        .outerjoin(LogisticsStatus, LogisticsStatus.order_id == Order.id)
        .outerjoin(pagos, pagos.c.order_id == Order.id)
        .where(Order.id.in_(ids))
    )
    return {
        order_id: armar_saldo(col_monto, precio_final, log_precio, pagado, nc)
        for order_id, col_monto, precio_final, log_precio, pagado, nc in session.execute(q)
    }
