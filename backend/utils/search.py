"""Filtros de busqueda unificados para Client y Company.

Por que existe este modulo
--------------------------
Habia 6 implementaciones distintas de "buscar cliente" / "buscar empresa"
repartidas por backend/views/main.py, cada una con su propio criterio. La
misma query daba resultados distintos segun la pantalla. Aca queda UNA sola
definicion por entidad y todas las vistas la consumen.

OJO: la semantica de `nombre` esta INVERTIDA entre las dos entidades
--------------------------------------------------------------------
No existen columnas `razon_social` ni `nombre_fantasia`. Los labels de la UI
NO se corresponden con los nombres de las columnas:

    Entidad   Label UI                 Columna real
    -------   ---------------------    ------------
    Client    "Razon social"           apellido
    Client    "Nombre" (fantasia)      nombre
    Company   "Razon social"           nombre
    Company   "Marca" (fantasia)       marca

Es decir: `Client.nombre` es el nombre de FANTASIA, y `Company.nombre` es la
RAZON SOCIAL. Son cosas opuestas.

Por eso hay dos helpers separados y NO uno generico. Un helper generico que
asuma que `nombre` significa lo mismo en ambas entidades devuelve resultados
equivocados EN SILENCIO, sin levantar ninguna excepcion.

Acentos
-------
La busqueda es insensible a acentos. NO se usa la extension `unaccent` de
Postgres: no esta instalada y no se puede instalar (prohibido tocar el
esquema). Se resuelve con `translate()`, que es SQL estandar y no requiere
ninguna extension.

Se normalizan LOS DOS LADOS: la columna (en SQL, via `func.translate`) y el
termino buscado (en Python, via `str.translate`). Normalizar uno solo no
alcanza: buscar "Teran" no encontraria "Teran" con acento si la columna
queda sin normalizar, y buscar "Teran" con acento no encontraria "Teran"
sin acento si el termino queda sin normalizar.
"""

from sqlalchemy import func, or_

from ..models import Client, Company

# Mapa de normalizacion de acentos. Ambas cadenas DEBEN tener el mismo largo:
# translate() mapea caracter a caracter por posicion.
# Se incluyen las mayusculas acentuadas aunque siempre se aplica lower() antes,
# para que el mapa siga siendo correcto si alguien reusa estas constantes sin
# bajar a minuscula primero.
ACCENT_SOURCE = "aeiouunAEIOUUN"
ACCENTED_CHARS = "áéíóúüñÁÉÍÓÚÜÑ"

assert len(ACCENTED_CHARS) == len(ACCENT_SOURCE), "los mapas de translate() deben tener el mismo largo"

_PY_ACCENT_MAP = str.maketrans(ACCENTED_CHARS, ACCENT_SOURCE)


def unaccent_lower(col):
    """Devuelve la expresion SQL `translate(lower(col), acentos, sin_acentos)`.

    Equivalente a `unaccent(lower(col))` pero sin depender de la extension
    `unaccent`, que no esta instalada en esta base.
    """
    return func.translate(func.lower(col), ACCENTED_CHARS, ACCENT_SOURCE)


def normalize_term(q):
    """Normaliza el termino buscado del mismo modo que `unaccent_lower` hace
    con la columna: minusculas y sin acentos. Devuelve "" si no hay termino.
    """
    return (q or "").strip().lower().translate(_PY_ACCENT_MAP)


def client_search_filter(q):
    """Filtro de busqueda de clientes, insensible a mayusculas y acentos.

    Busca en:
      - Client.apellido -> la RAZON SOCIAL (label "Razon social")
      - Client.nombre   -> el nombre de FANTASIA (label "Nombre")
      - la concatenacion "apellido nombre", para que ande "Ferreyra Marcelo"

    Devuelve None si no hay termino de busqueda, para que el llamador pueda
    decidir no filtrar. NO aplica `archived` ni filtros de owner: eso es
    responsabilidad de cada vista, que tiene reglas de permisos propias.
    """
    term = normalize_term(q)
    if not term:
        return None

    pattern = f"%{term}%"
    nombre_completo = Client.apellido + " " + Client.nombre

    return or_(
        unaccent_lower(Client.apellido).like(pattern),
        unaccent_lower(Client.nombre).like(pattern),
        unaccent_lower(nombre_completo).like(pattern),
    )


def company_search_filter(q):
    """Filtro de busqueda de empresas, insensible a mayusculas y acentos.

    Busca en:
      - Company.nombre -> la RAZON SOCIAL (label "Razon social")
      - Company.marca  -> la MARCA / fantasia (label "Marca"), que es nullable

    Company.marca puede ser NULL. `translate(lower(NULL))` da NULL y
    `NULL LIKE patron` da NULL, que dentro de un OR se comporta como falso
    sin descartar la fila por las otras condiciones. Es el comportamiento
    correcto y no hace falta un coalesce().

    Devuelve None si no hay termino de busqueda. NO aplica `archived`: eso
    es responsabilidad de cada vista.
    """
    term = normalize_term(q)
    if not term:
        return None

    pattern = f"%{term}%"

    return or_(
        unaccent_lower(Company.nombre).like(pattern),
        unaccent_lower(Company.marca).like(pattern),
    )
