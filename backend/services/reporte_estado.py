"""Estado del reporte diario para el aviso de admins en la web (spec H).

- `evaluar_estado(fila, esperado, ahora_utc)`: regla PURA que decide el aviso.
- `estado_reporte(ahora=None)`: busca en report_send_log la fila del día hábil
  esperado (fecha EXACTA, hora límite 09:00 ART) y la evalúa.

Garantías (corre dentro del render de CADA página para admins):
- Solo SELECT, en una conexión aparte de `db.session` (si falla no envenena la
  sesión del request) y, en Postgres, en transacción READ ONLY con timeout.
- Nunca lanza: ante cualquier error (tabla inexistente, base caída) devuelve
  {} = sin aviso, y la página carga normal.
- Cache por proceso de 5 minutos (también para los errores), así una página
  no consulta la base en cada request. La recuperación se ve en <= 5 min.
- Los mensajes no incluyen secretos: el error guardado se reduce a su primera
  línea, sin URLs, mails ni cadenas con pinta de token, y a lo sumo 200 chars.
"""
from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import text

from backend.services.reporte_diario.fechas import ultimo_habil_esperado

log = logging.getLogger(__name__)

REPORT_KEY = "diario"
CACHE_TTL = 300  # segundos
PENDING_COLGADO = timedelta(minutes=30)
MAX_DETALLE = 200
TIMEOUT_CONSULTA = "2s"

PREFIJO_INCIERTO = "estado incierto"  # mismo prefijo que guarda el job
_MARCAS_TOKEN_REVOCADO = ("invalid_grant", "token de gmail fue revocado")

_SQL = text(
    "SELECT status, error, updated_at FROM report_send_log "
    "WHERE report_key = :key AND fecha_negocio = :esperado"
)

_URL = re.compile(r"\b[a-z][a-z0-9+.\-]*://\S+", re.IGNORECASE)
_MAIL = re.compile(r"[\w.+\-]+@[\w\-]+(\.[\w\-]+)+")
# Cadenas largas con letras y dígitos: tokens, secretos, base64.
_TOKEN = re.compile(r"(?=[\w/+=.\-]*\d)(?=[\w/+=.\-]*[A-Za-z])[\w/+=.\-]{24,}")


@dataclass(frozen=True)
class FilaEstado:
    status: str
    error: str | None
    updated_at: datetime | None


def _dd_mm(fecha: date) -> str:
    return fecha.strftime("%d/%m")


def _utc_naive(momento: datetime) -> datetime:
    if momento.tzinfo is not None:
        momento = momento.astimezone(timezone.utc).replace(tzinfo=None)
    return momento


def _detalle_seguro(error: str | None) -> str:
    """Primera línea del error, sin secretos aparentes, <= MAX_DETALLE chars."""
    if not error:
        return ""
    primera = error.strip().splitlines()[0] if error.strip() else ""
    primera = _URL.sub("[url]", primera)
    primera = _MAIL.sub("[mail]", primera)
    primera = _TOKEN.sub("[oculto]", primera)
    primera = " ".join(primera.split())
    if len(primera) > MAX_DETALLE:
        primera = primera[: MAX_DETALLE - 1].rstrip() + "…"
    return primera


def _alerta(nivel: str, mensaje: str, esperado: date, detalle: str = "") -> dict:
    return {"nivel": nivel, "mensaje": mensaje, "detalle": detalle, "fecha": _dd_mm(esperado)}


def _incierto(esperado: date) -> dict:
    return _alerta(
        "warning",
        f"Estado incierto del reporte del {_dd_mm(esperado)}: el mail pudo haber salido. "
        "Revisar Enviados de la cuenta del negocio antes de reenviarlo con --force.",
        esperado,
    )


def evaluar_estado(fila: FilaEstado | None, esperado: date, ahora_utc: datetime) -> dict:
    """Aviso para la fecha esperada, o {} si no hay nada que avisar."""
    dia = _dd_mm(esperado)
    if fila is None:
        return _alerta(
            "danger",
            f"No se envió el reporte del {dia}. Revisar el log del cron del reporte diario.",
            esperado,
        )
    status = (fila.status or "").upper()
    if status == "SENT":
        return {}
    if status == "PENDING":
        if fila.updated_at is None:
            return _incierto(esperado)
        if _utc_naive(ahora_utc) - _utc_naive(fila.updated_at) > PENDING_COLGADO:
            return _incierto(esperado)
        return {}
    if status == "FAILED":
        error = (fila.error or "").strip()
        if error.lower().startswith(PREFIJO_INCIERTO):
            return _incierto(esperado)
        if any(marca in error.lower() for marca in _MARCAS_TOKEN_REVOCADO):
            return _alerta(
                "danger",
                f"Falló el reporte del {dia}: hay que re-autorizar Gmail "
                "(scripts/autorizar_gmail.py) y actualizar el token en Railway.",
                esperado,
            )
        return _alerta("danger", f"Falló el reporte del {dia}.", esperado, _detalle_seguro(error))
    return {}


def consultar_fila(conn, esperado: date) -> FilaEstado | None:
    """Fila de report_send_log del reporte diario para `esperado` (solo SELECT)."""
    fila = conn.execute(_SQL, {"key": REPORT_KEY, "esperado": esperado}).first()
    if fila is None:
        return None
    return FilaEstado(status=fila[0], error=fila[1], updated_at=fila[2])


def _conectar_default():
    from backend.extensions import db  # liviano: no importa la app web

    return db.engine.connect()


def _leer(conectar, esperado: date) -> FilaEstado | None:
    with conectar() as conn:
        if getattr(conn.dialect, "name", "") == "postgresql":
            conn.execute(text("SET TRANSACTION READ ONLY"))
            conn.execute(text(f"SET LOCAL statement_timeout = '{TIMEOUT_CONSULTA}'"))
        return consultar_fila(conn, esperado)
        # al salir del with, la conexión hace rollback y vuelve al pool


_lock = threading.Lock()
_cache: dict = {"clave": None, "vence": 0.0, "valor": {}}


def limpiar_cache() -> None:
    with _lock:
        _cache.update(clave=None, vence=0.0, valor={})


def estado_reporte(ahora: datetime | None = None, *, conectar=None, reloj=time.monotonic) -> dict:
    """Aviso del reporte del día hábil esperado ({} si está todo bien o si falla)."""
    try:
        if ahora is None:
            ahora = datetime.now(timezone.utc)
        esperado = ultimo_habil_esperado(ahora)
        t = reloj()
        with _lock:
            if _cache["clave"] == esperado and t < _cache["vence"]:
                return dict(_cache["valor"])
        try:
            fila = _leer(conectar or _conectar_default, esperado)
            valor = evaluar_estado(fila, esperado, _utc_naive(ahora))
        except Exception as exc:  # noqa: BLE001 - el aviso nunca rompe una página
            log.warning("no se pudo leer el estado del reporte diario: %s", type(exc).__name__)
            valor = {}
        with _lock:
            _cache.update(clave=esperado, vence=t + CACHE_TTL, valor=dict(valor))
        return dict(valor)
    except Exception:  # noqa: BLE001
        return {}
