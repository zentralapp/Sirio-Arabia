"""Clasificación pura de cobranzas y cumpleaños en las secciones del reporte.

Reglas (spec reporte-diario, decisiones del 29/10/2026):
1. "Pagada sin registrar cobro" va SOLO a su sección y no suma a ningún total.
2. Si no, va a UNA sección por vencimiento: == hoy, (hoy, domingo], < hoy
   (ATRASADAS, con días de atraso), NULL (sin vencimiento); > domingo, ninguna.
3. Si además es "Parcial", va TAMBIÉN a "Pagos parciales" (sin importar el
   vencimiento).
"Sin monto" sigue el paso 2 pero nunca es parcial ni pagada.
"""
from dataclasses import replace
from datetime import date
from decimal import Decimal
from typing import Iterable

from backend.services.reporte_diario.fechas import fin_de_semana, proyectar_cumple
from backend.services.reporte_diario.modelo import FilaCobranza, FilaCumple, Reporte
from backend.services.saldos import es_pagada_sin_registrar, es_parcial

# Secciones que llevan total de saldo al pie (y solo esas).
SECCIONES_CON_TOTAL = ("vencen_hoy", "vencen_semana", "atrasadas", "parciales")


def _clave_atrasada(f: FilaCobranza):
    return (-f.dias_atraso, f.cliente.casefold(), f.order_id)


def _clave_por_vencimiento(f: FilaCobranza):
    # Vencimiento ascendente con NULL al final, luego cliente y pedido.
    return (f.vencimiento is None, f.vencimiento or date.min, f.cliente.casefold(), f.order_id)


def clasificar_cobranzas(
    filas: Iterable[FilaCobranza], hoy: date, domingo: date
) -> dict[str, tuple[FilaCobranza, ...]]:
    """Reparte pedidos vigentes y sin cobrar en las secciones de cobranzas."""
    vencen_hoy, vencen_semana, atrasadas = [], [], []
    parciales, pagadas, sin_vencimiento = [], [], []

    for f in filas:
        if es_pagada_sin_registrar(f.saldo):
            pagadas.append(f)
            continue

        venc = f.vencimiento
        if venc is None:
            sin_vencimiento.append(f)
        elif venc == hoy:
            vencen_hoy.append(f)
        elif hoy < venc <= domingo:
            vencen_semana.append(f)
        elif venc < hoy:
            atrasadas.append(replace(f, dias_atraso=(hoy - venc).days))

        if es_parcial(f.saldo):
            parciales.append(f)

    return {
        "vencen_hoy": tuple(sorted(vencen_hoy, key=_clave_por_vencimiento)),
        "vencen_semana": tuple(sorted(vencen_semana, key=_clave_por_vencimiento)),
        "atrasadas": tuple(sorted(atrasadas, key=_clave_atrasada)),
        "parciales": tuple(sorted(parciales, key=_clave_por_vencimiento)),
        "pagadas_sin_registrar": tuple(sorted(pagadas, key=_clave_por_vencimiento)),
        "sin_vencimiento": tuple(sorted(sin_vencimiento, key=_clave_por_vencimiento)),
    }


def clasificar_cumples(
    filas: Iterable[FilaCumple], hoy: date, domingo: date
) -> dict[str, tuple[FilaCumple, ...]]:
    """Cumpleaños de hoy y de la semana (hoy, domingo], con su ocurrencia."""
    de_hoy, de_semana = [], []
    anios = sorted({hoy.year, domingo.year})

    for f in filas:
        if f.fecha is None:
            continue
        for anio in anios:
            ocurrencia = proyectar_cumple(f.fecha, anio)
            if ocurrencia == hoy:
                de_hoy.append(replace(f, ocurrencia=ocurrencia))
                break
            if hoy < ocurrencia <= domingo:
                de_semana.append(replace(f, ocurrencia=ocurrencia))
                break

    def clave(c: FilaCumple):
        return (c.ocurrencia, c.nombre.casefold())

    return {
        "cumples_hoy": tuple(sorted(de_hoy, key=clave)),
        "cumples_semana": tuple(sorted(de_semana, key=clave)),
    }


def total_saldo(filas: Iterable[FilaCobranza]) -> Decimal:
    """Suma de saldos efectivos; los "Sin monto" (None) no suman."""
    return sum((f.saldo_efectivo for f in filas if f.saldo_efectivo is not None), Decimal(0))


def totales(reporte: Reporte) -> dict[str, Decimal]:
    """Totales al pie, SOLO para vencen hoy, semana, ATRASADAS y parciales."""
    return {nombre: total_saldo(getattr(reporte, nombre)) for nombre in SECCIONES_CON_TOTAL}


def componer_reporte(
    hoy: date, cobranzas: Iterable[FilaCobranza], cumples: Iterable[FilaCumple]
) -> Reporte:
    """Arma el Reporte completo a partir de filas ya leídas (sin DB)."""
    domingo = fin_de_semana(hoy)
    return Reporte(
        hoy=hoy,
        domingo=domingo,
        **clasificar_cumples(cumples, hoy, domingo),
        **clasificar_cobranzas(cobranzas, hoy, domingo),
    )
