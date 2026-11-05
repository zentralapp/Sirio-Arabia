"""Regularidad de compra por par (cliente, empresa), solo los lunes (puro: sin DB ni Flask).

Reglas (spec reporte-regularidad K, decisiones #3567/#3571/#3574):
- Días de compra DISTINTOS por par (los arma datos.py; acá se descartan los
  posteriores a la fecha de negocio por las dudas).
- Hacen falta al menos MIN_DIAS días distintos. Se usan solo los últimos
  MAX_INTERVALOS intervalos (los de los MAX_INTERVALOS + 1 días más recientes).
- P (ritmo habitual) = mediana de esos intervalos, half-up a entero, mínimo 1.
- N (días sin comprar) = fecha de negocio - último día de compra.
- Dispara si P <= N <= min(FACTOR_TOPE * P, TOPE_DIAS).
- Orden: N/P descendente, N descendente, cliente y empresa ascendentes.
- La deuda es la del par: todas sus cobranzas abiertas salvo las pagadas sin
  registrar; los "Sin monto" se cuentan aparte.
"""
from datetime import date
from decimal import Decimal
from fractions import Fraction
from itertools import pairwise
from typing import Iterable

from backend.services.reporte_diario.modelo import ComprasPar, DeudaPar, FilaCobranza, FilaRegularidad
from backend.services.saldos import es_pagada_sin_registrar
from backend.utils.formato import fmt_plata_es

# ---------------------------------------------------------------------------
# UMBRALES PROVISIONALES (sin calibrar con prod, #3571): ajustar después del
# primer lunes real. Es el ÚNICO lugar donde viven; datos.py los importa.
MIN_DIAS = 3  # días de compra distintos necesarios para tener un ritmo
MAX_INTERVALOS = 12  # intervalos más recientes que entran en la mediana
FACTOR_TOPE = 3  # más de FACTOR_TOPE * P sin comprar: se lo da por perdido
TOPE_DIAS = 365  # techo absoluto de días sin comprar
# ---------------------------------------------------------------------------


def intervalos(dias: Iterable[date]) -> list[int]:
    """Días entre compras consecutivas (con días distintos y ascendentes, todos >= 1)."""
    return [(b - a).days for a, b in pairwise(dias)]


def mediana_half_up(xs: Iterable[int]) -> int:
    """Mediana entera: impar, el del medio; par, (a + b + 1) // 2 (half-up exacto, NO round())."""
    orden = sorted(xs)
    if not orden:
        raise ValueError("mediana de una lista vacía")
    medio = len(orden) // 2
    if len(orden) % 2:
        return orden[medio]
    return (orden[medio - 1] + orden[medio] + 1) // 2


def ritmo(dias: Iterable[date]) -> int | None:
    """P del par, o None si tiene menos de MIN_DIAS días distintos."""
    dias = sorted(set(dias))
    if len(dias) < MIN_DIAS:
        return None
    ventana = intervalos(dias)[-MAX_INTERVALOS:]
    return max(1, mediana_half_up(ventana))


def dispara(n: int, p: int) -> bool:
    """El par aparece si P <= N <= min(FACTOR_TOPE * P, TOPE_DIAS)."""
    return p <= n <= min(FACTOR_TOPE * p, TOPE_DIAS)


def deuda_por_par(filas: Iterable[FilaCobranza]) -> dict[tuple[int, int], DeudaPar]:
    """Deuda abierta por (client_id, company_id), sin las pagadas sin registrar."""
    acumulado: dict[tuple[int, int], list] = {}
    for f in filas:
        if f.client_id is None or f.company_id is None or es_pagada_sin_registrar(f.saldo):
            continue
        total, sin_monto, abiertas = acumulado.get((f.client_id, f.company_id), (Decimal(0), 0, 0))
        saldo = f.saldo_efectivo
        if saldo is None:
            sin_monto += 1
        else:
            total += saldo
        acumulado[(f.client_id, f.company_id)] = (total, sin_monto, abiertas + 1)
    return {par: DeudaPar(total=t, sin_monto=s, abiertas=a) for par, (t, s, a) in acumulado.items()}


def texto_deuda(d: DeudaPar) -> str:
    """Estado de deuda del par, entre paréntesis (textos exactos de la spec K)."""
    con_monto = d.total > 0
    if d.abiertas == 0 or (not con_monto and d.sin_monto == 0):
        return "(no tiene deudas actuales)"
    if not con_monto:
        return "(tiene deudas pendientes sin monto cargado)"
    plata = f"${fmt_plata_es(d.total)}"
    if d.sin_monto == 0:
        return f"(tiene deudas pendientes de {plata})"
    return f"(tiene deudas pendientes de {plata} + {d.sin_monto} sin monto)"


def calcular_regularidad(
    pares: Iterable[ComprasPar], hoy: date, deudas: dict[tuple[int, int], DeudaPar]
) -> tuple[FilaRegularidad, ...]:
    """Pares que se pasaron de su ritmo habitual, ordenados por N/P descendente."""
    candidatos = []
    for par in pares:
        dias = sorted({d for d in par.dias if d <= hoy})
        p = ritmo(dias)
        if p is None:
            continue
        n = (hoy - dias[-1]).days
        if not dispara(n, p):
            continue
        fila = FilaRegularidad(
            cliente=par.cliente,
            empresa=par.empresa,
            ultima_compra=dias[-1],
            dias_sin_comprar=n,
            ritmo=p,
            compras=min(len(dias), MAX_INTERVALOS + 1),
            deuda=deudas.get((par.client_id, par.company_id), DeudaPar()),
        )
        candidatos.append(fila)

    def clave(f: FilaRegularidad):
        return (-Fraction(f.dias_sin_comprar, f.ritmo), -f.dias_sin_comprar, f.cliente.casefold(), f.empresa.casefold())

    return tuple(sorted(candidatos, key=clave))
