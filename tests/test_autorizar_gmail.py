"""Script de autorización de Gmail (una sola vez, local).

No se ejecuta contra Google: el flujo OAuth se reemplaza por uno falso. Se
prueba lo que puede romperse sin red: `--help`, de dónde salen las
credenciales del cliente, los parámetros del flujo (scope, loopback, offline +
consent) y que el refresh token salga UNA vez por stdout y nada más.
"""
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "autorizar_gmail.py"

CLIENT_ID = "id-falso-123.apps.googleusercontent.com"
CLIENT_SECRET = "SECRETO-FALSO-cliente"
REFRESH = "1//REFRESH-FALSO-nuevo"
SCOPE = "https://www.googleapis.com/auth/gmail.send"


@pytest.fixture(scope="module")
def script():
    spec = importlib.util.spec_from_file_location("autorizar_gmail", SCRIPT)
    modulo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(modulo)
    return modulo


class CredencialesFalsas:
    def __init__(self, refresh_token=REFRESH, scopes=(SCOPE,)):
        self.refresh_token = refresh_token
        self.token = "ya29.ACCESS-FALSO"
        self.granted_scopes = list(scopes)


class FlujoFalso:
    def __init__(self, credenciales=None):
        self.credenciales = credenciales or CredencialesFalsas()
        self.config = None
        self.scopes = None
        self.kwargs = None

    def fabrica(self, config, scopes):
        self.config, self.scopes = config, scopes
        return self

    def run_local_server(self, **kwargs):
        self.kwargs = kwargs
        return self.credenciales


def no_preguntar(_texto):
    raise AssertionError("no debía pedir el secreto por consola")


def test_help_funciona_sin_tocar_google():
    resultado = subprocess.run(
        [sys.executable, str(SCRIPT), "--help"], cwd=REPO, capture_output=True, text=True, timeout=60
    )

    assert resultado.returncode == 0, resultado.stderr
    assert "gmail.send" in resultado.stdout
    assert "--client-secrets" in resultado.stdout


def test_importar_no_carga_google_auth_oauthlib():
    codigo = (
        "import importlib.util, sys\n"
        f"spec = importlib.util.spec_from_file_location('a', {str(SCRIPT)!r})\n"
        "m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)\n"
        "print(any(k.startswith('google_auth_oauthlib') for k in sys.modules))\n"
    )
    resultado = subprocess.run([sys.executable, "-c", codigo], capture_output=True, text=True, timeout=60)

    assert resultado.stdout.strip() == "False", resultado.stderr


def test_scope_solo_gmail_send(script):
    assert script.SCOPES == [SCOPE]


def test_flujo_loopback_offline_consent_e_imprime_solo_el_token(script, capsys):
    flujo = FlujoFalso()
    env = {"REPORTE_GMAIL_CLIENT_ID": CLIENT_ID, "REPORTE_GMAIL_CLIENT_SECRET": CLIENT_SECRET}

    codigo = script.main([], env=env, crear_flujo=flujo.fabrica, pedir_secreto=no_preguntar)

    assert codigo == 0
    assert flujo.scopes == [SCOPE]
    instalada = flujo.config["installed"]
    assert instalada["client_id"] == CLIENT_ID
    assert instalada["client_secret"] == CLIENT_SECRET
    assert instalada["token_uri"] == "https://oauth2.googleapis.com/token"
    assert flujo.kwargs["port"] == 0  # loopback con puerto libre (OOB no existe más)
    assert flujo.kwargs["access_type"] == "offline"
    assert flujo.kwargs["prompt"] == "consent"

    salida = capsys.readouterr()
    assert salida.out.strip() == REFRESH  # stdout = SOLO el token (para copiarlo o redirigirlo)
    assert REFRESH not in salida.err
    assert CLIENT_SECRET not in salida.out + salida.err
    assert "ya29.ACCESS-FALSO" not in salida.out + salida.err
    for aviso in ("100", "contraseña", "REPORTE_GMAIL_REFRESH_TOKEN", "commit"):
        assert aviso in salida.err


