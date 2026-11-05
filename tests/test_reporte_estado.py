"""Estado del reporte diario para el aviso de admins (spec H).

`evaluar_estado` es pura (fila + fecha esperada + reloj). `estado_reporte`
consulta report_send_log por la fecha EXACTA del día hábil esperado, con cache
de 5 minutos, y nunca lanza: ante cualquier error devuelve {} (sin aviso).
"""
from datetime import date, datetime, timedelta, timezone

import pytest

from backend.services import reporte_estado as re_
from backend.services.reporte_estado import (
    CACHE_TTL,
    FilaEstado,
    estado_reporte,
    evaluar_estado,
    limpiar_cache,
)
from backend.services.mailer.gmail_api import MENSAJE_INCIERTO, MENSAJE_TOKEN_REVOCADO

JUEVES = date(2026, 10, 29)
# jueves 29/10/2026 10:00 ART = 13:00 UTC (naive, como guarda el job)
AHORA_UTC = datetime(2026, 10, 29, 13, 0)

SECRETO = "1//0gAbCdEf1234567890-ZyXwVuTsRqPoNmLkJiHgFeDcBa"


def _fila(status, error=None, minutos=0):
    return FilaEstado(status=status, error=error, updated_at=AHORA_UTC - timedelta(minutes=minutos))


@pytest.fixture(autouse=True)
def _cache_limpio():
    limpiar_cache()
    yield
    limpiar_cache()


# --- evaluar_estado (pura) ---------------------------------------------------


def test_sin_fila_no_se_envio():
    alerta = evaluar_estado(None, JUEVES, AHORA_UTC)
    assert alerta["mensaje"].startswith("No se envió el reporte del 29/10")
    assert alerta["nivel"] == "danger"
    assert alerta["fecha"] == "29/10"


def test_sent_nada():
    assert evaluar_estado(_fila("SENT"), JUEVES, AHORA_UTC) == {}


def test_pending_10_min_nada():
    assert evaluar_estado(_fila("PENDING", minutos=10), JUEVES, AHORA_UTC) == {}


def test_pending_45_min_incierto():
    alerta = evaluar_estado(_fila("PENDING", minutos=45), JUEVES, AHORA_UTC)
    assert alerta["mensaje"].startswith("Estado incierto del reporte del 29/10")
    assert "Enviados" in alerta["mensaje"]
    assert alerta["nivel"] == "warning"


def test_pending_justo_30_min_todavia_nada():
    assert evaluar_estado(_fila("PENDING", minutos=30), JUEVES, AHORA_UTC) == {}


def test_failed_muestra_error_corto():
    error = "RuntimeError: " + "x" * 3000
    alerta = evaluar_estado(_fila("FAILED", error), JUEVES, AHORA_UTC)
    assert alerta["mensaje"].startswith("Falló el reporte del 29/10")
    assert alerta["nivel"] == "danger"
    assert 0 < len(alerta["detalle"]) <= 200
    assert alerta["detalle"].startswith("RuntimeError")


def test_failed_solo_primera_linea_del_error():
    alerta = evaluar_estado(_fila("FAILED", "Falla X\nTraceback interno\n  linea 2"), JUEVES, AHORA_UTC)
    assert alerta["detalle"] == "Falla X"


def test_failed_sin_texto_de_error():
    alerta = evaluar_estado(_fila("FAILED", None), JUEVES, AHORA_UTC)
    assert alerta["mensaje"].startswith("Falló el reporte del 29/10")
    assert alerta["detalle"] == ""


def test_failed_invalid_grant_dice_reautorizar():
    alerta = evaluar_estado(_fila("FAILED", MENSAJE_TOKEN_REVOCADO), JUEVES, AHORA_UTC)
    assert alerta["mensaje"].startswith("Falló el reporte del 29/10")
    assert "re-autorizar Gmail" in alerta["mensaje"]
    assert "scripts/autorizar_gmail.py" in alerta["mensaje"]
    # el texto propio ya lo explica: no se repite el error crudo
    assert alerta["detalle"] == ""


def test_failed_incierto_dice_revisar_enviados_antes_de_force():
    error = MENSAJE_INCIERTO
    assert error.startswith("estado incierto")
    alerta = evaluar_estado(_fila("FAILED", error), JUEVES, AHORA_UTC)
    assert alerta["mensaje"].startswith("Estado incierto del reporte del 29/10")
    assert "Enviados" in alerta["mensaje"]
    assert "--force" in alerta["mensaje"]
    assert alerta["nivel"] == "warning"
    assert alerta["detalle"] == ""


def test_failed_incierto_con_prefijo_del_job():
    alerta = evaluar_estado(_fila("FAILED", "estado incierto: ReadTimeout"), JUEVES, AHORA_UTC)
    assert alerta["mensaje"].startswith("Estado incierto del reporte del 29/10")


def test_estado_desconocido_nada():
    assert evaluar_estado(_fila("RARO"), JUEVES, AHORA_UTC) == {}


