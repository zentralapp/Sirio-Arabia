"""Lectura de datos del reporte contra la base LOCAL (marca `db`).

Cada test carga sus propias filas dentro de una transacción que se descarta
(fixture `sesion_tx`): no queda nada en la base. Los tests de `armar_reporte`
solo LEEN los datos existentes.
"""
import uuid
from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import event, func, select, text, update
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
    OrderDraft,
    RelationStatus,
)
from backend.services.reporte_diario.clasificar import clasificar_cobranzas
from backend.services.reporte_diario.fechas import fin_de_semana
from backend.services.reporte_diario.datos import (
    armar_reporte,
    leer_cobranzas,
    leer_compras_por_par,
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


def test_cobranza_trae_client_id_y_company_id(base):
    """Base de la deuda por par (cliente, empresa) de la regularidad."""
    sesion, cliente, empresa = base
    pedido = _pedido(sesion, cliente, empresa, precio=D("1000"), coll={"fecha_pago_estimada": datetime(2026, 10, 19)})

    f = _por_id(leer_cobranzas(sesion, HOY))[pedido.id]

    assert (f.client_id, f.company_id) == (cliente.id, empresa.id) == (pedido.client_id, pedido.company_id)


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


# ---------------------------------------------------------------- query E: días de compra por par

LUNES = date(2026, 11, 9)
MARTES = date(2026, 11, 10)


def _compra(sesion, cliente, empresa, *, dia=None, creado=None, deleted_at=None, sin_logistica=False, cobrado=True,
            precio=None):
    """Pedido con fecha_compra `dia` (None = NULL) y created_at `creado` (default: `dia` o 01/01/2026).

    `cobrado=True` le pone fecha_cobro_efectiva para que no aporte deuda (query A).
    """
    creado = creado or (datetime.combine(dia, datetime.min.time()) if dia else datetime(2026, 1, 1))
    pedido = Order(client_id=cliente.id, company_id=empresa.id, created_at=creado, deleted_at=deleted_at,
                   precio_final=precio)
    sesion.add(pedido)
    sesion.flush()
    if not sin_logistica:
        fecha = datetime.combine(dia, datetime.min.time().replace(hour=15)) if dia else None
        logistica = LogisticsStatus(order_id=pedido.id, fecha_compra=fecha)
        sesion.add(logistica)
        sesion.flush()
        if fecha is None:
            # fecha_compra=None en el constructor NO pisa el default utcnow: hay que dejarla NULL con un UPDATE.
            sesion.execute(update(LogisticsStatus).where(LogisticsStatus.id == logistica.id).values(fecha_compra=None))
            sesion.expire(logistica)
    if cobrado:
        sesion.add(Collection(order_id=pedido.id, fecha_cobro_efectiva=datetime(2026, 1, 2)))
    sesion.flush()
    return pedido


def _dias_del_par(sesion, cliente, empresa, hoy=LUNES):
    pares = {(p.client_id, p.company_id): p for p in leer_compras_por_par(sesion, hoy)}
    par = pares.get((cliente.id, empresa.id))
    return None if par is None else par.dias


def test_compras_varios_pedidos_el_mismo_dia_son_un_dia(base):
    sesion, cliente, empresa = base
    for _ in range(3):
        _compra(sesion, cliente, empresa, dia=date(2026, 10, 28))
    _compra(sesion, cliente, empresa, dia=date(2026, 10, 14))

    assert _dias_del_par(sesion, cliente, empresa) == (date(2026, 10, 14), date(2026, 10, 28))


def test_compras_pedido_borrado_no_cuenta(base):
    sesion, cliente, empresa = base
    _compra(sesion, cliente, empresa, dia=date(2026, 10, 1))
    _compra(sesion, cliente, empresa, dia=date(2026, 10, 15), deleted_at=datetime(2026, 10, 16))

    assert _dias_del_par(sesion, cliente, empresa) == (date(2026, 10, 1),)


def test_compras_fecha_compra_null_usa_created_at(base):
    sesion, cliente, empresa = base
    con_null = _compra(sesion, cliente, empresa, dia=None, creado=datetime(2026, 10, 1, 13, 0))
    _compra(sesion, cliente, empresa, sin_logistica=True, creado=datetime(2026, 10, 8, 9, 0))

    assert sesion.get(LogisticsStatus, sesion.scalar(
        select(LogisticsStatus.id).where(LogisticsStatus.order_id == con_null.id))).fecha_compra is None
    assert _dias_del_par(sesion, cliente, empresa) == (date(2026, 10, 1), date(2026, 10, 8))


def test_compras_fecha_compra_retrofechada_al_pasado(base):
    sesion, cliente, empresa = base
    _compra(sesion, cliente, empresa, dia=date(2026, 10, 5), creado=datetime(2026, 10, 20, 10, 0))

    assert _dias_del_par(sesion, cliente, empresa) == (date(2026, 10, 5),)


def test_compras_dia_futuro_no_cuenta(base):
    sesion, cliente, empresa = base
    for dia in (date(2026, 9, 1), date(2026, 9, 15), date(2026, 9, 29), date(2026, 10, 13)):
        _compra(sesion, cliente, empresa, dia=dia)
    _compra(sesion, cliente, empresa, dia=date(2026, 11, 20), creado=datetime(2026, 11, 2))

    dias = _dias_del_par(sesion, cliente, empresa)

    assert dias[-1] == date(2026, 10, 13)
    assert date(2026, 11, 20) not in dias
    # Visto el lunes posterior al 20/11, sí cuenta.
    assert _dias_del_par(sesion, cliente, empresa, hoy=date(2026, 11, 23))[-1] == date(2026, 11, 20)


def test_compras_ventana_trae_los_13_mas_recientes_ascendentes(base):
    sesion, cliente, empresa = base
    todos = [date(2026, 1, 5) + timedelta(days=7 * k) for k in range(15)]
    for dia in todos:
        _compra(sesion, cliente, empresa, dia=dia)

    assert _dias_del_par(sesion, cliente, empresa) == tuple(todos[-13:])


def test_compras_cliente_archivado_excluido_y_desarchivado_vuelve_con_todo(base):
    sesion, cliente, empresa = base
    dias = [date(2026, 8, 1) + timedelta(days=14 * k) for k in range(5)]
    for dia in dias:
        _compra(sesion, cliente, empresa, dia=dia)
    cliente.archived = True
    sesion.flush()

    assert _dias_del_par(sesion, cliente, empresa) is None

    cliente.archived = False
    sesion.flush()

    assert _dias_del_par(sesion, cliente, empresa) == tuple(dias)


def test_compras_empresa_archivada_excluida_y_desarchivada_vuelve_con_todo(base):
    sesion, cliente, empresa_y = base
    empresa_x = Company(nombre=f"{_marca()} X SA", marca=None, plazo_pago_promedio_dias=30, archived=True)
    sesion.add(empresa_x)
    sesion.flush()
    dias = [date(2026, 8, 1) + timedelta(days=14 * k) for k in range(5)]
    for dia in dias:
        _compra(sesion, cliente, empresa_x, dia=dia)
        _compra(sesion, cliente, empresa_y, dia=dia)

    assert _dias_del_par(sesion, cliente, empresa_x) is None
    assert _dias_del_par(sesion, cliente, empresa_y) == tuple(dias)

    empresa_x.archived = False
    sesion.flush()

    assert _dias_del_par(sesion, cliente, empresa_x) == tuple(dias)


def test_compras_nombres_como_query_a(base):
    sesion, cliente, empresa = base
    _compra(sesion, cliente, empresa, dia=date(2026, 10, 1))

    (par,) = [p for p in leer_compras_por_par(sesion, LUNES) if p.client_id == cliente.id]

    assert par.cliente == f"{cliente.apellido} (Fantasia)"
    assert par.empresa == f"{empresa.nombre} (Marca)"


def test_compras_order_draft_no_aporta_dias(base):
    sesion, cliente, empresa = base
    sesion.add(OrderDraft(client_id=cliente.id, company_id=empresa.id, updated_at=datetime(2026, 10, 1)))
    sesion.flush()

    assert _dias_del_par(sesion, cliente, empresa) is None


def test_compras_es_una_sola_sentencia(base, db_ctx):
    sesion, cliente, empresa = base
    for dia in (date(2026, 9, 1), date(2026, 9, 15)):
        _compra(sesion, cliente, empresa, dia=dia)
    sentencias = []

    def _registrar(conn, cursor, statement, *args):
        sentencias.append(statement)

    event.listen(db_ctx.engine, "before_cursor_execute", _registrar)
    try:
        leer_compras_por_par(sesion, LUNES)
    finally:
        event.remove(db_ctx.engine, "before_cursor_execute", _registrar)

    assert len(sentencias) == 1
    assert "row_number" in sentencias[0].lower()


# ---------------------------------------------------------------- armar_reporte con la query E


def _contar_query_e(db_ctx, hoy, session=None):
    sentencias = []

    def _registrar(conn, cursor, statement, *args):
        sentencias.append(statement)

    event.listen(db_ctx.engine, "before_cursor_execute", _registrar)
    try:
        reporte = armar_reporte(hoy, session)
    finally:
        event.remove(db_ctx.engine, "before_cursor_execute", _registrar)
    return reporte, sum(1 for s in sentencias if "row_number" in s.lower()), sentencias


def test_armar_reporte_de_martes_no_ejecuta_la_query_e(db_ctx):
    reporte, query_e, sentencias = _contar_query_e(db_ctx, MARTES)

    assert reporte.regularidad == ()
    assert query_e == 0
    assert sentencias[0].strip().upper() == "SET TRANSACTION READ ONLY"


def test_armar_reporte_de_lunes_ejecuta_la_query_e_una_vez_en_solo_lectura(db_ctx):
    reporte, query_e, sentencias = _contar_query_e(db_ctx, LUNES)

    assert query_e == 1
    assert sentencias[0].strip().upper() == "SET TRANSACTION READ ONLY"
    # SET + cobranzas + saldos + cumpleaños + vínculos + query E.
    assert len(sentencias) <= 6
    assert not db_ctx.session().in_transaction()


def test_armar_reporte_de_lunes_con_un_par_que_dispara_trae_su_deuda(base):
    sesion, cliente, empresa = base
    # Intervalos 14, 14, 14; última compra 13/10 -> el lunes 09/11 N = 27, P = 14.
    for dia in (date(2026, 9, 1), date(2026, 9, 15), date(2026, 9, 29)):
        _compra(sesion, cliente, empresa, dia=dia)
    _compra(sesion, cliente, empresa, dia=date(2026, 10, 13), cobrado=False, precio=D("1234.56"))
    sesion.commit()  # libera el savepoint: solo_lectura exige una transacción nueva (la externa se descarta igual)

    reporte = armar_reporte(LUNES, sesion)

    (fila,) = [f for f in reporte.regularidad if f.cliente.startswith(cliente.apellido)]
    assert (fila.ultima_compra, fila.dias_sin_comprar, fila.ritmo, fila.compras) == (date(2026, 10, 13), 27, 14, 4)
    assert (fila.deuda.total, fila.deuda.sin_monto, fila.deuda.abiertas) == (D("1234.56"), 0, 1)
