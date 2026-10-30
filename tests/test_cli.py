"""Comando `python -m backend.jobs.reporte_diario` (sin DB, sin red).

Se reemplazan con monkeypatch la sesión de DB, el claim/registro de envíos,
la lectura de datos, el render y el mailer. Lo que se prueba es la
orquestación: guard de fin de semana, opciones, idempotencia y exit codes.
"""
import logging
import subprocess
import sys
from contextlib import contextmanager
from datetime import date
from pathlib import Path

import pytest

import backend.jobs.reporte_diario as rd
from backend.services.mailer.base import EnvioIncierto, MailerError, TokenRevocadoError
from backend.services.reporte_diario.fechas import fin_de_semana
from backend.services.reporte_diario.modelo import Reporte

REPO = Path(__file__).resolve().parent.parent

JUEVES = date(2026, 10, 29)
SABADO = date(2026, 10, 31)
DOMINGO = date(2026, 11, 1)
PDF = b"%PDF-1.7 de prueba"
SECRETO = "1//refresh-token-super-secreto-xyz"


class MailerFalso:
    destinatario = "negocio@example.com"

    def __init__(self, error: Exception | None = None):
        self.error = error
        self.enviados = []

    def send(self, *, to, subject, body, attachments):
        self.enviados.append({"to": to, "subject": subject, "body": body, "attachments": list(attachments)})
        if self.error is not None:
            raise self.error
        return "id-gmail-123"


class Registro:
    """Lo que el job hizo contra la "base" falsa."""

    def __init__(self):
        self.sesiones_abiertas = 0
        self.mailers_construidos = 0
        self.claims = []
        self.enviados = []
        self.fallas = []
        self.lecturas = []
        self.renders = 0
        self.reclamo = rd.Reclamo(rd.RECLAMADO, id=7, attempts=1)
        self.mailer = MailerFalso()
        self.error_mailer: Exception | None = None
        self.error_claim: Exception | None = None
        self.error_render: Exception | None = None


@pytest.fixture
def job(monkeypatch):
    r = Registro()

    @contextmanager
    def abrir_sesion():
        r.sesiones_abiertas += 1
        yield "sesion-falsa"

    def get_mailer():
        if r.error_mailer is not None:
            raise r.error_mailer
        r.mailers_construidos += 1
        return r.mailer

    def reclamar_envio(session, fecha, *, force, destinatario):
        if r.error_claim is not None:
            raise r.error_claim
        r.claims.append({"fecha": fecha, "force": force, "destinatario": destinatario})
        return r.reclamo

    def armar_reporte(hoy, session=None):
        r.lecturas.append(hoy)
        return Reporte(hoy=hoy, domingo=fin_de_semana(hoy))

    def render_pdf(reporte, generado_en):
        assert generado_en.tzinfo is not None
        if r.error_render is not None:
            raise r.error_render
        r.renders += 1
        return PDF

    monkeypatch.setattr(rd, "abrir_sesion", abrir_sesion)
    monkeypatch.setattr(rd, "get_mailer", get_mailer)
    monkeypatch.setattr(rd, "reclamar_envio", reclamar_envio)
    monkeypatch.setattr(rd, "registrar_envio", lambda s, id_, message_id: r.enviados.append((id_, message_id)))
    monkeypatch.setattr(rd, "registrar_falla", lambda s, id_, error: r.fallas.append((id_, error)))
    monkeypatch.setattr(rd, "armar_reporte", armar_reporte)
    monkeypatch.setattr(rd, "render_pdf", render_pdf)
    monkeypatch.setattr(rd, "hoy_negocio", lambda: JUEVES)
    return r


def _hoy(monkeypatch, fecha):
    monkeypatch.setattr(rd, "hoy_negocio", lambda: fecha)


# --------------------------------------------------------------------------
#  Día hábil sin flags: flujo completo
# --------------------------------------------------------------------------

def test_dia_habil_envia_y_registra_sent(job, caplog):
    caplog.set_level(logging.INFO)

    assert rd.main([]) == 0

    assert job.claims == [{"fecha": JUEVES, "force": False, "destinatario": "negocio@example.com"}]
    assert job.lecturas == [JUEVES]
    (mail,) = job.mailer.enviados
    assert mail["to"] == "negocio@example.com"
    assert mail["subject"] == "Reporte diario – jue 29/10/2026"
    assert mail["body"] == "Adjunto el reporte del día."
    (adjunto,) = mail["attachments"]
    assert adjunto.filename == "reporte-diario-2026-10-29.pdf"
    assert adjunto.content == PDF
    assert job.enviados == [(7, "id-gmail-123")]
    assert job.fallas == []
    assert "2026-10-29" in caplog.text
    assert "id-gmail-123" in caplog.text