@pytest.mark.parametrize(
    "error",
    [
        f"MailerError: Gmail respondió 400 con token {SECRETO}",
        "OperationalError: connection to postgresql://usuario:clave@db.interno:5432/prod failed",
        "MailerError: no se pudo mandar desde negocio@ejemplo.com",
    ],
)
def test_mensaje_sin_secretos(error):
    alerta = evaluar_estado(_fila("FAILED", error), JUEVES, AHORA_UTC)
    texto = alerta["mensaje"] + " " + alerta["detalle"]
    assert SECRETO not in texto
    assert "clave" not in texto
    assert "db.interno" not in texto
    assert "negocio@ejemplo.com" not in texto
    assert len(alerta["detalle"]) <= 200


def test_updated_at_con_zona_se_compara_bien():
    fila = FilaEstado(status="PENDING", error=None, updated_at=datetime(2026, 10, 29, 12, 0, tzinfo=timezone.utc))
    assert evaluar_estado(fila, JUEVES, AHORA_UTC)["mensaje"].startswith("Estado incierto")


# --- estado_reporte (consulta + cache) ---------------------------------------


class Reloj:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


class ConexionFalsa:
    """Imita `engine.connect()`: context manager con `execute` y `dialect`."""

    def __init__(self, filas, registro):
        self._filas = filas
        self._registro = registro
        self.dialect = type("D", (), {"name": "sqlite"})()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self._registro.append((str(sql), params))
        fila = self._filas.get(params["esperado"]) if params else None

        class _Res:
            def first(_self):
                return fila

        return _Res()


@pytest.fixture
def conexion():
    """Fábrica `conectar` que cuenta cuántas veces se consultó la base."""

    class Fabrica:
        def __init__(self):
            self.filas = {}
            self.consultas = []
            self.aperturas = 0

        def __call__(self):
            self.aperturas += 1
            return ConexionFalsa(self.filas, self.consultas)

    return Fabrica()


def _ahora_art(dia, hora, minuto=0):
    from zoneinfo import ZoneInfo

    return datetime(2026, 10, dia, hora, minuto, tzinfo=ZoneInfo("America/Argentina/Buenos_Aires"))


def test_consulta_por_fecha_exacta_y_solo_select(conexion):
    estado_reporte(_ahora_art(29, 10), conectar=conexion, reloj=Reloj())
    selects = [(sql, p) for sql, p in conexion.consultas if p]
    assert len(selects) == 1
    sql, params = selects[0]
    assert sql.lstrip().upper().startswith("SELECT")
    assert "report_send_log" in sql
    assert params == {"key": "diario", "esperado": JUEVES}
    for sql, _ in conexion.consultas:
        for prohibido in ("INSERT", "UPDATE ", "DELETE", "CREATE", "ALTER", "DROP"):
            assert prohibido not in sql.upper()


def test_sin_fila_despues_de_las_9(conexion):
    alerta = estado_reporte(_ahora_art(29, 9, 1), conectar=conexion, reloj=Reloj())
    assert alerta["mensaje"].startswith("No se envió el reporte del 29/10")


def test_antes_de_las_9_mira_el_dia_anterior(conexion):
    conexion.filas[date(2026, 10, 28)] = FilaEstado("SENT", None, AHORA_UTC)
    assert estado_reporte(_ahora_art(29, 8, 59), conectar=conexion, reloj=Reloj()) == {}
    assert conexion.consultas[-1][1]["esperado"] == date(2026, 10, 28)


def test_sabado_mira_el_viernes(conexion):
    conexion.filas[date(2026, 10, 30)] = FilaEstado("SENT", None, AHORA_UTC)
    assert estado_reporte(_ahora_art(31, 11), conectar=conexion, reloj=Reloj()) == {}
    assert conexion.consultas[-1][1]["esperado"] == date(2026, 10, 30)


def test_cache_ttl(conexion):
    reloj = Reloj(1000.0)
    ahora = _ahora_art(29, 10)
    primera = estado_reporte(ahora, conectar=conexion, reloj=reloj)
    assert conexion.aperturas == 1
    # se envía bien, pero la cache tapa la recuperación hasta 5 min
    conexion.filas[JUEVES] = FilaEstado("SENT", None, AHORA_UTC)
    reloj.t += CACHE_TTL - 1
    assert estado_reporte(ahora, conectar=conexion, reloj=reloj) == primera
    assert conexion.aperturas == 1
    reloj.t += 2  # 301 s después de la primera
    assert estado_reporte(ahora, conectar=conexion, reloj=reloj) == {}
    assert conexion.aperturas == 2
    assert CACHE_TTL == 300


def test_cache_se_invalida_si_cambia_el_dia_esperado(conexion):
    reloj = Reloj()
    estado_reporte(_ahora_art(29, 8, 59), conectar=conexion, reloj=reloj)
    estado_reporte(_ahora_art(29, 9, 0), conectar=conexion, reloj=reloj)
    assert conexion.aperturas == 2


