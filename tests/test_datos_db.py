"""Lectura de datos del reporte contra la base LOCAL (marca `db`).

Cada test carga sus propias filas dentro de una transacción que se descarta
(fixture `sesion_tx`): no queda nada en la base. Los tests de `armar_reporte`
solo LEEN los datos existentes.
"""
import uuid
from datetime import date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import event, func, select, text
from sqlalchemy.exc import DBAPIError

from backend.models import (
    AppUser,
    Client,
    ClientAlertState,
    ClientBirthday,
    ClientCompanyLink,
    Collection,
    CollectionDraft,
    CollectionPayment,
    Company,
    LogisticsStatus,
    Order,
    RelationStatus,
)
from backend.services.reporte_diario.clasificar import clasificar_cobranzas
from backend.services.reporte_diario.fechas import fin_de_semana
from backend.services.reporte_diario.datos import (
    armar_reporte,
    leer_cobranzas,
    leer_cumples,
    solo_lectura,
)
from backend.services.reporte_diario.modelo import A_COBRAR, ATRASADO, EN_CAMINO, Reporte
from backend.services.saldos import saldos_por_pedido

pytestmark = pytest.mark.db

D = Decimal
HOY = date(2026, 10, 29)


def _marca() -> str:
    return f"PRUEBA TEST {uuid.uuid4().hex[:8]}"


@pytest.fixture
def base(sesion_tx):
    """Cliente y empresa de prueba (dentro de la transacción descartable)."""
    m = _marca()
    cliente = Client(apellido=f"{m} Razon", nombre="Fantasia", archived=False)
    empresa = Company(nombre=f"{m} SA", marca="Marca", plazo_pago_promedio_dias=30)
    sesion_tx.add_all([cliente, empresa])
    sesion_tx.flush()
    return sesion_tx, cliente, empresa


def _pedido(sesion, cliente, empresa, *, precio=None, deleted_at=None, coll=None, log=None, pagos=()):
    pedido = Order(client_id=cliente.id, company_id=empresa.id, precio_final=precio, deleted_at=deleted_at)
    sesion.add(pedido)
    sesion.flush()
    if coll is not None:
        sesion.add(Collection(order_id=pedido.id, **coll))
    if log is not None:
        sesion.add(LogisticsStatus(order_id=pedido.id, **log))
    for kind, monto, anulado in pagos:
        sesion.add(
            CollectionPayment(
                order_id=pedido.id,
                kind=kind,
                amount=D(monto),
                voided_at=datetime(2026, 10, 1) if anulado else None,
            )
        )
    sesion.flush()
    return pedido


def _por_id(filas):
    return {f.order_id: f for f in filas}


# ---------------------------------------------------------------- saldos


def test_saldos_por_pedido_vacio_no_consulta(db_ctx):
    consultas = []

    def _registrar(conn, cursor, statement, *args):
        consultas.append(statement)

    event.listen(db_ctx.engine, "before_cursor_execute", _registrar)
    try:
        assert saldos_por_pedido([]) == {}
    finally:
        event.remove(db_ctx.engine, "before_cursor_execute", _registrar)
    assert consultas == []


def test_saldos_pagos_nc_borradores_y_anulados(base):
    sesion, cliente, empresa = base
    pedido = _pedido(
        sesion, cliente, empresa,
        precio=D("1000"),
        pagos=[
            ("PAYMENT", "300", False),
            ("CREDIT_NOTE", "-100", False),
            ("PAYMENT", "500", True),   # anulado: no cuenta
            ("DRAFT", "200", False),    # borrador: no cuenta
        ],
    )

    s = saldos_por_pedido([pedido.id], session=sesion)[pedido.id]

    assert (s.monto, s.pagado, s.nc, s.saldo) == (D("1000"), D("300"), D("100"), D("600"))


def test_saldos_precedencia_del_monto_base(base):
    sesion, cliente, empresa = base
    pedido = _pedido(
        sesion, cliente, empresa,
        precio=D("1000"),
        coll={"monto": D("0")},
        log={"precio": D("1200")},
    )
    sin_monto = _pedido(sesion, cliente, empresa)

    saldos = saldos_por_pedido([pedido.id, sin_monto.id], session=sesion)

    assert saldos[pedido.id].monto == D("1000")
    assert saldos[sin_monto.id].monto == D("0")
    assert saldos[sin_monto.id].saldo == D("0")


