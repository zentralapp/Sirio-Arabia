from datetime import datetime, timedelta, date
from enum import Enum
from typing import NamedTuple, Optional
from .extensions import db
from flask_login import UserMixin
from sqlalchemy import and_, func, or_, select
from werkzeug.security import generate_password_hash, check_password_hash

try:
    from zoneinfo import ZoneInfo  # Python 3.9+
except ImportError:  # pragma: no cover - solo entornos < 3.9
    ZoneInfo = None


class AppUser(db.Model, UserMixin):
    __tablename__ = "app_user"
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(64), nullable=False, unique=True)
    password_hash = db.Column(db.String(255), nullable=False)
    is_admin = db.Column(db.Boolean, nullable=False, default=False)
    permissions_json = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def set_password(self, password: str):
        self.password_hash = generate_password_hash(password or "")

    def check_password(self, password: str) -> bool:
        try:
            return check_password_hash(self.password_hash or "", password or "")
        except Exception:
            return False


class RelationStatus(str, Enum):
    TRABAJA = "TRABAJA"
    TRABAJABA = "TRABAJABA"
    A_INCORPORAR = "A_INCORPORAR"


class PaymentMethod(str, Enum):
    EFECTIVO = "EFECTIVO"
    CHEQUE = "CHEQUE"
    TRANSFERENCIA = "TRANSFERENCIA"
    NO_SE_SABE = "NO_SE_SABE"


class ClientCompanyLink(db.Model):
    __tablename__ = "client_company_link"
    id = db.Column(db.Integer, primary_key=True)
    client_id = db.Column(db.Integer, db.ForeignKey("client.id"), nullable=False)
    company_id = db.Column(db.Integer, db.ForeignKey("company.id"), nullable=False)
    status = db.Column(db.Enum(RelationStatus), nullable=False, default=RelationStatus.TRABAJA)
    comprobante_tipo = db.Column(db.String(20), nullable=False, default="FACTURA")
    descuento = db.Column(db.Numeric(5, 2))
    plazo_pago_dias = db.Column(db.Integer)

    __table_args__ = (db.UniqueConstraint("client_id", "company_id", name="uq_client_company"),)


class ClientCompanyBalance(db.Model):
    __tablename__ = "client_company_balance"
    id = db.Column(db.Integer, primary_key=True)
    owner_user_id = db.Column(db.Integer, db.ForeignKey("app_user.id"))
    client_id = db.Column(db.Integer, db.ForeignKey("client.id"), nullable=False)
    company_id = db.Column(db.Integer, db.ForeignKey("company.id"), nullable=False)
    balance_adjustment = db.Column(db.Numeric(12, 2), nullable=False, default=0)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (
        db.UniqueConstraint("owner_user_id", "client_id", "company_id", name="uq_client_company_balance"),
    )


class ClientBranch(db.Model):
    __tablename__ = "client_branch"
    id = db.Column(db.Integer, primary_key=True)
    client_id = db.Column(db.Integer, db.ForeignKey("client.id"), nullable=False)
    nombre = db.Column(db.String(120), nullable=False)

    __table_args__ = (db.UniqueConstraint("client_id", "nombre", name="uq_client_branch_name"),)


class ClientDeliveryPlace(db.Model):
    __tablename__ = "client_delivery_place"
    id = db.Column(db.Integer, primary_key=True)
    client_id = db.Column(db.Integer, db.ForeignKey("client.id"), nullable=False)
    nombre = db.Column(db.String(255), nullable=False)

    # Datos específicos por lugar de entrega
    provincia = db.Column(db.String(80))
    nota = db.Column(db.String(255))
    horario = db.Column(db.String(255))
    contacto = db.Column(db.String(255))
    telefono = db.Column(db.String(64))

    __table_args__ = (db.UniqueConstraint("client_id", "nombre", name="uq_client_delivery_name"),)


