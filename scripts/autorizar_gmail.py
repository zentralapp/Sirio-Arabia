#!/usr/bin/env python
"""Autoriza UNA VEZ la cuenta de Gmail del negocio y obtiene el refresh token del reporte diario.

Se corre en una máquina local con navegador (no en Railway):

    pip install -r requirements-dev.txt
    python scripts/autorizar_gmail.py --client-secrets client_secret.json

o con las variables REPORTE_GMAIL_CLIENT_ID / REPORTE_GMAIL_CLIENT_SECRET
exportadas (si falta el secreto, se pide por consola sin mostrarlo). El
secreto NO se acepta como argumento: quedaría en el historial del shell.

Abre el navegador (redirect por loopback a un puerto libre de localhost; el
flujo "OOB" de copiar/pegar un código ya no existe), se entra con la cuenta
GENERAL del negocio y se acepta el permiso "Enviar correo en tu nombre"
(scope gmail.send, nada más). Al terminar imprime por stdout SOLO el refresh
token, para copiarlo a REPORTE_GMAIL_REFRESH_TOKEN en Railway; los avisos van
por stderr. Se puede redirigir: `... > token.txt` (y borrar el archivo después).

`google-auth-oauthlib` es dependencia de desarrollo (requirements-dev.txt): se
importa recién al correr, nunca en producción.
"""
import argparse
import getpass
import json
import os
import sys
from pathlib import Path

SCOPES = ["https://www.googleapis.com/auth/gmail.send"]
AUTH_URI = "https://accounts.google.com/o/oauth2/auth"
TOKEN_URI = "https://oauth2.googleapis.com/token"

ENV_CLIENT_ID = "REPORTE_GMAIL_CLIENT_ID"
ENV_CLIENT_SECRET = "REPORTE_GMAIL_CLIENT_SECRET"

AVISOS = """
Listo. Arriba (stdout) quedó el refresh token: copialo a la variable
REPORTE_GMAIL_REFRESH_TOKEN del servicio cron en Railway.

Antes de cerrar esta terminal:
- NO lo pegues en el repo, en un commit, en un chat ni en un ticket.
- NO corras este script de más: Google guarda como máximo 100 refresh tokens
  por cuenta y cliente; el 101 invalida el más viejo sin avisar.
- Si se cambia la contraseña de la cuenta (o se quita el acceso de la app en
  "Apps con acceso a tu cuenta"), el token se revoca: hay que volver a correr
  este script y actualizar REPORTE_GMAIL_REFRESH_TOKEN.
- La app de OAuth tiene que estar publicada "In production": en "Testing" el
  token vence a los 7 días.
"""


class ErrorDeUso(Exception):
    pass


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="autorizar_gmail.py",
        allow_abbrev=False,  # que "--client-secret X" no se tome como "--client-secrets X"
        description=(
            "Autoriza una vez la cuenta de Gmail del negocio (scope gmail.send) e imprime el "
            "refresh token para REPORTE_GMAIL_REFRESH_TOKEN."
        ),
    )
    parser.add_argument(
        "--client-secrets",
        type=Path,
        metavar="RUTA.json",
        help="JSON del cliente OAuth tipo 'Desktop app' bajado de Google Cloud (no lo commitees).",
    )
    parser.add_argument(
        "--client-id",
        help=f"Client ID del cliente OAuth (default: ${ENV_CLIENT_ID}). El secreto se toma de "
        f"${ENV_CLIENT_SECRET} o se pide por consola.",
    )
    return parser


def config_cliente(args, env, pedir_secreto) -> dict:
    """Config de cliente "installed" (Desktop app) para InstalledAppFlow."""
    if args.client_secrets:
        try:
            datos = json.loads(args.client_secrets.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ErrorDeUso(f"No se pudo leer {args.client_secrets}: {type(exc).__name__}.") from None
        instalada = datos.get("installed") if isinstance(datos, dict) else None
        if not isinstance(instalada, dict):
            raise ErrorDeUso("El JSON no es de un cliente OAuth tipo 'Desktop app' (falta la clave 'installed').")
        client_id = instalada.get("client_id")
        client_secret = instalada.get("client_secret")
    else:
        client_id = (args.client_id or env.get(ENV_CLIENT_ID) or "").strip()
        if not client_id:
            raise ErrorDeUso(f"Falta el client ID: usá --client-secrets o {ENV_CLIENT_ID} / --client-id.")
        client_secret = (env.get(ENV_CLIENT_SECRET) or "").strip() or pedir_secreto(
            "Client secret (no se muestra): "
        ).strip()

    if not client_id or not client_secret:
        raise ErrorDeUso("Faltan client_id o client_secret del cliente OAuth.")
    return {
        "installed": {
            "client_id": client_id,
            "client_secret": client_secret,
            "auth_uri": AUTH_URI,
            "token_uri": TOKEN_URI,
            "redirect_uris": ["http://localhost"],
        }
    }


def _flujo_real(config, scopes):
    from google_auth_oauthlib.flow import InstalledAppFlow  # dev-only

    return InstalledAppFlow.from_client_config(config, scopes=scopes)


def _trae_secreto_en_argumentos(argv) -> bool:
    return any(a == "--client-secret" or a.startswith("--client-secret=") for a in argv)


def main(argv=None, *, env=None, crear_flujo=_flujo_real, pedir_secreto=getpass.getpass) -> int:
    argv = sys.argv[1:] if argv is None else list(argv)
    err = sys.stderr
    if _trae_secreto_en_argumentos(argv):
        # Se corta ANTES de argparse, que repetiría el valor en el mensaje de error.
        print(
            f"Error: el client secret no se pasa como argumento (queda en el historial). "
            f"Usá --client-secrets RUTA.json, ${ENV_CLIENT_SECRET} o dejá que lo pida por consola.",
            file=err,
        )
        return 2
    args = _parser().parse_args(argv)
    env = os.environ if env is None else env

    try:
        config = config_cliente(args, env, pedir_secreto)
    except ErrorDeUso as exc:
        print(f"Error: {exc}", file=err)
        return 2

    print("Se abre el navegador: entrá con la cuenta GENERAL del negocio y aceptá el permiso.", file=err)
    flujo = crear_flujo(config, SCOPES)
    credenciales = flujo.run_local_server(
        port=0,  # loopback en un puerto libre
        access_type="offline",  # para que haya refresh token
        prompt="consent",  # fuerza un refresh token nuevo aunque ya se haya autorizado antes
        authorization_prompt_message="Si no se abrió solo, abrí esta URL: {url}",
        success_message="Autorización completa. Ya podés cerrar esta pestaña y volver a la terminal.",
    )

    otorgados = getattr(credenciales, "granted_scopes", None)
    if otorgados is not None and SCOPES[0] not in otorgados:
        print("Error: Google no otorgó el permiso gmail.send (¿se destildó en la pantalla?). Repetí.", file=err)
        return 1
    if not credenciales.refresh_token:
        print("Error: Google no devolvió un refresh token. Revocá el acceso de la app y repetí.", file=err)
        return 1

    print(credenciales.refresh_token)
    print(AVISOS, file=err)
    return 0


if __name__ == "__main__":
    sys.exit(main())