# ---------------------------------------------------------------- cobranzas


def test_universo_de_cobranzas(base):
    sesion, cliente, empresa = base
    archivado = Client(apellido=f"{_marca()} Archivado", nombre="", archived=True)
    sesion.add(archivado)
    sesion.flush()

    vence_hoy = datetime(2026, 10, 29)
    sin_collection = _pedido(
        sesion, cliente, empresa, precio=D("1000"),
        log={"fecha_entrega_efectiva": datetime(2026, 9, 29)},  # + 30 días = 29/10
    )
    borrado = _pedido(sesion, cliente, empresa, precio=D("1000"), deleted_at=datetime(2026, 10, 1),
                      coll={"fecha_pago_estimada": vence_hoy})
    cobrado = _pedido(sesion, cliente, empresa, precio=D("1000"),
                      coll={"fecha_pago_estimada": vence_hoy, "fecha_cobro_efectiva": datetime(2026, 10, 20)})
    de_archivado = _pedido(sesion, archivado, empresa, precio=D("1000"),
                           coll={"fecha_pago_estimada": datetime(2026, 10, 19)})
    en_camino = _pedido(sesion, cliente, empresa, precio=D("500"),
                        log={"fecha_entrega_estimada": datetime(2026, 11, 20)})

    filas = _por_id(leer_cobranzas(sesion, HOY))

    assert borrado.id not in filas
    assert cobrado.id not in filas

    f = filas[sin_collection.id]
    assert f.vencimiento == HOY
    assert f.estado == A_COBRAR
    assert f.saldo.monto == D("1000")
    assert f.cliente == cliente.apellido + " (Fantasia)"
    assert f.empresa == empresa.nombre + " (Marca)"

    a = filas[de_archivado.id]
    assert a.estado == ATRASADO
    assert a.vencimiento == date(2026, 10, 19)
    assert a.cliente == archivado.apellido  # sin fantasía: sin paréntesis

    c = filas[en_camino.id]
    assert c.estado == EN_CAMINO
    assert c.vencimiento == date(2026, 12, 20)


def test_cobranza_sin_vencimiento(base):
    sesion, cliente, empresa = base
    pedido = _pedido(sesion, cliente, empresa, precio=D("1000"), coll={})

    f = _por_id(leer_cobranzas(sesion, HOY))[pedido.id]

    assert f.vencimiento is None
    assert f.estado == EN_CAMINO


def _secciones(sesion):
    """{nombre de sección: {order_id, ...}} con la clasificación real del reporte."""
    secciones = clasificar_cobranzas(leer_cobranzas(sesion, HOY), HOY, fin_de_semana(HOY))
    return {nombre: {f.order_id for f in filas} for nombre, filas in secciones.items()}


def _donde(secciones, order_id):
    return sorted(nombre for nombre, ids in secciones.items() if order_id in ids)


def test_universo_ignora_owner_y_alerta_silenciada(base):
    """C "Pedido de otro usuario / alerta silenciada": el alcance es global."""
    sesion, cliente, empresa = base
    otro = AppUser(username=f"prueba-{uuid.uuid4().hex[:10]}", password_hash="x", is_admin=False)
    sesion.add(otro)
    sesion.flush()

    vencida = {"fecha_pago_estimada": datetime(2026, 10, 19)}
    de_otro = _pedido(sesion, cliente, empresa, precio=D("1000"), coll=vencida)
    de_otro.owner_user_id = otro.id
    silenciada = _pedido(sesion, cliente, empresa, precio=D("2000"), coll=vencida)
    silenciada.owner_user_id = otro.id
    sesion.add(
        ClientAlertState(
            client_id=cliente.id,
            order_id=silenciada.id,
            owner_user_id=otro.id,
            kind="COBRANZA",
            dismissed_at=datetime(2026, 10, 20),
            snoozed_until=datetime(2027, 1, 1),
        )
    )
    sesion.flush()

    secciones = _secciones(sesion)

    assert _donde(secciones, de_otro.id) == ["atrasadas"]
    assert _donde(secciones, silenciada.id) == ["atrasadas"]