def test_client_secrets_json_de_google_cloud(script, tmp_path, capsys):
    ruta = tmp_path / "client_secret.json"
    ruta.write_text(json.dumps({"installed": {"client_id": CLIENT_ID, "client_secret": CLIENT_SECRET}}))
    flujo = FlujoFalso()

    codigo = script.main(["--client-secrets", str(ruta)], env={}, crear_flujo=flujo.fabrica, pedir_secreto=no_preguntar)

    assert codigo == 0
    assert flujo.config["installed"]["client_id"] == CLIENT_ID
    assert flujo.config["installed"]["client_secret"] == CLIENT_SECRET
    assert capsys.readouterr().out.strip() == REFRESH


def test_client_secrets_json_de_tipo_web_se_rechaza(script, tmp_path, capsys):
    ruta = tmp_path / "client_secret.json"
    ruta.write_text(json.dumps({"web": {"client_id": CLIENT_ID, "client_secret": CLIENT_SECRET}}))

    codigo = script.main(["--client-secrets", str(ruta)], env={}, crear_flujo=FlujoFalso().fabrica)

    assert codigo == 2
    assert "Desktop app" in capsys.readouterr().err


def test_secreto_por_consola_si_falta(script, capsys):
    flujo = FlujoFalso()
    preguntas = []

    def preguntar(texto):
        preguntas.append(texto)
        return CLIENT_SECRET

    codigo = script.main(["--client-id", CLIENT_ID], env={}, crear_flujo=flujo.fabrica, pedir_secreto=preguntar)

    assert codigo == 0
    assert len(preguntas) == 1
    assert flujo.config["installed"]["client_secret"] == CLIENT_SECRET


@pytest.mark.parametrize(
    "argv",
    [["--client-secret", CLIENT_SECRET], [f"--client-secret={CLIENT_SECRET}"], ["--client-id", "x", "--client-secret", CLIENT_SECRET]],
)
def test_no_acepta_el_secreto_como_argumento_ni_lo_repite(script, argv, capsys):
    # Un secreto en la línea de comandos queda en el historial del shell y en `ps`.
    # Ojo: argparse por defecto toma "--client-secret" como abreviatura de "--client-secrets".
    flujo = FlujoFalso()

    codigo = script.main(argv, env={}, crear_flujo=flujo.fabrica, pedir_secreto=no_preguntar)

    salida = capsys.readouterr()
    assert codigo == 2
    assert flujo.config is None
    assert CLIENT_SECRET not in salida.out + salida.err


def test_no_acepta_abreviaturas(script):
    with pytest.raises(SystemExit) as exc:
        script.main(["--client-sec", "x.json"], env={}, crear_flujo=FlujoFalso().fabrica)
    assert exc.value.code == 2


def test_falta_client_id_sale_con_2_sin_abrir_el_flujo(script, capsys):
    flujo = FlujoFalso()

    codigo = script.main([], env={}, crear_flujo=flujo.fabrica, pedir_secreto=no_preguntar)

    assert codigo == 2
    assert flujo.config is None
    assert "REPORTE_GMAIL_CLIENT_ID" in capsys.readouterr().err


def test_sin_refresh_token_falla(script, capsys):
    flujo = FlujoFalso(CredencialesFalsas(refresh_token=None))
    env = {"REPORTE_GMAIL_CLIENT_ID": CLIENT_ID, "REPORTE_GMAIL_CLIENT_SECRET": CLIENT_SECRET}

    codigo = script.main([], env=env, crear_flujo=flujo.fabrica)

    salida = capsys.readouterr()
    assert codigo == 1
    assert salida.out == ""
    assert "refresh token" in salida.err


def test_scope_no_otorgado_falla(script, capsys):
    flujo = FlujoFalso(CredencialesFalsas(scopes=()))
    env = {"REPORTE_GMAIL_CLIENT_ID": CLIENT_ID, "REPORTE_GMAIL_CLIENT_SECRET": CLIENT_SECRET}

    codigo = script.main([], env=env, crear_flujo=flujo.fabrica)

    salida = capsys.readouterr()
    assert codigo == 1
    assert salida.out == ""
    assert "gmail.send" in salida.err
