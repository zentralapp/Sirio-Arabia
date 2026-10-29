"""Modo local: escribe el mail completo como `.eml` en disco (nunca contacta a Google).

El archivo se llama como el adjunto con extensión `.eml`
(`reporte-diario-2026-10-29.eml`) y se reescribe si ya existe (una corrida
con --force pisa la anterior). Se abre con cualquier cliente de mail.
"""
from pathlib import Path, PurePath
from typing import Sequence

from backend.services.mailer.base import Adjunto, armar_mensaje


class ArchivoMailer:
    def __init__(self, *, directorio: Path, remitente_nombre: str, remitente: str, destinatario: str):
        self.directorio = Path(directorio)
        self.remitente_nombre = remitente_nombre
        self.remitente = remitente
        self.destinatario = destinatario

    def send(self, *, to: str, subject: str, body: str, attachments: Sequence[Adjunto]) -> str:
        msg = armar_mensaje(
            remitente_nombre=self.remitente_nombre,
            remitente=self.remitente,
            to=to,
            subject=subject,
            body=body,
            attachments=attachments,
        )
        # Solo el nombre base: un filename con "../" no saca el archivo del directorio.
        nombre = PurePath(attachments[0].filename.replace("\\", "/")).stem or "reporte"
        self.directorio.mkdir(parents=True, exist_ok=True)
        ruta = self.directorio / f"{nombre}.eml"
        ruta.write_bytes(msg.as_bytes())
        return f"archivo:{ruta}"