def test_solo_collection_draft_no_es_parcial(base):
    """C Pagos parciales "Solo CollectionDraft": un borrador nunca es plata."""
    sesion, cliente, empresa = base
    pedido = _pedido(sesion, cliente, empresa, precio=D("1000"),
                     coll={"fecha_pago_estimada": datetime(2026, 10, 31)})
    sesion.add(CollectionDraft(client_id=cliente.id, company_id=empresa.id, order_id=pedido.id, notes="borrador"))
    sesion.flush()

    fila = _por_id(leer_cobranzas(sesion, HOY))[pedido.id]
    secciones = _secciones(sesion)

    assert (fila.saldo.pagado, fila.saldo.nc, fila.saldo.saldo) == (D("0"), D("0"), D("1000"))
    assert _donde(secciones, pedido.id) == ["vencen_semana"]


def _pago(sesion, pedido, kind, method, monto):
    sesion.add(CollectionPayment(order_id=pedido.id, kind=kind, method=method, amount=D(monto)))
    sesion.flush()


def test_nota_de_credito_como_la_graba_la_app_cuenta_como_nc(base):
    """La app graba las NC con kind=CREDIT_NOTE y method="NC" (monto positivo)."""
    sesion, cliente, empresa = base
    vencida = {"fecha_pago_estimada": datetime(2026, 10, 19)}
    parcial = _pedido(sesion, cliente, empresa, precio=D("1000"), coll=vencida)
    _pago(sesion, parcial, "CREDIT_NOTE", "NC", "200")
    saldada = _pedido(sesion, cliente, empresa, precio=D("1000"), coll=vencida)
    _pago(sesion, saldada, "PAYMENT", "TRANSFERENCIA", "600")
    _pago(sesion, saldada, "CREDIT_NOTE", "NC", "400")

    filas = _por_id(leer_cobranzas(sesion, HOY))
    secciones = _secciones(sesion)

    p = filas[parcial.id].saldo
    assert (p.pagado, p.nc, p.saldo) == (D("0"), D("200"), D("800"))
    assert _donde(secciones, parcial.id) == ["atrasadas", "parciales"]
    s_ = filas[saldada.id].saldo
    assert (s_.pagado, s_.nc, s_.saldo) == (D("600"), D("400"), D("0"))
    assert _donde(secciones, saldada.id) == ["pagadas_sin_registrar"]


def test_payment_con_method_nc_cuenta_como_pagado(base):
    """Glosario: se clasifica por `kind`, NO por `method`. Un PAYMENT con
    method "NC" va a Pagado (el saldo es el mismo)."""
    sesion, cliente, empresa = base
    pedido = _pedido(sesion, cliente, empresa, precio=D("1000"),
                     coll={"fecha_pago_estimada": datetime(2026, 10, 19)})
    _pago(sesion, pedido, "PAYMENT", "NC", "300")

    s_ = _por_id(leer_cobranzas(sesion, HOY))[pedido.id].saldo

    assert (s_.pagado, s_.nc, s_.saldo) == (D("300"), D("0"), D("700"))
    assert _donde(_secciones(sesion), pedido.id) == ["atrasadas", "parciales"]


# ---------------------------------------------------------------- cumpleaños


