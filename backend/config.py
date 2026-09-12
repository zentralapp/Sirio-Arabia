import os

_HELP_DATABASE_URL = (
    "DATABASE_URL no esta definida.\n"
    "\n"
    "Este proyecto requiere PostgreSQL. No hay base por defecto ni fallback a SQLite:\n"
    "el modelo usa aritmetica de fechas de Postgres (ver vencimiento_efectivo_expr()\n"
    "en backend/models.py), que en otros motores devuelve resultados silenciosamente\n"
    "incorrectos.\n"
    "\n"
    "Para desarrollo local:\n"
    "  1. Copiar .env.example a .env\n"
    "  2. Definir DATABASE_URL, por ejemplo:\n"
    "     DATABASE_URL=postgresql://usuario@localhost:5432/sirioarabia\n"
    "\n"
    "En Railway la variable la inyecta el plugin de Postgres del servicio."
)

_HELP_SQLITE = (
    "DATABASE_URL apunta a SQLite y SQLite no esta soportado.\n"
    "\n"
    "El calculo de vencimiento hace `date(...) + plazo_en_dias`, que en Postgres suma\n"
    "dias y en SQLite hace aritmetica numerica sobre texto (date('2026-09-01') + 30\n"
    "devuelve 2056, sin error). Vencimientos, buckets y filtros darian distinto.\n"
    "\n"
    "Usar PostgreSQL, por ejemplo:\n"
    "  DATABASE_URL=postgresql://usuario@localhost:5432/sirioarabia"
)


def _normalizar_uri_postgres(uri: str) -> str:
    """Normaliza el esquema para que SQLAlchemy use el driver psycopg3."""
    if uri.startswith("postgres://"):
        return uri.replace("postgres://", "postgresql+psycopg://", 1)
    if uri.startswith("postgresql://"):
        return uri.replace("postgresql://", "postgresql+psycopg://", 1)
    return uri


class Config:
    """Configuracion de la app.

    Los valores se resuelven en __init__, NO en el cuerpo de la clase: si se
    leyeran como atributos de clase quedarian congelados en el import de
    `backend.config`, que ocurre antes del `load_dotenv()` de
    `backend.app.create_app()`. Leerlos aca garantiza que el `.env` ya este
    cargado cuando se instancia la config.
    """

    def __init__(self) -> None:
        self.SECRET_KEY = os.getenv("SECRET_KEY", "dev-secret-key")

        database_url = (os.getenv("DATABASE_URL") or "").strip()
        if not database_url:
            raise RuntimeError(_HELP_DATABASE_URL)
        if database_url.startswith("sqlite"):
            raise RuntimeError(_HELP_SQLITE)

        self.SQLALCHEMY_DATABASE_URI = _normalizar_uri_postgres(database_url)
        self.SQLALCHEMY_TRACK_MODIFICATIONS = False
