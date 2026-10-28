import os
from urllib.parse import urlsplit

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


_MOTORES_POSTGRES = frozenset({"postgres", "postgresql"})


def _help_motor_no_soportado(motor: str) -> str:
    return (
        f"DATABASE_URL apunta a `{motor}` y ese motor no esta soportado.\n"
        "\n"
        "Este proyecto requiere PostgreSQL: el modelo usa aritmetica de fechas de\n"
        "Postgres (ver vencimiento_efectivo_expr() en backend/models.py), que en otros\n"
        "motores devuelve resultados silenciosamente incorrectos.\n"
        "\n"
        "Esquemas aceptados: postgres://, postgresql:// y postgresql+<driver>://\n"
        "(por ejemplo postgresql+psycopg:// o postgresql+psycopg2://).\n"
        "\n"
        "Usar PostgreSQL, por ejemplo:\n"
        "  DATABASE_URL=postgresql://usuario@localhost:5432/sirioarabia"
    )


def _dialecto_de(uri: str) -> str:
    """Devuelve el dialecto de la URI (el esquema sin el `+driver`), en minusculas.

    Se parsea el esquema en lugar de usar `startswith` para que `postgresqlfoo://`
    no cuele como Postgres y para que `postgresql+psycopg2://` (driver viejo) si lo
    haga.
    """
    return urlsplit(uri).scheme.lower().split("+", 1)[0]


def _validar_motor(uri: str) -> None:
    """Acepta solo Postgres. Cualquier otro motor (o ninguno) es error."""
    dialecto = _dialecto_de(uri)
    if dialecto in _MOTORES_POSTGRES:
        return
    if dialecto == "sqlite":
        raise RuntimeError(_HELP_SQLITE)
    raise RuntimeError(_help_motor_no_soportado(dialecto or "(sin esquema)"))


def _normalizar_uri_postgres(uri: str) -> str:
    """Normaliza el esquema para que SQLAlchemy use el driver psycopg3.

    Solo se toca cuando la URI no trae `+driver` explicito: si el usuario pidio
    `postgresql+psycopg2://` se respeta su eleccion.
    """
    esquema = urlsplit(uri).scheme
    if "+" in esquema:
        return uri
    return uri.replace(f"{esquema}://", "postgresql+psycopg://", 1)


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
        _validar_motor(database_url)

        self.SQLALCHEMY_DATABASE_URI = _normalizar_uri_postgres(database_url)
        self.SQLALCHEMY_TRACK_MODIFICATIONS = False
