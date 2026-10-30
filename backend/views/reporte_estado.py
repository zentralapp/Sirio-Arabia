"""Aviso del reporte diario para admins (spec H, ADR-8).

Blueprint mínimo, sin rutas: solo un context processor que inyecta
`reporte_alerta` en todas las plantillas. Lo muestra `_reporte_alerta.html`
(incluido desde layout.html).

- Apagado salvo REPORTE_BANNER=1 (kill switch del servicio web).
- Solo para usuarios autenticados con is_admin.
- Nunca lanza: si algo falla, la página se renderiza sin aviso.
"""
import os

from flask import Blueprint
from flask_login import current_user

from backend.services.reporte_estado import estado_reporte

bp = Blueprint("reporte_estado", __name__)


def banner_activo() -> bool:
    return os.getenv("REPORTE_BANNER", "").strip() == "1"


@bp.app_context_processor
def inyectar_reporte_alerta():
    try:
        if not banner_activo():
            return {}
        if not (getattr(current_user, "is_authenticated", False) and getattr(current_user, "is_admin", False)):
            return {}
        alerta = estado_reporte()
        return {"reporte_alerta": alerta} if alerta else {}
    except Exception:  # noqa: BLE001 - el aviso nunca rompe una página
        return {}