def test_log_tiene_conteos_por_seccion(job, caplog):
    caplog.set_level(logging.INFO)
    assert rd.main([]) == 0
    assert "atrasadas=0" in caplog.text
    assert "cumples_hoy=0" in caplog.text


# --------------------------------------------------------------------------
#  Fin de semana (A "Negativa a enviar en fin de semana")
# --------------------------------------------------------------------------

def test_sabado_sin_force_exit0_sin_mailer_ni_app(job, monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    _hoy(monkeypatch, SABADO)

    assert rd.main([]) == 0

    assert job.mailers_construidos == 0
    assert job.sesiones_abiertas == 0
    assert job.claims == []
    assert "no es día hábil" in caplog.text


def test_domingo_sin_force_config_incompleta_exit0(job, monkeypatch):
    _hoy(monkeypatch, DOMINGO)
    job.error_mailer = MailerError("faltan REPORTE_GMAIL_REFRESH_TOKEN")

    assert rd.main([]) == 0
    assert job.sesiones_abiertas == 0


def test_sabado_force_envia_asunto_sab(job, monkeypatch):
    _hoy(monkeypatch, SABADO)

    assert rd.main(["--force"]) == 0

    (mail,) = job.mailer.enviados
    assert mail["subject"] == "Reporte diario – sáb 31/10/2026"
    assert job.claims[0]["force"] is True


def test_sabado_dry_run_genera_pdf(job, monkeypatch, tmp_path):
    _hoy(monkeypatch, SABADO)
    out = tmp_path / "r.pdf"

    assert rd.main(["--dry-run", "--out", str(out)]) == 0

    assert out.read_bytes() == PDF
    assert job.lecturas == [SABADO]


# --------------------------------------------------------------------------
#  Opciones (G)
# --------------------------------------------------------------------------

def test_fecha_sin_force_ni_dry_run_exit2(job):
    assert rd.main(["--fecha", "2026-10-28"]) == 2
    assert job.sesiones_abiertas == 0
    assert job.mailers_construidos == 0


def test_dry_run_sin_out_exit2(job):
    assert rd.main(["--dry-run"]) == 2
    assert job.sesiones_abiertas == 0


@pytest.mark.parametrize("valor", ["2026-13-40", "29/10/2026", "20261029", "2026-1-5", ""])
def test_fecha_invalida_exit2(job, valor):
    assert rd.main(["--fecha", valor, "--dry-run", "--out", "/nunca/se/escribe.pdf"]) == 2
    assert job.sesiones_abiertas == 0


def test_fecha_con_force_usa_esa_fecha(job):
    assert rd.main(["--fecha", "2026-10-28", "--force"]) == 0
    assert job.claims[0]["fecha"] == date(2026, 10, 28)
    assert job.mailer.enviados[0]["subject"] == "Reporte diario – mié 28/10/2026"


def test_dry_run_no_construye_mailer_ni_toca_log(job, tmp_path):
    out = tmp_path / "vie.pdf"

    assert rd.main(["--dry-run", "--out", str(out), "--fecha", "2026-10-30"]) == 0

    assert job.mailers_construidos == 0
    assert job.claims == []
    assert job.enviados == [] and job.fallas == []
    assert job.lecturas == [date(2026, 10, 30)]
    assert out.read_bytes() == PDF


def test_dry_run_falla_de_lectura_exit1(job, monkeypatch, tmp_path):
    def explota(hoy, session=None):
        raise RuntimeError("se cayó la base")

    monkeypatch.setattr(rd, "armar_reporte", explota)
    out = tmp_path / "r.pdf"
    assert rd.main(["--dry-run", "--out", str(out)]) == 1
    assert not out.exists()


def test_out_sin_dry_run_guarda_copia_y_envia(job, tmp_path):
    out = tmp_path / "copia.pdf"

    assert rd.main(["--out", str(out)]) == 0

    assert out.read_bytes() == PDF
    assert len(job.mailer.enviados) == 1


# --------------------------------------------------------------------------
#  Configuración e idempotencia (E, F)
# --------------------------------------------------------------------------

def test_config_invalida_exit1_sin_claim(job):
    job.error_mailer = MailerError("Configuración del reporte incompleta: faltan REPORTE_MAIL_TO.")

    assert rd.main([]) == 1

    assert job.sesiones_abiertas == 0
    assert job.claims == []


def test_claim_salteado_sent_exit0(job, caplog):
    caplog.set_level(logging.INFO)
    job.reclamo = rd.Reclamo(rd.YA_ENVIADO, id=7, attempts=1)

    assert rd.main([]) == 0

    assert job.mailer.enviados == []
    assert job.lecturas == []
    assert "ya se envió" in caplog.text


def test_claim_salteado_pending_exit0_con_advertencia(job, caplog):
    caplog.set_level(logging.INFO)
    job.reclamo = rd.Reclamo(rd.EN_CURSO, id=7, attempts=1)

    assert rd.main([]) == 0

    assert job.mailer.enviados == []
    assert any(r.levelno == logging.WARNING and "PENDING" in r.getMessage() for r in caplog.records)


def test_claim_salteado_incierto_exit0_con_advertencia(job, caplog):
    caplog.set_level(logging.INFO)
    job.reclamo = rd.Reclamo(rd.INCIERTO, id=7, attempts=1)

    assert rd.main([]) == 0

    assert job.mailer.enviados == []
    # Ni lee, ni arma el PDF, ni pisa la fila: sigue FAILED "estado incierto".
    assert job.lecturas == [] and job.renders == 0
    assert job.enviados == [] and job.fallas == []
    assert any(r.levelno == logging.WARNING and "--force" in r.getMessage() for r in caplog.records)


def test_tabla_inexistente_mensaje_claro_exit1(job, caplog):
    class _Orig(Exception):
        sqlstate = "42P01"

    class _ProgrammingError(Exception):
        orig = _Orig()

    job.error_claim = _ProgrammingError("relation report_send_log does not exist")

    assert rd.main([]) == 1

    assert "falta la migración report_send_log" in caplog.text
    assert job.mailer.enviados == []


def test_falla_de_db_en_claim_exit1(job):
    job.error_claim = RuntimeError("conexión rechazada")
    assert rd.main([]) == 1
    assert job.mailer.enviados == []


# --------------------------------------------------------------------------
#  Fallas después del claim -> FAILED + exit 1
# --------------------------------------------------------------------------

def test_falla_render_registra_failed_exit1_sin_envio(job):
    job.error_render = ValueError("fuente rota")

    assert rd.main([]) == 1

    assert job.mailer.enviados == []
    ((id_, error),) = job.fallas
    assert id_ == 7
    assert "ValueError" in error and "fuente rota" in error
    assert job.enviados == []


def test_falla_lectura_registra_failed_exit1(job, monkeypatch):
    def explota(hoy, session=None):
        raise RuntimeError("timeout de la base")

    monkeypatch.setattr(rd, "armar_reporte", explota)
    assert rd.main([]) == 1
    assert job.mailer.enviados == []
    assert len(job.fallas) == 1


def test_token_revocado_registra_failed_con_reautorizar(job):
    job.mailer = MailerFalso(
        TokenRevocadoError("Token revocado: volver a correr scripts/autorizar_gmail.py")
    )

    assert rd.main([]) == 1

    ((_, error),) = job.fallas
    assert "scripts/autorizar_gmail.py" in error


def test_envio_incierto_registra_failed_estado_incierto(job):
    job.mailer = MailerFalso(EnvioIncierto("estado incierto: revisar Enviados antes de usar --force."))

    assert rd.main([]) == 1

    ((_, error),) = job.fallas
    assert error.startswith("estado incierto")


def test_envio_incierto_sin_prefijo_igual_queda_marcado(job):
    job.mailer = MailerFalso(EnvioIncierto("Gmail respondió 200 sin id"))

    assert rd.main([]) == 1

    ((_, error),) = job.fallas
    assert error.startswith(rd.PREFIJO_INCIERTO)


def test_error_se_trunca_a_2000(job):
    job.mailer = MailerFalso(MailerError("x" * 5000))
    assert rd.main([]) == 1
    ((_, error),) = job.fallas
    assert len(error) == 2000


def test_falla_al_registrar_failed_igual_exit1(job, monkeypatch):
    job.error_render = ValueError("roto")

    def registrar_falla(session, id_, error):
        raise RuntimeError("base caída")

    monkeypatch.setattr(rd, "registrar_falla", registrar_falla)
    assert rd.main([]) == 1


def test_log_no_contiene_refresh_token(monkeypatch, job, caplog, capsys):
    """Con el mailer REAL de gmail_api (sin red): el secreto nunca se loguea."""
    caplog.set_level(logging.DEBUG)
    from backend.services import mailer as mailer_pkg
    from backend.services.mailer.gmail_api import MENSAJE_TOKEN_REVOCADO, GmailApiMailer

    for nombre, valor in {
        "REPORTE_MAILER": "gmail_api",
        "REPORTE_GMAIL_CLIENT_ID": "cliente-id-falso",
        "REPORTE_GMAIL_CLIENT_SECRET": "client-secret-falso-abc",
        "REPORTE_GMAIL_REFRESH_TOKEN": SECRETO,
        "REPORTE_MAIL_FROM": "negocio@example.com",
        "REPORTE_MAIL_TO": "negocio@example.com",
    }.items():
        monkeypatch.setenv(nombre, valor)

    def send_revocado(self, **kwargs):
        raise TokenRevocadoError(MENSAJE_TOKEN_REVOCADO)

    monkeypatch.setattr(GmailApiMailer, "send", send_revocado)
    monkeypatch.setattr(rd, "get_mailer", mailer_pkg.get_mailer)

    assert rd.main([]) == 1

    salida = caplog.text + capsys.readouterr().out
    ((_, error),) = job.fallas
    for secreto in (SECRETO, "client-secret-falso-abc"):
        assert secreto not in salida
        assert secreto not in error
    assert "autorizar_gmail.py" in error


# --------------------------------------------------------------------------
#  Claim con --force sobre una fila SENT (sin DB: se mira el UPDATE emitido)
# --------------------------------------------------------------------------

class _Resultado:
    def __init__(self, valor):
        self.valor = valor

    def scalar(self):
        return self.valor

    def scalar_one(self):
        return self.valor

    def one(self):
        return self.valor


class SesionFalsa:
    """INSERT en conflicto -> SELECT de una fila SENT -> UPDATE del reclamo."""

    def __init__(self, fila):
        self.respuestas = [None, fila, fila.attempts + 1]
        self.sentencias = []
        self.commits = 0

    def execute(self, stmt):
        self.sentencias.append(stmt)
        return _Resultado(self.respuestas.pop(0))

    def commit(self):
        self.commits += 1

    def rollback(self):
        pass


def test_force_sobre_sent_limpia_message_id_y_sent_at_en_el_claim():
    from types import SimpleNamespace

    from sqlalchemy.dialects import postgresql

    fila = SimpleNamespace(id=7, status=rd.SENT, attempts=1, error=None)
    sesion = SesionFalsa(fila)

    r = rd.reclamar_envio(sesion, JUEVES, force=True, destinatario="negocio@example.com")

    assert (r.estado, r.id, r.attempts) == (rd.RECLAMADO, 7, 2)
    update = sesion.sentencias[-1]
    params = update.compile(dialect=postgresql.dialect()).params
    assert params["status"] == rd.PENDING
    assert params["message_id"] is None and params["sent_at"] is None and params["error"] is None
    assert sesion.commits == 1  # mismo commit que el reclamo


# --------------------------------------------------------------------------
#  Aislamiento (I "Módulos importados") y entrada por -m
# --------------------------------------------------------------------------

def test_job_no_importa_backend_app():
    script = (
        "import sys, backend.jobs.reporte_diario\n"
        "print(','.join(sorted(m for m in sys.modules if m == 'backend.app' or m.startswith('backend.views'))))"
    )
    res = subprocess.run([sys.executable, "-c", script], cwd=REPO, capture_output=True, text=True, timeout=60)
    assert res.returncode == 0, res.stderr
    assert res.stdout.strip() == ""


def test_modulo_ejecutable_help():
    res = subprocess.run(
        [sys.executable, "-m", "backend.jobs.reporte_diario", "--help"],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert res.returncode == 0, res.stderr
    for opcion in ("--fecha", "--dry-run", "--out", "--force"):
        assert opcion in res.stdout


def test_logging_silencia_el_detalle_de_fonttools():
    """fontTools.subset loguea en INFO cada glifo del PDF: taparía el log del cron."""
    script = (
        "import logging, backend.jobs.reporte_diario as rd\n"
        "rd._configurar_logging()\n"
        "print(logging.getLogger('fontTools.subset').getEffectiveLevel(),"
        " logging.getLogger('backend.jobs.reporte_diario').getEffectiveLevel())"
    )
    res = subprocess.run([sys.executable, "-c", script], cwd=REPO, capture_output=True, text=True, timeout=60)
    assert res.returncode == 0, res.stderr
    nivel_fonttools, nivel_job = map(int, res.stdout.split())
    assert nivel_fonttools >= logging.WARNING
    assert nivel_job == logging.INFO