def test_cache_devuelve_copia(conexion):
    reloj = Reloj()
    alerta = estado_reporte(_ahora_art(29, 10), conectar=conexion, reloj=reloj)
    alerta["mensaje"] = "pisado"
    assert estado_reporte(_ahora_art(29, 10), conectar=conexion, reloj=reloj)["mensaje"] != "pisado"


def test_error_de_db_devuelve_vacio():
    def conectar():
        raise RuntimeError('relation "report_send_log" does not exist')

    assert estado_reporte(_ahora_art(29, 10), conectar=conectar, reloj=Reloj()) == {}


def test_error_en_execute_devuelve_vacio_y_se_cachea(conexion):
    llamadas = []

    class Rota(ConexionFalsa):
        def execute(self, sql, params=None):
            llamadas.append(sql)
            raise RuntimeError("UndefinedTable")

    reloj = Reloj()
    for _ in range(3):
        assert estado_reporte(_ahora_art(29, 10), conectar=lambda: Rota({}, []), reloj=reloj) == {}
    # una tabla inexistente no se re-consulta en cada request
    assert len(llamadas) == 1


def test_sin_conectar_y_sin_app_devuelve_vacio():
    # fuera de un app context `db.engine` lanza: nunca debe propagarse
    assert estado_reporte(_ahora_art(29, 10), reloj=Reloj()) == {}


def test_postgres_lee_en_transaccion_read_only_con_timeout(conexion):
    class Pg(ConexionFalsa):
        def __init__(self, *a):
            super().__init__(*a)
            self.dialect = type("D", (), {"name": "postgresql"})()

    registro = []
    estado_reporte(_ahora_art(29, 10), conectar=lambda: Pg({}, registro), reloj=Reloj())
    sentencias = [sql.strip().upper() for sql, _ in registro]
    assert sentencias[0] == "SET TRANSACTION READ ONLY"
    assert sentencias[1].startswith("SET LOCAL STATEMENT_TIMEOUT")
    assert sentencias[2].startswith("SELECT")


def test_modulo_no_importa_la_app_web():
    fuente = open(re_.__file__, encoding="utf-8").read()
    assert "backend.app" not in fuente
    assert "backend.views" not in fuente


# --- contra la base LOCAL ----------------------------------------------------


@pytest.mark.db
def test_consultar_fila_contra_la_base_local(sesion_tx):
    from sqlalchemy import text

    sesion_tx.execute(
        text(
            "INSERT INTO report_send_log (report_key, fecha_negocio, status, error, updated_at) "
            "VALUES ('diario', :f, 'FAILED', 'boom', :u)"
        ),
        {"f": date(1990, 1, 2), "u": AHORA_UTC},
    )
    fila = re_.consultar_fila(sesion_tx.connection(), date(1990, 1, 2))
    assert fila == FilaEstado("FAILED", "boom", AHORA_UTC)
    assert re_.consultar_fila(sesion_tx.connection(), date(1990, 1, 3)) is None


# --- filas 'prueba' del botón: el aviso las ignora (spec F) ------------------------


@pytest.mark.db
def test_fila_prueba_no_cuenta_para_el_aviso(sesion_tx):
    """Contra la base LOCAL, en la transacción descartable de `sesion_tx`."""
    from sqlalchemy import delete, text

    from backend.models import ReportSendLog
    from backend.services.reporte_diario import envios

    if not sesion_tx.execute(text("select to_regclass('public.report_send_log')")).scalar():
        pytest.skip("report_send_log no existe en la base local")
    fecha = date(1990, 1, 5)
    sesion_tx.execute(delete(ReportSendLog.__table__).where(ReportSendLog.report_key == "prueba"))
    sesion_tx.commit()

    # Solo una 'prueba' FAILED en la fecha esperada: para el aviso no hay fila.
    r = envios.reclamar_prueba(sesion_tx, fecha, destinatario="x@example.com")
    envios.registrar_falla(sesion_tx, r.id, "MailerError: 503")
    assert re_.consultar_fila(sesion_tx.connection(), fecha) is None

    # 'diario' FAILED + 'prueba' SENT: el aviso sigue mostrando la falla.
    d = envios.reclamar_envio(sesion_tx, fecha, force=False, destinatario="x@example.com")
    envios.registrar_falla(sesion_tx, d.id, "MailerError: 503 persistente")
    r2 = envios.reclamar_prueba(sesion_tx, fecha, destinatario="x@example.com")
    envios.registrar_envio(sesion_tx, r2.id, "id-prueba")

    fila = re_.consultar_fila(sesion_tx.connection(), fecha)
    assert fila.status == "FAILED"
    alerta = evaluar_estado(fila, fecha, datetime(1990, 1, 5, 13, 0))
    assert alerta["nivel"] == "danger" and alerta["mensaje"].startswith("Falló el reporte del 05/01")
