"""Lectura de la base para el reporte diario (SOLO LECTURA, sin N+1).

Cuatro queries en total:
  A. cobranzas vigentes sin cobrar (vencimiento y estado de la fuente única
     de models.py: `condiciones_estado_cobranza`);
  B. saldos agregados de esos pedidos (`saldos_por_pedido`);
  C. cumpleaños con fecha de clientes no archivados (+ su cliente);
  D. vínculos cliente-empresa de esos clientes con su empresa (selectinload).

`armar_reporte(hoy)` abre una transacción READ ONLY (primera sentencia), lee,
hace rollback y clasifica en Python con las funciones puras de clasificar.py.

Alcance GLOBAL: sin filtro por owner_user_id ni ClientAlertState. Clientes
archivados: sus cobranzas SÍ entran; sus cumpleaños NO.
"""
from contextlib import contextmanager
from datetime import date, datetime

from sqlalchemy import case, select, text
from sqlalchemy.orm import Session, contains_eager, joinedload, selectinload

from backend.extensions import db
from backend.models import (
    Client,
    ClientBirthday,
    ClientCompanyLink,
    Collection,
    Company,
    LogisticsStatus,
    Order,
    RelationStatus,
    condiciones_estado_cobranza,
)
from backend.services.reporte_diario.clasificar import componer_reporte
from backend.services.reporte_diario.modelo import (
    A_COBRAR,
    ATRASADO,
    EN_CAMINO,
    FilaCobranza,
    FilaCumple,
    Reporte,
)
from backend.services.saldos import saldos_por_pedido


# --------------------------------------------------------------------------
#  Nombres para mostrar: réplica pura de Client.display_name y
#  Company.display_name (models.py) para usarlos sobre columnas sueltas.
# --------------------------------------------------------------------------

def nombre_cliente(apellido, nombre) -> str:
    """"Razón social (Fantasía)"; sin fantasía, solo la razón social."""
    razon_social = (apellido or "").strip()
    fantasia = (nombre or "").strip()
    if not fantasia:
        return razon_social
    return f"{razon_social} ({fantasia})"


def nombre_empresa(nombre, marca) -> str:
    """"Razón social (Marca)"; sin marca (o el literal "none"), solo la razón social."""
    razon_social = (nombre or "").strip()
    marca = (marca or "").strip()
    if not marca or marca.lower() == "none":
        return razon_social
    return f"{razon_social} ({marca})"


# --------------------------------------------------------------------------
#  Transacción de solo lectura
# --------------------------------------------------------------------------

@contextmanager
def solo_lectura(session: Session):
    """Transacción READ ONLY: cualquier escritura la rechaza Postgres.

    `SET TRANSACTION READ ONLY` tiene que ser la PRIMERA sentencia de la
    transacción; por eso se exige que la sesión no tenga una abierta (el claim
    del envío commitea antes). Siempre termina con rollback.
    """
    if session.in_transaction():
        raise RuntimeError(
            "La lectura del reporte tiene que arrancar en una transacción nueva "
            "(commit o rollback antes de llamar a armar_reporte)."
        )
    try:
        session.execute(text("SET TRANSACTION READ ONLY"))
        yield session
    finally:
        session.rollback()


# --------------------------------------------------------------------------
#  Queries
# --------------------------------------------------------------------------

def _fecha(valor) -> date | None:
    return valor.date() if isinstance(valor, datetime) else valor


def leer_cobranzas(session: Session, hoy: date) -> list[FilaCobranza]:
    """Pedidos vigentes sin cobrar (con o sin Collection), con su saldo."""
    c = condiciones_estado_cobranza(hoy)
    estado = case((c.atrasado, ATRASADO), (c.a_cobrar, A_COBRAR), else_=EN_CAMINO)
    q = (
        select(
            Order.id,
            Order.client_id,
            Order.company_id,
            Client.apellido,
            Client.nombre,
            Company.nombre.label("empresa"),
            Company.marca,
            c.vencimiento.label("venc"),
            estado.label("estado"),
        )
        .select_from(Order)
        .outerjoin(Collection, Collection.order_id == Order.id)
        .outerjoin(LogisticsStatus, LogisticsStatus.order_id == Order.id)
        .join(Client, Client.id == Order.client_id)
        .join(Company, Company.id == Order.company_id)
        .where(Order.deleted_at.is_(None), Collection.fecha_cobro_efectiva.is_(None))
        .order_by(Order.id)
    )
    filas = session.execute(q).all()
    saldos = saldos_por_pedido([f.id for f in filas], session=session)
    return [
        FilaCobranza(
            order_id=f.id,
            cliente=nombre_cliente(f.apellido, f.nombre),
            empresa=nombre_empresa(f.empresa, f.marca),
            vencimiento=_fecha(f.venc),
            estado=f.estado,
            saldo=saldos[f.id],
            client_id=f.client_id,
            company_id=f.company_id,
        )
        for f in filas
    ]


def _empresa_representativa(links) -> str:
    """Primer vínculo TRABAJA (por id); si no hay, el primero con empresa."""
    con_empresa = sorted((l for l in links if l.company is not None), key=lambda l: l.id)
    for link in con_empresa:
        if link.status == RelationStatus.TRABAJA:
            return nombre_empresa(link.company.nombre, link.company.marca)
    if con_empresa:
        return nombre_empresa(con_empresa[0].company.nombre, con_empresa[0].company.marca)
    return ""


def leer_cumples(session: Session) -> list[FilaCumple]:
    """Cumpleaños con fecha de clientes NO archivados (2 queries)."""
    q = (
        select(ClientBirthday)
        .join(ClientBirthday.client)
        .where(ClientBirthday.fecha.isnot(None), Client.archived.is_(False))
        .options(
            contains_eager(ClientBirthday.client)
            .selectinload(Client.links)
            .joinedload(ClientCompanyLink.company)
        )
        .order_by(ClientBirthday.id)
    )
    return [
        FilaCumple(
            nombre=(b.nombre or "").strip(),
            puesto=(b.puesto or "").strip(),
            cliente=nombre_cliente(b.client.apellido, b.client.nombre),
            empresa=_empresa_representativa(b.client.links),
            fecha=b.fecha,
        )
        for b in session.scalars(q).all()
    ]


def armar_reporte(hoy: date, session: Session | None = None) -> Reporte:
    """Lee todo en una transacción READ ONLY y devuelve el Reporte clasificado."""
    session = session if session is not None else db.session()  # Session real, no el proxy
    with solo_lectura(session):
        cobranzas = leer_cobranzas(session, hoy)
        cumples = leer_cumples(session)
    return componer_reporte(hoy, cobranzas, cumples)
