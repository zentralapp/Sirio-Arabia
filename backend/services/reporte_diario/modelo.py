"""Modelo de datos del reporte diario (inmutable, sin DB ni Flask).

`datos.py` llena estas filas desde la base; `clasificar.py` las reparte en
secciones; `pdf.py` las dibuja.
"""
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from backend.services.reporte_diario.fechas import DEL_DIA, EXTENDIDO
from backend.services.saldos import Saldo, es_sin_monto, saldo_efectivo

# Estados de cobranza (según condiciones_estado_cobranza de models.py).
ATRASADO = "ATRASADO"
A_COBRAR = "A_COBRAR"
EN_CAMINO = "EN_CAMINO"

# Secciones (atributos de Reporte) que tiene cada modo, en el orden del PDF.
# Fuente ÚNICA: componer_reporte vacía las que no son del modo y
# Reporte.secciones / sin_novedades solo miran estas.
SECCIONES_POR_MODO = {
    DEL_DIA: ("cumples_hoy", "vencen_hoy", "atrasadas", "pagadas_sin_registrar", "sin_vencimiento"),
    EXTENDIDO: (
        "cumples_hoy",
        "cumples_semana",
        "vencen_hoy",
        "vencen_semana",
        "atrasadas",
        "vencen_mas_adelante",
        "sin_vencimiento",
        "parciales",
        "pagadas_sin_registrar",
        "regularidad",
    ),
}


@dataclass(frozen=True)
class FilaCobranza:
    order_id: int
    cliente: str
    empresa: str
    vencimiento: date | None
    estado: str
    saldo: Saldo
    dias_atraso: int | None = None  # solo en ATRASADAS
    client_id: int | None = None
    company_id: int | None = None

    @property
    def sin_monto(self) -> bool:
        return es_sin_monto(self.saldo.monto)

    @property
    def saldo_efectivo(self) -> Decimal | None:
        """None = "Sin monto" (no suma a totales)."""
        return saldo_efectivo(self.saldo)


@dataclass(frozen=True)
class FilaCumple:
    nombre: str
    puesto: str
    cliente: str
    empresa: str
    fecha: date  # fecha de nacimiento tal cual está cargada
    ocurrencia: date | None = None  # próxima ocurrencia (la pone clasificar)


@dataclass(frozen=True)
class TotalGeneral:
    """Total general de deuda vigente: suma de saldos con monto + cuántas son Sin monto."""

    monto: Decimal
    sin_monto: int


@dataclass(frozen=True)
class DeudaPar:
    """Deuda abierta de un par (cliente, empresa) para la regularidad."""

    total: Decimal = Decimal(0)
    sin_monto: int = 0
    abiertas: int = 0


@dataclass(frozen=True)
class ComprasPar:
    """Días de compra DISTINTOS, ascendentes y <= hoy de un par (lo arma datos.py)."""

    client_id: int
    company_id: int
    cliente: str
    empresa: str
    dias: tuple[date, ...]


@dataclass(frozen=True)
class FilaRegularidad:
    cliente: str
    empresa: str
    ultima_compra: date
    dias_sin_comprar: int  # N
    ritmo: int  # P
    compras: int  # días distintos usados
    deuda: DeudaPar


@dataclass(frozen=True)
class Reporte:
    hoy: date
    domingo: date
    cumples_hoy: tuple[FilaCumple, ...] = ()
    cumples_semana: tuple[FilaCumple, ...] = ()
    vencen_hoy: tuple[FilaCobranza, ...] = ()
    vencen_semana: tuple[FilaCobranza, ...] = ()
    atrasadas: tuple[FilaCobranza, ...] = ()
    parciales: tuple[FilaCobranza, ...] = ()
    pagadas_sin_registrar: tuple[FilaCobranza, ...] = ()
    sin_vencimiento: tuple[FilaCobranza, ...] = ()
    modo: str = DEL_DIA
    fin_hoy: date | None = None  # último día que cuenta como "hoy" (None = hoy)
    vencen_mas_adelante: tuple[FilaCobranza, ...] = ()
    regularidad: tuple[FilaRegularidad, ...] = ()

    @property
    def secciones(self) -> tuple[tuple, ...]:
        """Las secciones del modo, en el orden del PDF."""
        return tuple(getattr(self, nombre) for nombre in SECCIONES_POR_MODO[self.modo])

    @property
    def sin_novedades(self) -> bool:
        """True si TODAS las secciones del modo (incluida sin vencimiento) están vacías."""
        return not any(self.secciones)

    @property
    def vacio(self) -> bool:
        return self.sin_novedades