def test_cumples_filtra_archivados_y_sin_fecha_y_elige_empresa(base):
    sesion, cliente, empresa = base
    m = _marca()
    otra = Company(nombre=f"{m} Otra", marca="none")
    archivado = Client(apellido=f"{m} Arch", nombre="X", archived=True)
    sesion.add_all([otra, archivado])
    sesion.flush()
    # El primer vínculo NO es TRABAJA: tiene que ganar el TRABAJA.
    sesion.add_all([
        ClientCompanyLink(client_id=cliente.id, company_id=empresa.id, status=RelationStatus.TRABAJABA),
        ClientCompanyLink(client_id=cliente.id, company_id=otra.id, status=RelationStatus.TRABAJA),
        ClientCompanyLink(client_id=archivado.id, company_id=empresa.id, status=RelationStatus.TRABAJA),
    ])
    sesion.add_all([
        ClientBirthday(client_id=cliente.id, nombre=f"{m} Ana", puesto="Compras", fecha=date(1985, 10, 29)),
        ClientBirthday(client_id=cliente.id, nombre=f"{m} Sin Fecha", puesto=None, fecha=None),
        ClientBirthday(client_id=archivado.id, nombre=f"{m} Archivada", puesto=None, fecha=date(1990, 10, 29)),
    ])
    sesion.flush()

    mias = [c for c in leer_cumples(sesion) if c.nombre.startswith(m)]

    assert [c.nombre for c in mias] == [f"{m} Ana"]
    ana = mias[0]
    assert ana.empresa == otra.nombre  # marca "none" no se muestra
    assert ana.cliente == f"{cliente.apellido} (Fantasia)"
    assert ana.puesto == "Compras"
    assert ana.fecha == date(1985, 10, 29)


def test_cumples_sin_trabaja_toma_el_primer_vinculo(base):
    sesion, cliente, empresa = base
    m = _marca()
    otra = Company(nombre=f"{m} Segunda")
    sesion.add(otra)
    sesion.flush()
    sesion.add_all([
        ClientCompanyLink(client_id=cliente.id, company_id=empresa.id, status=RelationStatus.TRABAJABA),
        ClientCompanyLink(client_id=cliente.id, company_id=otra.id, status=RelationStatus.A_INCORPORAR),
        ClientBirthday(client_id=cliente.id, nombre=f"{m} Beto", puesto=None, fecha=date(1980, 1, 2)),
    ])
    sesion.flush()

    beto = next(c for c in leer_cumples(sesion) if c.nombre == f"{m} Beto")

    assert beto.empresa == f"{empresa.nombre} (Marca)"
    assert beto.puesto == ""


def test_cumples_cliente_sin_vinculos_empresa_vacia(base):
    sesion, cliente, _ = base
    m = _marca()
    sesion.add(ClientBirthday(client_id=cliente.id, nombre=f"{m} Caro", fecha=date(1980, 1, 2)))
    sesion.flush()

    caro = next(c for c in leer_cumples(sesion) if c.nombre == f"{m} Caro")

    assert caro.empresa == ""


# ---------------------------------------------------------------- solo lectura


def test_solo_lectura_rechaza_escrituras(db_ctx):
    from sqlalchemy.orm import Session

    with Session(db_ctx.engine) as sesion:
        with pytest.raises(DBAPIError, match="read-only"):
            with solo_lectura(sesion):
                sesion.execute(text("CREATE TEMP TABLE prueba_solo_lectura (x int)"))


def test_solo_lectura_exige_transaccion_nueva(db_ctx):
    from sqlalchemy.orm import Session

    with Session(db_ctx.engine) as sesion:
        sesion.execute(text("SELECT 1"))
        with pytest.raises(RuntimeError, match="transacción nueva"):
            with solo_lectura(sesion):
                pass
        sesion.rollback()


def test_armar_reporte_lee_sin_escribir_y_sin_n_mas_1(db_ctx):
    db = db_ctx
    antes = db.session.execute(select(func.count()).select_from(Collection)).scalar()
    db.session.rollback()

    sentencias = []

    def _registrar(conn, cursor, statement, *args):
        sentencias.append(statement)

    event.listen(db.engine, "before_cursor_execute", _registrar)
    try:
        reporte = armar_reporte(HOY)
    finally:
        event.remove(db.engine, "before_cursor_execute", _registrar)

    assert isinstance(reporte, Reporte)
    assert reporte.hoy == HOY
    assert sentencias[0].strip().upper() == "SET TRANSACTION READ ONLY"
    # SET + cobranzas + saldos (si hay pedidos) + cumpleaños + vínculos/empresas.
    assert len(sentencias) <= 5
    assert not db.session().in_transaction()

    despues = db.session.execute(select(func.count()).select_from(Collection)).scalar()
    assert despues == antes