class ClientBirthday(db.Model):
    __tablename__ = "client_birthday"
    id = db.Column(db.Integer, primary_key=True)
    client_id = db.Column(db.Integer, db.ForeignKey("client.id"), nullable=False)
    nombre = db.Column(db.String(255), nullable=False)
    puesto = db.Column(db.String(255))
    fecha = db.Column(db.Date)
    notas = db.Column(db.Text)

    __table_args__ = (db.UniqueConstraint("client_id", "nombre", "puesto", name="uq_client_birthday"),)


class Client(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    owner_user_id = db.Column(db.Integer, db.ForeignKey("app_user.id"))
    archived = db.Column(db.Boolean, nullable=False, default=False)
    apellido = db.Column(db.String(120), nullable=False)
    nombre = db.Column(db.String(120), nullable=False)
    sucursal = db.Column(db.String(120))
    cuit = db.Column(db.String(32))
    direccion_principal = db.Column(db.String(255))
    forma_pago_habitual = db.Column(db.String(32))
    transporte_recomendado = db.Column(db.String(120))
    delivery_schedule = db.Column(db.String(255))
    delivery_contact = db.Column(db.String(255))
    delivery_phone = db.Column(db.String(64))
    provincia = db.Column(db.String(80))
    fecha_incorporacion = db.Column(db.Date, default=date.today)
    telefono = db.Column(db.String(50))
    mail = db.Column(db.String(255))
    transporte_contacto = db.Column(db.String(255))

    links = db.relationship("ClientCompanyLink", backref="client", cascade="all, delete-orphan")
    orders = db.relationship("Order", backref="client", cascade="all, delete-orphan")
    branches = db.relationship("ClientBranch", backref="client", cascade="all, delete-orphan")
    delivery_places = db.relationship("ClientDeliveryPlace", backref="client", cascade="all, delete-orphan")
    birthdays = db.relationship("ClientBirthday", backref="client", cascade="all, delete-orphan")
    documents = db.relationship("ClientDocument", backref="client", cascade="all, delete-orphan")

    @property
    def display_name(self):
        """Como se muestra el cliente en selects y listados: "Ferreyra (Marcelo)".

        OJO con la semantica invertida de las columnas:
          apellido = RAZON SOCIAL (label "Razon social" en la UI)
          nombre   = nombre de FANTASIA (label "Nombre" en la UI)

        Si no hay nombre de fantasia devuelve solo la razon social, sin dejar
        los parentesis colgando.
        """
        razon_social = (self.apellido or "").strip()
        fantasia = (self.nombre or "").strip()
        if not fantasia:
            return razon_social
        return f"{razon_social} ({fantasia})"

    @property
    def empresas_trabaja(self):
        return [l for l in self.links if l.status == RelationStatus.TRABAJA]

    @property
    def empresas_trabajaba(self):
        return [l for l in self.links if l.status == RelationStatus.TRABAJABA]

    @property
    def empresas_a_incorporar(self):
        return [l for l in self.links if l.status == RelationStatus.A_INCORPORAR]


class Company(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    archived = db.Column(db.Boolean, nullable=False, default=False)
    nombre = db.Column(db.String(200), nullable=False, unique=True)
    marca = db.Column(db.String(120))
    demora_despacho_promedio_dias = db.Column(db.Integer, default=0)
    plazo_pago_promedio_dias = db.Column(db.Integer, default=30)
    mail_pedido = db.Column(db.String(255))
    mail_pago = db.Column(db.String(255))
    pedido_estandar_recomendado = db.Column(db.Text)
    nota_pedido = db.Column(db.Text)
    plazo_usual = db.Column(db.String(120))
    forma_pago_default = db.Column(db.String(32))
    # Información complementaria
    cuit = db.Column(db.String(32))
    notas = db.Column(db.Text)
    cuenta_bancaria_notas = db.Column(db.Text)

    links = db.relationship("ClientCompanyLink", backref="company", cascade="all, delete-orphan")
    orders = db.relationship("Order", backref="company", cascade="all, delete-orphan")


class CompanyDocument(db.Model):
    __tablename__ = "company_document"
    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("company.id"), nullable=False)
    # category: por ejemplo CONSTANCIA, CATALOGO
    category = db.Column(db.String(32), nullable=False)
    filename = db.Column(db.String(255), nullable=False)
    filepath = db.Column(db.String(500), nullable=False)
    data = db.Column(db.LargeBinary)
    mimetype = db.Column(db.String(120))
    size = db.Column(db.Integer)
    uploaded_at = db.Column(db.DateTime, default=datetime.utcnow)

    company = db.relationship("Company", backref=db.backref("documents", cascade="all, delete-orphan"))


class CompanyProductSheet(db.Model):
    __tablename__ = "company_product_sheet"
    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("company.id"), nullable=False)
    filename = db.Column(db.String(255), nullable=False)
    filepath = db.Column(db.String(500))  # URL (Cloudinary) opcional
    data = db.Column(db.LargeBinary)  # BLOB opcional (fallback)
    mimetype = db.Column(db.String(120))
    size = db.Column(db.Integer)
    uploaded_at = db.Column(db.DateTime, default=datetime.utcnow)

    company = db.relationship("Company", backref=db.backref("product_sheets", cascade="all, delete-orphan"))


