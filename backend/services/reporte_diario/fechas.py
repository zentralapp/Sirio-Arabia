"""Fechas de negocio del reporte diario (funciones puras salvo el reloj).

- "Hoy" es SIEMPRE la fecha en America/Argentina/Buenos_Aires, nunca UTC.
- Día hábil = lunes a viernes (los feriados cuentan como hábiles).
- Ventana "semana" = (hoy, domingo]: excluye hoy, incluye el domingo.
"""
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

# Mismo valor que backend.models.TZ_NEGOCIO (hay un test que lo controla).
# Se repite acá para no importar models (SQLAlchemy) desde lógica pura.
TZ_NEGOCIO = "America/Argentina/Buenos_Aires"
_TZ = ZoneInfo(TZ_NEGOCIO)

_DOMINGO = 6  # date.weekday()


def _a_art(momento: datetime) -> datetime:
    """Pasa un datetime a hora argentina; uno naive se interpreta como UTC."""
    if momento.tzinfo is None:
        momento = momento.replace(tzinfo=timezone.utc)
    return momento.astimezone(_TZ)


def hoy_negocio(ahora: datetime | None = None) -> date:
    """Fecha calendario en Buenos Aires para `ahora` (default: el reloj)."""
    if ahora is None:
        return datetime.now(_TZ).date()
    return _a_art(ahora).date()


def es_habil(fecha: date) -> bool:
    """Lunes a viernes."""
    return fecha.weekday() < 5


def fin_de_semana(hoy: date) -> date:
    """El domingo igual o posterior a `hoy`."""
    return hoy + timedelta(days=_DOMINGO - hoy.weekday())


def proyectar_cumple(fecha: date, anio: int) -> date:
    """Día/mes de `fecha` en el año `anio`; el 29/2 cae 28/2 si no es bisiesto."""
    try:
        return date(anio, fecha.month, fecha.day)
    except ValueError:
        # Solo puede pasar con el 29/2 en un año no bisiesto.
        return date(anio, 2, 28)


def _habil_anterior(fecha: date) -> date:
    previo = fecha - timedelta(days=1)
    while not es_habil(previo):
        previo -= timedelta(days=1)
    return previo


def ultimo_habil_esperado(ahora: datetime, hora_limite: time = time(9, 0)) -> date:
    """Día hábil cuyo reporte ya debería haber salido (para el aviso de admins).

    Si `ahora` (en ART) es lunes a viernes y la hora es >= `hora_limite`, es
    hoy; en cualquier otro caso, el día hábil anterior. Sábados y domingos
    nunca son días esperados.
    """
    local = _a_art(ahora)
    hoy = local.date()
    if es_habil(hoy) and local.time() >= hora_limite:
        return hoy
    return _habil_anterior(hoy)
