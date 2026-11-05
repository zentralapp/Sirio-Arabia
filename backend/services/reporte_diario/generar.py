"""Generación compartida del reporte: la usan el job (cron) y el botón de la web.

- `generar_pdf(session, fecha)`: lee (READ ONLY), clasifica y dibuja. Es lo
  mismo que hace el `--dry-run` del job: no construye mailer ni toca
  report_send_log.
- `armar_mail(fecha, pdf, prueba=...)`: asunto, cuerpo y adjunto del mail.
  Con `prueba=False` es exactamente el mail del envío automático.

`armar_reporte` y `render_pdf` se llaman por el nombre de ESTE módulo, así
los tests los parchean en `generar.armar_reporte` / `generar.render_pdf`.

NUNCA importa `backend.app` ni `backend.views` (tests/test_aislamiento.py).
"""
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Callable

from backend.services.mailer.base import (
    CUERPO_PRUEBA,
    CUERPO_REPORTE,
    Adjunto,
    asunto_reporte,
    nombre_adjunto,
)
from backend.services.reporte_diario.datos import armar_reporte
from backend.services.reporte_diario.modelo import Reporte
from backend.services.reporte_diario.pdf import render_pdf


@dataclass(frozen=True)
class Mail:
    subject: str
    body: str
    attachments: tuple[Adjunto, ...]


def generar_pdf(
    session,
    fecha: date,
    generado_en: datetime | None = None,
    *,
    al_armar: Callable[[Reporte], None] | None = None,
) -> tuple[Reporte, bytes]:
    """Reporte de `fecha` y su PDF.

    `generado_en` (con zona) va al pie; por defecto, ahora en UTC.
    `al_armar(reporte)` se llama entre la lectura y el render: el job lo usa
    para loguear los conteos antes de dibujar, como siempre.
    """
    reporte = armar_reporte(fecha, session=session)
    if al_armar is not None:
        al_armar(reporte)
    pdf = render_pdf(reporte, generado_en if generado_en is not None else datetime.now(timezone.utc))
    return reporte, pdf


def armar_mail(fecha: date, pdf: bytes, *, prueba: bool = False) -> Mail:
    """Mail del reporte. `prueba=True`: asunto "[PRUEBA] ..." y cuerpo de prueba."""
    return Mail(
        subject=asunto_reporte(fecha, prueba=prueba),
        body=CUERPO_PRUEBA if prueba else CUERPO_REPORTE,
        attachments=(Adjunto(nombre_adjunto(fecha), pdf),),
    )
