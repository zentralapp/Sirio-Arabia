"""Clasificación pura de cobranzas y cumpleaños en las secciones del reporte.

Reglas (spec reporte-diario, decisiones del 29/10/2026; reporte-regularidad):
1. "Pagada sin registrar cobro" va SOLO a su sección y no suma a ningún total.
2. Si no, va a UNA sección por vencimiento: [hoy, fin_hoy] (vencen hoy),
   (fin_hoy, domingo] (semana), > domingo (vencen más adelante), < hoy
   (ATRASADAS, con días de atraso contra `hoy`), NULL (sin vencimiento).
3. Si además es "Parcial", va TAMBIÉN a "Pagos parciales" (sin importar el
   vencimiento).
"Sin monto" sigue el paso 2 pero nunca es parcial ni pagada.

La partición es la misma todos los días; `componer_reporte` deja vacías las
secciones que no son del modo del día (SECCIONES_POR_MODO).
"""
from dataclasses import replace
from datetime import date
from decimal import Decimal
from typing import Iterable

from backend.services.reporte_diario.fechas import fin_de_hoy, fin_de_semana, modo_reporte, proyectar_cumple
from backend.services.reporte_diario.modelo import (
    SECCIONES_POR_MODO,
    FilaCobranza,
    FilaCumple,
    Reporte,
    TotalGeneral,
)
from backend.services.saldos import es_pagada_sin_registrar, es_parcial

# Secciones que llevan total de saldo al pie (y solo esas).
SECCIONES_CON_TOTAL = ("vencen_hoy", "vencen_semana", "atrasadas", "vencen_mas_adelante", "parciales")

# Secciones que suman al total general: cada cobranza abierta está en UNA sola
# (los parciales ya están en su sección por vencimiento: no se suman dos veces).
SECCIONES_DEL_TOTAL_GENERAL = ("vencen_hoy", "vencen_semana", "atrasadas", "vencen_mas_adelante", "sin_vencimiento")


def _clave_atrasada(f: FilaCobranza):
    return (-f.dias_atraso, f.cliente.casefold(), f.order_id)


def _clave_por_vencimiento(f: FilaCobranza):
    # Vencimiento ascendente con NULL al final, luego cliente y pedido.
    return (f.vencimiento is None, f.vencimiento or date.min, f.cliente.casefold(), f.order_id)


def clasificar_cobranzas(
    filas: Iterable[FilaCobranza], hoy: date, domingo: date, *, fin_hoy: date | None = None
) -> dict[str, tuple[FilaCobranza, ...]]:
    """Reparte pedidos vigentes y sin cobrar en las secciones de cobranzas.

    `fin_hoy` (default: `hoy`) es el último día que cuenta como "hoy"; el
    viernes es el domingo.
    """
    fin_hoy = hoy if fin_hoy is None else fin_hoy
    vencen_hoy, vencen_semana, mas_adelante, atrasadas = [], [], [], []
    parciales, pagadas, sin_vencimiento = [], [], []

    for f in filas:
        if es_pagada_sin_registrar(f.saldo):
            pagadas.append(f)
            continue

        venc = f.vencimiento
        if venc is None:
            sin_vencimiento.append(f)
        elif venc < hoy:
            atrasadas.append(replace(f, dias_atraso=(hoy - venc).days))
        elif venc <= fin_hoy:
            vencen_hoy.append(f)
        elif venc <= domingo:
            vencen_semana.append(f)
        else:
            mas_adelante.append(f)

        if es_parcial(f.saldo):
            parciales.append(f)

    return {
        "vencen_hoy": tuple(sorted(vencen_hoy, key=_clave_por_vencimiento)),
        "vencen_semana": tuple(sorted(vencen_semana, key=_clave_por_vencimiento)),
        "vencen_mas_adelante": tuple(sorted(mas_adelante, key=_clave_por_vencimiento)),
        "atrasadas": tuple(sorted(atrasadas, key=_clave_atrasada)),
        "parciales": tuple(sorted(parciales, key=_clave_por_vencimiento)),
        "pagadas_sin_registrar": tuple(sorted(pagadas, key=_clave_por_vencimiento)),
        "sin_vencimiento": tuple(sorted(sin_vencimiento, key=_clave_por_vencimiento)),
    }


def clasificar_cumples(
    filas: Iterable[FilaCumple], hoy: date, domingo: date, *, fin_hoy: date | None = None
) -> dict[str, tuple[FilaCumple, ...]]:
    """Cumpleaños de "hoy" [hoy, fin_hoy] y de la semana (fin_hoy, domingo], con su ocurrencia."""
    fin_hoy = hoy if fin_hoy is None else fin_hoy
    de_hoy, de_semana = [], []
    anios = sorted({hoy.year, domingo.year})

    for f in filas:
        if f.fecha is None:
            continue
        for anio in anios:
            ocurrencia = proyectar_cumple(f.fecha, anio)
            if hoy <= ocurrencia <= fin_hoy:
                de_hoy.append(replace(f, ocurrencia=ocurrencia))
                break
            if fin_hoy < ocurrencia <= domingo:
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
    """Totales al pie, SOLO para vencen hoy, semana, ATRASADAS, más adelante y parciales."""
    return {nombre: total_saldo(getattr(reporte, nombre)) for nombre in SECCIONES_CON_TOTAL}


def total_general(reporte: Reporte) -> TotalGeneral:
    """Total general de deuda vigente: cada cobranza abierta UNA vez.

    Suma vencen hoy + semana + ATRASADAS + más adelante + sin vencimiento (NO
    los parciales, que ya están en su sección por vencimiento; las pagadas sin
    registrar nunca están en esas secciones). Los "Sin monto" no suman: se cuentan.
    """
    filas = [f for nombre in SECCIONES_DEL_TOTAL_GENERAL for f in getattr(reporte, nombre)]
    return TotalGeneral(
        monto=total_saldo(filas),
        sin_monto=sum(1 for f in filas if f.saldo_efectivo is None),
    )


def componer_reporte(
    hoy: date,
    cobranzas: Iterable[FilaCobranza],
    cumples: Iterable[FilaCumple],
    compras: Iterable = (),
) -> Reporte:
    """Arma el Reporte del modo del día a partir de filas ya leídas (sin DB).

    Clasifica con la partición completa y deja vacías () las secciones que no
    son del modo, así el log de conteos y `sin_novedades` reflejan lo que se
    dibuja. `compras` (regularidad) todavía no se usa.
    """
    modo = modo_reporte(hoy)
    domingo = fin_de_semana(hoy)
    fin_hoy = fin_de_hoy(hoy)
    secciones = {
        **clasificar_cumples(cumples, hoy, domingo, fin_hoy=fin_hoy),
        **clasificar_cobranzas(cobranzas, hoy, domingo, fin_hoy=fin_hoy),
    }
    del_modo = SECCIONES_POR_MODO[modo]
    return Reporte(
        hoy=hoy,
        domingo=domingo,
        modo=modo,
        fin_hoy=fin_hoy,
        **{nombre: (filas if nombre in del_modo else ()) for nombre, filas in secciones.items()},
    )