class Order(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    deleted_at = db.Column(db.DateTime)

    owner_user_id = db.Column(db.Integer, db.ForeignKey("app_user.id"))

    client_id = db.Column(db.Integer, db.ForeignKey("client.id"), nullable=False)
    company_id = db.Column(db.Integer, db.ForeignKey("company.id"), nullable=False)

    sucursal = db.Column(db.String(120))
    branch_id = db.Column(db.Integer, db.ForeignKey("client_branch.id"))
    nota = db.Column(db.Text)
    descripcion = db.Column(db.Text)

    precio_final = db.Column(db.Numeric(12, 2))
    forma_pago = db.Column(db.Enum(PaymentMethod))
    forma_pago_detalle = db.Column(db.String(32))

    # Tipo de comprobante asociado al pedido (por ejemplo FACTURA o REMITO)
    tipo_comprobante = db.Column(db.String(16))

    # Plazo de pago específico del pedido (días). Si es None, usar el de la empresa.
    plazo_pago_dias = db.Column(db.Integer)

    # Snapshots from company at order time
    demora_despacho_promedio_dias = db.Column(db.Integer)
    mail_pedido = db.Column(db.String(255))

    logistics = db.relationship("LogisticsStatus", uselist=False, backref="order", cascade="all, delete-orphan")
    collection = db.relationship("Collection", uselist=False, backref="order", cascade="all, delete-orphan")


class LogisticsStatus(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    order_id = db.Column(db.Integer, db.ForeignKey("order.id"), nullable=False, unique=True)

    fecha_compra = db.Column(db.DateTime, default=datetime.utcnow)
    fecha_entrega_estimada = db.Column(db.DateTime)
    fecha_entrega_efectiva = db.Column(db.DateTime)

    precio = db.Column(db.Numeric(12, 2))
    forma_pago = db.Column(db.Enum(PaymentMethod))
    forma_pago_detalle = db.Column(db.String(32))

    nota = db.Column(db.Text)
    descripcion = db.Column(db.Text)

    @property
    def status(self):
        if self.fecha_entrega_efectiva:
            return "ENTREGADO"
        if self.fecha_entrega_estimada and datetime.utcnow() > self.fecha_entrega_estimada:
            return "ATRASADO"
        return "EN CAMINO"


class Collection(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    order_id = db.Column(db.Integer, db.ForeignKey("order.id"), nullable=False, unique=True)

    owner_user_id = db.Column(db.Integer, db.ForeignKey("app_user.id"))

    fecha_entrega_efectiva = db.Column(db.DateTime)
    monto = db.Column(db.Numeric(12, 2))
    forma_pago = db.Column(db.Enum(PaymentMethod))
    forma_pago_detalle = db.Column(db.String(32))

    fecha_pago_estimada = db.Column(db.DateTime)
    fecha_cobro_efectiva = db.Column(db.DateTime)

    # --- Estado y vencimiento: TODO delega en la fuente única del final de
    # --- este archivo. No reimplementes la lógica acá ni en los templates.

    @property
    def entrega_efectiva(self):
        """Entrega efectiva resuelta (regla 2 de la fuente única)."""
        return entrega_efectiva_de(self)

    @property
    def vencimiento_efectivo(self):
        """Vencimiento efectivo resuelto (regla 1 de la fuente única).

        Es el valor que deben consumir los templates y el JS: ni el Jinja ni
        el navegador vuelven a derivar el vencimiento.
        """
        return vencimiento_efectivo_de(self)

    @property
    def status(self):
        """COBRADO | ATRASADO | A_COBRAR | EN_CAMINO (regla 5).

        OJO: antes devolvía "EN CAMINO" (con espacio) y no existía el bucket
        A_COBRAR. Ahora devuelve las mismas claves que usan los filtros de
        /deudas y las queries SQL, para que no haya dos vocabularios.
        """
        return estado_cobranza_de(self)


class CollectionPayment(db.Model):
    __tablename__ = "collection_payment"
    id = db.Column(db.Integer, primary_key=True)
    order_id = db.Column(db.Integer, db.ForeignKey("order.id"), nullable=False)

    owner_user_id = db.Column(db.Integer, db.ForeignKey("app_user.id"))
    kind = db.Column(db.String(16), nullable=False)  # PAYMENT / CREDIT_NOTE
    method = db.Column(db.String(32))
    amount = db.Column(db.Numeric(12, 2))
    due_date = db.Column(db.DateTime)
    attachment_url = db.Column(db.String(500))
    notes = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    voided_at = db.Column(db.DateTime)
    voided_by_user_id = db.Column(db.Integer, db.ForeignKey("app_user.id"))
    voided_reason = db.Column(db.Text)

    order = db.relationship("Order", backref=db.backref("collection_payments", cascade="all, delete-orphan"))


class CommissionState(db.Model):
    __tablename__ = "commission_state"
    id = db.Column(db.Integer, primary_key=True)
    last_paid_at = db.Column(db.DateTime)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class OrderAttachment(db.Model):
    __tablename__ = "order_attachment"
    id = db.Column(db.Integer, primary_key=True)
    order_id = db.Column(db.Integer, db.ForeignKey("order.id"), nullable=False)
    url = db.Column(db.String(500), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    order = db.relationship("Order", backref=db.backref("attachments", cascade="all, delete-orphan"))


class OrderDraft(db.Model):
    __tablename__ = "order_draft"
    id = db.Column(db.Integer, primary_key=True)
    owner_user_id = db.Column(db.Integer, db.ForeignKey("app_user.id"))
    client_id = db.Column(db.Integer, db.ForeignKey("client.id"), nullable=False)
    company_id = db.Column(db.Integer, db.ForeignKey("company.id"), nullable=False)
    branch_id = db.Column(db.Integer)
    sucursal = db.Column(db.String(120))
    nota = db.Column(db.Text)
    descripcion = db.Column(db.Text)
    precio_final = db.Column(db.Numeric(12, 2))
    forma_pago = db.Column(db.Enum(PaymentMethod))
    forma_pago_detalle = db.Column(db.String(32))
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (db.UniqueConstraint("client_id", "company_id", name="uq_draft_client_company"),)


class CollectionDraft(db.Model):
    __tablename__ = "collection_draft"
    id = db.Column(db.Integer, primary_key=True)
    owner_user_id = db.Column(db.Integer, db.ForeignKey("app_user.id"))
    client_id = db.Column(db.Integer, db.ForeignKey("client.id"), nullable=False)
    company_id = db.Column(db.Integer, db.ForeignKey("company.id"), nullable=False)
    order_id = db.Column(db.Integer, db.ForeignKey("order.id"))
    notes = db.Column(db.Text)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class ClientDocument(db.Model):
    __tablename__ = "client_document"
    id = db.Column(db.Integer, primary_key=True)
    client_id = db.Column(db.Integer, db.ForeignKey("client.id"), nullable=False)

    owner_user_id = db.Column(db.Integer, db.ForeignKey("app_user.id"))
    category = db.Column(db.String(32))
    filename = db.Column(db.String(255), nullable=False)
    filepath = db.Column(db.String(500), nullable=False)
    # Nuevo: soporte de almacenamiento en DB (Postgres) para archivos
    data = db.Column(db.LargeBinary)
    mimetype = db.Column(db.String(120))
    size = db.Column(db.Integer)
    uploaded_at = db.Column(db.DateTime, default=datetime.utcnow)


class ClientAlertState(db.Model):
    __tablename__ = "client_alert_state"
    id = db.Column(db.Integer, primary_key=True)
    client_id = db.Column(db.Integer, db.ForeignKey("client.id"), nullable=False)
    order_id = db.Column(db.Integer, db.ForeignKey("order.id"), nullable=False)

    owner_user_id = db.Column(db.Integer, db.ForeignKey("app_user.id"))
    kind = db.Column(db.String(32), nullable=False)
    dismissed_at = db.Column(db.DateTime)
    snoozed_until = db.Column(db.DateTime)
    message = db.Column(db.Text)
    severity = db.Column(db.String(16))
    company = db.Column(db.String(200))
    first_seen_at = db.Column(db.DateTime)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (
        db.UniqueConstraint("client_id", "order_id", "kind", name="uq_client_alert_state"),
    )


# ===========================================================================
#  FUENTE ÚNICA DE VERDAD — vencimiento efectivo de una cobranza y su estado
# ===========================================================================
#
#  ESTA ES *LA* DEFINICIÓN. NO LA DUPLIQUES.
#
#  Antes de esto había 8 implementaciones distintas del mismo concepto
#  (property del modelo, 3 queries SQL, badge Jinja, JS del navegador, orden
#  visual) y se contradecían entre sí: había filas cuyo badge decía ATRASADO
#  pero que el filtro "Atrasado" nunca devolvía, y que las notificaciones no
#  veían. Si necesitás el vencimiento o el estado de una deuda —en SQL, en
#  Python, en un template o en JS— pasá por acá. No copies la lógica.
#
#  REGLAS (idénticas en el mundo SQL y en el mundo Python):
#
#  1. Vencimiento efectivo:
#         Collection.fecha_pago_estimada
#         si es NULL  ->  entrega efectiva + plazo de pago (días)
#         si tampoco hay entrega efectiva  ->  NULL (no hay vencimiento)
#     NO se persiste nada: la derivación es al vuelo, en cada consulta.
#     Se aceptó explícitamente el costo (los índices sobre
#     collection.fecha_pago_estimada no aplican a este filtro). Es un CRM
#     interno de bajo volumen.
#
#  2. Entrega efectiva:
#         coalesce(LogisticsStatus.fecha_entrega_efectiva,
#                  Collection.fecha_entrega_efectiva,
#                  LogisticsStatus.fecha_entrega_estimada)
#
#  3. Plazo de pago (días):
#         coalesce(Order.plazo_pago_dias,
#                  Company.plazo_pago_promedio_dias,
#                  30)
#
#  4. "Hoy" es SIEMPRE la fecha local de Argentina (ver TZ_NEGOCIO). Que una
#     deuda esté atrasada es un concepto de negocio, y el negocio vive en
#     Argentina: vence al final del día argentino, no del día UTC. Por eso
#     acá NO se usa datetime.utcnow() (utcnow queda sólo para timestamps de
#     auditoría: created_at / updated_at / uploaded_at).
#
#  5. Los cuatro estados son mutuamente excluyentes y exhaustivos:
#         COBRADO   -> hay fecha_cobro_efectiva
#         ATRASADO  -> sin cobrar y vencimiento efectivo < hoy
#         A_COBRAR  -> sin cobrar, no vencido y ya entregado (entrega <= hoy)
#         EN_CAMINO -> sin cobrar, no vencido y todavía no entregado
#     PARCIAL es ORTOGONAL (un flag, no un estado): una deuda con pagos
#     parciales o borradores puede estar en cualquiera de los cuatro. Se
#     resuelve aparte (partial_exists / partial_order_ids) y no se toca acá.
# ===========================================================================

TZ_NEGOCIO = "America/Argentina/Buenos_Aires"
PLAZO_PAGO_DIAS_DEFAULT = 30

ESTADOS_COBRANZA = ("COBRADO", "ATRASADO", "A_COBRAR", "EN_CAMINO")


def hoy_negocio() -> date:
    """Fecha de 'hoy' según el huso del negocio (Argentina).

    Único 'hoy' válido para decidir si una deuda está vencida.
    """
    if ZoneInfo is None:
        return date.today()
    return datetime.now(ZoneInfo(TZ_NEGOCIO)).date()


def _as_date(value) -> Optional[date]:
    """Normaliza datetime | date | None -> date | None."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return None


# ---------------------------------------------------------------------------
#  Mundo Python: para una fila ya cargada en memoria (ORM / templates)
# ---------------------------------------------------------------------------

def plazo_pago_dias_de(order) -> int:
    """Regla 3 en Python. Debe dar lo mismo que plazo_pago_dias_expr()."""
    if order is not None:
        propio = getattr(order, "plazo_pago_dias", None)
        if propio is not None:
            return int(propio)
        company = getattr(order, "company", None)
        if company is not None:
            de_empresa = getattr(company, "plazo_pago_promedio_dias", None)
            if de_empresa is not None:
                return int(de_empresa)
    return PLAZO_PAGO_DIAS_DEFAULT


def entrega_efectiva_de(coll) -> Optional[datetime]:
    """Regla 2 en Python. Debe dar lo mismo que entrega_efectiva_expr()."""
    if coll is None:
        return None
    order = getattr(coll, "order", None)
    logistics = getattr(order, "logistics", None) if order is not None else None
    return (
        getattr(logistics, "fecha_entrega_efectiva", None)
        or getattr(coll, "fecha_entrega_efectiva", None)
        or getattr(logistics, "fecha_entrega_estimada", None)
    )


def vencimiento_efectivo_de(coll) -> Optional[datetime]:
    """Regla 1 en Python. Debe dar lo mismo que vencimiento_efectivo_expr()."""
    if coll is None:
        return None
    persistido = getattr(coll, "fecha_pago_estimada", None)
    if persistido is not None:
        return persistido
    entrega = entrega_efectiva_de(coll)
    if entrega is None:
        return None
    return entrega + timedelta(days=plazo_pago_dias_de(getattr(coll, "order", None)))


def estado_cobranza_de(coll, hoy: Optional[date] = None) -> str:
    """Regla 5 en Python. Debe dar lo mismo que condiciones_estado_cobranza()."""
    if getattr(coll, "fecha_cobro_efectiva", None):
        return "COBRADO"
    if hoy is None:
        hoy = hoy_negocio()
    vencimiento = _as_date(vencimiento_efectivo_de(coll))
    if vencimiento is not None and vencimiento < hoy:
        return "ATRASADO"
    entrega = _as_date(entrega_efectiva_de(coll))
    if entrega is not None and entrega <= hoy:
        return "A_COBRAR"
    return "EN_CAMINO"


# ---------------------------------------------------------------------------
#  Mundo SQL: expresiones reutilizables para filtrar/ordenar en queries
# ---------------------------------------------------------------------------
#  PRECONDICIÓN: la query tiene que tener Collection, Order y LogisticsStatus
#  en el FROM (join / outerjoin). Company NO hace falta joinearla: se resuelve
#  con un subquery correlacionado, así estas expresiones se pueden pegar en
#  cualquier query sin obligar a cambiar sus joins.
# ---------------------------------------------------------------------------

def entrega_efectiva_expr():
    """Regla 2 en SQL (timestamp)."""
    return func.coalesce(
        LogisticsStatus.fecha_entrega_efectiva,
        Collection.fecha_entrega_efectiva,
        LogisticsStatus.fecha_entrega_estimada,
    )


def plazo_pago_dias_expr():
    """Regla 3 en SQL (entero de días)."""
    # .correlate(Order) es OBLIGATORIO: sin eso SQLAlchemy mete "order" en el
    # FROM del subquery y devuelve más de una fila (producto cartesiano).
    plazo_de_empresa = (
        select(Company.plazo_pago_promedio_dias)
        .where(Company.id == Order.company_id)
        .correlate(Order)
        .scalar_subquery()
    )
    return func.coalesce(
        Order.plazo_pago_dias,
        plazo_de_empresa,
        PLAZO_PAGO_DIAS_DEFAULT,
    )


def vencimiento_efectivo_expr():
    """Regla 1 en SQL. Devuelve un DATE (o NULL si no hay vencimiento)."""
    # OJO: `date + entero` de abajo es aritmetica de fechas de PostgreSQL
    # (suma dias y devuelve un DATE). NO es portable y NO hace falta que lo sea:
    # el proyecto soporta unicamente PostgreSQL. backend/config.py exige
    # DATABASE_URL y rechaza explicitamente las URLs sqlite:// al arrancar.
    #
    # Por que importa: en SQLite `date('2026-09-01') + 30` no falla, hace
    # aritmetica numerica sobre el texto y devuelve 2056. Silencioso y
    # totalmente equivocado: vencimientos, buckets y filtros darian distinto.
    #
    # Antes de "hacer esto portable", leer ese contexto: la decision tomada fue
    # retirar SQLite formalmente, no emular su aritmetica.
    return func.coalesce(
        func.date(Collection.fecha_pago_estimada),
        func.date(entrega_efectiva_expr()) + plazo_pago_dias_expr(),
    )


class CondicionesCobranza(NamedTuple):
    """Condiciones SQL de los 4 estados + las expresiones de fecha crudas."""

    vencimiento: object
    entrega: object
    cobrado: object
    atrasado: object
    a_cobrar: object
    en_camino: object


def condiciones_estado_cobranza(hoy: Optional[date] = None) -> CondicionesCobranza:
    """Regla 5 en SQL. Debe dar lo mismo que estado_cobranza_de()."""
    if hoy is None:
        hoy = hoy_negocio()

    vencimiento = vencimiento_efectivo_expr()
    entrega = entrega_efectiva_expr()

    sin_cobrar = Collection.fecha_cobro_efectiva.is_(None)
    # `vencimiento IS NOT NULL` nunca es NULL, así que estas dos condiciones
    # son complementarias exactas (no hay agujero por lógica trivaluada).
    vencido = and_(vencimiento.isnot(None), vencimiento < hoy)
    no_vencido = or_(vencimiento.is_(None), vencimiento >= hoy)
    ya_entregado = and_(entrega.isnot(None), func.date(entrega) <= hoy)
    no_entregado = or_(entrega.is_(None), func.date(entrega) > hoy)

    return CondicionesCobranza(
        vencimiento=vencimiento,
        entrega=entrega,
        cobrado=Collection.fecha_cobro_efectiva.isnot(None),
        atrasado=and_(sin_cobrar, vencido),
        a_cobrar=and_(sin_cobrar, no_vencido, ya_entregado),
        en_camino=and_(sin_cobrar, no_vencido, no_entregado),
    )
