"""Modelo de datos del reporte diario (inmutable, sin DB ni Flask).

`datos.py` llena estas filas desde la base; `clasificar.py` las reparte en
secciones; `pdf.py` las dibuja.
"""
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from backend.services.saldos import Saldo, es_sin_monto, saldo_efectivo

# Estados de cobranza (según condiciones_estado_cobranza de models.py).
ATRASADO = "ATRASADO"
A_COBRAR = "A_COBRAR"
EN_CAMINO = "EN_CAMINO"


@dataclass(frozen=True)
class FilaCobranza:
    order_id: int
    cliente: str
    empresa: str
    vencimiento: date | None
    estado: str
    saldo: Saldo
    dias_atraso: int | None = None  # solo en ATRASADAS

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

    @property
    def secciones(self) -> tuple[tuple, ...]:
        return (
            self.cumples_hoy,
            self.cumples_semana,
            self.vencen_hoy,
            self.vencen_semana,
            self.atrasadas,
            self.parciales,
            self.pagadas_sin_registrar,
            self.sin_vencimiento,
        )

    @property
    def sin_novedades(self) -> bool:
        """True si TODAS las secciones (incluida sin vencimiento) están vacías."""
        return not any(self.secciones)

    @property
    def vacio(self) -> bool:
        return self.sin_novedades
