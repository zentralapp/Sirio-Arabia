from flask import Blueprint, render_template, request, redirect, url_for, jsonify, abort, send_file

from flask import current_app

from flask import session

from datetime import date, datetime, timedelta

from io import BytesIO

from urllib.parse import urlparse

from urllib.parse import urlencode

from typing import Optional

import smtplib

from email.message import EmailMessage

try:

    from zoneinfo import ZoneInfo  # Python 3.9+

except Exception:

    ZoneInfo = None

import os

import requests

import json

import re

import unicodedata

from ..extensions import db

from ..models import Client, Company, ClientCompanyLink, ClientCompanyBalance, RelationStatus, Order, LogisticsStatus, Collection, CollectionPayment, PaymentMethod, ClientBranch, ClientDeliveryPlace, ClientBirthday, OrderAttachment, ClientDocument, CompanyDocument, CompanyProductSheet, ClientAlertState, CommissionState, AppUser, CollectionDraft, OrderDraft

# FUENTE ÚNICA DE VERDAD del vencimiento efectivo y del estado de una
# cobranza. Está definida en backend/models.py (bloque del final del
# archivo). NO reimplementes nada de esto acá: consumila.
from ..models import (
    PLAZO_PAGO_DIAS_DEFAULT,
    condiciones_estado_cobranza,
    estado_cobranza_de,
    hoy_negocio,
    vencimiento_efectivo_expr,
)

from sqlalchemy import func, text, or_, and_, case

from sqlalchemy.exc import OperationalError

from sqlalchemy.orm import selectinload, joinedload

from flask_login import current_user



from ..utils.company_products import parse_dynamic_table, read_dataframe_from_bytes, validate_filename



bp = Blueprint("main", __name__)


@bp.get("/favicon.ico")
def favicon_ico():

    return redirect(url_for("static", filename="favicon.svg"), code=302)





_MODULES = [

    "dashboard",

    "calendario",

    "clientes",

    "empresas",

    "pedidos",

    "status",

    "nueva_cobranza",

    "deudas",

    "historial",

    "crm",

    "comisiones",

    "mail_pagos",

    "usuarios",

]





@bp.app_context_processor

def inject_permission_helpers():

    return {

        "can_view": _can_view_module,

    }





def _get_effective_user_obj():

    try:

        uid = _effective_user_id()

    except Exception:

        uid = None

    if not uid:

        return None

    try:

        return AppUser.query.get(int(uid))

    except Exception:

        return None





def _parse_permissions_json(raw: str) -> dict:

    try:

        s = (raw or "").strip()

        if not s:

            return {}

        obj = json.loads(s)

        return obj if isinstance(obj, dict) else {}

    except Exception:

        return {}





def _can_view_module(module_key: str) -> bool:

    module_key = (module_key or "").strip()

    if not module_key:

        return True



    # Admin (sin soporte) ve todo.

    try:

        if _has_global_access():

            return True

    except Exception:

        pass



    u = _get_effective_user_obj()

    if u is None:

        return False



    # Admin impersonando: respetar permisos del usuario impersonado.

    perms = _parse_permissions_json(getattr(u, "permissions_json", None))

    v = perms.get(module_key)

    if v is None:

        # Por default: permitir módulos base; restringir los admin-only.

        if module_key in {"usuarios", "mail_pagos"}:

            return False

        return True

    return bool(v)





def _endpoint_to_module(endpoint: str) -> str:

    ep = (endpoint or "").strip()

    if not ep.startswith("main."):

        return ""

    name = ep.split(".", 1)[1]

    mapping = {

        "index": "dashboard",

        "calendario": "calendario",

        "clientes": "clientes",

        "clientes_edit_view": "clientes",

        "empresas": "empresas",

        "empresas_edit_view": "empresas",

        "empresas_productos_view": "empresas",

        "pedidos": "pedidos",

        "pedido_nuevo": "pedidos",

        "status": "status",

        "nueva_cobranza": "nueva_cobranza",

        "deudas_pendientes": "deudas",

        "historial": "historial",

        "crm": "crm",

        "comisiones": "comisiones",

        "mail_pagos": "mail_pagos",

        "usuarios": "usuarios",

    }

    return mapping.get(name, "")





@bp.before_app_request

def enforce_module_permissions():

    try:

        if not getattr(current_user, "is_authenticated", False):

            return None

        ep = request.endpoint or ""

        mod = _endpoint_to_module(ep)

        if not mod:

            return None

        if not _can_view_module(mod):

            abort(403)

    except Exception:

        return None

    return None





def _fmt_money_es(v: float) -> str:

    try:

        n = float(v or 0.0)

    except Exception:

        n = 0.0

    s = f"{n:,.2f}"

    return s.replace(",", "X").replace(".", ",").replace("X", ".")





def _get_company_mail_to(company: Company) -> list:

    raw = (getattr(company, "mail_pago", None) or "").strip()

    if not raw:

        return []

    return [m.strip() for m in re.split(r"[;,]", raw) if m.strip()]





def _send_email(

    subject: str,

    to_all: list,

    body_text: str,

    body_html: Optional[str] = None,

    attachments: Optional[list] = None,

    log_tag: str = "mail",

):

    msg = EmailMessage()

    msg["Subject"] = subject

    from_addr = os.getenv("GMAIL_FROM") or os.getenv("GMAIL_USER")

    if not from_addr:

        raise RuntimeError("Configurar GMAIL_USER o GMAIL_FROM en variables de entorno")

    msg["From"] = from_addr

    msg["To"] = ", ".join([m for m in (to_all or []) if m])

    msg.set_content(body_text or "")

    if body_html:

        msg.add_alternative(body_html, subtype="html")

    for att in attachments or []:

        try:

            content = att.get("content")

            if not content:

                continue

            maintype = (att.get("maintype") or "application").strip() or "application"

            subtype = (att.get("subtype") or "octet-stream").strip() or "octet-stream"

            filename = (att.get("filename") or "adjunto.bin").strip() or "adjunto.bin"

            msg.add_attachment(content, maintype=maintype, subtype=subtype, filename=filename)

        except Exception:

            continue



    mail_fake = (os.getenv("MAIL_FAKE") or "").strip().lower() in {"1", "true", "yes", "on"}

    if mail_fake:

        print(f"[{log_tag}] MAIL_FAKE activo - no se envía mail real")

        print(f"[{log_tag}] From:", msg["From"], "To:", msg["To"])

        print(f"[{log_tag}] Subject:", msg["Subject"])

        return {"ok": True, "fake": True}



    gmail_user = os.getenv("GMAIL_USER")

    gmail_password = os.getenv("GMAIL_PASSWORD")

    if not gmail_user or not gmail_password:

        raise RuntimeError("Configurar GMAIL_USER y GMAIL_PASSWORD en variables de entorno")



    with smtplib.SMTP("smtp.gmail.com", 587, timeout=20) as smtp:

        smtp.starttls()

        smtp.login(gmail_user, gmail_password)

        smtp.send_message(msg)

    return {"ok": True}





@bp.get("/mail_pagos")

def mail_pagos():

    _require_admin()

    company_id = request.args.get("company_id", type=int)

    desde = request.args.get("from")

    hasta = request.args.get("to")

    d_from = _parse_datetime_like(desde) if (desde or "").strip() else None

    d_to = _parse_datetime_like(hasta) if (hasta or "").strip() else None

    try:

        if d_to is not None:

            d_to = d_to + timedelta(days=1)

    except Exception:

        pass



    companies = Company.query.order_by(Company.nombre).all()



    q = (

        CollectionPayment.query

        .join(Order, CollectionPayment.order_id == Order.id)

        .join(Company, Order.company_id == Company.id)

        .filter(CollectionPayment.kind.in_(["PAYMENT", "CREDIT_NOTE"]))

        .options(

            selectinload(CollectionPayment.order).selectinload(Order.client),

            selectinload(CollectionPayment.order).selectinload(Order.company),

        )
    )

    if not _has_global_access():

        uid = _effective_user_id()

        if uid is not None:

            q = q.filter(Order.owner_user_id == uid)

    if company_id:

        q = q.filter(Order.company_id == company_id)

    if d_from is not None:

        q = q.filter(CollectionPayment.created_at >= d_from)

    if d_to is not None:

        q = q.filter(CollectionPayment.created_at < d_to)



    rows = q.order_by(Company.nombre.asc(), CollectionPayment.created_at.asc()).all()



    user_map = {}

    try:

        user_ids = []

        for p in rows:

            try:

                oid = int(getattr(getattr(p, "order", None), "owner_user_id", None) or 0)

            except Exception:

                oid = 0

            if oid > 0:

                user_ids.append(oid)

        if user_ids:

            for u in AppUser.query.filter(AppUser.id.in_(list(set(user_ids)))).all():

                try:

                    user_map[int(u.id)] = (getattr(u, "username", None) or f"Usuario {u.id}")

                except Exception:

                    pass

    except Exception:

        user_map = {}



    from collections import defaultdict

    grouped = defaultdict(lambda: defaultdict(list))

    totals_by_company = defaultdict(float)

    totals_by_user = defaultdict(lambda: defaultdict(float))



    for p in rows:

        o = getattr(p, "order", None)

        comp_obj = getattr(o, "company", None) if o else None

        comp_name = (getattr(comp_obj, "nombre", None) or "-") if comp_obj else "-"



        uid = None

        try:

            uid = int(getattr(o, "owner_user_id", None) or 0) if o else 0

        except Exception:

            uid = 0

        user_label = user_map.get(uid) if uid else None

        user_label = user_label or "Sin usuario"



        client = getattr(o, "client", None) if o else None

        client_name = " ".join([x for x in [getattr(client, "apellido", None), getattr(client, "nombre", None)] if x]) or "-"

        order_id = getattr(o, "id", None) if o else None



        kind = (getattr(p, "kind", "") or "").strip().upper()

        amt = 0.0

        try:

            amt = float(getattr(p, "amount", 0) or 0)

        except Exception:

            amt = 0.0

        signed = abs(amt)

        if kind == "CREDIT_NOTE":

            signed = -abs(amt)



        pay_date = None

        # Mostrar fecha efectiva del pago (due_date) y fallback a created_at

        pay_date = None

        try:

            dt_eff = getattr(p, "due_date", None) or getattr(p, "created_at", None)

            pay_date = dt_eff.date().isoformat() if dt_eff else None

        except Exception:

            pay_date = None



        method = (getattr(p, "method", None) or "")



        row_obj = {

            "payment_id": getattr(p, "id", None),

            "order_id": order_id,

            "client": client_name,

            "date": pay_date,

            "kind": kind,

            "method": method,

            "amount": float(signed or 0.0),

        }

        grouped[comp_name][user_label].append(row_obj)

        totals_by_company[comp_name] += float(signed or 0.0)

        totals_by_user[comp_name][user_label] += float(signed or 0.0)



    # Construir mails por empresa

    mails = []

    for comp in companies:

        if company_id and int(getattr(comp, "id", 0) or 0) != int(company_id):

            continue

        comp_name = (getattr(comp, "nombre", None) or "-")

        if comp_name not in grouped:

            continue

        to_all = _get_company_mail_to(comp)

        subject = f"Pagos {comp_name}"

        if d_from and d_to:

            try:

                subject = f"Pagos {comp_name} ({(d_from.date().isoformat())} a {(d_to - timedelta(days=1)).date().isoformat()})"

            except Exception:

                pass



        # Texto

        lines = []

        lines.append("RESUMEN DE PAGOS")

        lines.append(f"Empresa: {comp_name}")

        if d_from or d_to:

            try:

                lines.append(f"Período: {(d_from.date().isoformat() if d_from else '-') } a {( (d_to - timedelta(days=1)).date().isoformat() if d_to else '-') }")

            except Exception:

                pass

        lines.append("")

        for user_label, entries in grouped[comp_name].items():

            lines.append(f"Usuario: {user_label}")

            for r in entries:

                amt_s = _fmt_money_es(r.get("amount") or 0)

                lines.append(f"- {r.get('date') or ''} | Pedido {r.get('order_id') or ''} | {r.get('client') or ''} | {r.get('kind') or ''} {r.get('method') or ''} | $ {amt_s}")

            lines.append(f"Subtotal {user_label}: $ {_fmt_money_es(totals_by_user[comp_name].get(user_label) or 0)}")

            lines.append("")

        lines.append(f"TOTAL {comp_name}: $ {_fmt_money_es(totals_by_company.get(comp_name) or 0)}")

        body_text = "\n".join(lines)



        # HTML

        html_parts = []

        html_parts.append("<html><body style=\"font-family:Arial,Helvetica,sans-serif; font-size:14px;\">")

        html_parts.append(f"<h2 style=\"margin:0 0 8px 0;\">Resumen de pagos</h2>")

        html_parts.append(f"<div><strong>Empresa:</strong> {comp_name}</div>")

        if d_from or d_to:

            try:

                html_parts.append(f"<div><strong>Período:</strong> {(d_from.date().isoformat() if d_from else '-')} a {((d_to - timedelta(days=1)).date().isoformat() if d_to else '-')}</div>")

            except Exception:

                pass

        html_parts.append("<hr style=\"border:none;border-top:1px solid #ddd;margin:12px 0;\"/>")

        for user_label, entries in grouped[comp_name].items():

            html_parts.append(f"<h3 style=\"margin:14px 0 6px 0; font-size:15px;\">Usuario: {user_label}</h3>")

            html_parts.append("<table style=\"border-collapse:collapse; width:100%;\" border=\"1\" cellpadding=\"6\">")

            html_parts.append("<thead><tr style=\"background:#f3f4f6;\"><th>Fecha</th><th>Pedido</th><th>Cliente</th><th>Tipo</th><th>Método</th><th style=\"text-align:right;\">Monto</th></tr></thead>")

            html_parts.append("<tbody>")

            for r in entries:

                amt_s = _fmt_money_es(r.get("amount") or 0)

                html_parts.append(

                    "<tr>"

                    f"<td>{r.get('date') or ''}</td>"

                    f"<td>{r.get('order_id') or ''}</td>"

                    f"<td>{r.get('client') or ''}</td>"

                    f"<td>{r.get('kind') or ''}</td>"

                    f"<td>{r.get('method') or ''}</td>"

                    f"<td style=\"text-align:right;\">$ {amt_s}</td>"

                    "</tr>"

                )

            html_parts.append("</tbody></table>")

            html_parts.append(f"<div style=\"margin-top:6px;\"><strong>Subtotal {user_label}:</strong> $ {_fmt_money_es(totals_by_user[comp_name].get(user_label) or 0)}</div>")

        html_parts.append("<hr style=\"border:none;border-top:1px solid #ddd;margin:12px 0;\"/>")

        html_parts.append(f"<div style=\"font-size:16px;\"><strong>TOTAL {comp_name}:</strong> $ {_fmt_money_es(totals_by_company.get(comp_name) or 0)}</div>")

        html_parts.append("</body></html>")

        body_html = "".join(html_parts)



        mails.append({

            "company_id": int(getattr(comp, "id", 0) or 0),

            "company": comp_name,

            "to": ", ".join(to_all),

            "subject": subject,

            "body_text": body_text,

            "body_html": body_html,

            "total": float(totals_by_company.get(comp_name) or 0),

        })



    return render_template(

        "mail_pagos.html",

        active="mail_pagos",

        companies=companies,

        company_id=company_id,

        from_val=(d_from.date().isoformat() if d_from else (desde or "")),

        to_val=((d_to - timedelta(days=1)).date().isoformat() if d_to else (hasta or "")),

        mails=mails,

    )





@bp.post("/mail_pagos/enviar")

def mail_pagos_enviar():

    _require_admin()

    company_id = request.form.get("company_id", type=int)

    subject = (request.form.get("subject") or "").strip()

    body_html = (request.form.get("body_html") or "").strip()

    body_text = (request.form.get("body_text") or "").strip()

    if not company_id:

        return jsonify({"ok": False, "error": "company_id requerido"}), 400

    if not subject:

        return jsonify({"ok": False, "error": "subject requerido"}), 400

    comp = Company.query.get_or_404(int(company_id))

    to_all = _get_company_mail_to(comp)

    if not to_all:

        return jsonify({"ok": False, "error": "La empresa no tiene mail_pago configurado"}), 400



    try:

        res = _send_email(subject=subject, to_all=to_all, body_text=body_text or "(sin texto)", body_html=body_html or None)

        return jsonify({"ok": True, **(res or {})})

    except Exception as e:

        err_text = str(e) or "Error desconocido enviando el mail"

        if "Network is unreachable" in err_text:

            err_text = "No se pudo conectar al servidor de correo (sin conexión o bloqueado)."

        return jsonify({"ok": False, "error": err_text}), 500





def _has_global_access() -> bool:

    try:

        if not current_user.is_authenticated:

            return False

        if not getattr(current_user, "is_admin", False):

            return False

        # Si está en modo soporte (impersonación), NO es acceso global.

        return not bool(session.get("impersonate_user_id"))

    except Exception:

        return False





def _effective_user_id():

    if not getattr(current_user, "is_authenticated", False):

        return None



    try:

        if getattr(current_user, "is_admin", False):

            imp = session.get("impersonate_user_id")

            if imp:

                try:

                    return int(imp)

                except Exception:

                    return None

    except Exception:

        pass

    try:

        return int(getattr(current_user, "id", None))

    except Exception:

        return None





def _require_owner(obj):

    if obj is None:

        return

    if _has_global_access():

        return

    uid = _effective_user_id()

    try:

        if uid is None:

            abort(403)

        if getattr(obj, "owner_user_id", None) != uid:

            abort(403)

    except Exception:

        abort(403)





def _only_digits(v: str) -> str:

    return "".join([ch for ch in (v or "") if ch.isdigit()])





def _parse_money_like(raw: str) -> float:

    s = (raw or "").strip()

    if not s:

        return 0.0

    s = s.replace(" ", "")

    if "," in s and "." in s:

        s = s.replace(".", "").replace(",", ".")

    elif "," in s:

        s = s.replace(",", ".")

    else:

        if "." in s:

            try:

                parts = s.split(".")

                all_digits = all((p.isdigit() or p == "") for p in parts)

                last = parts[-1] if parts else ""

                if all_digits and (len(parts) > 2 or len(last) == 3):

                    s = "".join(parts)

            except Exception:

                pass

    s = "".join([ch for ch in s if (ch.isdigit() or ch in [".", "-"])])

    try:

        return float(s)

    except Exception:

        return 0.0





def _balance_owner_user_id():

    try:

        return _effective_user_id()

    except Exception:

        return None





def _safe_filter_not_voided(q):

    """Railway/Postgres: tolerar faltante de columna collection_payment.voided_at.



    Si la columna no existe todavía, el filtro dispara UndefinedColumn. Reintentamos sin filtro.

    """

    try:

        return q.filter(CollectionPayment.voided_at.is_(None))

    except Exception:

        try:

            db.session.rollback()

        except Exception:

            pass

        return q





@bp.get("/api/cobranzas/saldo_historico")

def api_cobranzas_get_saldo_historico():

    # Compatibilidad: este endpoint histórico ahora devuelve "deudas pendientes"
    # para evitar conceptos de saldo ajustable/manual.
    client_id = request.args.get("client_id", type=int)
    if not client_id:
        return jsonify({"ok": True, "computed_total": 0.0, "adjustment": 0.0, "desired_total": 0.0, "pending_total": 0.0})

    try:
        pending_total = float(_compute_client_pending_total(client_id) or 0.0)
    except Exception:
        pending_total = 0.0

    return jsonify({
        "ok": True,
        "computed_total": float(pending_total),
        "adjustment": 0.0,
        "desired_total": float(pending_total),
        "pending_total": float(pending_total),
    })


def _compute_client_pending_total(client_id: int) -> float:
    client = Client.query.get_or_404(client_id)
    _require_owner(client)

    q = (
        Order.query
        .outerjoin(Collection, Collection.order_id == Order.id)
        .options(
            selectinload(Order.collection),
            selectinload(Order.logistics),
        )
        .filter(Order.client_id == client_id)
        .filter(Order.deleted_at.is_(None))
    )

    if not _has_global_access():
        uid = _effective_user_id()
        if uid is not None:
            q = q.filter((Order.owner_user_id == uid) | (Order.owner_user_id.is_(None)))

    total_pending = 0.0
    now_dt = datetime.utcnow()
    for o in q.all():
        col = getattr(o, "collection", None)
        if col is not None and getattr(col, "fecha_cobro_efectiva", None):
            continue

        monto_val = 0.0
        try:
            candidates = []
            if col is not None and getattr(col, "monto", None) is not None:
                candidates.append(float(col.monto))
            if getattr(o, "precio_final", None) is not None:
                candidates.append(float(o.precio_final))
            lg = getattr(o, "logistics", None)
            if lg is not None and getattr(lg, "precio", None) is not None:
                candidates.append(float(lg.precio))
            for v in candidates:
                if v is not None and v > 0:
                    monto_val = float(v)
                    break
            if monto_val <= 0.0 and candidates:
                monto_val = float(candidates[0] or 0.0)
        except Exception:
            monto_val = 0.0

        total_paid = 0.0
        total_credit = 0.0
        try:
            pay_q = (
                CollectionPayment.query
                .filter_by(order_id=o.id)
                .filter(CollectionPayment.kind != "DRAFT")
            )
            try:
                pay_q = _safe_filter_not_voided(pay_q)
            except Exception:
                pay_q = pay_q.filter(CollectionPayment.voided_at.is_(None))

            for p in pay_q.all():
                kind = (getattr(p, "kind", "") or "").strip().upper()
                amt = float(getattr(p, "amount", 0) or 0)
                if kind == "CREDIT_NOTE":
                    total_credit += abs(amt)
                elif kind == "PAYMENT":
                    total_paid += abs(amt)
        except Exception:
            total_paid = 0.0
            total_credit = 0.0

        # Mismo criterio conceptual de /deudas para estados pendientes.
        partial_exists = False
        try:
            pay_exists_q = (
                CollectionPayment.query
                .filter(CollectionPayment.order_id == o.id)
                .filter(CollectionPayment.kind != "DRAFT")
            )
            try:
                pay_exists_q = _safe_filter_not_voided(pay_exists_q)
            except Exception:
                pay_exists_q = pay_exists_q.filter(CollectionPayment.voided_at.is_(None))
            partial_exists = (pay_exists_q.first() is not None)
        except Exception:
            partial_exists = False
        if not partial_exists:
            try:
                partial_exists = (
                    CollectionDraft.query
                    .filter(CollectionDraft.order_id == o.id)
                    .first()
                    is not None
                )
            except Exception:
                partial_exists = False

        has_due = bool(col is not None and getattr(col, "fecha_pago_estimada", None) is not None)
        overdue = bool(has_due and getattr(col, "fecha_pago_estimada", None) < now_dt)
        is_pending_status = bool(partial_exists or (not has_due) or overdue or (has_due and (not overdue) and (not partial_exists)))
        if not is_pending_status:
            continue

        remaining = float(monto_val or 0.0) - float(total_paid or 0.0) - float(total_credit or 0.0)
        if remaining > 0.009:
            total_pending += float(remaining)

    return float(total_pending or 0.0)


@bp.get("/api/cobranzas/deudas_pendientes")
def api_cobranzas_deudas_pendientes():
    client_id = request.args.get("client_id", type=int)
    if not client_id:
        return jsonify({"ok": True, "pending_total": 0.0})

    try:
        pending_total = float(_compute_client_pending_total(client_id) or 0.0)
    except Exception:
        pending_total = 0.0

    return jsonify({"ok": True, "pending_total": float(pending_total)})





@bp.post("/api/cobranzas/saldo_historico")

def api_cobranzas_set_saldo_historico():

    client_id = request.form.get("client_id", type=int)

    company_id = request.form.get("company_id", type=int)

    desired_total_raw = (request.form.get("desired_total") or "").strip()

    computed_raw = (request.form.get("computed_total") or "").strip()

    if not client_id or not company_id:

        abort(400)



    client = Client.query.get_or_404(client_id)

    _require_owner(client)



    desired_total = _parse_money_like(desired_total_raw)

    computed_total = _parse_money_like(computed_raw)

    adjustment = float(desired_total or 0.0) - float(computed_total or 0.0)



    uid = _balance_owner_user_id()

    row = (

        ClientCompanyBalance.query

        .filter_by(owner_user_id=uid, client_id=client_id, company_id=company_id)

        .first()

    )

    if row is None:

        row = ClientCompanyBalance(owner_user_id=uid, client_id=client_id, company_id=company_id)

        db.session.add(row)

    try:

        row.balance_adjustment = float(adjustment or 0.0)

    except Exception:

        row.balance_adjustment = 0

    db.session.commit()

    return jsonify({"ok": True, "adjustment": float(adjustment or 0.0)})





def _require_admin():

    try:

        if not current_user.is_authenticated:

            abort(403)

        if not getattr(current_user, "is_admin", False):

            abort(403)

    except Exception:

        abort(403)





@bp.get("/usuarios")

def usuarios():

    _require_admin()

    users = AppUser.query.order_by(AppUser.username.asc()).all()

    imp_id = session.get("impersonate_user_id")

    try:

        imp_id = int(imp_id) if imp_id else None

    except Exception:

        imp_id = None



    perms_map = {}

    try:

        for u in users:

            try:

                perms_map[int(u.id)] = _parse_permissions_json(getattr(u, "permissions_json", None))

            except Exception:

                continue

    except Exception:

        perms_map = {}

    # Módulos disponibles para permisos (simple y explícito)

    mod_labels = {

        "dashboard": "Dashboard",

        "calendario": "Calendario",

        "clientes": "Clientes",

        "empresas": "Empresas",

        "pedidos": "Pedidos",

        "status": "Status Mercadería",

        "nueva_cobranza": "Nuevas cobranzas",

        "deudas": "Deudas pendientes",

        "historial": "Historial",

        "crm": "CRM",

        "comisiones": "Comisiones",

        "mail_pagos": "Mail Pagos",

        "usuarios": "Usuarios (admin)",

    }

    return render_template(

        "usuarios.html",

        active="usuarios",

        users=users,

        impersonate_user_id=imp_id,

        modules=_MODULES,

        module_labels=mod_labels,

        perms_map=perms_map,

    )





@bp.post("/usuarios/<int:user_id>/permisos")

def usuarios_set_permisos(user_id: int):

    _require_admin()

    u = AppUser.query.get_or_404(user_id)

    allowed = set(request.form.getlist("modules"))

    data = {}

    for m in _MODULES:

        data[m] = (m in allowed)

    try:

        u.permissions_json = json.dumps(data, ensure_ascii=False)

    except Exception:

        u.permissions_json = None

    db.session.commit()

    return redirect(url_for("main.usuarios"))





@bp.post("/usuarios/<int:user_id>/soporte")

def usuarios_soporte(user_id: int):

    _require_admin()

    # No permitir impersonar a uno mismo

    try:

        if int(getattr(current_user, "id", 0) or 0) == int(user_id):

            session.pop("impersonate_user_id", None)

            return redirect(url_for("main.usuarios"))

    except Exception:

        pass

    u = AppUser.query.get_or_404(user_id)

    session["impersonate_user_id"] = int(u.id)

    return redirect(url_for("main.index"))





@bp.post("/usuarios/salir_soporte")

def usuarios_salir_soporte():

    _require_admin()

    session.pop("impersonate_user_id", None)

    return redirect(url_for("main.usuarios"))





def _parse_amount_like(s: str):

    s = (s or "").strip()

    if s == "":

        return None

    s = s.replace(" ", "")

    if "," in s and "." in s:

        s = s.replace(".", "").replace(",", ".")

    else:

        if "," in s:

            s = s.replace(",", ".")

        else:

            parts = s.split(".")

            # Si hay puntos y el último grupo tiene 3 dígitos, asumir miles (p.ej. 3.040 o 1.245.340)

            # y eliminar los puntos. Esto evita que float("3.040") se convierta en 3.04.

            try:

                if len(parts) > 1:

                    last = parts[-1]

                    if last.isdigit() and len(last) == 3 and all(p.isdigit() for p in parts):

                        s = "".join(parts)

                    elif len(parts) > 2:

                        # múltiples puntos (sin coma): casi seguro miles

                        if all(p.isdigit() for p in parts):

                            s = "".join(parts)

            except Exception:

                pass

    try:

        return float(s)

    except Exception:

        try:

            digits = _only_digits(s)

            return float(digits) if digits != "" else None

        except Exception:

            return None





# ---------------------------------------------------------------------------

# FUENTE UNICA DE VERDAD del PARSEO de fechas de entrada.

#

# Formatos aceptados, en este orden de prioridad:

#   1. ISO, via datetime.fromisoformat: "AAAA-MM-DD", "AAAAMMDD",

#      "AAAA-MM-DDTHH:MM[:SS[.ffffff]]", "AAAA-MM-DD HH:MM", con offset o "Z".

#   2. DD/MM/AAAA y D/M/AAAA -- lo que escribe el usuario (mascara de Fase 2).

#   3. DD-MM-AAAA y D-M-AAAA.

#   4. AAAA-M-D (ISO sin cero a la izquierda; fromisoformat lo rechaza).

#   5. 8 digitos con cualquier separador ("10 09 2026", "10.09.2026") -> DDMMAAAA.

#

# Son DOS funciones publicas A PROPOSITO, no una: tienen tipo de retorno

# distinto y ~40 call sites que dependen de ese tipo.

#   - _parse_datetime_like -> datetime | None. La usan los call sites que

#     escriben columnas DateTime (fecha_compra, fecha_cobro_efectiva, ...).

#   - _parse_date_like     -> date | None. La usan los call sites de columnas

#     Date (fecha_incorporacion, fechas de comisiones) y los filtros de busqueda.

# NO se fusionan: se comparten los formatos. _parse_date_like delega en

# _parse_datetime_like y trunca la hora, asi nunca se separan.

#

# _RE_DMY_DASH / _RE_ISO_LOOSE son mutuamente excluyentes: una exige 1-2

# digitos al principio y la otra exige 4, asi que no hay ambiguedad entre

# "10-09-2026" (DD-MM-AAAA) y "2026-9-10" (ISO flojo).

# ---------------------------------------------------------------------------



_RE_DMY_DASH = re.compile(r"^(\d{1,2})-(\d{1,2})-(\d{4})$")

_RE_ISO_LOOSE = re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})$")



def _parse_date_flexible(raw: str):

    """Formatos NO-ISO (2 a 5 de la lista de arriba) -> date | None.



    Interno: los callers usan _parse_date_like / _parse_datetime_like.

    """

    raw = (raw or "").strip()

    if not raw:

        return None

    # DD/MM/AAAA -- se acepta cualquier largo de anio para no perder datos que

    # el parser viejo si aceptaba (ver "10/09/26" en el reporte de Fase 5).

    if "/" in raw:

        parts = [p.strip() for p in raw.split("/")]

        if len(parts) == 3:

            try:

                return date(int(parts[2]), int(parts[1]), int(parts[0]))

            except (ValueError, TypeError):

                pass

    m = _RE_DMY_DASH.match(raw)

    if m:

        try:

            return date(int(m.group(3)), int(m.group(2)), int(m.group(1)))

        except (ValueError, TypeError):

            pass

    m = _RE_ISO_LOOSE.match(raw)

    if m:

        try:

            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))

        except (ValueError, TypeError):

            pass

    digits = _only_digits(raw)

    if len(digits) == 8:

        try:

            return date(int(digits[4:8]), int(digits[2:4]), int(digits[0:2]))

        except (ValueError, TypeError):

            pass

    return None



def _parse_datetime_like(raw: str):

    """Cualquiera de los formatos aceptados -> datetime | None."""

    raw = (raw or "").strip()

    if not raw:

        return None

    # ISO completo (fecha o fecha+hora). Va PRIMERO: es lo que mandan los

    # inputs con flatpickr (dateFormat 'Y-m-d') y las APIs internas.

    try:

        return datetime.fromisoformat(raw)

    except (ValueError, TypeError):

        pass

    d = _parse_date_flexible(raw)

    if d is not None:

        return datetime(d.year, d.month, d.day)

    return None



def _parse_date_like(raw: str):

    """Cualquiera de los formatos aceptados -> date | None (trunca la hora)."""

    dt = _parse_datetime_like(raw)

    return dt.date() if dt is not None else None



# ---------------------------------------------------------------------------

# FUENTE UNICA DE VERDAD de la VISUALIZACION de fechas.

#

# Toda fecha que el usuario VE se muestra como DD/MM/AAAA. Se expone como

# filtro Jinja para no repetir strftime ni, peor, rebanar strings ISO a mano

# en los templates (comisiones.html lo hacia con last_paid_iso[8:10] ~ '/' ...).

#

# Acepta date, datetime, string en cualquiera de los formatos que parsea

# _parse_datetime_like, y None. Si no puede interpretar el valor devuelve

# `default` en lugar de reventar el render.

#

# Uso en templates:

#     {{ o.created_at|fecha }}            -> "10/09/2026"

#     {{ o.created_at|fecha('-') }}        -> "-" si es None/invalido

#     {{ s.uploaded_at|fecha_hora }}       -> "10/09/2026 14:30"

#     {{ d|fecha_iso }}                    -> "2026-09-10" (para value= de inputs)

#

# El formato corto "%d/%m" NO se expone a proposito: la unica excepcion viva

# son las etiquetas del eje X del grafico del dashboard (ver chart_labels).

# ---------------------------------------------------------------------------



_FMT_FECHA = "%d/%m/%Y"

_FMT_FECHA_HORA = "%d/%m/%Y %H:%M"



def _coerce_fecha(value):

    """date | datetime | str | None -> date | datetime | None."""

    if value is None:

        return None

    if isinstance(value, datetime):

        return value

    if isinstance(value, date):

        return value

    if isinstance(value, str):

        return _parse_datetime_like(value)

    return None



def fmt_fecha(value, default: str = "") -> str:

    """DD/MM/AAAA. Fuente unica de verdad de la fecha visible."""

    d = _coerce_fecha(value)

    return d.strftime(_FMT_FECHA) if d is not None else default



def fmt_fecha_hora(value, default: str = "") -> str:

    """DD/MM/AAAA HH:MM. Solo donde la hora aporta (auditoria, uploads)."""

    d = _coerce_fecha(value)

    if d is None:

        return default

    if not isinstance(d, datetime):

        return d.strftime(_FMT_FECHA)

    return d.strftime(_FMT_FECHA_HORA)



def fmt_fecha_iso(value, default: str = "") -> str:

    """AAAA-MM-DD. Para los value= de los inputs: es lo que espera el backend

    y el dateFormat de flatpickr. NO es formato de lectura."""

    d = _coerce_fecha(value)

    if d is None:

        return default

    return (d.date() if isinstance(d, datetime) else d).isoformat()



bp.add_app_template_filter(fmt_fecha, "fecha")

bp.add_app_template_filter(fmt_fecha_hora, "fecha_hora")

bp.add_app_template_filter(fmt_fecha_iso, "fecha_iso")





def _compute_alerts_for_all_clients(

    now_dt: datetime,

    include_delivery_alerts: bool = False,

    include_inactivity_alerts: bool = False,

):

    # Alertas activas = entregas vencidas + cobranzas vencidas.

    # Se respeta el estado del usuario en ClientAlertState (dismissed/snoozed).

    KIND_ENT = "ENTREGA_ATRASADA"

    KIND_COB = "COBRANZA_ATRASADA"

    KIND_INACT = "EMPRESA_INACTIVA"



    # Las alertas se calculan desde Status Mercadería (LogisticsStatus) y Deudas (Collection).

    # Si aparece una alerta para un par Cliente–Empresa sin registro en ClientCompanyLink,

    # se crea el vínculo para mantener consistencia (sin depender del status).



    def _ensure_link(client_id: int, company_id: int):

        try:

            if not client_id or not company_id:

                return

            lnk = ClientCompanyLink.query.filter_by(client_id=int(client_id), company_id=int(company_id)).first()

            if lnk is not None:

                return

            db.session.add(ClientCompanyLink(client_id=int(client_id), company_id=int(company_id), status=RelationStatus.TRABAJA, comprobante_tipo="FACTURA"))

            db.session.commit()

        except Exception:

            try:

                db.session.rollback()

            except Exception:

                pass



    def _muted_keys():

        muted = set()

        try:

            qst = ClientAlertState.query

            if not _has_global_access():

                uid = _effective_user_id()

                if uid is None:

                    return set()

                qst = qst.filter(ClientAlertState.owner_user_id == uid)

            qst = qst.filter(

                (ClientAlertState.dismissed_at.isnot(None))

                | (ClientAlertState.snoozed_until.isnot(None))

            )

            for st in qst.all():

                try:

                    if st.dismissed_at is not None:

                        muted.add((int(st.client_id), int(st.order_id), str(st.kind)))

                        continue

                    if st.snoozed_until is not None and st.snoozed_until > now_dt:

                        muted.add((int(st.client_id), int(st.order_id), str(st.kind)))

                        continue

                except Exception:

                    continue

        except Exception:

            try:

                db.session.rollback()

            except Exception:

                pass

            return set()

        return muted



    muted = _muted_keys()

    alerts = []

    now_date_local = date.today()

    try:

        if ZoneInfo:

            now_date_local = datetime.now(ZoneInfo("America/Argentina/Buenos_Aires")).date()

    except Exception:

        now_date_local = date.today()



    if include_delivery_alerts:

        try:

            # En la app, la entrega efectiva puede quedar registrada en LogisticsStatus o en Collection.

            q_ent = (

                LogisticsStatus.query

                .join(Order, LogisticsStatus.order_id == Order.id)

                .outerjoin(Collection, Collection.order_id == Order.id)

                .join(Client, Order.client_id == Client.id)

                .join(Company, Order.company_id == Company.id)

                .filter(LogisticsStatus.fecha_entrega_efectiva.is_(None))

                .filter(Collection.fecha_entrega_efectiva.is_(None))

                .filter(LogisticsStatus.fecha_entrega_estimada.isnot(None))

                .filter(LogisticsStatus.fecha_entrega_estimada < now_dt)

                .filter(Order.deleted_at.is_(None))

            )

            if not _has_global_access():

                uid = _effective_user_id()

                if uid is not None:

                    q_ent = q_ent.filter(Order.owner_user_id == uid)

            for lg in q_ent.all():

                o = getattr(lg, "order", None)

                if not o or not getattr(o, "client", None):

                    continue

                try:

                    _ensure_link(int(o.client_id), int(o.company_id))

                except Exception:

                    pass

                try:

                    key = (int(o.client_id), int(o.id), KIND_ENT)

                except Exception:

                    continue

                if key in muted:

                    continue

                client_name = f"{o.client.apellido} {o.client.nombre}" if o.client else "-"

                comp_name = (o.company.nombre if getattr(o, "company", None) else "-")

                alerts.append({

                    "client_id": int(o.client_id),

                    "client_name": client_name,

                    "client_razon_social": str(getattr(o.client, "apellido", "") or "-"),

                    "client_nombre": str(getattr(o.client, "nombre", "") or "-"),

                    "order_id": int(o.id),

                    "kind": KIND_ENT,

                    "message": "Entrega atrasada",

                    "severity": "danger",

                    "company": comp_name,

                })

        except Exception:

            try:

                db.session.rollback()

            except Exception:

                pass

            pass



    try:

        q_cob = (

            Collection.query

            .join(Order, Collection.order_id == Order.id)

            .outerjoin(LogisticsStatus, LogisticsStatus.order_id == Order.id)

            .join(Client, Order.client_id == Client.id)

            .join(Company, Order.company_id == Company.id)

            # ATRASADO según la FUENTE ÚNICA DE VERDAD (backend/models.py).
            # Antes esto era un copy-paste de la condición de /deudas y exigía
            # fecha_pago_estimada IS NOT NULL, así que las deudas con
            # vencimiento derivado quedaban invisibles para las notificaciones.
            .filter(condiciones_estado_cobranza(now_date_local).atrasado)

            .filter(Order.deleted_at.is_(None))

        )

        if not _has_global_access():

            uid = _effective_user_id()

            if uid is not None:

                q_cob = q_cob.filter(Order.owner_user_id == uid)

        for coll in q_cob.all():

            o = getattr(coll, "order", None)

            if not o or not getattr(o, "client", None):

                continue



            # Alinear con /api/cobranzas/pedidos_pendientes: si ya está saldado (por pagos/NC), no alertar.

            try:

                monto_val = 0.0

                has_monto = False

                try:

                    candidates = []

                    if getattr(coll, "monto", None) is not None:

                        candidates.append(float(coll.monto))

                    if getattr(o, "precio_final", None) is not None:

                        candidates.append(float(o.precio_final))

                    lg = getattr(o, "logistics", None)

                    if lg is not None and getattr(lg, "precio", None) is not None:

                        candidates.append(float(lg.precio))

                    for v in candidates:

                        if v is not None and v > 0:

                            monto_val = float(v)

                            has_monto = True

                            break

                    if (monto_val or 0.0) <= 0 and candidates:

                        # Si hay candidatos pero ninguno > 0, tratar como "sin monto".

                        monto_val = 0.0

                        has_monto = False

                except Exception:

                    monto_val = float(getattr(coll, "monto", 0) or 0)

                    has_monto = bool((monto_val or 0.0) > 0)



                total_paid = 0.0

                total_credit = 0.0

                qpay = (

                    CollectionPayment.query

                    .filter_by(order_id=o.id)

                    .filter(CollectionPayment.kind != "DRAFT")

                )

                try:

                    qpay = _safe_filter_not_voided(qpay)

                except Exception:

                    pass

                for p in qpay.all():

                    kind = (getattr(p, "kind", "") or "").strip().upper()

                    amt = float(getattr(p, "amount", 0) or 0)

                    if kind == "CREDIT_NOTE":

                        total_credit += abs(amt)

                    elif kind == "PAYMENT":

                        total_paid += abs(amt)

                remaining = float(monto_val or 0.0) - float(total_paid or 0.0) - float(total_credit or 0.0)

                # Si no tiene monto aún, igual debe alertar por vencimiento (coincidir con Deudas).

                if has_monto and remaining <= 0.009:

                    continue

            except Exception:

                pass



            try:

                _ensure_link(int(o.client_id), int(o.company_id))

            except Exception:

                pass

            try:

                key = (int(o.client_id), int(o.id), KIND_COB)

            except Exception:

                continue

            if key in muted:

                continue

            client_name = f"{o.client.apellido} {o.client.nombre}" if o.client else "-"

            comp_name = (o.company.nombre if getattr(o, "company", None) else "-")

            alerts.append({

                "client_id": int(o.client_id),

                "client_name": client_name,

                "client_razon_social": str(getattr(o.client, "apellido", "") or "-"),

                "client_nombre": str(getattr(o.client, "nombre", "") or "-"),

                "order_id": int(o.id),

                "kind": KIND_COB,

                "message": "Cobranza atrasada",

                "severity": "warning",

                "company": comp_name,

            })

    except Exception:

        try:

            db.session.rollback()

        except Exception:

            pass

        pass



    if include_inactivity_alerts:

        try:

            # Empresa sin compras (por cliente-empresa) desde hace 60+ días.

            # Se usa el último order_id como ancla para poder reutilizar ClientAlertState (visto/posponer).

            cutoff = now_dt - timedelta(days=60)

            q_last = (

                db.session.query(

                    Order.id.label("order_id"),

                    Order.client_id.label("client_id"),

                    Order.company_id.label("company_id"),

                    Order.created_at.label("created_at"),

                    LogisticsStatus.fecha_compra.label("fecha_compra"),

                )

                .outerjoin(LogisticsStatus, LogisticsStatus.order_id == Order.id)

                .filter(Order.deleted_at.is_(None))

            )

            if not _has_global_access():

                uid = _effective_user_id()

                if uid is not None:

                    q_last = q_last.filter(Order.owner_user_id == uid)

            base_rows = q_last.all()

            latest_by_pair = {}

            for r in base_rows:

                try:

                    client_id = int(getattr(r, "client_id", 0) or 0)

                    company_id = int(getattr(r, "company_id", 0) or 0)

                    order_id = int(getattr(r, "order_id", 0) or 0)

                    if not client_id or not company_id or not order_id:

                        continue

                    compra_dt = getattr(r, "fecha_compra", None)

                    created_dt = getattr(r, "created_at", None)

                    effective_dt = compra_dt or created_dt

                    if not effective_dt:

                        continue

                    key_pair = (client_id, company_id)

                    prev = latest_by_pair.get(key_pair)

                    is_newer = (
                        prev is None
                        or effective_dt > prev["last_dt"]
                        or (effective_dt == prev["last_dt"] and order_id > prev["last_order_id"])
                    )

                    if is_newer:

                        latest_by_pair[key_pair] = {
                            "client_id": client_id,
                            "company_id": company_id,
                            "last_dt": effective_dt,
                            "last_order_id": order_id,
                        }

                except Exception:

                    continue

            rows = list(latest_by_pair.values())

            if rows:

                client_ids = sorted({int(r.get("client_id")) for r in rows if r and r.get("client_id")})

                company_ids = sorted({int(r.get("company_id")) for r in rows if r and r.get("company_id")})

                clients_map = {}

                companies_map = {}

                try:

                    if client_ids:

                        cq = Client.query.filter(Client.id.in_(client_ids))

                        if not _has_global_access():

                            uid = _effective_user_id()

                            if uid is not None:

                                cq = cq.filter(Client.owner_user_id == uid)

                        clients_map = {c.id: c for c in cq.all()}

                except Exception:

                    clients_map = {}

                try:

                    if company_ids:

                        companies_map = {c.id: c for c in Company.query.filter(Company.id.in_(company_ids)).all()}

                except Exception:

                    companies_map = {}



                for r in rows:

                    try:

                        last_dt = r.get("last_dt")

                        if not last_dt or last_dt >= cutoff:

                            continue

                        client_id = int(r.get("client_id", 0) or 0)

                        company_id = int(r.get("company_id", 0) or 0)

                        last_order_id = int(r.get("last_order_id", 0) or 0)

                        if not client_id or not company_id or not last_order_id:

                            continue

                        key = (client_id, last_order_id, KIND_INACT)

                        if key in muted:

                            continue



                        cl = clients_map.get(client_id)

                        co = companies_map.get(company_id)

                        client_razon_social = "-"

                        client_nombre = "-"

                        try:

                            if cl is not None:

                                client_razon_social = str(getattr(cl, "apellido", None) or "-")

                                client_nombre = str(getattr(cl, "nombre", None) or "-")

                        except Exception:

                            client_razon_social = "-"

                            client_nombre = "-"

                        client_name = f"{client_razon_social} {client_nombre}".strip()

                        comp_name = (getattr(co, "nombre", None) or "-")

                        days = 0

                        try:

                            days = int((now_dt - last_dt).days)

                        except Exception:

                            days = 0

                        days = max(0, int(days or 0))

                        months = 0

                        try:

                            months = int((int(now_dt.year) - int(last_dt.year)) * 12 + (int(now_dt.month) - int(last_dt.month)))

                            if int(now_dt.day) < int(last_dt.day):

                                months -= 1

                        except Exception:

                            months = int(days // 30)

                        months = max(2, int(months or 0))

                        inact_bucket = "6PLUS" if months >= 6 else str(months)

                        msg_months = "6+" if months >= 6 else str(months)

                        alerts.append({

                            "client_id": client_id,

                            "client_name": client_name,

                            "client_razon_social": client_razon_social,

                            "client_nombre": client_nombre,

                            "order_id": last_order_id,

                            "kind": KIND_INACT,

                            "message": f"Empresa sin compras hace {msg_months} meses",

                            "severity": "warning",

                            "company": comp_name,

                            "last_order_date": last_dt,

                            "inactivity_months": months,

                            "inactivity_bucket": inact_bucket,

                        })

                    except Exception:

                        continue

        except Exception:

            try:

                db.session.rollback()

            except Exception:

                pass

            pass



    return alerts





def _refresh_link_statuses_by_last_order(now_dt: datetime, client_ids=None):

    threshold = now_dt - timedelta(days=90)



    q_links = ClientCompanyLink.query

    if client_ids:

        q_links = q_links.filter(ClientCompanyLink.client_id.in_(client_ids))

    links = q_links.all()

    if not links:

        return



    pairs = {(l.client_id, l.company_id) for l in links}

    if not pairs:

        return



    from sqlalchemy import or_, and_

    conds = [and_(Order.client_id == cid, Order.company_id == coid) for (cid, coid) in pairs]

    last_map = {}

    if conds:

        rows = (

            db.session.query(Order.client_id, Order.company_id, func.max(Order.created_at))

            .filter(or_(*conds))

            .group_by(Order.client_id, Order.company_id)

            .all()

        )

        last_map = {(cid, coid): last_dt for (cid, coid, last_dt) in rows}



    changed = False

    for l in links:

        if l.status != RelationStatus.TRABAJA:

            continue

        last_dt = last_map.get((l.client_id, l.company_id))

        if (not last_dt) or (last_dt < threshold):

            l.status = RelationStatus.TRABAJABA

            changed = True

    if changed:

        db.session.commit()







@bp.before_app_request

def auto_refresh_link_statuses_global():

    # Ejecutar la regla en toda la app, pero con throttling para no impactar rendimiento.

    # Se ejecuta como máximo cada 10 minutos por proceso.

    try:

        if request.endpoint and str(request.endpoint).startswith("static"):

            return

        last = current_app.config.get("_LAST_LINK_REFRESH_AT")

        now_dt = datetime.utcnow()

        if last and isinstance(last, datetime) and (now_dt - last) < timedelta(minutes=10):

            return

        current_app.config["_LAST_LINK_REFRESH_AT"] = now_dt

        _refresh_link_statuses_by_last_order(now_dt)

    except Exception:

        # Nunca romper navegación por errores en tarea automática

        try:

            db.session.rollback()

        except Exception:

            pass





@bp.before_app_request

def auto_patch_new_columns():

    # Evitar caídas por DB desactualizada (especialmente SQLite): agregar columnas nuevas si faltan.

    # Se intenta solo una vez por proceso.

    try:

        if request.endpoint and str(request.endpoint).startswith("static"):

            return

        if current_app.config.get("_DID_PATCH_NEW_COLS") is True:

            return

        # Marcar como "en progreso" para evitar loops; si falla, se limpia para reintentar.

        current_app.config["_DID_PATCH_NEW_COLS"] = "running"

        dialect = db.session.bind.dialect.name if db.session.bind is not None else ""

        if dialect == "postgresql":

            stmts = [

                """

                CREATE TABLE IF NOT EXISTS client_document (

                    id SERIAL PRIMARY KEY,

                    client_id INTEGER NOT NULL,

                    category VARCHAR(32),

                    filename VARCHAR(255) NOT NULL,

                    filepath VARCHAR(500) NOT NULL,

                    data BYTEA,

                    mimetype VARCHAR(120),

                    size INTEGER,

                    uploaded_at TIMESTAMP

                )

                """ ,

                "ALTER TABLE client_company_link ADD COLUMN IF NOT EXISTS descuento NUMERIC(5,2)",

                "ALTER TABLE client_delivery_place ADD COLUMN IF NOT EXISTS provincia VARCHAR(80)",

                "ALTER TABLE client_delivery_place ADD COLUMN IF NOT EXISTS nota VARCHAR(255)",

                "ALTER TABLE client_birthday ADD COLUMN IF NOT EXISTS notas TEXT",

                "ALTER TABLE client ADD COLUMN IF NOT EXISTS transporte_contacto VARCHAR(255)",

                "ALTER TABLE client ADD COLUMN IF NOT EXISTS forma_pago_habitual VARCHAR(32)",

                "ALTER TABLE client_document ADD COLUMN IF NOT EXISTS category VARCHAR(32)",

                "ALTER TABLE company ADD COLUMN IF NOT EXISTS pedido_estandar_recomendado TEXT",

                "ALTER TABLE company ADD COLUMN IF NOT EXISTS nota_pedido TEXT",

                "ALTER TABLE company ADD COLUMN IF NOT EXISTS plazo_usual VARCHAR(120)",

                "ALTER TABLE company ADD COLUMN IF NOT EXISTS forma_pago_default VARCHAR(32)",

                "ALTER TABLE \"order\" ADD COLUMN IF NOT EXISTS plazo_pago_dias INTEGER",

                """

                CREATE TABLE IF NOT EXISTS collection_payment (

                    id SERIAL PRIMARY KEY,

                    order_id INTEGER NOT NULL,

                    kind VARCHAR(16) NOT NULL,

                    method VARCHAR(32),

                    amount NUMERIC(12,2),

                    due_date TIMESTAMP,

                    attachment_url VARCHAR(500),

                    notes TEXT,

                    created_at TIMESTAMP

                )

                """,

            ]

        else:

            stmts = [

                """

                CREATE TABLE IF NOT EXISTS client_document (

                    id INTEGER PRIMARY KEY AUTOINCREMENT,

                    client_id INTEGER NOT NULL,

                    category VARCHAR(32),

                    filename VARCHAR(255) NOT NULL,

                    filepath VARCHAR(500) NOT NULL,

                    data BLOB,

                    mimetype VARCHAR(120),

                    size INTEGER,

                    uploaded_at DATETIME

                )

                """ ,

                "ALTER TABLE client_company_link ADD COLUMN descuento REAL",

                "ALTER TABLE client_delivery_place ADD COLUMN provincia VARCHAR(80)",

                "ALTER TABLE client_delivery_place ADD COLUMN nota VARCHAR(255)",

                "ALTER TABLE client_birthday ADD COLUMN notas TEXT",

                "ALTER TABLE client ADD COLUMN transporte_contacto VARCHAR(255)",

                "ALTER TABLE client ADD COLUMN forma_pago_habitual VARCHAR(32)",

                "ALTER TABLE client_document ADD COLUMN category VARCHAR(32)",

                "ALTER TABLE company ADD COLUMN pedido_estandar_recomendado TEXT",

                "ALTER TABLE company ADD COLUMN nota_pedido TEXT",

                "ALTER TABLE company ADD COLUMN plazo_usual VARCHAR(120)",

                "ALTER TABLE company ADD COLUMN forma_pago_default VARCHAR(32)",

                "ALTER TABLE \"order\" ADD COLUMN plazo_pago_dias INTEGER",

                """

                CREATE TABLE IF NOT EXISTS collection_payment (

                    id INTEGER PRIMARY KEY AUTOINCREMENT,

                    order_id INTEGER NOT NULL,

                    kind VARCHAR(16) NOT NULL,

                    method VARCHAR(32),

                    amount REAL,

                    due_date DATETIME,

                    attachment_url VARCHAR(500),

                    notes TEXT,

                    created_at DATETIME

                )

                """,

                "ALTER TABLE collection_payment ADD COLUMN voided_at DATETIME",

                "ALTER TABLE collection_payment ADD COLUMN voided_by_user_id INTEGER",

                "ALTER TABLE collection_payment ADD COLUMN voided_reason TEXT",

            ]

        for sql in stmts:

            try:

                db.session.execute(text(sql))

                db.session.commit()

            except Exception:

                db.session.rollback()

                continue



        try:

            if dialect == "postgresql":

                try:

                    db.session.rollback()

                except Exception:

                    pass



                do_blocks = [

                    """

                    DO $$

                    BEGIN

                        ALTER TABLE collection_payment ADD COLUMN voided_at TIMESTAMP;

                    EXCEPTION

                        WHEN duplicate_column THEN NULL;

                    END $$;

                    """,

                    """

                    DO $$

                    BEGIN

                        ALTER TABLE collection_payment ADD COLUMN voided_by_user_id INTEGER;

                    EXCEPTION

                        WHEN duplicate_column THEN NULL;

                    END $$;

                    """,

                    """

                    DO $$

                    BEGIN

                        ALTER TABLE collection_payment ADD COLUMN voided_reason TEXT;

                    EXCEPTION

                        WHEN duplicate_column THEN NULL;

                    END $$;

                    """,

                ]

                for sql in do_blocks:

                    try:

                        db.session.execute(text(sql))

                        db.session.commit()

                    except Exception:

                        db.session.rollback()

        except Exception:

            try:

                db.session.rollback()

            except Exception:

                pass



        current_app.config["_DID_PATCH_NEW_COLS"] = True

    except Exception:

        try:

            db.session.rollback()

        except Exception:

            pass



        # Permitir reintentar en el próximo request

        try:

            current_app.config.pop("_DID_PATCH_NEW_COLS", None)

        except Exception:

            pass





@bp.app_context_processor

def inject_notification_count():

    try:

        # IMPORTANTE: esto corre en CADA request para renderizar el navbar.

        # Evitar lógica pesada (loops de clientes/pedidos) que degrada toda la app.

        support_user_id = None

        support_username = None

        try:

            if getattr(current_user, "is_authenticated", False) and getattr(current_user, "is_admin", False):

                imp = session.get("impersonate_user_id")

                if imp:

                    try:

                        support_user_id = int(imp)

                    except Exception:

                        support_user_id = None

        except Exception:

            support_user_id = None



        if support_user_id is not None:

            try:

                u = AppUser.query.get(int(support_user_id))

                support_username = getattr(u, "username", None) if u else None

            except Exception:

                support_username = None



        now_dt = datetime.utcnow()

        try:

            cache_key = "G" if _has_global_access() else f"U:{_effective_user_id()}"

        except Exception:

            cache_key = "G"



        last_map = current_app.config.get("_LAST_NOTIF_COUNT_AT_MAP")

        val_map = current_app.config.get("_LAST_NOTIF_COUNT_VAL_MAP")

        if not isinstance(last_map, dict):

            last_map = {}

        if not isinstance(val_map, dict):

            val_map = {}



        last = last_map.get(cache_key)

        cached = val_map.get(cache_key)

        if last and isinstance(last, datetime) and cached is not None and (now_dt - last) < timedelta(seconds=60):

            return {

                "notif_active_count": int(cached),

                "support_mode": bool(support_user_id),

                "support_user_id": support_user_id,

                "support_username": support_username,

            }



        # Campana = SOLO cobranzas atrasadas (defaults del helper), a proposito:
        # es un semaforo de urgencia. La pagina /notificaciones muestra ademas
        # entregas e inactividad, asi que su total es mayor. Respeta ClientAlertState.

        notif_count = 0

        try:

            notif_count = int(len(_compute_alerts_for_all_clients(now_dt)) or 0)

        except Exception:

            notif_count = 0



        last_map[cache_key] = now_dt

        val_map[cache_key] = int(notif_count)

        current_app.config["_LAST_NOTIF_COUNT_AT_MAP"] = last_map

        current_app.config["_LAST_NOTIF_COUNT_VAL_MAP"] = val_map

        return {

            "notif_active_count": int(notif_count),

            "support_mode": bool(support_user_id),

            "support_user_id": support_user_id,

            "support_username": support_username,

        }

    except Exception:

        return {"notif_active_count": 0, "support_mode": False, "support_user_id": None, "support_username": None}





def _invalidate_notif_count_cache():

    try:

        try:

            current_app.config.pop("_LAST_NOTIF_COUNT_AT", None)

            current_app.config.pop("_LAST_NOTIF_COUNT_VAL", None)

        except Exception:

            pass

        try:

            current_app.config.pop("_LAST_NOTIF_COUNT_AT_MAP", None)

            current_app.config.pop("_LAST_NOTIF_COUNT_VAL_MAP", None)

        except Exception:

            pass

    except Exception:

        pass





def _sync_entrega_desde_cobro(coll, lg=None):

    """Fuente unica de verdad: si la cobranza esta cobrada, la entrega se da por realizada.

    Regla pedida por negocio: "cuando una cobranza se realice, dar por hecho que la

    entrega se realizo tambien" (para sacarla de Status Mercaderia y apagar la alerta).

    Decisiones:

    - Sin cobro (`fecha_cobro_efectiva is None`) no hace nada: no se asumen entregas

      de cobranzas que todavia no se cobraron.

    - Idempotente: si `fecha_entrega_efectiva` ya tiene valor NO se pisa. Un cobro no

      reescribe una fecha de entrega que logistica ya confirmo.

    - No crea el `LogisticsStatus` si no existe: sin fila de logistica no hay status

      ATRASADO que apagar (ver `LogisticsStatus.status` en models.py). El caller que

      necesite crearla lo hace antes y la pasa por parametro.

    - Fecha asumida: la entrega estimada de logistica; si no hay, la entrega efectiva

      ya registrada en la cobranza; como ultimo recurso `datetime.utcnow()`, que es la

      convencion que ya usaba este codigo (unificar husos horarios es otra fase).

    Devuelve True si completo la entrega, False si no toco nada.

    """

    if coll is None:

        return False

    if getattr(coll, "fecha_cobro_efectiva", None) is None:

        return False

    if lg is None:

        order_id = getattr(coll, "order_id", None)

        if order_id is None:

            return False

        lg = LogisticsStatus.query.filter_by(order_id=order_id).first()

    if lg is None:

        return False

    if getattr(lg, "fecha_entrega_efectiva", None) is not None:

        return False

    picked = (

        getattr(lg, "fecha_entrega_estimada", None)

        or getattr(coll, "fecha_entrega_efectiva", None)

        or datetime.utcnow()

    )

    lg.fecha_entrega_efectiva = picked

    if getattr(coll, "fecha_entrega_efectiva", None) is None:

        coll.fecha_entrega_efectiva = picked

    return True




@bp.get("/api/notificaciones/count")

def api_notificaciones_count():

    data = inject_notification_count() or {}

    try:

        return jsonify({"count": int(data.get("notif_active_count") or 0)})

    except Exception:

        return jsonify({"count": 0})





@bp.get("/")

def index():

    today_local = date.today()

    try:

        if ZoneInfo:

            today_local = datetime.now(ZoneInfo("America/Argentina/Buenos_Aires")).date()

    except Exception:

        today_local = date.today()

    month_start = today_local.replace(day=1)

    next_month = (month_start + timedelta(days=32)).replace(day=1)

    month_end = next_month - timedelta(days=1)

    raw_from = (request.args.get("from") or "").strip()

    raw_to = (request.args.get("to") or "").strip()

    selected_day = (request.args.get("day") or "").strip()

    preset = (request.args.get("preset") or "month_current").strip().lower()

    if preset not in {"", "custom", "today", "last_30", "month_current", "month_prev"}:

        preset = ""

    view_mode = (request.args.get("view") or "company").strip().lower()

    if view_mode not in {"company", "client"}:

        view_mode = "company"

    company_id = request.args.get("company_id", type=int)

    client_id = request.args.get("client_id", type=int)

    rank_mode = (request.args.get("rank") or "pending").strip().lower()

    if rank_mode not in {"pending", "orders"}:

        rank_mode = "pending"

    # Antes habia un parser propio aca que solo aceptaba "%Y-%m-%d" y

    # "%d/%m/%Y". Ahora se usa el parser compartido (superset verificado: no

    # perdio ningun formato, y suma DD-MM-AAAA, DDMMAAAA e ISO con hora).

    d_from = _parse_date_like(raw_from)

    d_to = _parse_date_like(raw_to)

    if d_from is None and d_to is None:

        d_from = month_start

        d_to = month_end

        if not preset:

            preset = "month_current"

    if d_from and d_to and d_from > d_to:

        d_from, d_to = d_to, d_from

    uid = _effective_user_id() if not _has_global_access() else None

    base_companies_q = (

        db.session.query(Company.id, Company.nombre)

        .join(Order, Order.company_id == Company.id)

        .filter(Order.deleted_at.is_(None))

        .distinct()

    )

    if uid is not None:

        base_companies_q = base_companies_q.filter(Order.owner_user_id == uid)

    companies = [

        {"id": int(cid), "name": str(cname or "-")}

        for cid, cname in base_companies_q.order_by(Company.nombre.asc()).all()

    ]

    base_clients_q = (

        db.session.query(Client.id, Client.apellido, Client.nombre)

        .join(Order, Order.client_id == Client.id)

        .filter(Order.deleted_at.is_(None))

        .distinct()

    )

    if uid is not None:

        base_clients_q = base_clients_q.filter(Order.owner_user_id == uid)

    clients = []

    for cid, capellido, cnombre in base_clients_q.order_by(Client.apellido.asc(), Client.nombre.asc()).all():

        razon_social = str(capellido or "").strip()

        fallback_nombre = str(cnombre or "").strip()

        clients.append({"id": int(cid), "name": razon_social or fallback_nombre or "-"})

    if company_id and not any(int(c["id"]) == int(company_id) for c in companies):

        company_id = None

    if client_id and not any(int(c["id"]) == int(client_id) for c in clients):

        client_id = None

    if view_mode == "company":

        client_id = None

    else:

        company_id = None

    dt_from = datetime.combine(d_from, datetime.min.time()) if d_from else None

    dt_to = datetime.combine(d_to, datetime.max.time()) if d_to else None

    order_date_expr = func.coalesce(LogisticsStatus.fecha_compra, Order.created_at)

    base_orders_q = (

        Order.query

        .outerjoin(LogisticsStatus, LogisticsStatus.order_id == Order.id)

        .filter(Order.deleted_at.is_(None))

    )

    if uid is not None:

        base_orders_q = base_orders_q.filter(Order.owner_user_id == uid)

    if view_mode == "company" and company_id:

        base_orders_q = base_orders_q.filter(Order.company_id == int(company_id))

    if view_mode == "client" and client_id:

        base_orders_q = base_orders_q.filter(Order.client_id == int(client_id))

    if dt_from is not None:

        base_orders_q = base_orders_q.filter(order_date_expr >= dt_from)

    if dt_to is not None:

        base_orders_q = base_orders_q.filter(order_date_expr <= dt_to)

    orders_period = (

        base_orders_q

        .options(

            selectinload(Order.client),

            selectinload(Order.company),

            selectinload(Order.logistics),

            selectinload(Order.collection),

        )

        .order_by(order_date_expr.desc(), Order.id.desc())

        .all()

    )

    order_days = []

    for o in orders_period:

        lg = getattr(o, "logistics", None)

        compra_dt = getattr(lg, "fecha_compra", None) or getattr(o, "created_at", None)

        try:

            compra_date = compra_dt.date() if compra_dt is not None else None

        except Exception:

            compra_date = None

        if compra_date is not None:

            order_days.append(compra_date)

    chart_days = []

    if d_from is not None or d_to is not None:

        range_from = d_from or (min(order_days) if order_days else None)

        range_to = d_to or (max(order_days) if order_days else range_from)

        if range_from is not None and range_to is not None and range_from <= range_to:

            chart_days = [

                date.fromordinal(range_from.toordinal() + i)

                for i in range((range_to - range_from).days + 1)

            ]

    else:

        chart_days = sorted(set(order_days))

    # EXCEPCION deliberada al formato DD/MM/AAAA: son las etiquetas del eje X

    # del grafico de 30 dias del dashboard. Con el anio completo no entran y el

    # grafico queda ilegible. El anio ya esta implicito en el rango elegido.

    chart_labels = [d.strftime("%d/%m") for d in chart_days]

    chart_days_iso = [d.isoformat() for d in chart_days]

    chart_counts_map = {k: 0 for k in chart_days_iso}

    detail_by_day = {k: [] for k in chart_days_iso}

    detail_all = []

    for o in orders_period:

        lg = getattr(o, "logistics", None)

        compra_dt = getattr(lg, "fecha_compra", None) or getattr(o, "created_at", None)

        compra_date = None

        try:

            compra_date = compra_dt.date() if compra_dt is not None else None

        except Exception:

            compra_date = None

        if compra_date is None:

            continue

        day_iso = compra_date.isoformat()

        if day_iso not in chart_counts_map:

            continue

        chart_counts_map[day_iso] = int(chart_counts_map.get(day_iso, 0) or 0) + 1

        client_label = "-"

        if getattr(o, "client", None) is not None:

            client_label = " ".join([

                x for x in [getattr(o.client, "apellido", None), getattr(o.client, "nombre", None)] if x

            ]) or (getattr(o.client, "apellido", None) or "-")

        row = {

            "date_iso": day_iso,

            "date_label": fmt_fecha(compra_date),

            "client": client_label,

            "company": (getattr(getattr(o, "company", None), "nombre", None) or "-"),

            "order_id": int(getattr(o, "id", 0) or 0),

            "order_code": f"PED-{int(getattr(o, 'id', 0) or 0):06d}",

            "order_url": url_for("main.pedidos", order_id=int(getattr(o, "id", 0) or 0)),

        }

        detail_by_day[day_iso].append(row)

        detail_all.append(row)

    for k in detail_by_day.keys():

        detail_by_day[k].sort(key=lambda r: (r.get("date_iso", ""), int(r.get("order_id", 0))), reverse=True)

    detail_all.sort(key=lambda r: (r.get("date_iso", ""), int(r.get("order_id", 0))), reverse=True)

    if selected_day not in detail_by_day:

        selected_day = ""

    selected_detail_rows = detail_by_day.get(selected_day, []) if selected_day else detail_all

    selected_detail_rows = selected_detail_rows[:8]

    kpi_pedidos_periodo = int(len(orders_period))

    # KPI de mercadería: alinear con módulo /status (EN_CAMINO y ATRASADO)
    status_merch_q = (

        LogisticsStatus.query

        .join(Order, LogisticsStatus.order_id == Order.id)

        .filter(Order.deleted_at.is_(None))

    )

    if uid is not None:

        status_merch_q = status_merch_q.filter(Order.owner_user_id == uid)

    if view_mode == "company" and company_id:

        status_merch_q = status_merch_q.filter(Order.company_id == int(company_id))

    if view_mode == "client" and client_id:

        status_merch_q = status_merch_q.filter(Order.client_id == int(client_id))

    now_utc = datetime.utcnow()

    kpi_en_camino = int(

        status_merch_q

        .filter(LogisticsStatus.fecha_entrega_efectiva.is_(None))

        .filter(or_(LogisticsStatus.fecha_entrega_estimada.is_(None), LogisticsStatus.fecha_entrega_estimada >= now_utc))

        .order_by(None)

        .count()

        or 0

    )

    kpi_entregas_atrasadas = int(

        status_merch_q

        .filter(LogisticsStatus.fecha_entrega_efectiva.is_(None))

        .filter(LogisticsStatus.fecha_entrega_estimada < now_utc)

        .order_by(None)

        .count()

        or 0

    )

    # Cobranzas: alinear con lógica de /deudas (estados excluyentes)
    coll_base_q = (

        Collection.query

        .join(Order, Collection.order_id == Order.id)

        .outerjoin(LogisticsStatus, LogisticsStatus.order_id == Order.id)

        .filter(Order.deleted_at.is_(None))

    )

    if uid is not None:

        coll_base_q = coll_base_q.filter(or_(Order.owner_user_id == uid, Order.owner_user_id.is_(None)))

    if view_mode == "company" and company_id:

        coll_base_q = coll_base_q.filter(Order.company_id == int(company_id))

    if view_mode == "client" and client_id:

        coll_base_q = coll_base_q.filter(Order.client_id == int(client_id))

    try:

        partial_pay_exists = _safe_filter_not_voided(

            db.session.query(CollectionPayment.id)

            .filter(CollectionPayment.order_id == Order.id)

            .filter(CollectionPayment.kind != "DRAFT")

        ).exists()

    except Exception:

        partial_pay_exists = (

            db.session.query(CollectionPayment.id)

            .filter(CollectionPayment.order_id == Order.id)

            .filter(CollectionPayment.kind != "DRAFT")

            .exists()

        )

    partial_draft_exists = (

        db.session.query(CollectionDraft.id)

        .filter(CollectionDraft.order_id == Order.id)

        .exists()

    )

    partial_exists = or_(partial_pay_exists, partial_draft_exists)

    # Estados de cobranza según la FUENTE ÚNICA DE VERDAD (backend/models.py).
    # Antes acá había un copy-paste de la condición de /deudas; los KPIs del
    # dashboard contaban distinto que el listado.
    cond_cob = condiciones_estado_cobranza(today_local)

    def _coll_by_status(status_key: str):

        q_st = coll_base_q

        if status_key == "COBRADO":

            return q_st.filter(cond_cob.cobrado)

        if status_key == "EN_CAMINO":

            return q_st.filter(cond_cob.en_camino)

        if status_key == "ATRASADO":

            return q_st.filter(cond_cob.atrasado)

        if status_key == "PARCIAL":

            # PARCIAL es ortogonal a los 4 estados (ver models.py): no se
            # deriva del vencimiento, sino de la existencia de pagos/borradores.
            return q_st.filter(and_(Collection.fecha_cobro_efectiva.is_(None), partial_exists))

        if status_key == "A_COBRAR":

            return q_st.filter(and_(cond_cob.a_cobrar, ~partial_exists))

        return q_st.filter(text("1=0"))

    status_order = ["EN_CAMINO", "A_COBRAR", "PARCIAL", "ATRASADO", "COBRADO"]

    status_counts = {

        st: int(_coll_by_status(st).order_by(None).count() or 0)

        for st in status_order

    }

    cobrado_period_q = _coll_by_status("COBRADO")

    if dt_from is not None:

        cobrado_period_q = cobrado_period_q.filter(Collection.fecha_cobro_efectiva >= dt_from)

    if dt_to is not None:

        cobrado_period_q = cobrado_period_q.filter(Collection.fecha_cobro_efectiva <= dt_to)

    cobrado_period_count = int(cobrado_period_q.order_by(None).count() or 0)

    pending_rank_rows = []

    if view_mode == "company":

        pending_rank_rows = [

            {"name": str(name or "-"), "value": int(total or 0)}

            for _cid, name, total in (

                coll_base_q

                .join(Company, Order.company_id == Company.id)

                .with_entities(

                    Company.id,

                    Company.nombre,

                    func.count(Collection.id).label("total"),

                )

                .filter(Collection.fecha_cobro_efectiva.is_(None))

                .group_by(Company.id, Company.nombre)

                .order_by(func.count(Collection.id).desc(), Company.nombre.asc())

                .limit(5)

                .all()

            )

        ]

    else:

        pending_rank_rows = [

            {

                "name": (" ".join([x for x in [str(last_name or "").strip(), str(first_name or "").strip()] if x]).strip() or "-"),

                "value": int(total or 0),

            }

            for _clid, last_name, first_name, total in (

                coll_base_q

                .join(Client, Order.client_id == Client.id)

                .with_entities(

                    Client.id,

                    Client.apellido,

                    Client.nombre,

                    func.count(Collection.id).label("total"),

                )

                .filter(Collection.fecha_cobro_efectiva.is_(None))

                .group_by(Client.id, Client.apellido, Client.nombre)

                .order_by(func.count(Collection.id).desc(), Client.apellido.asc(), Client.nombre.asc())

                .limit(5)

                .all()

            )

        ]

    orders_rank_q = (

        db.session.query(Order.id)

        .filter(Order.deleted_at.is_(None))

    )

    if uid is not None:

        orders_rank_q = orders_rank_q.filter(Order.owner_user_id == uid)

    if view_mode == "company" and company_id:

        orders_rank_q = orders_rank_q.filter(Order.company_id == int(company_id))

    if view_mode == "client" and client_id:

        orders_rank_q = orders_rank_q.filter(Order.client_id == int(client_id))

    orders_rank_rows = []

    if view_mode == "company":

        orders_rank_rows = [

            {"name": str(name or "-"), "value": int(total or 0)}

            for _cid, name, total in (

                orders_rank_q

                .join(Company, Order.company_id == Company.id)

                .with_entities(Company.id, Company.nombre, func.count(Order.id).label("total"))

                .group_by(Company.id, Company.nombre)

                .order_by(func.count(Order.id).desc(), Company.nombre.asc())

                .limit(5)

                .all()

            )

        ]

    else:

        orders_rank_rows = [

            {

                "name": (" ".join([x for x in [str(last_name or "").strip(), str(first_name or "").strip()] if x]).strip() or "-"),

                "value": int(total or 0),

            }

            for _clid, last_name, first_name, total in (

                orders_rank_q

                .join(Client, Order.client_id == Client.id)

                .with_entities(Client.id, Client.apellido, Client.nombre, func.count(Order.id).label("total"))

                .group_by(Client.id, Client.apellido, Client.nombre)

                .order_by(func.count(Order.id).desc(), Client.apellido.asc(), Client.nombre.asc())

                .limit(5)

                .all()

            )

        ]

    base_params = {}

    base_dashboard_params = {

        "view": view_mode,

    }

    if d_from:

        base_params["from"] = d_from.isoformat()

        base_dashboard_params["from"] = d_from.isoformat()

    if d_to:

        base_params["to"] = d_to.isoformat()

        base_dashboard_params["to"] = d_to.isoformat()

    if preset:

        base_dashboard_params["preset"] = preset

    if view_mode == "company" and company_id:

        base_params["company_id"] = int(company_id)

        base_dashboard_params["company_id"] = int(company_id)

    if view_mode == "client" and client_id:

        base_params["client_id"] = int(client_id)

        base_dashboard_params["client_id"] = int(client_id)

    selected_company_name = ""

    selected_client_name = ""

    if company_id:

        selected_company_name = next(

            (str(c.get("name") or "") for c in companies if int(c.get("id") or 0) == int(company_id)),

            "",

        )

    if client_id:

        selected_client_name = next(

            (str(c.get("name") or "") for c in clients if int(c.get("id") or 0) == int(client_id)),

            "",

        )

    status_base_params = {}

    if view_mode == "company" and selected_company_name:

        status_base_params["company_q"] = selected_company_name

    if view_mode == "client" and selected_client_name:

        status_base_params["client_q"] = selected_client_name

    debt_base_params = {}

    if view_mode == "company" and selected_company_name:

        debt_base_params["company_q"] = selected_company_name

    if view_mode == "client" and selected_client_name:

        debt_base_params["client_q"] = selected_client_name

    kpi_links = {

        "pedidos_periodo": url_for("main.historial", **base_params),

        "en_camino": url_for("main.status", **{"status": "EN_CAMINO", **status_base_params}),

        "cobranzas_pendientes": url_for("main.deudas_pendientes", status="A_COBRAR", **debt_base_params),

        "cobranzas_atrasadas": url_for("main.deudas_pendientes", status="ATRASADO", **debt_base_params),

        "entregas_atrasadas": url_for("main.status", **{"status": "ATRASADO", **status_base_params}),

    }

    bars = [

        {

            "key": "EN_CAMINO",

            "label": "En camino",

            "value": int(status_counts.get("EN_CAMINO", 0) or 0),

            "color": "#6b7280",

            "url": url_for("main.deudas_pendientes", status="EN_CAMINO", **debt_base_params),

        },

        {

            "key": "A_COBRAR",

            "label": "A cobrar",

            "value": int(status_counts.get("A_COBRAR", 0) or 0),

            "color": "#22c1dc",

            "url": url_for("main.deudas_pendientes", status="A_COBRAR", **debt_base_params),

        },

        {

            "key": "PARCIAL",

            "label": "Parcial",

            "value": int(status_counts.get("PARCIAL", 0) or 0),

            "color": "#f5c542",

            "url": url_for("main.deudas_pendientes", status="PARCIAL", **debt_base_params),

        },

        {

            "key": "ATRASADO",

            "label": "Atrasado",

            "value": int(status_counts.get("ATRASADO", 0) or 0),

            "color": "#ef4444",

            "url": url_for("main.deudas_pendientes", status="ATRASADO", **debt_base_params),

        },

        {

            "key": "COBRADO",

            "label": "Cobrado",

            "value": int(cobrado_period_count),

            "color": "#22a447",

            "url": url_for("main.deudas_pendientes", status="COBRADO", **base_params),

        },

    ]

    chart_counts = [int(chart_counts_map[k] or 0) for k in chart_days_iso]

    ranking_rows_source = pending_rank_rows if rank_mode == "pending" else orders_rank_rows

    ranking_rows = []

    for r in ranking_rows_source:

        row_name = str((r or {}).get("name") or "-")

        row_value_raw = (r or {}).get("value", 0)

        ranking_rows.append(

            {

                "name": row_name,

                "value": str(int(row_value_raw or 0)),

            }

        )

    if view_mode == "company":

        ranking_title = "Empresas con mayor cantidad de deudas pendientes" if rank_mode == "pending" else "Empresas con mayor cantidad de pedidos"

        ranking_entity_label = "Empresa"

        ranking_footer_label = "Ver todas las empresas"

        ranking_footer_url = url_for("main.empresas")

    else:

        ranking_title = "Clientes con mayor cantidad de deudas pendientes" if rank_mode == "pending" else "Clientes con mayor cantidad de pedidos"

        ranking_entity_label = "Cliente"

        ranking_footer_label = "Ver todos los clientes"

        ranking_footer_url = url_for("main.clientes")

    ranking_value_label = "Deudas pendientes" if rank_mode == "pending" else "Pedidos"

    ranking_links = {

        "pending": url_for("main.index", **{**base_dashboard_params, "rank": "pending"}),

        "orders": url_for("main.index", **{**base_dashboard_params, "rank": "orders"}),

    }

    clear_filters_params = {"view": view_mode, "preset": "month_current"}

    return render_template(

        "index.html",

        active="dashboard",

        dashboard={

            "filters": {

                "from": d_from.isoformat() if d_from else "",

                "to": d_to.isoformat() if d_to else "",

                "from_label": fmt_fecha(d_from),

                "to_label": fmt_fecha(d_to),

                "period_label": "Período completo" if not d_from and not d_to else "",

                "preset": preset,

                "view_mode": view_mode,

                "rank_mode": rank_mode,

                "company_id": int(company_id) if company_id else None,

                "company_options": companies,

                "client_id": int(client_id) if client_id else None,

                "client_options": clients,

            },

            "kpis": {

                "pedidos_periodo": int(kpi_pedidos_periodo),

                "en_camino": int(kpi_en_camino),

                "cobranzas_pendientes": int(status_counts.get("A_COBRAR", 0) or 0),

                "cobranzas_atrasadas": int(status_counts.get("ATRASADO", 0) or 0),

                "entregas_atrasadas": int(kpi_entregas_atrasadas),

            },

            "links": kpi_links,

            "orders_chart": {

                "labels": chart_labels,

                "days": chart_days_iso,

                "values": chart_counts,

            },

            "collections_chart": bars,

            "detail": {

                "selected_day": selected_day,

                "rows": selected_detail_rows,

                "all_rows": detail_all,

                "by_day": detail_by_day,

                "view_day_url": url_for(
                    "main.historial",
                    **{
                        "all": 1,
                        **({"from": d_from.isoformat()} if d_from else {}),
                        **({"to": d_to.isoformat()} if d_to else {}),
                        **({"company_id": int(company_id)} if company_id else {}),
                        **({"client_id": int(client_id)} if client_id else {}),
                    },
                ),

            },

            "top_ranking": {

                "mode": rank_mode,

                "title": ranking_title,

                "entity_label": ranking_entity_label,

                "value_label": ranking_value_label,

                "rows": ranking_rows,

                "switch_links": ranking_links,

                "footer_label": ranking_footer_label,

                "footer_url": ranking_footer_url,

            },

            "links_extra": {

                "all_companies": url_for("main.empresas"),

                "clear_filters": url_for("main.index", **clear_filters_params),

            },

        },

    )





@bp.get("/clientes")

def clientes():

    q = (request.args.get("q") or "").strip()

    prov = (request.args.get("prov") or "").strip()

    alertas = (request.args.get("alertas") or "").strip().upper()

    if alertas not in {"", "CON_ALERTAS"}:

        alertas = ""

    show_all = request.args.get("all") == "1"



    page = request.args.get("page", default=1, type=int) or 1

    per_page = request.args.get("per_page", default=10, type=int) or 10

    if page < 1:

        page = 1

    if per_page < 1:

        per_page = 10

    if per_page > 50:

        per_page = 50



    base = (

        Client.query

        .options(selectinload(Client.links).selectinload(ClientCompanyLink.company))

    )

    base = base.filter(Client.archived.is_(False))

    if not _has_global_access():

        uid = _effective_user_id()

        if uid is not None:

            base = base.filter(Client.owner_user_id == uid)

    if prov:

        try:

            base = base.filter(Client.provincia.ilike(f"%{prov}%"))

        except Exception:

            pass

    if q:

        ilike = f"%{q}%"

        base = base.filter(

            (Client.apellido.ilike(ilike))

            | (Client.nombre.ilike(ilike))

            | ((Client.apellido + " " + Client.nombre).ilike(ilike))

        )

    active_alerts_cache = None

    if alertas == "CON_ALERTAS":

        try:

            active_alerts_cache = _compute_alerts_for_all_clients(datetime.utcnow()) or []

            client_ids_with_alerts = {

                int(a.get("client_id") or 0)

                for a in active_alerts_cache

                if a and a.get("client_id")

            }

            client_ids_with_alerts = {cid for cid in client_ids_with_alerts if cid > 0}

            if client_ids_with_alerts:

                base = base.filter(Client.id.in_(client_ids_with_alerts))

            else:

                base = base.filter(Client.id == 0)

        except Exception:

            active_alerts_cache = []

            base = base.filter(Client.id == 0)

    base = base.order_by(Client.apellido, Client.nombre)

    has_next = False

    has_prev = False

    if show_all:

        items = base.all()

    else:

        offset = (page - 1) * per_page

        rows = base.offset(offset).limit(per_page + 1).all()

        if len(rows) > per_page:

            has_next = True

            rows = rows[:per_page]

        has_prev = page > 1

        items = rows



    companies = Company.query.order_by(Company.nombre).all()

    archived_base = Client.query.filter(Client.archived.is_(True))

    if not _has_global_access():

        uid = _effective_user_id()

        if uid is not None:

            archived_base = archived_base.filter(Client.owner_user_id == uid)

    archived_items = archived_base.order_by(Client.apellido, Client.nombre).all()



    alerts_by_client = {}

    total_active_alerts = 0

    try:

        active_alerts = active_alerts_cache

        if active_alerts is None:

            active_alerts = _compute_alerts_for_all_clients(datetime.utcnow()) or []

        total_active_alerts = int(len(active_alerts) or 0)

        order_ids = sorted({int(a.get("order_id")) for a in active_alerts if a and a.get("order_id")})

        orders = []

        if order_ids:

            oq = (

                Order.query

                .options(

                    selectinload(Order.logistics),

                    selectinload(Order.collection),

                    selectinload(Order.company),

                )

                .filter(Order.id.in_(order_ids))

            )

            if not _has_global_access():

                uid = _effective_user_id()

                if uid is not None:

                    oq = oq.filter(or_(Order.owner_user_id == uid, Order.owner_user_id.is_(None)))

            orders = oq.all()

        order_map = {int(o.id): o for o in (orders or []) if o and o.id}



        for a in active_alerts:

            if not a:

                continue

            try:

                cid = int(a.get("client_id") or 0)

            except Exception:

                cid = 0

            if not cid:

                continue

            try:

                oid = int(a.get("order_id") or 0)

            except Exception:

                oid = 0

            state_kind = (a.get("kind") or "").strip().upper()

            ui_kind = ""

            if state_kind == "COBRANZA_ATRASADA":

                ui_kind = "COBRANZA"

            elif state_kind == "ENTREGA_ATRASADA":

                ui_kind = "MERCADERIA"

            else:

                ui_kind = state_kind



            o = order_map.get(oid) if oid else None

            coll = getattr(o, "collection", None) if o else None

            lg = getattr(o, "logistics", None) if o else None

            company_name = None

            try:

                company_name = (getattr(getattr(o, "company", None), "nombre", None) if o else None) or (a.get("company") or None)

            except Exception:

                company_name = (a.get("company") or None)



            item = {

                "client_id": cid,

                "client_name": a.get("client_name"),

                "order_id": oid,

                "kind": ui_kind,

                "state_kind": state_kind,

                "message": a.get("message"),

                "severity": a.get("severity"),

                "company": company_name,

            }

            if ui_kind == "COBRANZA":

                try:

                    item["cob_entrega_efectiva"] = (coll.fecha_entrega_efectiva.date().isoformat() if coll and coll.fecha_entrega_efectiva else "")

                except Exception:

                    item["cob_entrega_efectiva"] = ""

                try:

                    item["cob_monto"] = (str(coll.monto) if coll and coll.monto is not None else "")

                except Exception:

                    item["cob_monto"] = ""

                try:

                    item["cob_forma_pago"] = ((coll.forma_pago.value if coll and coll.forma_pago else "") or "")

                except Exception:

                    item["cob_forma_pago"] = ""

                try:

                    item["cob_pago_estimado"] = (coll.fecha_pago_estimada.date().isoformat() if coll and coll.fecha_pago_estimada else "")

                except Exception:

                    item["cob_pago_estimado"] = ""

                try:

                    item["cob_cobro_efectivo"] = (coll.fecha_cobro_efectiva.date().isoformat() if coll and coll.fecha_cobro_efectiva else "")

                except Exception:

                    item["cob_cobro_efectivo"] = ""

            elif ui_kind == "MERCADERIA":

                item["st_has_logistics"] = bool(lg is not None)

                try:

                    item["st_fecha_compra"] = (lg.fecha_compra.date().isoformat() if lg and lg.fecha_compra else "")

                except Exception:

                    item["st_fecha_compra"] = ""

                try:

                    item["st_entrega_estimada"] = (lg.fecha_entrega_estimada.date().isoformat() if lg and lg.fecha_entrega_estimada else "")

                except Exception:

                    item["st_entrega_estimada"] = ""

                try:

                    item["st_entrega_efectiva"] = (lg.fecha_entrega_efectiva.date().isoformat() if lg and lg.fecha_entrega_efectiva else "")

                except Exception:

                    item["st_entrega_efectiva"] = ""

                try:

                    item["st_precio"] = (str(lg.precio) if lg and lg.precio is not None else "")

                except Exception:

                    item["st_precio"] = ""



            alerts_by_client.setdefault(cid, []).append(item)

    except Exception:

        try:

            db.session.rollback()

        except Exception:

            pass



    return render_template(

        "clientes.html",

        active="clientes",

        items=items,

        archived_items=archived_items,

        companies=companies,

        alerts_by_client=alerts_by_client,

        total_active_alerts=total_active_alerts,

        q=q,

        prov=prov,

        alertas=alertas,

        show_all=show_all,

        page=page,

        per_page=per_page,

        has_next=has_next,

        has_prev=has_prev,

    )





@bp.post("/empresas/<int:company_id>/archive")

def empresas_archive(company_id: int):

    obj = Company.query.get_or_404(company_id)

    obj.archived = True

    db.session.commit()

    return_to = (request.form.get("return_to") or "").strip()

    if return_to.startswith("/empresas"):

        return redirect(return_to)

    return redirect(url_for("main.empresas"))





@bp.post("/empresas/<int:company_id>/unarchive")

def empresas_unarchive(company_id: int):

    obj = Company.query.get_or_404(company_id)

    obj.archived = False

    db.session.commit()

    return_to = (request.form.get("return_to") or "").strip()

    if return_to.startswith("/empresas"):

        return redirect(return_to)

    return redirect(url_for("main.empresas"))





@bp.get("/api/empresas/archivadas")

def api_empresas_archivadas():

    q = Company.query.filter(Company.archived.is_(True))

    items = [

        {

            "id": c.id,

            "marca": c.marca,

            "razon_social": c.nombre,

        }

        for c in q.order_by(Company.marca, Company.nombre).all()

    ]

    return jsonify(items)





@bp.get("/api/pedidos/notas_previas")

def api_pedidos_notas_previas():

    client_id = request.args.get("client_id", type=int)

    company_id = request.args.get("company_id", type=int)

    limit = request.args.get("limit", default=12, type=int)

    if not client_id:

        abort(400)

    limit = max(1, min(int(limit or 12), 30))

    client = Client.query.get_or_404(client_id)

    _require_owner(client)

    out = []

    seen = set()

    def _append_notes(with_company: bool):

        q = (

            Order.query

            .filter(Order.client_id == client_id)

            .filter(Order.deleted_at.is_(None))

            .outerjoin(LogisticsStatus, LogisticsStatus.order_id == Order.id)

            .options(selectinload(Order.logistics))

        )

        if with_company and company_id:

            q = q.filter(Order.company_id == company_id)

        if not _has_global_access():

            uid = _effective_user_id()

            if uid is not None:

                q = q.filter(Order.owner_user_id == uid)

        try:

            q = q.order_by(func.coalesce(LogisticsStatus.fecha_compra, Order.created_at).desc(), Order.id.desc())

        except Exception:

            q = q.order_by(Order.created_at.desc(), Order.id.desc())

        for o in q.limit(200).all():

            raw_note = (getattr(o, "nota", None) or "")

            note = raw_note.strip()

            if not note:

                continue

            key = " ".join(note.lower().split())

            if key in seen:

                continue

            lg = getattr(o, "logistics", None)

            fc = getattr(lg, "fecha_compra", None) if lg is not None else None

            dt = None

            try:

                dt = fc.date().isoformat() if fc else (o.created_at.date().isoformat() if o.created_at else None)

            except Exception:

                dt = None

            out.append({

                "note": note,

                "date": dt,

            })

            seen.add(key)

            if len(out) >= limit:

                break

    _append_notes(with_company=True)

    if len(out) < limit and company_id:

        _append_notes(with_company=False)

    return jsonify(out[:limit])





@bp.post("/clientes/alertas/visto")

def clientes_alertas_visto():

    client_id = request.form.get("client_id", type=int)

    order_id = request.form.get("order_id", type=int)

    kind = (request.form.get("kind") or "").strip().upper()

    if not client_id or not order_id or not kind:

        return jsonify({"ok": False, "error": "missing_params"}), 400



    try:

        base = ClientAlertState.query.filter_by(client_id=client_id, order_id=order_id, kind=kind)

        if not _has_global_access():

            uid = _effective_user_id()

            if uid is None:

                abort(403)

            base = base.filter(ClientAlertState.owner_user_id == uid)

        st = base.first()

    except OperationalError:

        return jsonify({"ok": False, "error": "db_schema_outdated", "hint": "Ejecutar /admin/patch_client_columns"}), 500

    if not st:

        st = ClientAlertState(client_id=client_id, order_id=order_id, kind=kind)

        if not _has_global_access():

            uid = _effective_user_id()

            if uid is not None:

                st.owner_user_id = uid

        db.session.add(st)

    # Snapshot para historial

    st.message = (request.form.get("message") or st.message or "").strip() or st.message

    st.severity = (request.form.get("severity") or st.severity or "").strip() or st.severity

    st.company = (request.form.get("company") or st.company or "").strip() or st.company

    if not st.first_seen_at:

        st.first_seen_at = datetime.utcnow()

    st.dismissed_at = datetime.utcnow()

    st.snoozed_until = None

    try:

        db.session.commit()

    except OperationalError:

        db.session.rollback()

        return jsonify({"ok": False, "error": "db_schema_outdated", "hint": "Ejecutar /admin/patch_client_columns"}), 500

    try:

        _invalidate_notif_count_cache()

    except Exception:

        pass

    return jsonify({"ok": True})





@bp.post("/clientes/alertas/snooze")

def clientes_alertas_snooze():

    client_id = request.form.get("client_id", type=int)

    order_id = request.form.get("order_id", type=int)

    kind = (request.form.get("kind") or "").strip().upper()

    days = request.form.get("days", type=int)

    if not client_id or not order_id or not kind or not days:

        return jsonify({"ok": False, "error": "missing_params"}), 400

    if days not in (1, 7, 30):

        return jsonify({"ok": False, "error": "invalid_days"}), 400



    try:

        base = ClientAlertState.query.filter_by(client_id=client_id, order_id=order_id, kind=kind)

        if not _has_global_access():

            uid = _effective_user_id()

            if uid is None:

                abort(403)

            base = base.filter(ClientAlertState.owner_user_id == uid)

        st = base.first()

    except OperationalError:

        return jsonify({"ok": False, "error": "db_schema_outdated", "hint": "Ejecutar /admin/patch_client_columns"}), 500

    if not st:

        st = ClientAlertState(client_id=client_id, order_id=order_id, kind=kind)

        if not _has_global_access():

            uid = _effective_user_id()

            if uid is not None:

                st.owner_user_id = uid

        db.session.add(st)



    st.message = (request.form.get("message") or st.message or "").strip() or st.message

    st.severity = (request.form.get("severity") or st.severity or "").strip() or st.severity

    st.company = (request.form.get("company") or st.company or "").strip() or st.company

    if not st.first_seen_at:

        st.first_seen_at = datetime.utcnow()

    st.dismissed_at = None

    st.snoozed_until = datetime.utcnow() + timedelta(days=int(days))



    try:

        db.session.commit()

    except OperationalError:

        db.session.rollback()

        return jsonify({"ok": False, "error": "db_schema_outdated", "hint": "Ejecutar /admin/patch_client_columns"}), 500

    try:

        _invalidate_notif_count_cache()

    except Exception:

        pass

    return jsonify({"ok": True, "snoozed_until": st.snoozed_until.isoformat() if st.snoozed_until else None})





@bp.get("/notificaciones")

def notificaciones():

    now_dt = datetime.utcnow()

    try:

        # La PAGINA /notificaciones muestra los TRES tipos de alerta (cobranzas,
        # entregas e inactividad), separados por tabs y con el acordeon de
        # "Empresas sin compras desde:". Es la pantalla de trabajo diaria.
        #
        # La CAMPANA del navbar (inject_notification_count) sigue usando los
        # defaults, o sea SOLO cobranzas atrasadas: es un semaforo de urgencia a
        # proposito. La diferencia entre el numero de la campana y el total de
        # esta pagina es INTENCIONAL, no un bug.
        #
        # La firma y los defaults de _compute_alerts_for_all_clients quedan
        # intactos: los otros consumidores dependen de ellos.
        active_alerts = _compute_alerts_for_all_clients(
            now_dt,
            include_delivery_alerts=True,
            include_inactivity_alerts=True,
        )

    except Exception:

        try:

            db.session.rollback()

        except Exception:

            pass

        active_alerts = []

    return render_template(

        "notificaciones.html",

        active="notificaciones",

        active_alerts=active_alerts,

    )





# Calendario general (entregas y cobranzas)

@bp.get("/calendario")

def calendario():

    # "Hoy" del NEGOCIO (Argentina) por la FUENTE ÚNICA DE VERDAD, no
    # date.today() del server. Se le pasa tambien al template para que el JS
    # NO use el reloj del navegador (mismo patron que cobranzas.html).
    hoy = hoy_negocio()

    return render_template(
        "calendar.html",
        active="calendario",
        today=hoy.isoformat(),
        now_date=hoy,
    )





@bp.get("/comisiones")

def comisiones():

    st = CommissionState.query.get(1)

    last_paid_at = None

    try:

        last_paid_at = st.last_paid_at if st else None

    except Exception:

        last_paid_at = None



    try:

        commission_date_iso = date.today().isoformat()

    except Exception:

        commission_date_iso = ""



    company_id = request.args.get("company_id", type=int)



    desde = request.args.get("from")

    hasta = request.args.get("to")

    d_from = _parse_datetime_like(desde) if (desde or "").strip() else None

    d_to = _parse_datetime_like(hasta) if (hasta or "").strip() else None

    try:

        if d_to is not None:

            # incluir el día completo

            d_to = d_to + timedelta(days=1)

    except Exception:

        pass



    # Fecha efectiva del pago: se carga en due_date (inputs de pago). created_at puede ser "fecha de carga".

    try:

        from sqlalchemy import func

        pay_dt_expr = func.coalesce(CollectionPayment.due_date, CollectionPayment.created_at)

    except Exception:

        pay_dt_expr = CollectionPayment.created_at



    q = (

        CollectionPayment.query

        .join(Order, CollectionPayment.order_id == Order.id)

        .join(Client, Order.client_id == Client.id)

        .join(Company, Order.company_id == Company.id)

        .filter(CollectionPayment.kind == "PAYMENT")

        .options(selectinload(CollectionPayment.order).selectinload(Order.client), selectinload(CollectionPayment.order).selectinload(Order.company))

    )

    if not _has_global_access():

        uid = _effective_user_id()

        if uid is not None:

            q = q.filter(Order.owner_user_id == uid)

    if last_paid_at:

        # Tomar pagos DESPUÉS de la última liquidación (exclusivo)

        q = q.filter(pay_dt_expr > last_paid_at)



    if company_id:

        q = q.filter(Order.company_id == company_id)



    if d_from is not None:

        q = q.filter(pay_dt_expr >= d_from)

    if d_to is not None:

        q = q.filter(pay_dt_expr < d_to)



    rows = q.order_by(Company.nombre.asc(), Client.apellido.asc(), Client.nombre.asc(), Order.created_at.asc(), pay_dt_expr.desc()).all()



    companies = Company.query.order_by(Company.nombre).all()



    from collections import defaultdict



    grouped = defaultdict(dict)

    totals = defaultdict(float)

    month_totals = defaultdict(lambda: defaultdict(float))



    month_names_es = {

        "01": "Enero",

        "02": "Febrero",

        "03": "Marzo",

        "04": "Abril",

        "05": "Mayo",

        "06": "Junio",

        "07": "Julio",

        "08": "Agosto",

        "09": "Septiembre",

        "10": "Octubre",

        "11": "Noviembre",

        "12": "Diciembre",

    }



    complete_order_ids = set()

    order_payment_counts = {}

    order_total_amount = {}

    try:

        order_ids = []

        for p in rows:

            try:

                oid = int(getattr(getattr(p, "order", None), "id", None) or 0)

            except Exception:

                oid = 0

            if oid > 0:

                order_ids.append(oid)

                try:

                    order_payment_counts[oid] = int(order_payment_counts.get(oid, 0) or 0) + 1

                except Exception:

                    order_payment_counts[oid] = 1

        if order_ids:

            try:

                done_rows = (

                    db.session.query(Collection.order_id)

                    .filter(Collection.order_id.in_(order_ids))

                    .filter(Collection.fecha_cobro_efectiva.isnot(None))

                    .group_by(Collection.order_id)

                    .all()

                )

                for (oid,) in done_rows:

                    try:

                        complete_order_ids.add(int(oid))

                    except Exception:

                        pass

            except Exception:

                pass



            try:

                monto_rows = (

                    db.session.query(Collection.order_id, Collection.monto)

                    .filter(Collection.order_id.in_(order_ids))

                    .all()

                )

                for (oid, mt) in monto_rows:

                    try:

                        if oid is None:

                            continue

                        order_total_amount[int(oid)] = float(mt or 0)

                    except Exception:

                        pass

            except Exception:

                pass

    except Exception:

        complete_order_ids = set()



    payment_index = {}

    try:

        by_order = {}

        for p in rows:

            try:

                oid = int(getattr(getattr(p, "order", None), "id", None) or 0)

            except Exception:

                oid = 0

            if oid <= 0:

                continue

            by_order.setdefault(oid, []).append(p)



        for oid, plist in by_order.items():

            try:

                plist_sorted = sorted(

                    plist,

                    key=lambda x: (getattr(x, "due_date", None) or getattr(x, "created_at", None) or datetime.min),

                )

            except Exception:

                plist_sorted = plist

            total_parts = int(len(plist_sorted) or 0)

            for idx, p in enumerate(plist_sorted, start=1):

                try:

                    pid = int(getattr(p, "id", 0) or 0)

                    if pid > 0:

                        payment_index[pid] = {"idx": int(idx), "total": int(total_parts)}

                except Exception:

                    pass

    except Exception:

        payment_index = {}



    for p in rows:

        o = getattr(p, "order", None)

        comp = getattr(getattr(o, "company", None), "nombre", None) if o else None

        comp = comp or "-"

        cli = getattr(getattr(o, "client", None), "apellido", None) if o else None

        cli2 = getattr(getattr(o, "client", None), "nombre", None) if o else None

        client_name = " ".join([x for x in [cli, cli2] if x]) or "-"



        # Fecha a mostrar: fecha del pago realizado (due_date) y fallback a created_at

        pay_date = None

        try:

            dt_eff = getattr(p, "due_date", None) or getattr(p, "created_at", None)

            pay_date = dt_eff.date().isoformat() if dt_eff else None

        except Exception:

            pay_date = None



        amt = 0.0

        try:

            amt = float(getattr(p, "amount", 0) or 0)

        except Exception:

            amt = 0.0



        part_label = ""

        try:

            pid = int(getattr(p, "id", 0) or 0)

            info = payment_index.get(pid) if pid else None

            if info and int(info.get("total", 0) or 0) > 1:

                part_label = f"{int(info.get('idx', 0) or 0)}/{int(info.get('total', 0) or 0)}"

        except Exception:

            part_label = ""



        total_amt = 0.0

        try:

            oid_int = int(getattr(o, "id", 0) or 0) if o is not None else 0

        except Exception:

            oid_int = 0

        try:

            if oid_int > 0 and (oid_int in order_total_amount):

                total_amt = float(order_total_amount.get(oid_int) or 0)

        except Exception:

            total_amt = 0.0

        if (total_amt or 0) <= 0:

            try:

                if o is not None and getattr(o, "precio_final", None) is not None:

                    total_amt = float(o.precio_final or 0)

            except Exception:

                pass

        if (total_amt or 0) <= 0:

            try:

                lg = getattr(o, "logistics", None) if o is not None else None

                if lg is not None and getattr(lg, "precio", None) is not None:

                    total_amt = float(lg.precio or 0)

            except Exception:

                pass



        month_key = ""

        month_label = ""

        try:

            if pay_date and len(pay_date) >= 7:

                month_key = pay_date[:7]

                mm = month_key[5:7]

                yy = month_key[0:4]

                month_label = f"{month_names_es.get(mm, mm)} {yy}"

        except Exception:

            month_key = ""

            month_label = ""



        if not month_key:

            month_key = "0000-00"

            month_label = "Sin fecha"



        try:

            if month_key not in grouped[comp]:

                grouped[comp][month_key] = {"label": month_label, "rows": []}

        except Exception:

            pass



        row_obj = {

            "payment_id": getattr(p, "id", None),

            "order_id": getattr(o, "id", None),

            "client": client_name,

            "date": pay_date,

            "amount": amt,

            "method": (getattr(p, "method", None) or ""),

            "is_complete": bool(getattr(o, "id", None) and (int(getattr(o, "id", 0) or 0) in complete_order_ids)),

            "part_label": part_label,

            "order_total": float(total_amt or 0.0),

        }



        try:

            grouped[comp][month_key]["rows"].append(row_obj)

        except Exception:

            try:

                grouped[comp][month_key] = {"label": month_label, "rows": [row_obj]}

            except Exception:

                pass



        totals[comp] += abs(amt)

        try:

            month_totals[comp][month_key] += abs(amt)

        except Exception:

            pass



    try:

        last_paid_iso = last_paid_at.date().isoformat() if last_paid_at else ""

    except Exception:

        last_paid_iso = ""



    return render_template(

        "comisiones.html",

        active="comisiones",

        last_paid_iso=last_paid_iso,

        commission_date_iso=commission_date_iso,

        companies=companies,

        company_id=company_id,

        from_val=(d_from.date().isoformat() if d_from else (desde or "")),

        to_val=((d_to - timedelta(days=1)).date().isoformat() if d_to else (hasta or "")),

        grouped={k: dict(v) for k, v in grouped.items()},

        totals=dict(totals),

        month_totals={k: dict(v) for k, v in month_totals.items()},

    )





@bp.post("/comisiones/marcar")

def comisiones_marcar():

    raw = (request.form.get("paid_at") or "").strip()

    d = _parse_datetime_like(raw) if raw else None

    if not d:

        # aceptar YYYY-MM-DD o DD/MM/YYYY via _parse_date_like

        try:

            dd = _parse_date_like(raw)

            d = datetime.combine(dd, datetime.min.time()) if dd else None

        except Exception:

            d = None

    if not d:

        return redirect(url_for("main.comisiones"))



    st = CommissionState.query.get(1)

    if not st:

        st = CommissionState(id=1)

        db.session.add(st)

    st.last_paid_at = d

    db.session.commit()

    return redirect(url_for("main.comisiones"))





# ---------------------------------------------------------------------------
#  Calendario: colores por TIPO y severidad DERIVADA de la fuente única
# ---------------------------------------------------------------------------
#  Doble codificación visual (dos canales independientes):
#
#    * fondo/borde del evento  -> TIPO   (entrega / cobranza / cumpleaños)
#    * punto de color          -> SEVERIDAD
#
#  La SEVERIDAD se DERIVA del estado de la FUENTE ÚNICA DE VERDAD
#  (backend/models.py: condiciones_estado_cobranza / estado_cobranza_de) más
#  los días de atraso. NO se persiste, no hay modelo ni migración detrás.
#
#  OJO: no tiene NADA que ver con ClientAlertState. Ese modelo guarda el
#  estado de las alertas POR USUARIO (descartada / postergada) y responde
#  "¿qué está mal AHORA?". El calendario responde otra pregunta: "¿qué pasa
#  el día X?". Son dos conceptos distintos y no se cruzan acá.
# ---------------------------------------------------------------------------

CALENDARIO_COLOR_TIPO = {
    "entrega": "#0ea5a3",
    "cobranza": "#f59e0b",
    "cumpleanos": "#ec4899",
}

# Umbral entre "atrasado hace mucho" y "atrasado recién". Se elige el plazo de
# pago por defecto de la fuente única (PLAZO_PAGO_DIAS_DEFAULT = 30 días): si
# pasó un ciclo completo de plazo desde el vencimiento, la deuda ya consumió
# otra ventana entera de cobro sin cobrarse. Es el mismo número que usa la
# regla 3, así que el umbral no introduce una constante nueva de negocio.
CALENDARIO_SEVERIDAD_CRITICA_DIAS = PLAZO_PAGO_DIAS_DEFAULT


def _severidad_calendario(estado: str, dias_atraso: int = 0) -> str:
    """Estado (fuente única) + días de atraso -> severidad visual.

    critica  ATRASADO hace >= 30 días (un ciclo de plazo completo vencido)
    alta     ATRASADO hace 1..29 días
    media    A_COBRAR (ya entregado, todavía no vencido: hay que accionar)
    baja     EN_CAMINO (ni entregado ni vencido: nada que hacer hoy)
    ninguna  COBRADO (cerrado)
    """
    if estado == "COBRADO":
        return "ninguna"
    if estado == "ATRASADO":
        if (dias_atraso or 0) >= CALENDARIO_SEVERIDAD_CRITICA_DIAS:
            return "critica"
        return "alta"
    if estado == "A_COBRAR":
        return "media"
    return "baja"


def _fecha_de(value):
    """datetime | date | None -> date | None. Sin conversión de huso.

    Las columnas de fecha del proyecto son DateTime NAIVE (fechas de negocio
    guardadas a medianoche). El código anterior les hacía .astimezone(), que
    en un datetime naive Python interpreta como hora LOCAL DEL SERVER y
    convierte: eso puede correr el día. La fuente única (_as_date) y el filtro
    fecha_iso hacen .date() pelado; acá se hace lo mismo para que el
    calendario no muestre un día distinto al de /deudas.
    """
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return None


@bp.get("/api/calendario/events")
def api_calendario_events():
    # Rango opcional
    start_raw = request.args.get("start")
    end_raw = request.args.get("end")
    start = _parse_datetime_like(start_raw) if start_raw else None
    end = _parse_datetime_like(end_raw) if end_raw else None
    start_d = _fecha_de(start)
    end_d = _fecha_de(end)

    # UN SOLO "hoy": el del negocio, por la fuente única. Antes el endpoint
    # calculaba el suyo (date.today() del server) y podía discrepar con
    # /deudas cruzando la medianoche.
    hoy = hoy_negocio()

    # Estado por la MISMA expresión SQL que filtra /deudas. Se usa la
    # expresión (y no la property Python) porque el vencimiento efectivo
    # también hay que FILTRARLO en SQL por el rango del calendario, y porque
    # así el estado que pinta el calendario es bit a bit el mismo booleano que
    # evalúa el filtro de /deudas: no hay dos caminos que puedan divergir.
    cond = condiciones_estado_cobranza(hoy)
    estado_cob_expr = case(
        (Collection.id.is_(None), "SIN_COBRANZA"),
        (cond.cobrado, "COBRADO"),
        (cond.atrasado, "ATRASADO"),
        (cond.a_cobrar, "A_COBRAR"),
        else_="EN_CAMINO",
    )

    uid = None if _has_global_access() else _effective_user_id()

    events = []

    def _dias_atraso(venc):
        v = _fecha_de(venc)
        if v is None or v >= hoy:
            return 0
        return (hoy - v).days

    # -----------------------------------------------------------------
    #  Entregas estimadas (pendientes)
    # -----------------------------------------------------------------
    #  joinedload de order->client/company: antes cada evento disparaba
    #  o.client y o.company (N+1).
    q_ent = (
        db.session.query(LogisticsStatus, estado_cob_expr, cond.vencimiento)
        .join(Order, LogisticsStatus.order_id == Order.id)
        .outerjoin(Collection, Collection.order_id == Order.id)
        .options(
            joinedload(LogisticsStatus.order).joinedload(Order.client),
            joinedload(LogisticsStatus.order).joinedload(Order.company),
        )
        # Pedidos borrados (soft delete). deleted_at existe SOLO en Order.
        .filter(Order.deleted_at.is_(None))
        .filter(LogisticsStatus.fecha_entrega_efectiva.is_(None))
        .filter(LogisticsStatus.fecha_entrega_estimada.isnot(None))
    )

    if uid is not None:
        q_ent = q_ent.filter(Order.owner_user_id == uid)

    if start:
        q_ent = q_ent.filter(LogisticsStatus.fecha_entrega_estimada >= start)

    if end:
        q_ent = q_ent.filter(LogisticsStatus.fecha_entrega_estimada <= end)

    for lg, estado, venc in q_ent.all():
        o = lg.order
        d = _fecha_de(lg.fecha_entrega_estimada)
        if d is None:
            continue

        if o is not None and o.client is not None and o.company is not None:
            title = f"Entrega: {o.client.apellido} {o.client.nombre} - {o.company.nombre}"
        else:
            title = "Entrega"

        dias = _dias_atraso(venc)
        events.append({
            "id": f"E-{o.id}" if o is not None else f"E-L{lg.id}",
            "title": title,
            "start": d.isoformat(),
            "allDay": True,
            # Fondo/borde = TIPO
            "color": CALENDARIO_COLOR_TIPO["entrega"],
            "extendedProps": {
                "tipo": "entrega",
                "estado": estado,
                "severidad": _severidad_calendario(estado, dias),
                "diasAtraso": dias,
                "vencimiento": (_fecha_de(venc).isoformat() if _fecha_de(venc) else None),
            },
        })

    # -----------------------------------------------------------------
    #  Cobranzas pendientes: el evento cae en el VENCIMIENTO EFECTIVO
    # -----------------------------------------------------------------
    #  Antes leía Collection.fecha_pago_estimada DIRECTO. Ese campo está NULL
    #  en la mayoría de las filas, así que el calendario simplemente no
    #  mostraba esas deudas (el mismo bug que hacía que el filtro "Atrasado"
    #  de /deudas escondiera deudas vencidas). Ahora la fecha del evento y el
    #  filtro por rango salen de vencimiento_efectivo_expr().
    q_cob = (
        db.session.query(Collection, estado_cob_expr, cond.vencimiento)
        .join(Order, Collection.order_id == Order.id)
        .outerjoin(LogisticsStatus, LogisticsStatus.order_id == Order.id)
        .options(
            joinedload(Collection.order).joinedload(Order.client),
            joinedload(Collection.order).joinedload(Order.company),
            joinedload(Collection.order).joinedload(Order.logistics),
        )
        .filter(Order.deleted_at.is_(None))
        .filter(Collection.fecha_cobro_efectiva.is_(None))
        .filter(cond.vencimiento.isnot(None))
    )

    if uid is not None:
        q_cob = q_cob.filter(Order.owner_user_id == uid)

    if start_d:
        q_cob = q_cob.filter(cond.vencimiento >= start_d)

    if end_d:
        q_cob = q_cob.filter(cond.vencimiento <= end_d)

    for c, estado, venc in q_cob.all():
        o = c.order
        d = _fecha_de(venc)
        if d is None:
            continue

        if o is not None and o.client is not None and o.company is not None:
            title = f"Cobranza: {o.client.apellido} {o.client.nombre} - {o.company.nombre}"
        else:
            title = "Cobranza"

        dias = _dias_atraso(venc)
        events.append({
            "id": f"C-{o.id}" if o is not None else f"C-C{c.id}",
            "title": title,
            "start": d.isoformat(),
            "allDay": True,
            "color": CALENDARIO_COLOR_TIPO["cobranza"],
            "extendedProps": {
                "tipo": "cobranza",
                "estado": estado,
                "severidad": _severidad_calendario(estado, dias),
                "diasAtraso": dias,
                "vencimiento": d.isoformat(),
            },
        })

    # -----------------------------------------------------------------
    #  Cumpleaños de contactos de clientes
    # -----------------------------------------------------------------
    #  No tienen estado de cobranza: severidad "info" (canal neutro). No se
    #  les aplica deleted_at porque ese campo NO existe en Client ni en
    #  ClientBirthday: es exclusivo de Order.
    if start or end:
        year_start = (start.date().year if start else hoy.year)
        year_end = (end.date().year if end else year_start)

        bq = (
            ClientBirthday.query
            .join(Client, ClientBirthday.client_id == Client.id)
            .options(
                joinedload(ClientBirthday.client)
                .selectinload(Client.links)
                .joinedload(ClientCompanyLink.company)
            )
            .filter(ClientBirthday.fecha.isnot(None))
        )

        if uid is not None:
            bq = bq.filter(Client.owner_user_id == uid)

        for b in bq.all():
            if not b.fecha:
                continue

            # Elegir una empresa representativa: priorizar vínculos TRABAJA
            empresa = None
            client = b.client
            if client is not None:
                for l in client.links:
                    if l.company:
                        empresa = l.company
                        if getattr(l, "status", None) == RelationStatus.TRABAJA:
                            break

            if client is not None:
                emp_name = empresa.nombre if empresa and getattr(empresa, "nombre", None) else "-"
                if b.puesto:
                    title = f"Cumpleaños: {emp_name} - {b.nombre} ({b.puesto})"
                else:
                    title = f"Cumpleaños: {emp_name} - {b.nombre}"
            else:
                title = f"Cumpleaños: {b.nombre}"

            # Para cada año en el rango, crear un evento en ese año
            for y in range(year_start, year_end + 1):
                try:
                    d = date(y, b.fecha.month, b.fecha.day)
                except ValueError:
                    continue

                if start_d and d < start_d:
                    continue

                if end_d and d > end_d:
                    continue

                events.append({
                    "id": f"B-{b.id}-{y}",
                    "title": title,
                    "start": d.isoformat(),
                    "allDay": True,
                    "color": CALENDARIO_COLOR_TIPO["cumpleanos"],
                    "extendedProps": {
                        "tipo": "cumpleanos",
                        "estado": None,
                        "severidad": "info",
                        "diasAtraso": 0,
                        "vencimiento": None,
                    },
                })

    return jsonify(events)


@bp.get("/clientes/nuevo")

def clientes_new():

    q = Client.query

    if not _has_global_access():

        uid = _effective_user_id()

        if uid is not None:

            q = q.filter(Client.owner_user_id == uid)

    items = q.order_by(Client.apellido, Client.nombre).all()

    companies = Company.query.order_by(Company.nombre).all()

    branches = [r[0] for r in db.session.query(Client.sucursal).filter(Client.sucursal.isnot(None)).distinct().order_by(Client.sucursal).all()]

    return render_template("clientes_form.html", active="clientes", companies=companies, branches=branches)





@bp.post("/clientes/nuevo")

def clientes_create():

    apellido = request.form.get("apellido", "").strip()

    nombre = request.form.get("nombre", "").strip()

    cuit = (request.form.get("cuit", "") or "").strip() or None

    # sucursales múltiples

    branch_list = [b.strip() for b in request.form.getlist("branch_list") if (b or "").strip()]

    telefono = request.form.get("telefono", "").strip()

    provincia = (request.form.get("provincia") or "").strip() or None

    forma_pago_habitual = (request.form.get("forma_pago_habitual") or "").strip().upper() or None

    # Transporte (sección colapsable)

    transporte_nombre = (request.form.get("transporte_nombre") or "").strip() or None

    transporte_contacto = (request.form.get("transporte_contacto") or "").strip() or None

    # Mails múltiples

    mail_single = (request.form.get("mail", "") or "").strip()

    mail = (mail_single or None)

    relacion = (request.form.get("relacion") or "").strip() or None

    fecha_inc = request.form.get("fecha_incorporacion") or None

    company_id = request.form.get("company_id", type=int)

    # Direcciones múltiples con provincia, nota, horarios

    delivery_names = [d.strip() for d in request.form.getlist("delivery_name_list")]

    delivery_provincias = [d.strip() for d in request.form.getlist("delivery_provincia_list")]

    delivery_notas = [d.strip() for d in request.form.getlist("delivery_nota_list")]

    delivery_schedules = [d.strip() for d in request.form.getlist("delivery_schedule_list")]

    delivery_contacts = [d.strip() for d in request.form.getlist("delivery_contact_list")]

    delivery_phones = [d.strip() for d in request.form.getlist("delivery_phone_list")]

    # Personas (cumpleaños) con notas

    b_names = [v.strip() for v in request.form.getlist("birthday_name_list")]

    b_roles = [v.strip() for v in request.form.getlist("birthday_role_list")]

    b_dates = [v.strip() for v in request.form.getlist("birthday_date_list")]

    b_notas = [v.strip() for v in request.form.getlist("birthday_notas_list")]

    # Guardar compat 'sucursal' como la primera si viene lista

    compat_sucursal = (branch_list[0] if branch_list else None)

    client = Client(apellido=apellido or "-", nombre=nombre or "-", sucursal=compat_sucursal,

                    cuit=cuit,

                    transporte_recomendado=transporte_nombre, transporte_contacto=transporte_contacto,

                    provincia=provincia,

                    forma_pago_habitual=forma_pago_habitual,

                    telefono=telefono or None, mail=mail or None,

                    fecha_incorporacion=_parse_date_like(fecha_inc) if fecha_inc else None)

    uid = _effective_user_id()

    if uid is not None:

        client.owner_user_id = uid

    db.session.add(client)

    db.session.commit()



    # Sincronizar empresas confirmadas si vinieron company_ids desde el formulario

    company_ids = request.form.getlist("company_ids", type=int)

    if company_ids:

        try:

            keep_ids = set(company_ids)

            for coid in keep_ids:

                comp = Company.query.get(coid)

                if not comp:

                    continue

                link = ClientCompanyLink.query.filter_by(client_id=client.id, company_id=coid).first()

                if not link:

                    link = ClientCompanyLink(client_id=client.id, company_id=coid, status=RelationStatus.TRABAJA, comprobante_tipo="FACTURA")

                    db.session.add(link)

            db.session.commit()

        except Exception:

            db.session.rollback()

    # Crear sucursales

    if branch_list:

        for nm in branch_list:

            try:

                db.session.add(ClientBranch(client_id=client.id, nombre=nm))

            except Exception:

                pass

        db.session.commit()

    # Crear personas (cumpleaños) con notas

    if b_names or b_roles or b_dates or b_notas:

        try:

            max_len_b = max(len(b_names), len(b_roles), len(b_dates), len(b_notas))

            for idx in range(max_len_b):

                nm = (b_names[idx] if idx < len(b_names) else "").strip()

                rl = (b_roles[idx] if idx < len(b_roles) else "").strip()

                dt_raw = (b_dates[idx] if idx < len(b_dates) else "").strip()

                nt = (b_notas[idx] if idx < len(b_notas) else "").strip()

                if not (nm or rl or dt_raw or nt):

                    continue

                fecha = None

                if dt_raw:

                    try:

                        fecha = _parse_date_like(dt_raw)

                    except Exception:

                        fecha = None

                db.session.add(ClientBirthday(client_id=client.id, nombre=nm or "-", puesto=rl or None, fecha=fecha, notas=nt or None))

            db.session.commit()

        except Exception:

            db.session.rollback()

    # Crear direcciones con provincia, nota, horarios

    if delivery_names or delivery_provincias or delivery_notas or delivery_schedules or delivery_contacts or delivery_phones:

        try:

            max_len = max(len(delivery_names), len(delivery_provincias), len(delivery_notas), len(delivery_schedules), len(delivery_contacts), len(delivery_phones)) if (delivery_names or delivery_provincias or delivery_notas or delivery_schedules or delivery_contacts or delivery_phones) else 0

            for idx in range(max_len):

                nm = (delivery_names[idx] if idx < len(delivery_names) else "").strip()

                pv = (delivery_provincias[idx] if idx < len(delivery_provincias) else "").strip()

                nt = (delivery_notas[idx] if idx < len(delivery_notas) else "").strip()

                hs = (delivery_schedules[idx] if idx < len(delivery_schedules) else "").strip()

                ct = (delivery_contacts[idx] if idx < len(delivery_contacts) else "").strip()

                ph = (delivery_phones[idx] if idx < len(delivery_phones) else "").strip()

                if not (nm or pv or nt or hs or ct or ph):

                    continue

                db.session.add(ClientDeliveryPlace(client_id=client.id, nombre=nm or "-", provincia=pv or None, nota=nt or None, horario=hs or None, contacto=ct or None, telefono=ph or None))

            db.session.commit()

        except Exception:

            db.session.rollback()

    # Si se indicó relación y empresa, crear/actualizar vínculo; si no hay empresa, aplicar a vínculos existentes

    if relacion and company_id:

        try:

            st = RelationStatus(relacion)

            comp = Company.query.get(company_id)

            if comp:

                link = ClientCompanyLink.query.filter_by(client_id=client.id, company_id=comp.id).first()

                if not link:

                    link = ClientCompanyLink(client_id=client.id, company_id=comp.id, status=st, comprobante_tipo="FACTURA")

                    db.session.add(link)

                else:

                    link.status = st

                db.session.commit()

        except Exception:

            pass

    elif relacion:

        try:

            st = RelationStatus(relacion)

            for l in client.links:

                l.status = st

            db.session.commit()

        except Exception:

            pass

    return redirect(url_for("main.clientes"))





@bp.get("/admin/patch_client_columns")

def admin_patch_client_columns():

    # Ajustar sintaxis según motor (SQLite no soporta ADD COLUMN IF NOT EXISTS en todas las versiones)

    dialect = db.session.bind.dialect.name if db.session.bind is not None else ""

    if dialect == "postgresql":

        alter_link = "ALTER TABLE client_company_link ADD COLUMN IF NOT EXISTS comprobante_tipo VARCHAR(20) DEFAULT 'FACTURA'"

        alter_link_desc = "ALTER TABLE client_company_link ADD COLUMN IF NOT EXISTS descuento NUMERIC(5,2)"

        alter_client_cols = [

            "ALTER TABLE client ADD COLUMN IF NOT EXISTS cuit VARCHAR(32)",

            "ALTER TABLE client ADD COLUMN IF NOT EXISTS direccion_principal VARCHAR(255)",

            "ALTER TABLE client ADD COLUMN IF NOT EXISTS transporte_recomendado VARCHAR(120)",

            "ALTER TABLE client ADD COLUMN IF NOT EXISTS delivery_schedule VARCHAR(255)",

            "ALTER TABLE client ADD COLUMN IF NOT EXISTS delivery_contact VARCHAR(255)",

            "ALTER TABLE client ADD COLUMN IF NOT EXISTS delivery_phone VARCHAR(64)",

            "ALTER TABLE client ADD COLUMN IF NOT EXISTS provincia VARCHAR(80)",

            "ALTER TABLE client ADD COLUMN IF NOT EXISTS fecha_incorporacion DATE",

            "ALTER TABLE client ADD COLUMN IF NOT EXISTS telefono VARCHAR(50)",

            "ALTER TABLE client ADD COLUMN IF NOT EXISTS mail VARCHAR(255)",

            "ALTER TABLE client ADD COLUMN IF NOT EXISTS forma_pago_habitual VARCHAR(32)",

        ]

        alter_company_cols = [

            "ALTER TABLE company ADD COLUMN IF NOT EXISTS cuit VARCHAR(32)",

            "ALTER TABLE company ADD COLUMN IF NOT EXISTS notas TEXT",

            "ALTER TABLE company ADD COLUMN IF NOT EXISTS cuenta_bancaria_notas TEXT",

        ]

        create_company_document = """

        CREATE TABLE IF NOT EXISTS company_document (

            id SERIAL PRIMARY KEY,

            company_id INTEGER NOT NULL,

            category VARCHAR(32) NOT NULL,

            filename VARCHAR(255) NOT NULL,

            filepath VARCHAR(500) NOT NULL,

            data BYTEA,

            mimetype VARCHAR(120),

            size INTEGER,

            uploaded_at TIMESTAMP

        )

        """



        create_client_document = """

        CREATE TABLE IF NOT EXISTS client_document (

            id SERIAL PRIMARY KEY,

            client_id INTEGER NOT NULL,

            category VARCHAR(32),

            filename VARCHAR(255) NOT NULL,

            filepath VARCHAR(500) NOT NULL,

            data BYTEA,

            mimetype VARCHAR(120),

            size INTEGER,

            uploaded_at TIMESTAMP

        )

        """



        alter_client_document_cols = [

            "ALTER TABLE client_document ADD COLUMN IF NOT EXISTS category VARCHAR(32)",

        ]

        alter_delivery_place_cols = [

            "ALTER TABLE client_delivery_place ADD COLUMN IF NOT EXISTS horario VARCHAR(255)",

            "ALTER TABLE client_delivery_place ADD COLUMN IF NOT EXISTS contacto VARCHAR(255)",

            "ALTER TABLE client_delivery_place ADD COLUMN IF NOT EXISTS telefono VARCHAR(64)",

            "ALTER TABLE client_delivery_place ADD COLUMN IF NOT EXISTS provincia VARCHAR(80)",

            "ALTER TABLE client_delivery_place ADD COLUMN IF NOT EXISTS nota VARCHAR(255)",

        ]

        alter_birthday_cols = [

            "ALTER TABLE client_birthday ADD COLUMN IF NOT EXISTS notas TEXT",

        ]

        alter_client_transport_cols = [

            "ALTER TABLE client ADD COLUMN IF NOT EXISTS transporte_contacto VARCHAR(255)",

        ]

        create_client_alert_state = """

        CREATE TABLE IF NOT EXISTS client_alert_state (

            id SERIAL PRIMARY KEY,

            client_id INTEGER NOT NULL,

            order_id INTEGER NOT NULL,

            kind VARCHAR(32) NOT NULL,

            dismissed_at TIMESTAMP,

            snoozed_until TIMESTAMP,

            message TEXT,

            severity VARCHAR(16),

            company VARCHAR(200),

            first_seen_at TIMESTAMP,

            updated_at TIMESTAMP,

            CONSTRAINT uq_client_alert_state UNIQUE (client_id, order_id, kind)

        )

        """

        alter_client_alert_cols = [

            "ALTER TABLE client_alert_state ADD COLUMN IF NOT EXISTS message TEXT",

            "ALTER TABLE client_alert_state ADD COLUMN IF NOT EXISTS severity VARCHAR(16)",

            "ALTER TABLE client_alert_state ADD COLUMN IF NOT EXISTS company VARCHAR(200)",

            "ALTER TABLE client_alert_state ADD COLUMN IF NOT EXISTS first_seen_at TIMESTAMP",

            "ALTER TABLE client_alert_state ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP",

        ]

    else:

        # Sintaxis compatible con SQLite: si ya existe fallará pero lo ignoramos con try/except

        alter_link = "ALTER TABLE client_company_link ADD COLUMN comprobante_tipo VARCHAR(20) DEFAULT 'FACTURA'"

        alter_link_desc = "ALTER TABLE client_company_link ADD COLUMN descuento REAL"

        alter_client_cols = [

            "ALTER TABLE client ADD COLUMN cuit VARCHAR(32)",

            "ALTER TABLE client ADD COLUMN direccion_principal VARCHAR(255)",

            "ALTER TABLE client ADD COLUMN transporte_recomendado VARCHAR(120)",

            "ALTER TABLE client ADD COLUMN delivery_schedule VARCHAR(255)",

            "ALTER TABLE client ADD COLUMN delivery_contact VARCHAR(255)",

            "ALTER TABLE client ADD COLUMN delivery_phone VARCHAR(64)",

            "ALTER TABLE client ADD COLUMN provincia VARCHAR(80)",

            "ALTER TABLE client ADD COLUMN fecha_incorporacion DATE",

            "ALTER TABLE client ADD COLUMN telefono VARCHAR(50)",

            "ALTER TABLE client ADD COLUMN mail VARCHAR(255)",

            "ALTER TABLE client ADD COLUMN forma_pago_habitual VARCHAR(32)",

        ]

        alter_company_cols = [

            "ALTER TABLE company ADD COLUMN cuit VARCHAR(32)",

            "ALTER TABLE company ADD COLUMN notas TEXT",

            "ALTER TABLE company ADD COLUMN cuenta_bancaria_notas TEXT",

        ]

        create_company_document = """

        CREATE TABLE IF NOT EXISTS company_document (

            id INTEGER PRIMARY KEY AUTOINCREMENT,

            company_id INTEGER NOT NULL,

            category VARCHAR(32) NOT NULL,

            filename VARCHAR(255) NOT NULL,

            filepath VARCHAR(500) NOT NULL,

            data BLOB,

            mimetype VARCHAR(120),

            size INTEGER,

            uploaded_at DATETIME

        )

        """



        create_client_document = """

        CREATE TABLE IF NOT EXISTS client_document (

            id INTEGER PRIMARY KEY AUTOINCREMENT,

            client_id INTEGER NOT NULL,

            category VARCHAR(32),

            filename VARCHAR(255) NOT NULL,

            filepath VARCHAR(500) NOT NULL,

            data BLOB,

            mimetype VARCHAR(120),

            size INTEGER,

            uploaded_at DATETIME

        )

        """



        alter_client_document_cols = [

            "ALTER TABLE client_document ADD COLUMN category VARCHAR(32)",

        ]



        alter_delivery_place_cols = [

            "ALTER TABLE client_delivery_place ADD COLUMN horario VARCHAR(255)",

            "ALTER TABLE client_delivery_place ADD COLUMN contacto VARCHAR(255)",

            "ALTER TABLE client_delivery_place ADD COLUMN telefono VARCHAR(64)",

            "ALTER TABLE client_delivery_place ADD COLUMN provincia VARCHAR(80)",

            "ALTER TABLE client_delivery_place ADD COLUMN nota VARCHAR(255)",

        ]

        alter_birthday_cols = [

            "ALTER TABLE client_birthday ADD COLUMN notas TEXT",

        ]

        alter_client_transport_cols = [

            "ALTER TABLE client ADD COLUMN transporte_contacto VARCHAR(255)",

        ]



        create_client_alert_state = """

        CREATE TABLE IF NOT EXISTS client_alert_state (

            id INTEGER PRIMARY KEY AUTOINCREMENT,

            client_id INTEGER NOT NULL,

            order_id INTEGER NOT NULL,

            kind VARCHAR(32) NOT NULL,

            dismissed_at DATETIME,

            snoozed_until DATETIME,

            message TEXT,

            severity VARCHAR(16),

            company VARCHAR(200),

            first_seen_at DATETIME,

            updated_at DATETIME,

            CONSTRAINT uq_client_alert_state UNIQUE (client_id, order_id, kind)

        )

        """



        alter_client_alert_cols = [

            "ALTER TABLE client_alert_state ADD COLUMN message TEXT",

            "ALTER TABLE client_alert_state ADD COLUMN severity VARCHAR(16)",

            "ALTER TABLE client_alert_state ADD COLUMN company VARCHAR(200)",

            "ALTER TABLE client_alert_state ADD COLUMN first_seen_at DATETIME",

            "ALTER TABLE client_alert_state ADD COLUMN updated_at DATETIME",

        ]



    stmts = [

        # Tablas relacionadas nuevas (crear si no existen)

        """

        CREATE TABLE IF NOT EXISTS client_delivery_place (

            id SERIAL PRIMARY KEY,

            client_id INTEGER NOT NULL,

            nombre VARCHAR(255) NOT NULL,

            CONSTRAINT uq_client_delivery_name UNIQUE (client_id, nombre)

        )

        """,

        # Nueva columna en vínculos empresa-cliente: tipo de comprobante (FACTURA/REMITO)

        alter_link,

        # Nueva columna en vínculos empresa-cliente: descuento (%)

        alter_link_desc,

        """

        CREATE TABLE IF NOT EXISTS client_birthday (

            id SERIAL PRIMARY KEY,

            client_id INTEGER NOT NULL,

            nombre VARCHAR(255) NOT NULL,

            puesto VARCHAR(255),

            fecha DATE,

            CONSTRAINT uq_client_birthday UNIQUE (client_id, nombre, puesto)

        )

        """,

        # Tabla de documentos de cliente (varía según motor)

        create_client_document,

        # Tabla de documentos de empresa (varía según motor)

        create_company_document,

        # Alertas persistidas (centro de notificaciones)

        create_client_alert_state,

        # Columnas nuevas para historial (si la tabla ya existía)

        *alter_client_alert_cols,

        # Columnas nuevas en client_delivery_place, client y company

        *alter_delivery_place_cols,

        *alter_client_cols,

        *alter_company_cols,

        # Columnas nuevas para personas (cumpleaños) y transporte

        *alter_birthday_cols,

        *alter_client_transport_cols,

        # Columnas nuevas en client_document

        *alter_client_document_cols,

    ]

    applied = []

    errors = []

    for sql in stmts:

        try:

            db.session.execute(text(sql))

            db.session.commit()

            applied.append(sql)

        except Exception as e:

            db.session.rollback()

            # Guardamos el error para diagnóstico (no rompe el resto)

            try:

                errors.append({"sql": sql, "error": str(e)})

            except Exception:

                pass

            continue

    return {"status": "ok", "applied": applied, "errors": errors}





@bp.get("/admin/client_columns")

def admin_client_columns():

    rows = db.session.execute(text("""

        SELECT column_name, data_type

        FROM information_schema.columns

        WHERE table_name = 'client'

        ORDER BY ordinal_position

    """)).mappings().all()

    return {"columns": [dict(r) for r in rows]}





# Documentos de clientes

@bp.post("/clientes/<int:client_id>/docs/upload")

def clientes_docs_upload(client_id: int):

    client = Client.query.get_or_404(client_id)

    _require_owner(client)

    files = request.files.getlist("documents")

    if not files:

        return redirect(url_for("main.clientes"))

    cloud_name = os.getenv("CLOUDINARY_CLOUD_NAME")

    upload_preset = os.getenv("CLOUDINARY_UPLOAD_PRESET")

    for f in files:

        try:

            # Ignorar inputs vacíos

            if not f or not getattr(f, 'filename', None):

                continue

            # Leer contenido una vez para DB y/o Cloudinary

            content = f.read() or b""

            fname = f.filename or "documento"

            mtype = f.mimetype or "application/octet-stream"

            size = len(content)

            if size <= 0:

                continue

            url = ""

            if content and cloud_name and upload_preset:

                try:

                    r = requests.post(

                        f"https://api.cloudinary.com/v1_1/{cloud_name}/auto/upload",

                        data={"upload_preset": upload_preset},

                        files={"file": (fname, content, mtype)},

                        timeout=30,

                    )

                    if r.ok:

                        j = r.json()

                        url = j.get("secure_url") or j.get("url") or ""

                except Exception:

                    url = ""

            category = (request.form.get("category") or "CONSTANCIA").upper()

            uid = _effective_user_id()

            doc = ClientDocument(client_id=client.id, category=category, filename=fname, filepath=url or "", data=content, mimetype=mtype, size=size)

            if uid is not None:

                doc.owner_user_id = uid

            db.session.add(doc)

        except Exception:

            continue

    db.session.commit()

    # Respuesta AJAX: devolver documentos actualizados

    wants_json = request.headers.get("X-Requested-With") == "XMLHttpRequest" or request.accept_mimetypes.best == "application/json"

    if wants_json:

        docs = (

            ClientDocument.query.filter_by(client_id=client.id)

            .order_by(ClientDocument.uploaded_at.desc())

            .all()

        )

        return jsonify([

            {

                "id": d.id,

                "category": getattr(d, "category", None),

                "filename": d.filename,

                "download_url": url_for("main.clientes_docs_download", client_id=client.id, doc_id=d.id),

                "delete_url": url_for("main.clientes_docs_delete", client_id=client.id, doc_id=d.id),

                "uploaded_at": (d.uploaded_at.isoformat() if d.uploaded_at else None),

            }

            for d in docs

        ])

    return redirect(url_for("main.clientes"))





@bp.post("/clientes/<int:client_id>/docs/<int:doc_id>/delete")

def clientes_docs_delete(client_id: int, doc_id: int):

    doc = ClientDocument.query.filter_by(id=doc_id, client_id=client_id).first_or_404()

    if not _has_global_access():

        uid = _effective_user_id()

        if uid is None or getattr(doc, "owner_user_id", None) != uid:

            abort(403)

    db.session.delete(doc)

    db.session.commit()

    wants_json = request.headers.get("X-Requested-With") == "XMLHttpRequest" or request.accept_mimetypes.best == "application/json"

    if wants_json:

        docs = (

            ClientDocument.query.filter_by(client_id=client_id)

            .order_by(ClientDocument.uploaded_at.desc())

            .all()

        )

        return jsonify([

            {

                "id": d.id,

                "category": getattr(d, "category", None),

                "filename": d.filename,

                "download_url": url_for("main.clientes_docs_download", client_id=client_id, doc_id=d.id),

                "delete_url": url_for("main.clientes_docs_delete", client_id=client_id, doc_id=d.id),

                "uploaded_at": (d.uploaded_at.isoformat() if d.uploaded_at else None),

            }

            for d in docs

        ])

    return redirect(url_for("main.clientes_edit_view", client_id=client_id))





@bp.get("/clientes/<int:client_id>/docs/<int:doc_id>/download")

def clientes_docs_download(client_id: int, doc_id: int):

    doc = ClientDocument.query.filter_by(id=doc_id, client_id=client_id).first_or_404()

    if not _has_global_access():

        uid = _effective_user_id()

        if uid is None or getattr(doc, "owner_user_id", None) != uid:

            abort(403)

    # Prioridad: si hay datos en DB, servirlos

    try:

        if getattr(doc, "data", None):

            return send_file(

                BytesIO(doc.data),

                mimetype=doc.mimetype or "application/octet-stream",

                as_attachment=False,

                download_name=doc.filename or "documento"

            )

    except Exception:

        pass

    if doc.filepath:

        return redirect(doc.filepath)

    abort(404)





@bp.post("/empresas/<int:company_id>/productos/upload")

def empresas_productos_upload(company_id: int):

    company = Company.query.get_or_404(company_id)

    existing_sheet = (

        CompanyProductSheet.query

        .filter_by(company_id=company.id)

        .order_by(CompanyProductSheet.uploaded_at.desc())

        .first()

    )

    if existing_sheet is not None:

        return redirect(

            url_for("main.empresas_edit_view", company_id=company.id)

            + "?sheet_error=replace_required#empresaProductosCollapse"

        )

    f = request.files.get("sheet")

    if not f or not getattr(f, "filename", None):

        return redirect(url_for("main.empresas_edit_view", company_id=company.id))



    filename = f.filename or "planilla"

    ok, msg = validate_filename(filename)

    if not ok:

        return redirect(url_for("main.empresas_edit_view", company_id=company.id))



    content = f.read() or b""

    if not content:

        return redirect(url_for("main.empresas_edit_view", company_id=company.id))



    mtype = f.mimetype or "application/octet-stream"

    size = len(content)



    cloud_name = os.getenv("CLOUDINARY_CLOUD_NAME")

    upload_preset = os.getenv("CLOUDINARY_UPLOAD_PRESET")

    url = ""

    if cloud_name and upload_preset:

        try:

            r = requests.post(

                f"https://api.cloudinary.com/v1_1/{cloud_name}/auto/upload",

                data={"upload_preset": upload_preset},

                files={"file": (filename, content, mtype)},

                timeout=30,

            )

            if r.ok:

                j = r.json()

                url = j.get("secure_url") or j.get("url") or ""

        except Exception:

            url = ""



    row = CompanyProductSheet(

        company_id=company.id,

        filename=filename,

        filepath=(url or None),

        data=content,

        mimetype=mtype,

        size=size,

    )

    did_commit = False

    for _ in range(2):

        try:

            db.session.add(row)

            db.session.commit()

            did_commit = True

            break

        except Exception:

            try:

                db.session.rollback()

            except Exception:

                pass

            # Si la tabla no existe aún (local/railway sin migración), intentar crear.

            try:

                db.create_all()

            except Exception:

                pass

    if not did_commit:

        abort(500)



    # Mantenerte en el legajo (no sacar a otra pantalla)

    return redirect(url_for("main.empresas_edit_view", company_id=company.id) + "#empresaProductosCollapse")





@bp.get("/empresas/<int:company_id>/productos")

def empresas_productos_view(company_id: int):

    company = Company.query.get_or_404(company_id)

    sheet = (

        CompanyProductSheet.query

        .filter_by(company_id=company.id)

        .order_by(CompanyProductSheet.uploaded_at.desc())

        .first()

    )

    q = (request.args.get("q") or "").strip()

    page = request.args.get("page", default=1, type=int) or 1

    per_page = request.args.get("per_page", default=50, type=int) or 50

    if per_page not in (10, 20, 50, 100, 200):

        per_page = 50



    columns = []

    rows = []

    total_rows = 0

    error = ""

    filename = ""



    try:

        if sheet is not None:

            filename = sheet.filename or ""

            content = None

            if getattr(sheet, "data", None):

                content = bytes(sheet.data)

            elif getattr(sheet, "filepath", None):

                try:

                    rr = requests.get(sheet.filepath, timeout=30)

                    if rr.ok:

                        content = rr.content

                except Exception:

                    content = None

            if content:

                df = read_dataframe_from_bytes(filename, content)

                parsed = parse_dynamic_table(df, q=q, page=page, per_page=per_page)

                columns = parsed.columns

                rows = parsed.rows

                total_rows = parsed.total_rows

    except Exception:

        error = "No se pudo procesar la planilla"



    has_prev = page > 1

    has_next = (page * per_page) < int(total_rows or 0)



    base_args = {}

    try:

        if q:

            base_args["q"] = q

    except Exception:

        pass

    try:

        base_args["per_page"] = str(per_page)

    except Exception:

        base_args["per_page"] = "50"

    prev_url = None

    next_url = None

    try:

        if has_prev:

            prev_url = url_for("main.empresas_productos_view", company_id=company.id) + "?" + urlencode({**base_args, "page": str(page - 1)})

        if has_next:

            next_url = url_for("main.empresas_productos_view", company_id=company.id) + "?" + urlencode({**base_args, "page": str(page + 1)})

    except Exception:

        prev_url = None

        next_url = None



    return render_template(

        "empresa_productos.html",

        active="empresas",

        company=company,

        sheet=sheet,

        columns=columns,

        rows=rows,

        total_rows=total_rows,

        q=q,

        page=page,

        per_page=per_page,

        has_prev=has_prev,

        has_next=has_next,

        prev_url=prev_url,

        next_url=next_url,

        error=error,

        filename=filename,

    )





@bp.get("/empresas/<int:company_id>/productos/download")

def empresas_productos_download(company_id: int):

    sheet = (

        CompanyProductSheet.query

        .filter_by(company_id=company_id)

        .order_by(CompanyProductSheet.uploaded_at.desc())

        .first_or_404()

    )

    try:

        if getattr(sheet, "data", None):

            return send_file(

                BytesIO(sheet.data),

                mimetype=sheet.mimetype or "application/octet-stream",

                as_attachment=True,

                download_name=sheet.filename or "planilla_productos",

            )

    except Exception:

        pass

    if getattr(sheet, "filepath", None):

        return redirect(sheet.filepath)

    abort(404)





@bp.post("/empresas/<int:company_id>/productos/delete")

def empresas_productos_delete(company_id: int):

    company = Company.query.get_or_404(company_id)

    sheets = CompanyProductSheet.query.filter_by(company_id=company.id).all()

    try:

        for sheet in sheets:

            fp = (getattr(sheet, "filepath", None) or "").strip()

            if fp and not fp.lower().startswith(("http://", "https://")):

                try:

                    candidate = fp

                    if not os.path.isabs(candidate):

                        candidate = os.path.join(current_app.root_path, candidate.lstrip("/\\"))

                    if os.path.isfile(candidate):

                        os.remove(candidate)

                except Exception:

                    pass

            db.session.delete(sheet)

        db.session.commit()

    except Exception:

        try:

            db.session.rollback()

        except Exception:

            pass

        return redirect(

            url_for("main.empresas_edit_view", company_id=company.id)

            + "?sheet_error=delete_failed#empresaProductosCollapse"

        )

    return redirect(

        url_for("main.empresas_edit_view", company_id=company.id)

        + "?sheet_deleted=1#empresaProductosCollapse"

    )





# Gestión de vínculos desde Clientes (bilateral con Empresas)

@bp.post("/clientes/<int:client_id>/links/add")

def clientes_link_add(client_id: int):

    client = Client.query.get_or_404(client_id)

    _require_owner(client)

    company_id = request.form.get("company_id", type=int)

    status = request.form.get("status") or RelationStatus.TRABAJA.value

    comprobante_tipo = (request.form.get("comprobante_tipo") or "").upper() or None

    comp = Company.query.get_or_404(company_id)

    link = ClientCompanyLink.query.filter_by(client_id=client.id, company_id=comp.id).first()

    if not link:

        link = ClientCompanyLink(client_id=client.id, company_id=comp.id)

        db.session.add(link)

    link.status = RelationStatus(status)

    if comprobante_tipo in ("FACTURA", "REMITO"):

        link.comprobante_tipo = comprobante_tipo

    db.session.commit()

    return redirect(url_for("main.clientes", open_links=client.id))





@bp.post("/clientes/<int:client_id>/links/<int:link_id>/status")

def clientes_link_update(client_id: int, link_id: int):

    client = Client.query.get_or_404(client_id)

    _require_owner(client)

    link = ClientCompanyLink.query.filter_by(id=link_id, client_id=client_id).first_or_404()

    status = request.form.get("status") or None

    if status:

        link.status = RelationStatus(status)

        db.session.commit()

    return redirect(url_for("main.clientes", open_links=client_id))





@bp.post("/clientes/<int:client_id>/links/<int:link_id>/delete")

def clientes_link_delete(client_id: int, link_id: int):

    client = Client.query.get_or_404(client_id)

    _require_owner(client)

    link = ClientCompanyLink.query.filter_by(id=link_id, client_id=client_id).first_or_404()

    db.session.delete(link)

    db.session.commit()

    return redirect(url_for("main.clientes", open_links=client_id))





@bp.get("/clientes/<int:client_id>/editar")

def clientes_edit_view(client_id: int):

    client = Client.query.get_or_404(client_id)

    _require_owner(client)

    companies = Company.query.order_by(Company.nombre).all()

    branches = [r[0] for r in db.session.query(Client.sucursal).filter(Client.sucursal.isnot(None)).distinct().order_by(Client.sucursal).all()]

    return render_template("clientes_form.html", active="clientes", client=client, companies=companies, branches=branches)





@bp.post("/clientes/<int:client_id>/editar")

def clientes_update(client_id: int):

    obj = Client.query.get_or_404(client_id)

    _require_owner(obj)

    obj.apellido = request.form.get("apellido", obj.apellido)

    obj.nombre = request.form.get("nombre", obj.nombre)

    obj.cuit = (request.form.get("cuit") or None)

    # sucursales múltiples

    branch_list = [b.strip() for b in request.form.getlist("branch_list") if (b or "").strip()]

    # compat: actualizar sucursal como primera de la lista

    obj.sucursal = (branch_list[0] if branch_list else None)

    # sincronizar tabla ClientBranch con la selección actual

    try:

        ClientBranch.query.filter_by(client_id=obj.id).delete()

        for nm in branch_list:

            if nm:

                db.session.add(ClientBranch(client_id=obj.id, nombre=nm))

    except Exception:

        pass

    obj.telefono = request.form.get("telefono") or None

    obj.provincia = (request.form.get("provincia") or None)

    obj.forma_pago_habitual = ((request.form.get("forma_pago_habitual") or "").strip().upper() or None)

    # Transporte (sección colapsable)

    obj.transporte_recomendado = (request.form.get("transporte_nombre") or None)

    obj.transporte_contacto = (request.form.get("transporte_contacto") or None)

    # Mails múltiples

    mail_single = (request.form.get("mail") or "").strip()

    obj.mail = (mail_single or None)

    # Direcciones múltiples con provincia, nota, horarios

    delivery_names = [d.strip() for d in request.form.getlist("delivery_name_list")]

    delivery_provincias = [d.strip() for d in request.form.getlist("delivery_provincia_list")]

    delivery_notas = [d.strip() for d in request.form.getlist("delivery_nota_list")]

    delivery_schedules = [d.strip() for d in request.form.getlist("delivery_schedule_list")]

    delivery_contacts = [d.strip() for d in request.form.getlist("delivery_contact_list")]

    delivery_phones = [d.strip() for d in request.form.getlist("delivery_phone_list")]

    # Personas (cumpleaños) con notas

    b_names = [v.strip() for v in request.form.getlist("birthday_name_list")]

    b_roles = [v.strip() for v in request.form.getlist("birthday_role_list")]

    b_dates = [v.strip() for v in request.form.getlist("birthday_date_list")]

    b_notas = [v.strip() for v in request.form.getlist("birthday_notas_list")]

    relacion = (request.form.get("relacion") or "").strip() or None

    fecha_inc = request.form.get("fecha_incorporacion") or None

    obj.fecha_incorporacion = _parse_date_like(fecha_inc) if fecha_inc else obj.fecha_incorporacion

    # Sincronizar direcciones (lugares de entrega) con provincia, nota, horarios

    try:

        ClientDeliveryPlace.query.filter_by(client_id=obj.id).delete()

        max_len = max(len(delivery_names), len(delivery_provincias), len(delivery_notas), len(delivery_schedules), len(delivery_contacts), len(delivery_phones)) if (delivery_names or delivery_provincias or delivery_notas or delivery_schedules or delivery_contacts or delivery_phones) else 0

        for idx in range(max_len):

            nm = (delivery_names[idx] if idx < len(delivery_names) else "").strip()

            pv = (delivery_provincias[idx] if idx < len(delivery_provincias) else "").strip()

            nt = (delivery_notas[idx] if idx < len(delivery_notas) else "").strip()

            hs = (delivery_schedules[idx] if idx < len(delivery_schedules) else "").strip()

            ct = (delivery_contacts[idx] if idx < len(delivery_contacts) else "").strip()

            ph = (delivery_phones[idx] if idx < len(delivery_phones) else "").strip()

            if not (nm or pv or nt or hs or ct or ph):

                continue

            db.session.add(ClientDeliveryPlace(client_id=obj.id, nombre=nm or "-", provincia=pv or None, nota=nt or None, horario=hs or None, contacto=ct or None, telefono=ph or None))

    except Exception:

        db.session.rollback()

    # Sincronizar personas (cumpleaños) con notas

    try:

        ClientBirthday.query.filter_by(client_id=obj.id).delete()

        max_len_b = max(len(b_names), len(b_roles), len(b_dates), len(b_notas)) if (b_names or b_roles or b_dates or b_notas) else 0

        for idx in range(max_len_b):

            nm = (b_names[idx] if idx < len(b_names) else "").strip()

            rl = (b_roles[idx] if idx < len(b_roles) else "").strip()

            dt_raw = (b_dates[idx] if idx < len(b_dates) else "").strip()

            nt = (b_notas[idx] if idx < len(b_notas) else "").strip()

            if not (nm or rl or dt_raw or nt):

                continue

            fecha = None

            if dt_raw:

                try:

                    fecha = _parse_date_like(dt_raw)

                except Exception:

                    fecha = None

            db.session.add(ClientBirthday(client_id=obj.id, nombre=nm or "-", puesto=rl or None, fecha=fecha, notas=nt or None))

    except Exception:

        pass

    db.session.commit()

    # Aplicar estado: si vino empresa, crear/actualizar vínculo; si no, aplicar a todos

    company_id = request.form.get("company_id", type=int)

    if relacion and company_id:

        try:

            st = RelationStatus(relacion)

            comp = Company.query.get(company_id)

            if comp:

                link = ClientCompanyLink.query.filter_by(client_id=obj.id, company_id=comp.id).first()

                if not link:

                    link = ClientCompanyLink(client_id=obj.id, company_id=comp.id, status=st)

                    db.session.add(link)

                else:

                    link.status = st

                db.session.commit()

        except Exception:

            pass

    elif relacion:

        try:

            st = RelationStatus(relacion)

            for l in obj.links:

                l.status = st

            db.session.commit()

        except Exception:

            pass



    # Sincronizar empresas confirmadas si vinieron company_ids desde el formulario

    company_ids = request.form.getlist("company_ids", type=int)

    if company_ids:

        try:

            keep_ids = set(company_ids)

            existing = {l.company_id: l for l in obj.links}

            for l in list(obj.links):

                if l.company_id not in keep_ids:

                    db.session.delete(l)

            for coid in keep_ids:

                if coid in existing:

                    continue

                comp = Company.query.get(coid)

                if not comp:

                    continue

                db.session.add(ClientCompanyLink(client_id=obj.id, company_id=coid, status=RelationStatus.TRABAJA, comprobante_tipo="FACTURA"))

            db.session.commit()

        except Exception:

            db.session.rollback()

    return redirect(url_for("main.clientes"))





@bp.post("/clientes/<int:client_id>/share_to_company")

def clientes_share_to_company(client_id: int):

    client = Client.query.get_or_404(client_id)

    _require_owner(client)

    data = request.get_json(silent=True) or {}

    company_id_raw = data.get("company_id")

    if company_id_raw is None:

        company_id_raw = request.form.get("company_id")

    try:

        company_id = int(company_id_raw) if company_id_raw else None

    except Exception:

        company_id = None

    if not company_id:

        return jsonify({"ok": False, "error": "company_id requerido"}), 400

    company = Company.query.get_or_404(company_id)

    to_all = [m.strip() for m in re.split(r"[;,]", (company.mail_pedido or "")) if m and m.strip()]

    if not to_all:

        return jsonify({"ok": False, "error": "La empresa seleccionada no tiene mails de pedido cargados"}), 400

    comment = (data.get("comment") or request.form.get("comment") or "").strip()

    def _as_bool(v):

        if isinstance(v, bool):

            return v

        if v is None:

            return False

        return str(v).strip().lower() in {"1", "true", "yes", "si", "on"}

    preview_mode = _as_bool(data.get("preview") if "preview" in data else request.form.get("preview"))

    confirm_send = _as_bool(data.get("confirm_send") if "confirm_send" in data else request.form.get("confirm_send"))

    def _safe(v):

        try:

            s = (v or "").strip()

            return s or "-"

        except Exception:

            return "-"

    def _normalize_note(v):

        txt = (v or "").strip().lower()

        if not txt:

            return ""

        try:

            txt = "".join(ch for ch in unicodedata.normalize("NFKD", txt) if not unicodedata.combining(ch))

        except Exception:

            pass

        return txt

    def _has_meaningful_text(v):

        txt = (v or "").strip()

        if not txt:

            return False

        normalized = txt.lower()

        return normalized not in {"-", "--", "---", "—", "n/a", "na", "s/d", "sin datos"}

    def _direccion_compuesta(place):

        if not place:

            return _safe(client.direccion_principal)

        direccion = (getattr(place, "nombre", None) or "").strip()

        provincia = (getattr(place, "provincia", None) or "").strip()

        if direccion and provincia:

            return f"{direccion} - {provincia}"

        return direccion or provincia or "-"

    delivery_places = list(getattr(client, "delivery_places", None) or [])

    deposito = None

    for p in delivery_places:

        if "deposito" in _normalize_note(getattr(p, "nota", None)):

            deposito = p

            break

    if deposito is None and delivery_places:

        deposito = delivery_places[0]

    sucursales = [p for p in delivery_places if not deposito or p.id != deposito.id]

    direccion_deposito = _direccion_compuesta(deposito)

    horario_deposito = _safe(getattr(deposito, "horario", None) or client.delivery_schedule)

    contacto_deposito = _safe(getattr(deposito, "contacto", None) or client.delivery_contact)

    telefono_deposito = _safe(getattr(deposito, "telefono", None) or client.delivery_phone or client.telefono)

    sucursales_data = []

    for p in sucursales:

        sucursales_data.append(

            {

                "direccion": _direccion_compuesta(p),

                "horario": _safe(getattr(p, "horario", None)),

                "contacto": _safe(getattr(p, "contacto", None)),

                "telefono": _safe(getattr(p, "telefono", None)),

            }

        )

    const_docs = (

        ClientDocument.query.filter_by(client_id=client.id)

        .order_by(ClientDocument.uploaded_at.desc())

        .all()

    )

    constancia_names = []

    seen_names = set()

    for d in const_docs:

        cat = (getattr(d, "category", None) or "CONSTANCIA").upper()

        if cat != "CONSTANCIA":

            continue

        fn = (getattr(d, "filename", None) or f"documento_{d.id}").strip() or f"documento_{d.id}"

        key = fn.lower()

        if key in seen_names:

            continue

        seen_names.add(key)

        constancia_names.append(fn)

    attachments = []

    attached_files = []

    attached_keys = set()

    for d in const_docs:

        cat = (getattr(d, "category", None) or "CONSTANCIA").upper()

        if cat != "CONSTANCIA":

            continue

        filename = (getattr(d, "filename", None) or f"documento_{d.id}").strip() or f"documento_{d.id}"

        key = filename.lower()

        if key in attached_keys:

            continue

        content = None

        try:

            if getattr(d, "data", None):

                content = d.data

            elif getattr(d, "filepath", None):

                r = requests.get(d.filepath, timeout=15)

                if r.ok:

                    content = r.content

        except Exception:

            content = None

        if not content:

            continue

        maintype = "application"

        subtype = "octet-stream"

        mtype = getattr(d, "mimetype", None)

        if mtype and "/" in mtype:

            try:

                maintype, subtype = mtype.split("/", 1)

            except Exception:

                maintype, subtype = "application", "octet-stream"

        attachments.append(

            {

                "content": content,

                "maintype": maintype,

                "subtype": subtype,

                "filename": filename,

            }

        )

        attached_files.append(filename)

        attached_keys.add(key)

    cliente_nombre = " ".join([(client.apellido or "").strip(), (client.nombre or "").strip()]).strip()

    company_label = (company.nombre or "").strip() or "Empresa"

    subject = f"ALTA cliente - {cliente_nombre or client.apellido or 'Cliente'} - {company_label}"

    body_lines = [

        subject,

        "",

        f"Empresa destino: {company_label}",

        "",

        "Datos del cliente:",

        f"Razón social: {_safe(client.apellido)}",

        f"Nombre: {_safe(client.nombre)}",

        f"CUIL/CUIT: {_safe(client.cuit)}",

        "",

        "Datos de depósito:",

        f"Dirección: {direccion_deposito}",

        f"Horarios de entrega: {horario_deposito}",

        f"Contacto: {contacto_deposito}",

        f"Teléfono: {telefono_deposito}",
    ]

    if sucursales_data:

        body_lines.extend(["", "Dirección de sucursales puntuales:", ""])

        for idx, suc in enumerate(sucursales_data):

            body_lines.extend(

                [

                    "Sucursal puntual:",

                    f"Dirección: {suc['direccion']}",

                    f"Horarios de entrega: {suc['horario']}",

                    f"Contacto: {suc['contacto']}",

                    f"Teléfono: {suc['telefono']}",

                ]

            )

            if idx < len(sucursales_data) - 1:

                body_lines.append("")

    comment_text = (comment or "").strip()

    if _has_meaningful_text(comment_text):

        body_lines.extend(["", "Información complementaria:", comment_text])

    warning = None

    if not constancia_names:

        warning = "No hay constancias cargadas para adjuntar."

    elif constancia_names and not attached_files:

        warning = "Hay constancias cargadas, pero no se pudieron adjuntar automáticamente."

    body_text = "\n".join(body_lines)

    preview_attachments = attached_files or constancia_names

    if preview_mode and not confirm_send:

        return jsonify(

            {

                "ok": True,

                "preview": True,

                "to_all": to_all,

                "subject": subject,

                "body_text": body_text,

                "attachment_names": preview_attachments,

                "warning": warning,

            }

        )

    try:

        _send_email(

            subject=subject,

            to_all=to_all,

            body_text=body_text,

            attachments=attachments,

            log_tag="clientes_share_to_company",

        )

    except Exception as e:

        err_text = str(e) or "Error desconocido enviando el mail"

        if "Network is unreachable" in err_text:

            user_msg = "No se pudo conectar al servidor de correo (sin conexión o bloqueado)."

        else:

            user_msg = err_text

        return jsonify({"ok": False, "error": user_msg}), 500

    return jsonify(

        {

            "ok": True,

            "warning": warning,

            "attached_count": len(attached_files),

            "attached_files": attached_files,

        }

    )





@bp.post("/clientes/<int:client_id>/delete")

def clientes_delete(client_id: int):

    obj = Client.query.get_or_404(client_id)

    _require_owner(obj)

    db.session.delete(obj)

    db.session.commit()

    return redirect(url_for("main.clientes"))





@bp.post("/clientes/<int:client_id>/archive")

def clientes_archive(client_id: int):

    obj = Client.query.get_or_404(client_id)

    _require_owner(obj)

    obj.archived = True

    db.session.commit()

    return_to = (request.form.get("return_to") or "").strip()

    if return_to.startswith("/clientes"):

        return redirect(return_to)

    return redirect(url_for("main.clientes"))





@bp.post("/clientes/<int:client_id>/unarchive")

def clientes_unarchive(client_id: int):

    obj = Client.query.get_or_404(client_id)

    _require_owner(obj)

    obj.archived = False

    db.session.commit()

    return_to = (request.form.get("return_to") or "").strip()

    if return_to.startswith("/clientes"):

        return redirect(return_to)

    return redirect(url_for("main.clientes"))





@bp.post("/clientes/<int:client_id>/edit")

def clientes_edit(client_id: int):

    obj = Client.query.get_or_404(client_id)

    obj.apellido = request.form.get("apellido", obj.apellido)

    obj.nombre = request.form.get("nombre", obj.nombre)

    obj.sucursal = request.form.get("sucursal") or None

    obj.telefono = request.form.get("telefono") or None

    obj.mail = request.form.get("mail") or None

    fecha_inc = request.form.get("fecha_incorporacion") or None

    obj.fecha_incorporacion = _parse_date_like(fecha_inc) if fecha_inc else obj.fecha_incorporacion

    db.session.commit()

    return redirect(url_for("main.clientes"))





@bp.get("/api/clientes")

def api_clientes():

    q = (request.args.get("q") or "").strip().lower()

    base = Client.query

    if q:

        base = base.filter((Client.apellido + " " + Client.nombre).ilike(f"%{q}%"))

    res = [{"id": c.id, "label": f"{c.apellido} {c.nombre}"} for c in base.order_by(Client.apellido).limit(20)]

    return jsonify(res)





@bp.get("/api/clientes/archivados")

def api_clientes_archivados():

    q = Client.query.filter(Client.archived.is_(True))

    if not _has_global_access():

        uid = _effective_user_id()

        if uid is not None:

            q = q.filter(Client.owner_user_id == uid)

    items = [

        {

            "id": c.id,

            "razon_social": c.apellido,

            "nombre": c.nombre,

            "provincia": c.provincia,

        }

        for c in q.order_by(Client.apellido, Client.nombre).all()

    ]

    return jsonify(items)





@bp.get("/api/cobranzas/pagos_por_pedido")

def api_cobranzas_pagos_por_pedido():

    order_id = request.args.get("order_id", type=int)

    if not order_id:

        return jsonify([])

    o = Order.query.get_or_404(order_id)

    _require_owner(o)



    def _to_iso(dtv):

        try:

            if not dtv:

                return None

            try:

                return dtv.date().isoformat()

            except Exception:

                return dtv.isoformat()

        except Exception:

            return None



    rows = []

    try:

        q = (

            CollectionPayment.query

            .filter_by(order_id=order_id)

            .filter(CollectionPayment.kind != "DRAFT")

            .order_by(CollectionPayment.created_at.asc())

        )

        for p in q.all():

            try:

                kind_norm = (getattr(p, "kind", None) or "").strip().upper()

                method_raw = getattr(p, "method", None)

                method_norm = (method_raw or "").strip().upper()

                # normalizar separadores para que matchee con el frontend

                method_norm = method_norm.replace("-", "_").replace(" ", "_")

                voided_at = getattr(p, "voided_at", None)

                rows.append({

                    "id": p.id,

                    "kind": kind_norm,

                    "method": (method_norm or None),

                    "amount": float(getattr(p, "amount", 0) or 0),

                    "due_date": _to_iso(getattr(p, "due_date", None)),

                    "notes": getattr(p, "notes", None),

                    "attachment_url": getattr(p, "attachment_url", None),

                    "created_at": _to_iso(getattr(p, "created_at", None)),

                    "voided_at": _to_iso(voided_at),

                })

            except Exception:

                continue

    except Exception:

        rows = []

    return jsonify(rows)





@bp.post("/cobranzas/pagos/<int:payment_id>/anular")

def cobranzas_pago_anular(payment_id: int):

    p = CollectionPayment.query.get_or_404(payment_id)

    o = Order.query.get_or_404(int(p.order_id))

    _require_owner(o)



    # No anular borradores

    try:

        if (getattr(p, "kind", "") or "").strip().upper() == "DRAFT":

            abort(400)

    except Exception:

        abort(400)



    # Idempotente

    if getattr(p, "voided_at", None) is None:

        try:

            p.voided_at = datetime.utcnow()

        except Exception:

            pass

        try:

            uid = _effective_user_id()

            if uid is not None:

                p.voided_by_user_id = uid

        except Exception:

            pass

        try:

            reason = (request.form.get("reason") or "").strip()

            if reason:

                p.voided_reason = reason

        except Exception:

            pass



    # Recalcular Collection del pedido ignorando anulados

    try:

        coll = Collection.query.filter_by(order_id=o.id).first()

    except Exception:

        coll = None

    if coll is not None:

        try:

            # base monto

            candidates = []

            if getattr(coll, "monto", None) is not None:

                candidates.append(float(coll.monto))

            if getattr(o, "precio_final", None) is not None:

                candidates.append(float(o.precio_final))

            lg = getattr(o, "logistics", None)

            if lg is not None and getattr(lg, "precio", None) is not None:

                candidates.append(float(lg.precio))

            picked = None

            for v in candidates:

                if v is not None and v > 0:

                    picked = v

                    break

            if picked is None and candidates:

                picked = candidates[0]

            base_monto = float(picked or 0.0)

        except Exception:

            base_monto = 0.0



        total_paid = 0.0

        total_credit = 0.0

        max_due_all = None

        try:

            for pr in (

                CollectionPayment.query

                .filter_by(order_id=o.id)

                .filter(CollectionPayment.kind != "DRAFT")

                .filter(CollectionPayment.voided_at.is_(None))

                .all()

            ):

                kind = (getattr(pr, "kind", "") or "").strip().upper()

                amt = float(getattr(pr, "amount", 0) or 0)

                if kind == "CREDIT_NOTE":

                    total_credit += abs(amt)

                elif kind == "PAYMENT":

                    total_paid += abs(amt)

                dd = getattr(pr, "due_date", None)

                if dd and (max_due_all is None or dd > max_due_all):

                    max_due_all = dd

        except Exception:

            total_paid = 0.0

            total_credit = 0.0



        # Reintento tolerante por Railway si falló por columna faltante

        if total_paid == 0.0 and total_credit == 0.0:

            try:

                total_paid2 = 0.0

                total_credit2 = 0.0

                qpay = (

                    CollectionPayment.query

                    .filter_by(order_id=o.id)

                    .filter(CollectionPayment.kind != "DRAFT")

                )

                try:

                    qpay = _safe_filter_not_voided(qpay)

                except Exception:

                    pass

                for p in qpay.all():

                    kind = (getattr(p, "kind", "") or "").strip().upper()

                    amt = float(getattr(p, "amount", 0) or 0)

                    if kind == "CREDIT_NOTE":

                        total_credit2 += abs(amt)

                    elif kind == "PAYMENT":

                        total_paid2 += abs(amt)

                total_paid = float(total_paid2 or 0.0)

                total_credit = float(total_credit2 or 0.0)

            except Exception:

                try:

                    db.session.rollback()

                except Exception:

                    pass

            max_due_all = None



        net_due = float(base_monto or 0.0) - float(total_credit or 0.0)

        try:

            # actualizar vencimiento estimado al más lejano si hay alguno

            if max_due_all:

                coll.fecha_pago_estimada = max_due_all

        except Exception:

            pass



        try:

            if net_due <= 0:

                if not coll.fecha_cobro_efectiva:

                    coll.fecha_cobro_efectiva = datetime.utcnow()

            elif float(total_paid or 0.0) >= float(net_due or 0.0):

                if not coll.fecha_cobro_efectiva:

                    coll.fecha_cobro_efectiva = datetime.utcnow()

            else:

                coll.fecha_cobro_efectiva = None

        except Exception:

            pass

        # Si se cobro, asumir entrega efectiva (regla unica en _sync_entrega_desde_cobro)

        _sync_entrega_desde_cobro(coll, lg)



    try:

        db.session.commit()

    except Exception:

        db.session.rollback()



    try:

        _invalidate_notif_count_cache()

    except Exception:

        pass



    if request.headers.get("X-Requested-With") == "XMLHttpRequest":

        return jsonify({"ok": True})

    return redirect(url_for("main.nueva_cobranza", order_id=o.id))





@bp.get("/api/clientes/<int:client_id>/empresas")

def api_client_companies(client_id: int):

    c = Client.query.get_or_404(client_id)

    try:

        _require_owner(c)

    except Exception:

        abort(403)

    rows = []

    seen = set()



    # 1) Empresas vinculadas en CRM

    for l in c.links:

        if l.company:

            coid = int(l.company.id)

            if coid in seen:

                continue

            seen.add(coid)

            rows.append({

                "id": coid,

                "label": l.company.nombre,

                "mail_pedido": l.company.mail_pedido or "",

                "mail_pago": l.company.mail_pago or "",

                "pedido_estandar_recomendado": l.company.pedido_estandar_recomendado or "",

                "nota_pedido": getattr(l.company, "nota_pedido", None) or "",

                "comprobante_tipo": (getattr(l, "comprobante_tipo", None) or "FACTURA"),

                "forma_pago_default": (l.company.forma_pago_default or ""),

                "plazo_pago_dias": getattr(l, "plazo_pago_dias", None),

                "plazo_pago_promedio_dias": l.company.plazo_pago_promedio_dias,

                "status": getattr(l.status, "value", str(l.status)),

            })



    # 2) Empresas que aparecen en pedidos del cliente (aunque no estén vinculadas)

    try:

        uid = _effective_user_id()

        order_company_ids = (

            db.session.query(Order.company_id)

            .filter(Order.client_id == client_id)

            .filter(Order.deleted_at.is_(None))

            .filter(Order.company_id.isnot(None))

            .filter(

                True

                if _has_global_access()

                else (

                    (Order.owner_user_id == uid)

                    # Compatibilidad: pedidos legacy pueden tener owner_user_id NULL.

                    # Como ya validamos _require_owner(c), permitirlos para este cliente.

                    | (Order.owner_user_id.is_(None))

                )

            )

            .distinct()

            .all()

        )

        order_company_ids = [int(r[0]) for r in (order_company_ids or []) if r and r[0]]

    except Exception:

        order_company_ids = []



    if order_company_ids:

        for comp in Company.query.filter(Company.id.in_(order_company_ids)).all():

            try:

                coid = int(comp.id)

            except Exception:

                continue

            if coid in seen:

                continue

            seen.add(coid)

            rows.append({

                "id": coid,

                "label": comp.nombre,

                "mail_pedido": comp.mail_pedido or "",

                "mail_pago": comp.mail_pago or "",

                "pedido_estandar_recomendado": comp.pedido_estandar_recomendado or "",

                "nota_pedido": getattr(comp, "nota_pedido", None) or "",

                "comprobante_tipo": "FACTURA",

                "forma_pago_default": (comp.forma_pago_default or ""),

                "plazo_pago_dias": None,

                "plazo_pago_promedio_dias": comp.plazo_pago_promedio_dias,

                "status": "TRABAJA",

            })

    # Sort by name

    rows.sort(key=lambda r: r["label"].lower())

    return jsonify(rows)





@bp.get("/empresas")

def empresas():

    q = (request.args.get("q") or "").strip()

    show_all = request.args.get("all") == "1"



    page = request.args.get("page", default=1, type=int) or 1

    per_page = request.args.get("per_page", default=10, type=int) or 10

    if page < 1:

        page = 1

    if per_page < 1:

        per_page = 10

    if per_page > 50:

        per_page = 50

    base = Company.query.filter(Company.archived.is_(False))

    if q:

        ilike = f"%{q}%"

        base = base.filter((Company.marca.ilike(ilike)) | (Company.nombre.ilike(ilike)))

    base = base.order_by(Company.marca.nullslast(), Company.nombre)

    has_next = False

    has_prev = False

    if show_all:

        items = base.all()

    else:

        offset = (page - 1) * per_page

        rows = base.offset(offset).limit(per_page + 1).all()

        if len(rows) > per_page:

            has_next = True

            rows = rows[:per_page]

        has_prev = page > 1

        items = rows

    archived_items = Company.query.filter(Company.archived.is_(True)).order_by(Company.marca, Company.nombre).all()

    cq = Client.query

    if not _has_global_access():

        uid = _effective_user_id()

        if uid is not None:

            cq = cq.filter(Client.owner_user_id == uid)

    clients = cq.order_by(Client.apellido, Client.nombre).all()



    alerts_by_company = {}

    total_active_alerts = 0

    try:

        now_dt = datetime.utcnow()

        active_alerts = _compute_alerts_for_all_clients(now_dt) or []

        total_active_alerts = int(len(active_alerts) or 0)

        order_ids = sorted({int(a.get("order_id")) for a in active_alerts if a and a.get("order_id")})

        orders = []

        if order_ids:

            oq = (

                Order.query

                .options(

                    selectinload(Order.logistics),

                    selectinload(Order.collection),

                    selectinload(Order.company),

                )

                .filter(Order.id.in_(order_ids))

            )

            if not _has_global_access():

                uid = _effective_user_id()

                if uid is not None:

                    oq = oq.filter(or_(Order.owner_user_id == uid, Order.owner_user_id.is_(None)))

            orders = oq.all()

        order_map = {int(o.id): o for o in (orders or []) if o and o.id}



        for a in active_alerts:

            if not a:

                continue

            try:

                oid = int(a.get("order_id") or 0)

            except Exception:

                oid = 0

            o = order_map.get(oid) if oid else None

            if not o:

                continue

            try:

                coid = int(getattr(o, "company_id", None) or 0)

            except Exception:

                coid = 0

            if not coid:

                continue



            state_kind = (a.get("kind") or "").strip().upper()

            ui_kind = ""

            if state_kind == "COBRANZA_ATRASADA":

                ui_kind = "COBRANZA"

            elif state_kind == "ENTREGA_ATRASADA":

                ui_kind = "MERCADERIA"

            else:

                ui_kind = state_kind



            coll = getattr(o, "collection", None)

            lg = getattr(o, "logistics", None)

            company_name = None

            try:

                company_name = (getattr(getattr(o, "company", None), "nombre", None) if o else None) or (a.get("company") or None)

            except Exception:

                company_name = (a.get("company") or None)



            item = {

                "client_id": a.get("client_id"),

                "client_name": a.get("client_name"),

                "order_id": oid,

                "kind": ui_kind,

                "state_kind": state_kind,

                "message": a.get("message"),

                "severity": a.get("severity"),

                "company": company_name,

            }

            if ui_kind == "COBRANZA":

                try:

                    item["cob_entrega_efectiva"] = (coll.fecha_entrega_efectiva.date().isoformat() if coll and coll.fecha_entrega_efectiva else "")

                except Exception:

                    item["cob_entrega_efectiva"] = ""

                try:

                    item["cob_monto"] = (str(coll.monto) if coll and coll.monto is not None else "")

                except Exception:

                    item["cob_monto"] = ""

                try:

                    item["cob_forma_pago"] = ((coll.forma_pago.value if coll and coll.forma_pago else "") or "")

                except Exception:

                    item["cob_forma_pago"] = ""

                try:

                    item["cob_pago_estimado"] = (coll.fecha_pago_estimada.date().isoformat() if coll and coll.fecha_pago_estimada else "")

                except Exception:

                    item["cob_pago_estimado"] = ""

                try:

                    item["cob_cobro_efectivo"] = (coll.fecha_cobro_efectiva.date().isoformat() if coll and coll.fecha_cobro_efectiva else "")

                except Exception:

                    item["cob_cobro_efectivo"] = ""

            elif ui_kind == "MERCADERIA":

                item["st_has_logistics"] = bool(lg is not None)

                try:

                    item["st_fecha_compra"] = (lg.fecha_compra.date().isoformat() if lg and lg.fecha_compra else "")

                except Exception:

                    item["st_fecha_compra"] = ""

                try:

                    item["st_entrega_estimada"] = (lg.fecha_entrega_estimada.date().isoformat() if lg and lg.fecha_entrega_estimada else "")

                except Exception:

                    item["st_entrega_estimada"] = ""

                try:

                    item["st_entrega_efectiva"] = (lg.fecha_entrega_efectiva.date().isoformat() if lg and lg.fecha_entrega_efectiva else "")

                except Exception:

                    item["st_entrega_efectiva"] = ""

                try:

                    item["st_precio"] = (str(lg.precio) if lg and lg.precio is not None else "")

                except Exception:

                    item["st_precio"] = ""



            alerts_by_company.setdefault(coid, []).append(item)

    except Exception:

        try:

            db.session.rollback()

        except Exception:

            pass



    return render_template(

        "empresas.html",

        active="empresas",

        items=items,

        archived_items=archived_items,

        clients=clients,

        q=q,

        alerts_by_company=alerts_by_company,

        total_active_alerts=total_active_alerts,

        show_all=show_all,

        page=page,

        per_page=per_page,

        has_next=has_next,

        has_prev=has_prev,

    )





@bp.get("/empresas/nueva")

def empresas_new():

    q = Client.query

    if not _has_global_access():

        uid = _effective_user_id()

        if uid is not None:

            q = q.filter(Client.owner_user_id == uid)

    clients = q.order_by(Client.apellido, Client.nombre).all()

    return render_template("empresas_form.html", active="empresas", clients=clients)





@bp.post("/empresas/nueva")

def empresas_create():

    nombre = request.form.get("nombre", "").strip()

    marca = (request.form.get("marca", "") or "").strip() or None

    demora = request.form.get("demora", type=int)

    plazo = request.form.get("plazo", type=int)

    plazo_usual = (request.form.get("plazo_usual") or "").strip() or None

    pedido_estandar_recomendado = (request.form.get("pedido_estandar_recomendado") or "").strip() or None

    nota_pedido = (request.form.get("nota_pedido") or "").strip() or None

    forma_pago_default = ((request.form.get("forma_pago_default") or "").strip().upper() or None)

    mail_pedido_list = [m.strip() for m in request.form.getlist("mail_pedido_list") if (m or "").strip()]

    mail_pedido_single = (request.form.get("mail_pedido", "") or "").strip()

    mail_pedido = ", ".join(mail_pedido_list) if mail_pedido_list else (mail_pedido_single or None)

    mail_pago_list = [m.strip() for m in request.form.getlist("mail_pago_list") if (m or "").strip()]

    mail_pago_single = (request.form.get("mail_pago", "") or "").strip()

    mail_pago = ", ".join(mail_pago_list) if mail_pago_list else (mail_pago_single or None)

    cuit = (request.form.get("cuit", "") or "").strip() or None

    notas = (request.form.get("notas", "") or "").strip() or None

    cuenta_bancaria_notas = (request.form.get("cuenta_bancaria_notas", "") or "").strip() or None

    company = Company(

        nombre=nombre or "-",

        marca=marca,

        demora_despacho_promedio_dias=demora or 0,

        plazo_pago_promedio_dias=plazo or 30,

        plazo_usual=plazo_usual,

        pedido_estandar_recomendado=pedido_estandar_recomendado,

        nota_pedido=nota_pedido,

        forma_pago_default=forma_pago_default,

        mail_pedido=mail_pedido,

        mail_pago=mail_pago,

        cuit=cuit,

        notas=notas,

        cuenta_bancaria_notas=cuenta_bancaria_notas,

    )

    db.session.add(company)

    db.session.flush()

    client_ids = request.form.getlist("client_ids", type=int)

    if client_ids:

        for cid in client_ids:

            c = Client.query.get(cid)

            if not c:

                continue

            try:

                _require_owner(c)

            except Exception:

                continue

            link = ClientCompanyLink.query.filter_by(client_id=cid, company_id=company.id).first()

            if not link:

                link = ClientCompanyLink(client_id=cid, company_id=company.id, status=RelationStatus.TRABAJA, comprobante_tipo="FACTURA")

                db.session.add(link)

    db.session.commit()

    return redirect(url_for("main.empresas"))





@bp.post("/empresas/<int:company_id>/share_to_client")

def empresas_share_to_client(company_id: int):

    """Generar (y opcionalmente enviar) un mail desde empresa a cliente."""

    company = Company.query.get_or_404(company_id)

    data = request.get_json(silent=True) or {}

    client_id = data.get("client_id") or request.form.get("client_id", type=int)

    comment = (data.get("comment") or request.form.get("comment") or "").strip()

    def _as_bool(v):

        if isinstance(v, bool):

            return v

        if v is None:

            return False

        return str(v).strip().lower() in {"1", "true", "yes", "si", "on"}

    preview_mode = _as_bool(data.get("preview") if "preview" in data else request.form.get("preview"))

    confirm_send = _as_bool(data.get("confirm_send") if "confirm_send" in data else request.form.get("confirm_send"))

    def _has_meaningful_text(v):

        txt = (v or "").strip()

        if not txt:

            return False

        normalized = txt.lower()

        return normalized not in {"-", "--", "---", "—", "n/a", "na", "s/d", "sin datos"}



    if not client_id:

        return jsonify({"ok": False, "error": "client_id requerido"}), 400



    client = Client.query.get_or_404(client_id)

    if not (client.mail or "").strip():

        return jsonify({"ok": False, "error": "El cliente no tiene mails configurados"}), 400



    # Preparar destinatarios

    to_all = [m.strip() for m in (client.mail or "").split(",") if m.strip()]

    if not to_all:

        return jsonify({"ok": False, "error": "No se pudieron interpretar los mails del cliente"}), 400



    # Construir asunto y cuerpo

    razon_social = (company.nombre or "").strip()

    marca = (company.marca or "").strip()

    cuit = (company.cuit or "").strip()

    cuenta_bancaria = (company.cuenta_bancaria_notas or "").strip()



    subject = f"Información de empresa - {razon_social or marca or 'Empresa'}"

    body_lines = []

    body_lines.append("INFORMACIÓN DE EMPRESA")

    body_lines.append("")

    body_lines.append(f"Razón social: {razon_social or '-'}")

    body_lines.append(f"Marca: {marca or '-'}")

    body_lines.append(f"CUIL/CUIT: {cuit or '-'}")

    body_lines.append("")

    body_lines.append("Cuenta bancaria:")

    body_lines.append(cuenta_bancaria or "-")

    if _has_meaningful_text(comment):

        body_lines.append("")

        body_lines.append("Comentario:")

        body_lines.append(comment)

    body = "\n".join(body_lines)

    if preview_mode and not confirm_send:

        return jsonify(

            {

                "ok": True,

                "preview": True,

                "to_all": to_all,

                "subject": subject,

                "body_text": body,

            }

        )



    # Armar mensaje

    msg = EmailMessage()

    msg["Subject"] = subject

    from_addr = os.getenv("GMAIL_FROM") or os.getenv("GMAIL_USER")

    if not from_addr:

        return jsonify({"ok": False, "error": "Configurar GMAIL_USER o GMAIL_FROM en variables de entorno"}), 500

    msg["From"] = from_addr

    msg["To"] = ", ".join(to_all)

    msg.set_content(body)



    # Enviar por SMTP (Gmail) o modo fake según entorno

    gmail_user = os.getenv("GMAIL_USER")

    gmail_password = os.getenv("GMAIL_PASSWORD")



    # Modo desarrollo / sin salida a Internet: no envía realmente, solo loguea

    mail_fake = (os.getenv("MAIL_FAKE") or "").strip().lower() in {"1", "true", "yes", "on"}

    if mail_fake:

        print("[empresas_share_to_client] MAIL_FAKE activo - no se envía mail real")

        print("[empresas_share_to_client] From:", msg["From"], "To:", msg["To"])

        print("[empresas_share_to_client] Subject:", msg["Subject"])

        # No mostramos el cuerpo completo por consola para evitar ruido excesivo

        return jsonify({"ok": True, "fake": True})



    if not gmail_user or not gmail_password:

        return jsonify({"ok": False, "error": "Configurar GMAIL_USER y GMAIL_PASSWORD en variables de entorno"}), 500



    try:

        print("[empresas_share_to_client] Enviando mail a:", to_all)

        with smtplib.SMTP("smtp.gmail.com", 587, timeout=20) as smtp:

            smtp.starttls()

            smtp.login(gmail_user, gmail_password)

            smtp.send_message(msg)

        print("[empresas_share_to_client] Mail enviado correctamente")

    except Exception as e:

        # Mensajes más amigables para el frontend

        err_text = str(e) or "Error desconocido enviando el mail"

        if "Network is unreachable" in err_text:

            user_msg = "No se pudo conectar al servidor de correo (sin conexión o bloqueado)."

        else:

            user_msg = err_text

        print("[empresas_share_to_client] Error al enviar mail:", err_text)

        return jsonify({"ok": False, "error": user_msg}), 500



    return jsonify({"ok": True})



# Documentos de empresas (Constancias, Catálogos)

@bp.post("/empresas/<int:company_id>/docs/upload")

def empresas_docs_upload(company_id: int):

    company = Company.query.get_or_404(company_id)

    files = request.files.getlist("documents")

    category = (request.form.get("category") or "").upper() or "CONSTANCIA"

    if not files:

        return redirect(url_for("main.empresas_edit_view", company_id=company.id))

    cloud_name = os.getenv("CLOUDINARY_CLOUD_NAME")

    upload_preset = os.getenv("CLOUDINARY_UPLOAD_PRESET")

    for f in files:

        try:

            if not f or not getattr(f, "filename", None):

                continue

            content = f.read() or b""

            fname = f.filename or "documento"

            mtype = f.mimetype or "application/octet-stream"

            size = len(content)

            if size <= 0:

                continue

            url = ""

            if content and cloud_name and upload_preset:

                try:

                    r = requests.post(

                        f"https://api.cloudinary.com/v1_1/{cloud_name}/auto/upload",

                        data={"upload_preset": upload_preset},

                        files={"file": (fname, content, mtype)},

                        timeout=30,

                    )

                    if r.ok:

                        j = r.json()

                        url = j.get("secure_url") or j.get("url") or ""

                except Exception:

                    url = ""

            doc = CompanyDocument(

                company_id=company.id,

                category=category,

                filename=fname,

                filepath=url or "",

                data=content,

                mimetype=mtype,

                size=size,

            )

            db.session.add(doc)

        except Exception:

            continue

    db.session.commit()

    return redirect(url_for("main.empresas_edit_view", company_id=company.id))





@bp.post("/empresas/<int:company_id>/docs/<int:doc_id>/delete")

def empresas_docs_delete(company_id: int, doc_id: int):

    doc = CompanyDocument.query.filter_by(id=doc_id, company_id=company_id).first_or_404()

    db.session.delete(doc)

    db.session.commit()

    return redirect(url_for("main.empresas_edit_view", company_id=company_id))





@bp.get("/empresas/<int:company_id>/docs/<int:doc_id>/download")

def empresas_docs_download(company_id: int, doc_id: int):

    doc = CompanyDocument.query.filter_by(id=doc_id, company_id=company_id).first_or_404()

    try:

        if getattr(doc, "data", None):

            return send_file(

                BytesIO(doc.data),

                mimetype=doc.mimetype or "application/octet-stream",

                as_attachment=False,

                download_name=doc.filename or "documento",

            )

    except Exception:

        pass

    if doc.filepath:

        return redirect(doc.filepath)

    abort(404)





@bp.get("/estado-clientes")

def estado_clientes():

    q = Client.query

    if not _has_global_access():

        uid = _effective_user_id()

        if uid is not None:

            q = q.filter(Client.owner_user_id == uid)

    clients = q.order_by(Client.apellido, Client.nombre).all()

    return render_template("estado_clientes.html", active="clientes", clients=clients)





@bp.post("/estado-clientes/links/<int:link_id>/status")

def estado_clientes_update(link_id: int):

    link = ClientCompanyLink.query.get_or_404(link_id)

    status = request.form.get("status") or None

    if status:

        link.status = RelationStatus(status)

        db.session.commit()

    return redirect(url_for("main.estado_clientes"))





@bp.post("/estado-clientes/<int:client_id>/status")

def estado_clientes_update_all(client_id: int):

    client = Client.query.get_or_404(client_id)

    status = request.form.get("status") or None

    if status:

        new_status = RelationStatus(status)

        for l in client.links:

            l.status = new_status

        db.session.commit()

    return redirect(url_for("main.estado_clientes"))





@bp.get("/empresas/<int:company_id>/editar")

def empresas_edit_view(company_id: int):

    company = Company.query.get_or_404(company_id)

    visible_links = []

    try:

        if _has_global_access():

            visible_links = list(getattr(company, "links", None) or [])

        else:

            uid = _effective_user_id()

            visible_links = [

                l for l in (getattr(company, "links", None) or [])

                if getattr(getattr(l, "client", None), "owner_user_id", None) == uid

            ]

    except Exception:

        visible_links = []

    q = Client.query

    if not _has_global_access():

        uid = _effective_user_id()

        if uid is not None:

            q = q.filter(Client.owner_user_id == uid)

    clients = q.order_by(Client.apellido, Client.nombre).all()

    return render_template(

        "empresas_form.html",

        active="empresas",

        company=company,

        clients=clients,

        visible_links=visible_links,

        is_admin=bool(getattr(current_user, "is_admin", False)),

    )





@bp.post("/empresas/<int:company_id>/editar")

def empresas_update(company_id: int):

    obj = Company.query.get_or_404(company_id)

    obj.nombre = request.form.get("nombre", obj.nombre)

    obj.marca = (request.form.get("marca") or obj.marca)

    obj.demora_despacho_promedio_dias = request.form.get("demora", type=int) or obj.demora_despacho_promedio_dias

    obj.plazo_pago_promedio_dias = request.form.get("plazo", type=int) or obj.plazo_pago_promedio_dias

    obj.plazo_usual = (request.form.get("plazo_usual") or "").strip() or None

    obj.pedido_estandar_recomendado = (request.form.get("pedido_estandar_recomendado") or "").strip() or None

    obj.nota_pedido = (request.form.get("nota_pedido") or "").strip() or None

    obj.forma_pago_default = ((request.form.get("forma_pago_default") or "").strip().upper() or None)

    mail_pedido_list = [m.strip() for m in request.form.getlist("mail_pedido_list") if (m or "").strip()]

    mail_pedido_single = (request.form.get("mail_pedido", "") or "").strip()

    obj.mail_pedido = (", ".join(mail_pedido_list) if mail_pedido_list else (mail_pedido_single or None))

    mail_pago_list = [m.strip() for m in request.form.getlist("mail_pago_list") if (m or "").strip()]

    mail_pago_single = (request.form.get("mail_pago", "") or "").strip()

    if _has_global_access():

        obj.mail_pago = (", ".join(mail_pago_list) if mail_pago_list else (mail_pago_single or None))

    obj.cuit = (request.form.get("cuit") or None)

    obj.notas = (request.form.get("notas") or None)

    obj.cuenta_bancaria_notas = (request.form.get("cuenta_bancaria_notas") or None)



    # Sincronizar clientes vinculados si vinieron client_ids desde el formulario

    client_ids = request.form.getlist("client_ids", type=int)

    if client_ids:

        keep_ids = set(client_ids)

        # Mapear vínculos existentes (solo los que pertenecen al usuario actual si no es admin)

        visible_existing = []

        try:

            if _has_global_access():

                visible_existing = list(getattr(obj, "links", None) or [])

            else:

                uid = _effective_user_id()

                visible_existing = [

                    l for l in (getattr(obj, "links", None) or [])

                    if getattr(getattr(l, "client", None), "owner_user_id", None) == uid

                ]

        except Exception:

            visible_existing = []

        existing = {l.client_id: l for l in visible_existing}

        # Eliminar vínculos que ya no están seleccionados (solo los visibles del usuario)

        for l in list(visible_existing):

            if l.client_id not in keep_ids:

                db.session.delete(l)

        # Crear vínculos nuevos para los ids seleccionados que no existían

        for cid in keep_ids:

            if cid in existing:

                continue

            c = Client.query.get(cid)

            if not c:

                continue

            try:

                _require_owner(c)

            except Exception:

                continue

            db.session.add(ClientCompanyLink(client_id=cid, company_id=obj.id, status=RelationStatus.TRABAJA, comprobante_tipo="FACTURA"))



    db.session.commit()

    return redirect(url_for("main.empresas"))





@bp.post("/empresas/<int:company_id>/links/add")

def empresas_link_add(company_id: int):

    company = Company.query.get_or_404(company_id)

    client_id = request.form.get("client_id", type=int)

    status = request.form.get("status") or RelationStatus.TRABAJA.value

    comprobante_tipo = (request.form.get("comprobante_tipo") or "").upper() or None

    descuento_raw = (request.form.get("descuento") or "").strip()

    client = Client.query.get_or_404(client_id)

    _require_owner(client)

    link = ClientCompanyLink.query.filter_by(client_id=client.id, company_id=company.id).first()

    if not link:

        link = ClientCompanyLink(client_id=client.id, company_id=company.id)

        db.session.add(link)

    link.status = RelationStatus(status)

    if comprobante_tipo in ("FACTURA", "REMITO"):

        link.comprobante_tipo = comprobante_tipo

    if not getattr(link, "comprobante_tipo", None):

        link.comprobante_tipo = "FACTURA"

    if descuento_raw != "":

        try:

            val = int(round(float(descuento_raw)))

            if val < 0:

                val = 0

            if val > 100:

                val = 100

            link.descuento = val

        except Exception:

            pass

    db.session.commit()

    return redirect(url_for("main.empresas", open_links=company.id))





@bp.post("/empresas/<int:company_id>/links/<int:link_id>/status")

def empresas_link_update(company_id: int, link_id: int):

    link = ClientCompanyLink.query.filter_by(id=link_id, company_id=company_id).first_or_404()

    try:

        client = Client.query.get(getattr(link, "client_id", None))

        if client is not None:

            _require_owner(client)

    except Exception:

        abort(403)

    status = request.form.get("status") or None

    comprobante_tipo = (request.form.get("comprobante_tipo") or "").upper() or None

    descuento_raw = (request.form.get("descuento") or "").strip()

    if status:

        link.status = RelationStatus(status)

    if comprobante_tipo in ("FACTURA", "REMITO"):

        link.comprobante_tipo = comprobante_tipo

    if descuento_raw != "":

        try:

            val = int(round(float(descuento_raw)))

            if val < 0:

                val = 0

            if val > 100:

                val = 100

            link.descuento = val

        except Exception:

            pass

    db.session.commit()

    return redirect(url_for("main.empresas", open_links=company_id))





@bp.post("/empresas/<int:company_id>/links/<int:link_id>/delete")

def empresas_link_delete(company_id: int, link_id: int):

    link = ClientCompanyLink.query.filter_by(id=link_id, company_id=company_id).first_or_404()

    try:

        client = Client.query.get(getattr(link, "client_id", None))

        if client is not None:

            _require_owner(client)

    except Exception:

        abort(403)

    db.session.delete(link)

    db.session.commit()

    return redirect(url_for("main.empresas", open_links=company_id))





@bp.post("/empresas/<int:company_id>/delete")

def empresas_delete(company_id: int):

    obj = Company.query.get_or_404(company_id)

    db.session.delete(obj)

    db.session.commit()

    return redirect(url_for("main.empresas"))





@bp.post("/empresas/<int:company_id>/edit")

def empresas_edit(company_id: int):

    obj = Company.query.get_or_404(company_id)

    obj.nombre = request.form.get("nombre", obj.nombre)

    obj.demora_despacho_promedio_dias = request.form.get("demora", type=int) or obj.demora_despacho_promedio_dias

    obj.plazo_pago_promedio_dias = request.form.get("plazo", type=int) or obj.plazo_pago_promedio_dias

    obj.mail_pedido = (request.form.get("mail_pedido") or None)

    obj.mail_pago = (request.form.get("mail_pago") or None)

    obj.plazo_usual = (request.form.get("plazo_usual") or "").strip() or None

    obj.pedido_estandar_recomendado = (request.form.get("pedido_estandar_recomendado") or "").strip() or None

    obj.forma_pago_default = ((request.form.get("forma_pago_default") or "").strip().upper() or None)

    db.session.commit()

    return redirect(url_for("main.empresas"))





@bp.get("/api/empresas")

def api_empresas():

    q = (request.args.get("q") or "").strip().lower()

    base = Company.query

    if q:

        base = base.filter(Company.nombre.ilike(f"%{q}%"))

    res = [{"id": e.id, "label": e.nombre} for e in base.order_by(Company.nombre).limit(20)]

    return jsonify(res)





@bp.get("/pedidos")

def pedidos():

    order_id = request.args.get("order_id", type=int)

    return_to = (request.args.get("return_to") or "").strip() or None

    order = None

    logistics = None

    today_iso = date.today().isoformat()

    if order_id:

        order = Order.query.get_or_404(order_id)

        _require_owner(order)

        logistics = LogisticsStatus.query.filter_by(order_id=order.id).first()

        try:

            if logistics and logistics.fecha_compra:

                today_iso = logistics.fecha_compra.date().isoformat()

            elif order.created_at:

                today_iso = order.created_at.date().isoformat()

        except Exception:

            today_iso = date.today().isoformat()

    return render_template(

        "pedidos.html",

        active="pedidos",

        today=today_iso,

        clientes=(

            (Client.query if _has_global_access() else Client.query.filter(Client.owner_user_id == _effective_user_id()))

            .order_by(Client.apellido, Client.nombre)

            .all()

        ),

        edit_order=order,

        edit_logistics=logistics,

        return_to=return_to,

    )





@bp.get("/api/pedidos/drafts")

def api_pedidos_drafts_list():

    client_id = request.args.get("client_id", type=int)

    company_id = request.args.get("company_id", type=int)

    q = (

        db.session.query(OrderDraft, Client, Company)

        .join(Client, OrderDraft.client_id == Client.id)

        .join(Company, OrderDraft.company_id == Company.id)

        .order_by(Client.apellido.asc(), Client.nombre.asc(), Company.nombre.asc())

    )

    if not _has_global_access():

        uid = _effective_user_id()

        if uid is None:

            abort(403)

        q = q.filter(OrderDraft.owner_user_id == uid)

    if client_id:

        q = q.filter(OrderDraft.client_id == client_id)

    if company_id:

        q = q.filter(OrderDraft.company_id == company_id)



    rows = []

    for d, c, co in q.limit(200).all():

        try:

            rows.append({

                "id": int(getattr(d, "id", 0) or 0),

                "client_id": int(getattr(d, "client_id", 0) or 0),

                "company_id": int(getattr(d, "company_id", 0) or 0),

                "client_label": " ".join([x for x in [getattr(c, "apellido", None), getattr(c, "nombre", None)] if x]) or (getattr(c, "apellido", None) or ""),

                "company_label": (getattr(co, "nombre", None) or ""),

                "branch_id": (getattr(d, "branch_id", None) or ""),

                "sucursal": (getattr(d, "sucursal", None) or ""),

                "nota": (getattr(d, "nota", None) or ""),

                "descripcion": (getattr(d, "descripcion", None) or ""),

                "precio_final": (str(getattr(d, "precio_final", "") or "") if getattr(d, "precio_final", None) is not None else ""),

                "forma_pago": (getattr(getattr(d, "forma_pago", None), "value", None) or ""),

                "forma_pago_detalle": (getattr(d, "forma_pago_detalle", None) or ""),

                "plazo_pago_dias": (getattr(d, "plazo_pago_dias", None) or ""),

                "fecha_compra": "",

            })

        except Exception:

            continue

    return jsonify(rows)





@bp.post("/api/pedidos/drafts")

def api_pedidos_drafts_upsert():

    client_id = request.form.get("client_id", type=int)

    company_id = request.form.get("company_id", type=int)

    if not client_id or not company_id:

        abort(400)



    client = Client.query.get_or_404(client_id)

    _require_owner(client)

    company = Company.query.get_or_404(company_id)



    uid = _effective_user_id() if not _has_global_access() else None

    if not _has_global_access() and uid is None:

        abort(403)



    d = None

    try:

        base = OrderDraft.query.filter_by(client_id=client_id, company_id=company_id)

        if not _has_global_access():

            base = base.filter(OrderDraft.owner_user_id == uid)

        d = base.first()

    except Exception:

        d = None



    if d is None:

        d = OrderDraft(

            owner_user_id=(None if _has_global_access() else uid),

            client_id=client_id,

            company_id=company_id,

        )

        db.session.add(d)



    try:

        d.branch_id = request.form.get("branch_id", type=int)

    except Exception:

        pass

    try:

        d.sucursal = (request.form.get("sucursal") or "")

    except Exception:

        pass

    try:

        d.nota = (request.form.get("nota") or "")

    except Exception:

        pass

    try:

        d.descripcion = (request.form.get("descripcion") or "")

    except Exception:

        pass

    try:

        d.plazo_pago_dias = request.form.get("plazo_pago_dias", type=int)

    except Exception:

        pass

    try:

        d.forma_pago_detalle = (request.form.get("forma_pago_detalle") or "").strip().upper() or None

    except Exception:

        pass

    try:

        fp = (request.form.get("forma_pago") or "").strip().upper() or ""

        d.forma_pago = PaymentMethod(fp) if fp else None

    except Exception:

        d.forma_pago = None

    try:

        d.precio_final = _parse_amount_like(request.form.get("precio_final"))

    except Exception:

        pass



    try:

        db.session.commit()

    except Exception:

        db.session.rollback()

        abort(500)



    return jsonify({"ok": True, "id": int(getattr(d, "id", 0) or 0)})





@bp.delete("/api/pedidos/drafts/<int:draft_id>")

def api_pedidos_drafts_delete(draft_id: int):

    d = OrderDraft.query.get_or_404(draft_id)

    if not _has_global_access():

        uid = _effective_user_id()

        if uid is None or int(getattr(d, "owner_user_id", 0) or 0) != int(uid):

            abort(403)

    db.session.delete(d)

    try:

        db.session.commit()

    except Exception:

        db.session.rollback()

        abort(500)

    return jsonify({"ok": True})





@bp.get("/historial/eliminados")

def historial_eliminados():

    q = (

        Order.query

        .filter(Order.deleted_at.isnot(None))

        .options(

            selectinload(Order.client),

            selectinload(Order.company),

            selectinload(Order.logistics),

            selectinload(Order.collection),

        )

        .order_by(Order.deleted_at.desc(), Order.id.desc())

    )

    if not _has_global_access():

        uid = _effective_user_id()

        if uid is not None:

            q = q.filter(Order.owner_user_id == uid)

    items = q.limit(500).all()

    return render_template("pedidos_eliminados.html", active="historial", orders=items)





@bp.post("/historial/<int:order_id>/update")

def historial_update(order_id: int):

    o = Order.query.get_or_404(order_id)

    _require_owner(o)

    nota = request.form.get("nota")

    descripcion = request.form.get("descripcion")

    monto = request.form.get("monto", type=float)

    entrega_efectiva_raw = (request.form.get("fecha_entrega_efectiva") or "").strip()

    cobro_efectivo_raw = (request.form.get("fecha_cobro_efectiva") or "").strip()



    if nota is not None:

        o.nota = nota

    if descripcion is not None:

        o.descripcion = descripcion



    # Asegurar logistics/collection según lo editado

    lg = LogisticsStatus.query.filter_by(order_id=o.id).first()

    coll = Collection.query.filter_by(order_id=o.id).first()



    if entrega_efectiva_raw is not None:

        if not lg:

            lg = LogisticsStatus(order_id=o.id)

            db.session.add(lg)

        try:

            lg.fecha_entrega_efectiva = _parse_datetime_like(entrega_efectiva_raw) if entrega_efectiva_raw else None

        except Exception:

            pass



    if cobro_efectivo_raw is not None:

        if not coll:

            coll = Collection(order_id=o.id)

            db.session.add(coll)

        try:

            coll.fecha_cobro_efectiva = _parse_datetime_like(cobro_efectivo_raw) if cobro_efectivo_raw else None

        except Exception:

            pass



    # Si se cobro, asumir entrega efectiva (regla unica en _sync_entrega_desde_cobro)

    _sync_entrega_desde_cobro(coll, lg)



    if monto is not None:

        o.precio_final = monto

        if lg:

            lg.precio = monto

        if coll:

            coll.monto = monto



    db.session.commit()



    return_to = (request.form.get("return_to") or "").strip()

    if return_to and return_to.startswith("/") and ("//" not in return_to):

        return redirect(return_to)

    return redirect(url_for("main.historial"))





@bp.post("/pedidos")

def pedidos_create():

    order_id = request.form.get("order_id", type=int)

    # Get selected client/company IDs from the form

    client_id = request.form.get("client_id", type=int)

    company_id = request.form.get("company_id", type=int)

    sucursal = request.form.get("sucursal") or None

    branch_id = request.form.get("branch_id", type=int)

    nota = request.form.get("nota") or None

    descripcion = request.form.get("descripcion") or None

    # fechas proporcionadas por el formulario

    fecha_compra_raw = (request.form.get("fecha_compra") or "").strip()

    fecha_entrega_estimada_raw = (request.form.get("fecha_entrega_estimada") or "").strip()

    # tipo de comprobante (FACTURA / REMITO)

    tipo_comprobante = (request.form.get("tipo_comprobante") or "").strip().upper() or None

    precio_final_raw = request.form.get("precio_final")

    precio_final = _parse_amount_like(precio_final_raw)

    forma_pago = (request.form.get("forma_pago") or "").strip().upper() or None

    forma_pago_detalle = (request.form.get("forma_pago_detalle") or "").strip().upper() or None

    plazo_pago_dias = request.form.get("plazo_pago_dias", type=int)



    if not client_id:

        abort(400, "Debe seleccionar un cliente")

    if not company_id:

        abort(400, "Debe seleccionar una empresa vinculada al cliente")

    if tipo_comprobante not in ("FACTURA", "REMITO"):

        abort(400, "Debe seleccionar si es Factura o Remito")



    client = Client.query.get_or_404(client_id)

    company = Company.query.get_or_404(company_id)

    _require_owner(client)



    # Plazo de pago del pedido: default empresa pero editable

    if plazo_pago_dias is None:

        try:

            plazo_pago_dias = int(company.plazo_pago_promedio_dias or 0)

        except Exception:

            plazo_pago_dias = None



    forma_pago_enum = None

    if forma_pago:

        try:

            forma_pago_enum = PaymentMethod(forma_pago)

        except Exception:

            forma_pago_enum = None



    # Si no se envió branch_id, tomar la primera sucursal del cliente como default

    if not branch_id:

        first_branch = ClientBranch.query.filter_by(client_id=client.id).order_by(ClientBranch.id.asc()).first()

        if first_branch:

            branch_id = first_branch.id

            # también setear texto sucursal si no vino

            if not sucursal:

                sucursal = first_branch.nombre



    if order_id:

        order = Order.query.get_or_404(order_id)

        order.client = client

        order.company = company

        order.sucursal = sucursal

        order.branch_id = branch_id

        order.nota = nota

        order.descripcion = descripcion

        order.precio_final = precio_final

        order.forma_pago = forma_pago_enum

        order.forma_pago_detalle = forma_pago_detalle

        order.tipo_comprobante = tipo_comprobante

        order.plazo_pago_dias = plazo_pago_dias

        order.demora_despacho_promedio_dias = company.demora_despacho_promedio_dias

        order.mail_pedido = company.mail_pedido

        db.session.add(order)

        db.session.flush()

    else:

        order = Order(client=client, company=company, sucursal=sucursal, branch_id=branch_id, nota=nota, descripcion=descripcion,

                      precio_final=precio_final, forma_pago=forma_pago_enum, forma_pago_detalle=forma_pago_detalle, tipo_comprobante=tipo_comprobante,

                      plazo_pago_dias=plazo_pago_dias,

                      demora_despacho_promedio_dias=company.demora_despacho_promedio_dias,

                      mail_pedido=company.mail_pedido)

        uid = _effective_user_id()

        if uid is not None:

            order.owner_user_id = uid

        db.session.add(order)

        db.session.flush()



    # Regla: al crear pedido, si la relación estaba A_INCORPORAR pasa a TRABAJA

    try:

        link = ClientCompanyLink.query.filter_by(client_id=client.id, company_id=company.id).first()

        if not link:

            link = ClientCompanyLink(

                client_id=client.id,

                company_id=company.id,

                status=RelationStatus.TRABAJA,

                comprobante_tipo="FACTURA",

            )

            db.session.add(link)

        if link.status == RelationStatus.A_INCORPORAR:

            link.status = RelationStatus.TRABAJA

        link.plazo_pago_dias = plazo_pago_dias

    except Exception:

        db.session.rollback()



    # Create/update logistics record

    logistics = LogisticsStatus.query.filter_by(order_id=order.id).first()

    try:

        fecha_compra = _parse_datetime_like(fecha_compra_raw) if fecha_compra_raw else datetime.utcnow()

        if not fecha_compra:

            fecha_compra = datetime.utcnow()

    except Exception:

        fecha_compra = datetime.utcnow()

    # fecha_entrega_estimada: usar la provista o calcular por demora promedio

    try:

        parsed_est = _parse_datetime_like(fecha_entrega_estimada_raw) if fecha_entrega_estimada_raw else None

        fecha_estimada = parsed_est if parsed_est else (fecha_compra + timedelta(days=company.demora_despacho_promedio_dias or 0))

    except Exception:

        fecha_estimada = fecha_compra + timedelta(days=company.demora_despacho_promedio_dias or 0)

    if logistics:

        logistics.fecha_compra = fecha_compra

        logistics.fecha_entrega_estimada = fecha_estimada

        logistics.nota = nota

        logistics.descripcion = descripcion

        logistics.precio = precio_final

        logistics.forma_pago = order.forma_pago

        logistics.forma_pago_detalle = order.forma_pago_detalle

        db.session.add(logistics)

    else:

        logistics = LogisticsStatus(order_id=order.id, fecha_compra=fecha_compra,

                                    fecha_entrega_estimada=fecha_estimada, nota=nota, descripcion=descripcion,

                                    precio=precio_final, forma_pago=order.forma_pago, forma_pago_detalle=order.forma_pago_detalle)

        db.session.add(logistics)

    files = request.files.getlist("attachments")

    cloud_name = os.getenv("CLOUDINARY_CLOUD_NAME")

    upload_preset = os.getenv("CLOUDINARY_UPLOAD_PRESET")

    if files and cloud_name and upload_preset:

        for f in files:

            try:

                r = requests.post(

                    f"https://api.cloudinary.com/v1_1/{cloud_name}/auto/upload",

                    data={"upload_preset": upload_preset, "resource_type": "auto"},

                    files={"file": (f.filename, f.stream, f.mimetype)},

                )

                if r.ok:

                    url = r.json().get("secure_url") or r.json().get("url")

                    if url:

                        db.session.add(OrderAttachment(order_id=order.id, url=url))

            except Exception:

                pass



    # Si se creó un pedido nuevo, eliminar borradores (pendientes) del mismo cliente+empresa.

    # Nota: los borradores se guardan en servidor en OrderDraft (además del fallback localStorage).

    try:

        if not order_id:

            base = OrderDraft.query.filter_by(client_id=int(client.id), company_id=int(company.id))

            if _has_global_access():

                base = base.filter(OrderDraft.owner_user_id.is_(None))

            else:

                uid = _effective_user_id()

                if uid is not None:

                    base = base.filter(OrderDraft.owner_user_id == uid)

            for d in base.all():

                try:

                    db.session.delete(d)

                except Exception:

                    pass

    except Exception:

        pass

    db.session.commit()

    # Volver a la pantalla que inició la edición (por ejemplo /deudas)

    return_to = (request.form.get("return_to") or "").strip()

    if return_to and return_to.startswith("/") and ("//" not in return_to):

        return redirect(return_to)

    return redirect(url_for("main.status"))





@bp.get("/status")

def status():

    from sqlalchemy import or_



    q = (

        LogisticsStatus.query

        .join(Order)

        .options(

            selectinload(LogisticsStatus.order).selectinload(Order.client),

            selectinload(LogisticsStatus.order).selectinload(Order.company),

        )

    )

    q = q.filter(Order.deleted_at.is_(None))

    estados = request.args.getlist("status")

    tipos = request.args.getlist("tipo")  # FACTURA / REMITO

    desde = request.args.get("from")

    hasta = request.args.get("to")

    client_q = (request.args.get("client_q") or "").strip()

    company_q = (request.args.get("company_q") or "").strip()

    if estados:

        estado_map = {

            "EN_CAMINO": lambda s: s.fecha_entrega_efectiva.is_(None) and (s.fecha_entrega_estimada.is_(None) | (LogisticsStatus.fecha_entrega_estimada >= datetime.utcnow())),

            "ATRASADO": lambda s: s.fecha_entrega_efectiva.is_(None) and (LogisticsStatus.fecha_entrega_estimada < datetime.utcnow()),

            "ENTREGADO": lambda s: LogisticsStatus.fecha_entrega_efectiva.isnot(None),

        }

        conds = []

        now = datetime.utcnow()

        if "EN_CAMINO" in estados:

            conds.append((LogisticsStatus.fecha_entrega_efectiva.is_(None)) & ((LogisticsStatus.fecha_entrega_estimada.is_(None)) | (LogisticsStatus.fecha_entrega_estimada >= now)))

        if "ATRASADO" in estados:

            conds.append((LogisticsStatus.fecha_entrega_efectiva.is_(None)) & (LogisticsStatus.fecha_entrega_estimada < now))

        if "ENTREGADO" in estados:

            conds.append(LogisticsStatus.fecha_entrega_efectiva.isnot(None))

        if conds:

            q = q.filter(or_(*conds))

    else:

        # Por defecto mostrar solo lo pendiente de entrega (EN_CAMINO + ATRASADO).

        # Si el usuario quiere ver ENTREGADO, lo activa con el filtro.

        q = q.filter(LogisticsStatus.fecha_entrega_efectiva.is_(None))

    if tipos:

        q = q.filter(Order.tipo_comprobante.in_(tipos))

    if client_q:

        q = q.join(Client, Order.client_id == Client.id)

        pat = f"%{client_q}%"

        q = q.filter(or_(Client.apellido.ilike(pat), Client.nombre.ilike(pat)))

    if company_q:

        q = q.join(Company, Order.company_id == Company.id)

        pat = f"%{company_q}%"

        q = q.filter(Company.nombre.ilike(pat))

    if desde:

        try:

            d = _parse_datetime_like(desde)

            if not d:

                raise ValueError("invalid_from")

            q = q.filter(LogisticsStatus.fecha_compra >= d)

        except Exception:

            pass

    if hasta:

        try:

            h = _parse_datetime_like(hasta)

            if not h:

                raise ValueError("invalid_to")

            q = q.filter(LogisticsStatus.fecha_compra <= h)

        except Exception:

            pass

    # Mostrar primero el último agregado (proxy: id descendente)

    show_all = (request.args.get("all") == "1") or bool(client_q or company_q)

    q = q.order_by(LogisticsStatus.id.desc())

    if not show_all:

        q = q.limit(300)

    items = q.all()

    return render_template("status.html", active="status", items=items)





@bp.post("/status/<int:order_id>/entregar")

def status_mark_entregado(order_id: int):

    logistics = LogisticsStatus.query.filter_by(order_id=order_id).first_or_404()

    if not logistics.fecha_entrega_efectiva:

        # Preferir la fecha estimada (o la enviada por el form) como efectiva; fallback a hoy

        picked = None

        try:

            raw = (request.form.get("fecha") or "").strip()

            if raw:

                picked = _parse_datetime_like(raw)

        except Exception:

            picked = None

        if picked is None:

            picked = getattr(logistics, "fecha_entrega_estimada", None)

        logistics.fecha_entrega_efectiva = picked or datetime.utcnow()

        # Create or update collection

        coll = Collection.query.filter_by(order_id=order_id).first()

        if not coll:

            coll = Collection(order_id=order_id)

            db.session.add(coll)

        coll.fecha_entrega_efectiva = logistics.fecha_entrega_efectiva

        try:

            plazo_days = None

            if getattr(logistics.order, "plazo_pago_dias", None) is not None:

                plazo_days = int(logistics.order.plazo_pago_dias or 0)

            elif logistics.order.company and logistics.order.company.plazo_pago_promedio_dias is not None:

                plazo_days = int(logistics.order.company.plazo_pago_promedio_dias or 0)

            else:

                plazo_days = 30

        except Exception:

            plazo_days = 30

        coll.fecha_pago_estimada = logistics.fecha_entrega_efectiva + timedelta(days=plazo_days)

        coll.monto = logistics.precio

        coll.forma_pago = logistics.forma_pago

        db.session.commit()

    if request.headers.get("X-Requested-With") == "XMLHttpRequest":

        return jsonify({"ok": True})

    return redirect(url_for("main.status"))





@bp.post("/status/<int:order_id>/update")

def status_update(order_id: int):

    logistics = LogisticsStatus.query.filter_by(order_id=order_id).first_or_404()

    precio_raw = request.form.get("precio")

    precio = None

    precio_is_set = False

    if precio_raw is not None:

        precio_is_set = True

        try:

            precio = float(precio_raw) if str(precio_raw).strip() != "" else None

        except Exception:

            precio = None

    forma_pago_raw = request.form.get("forma_pago")

    forma_pago = None if forma_pago_raw == "" else (PaymentMethod(forma_pago_raw) if forma_pago_raw else None)

    fecha_entrega_estimada_raw = (request.form.get("fecha_entrega_estimada") or "").strip()

    fecha_compra_raw = (request.form.get("fecha_compra") or "").strip()

    fecha_entrega_efectiva_raw = (request.form.get("fecha_entrega_efectiva") or "").strip()

    if precio_is_set:

        logistics.precio = precio

    if forma_pago_raw is not None:

        logistics.forma_pago = forma_pago

    if fecha_compra_raw is not None:

        try:

            logistics.fecha_compra = _parse_datetime_like(fecha_compra_raw) if fecha_compra_raw else None

        except Exception:

            pass

    if fecha_entrega_estimada_raw:

        try:

            logistics.fecha_entrega_estimada = _parse_datetime_like(fecha_entrega_estimada_raw)

        except Exception:

            pass

    if fecha_entrega_efectiva_raw is not None:

        try:

            logistics.fecha_entrega_efectiva = _parse_datetime_like(fecha_entrega_efectiva_raw) if fecha_entrega_efectiva_raw else None

        except Exception:

            pass

    # Mantener consistencia con cobranzas si existe registro

    coll = Collection.query.filter_by(order_id=order_id).first()

    if coll:

        if precio_is_set:

            coll.monto = precio

        if forma_pago_raw is not None:

            coll.forma_pago = forma_pago

        if fecha_entrega_efectiva_raw is not None:

            try:

                coll.fecha_entrega_efectiva = _parse_datetime_like(fecha_entrega_efectiva_raw) if fecha_entrega_efectiva_raw else None

            except Exception:

                pass

    else:

        # Si se cargó entrega efectiva manualmente, crear cobranzas para mantener conexión

        if fecha_entrega_efectiva_raw:

            try:

                coll = Collection(order_id=order_id)

                coll.fecha_entrega_efectiva = _parse_datetime_like(fecha_entrega_efectiva_raw)

                try:

                    plazo_days = None

                    if getattr(logistics.order, "plazo_pago_dias", None) is not None:

                        plazo_days = int(logistics.order.plazo_pago_dias or 0)

                    elif logistics.order.company and logistics.order.company.plazo_pago_promedio_dias is not None:

                        plazo_days = int(logistics.order.company.plazo_pago_promedio_dias or 0)

                    else:

                        plazo_days = 30

                except Exception:

                    plazo_days = 30

                coll.fecha_pago_estimada = coll.fecha_entrega_efectiva + timedelta(days=plazo_days)

                coll.monto = logistics.precio

                coll.forma_pago = logistics.forma_pago

                db.session.add(coll)

            except Exception:

                pass

    # Mantener consistencia con la Orden

    order = Order.query.get(order_id)

    if order:

        if precio_is_set:

            order.precio_final = precio

        if forma_pago_raw is not None:

            order.forma_pago = forma_pago

    db.session.commit()

    try:

        _invalidate_notif_count_cache()

    except Exception:

        pass

    if request.headers.get("X-Requested-With") == "XMLHttpRequest":

        return jsonify({"ok": True})

    return_to = (request.form.get("return_to") or "").strip()

    if return_to and return_to.startswith("/") and ("//" not in return_to):

        return redirect(return_to)

    return redirect(url_for("main.status"))





@bp.post("/cobranzas/<int:order_id>/entregar")

def cobranzas_mark_entregado(order_id: int):

    coll = Collection.query.filter_by(order_id=order_id).first_or_404()

    o = Order.query.get_or_404(order_id)

    if not _has_global_access():

        _require_owner(o)



    now_dt = datetime.now()

    logistics = None

    try:

        logistics = LogisticsStatus.query.filter_by(order_id=order_id).first()

    except Exception:

        logistics = None

    if logistics is None:

        logistics = LogisticsStatus(order_id=order_id)

        try:

            logistics.fecha_compra = getattr(o, "created_at", None) or now_dt

        except Exception:

            logistics.fecha_compra = now_dt

        db.session.add(logistics)



    # Al marcar ENTREGADO, la entrega efectiva debe ser la REAL (hoy), no una estimación previa.

    try:

        logistics.fecha_entrega_efectiva = now_dt

    except Exception:

        pass



    # Mantener consistencia visual en Deudas (columna editable)

    try:

        coll.fecha_entrega_efectiva = getattr(logistics, "fecha_entrega_efectiva", None) or now_dt

    except Exception:

        pass



    # Autocalcular vencimiento (fecha_pago_estimada) si corresponde

    try:

        if getattr(coll, "fecha_pago_estimada", None) is None and getattr(coll, "fecha_entrega_efectiva", None) is not None:

            try:

                plazo_days = None

                if getattr(o, "plazo_pago_dias", None) is not None:

                    plazo_days = int(o.plazo_pago_dias or 0)

                elif o.company and getattr(o.company, "plazo_pago_promedio_dias", None) is not None:

                    plazo_days = int(o.company.plazo_pago_promedio_dias or 0)

                else:

                    plazo_days = 30

            except Exception:

                plazo_days = 30

            coll.fecha_pago_estimada = coll.fecha_entrega_efectiva + timedelta(days=int(plazo_days or 0))

    except Exception:

        pass



    db.session.commit()

    try:

        _invalidate_notif_count_cache()

    except Exception:

        pass

    if request.headers.get("X-Requested-With") == "XMLHttpRequest":

        try:

            iso = logistics.fecha_entrega_efectiva.date().isoformat() if logistics and logistics.fecha_entrega_efectiva else ""

        except Exception:

            iso = ""

        try:

            venc_iso = coll.fecha_pago_estimada.date().isoformat() if coll and coll.fecha_pago_estimada else ""

        except Exception:

            venc_iso = ""

        return jsonify({"ok": True, "fecha_entrega_efectiva": iso, "fecha_pago_estimada": venc_iso})

    return redirect(url_for("main.deudas_pendientes"))





@bp.get("/deudas")

def deudas_pendientes():

    from sqlalchemy import or_



    # Auto-pasaje a Cobranzas (provisorio):

    # Si la entrega estimada ya venció y no hay entrega efectiva en logística,

    # asumir entrega "provisoria" SOLO para cobranzas (Collection.fecha_entrega_efectiva)

    # para evitar doble confirmación con Status Mercadería.

    try:

        now_dt = datetime.utcnow()

        overdue_lg = (

            LogisticsStatus.query

            .join(Order)

            .options(selectinload(LogisticsStatus.order).selectinload(Order.company))

            .filter(LogisticsStatus.fecha_entrega_efectiva.is_(None))

            .filter(LogisticsStatus.fecha_entrega_estimada.isnot(None))

            .filter(LogisticsStatus.fecha_entrega_estimada < now_dt)

        )

        for lg in overdue_lg.all():

            o = getattr(lg, "order", None)

            if not o:

                continue

            # Si ya existe entrega efectiva en cobranzas, no tocar

            coll = Collection.query.filter_by(order_id=o.id).first()

            if coll and getattr(coll, "fecha_entrega_efectiva", None):

                continue

            # Crear cobranza si no existe

            if not coll:

                coll = Collection(order_id=o.id)

                db.session.add(coll)



            # No setear entrega efectiva automáticamente desde la estimada.

            # Si se había seteado una entrega "provisoria" en cobranzas (igual a la estimada)

            # y logística sigue sin entrega efectiva, revertirla para que quede claro "SIN ENTREGA".

            try:

                if (

                    getattr(coll, "fecha_entrega_efectiva", None)

                    and getattr(lg, "fecha_entrega_efectiva", None) is None

                    and getattr(lg, "fecha_entrega_estimada", None) is not None

                    and getattr(coll, "fecha_entrega_efectiva", None) == getattr(lg, "fecha_entrega_estimada", None)

                ):

                    coll.fecha_entrega_efectiva = None

            except Exception:

                pass



            # Monto base (preferir valores > 0)

            try:

                candidates = []

                if getattr(coll, "monto", None) is not None:

                    candidates.append(float(coll.monto))

                if getattr(o, "precio_final", None) is not None:

                    candidates.append(float(o.precio_final))

                if getattr(lg, "precio", None) is not None:

                    candidates.append(float(lg.precio))

                picked = None

                for v in candidates:

                    if v is not None and v > 0:

                        picked = v

                        break

                if picked is None and candidates:

                    picked = candidates[0]

                if picked is not None and (getattr(coll, "monto", None) is None or float(coll.monto or 0) <= 0):

                    coll.monto = float(picked)

            except Exception:

                pass



            # Vencimiento por plazo (si no hay vencimiento ya definido)

            try:

                if getattr(coll, "fecha_pago_estimada", None) is None and getattr(coll, "fecha_entrega_efectiva", None) is not None:

                    try:

                        plazo_days = None

                        if getattr(o, "plazo_pago_dias", None) is not None:

                            plazo_days = int(o.plazo_pago_dias or 0)

                        elif o.company and getattr(o.company, "plazo_pago_promedio_dias", None) is not None:

                            plazo_days = int(o.company.plazo_pago_promedio_dias or 0)

                        else:

                            plazo_days = 30

                    except Exception:

                        plazo_days = 30

                    coll.fecha_pago_estimada = coll.fecha_entrega_efectiva + timedelta(days=plazo_days)

            except Exception:

                pass



        try:

            db.session.commit()

        except Exception:

            db.session.rollback()

    except Exception:

        try:

            db.session.rollback()

        except Exception:

            pass



    # Incluir pedidos SIN entrega efectiva también en Deudas (aunque no exista Collection todavía).

    # No setear entrega efectiva: solo crear Collection mínima para que aparezca en la tabla.

    try:

        q_missing = (

            Order.query

            .outerjoin(Collection, Collection.order_id == Order.id)

            .outerjoin(LogisticsStatus, LogisticsStatus.order_id == Order.id)

            .filter(Order.deleted_at.is_(None))

            .filter(Collection.id.is_(None))

            .filter(or_(LogisticsStatus.id.is_(None), LogisticsStatus.fecha_entrega_efectiva.is_(None)))

        )

        if not _has_global_access():

            uid = _effective_user_id()

            if uid is not None:

                q_missing = q_missing.filter(or_(Order.owner_user_id == uid, Order.owner_user_id.is_(None)))

        for o in q_missing.all():

            try:

                coll = Collection(order_id=o.id)

                uid2 = _effective_user_id()

                if uid2 is not None:

                    coll.owner_user_id = uid2

                try:

                    picked = None

                    lg = getattr(o, "logistics", None)

                    candidates = []

                    if getattr(o, "precio_final", None) is not None:

                        candidates.append(float(o.precio_final))

                    if lg is not None and getattr(lg, "precio", None) is not None:

                        candidates.append(float(lg.precio))

                    for v in candidates:

                        if v is not None and float(v) > 0:

                            picked = float(v)

                            break

                    if picked is None and candidates:

                        picked = float(candidates[0])

                    if picked is not None:

                        coll.monto = float(picked)

                except Exception:

                    pass

                db.session.add(coll)

            except Exception:

                continue

        try:

            db.session.commit()

        except Exception:

            db.session.rollback()

    except Exception:

        try:

            db.session.rollback()

        except Exception:

            pass



    q = (

        Collection.query

        .join(Order)

        .outerjoin(LogisticsStatus, LogisticsStatus.order_id == Order.id)

        .options(

            selectinload(Collection.order).selectinload(Order.client),

            selectinload(Collection.order).selectinload(Order.company),

            selectinload(Collection.order).selectinload(Order.logistics),

        )

    )

    q = q.filter(Order.deleted_at.is_(None))

    if not _has_global_access():

        uid = _effective_user_id()

        if uid is not None:

            q = q.filter(or_(Order.owner_user_id == uid, Order.owner_user_id.is_(None)))

    estados = request.args.getlist("status")

    # Filtro excluyente: si por algún motivo llegaron varios, usar solo el último.

    # (Puede ocurrir por URL guardada/restaurada o por duplicación de inputs hidden.)

    try:

        if estados and len(estados) > 1:

            estados = [estados[-1]]

    except Exception:

        pass

    tipos = request.args.getlist("tipo")  # FACTURA / REMITO

    desde = request.args.get("from")

    hasta = request.args.get("to")

    client_q = (request.args.get("client_q") or "").strip()

    company_q = (request.args.get("company_q") or "").strip()

    sort = (request.args.get("sort") or "").strip().lower()

    if sort not in ("entrega_efectiva", "vencimiento"):

        sort = ""

    direction = (request.args.get("direction") or "asc").strip().lower()

    if direction not in ("asc", "desc"):

        direction = "asc"

    now_date_local = date.today()

    try:

        if ZoneInfo:

            now_date_local = datetime.now(ZoneInfo("America/Argentina/Buenos_Aires")).date()

    except Exception:

        now_date_local = date.today()

    if estados:

        from sqlalchemy import or_, exists

        conds = []

        partial_pay_exists = (

            db.session.query(CollectionPayment.id)

            .filter(CollectionPayment.order_id == Order.id)

            .filter(CollectionPayment.kind != "DRAFT")

            .exists()

        )

        try:

            partial_pay_exists = _safe_filter_not_voided(

                db.session.query(CollectionPayment.id)

                .filter(CollectionPayment.order_id == Order.id)

                .filter(CollectionPayment.kind != "DRAFT")

            ).exists()

        except Exception:

            partial_pay_exists = (

                db.session.query(CollectionPayment.id)

                .filter(CollectionPayment.order_id == Order.id)

                .filter(CollectionPayment.kind != "DRAFT")

                .exists()

            )

        partial_draft_exists = (

            db.session.query(CollectionDraft.id)

            .filter(CollectionDraft.order_id == Order.id)

            .exists()

        )

        partial_exists = or_(partial_pay_exists, partial_draft_exists)



        # Estados de cobranza según la FUENTE ÚNICA DE VERDAD (backend/models.py).
        # El vencimiento efectivo ya viene derivado (fecha_pago_estimada, o
        # entrega efectiva + plazo de pago), así que el filtro coincide con el
        # badge que pinta el template. Antes no coincidía: había filas con
        # badge ATRASADO que este filtro nunca devolvía.
        cond_cob = condiciones_estado_cobranza(now_date_local)

        # Filtros excluyentes (no mezclar)

        if "COBRADO" in estados:

            conds.append(cond_cob.cobrado)



        if "EN_CAMINO" in estados:

            conds.append(cond_cob.en_camino)



        if "ATRASADO" in estados:

            conds.append(cond_cob.atrasado)



        if "PARCIAL" in estados:

            # Parcial = sin cobro + con pagos/borrador (puede estar vigente o vencido).
            # Es ORTOGONAL a los 4 estados, no se deriva del vencimiento.

            conds.append(and_(Collection.fecha_cobro_efectiva.is_(None), partial_exists))



        if "A_COBRAR" in estados:

            # A cobrar = sin cobro + entrega efectiva hoy/pasada + vencimiento NO vencido

            conds.append(and_(cond_cob.a_cobrar, ~partial_exists))

        if conds:

            q = q.filter(or_(*conds))

    if tipos:

        q = q.filter(Order.tipo_comprobante.in_(tipos))

    if client_q:

        q = q.join(Client, Order.client_id == Client.id)

        pat = f"%{client_q}%"

        q = q.filter(or_(Client.apellido.ilike(pat), Client.nombre.ilike(pat)))

    if company_q:

        q = q.join(Company, Order.company_id == Company.id)

        pat = f"%{company_q}%"

        q = q.filter(Company.nombre.ilike(pat))

    if desde:

        try:

            d = _parse_datetime_like(desde)

            if not d:

                raise ValueError("invalid_from")

            q = q.filter(Collection.fecha_pago_estimada >= d)

        except Exception:

            pass

    if hasta:

        try:

            h = _parse_datetime_like(hasta)

            if not h:

                raise ValueError("invalid_to")

            q = q.filter(Collection.fecha_pago_estimada <= h)

        except Exception:

            pass

    # Paginación

    try:

        page = int(request.args.get("page", 1) or 1)

    except Exception:

        page = 1

    if page < 1:

        page = 1

    try:

        per_page = int(request.args.get("per_page", 20) or 20)

    except Exception:

        per_page = 20

    if per_page not in (10, 20, 50):

        per_page = 20



    total_count = 0

    try:

        total_count = int(q.order_by(None).count() or 0)

    except Exception:

        total_count = 0



    sort_col = None

    sort_entrega_expr = func.coalesce(
        LogisticsStatus.fecha_entrega_efectiva,
        Collection.fecha_entrega_efectiva,
        LogisticsStatus.fecha_entrega_estimada,
    )

    if sort == "entrega_efectiva":

        sort_col = sort_entrega_expr

    elif sort == "vencimiento":

        # Ordenar por el vencimiento EFECTIVO de la FUENTE ÚNICA DE VERDAD.
        # Antes había dos ordenamientos distintos y ninguno era el de los
        # filtros: en SQL se usaba coalesce(fecha_pago_estimada, entrega) SIN
        # sumar el plazo de pago, y para el caso asc se reordenaba en Python
        # con una tercera copia de la derivación. Ahora SQL alcanza para las
        # dos direcciones, así que se borró el camino en Python.
        sort_col = vencimiento_efectivo_expr()

    if sort_col is not None:

        sort_dir = sort_col.desc() if direction == "desc" else sort_col.asc()

        q = q.order_by(sort_col.is_(None).asc(), sort_dir, Collection.id.desc())

    else:

        q = q.order_by(Collection.id.desc())

    try:

        items = q.limit(per_page).offset((page - 1) * per_page).all()

    except Exception:

        items = q.limit(per_page).all()



    partial_order_ids = set()

    try:

        order_ids = [int(getattr(getattr(c, "order", None), "id", None) or 0) for c in items]

        order_ids = [oid for oid in order_ids if oid > 0]

        if order_ids:

            try:

                paid_rows = (

                    db.session.query(CollectionPayment.order_id)

                    .filter(CollectionPayment.order_id.in_(order_ids))

                    .filter(CollectionPayment.kind != "DRAFT")

                    .group_by(CollectionPayment.order_id)

                    .all()

                )

                try:

                    paid_rows = (

                        _safe_filter_not_voided(

                            db.session.query(CollectionPayment.order_id)

                            .filter(CollectionPayment.order_id.in_(order_ids))

                            .filter(CollectionPayment.kind != "DRAFT")

                        )

                        .group_by(CollectionPayment.order_id)

                        .all()

                    )

                except Exception:

                    paid_rows = (

                        db.session.query(CollectionPayment.order_id)

                        .filter(CollectionPayment.order_id.in_(order_ids))

                        .filter(CollectionPayment.kind != "DRAFT")

                        .group_by(CollectionPayment.order_id)

                        .all()

                    )

                for (oid,) in paid_rows:

                    try:

                        partial_order_ids.add(int(oid))

                    except Exception:

                        pass

            except Exception:

                pass

            try:

                draft_rows = (

                    db.session.query(CollectionDraft.order_id)

                    .filter(CollectionDraft.order_id.isnot(None))

                    .filter(CollectionDraft.order_id.in_(order_ids))

                    .group_by(CollectionDraft.order_id)

                    .all()

                )

                for (oid,) in draft_rows:

                    try:

                        partial_order_ids.add(int(oid))

                    except Exception:

                        pass

            except Exception:

                pass

    except Exception:

        partial_order_ids = set()



    # Orden visual por defecto: arriba ATRASADO y PARCIAL, abajo COBRADO.

    if not sort:

        try:

            def _sort_key(c):

                oid = int(getattr(getattr(c, "order", None), "id", None) or 0)

                is_cobrado = bool(getattr(c, "fecha_cobro_efectiva", None))

                # FUENTE ÚNICA DE VERDAD, con el mismo "hoy" que el resto del
                # request (antes esto salía de Collection.status, que comparaba
                # contra datetime.utcnow() y no conocía el bucket A_COBRAR).
                is_overdue = bool(estado_cobranza_de(c, now_date_local) == "ATRASADO")

                is_partial = bool(oid and (oid in partial_order_ids))

                if is_cobrado:

                    grp = 2

                elif is_overdue or is_partial:

                    grp = 0

                else:

                    grp = 1

                return (grp, -int(getattr(c, "id", 0) or 0))



            items = sorted(items, key=_sort_key)

        except Exception:

            pass

    try:

        sort_params_base = request.args.to_dict(flat=False)

        def _sort_url(sort_key: str) -> str:

            params = dict(sort_params_base)

            next_direction = "asc"

            if sort == sort_key:

                next_direction = "desc" if direction == "asc" else "asc"

            params["sort"] = [sort_key]

            params["direction"] = [next_direction]

            params["page"] = ["1"]

            return url_for("main.deudas_pendientes") + "?" + urlencode(params, doseq=True)

        sort_entrega_url = _sort_url("entrega_efectiva")

        sort_vencimiento_url = _sort_url("vencimiento")

    except Exception:

        sort_entrega_url = url_for("main.deudas_pendientes") + "?sort=entrega_efectiva&direction=asc&page=1"

        sort_vencimiento_url = url_for("main.deudas_pendientes") + "?sort=vencimiento&direction=asc&page=1"



    return render_template(

        "cobranzas.html",

        active="deudas",

        items=items,

        now_date=now_date_local,

        # El template NO deriva más el vencimiento ni el estado: consume este
        # helper, que es la FUENTE ÚNICA DE VERDAD (backend/models.py) evaluada
        # con el MISMO "hoy" que usaron los filtros SQL de arriba. El
        # vencimiento sale de la property Collection.vencimiento_efectivo.
        estado_cobranza=(lambda coll: estado_cobranza_de(coll, now_date_local)),

        partial_order_ids=partial_order_ids,

        timedelta=timedelta,

        page=page,

        per_page=per_page,

        current_sort=sort,

        current_direction=direction,

        sort_entrega_url=sort_entrega_url,

        sort_vencimiento_url=sort_vencimiento_url,

        total_count=total_count,

        prev_url=(

            url_for("main.deudas_pendientes")

            + "?"

            + urlencode({**request.args.to_dict(flat=False), "page": page - 1, "per_page": per_page}, doseq=True)

            if page > 1

            else None

        ),

        next_url=(

            url_for("main.deudas_pendientes")

            + "?"

            + urlencode({**request.args.to_dict(flat=False), "page": page + 1, "per_page": per_page}, doseq=True)

            if (page * per_page) < int(total_count or 0)

            else None

        ),

    )





@bp.get("/cobranzas/nueva")

def nueva_cobranza():

    # Soporte para reabrir borradores

    order_id = request.args.get("order_id", type=int)

    draft_id = request.args.get("draft_id", type=int)

    draft_data = None

    if draft_id:

        try:

            d = CollectionDraft.query.get(int(draft_id))

        except Exception:

            d = None

        if d is not None:

            _require_owner(d)

            if (d.notes or "").strip():

                try:

                    draft_data = json.loads(d.notes)

                except Exception:

                    draft_data = None

            if isinstance(draft_data, dict):

                draft_data["draft_id"] = int(getattr(d, "id", None) or 0)

                try:

                    if "client_id" not in draft_data:

                        draft_data["client_id"] = int(getattr(d, "client_id", None) or 0)

                    if "company_id" not in draft_data:

                        draft_data["company_id"] = int(getattr(d, "company_id", None) or 0)

                    if "order_id" not in draft_data:

                        draft_data["order_id"] = int(getattr(d, "order_id", None) or 0) or None

                except Exception:

                    pass

    if order_id:

        try:

            o = Order.query.options(selectinload(Order.client), selectinload(Order.company)).get(order_id)

            if o is not None:

                _require_owner(o)



                if draft_data is None:

                    try:

                        d2 = CollectionDraft.query.filter_by(order_id=order_id).order_by(CollectionDraft.updated_at.desc()).first()

                    except Exception:

                        d2 = None

                    if d2 is not None:

                        try:

                            _require_owner(d2)

                        except Exception:

                            d2 = None

                    if d2 is not None and (d2.notes or "").strip():

                        try:

                            draft_data = json.loads(d2.notes)

                        except Exception:

                            draft_data = None

                        if isinstance(draft_data, dict):

                            try:

                                draft_data["draft_id"] = int(getattr(d2, "id", None) or 0)

                            except Exception:

                                pass



                dr = (

                    CollectionPayment.query

                    .filter_by(order_id=order_id)

                    .filter(CollectionPayment.kind == "DRAFT")

                    .order_by(CollectionPayment.created_at.desc())

                    .first()

                )

                if draft_data is None and dr and (dr.notes or "").strip():

                    try:

                        draft_data = json.loads(dr.notes)

                    except Exception:

                        draft_data = None

                # Si no hay DRAFT, precargar con lo YA guardado en el pedido/cobranza.

                if draft_data is None or (isinstance(draft_data, dict) and set(draft_data.keys()) <= {"client_id", "company_id", "order_id"}):

                    try:

                        coll = Collection.query.filter_by(order_id=o.id).first()

                    except Exception:

                        coll = None



                    def _to_date_iso(dt):

                        try:

                            if not dt:

                                return ""

                            try:

                                return dt.date().isoformat()

                            except Exception:

                                return dt.isoformat()

                        except Exception:

                            return ""



                    # Base pedido

                    order_created_iso = ""

                    try:

                        lg = getattr(o, "logistics", None)

                        if lg is not None and getattr(lg, "fecha_compra", None):

                            order_created_iso = _to_date_iso(lg.fecha_compra)

                    except Exception:

                        order_created_iso = ""

                    if not order_created_iso:

                        try:

                            order_created_iso = _to_date_iso(getattr(o, "created_at", None))

                        except Exception:

                            order_created_iso = ""



                    order_entrega_iso = ""

                    try:

                        if coll is not None and getattr(coll, "fecha_entrega_efectiva", None):

                            order_entrega_iso = _to_date_iso(coll.fecha_entrega_efectiva)

                    except Exception:

                        order_entrega_iso = ""

                    if not order_entrega_iso:

                        try:

                            lg = getattr(o, "logistics", None)

                            if lg is not None and getattr(lg, "fecha_entrega_efectiva", None):

                                order_entrega_iso = _to_date_iso(lg.fecha_entrega_efectiva)

                        except Exception:

                            order_entrega_iso = ""



                    order_venc_iso = ""

                    try:

                        if coll is not None and getattr(coll, "fecha_pago_estimada", None):

                            order_venc_iso = _to_date_iso(coll.fecha_pago_estimada)

                    except Exception:

                        order_venc_iso = ""



                    # Monto: preferir collection.monto, luego order.precio_final, luego logistics.precio

                    order_monto = ""

                    try:

                        candidates = []

                        if coll is not None and getattr(coll, "monto", None) is not None:

                            candidates.append(float(coll.monto))

                        if getattr(o, "precio_final", None) is not None:

                            candidates.append(float(o.precio_final))

                        lg = getattr(o, "logistics", None)

                        if lg is not None and getattr(lg, "precio", None) is not None:

                            candidates.append(float(lg.precio))

                        picked = None

                        for v in candidates:

                            if v is not None and v > 0:

                                picked = v

                                break

                        if picked is None and candidates:

                            picked = candidates[0]

                        if picked is not None:

                            order_monto = str(picked)

                    except Exception:

                        order_monto = ""



                    # Notas

                    order_notes = ""

                    try:

                        parts = []

                        if getattr(o, "nota", None):

                            parts.append(str(o.nota))

                        if getattr(o, "descripcion", None):

                            parts.append(str(o.descripcion))

                        order_notes = "\n".join([p for p in parts if p is not None])

                    except Exception:

                        order_notes = ""



                    # Pagos/NC/retenciones

                    nc_total = 0.0

                    ret_total = 0.0

                    nc_items = []

                    ret_items = []

                    pm = {}

                    rows = []

                    try:

                        mp = {

                            "EFECTIVO": "pm_efectivo",

                            "TRANSFERENCIA": "pm_transferencia",

                            "E-CHEQ": "pm_echeq",

                            "CHEQUE_TERCEROS": "pm_cheque_terceros",

                            "CHEQUE_PROPIO": "pm_cheque_propio",

                        }

                        payments = (

                            CollectionPayment.query

                            .filter_by(order_id=o.id)

                            .filter(CollectionPayment.kind != "DRAFT")

                            .order_by(CollectionPayment.created_at.asc())

                            .all()

                        )

                        try:

                            payments = (

                                _safe_filter_not_voided(

                                    CollectionPayment.query

                                    .filter_by(order_id=o.id)

                                    .filter(CollectionPayment.kind != "DRAFT")

                                )

                                .order_by(CollectionPayment.created_at.asc())

                                .all()

                            )

                        except Exception:

                            payments = (

                                CollectionPayment.query

                                .filter_by(order_id=o.id)

                                .filter(CollectionPayment.kind != "DRAFT")

                                .order_by(CollectionPayment.created_at.asc())

                                .all()

                            )

                        for p in payments:

                            kind = (getattr(p, "kind", "") or "").strip().upper()

                            method = (getattr(p, "method", None) or "").strip().upper()

                            amt = float(getattr(p, "amount", 0) or 0)

                            if kind == "CREDIT_NOTE" or method == "NC":

                                nc_total += abs(amt)

                                concept = ""

                                try:

                                    notes_txt = (getattr(p, "notes", None) or "").strip()

                                    prefix = "NOTA DE CRÉDITO - "

                                    if notes_txt.upper().startswith(prefix):

                                        concept = notes_txt[len(prefix):].strip()

                                except Exception:

                                    concept = ""

                                nc_items.append({"amount": str(abs(amt)), "concept": concept})

                                continue

                            if method == "RETENCION":

                                ret_total += abs(amt)

                                concept = ""

                                try:

                                    notes_txt = (getattr(p, "notes", None) or "").strip()

                                    prefix = "RETENCIONES - "

                                    if notes_txt.upper().startswith(prefix):

                                        concept = notes_txt[len(prefix):].strip()

                                except Exception:

                                    concept = ""

                                ret_items.append({"amount": str(abs(amt)), "concept": concept})

                                continue

                            prefix = mp.get(method)

                            if not prefix:

                                continue

                            dd = getattr(p, "due_date", None) or getattr(p, "created_at", None)

                            dd_iso = _to_date_iso(dd)

                            if prefix not in pm or not (pm.get(prefix) or {}).get("amount"):

                                pm[prefix] = {"amount": str(abs(amt)), "date": dd_iso}

                            else:

                                rows.append({"prefix": prefix, "amount": str(abs(amt)), "date": dd_iso})

                    except Exception:

                        nc_total = nc_total

                        ret_total = ret_total



                    draft_data = {

                        "client_id": o.client_id,

                        "company_id": o.company_id,

                        "order_id": o.id,

                        "order_created_at": order_created_iso,

                        "order_fecha_entrega_efectiva": order_entrega_iso,

                        "order_fecha_pago_estimada": order_venc_iso,

                        "order_monto": order_monto,

                        "order_credit_note": ("" if abs(nc_total) <= 0.009 else str(nc_total)),

                        "order_retenciones": ("" if abs(ret_total) <= 0.009 else str(ret_total)),

                        "nc_items": nc_items,

                        "ret_items": ret_items,

                        "order_notes": order_notes,

                        "pm": pm,

                        "rows": rows,

                    }

        except Exception:

            draft_data = None

    return render_template(

        "nueva_cobranza.html",

        active="nueva_cobranza",

        today=date.today().isoformat(),

        clientes=(

            (Client.query if _has_global_access() else Client.query.filter(Client.owner_user_id == _effective_user_id()))

            .order_by(Client.apellido, Client.nombre)

            .all()

        ),

        draft_data=draft_data,

    )





@bp.get("/cobranzas/pendientes")

def cobranzas_pendientes():

    q = CollectionDraft.query.order_by(CollectionDraft.updated_at.desc())

    if not _has_global_access():

        uid = _effective_user_id()

        if uid is not None:

            q = q.filter(CollectionDraft.owner_user_id == uid)

    rows = []

    for d in q.all():

        o = None

        try:

            if getattr(d, "order_id", None):

                o = Order.query.options(selectinload(Order.client), selectinload(Order.company)).get(int(d.order_id))

        except Exception:

            o = None

        try:

            if o is not None:

                _require_owner(o)

        except Exception:

            o = None

        try:

            if o is not None:

                client_label = getattr(getattr(o, "client", None), "apellido", None) or "-"

            else:

                cobj = Client.query.get(getattr(d, "client_id", None)) if getattr(d, "client_id", None) else None

                client_label = getattr(cobj, "apellido", None) or "-"

        except Exception:

            client_label = "-"

        try:

            if o is not None:

                company_label = getattr(getattr(o, "company", None), "nombre", None) or "-"

            else:

                compobj = Company.query.get(getattr(d, "company_id", None)) if getattr(d, "company_id", None) else None

                company_label = getattr(compobj, "nombre", None) or "-"

        except Exception:

            company_label = "-"

        try:

            created = o.created_at.date().isoformat() if (o is not None and getattr(o, "created_at", None)) else ""

        except Exception:

            created = ""

        try:

            saved = d.updated_at.date().isoformat() if getattr(d, "updated_at", None) else ""

        except Exception:

            saved = ""

        rows.append({

            "draft_id": d.id,

            "order_id": (o.id if o is not None else None),

            "client": client_label,

            "company": company_label,

            "order_date": created,

            "saved_at": saved,

        })

    return render_template("cobranzas_pendientes.html", active="nueva_cobranza", items=rows)





@bp.get("/api/cobranzas/pedidos_pendientes")

def api_cobranzas_pedidos_pendientes():

    client_id = request.args.get("client_id", type=int)

    company_id = request.args.get("company_id", type=int)

    if not client_id or not company_id:

        return jsonify([])



    client = Client.query.get_or_404(client_id)

    _require_owner(client)



    q = (

        Order.query

        .filter(Order.client_id == client_id)

        .filter(Order.company_id == company_id)

        .options(

            selectinload(Order.logistics),

            selectinload(Order.collection),

            selectinload(Order.company),

            selectinload(Order.collection_payments),

        )

        .order_by(Order.created_at.asc())

    )

    q = q.filter(Order.deleted_at.is_(None))



    if not _has_global_access():

        uid = _effective_user_id()

        if uid is not None:

            # Compatibilidad: pedidos legacy pueden tener owner_user_id NULL.

            # Como ya validamos _require_owner(client), permitirlos para este cliente.

            q = q.filter((Order.owner_user_id == uid) | (Order.owner_user_id.is_(None)))



    def _pick_monto(order: Order):

        """Devuelve (monto_val, has_monto).



        has_monto=True si existe algún monto > 0 en collection/order/logistics.

        Si no hay monto todavía, devolvemos 0.0 y has_monto=False para NO filtrar el pedido.

        """

        col = getattr(order, "collection", None)

        try:

            candidates = []

            if col is not None and getattr(col, "monto", None) is not None:

                candidates.append(float(col.monto))

            if getattr(order, "precio_final", None) is not None:

                candidates.append(float(order.precio_final))

            lg = getattr(order, "logistics", None)

            if lg is not None and getattr(lg, "precio", None) is not None:

                candidates.append(float(lg.precio))

            for v in candidates:

                if v is not None and v > 0:

                    return float(v), True

            # Si hay candidatos pero ninguno > 0, tratarlos como "sin monto".

            return 0.0, False

        except Exception:

            return 0.0, False



    def _iso_date(dt):

        try:

            return dt.date().isoformat() if dt else None

        except Exception:

            return None



    def _calc_venc(order: Order):

        col = getattr(order, "collection", None)

        if col is not None and getattr(col, "fecha_pago_estimada", None):

            return _iso_date(col.fecha_pago_estimada)

        # Vencimiento: entrega efectiva (si existe) o fecha pedido + plazo

        base_dt = None

        lg = getattr(order, "logistics", None)

        if lg is not None and getattr(lg, "fecha_entrega_efectiva", None):

            base_dt = lg.fecha_entrega_efectiva

        if base_dt is None:

            # Fecha base del pedido: preferir fecha_compra (si existe) y como fallback created_at

            if lg is not None and getattr(lg, "fecha_compra", None):

                base_dt = lg.fecha_compra

            else:

                base_dt = getattr(order, "created_at", None)

        plazo = getattr(order, "plazo_pago_dias", None)

        if plazo is None:

            try:

                comp = getattr(order, "company", None)

                plazo = getattr(comp, "plazo_pago_promedio_dias", None) if comp is not None else None

            except Exception:

                plazo = None

        try:

            plazo_i = int(plazo or 0)

        except Exception:

            plazo_i = 0

        try:

            if base_dt is None:

                return None

            return _iso_date(base_dt + timedelta(days=max(0, plazo_i)))

        except Exception:

            return None



    orders = q.all()



    # Precalcular datos por pedido, asegurando que pagos/NC sean SOLO de ese order_id.

    all_rows = []

    for o in orders:

        col = getattr(o, "collection", None)

        lg = getattr(o, "logistics", None)

        monto_val, has_monto = _pick_monto(o)



        total_paid = 0.0

        total_credit = 0.0

        try:

            for p in (

                CollectionPayment.query

                .filter_by(order_id=o.id)

                .filter(CollectionPayment.kind != "DRAFT")

                .all()

            ):

                kind = (getattr(p, "kind", "") or "").strip().upper()

                amt = float(getattr(p, "amount", 0) or 0)

                if kind == "CREDIT_NOTE":

                    total_credit += abs(amt)

                elif kind == "PAYMENT":

                    total_paid += abs(amt)

        except Exception:

            total_paid = 0.0

            total_credit = 0.0



        remaining = float(monto_val or 0.0) - float(total_paid or 0.0) - float(total_credit or 0.0)

        cobrado = False

        try:

            cobrado = (col is not None and getattr(col, "fecha_cobro_efectiva", None) is not None)

        except Exception:

            cobrado = False



        entrega_eff = None

        try:

            entrega_eff = _iso_date(getattr(lg, "fecha_entrega_efectiva", None)) if lg is not None else None

        except Exception:

            entrega_eff = None

        if not entrega_eff:

            try:

                entrega_eff = _iso_date(getattr(col, "fecha_entrega_efectiva", None)) if col is not None else None

            except Exception:

                entrega_eff = None



        all_rows.append({

            "order_id": o.id,

            "order_date": (

                _iso_date(getattr(lg, "fecha_compra", None))

                if lg is not None and getattr(lg, "fecha_compra", None)

                else (o.created_at.date().isoformat() if o.created_at else None)

            ),

            "fecha_entrega_efectiva": entrega_eff,

            "fecha_pago_estimada": _calc_venc(o),

            "monto": float(monto_val or 0.0),

            "nota": o.nota,

            "descripcion": o.descripcion,

            "plazo_pago_dias": getattr(o, "plazo_pago_dias", None),

            "total_paid": float(total_paid or 0.0),

            "total_credit": float(total_credit or 0.0),

            "remaining": float(remaining or 0.0),

            "_cobrado": cobrado,

            "_has_monto": bool(has_monto),

        })



    company_diff_total_calc = 0.0

    try:

        for r in all_rows:

            if r.get("_cobrado") and abs(float(r.get("remaining") or 0.0)) >= 1000.0:

                company_diff_total_calc += float(r.get("remaining") or 0.0)

    except Exception:

        company_diff_total_calc = 0.0



    company_balance_adjustment = 0.0

    try:

        uid = _balance_owner_user_id()

        bal = (

            ClientCompanyBalance.query

            .filter_by(owner_user_id=uid, client_id=client_id, company_id=company_id)

            .first()

        )

        if bal is not None and getattr(bal, "balance_adjustment", None) is not None:

            company_balance_adjustment = float(bal.balance_adjustment or 0.0)

    except Exception:

        company_balance_adjustment = 0.0



    company_diff_total = float(company_diff_total_calc or 0.0) + float(company_balance_adjustment or 0.0)



    # Devolver solo pendientes

    res = []

    for r in all_rows:

        rem = float(r.get("remaining") or 0.0)

        cobrado = bool(r.get("_cobrado"))

        has_monto = bool(r.get("_has_monto"))

        if cobrado:

            continue

        # Si el pedido no tiene monto aún, igual debe aparecer para poder asignarlo en Nueva cobranza.

        if has_monto and rem <= 0.009:

            continue

        rr = {k: v for k, v in r.items() if not k.startswith("_")}

        rr["company_diff_total"] = float(company_diff_total or 0.0)

        rr["company_diff_total_calc"] = float(company_diff_total_calc or 0.0)

        rr["company_balance_adjustment"] = float(company_balance_adjustment or 0.0)

        res.append(rr)



    return jsonify(res)





@bp.post("/cobranzas/nueva")

def nueva_cobranza_create():

    client_id = request.form.get("client_id", type=int)

    company_id = request.form.get("company_id", type=int)

    order_id = request.form.get("order_id", type=int) or request.form.get("order_id_override", type=int)

    draft_id = request.form.get("draft_id", type=int)

    if not client_id or not company_id:

        return redirect(url_for("main.nueva_cobranza"))



    # Borrador: no debe alterar pagos/NC existentes; sólo guardar snapshot para retomar.

    try:

        is_draft = (request.form.get("draft") == "1")

    except Exception:

        is_draft = False



    def _parse_amount_inline(s: str) -> float:

        try:

            s = (s or "").strip()

            if not s:

                return 0.0

            s = s.replace(" ", "")

            if "," in s and "." in s:

                s = s.replace(".", "").replace(",", ".")

            elif "," in s:

                s = s.replace(",", ".")

            val = float(s)

            if val < 0:

                return 0.0

            return float(val)

        except Exception:

            return 0.0



    raw_nc = (request.form.get("order_credit_note") or "").strip()

    raw_ret = (request.form.get("order_retenciones") or "").strip()

    nc_amounts = request.form.getlist("nc_amount")

    nc_concepts = request.form.getlist("nc_concept")

    ret_amounts = request.form.getlist("ret_amount")

    ret_concepts = request.form.getlist("ret_concept")



    def _collect_items(amounts, concepts, legacy_raw=""):

        items = []

        total = 0.0

        row_count = max(len(amounts or []), len(concepts or []))

        for i in range(row_count):

            amount_raw = (amounts[i] if i < len(amounts) else "").strip()

            concept = (concepts[i] if i < len(concepts) else "").strip()

            if not amount_raw and not concept:

                continue

            amount_val = _parse_amount_inline(amount_raw)

            if amount_val > 0:

                total += float(amount_val)

            items.append({

                "amount_raw": amount_raw,

                "amount": float(amount_val or 0.0),

                "concept": concept,

            })



        if not items and (legacy_raw or "").strip():

            legacy_val = _parse_amount_inline(legacy_raw)

            if legacy_val > 0:

                total += float(legacy_val)

            items.append({

                "amount_raw": (legacy_raw or "").strip(),

                "amount": float(legacy_val or 0.0),

                "concept": "",

            })



        return items, float(total)



    nc_items, nc_total_form = _collect_items(nc_amounts, nc_concepts, raw_nc)

    ret_items, ret_total_form = _collect_items(ret_amounts, ret_concepts, raw_ret)



    if is_draft:

        saved_draft = None

        try:

            if order_id:

                o = Order.query.get_or_404(order_id)

                _require_owner(o)

                if o.client_id != client_id or o.company_id != company_id:

                    abort(400)



            snap = {

                "client_id": client_id,

                "company_id": company_id,

                "order_id": (order_id or None),

                "order_created_at": (request.form.get("order_created_at") or "").strip(),

                "order_fecha_entrega_efectiva": (request.form.get("order_fecha_entrega_efectiva") or "").strip(),

                "order_fecha_pago_estimada": (request.form.get("order_fecha_pago_estimada") or "").strip(),

                "order_monto": (request.form.get("order_monto") or "").strip(),

                "order_credit_note": ("" if abs(nc_total_form) <= 0.009 else str(nc_total_form)),

                "order_retenciones": ("" if abs(ret_total_form) <= 0.009 else str(ret_total_form)),

                "nc_items": [

                    {

                        "amount": str(it.get("amount_raw") or ""),

                        "concept": str(it.get("concept") or ""),

                    }

                    for it in (nc_items or [])

                    if (str(it.get("amount_raw") or "").strip() or str(it.get("concept") or "").strip())

                ],

                "ret_items": [

                    {

                        "amount": str(it.get("amount_raw") or ""),

                        "concept": str(it.get("concept") or ""),

                    }

                    for it in (ret_items or [])

                    if (str(it.get("amount_raw") or "").strip() or str(it.get("concept") or "").strip())

                ],

                "order_notes": (request.form.get("order_notes") or "").strip(),

                "pm": {},

                "rows": [],

            }



            try:

                for prefix in (

                    "pm_efectivo",

                    "pm_transferencia",

                    "pm_echeq",

                    "pm_cheque_terceros",

                    "pm_cheque_propio",

                ):

                    snap["pm"][prefix] = {

                        "amount": (request.form.get(f"{prefix}_amount") or "").strip(),

                        "date": (request.form.get(f"{prefix}_date") or "").strip(),

                    }

            except Exception:

                pass



            try:

                kinds = request.form.getlist("row_kind")

                methods = request.form.getlist("row_method")

                amounts = request.form.getlist("row_amount")

                dues = request.form.getlist("row_due_date")

                row_count = max(len(kinds), len(methods), len(amounts), len(dues)) if any([kinds, methods, amounts, dues]) else 0

                for i in range(row_count):

                    method = (methods[i] if i < len(methods) else "").strip()

                    raw_amount = (amounts[i] if i < len(amounts) else "").strip()

                    raw_due = (dues[i] if i < len(dues) else "").strip()

                    if not method and not raw_amount and not raw_due:

                        continue

                    prefix = None

                    try:

                        mp = {

                            "EFECTIVO": "pm_efectivo",

                            "TRANSFERENCIA": "pm_transferencia",

                            "E-CHEQ": "pm_echeq",

                            "CHEQUE_TERCEROS": "pm_cheque_terceros",

                            "CHEQUE_PROPIO": "pm_cheque_propio",

                        }

                        prefix = mp.get(method)

                    except Exception:

                        prefix = None

                    if not prefix:

                        continue

                    snap["rows"].append({"prefix": prefix, "amount": raw_amount, "date": raw_due})

            except Exception:

                pass



            drow = None

            if draft_id:

                try:

                    drow = CollectionDraft.query.get(int(draft_id))

                except Exception:

                    drow = None

                if drow is not None:

                    _require_owner(drow)



            if drow is None:

                uid = (None if _has_global_access() else _effective_user_id())

                qd = (

                    CollectionDraft.query

                    .filter_by(owner_user_id=uid)

                    .filter(CollectionDraft.client_id == client_id)

                    .filter(CollectionDraft.company_id == company_id)

                )

                if order_id:

                    qd = qd.filter(CollectionDraft.order_id == order_id)

                else:

                    qd = qd.filter(CollectionDraft.order_id.is_(None))

                drow = qd.order_by(CollectionDraft.updated_at.desc()).first()



            if drow is None:

                drow = CollectionDraft(

                    owner_user_id=(None if _has_global_access() else _effective_user_id()),

                    client_id=client_id,

                    company_id=company_id,

                    order_id=(order_id or None),

                )

                db.session.add(drow)

            try:

                drow.order_id = (order_id or None)

            except Exception:

                pass

            try:

                drow.notes = json.dumps(snap, ensure_ascii=False)

            except Exception:

                pass

            saved_draft = drow

            db.session.commit()

        except Exception:

            try:

                db.session.rollback()

            except Exception:

                pass



        try:

            if request.headers.get("X-Requested-With") == "XMLHttpRequest":

                return jsonify({

                    "ok": True,

                    "order_id": (int(order_id) if order_id else None),

                    "draft_id": (int(getattr(saved_draft, "id", None) or 0) if saved_draft is not None else None),

                })

        except Exception:

            pass

        return redirect(url_for("main.cobranzas_pendientes"))



    o = None

    coll = None

    if order_id:

        o = Order.query.get_or_404(order_id)

        _require_owner(o)

        if o.client_id != client_id or o.company_id != company_id:

            abort(400)

        coll = Collection.query.filter_by(order_id=order_id).first()

        if not coll:

            coll = Collection(order_id=order_id)

            uid = _effective_user_id()

            if uid is not None:

                coll.owner_user_id = uid

            db.session.add(coll)

    else:

        if not is_draft:

            client = Client.query.get_or_404(client_id)

            _require_owner(client)

            company = Company.query.get_or_404(company_id)

            o = Order(

                client=client,

                company=company,

                tipo_comprobante="FACTURA",

            )

            uid = _effective_user_id()

            if uid is not None:

                o.owner_user_id = uid

            db.session.add(o)

            db.session.flush()

            order_id = o.id

            coll = Collection.query.filter_by(order_id=order_id).first()

            if not coll:

                coll = Collection(order_id=order_id)

                uid = _effective_user_id()

                if uid is not None:

                    coll.owner_user_id = uid

                db.session.add(coll)



    # Edición manual de datos del pedido (opcional)

    raw_created = (request.form.get("order_created_at") or "").strip()

    raw_entrega = (request.form.get("order_fecha_entrega_efectiva") or "").strip()

    raw_venc = (request.form.get("order_fecha_pago_estimada") or "").strip()

    raw_monto = (request.form.get("order_monto") or "").strip()

    raw_nc = (request.form.get("order_credit_note") or "").strip()

    raw_ret = (request.form.get("order_retenciones") or "").strip()

    raw_notes = (request.form.get("order_notes") or "").strip()



    kinds = request.form.getlist("row_kind")

    methods = request.form.getlist("row_method")

    amounts = request.form.getlist("row_amount")

    dues = request.form.getlist("row_due_date")

    notes = request.form.getlist("row_notes")



    cloud_name = os.getenv("CLOUDINARY_CLOUD_NAME")

    upload_preset = os.getenv("CLOUDINARY_UPLOAD_PRESET")

    files = request.files.getlist("row_attachment")



    # Si llegaron archivos y no está configurado Cloudinary, no fallar silenciosamente

    try:

        any_upload = False

        try:

            any_upload = any(f and getattr(f, "filename", "") for f in (files or []))

        except Exception:

            any_upload = False

        if not any_upload:

            # también considerar la variante de inputs fijos pm_*_attachment

            try:

                for k in (

                    "pm_transferencia_attachment",

                    "pm_echeq_attachment",

                    "pm_cheque_terceros_attachment",

                    "pm_cheque_propio_attachment",

                ):

                    f2 = request.files.get(k)

                    if f2 and getattr(f2, "filename", ""):

                        any_upload = True

                        break

            except Exception:

                pass

        if any_upload and (not cloud_name or not upload_preset):

            abort(500, "Falta configurar Cloudinary (CLOUDINARY_CLOUD_NAME y CLOUDINARY_UPLOAD_PRESET) para subir adjuntos.")

    except Exception:

        pass



    if order_id:

        try:

            CollectionPayment.query.filter_by(order_id=order_id).filter(CollectionPayment.kind == "DRAFT").delete()

        except Exception:

            try:

                db.session.rollback()

            except Exception:

                pass



    max_due = None



    def _already_inserted_recent(kind: str, method: str, amount_val: float, due_dt, notes_val: str) -> bool:

        try:

            since = datetime.utcnow() - timedelta(seconds=15)

            q = (

                CollectionPayment.query

                .filter(CollectionPayment.order_id == order_id)

                .filter(CollectionPayment.kind == kind)

                .filter(CollectionPayment.method.is_(None) if method is None else (CollectionPayment.method == method))

                .filter(CollectionPayment.amount == amount_val)

                .filter(CollectionPayment.due_date.is_(None) if due_dt is None else (CollectionPayment.due_date == due_dt))

                .filter(CollectionPayment.notes.is_(None) if not notes_val else (CollectionPayment.notes == notes_val))

                .filter(CollectionPayment.created_at.isnot(None))

                .filter(CollectionPayment.created_at >= since)

            )

            try:

                q = _safe_filter_not_voided(q)

            except Exception:

                try:

                    q = q.filter(CollectionPayment.voided_at.is_(None))

                except Exception:

                    pass

            return q.first() is not None

        except Exception:

            return False



    def _parse_amount(s: str) -> float:

        s = (s or "").strip()

        if not s:

            return 0.0

        s = s.replace(" ", "")

        # Soportar formatos: 1234.56 / 1234,56 / 1.234,56

        if "," in s and "." in s:

            # asume '.' miles y ',' decimal

            s = s.replace(".", "").replace(",", ".")

        elif "," in s:

            s = s.replace(",", ".")

        return float(s)



    try:

        if raw_created:

            # viene como YYYY-MM-DD por flatpickr

            dt = _parse_datetime_like(raw_created)

            if dt:

                # Guardar como fecha real del pedido (fecha_compra) y NO tocar created_at

                try:

                    from backend.models import LogisticsStatus

                except Exception:

                    LogisticsStatus = None

                try:

                    lg = getattr(o, "logistics", None)

                    if lg is None and LogisticsStatus is not None:

                        lg = LogisticsStatus(order_id=o.id)

                        o.logistics = lg

                        db.session.add(lg)

                    if lg is not None:

                        lg.fecha_compra = dt

                except Exception:

                    pass

    except Exception:

        pass

    try:

        if raw_entrega != "":

            coll.fecha_entrega_efectiva = _parse_datetime_like(raw_entrega) if raw_entrega else None

    except Exception:

        pass

    try:

        if raw_venc != "":

            coll.fecha_pago_estimada = _parse_datetime_like(raw_venc) if raw_venc else None

    except Exception:

        pass

    try:

        if raw_monto != "":

            mval = _parse_amount(raw_monto)

            if mval is not None and mval >= 0:

                coll.monto = mval

                o.precio_final = mval

                if getattr(o, "logistics", None) is not None:

                    try:

                        o.logistics.precio = mval

                    except Exception:

                        pass

    except Exception:

        pass

    try:

        if raw_notes != "":

            # Guardar primer línea como nota y el resto como descripción

            parts = [p for p in raw_notes.split("\n")]

            first = (parts[0] if parts else "").strip()

            rest = "\n".join(parts[1:]).strip() if len(parts) > 1 else ""

            if first:

                o.nota = first

            if rest or (raw_notes and not first):

                o.descripcion = rest or raw_notes

    except Exception:

        pass



    def _upload_file_to_cloudinary(f):

        if not f or not getattr(f, "filename", ""):

            return None

        if not cloud_name or not upload_preset:

            return None

        try:

            r = requests.post(

                f"https://api.cloudinary.com/v1_1/{cloud_name}/auto/upload",

                data={"upload_preset": upload_preset, "resource_type": "auto"},

                files={"file": (f.filename, f.stream, f.mimetype)},

                timeout=30,

            )

            if r.ok:

                return r.json().get("secure_url") or r.json().get("url")

        except Exception:

            return None

        return None



    # Pagos extras enviados como filas row_* (agregados por método). Se procesan junto con pm_*.

    if (not is_draft) and any([kinds, methods, amounts, dues, notes, files]):

        row_count = max(len(kinds), len(methods), len(amounts), len(dues), len(notes), len(files))

        for i in range(row_count):

            kind = (kinds[i] if i < len(kinds) else "").strip().upper() or "PAYMENT"

            method = (methods[i] if i < len(methods) else "").strip() or None

            raw_amount = (amounts[i] if i < len(amounts) else "").strip()

            raw_due = (dues[i] if i < len(dues) else "").strip()

            row_note = (notes[i] if i < len(notes) else "").strip() or None

            f = files[i] if i < len(files) else None



            try:

                amount_val = _parse_amount(raw_amount)

            except Exception:

                amount_val = 0.0

            if not amount_val and not method and not raw_due and not row_note and (not f or not getattr(f, "filename", "")):

                continue



            # No crear pagos con monto 0 (pueden venir por autofill/inputs habilitados sin querer)

            try:

                if float(amount_val or 0.0) <= 0.009:

                    continue

            except Exception:

                continue



            due_dt = None

            if raw_due:

                try:

                    due_dt = _parse_datetime_like(raw_due)

                except Exception:

                    due_dt = None



            attach_url = _upload_file_to_cloudinary(f)

            if getattr(f, "filename", "") and (not attach_url):

                abort(500, "No se pudo subir el adjunto a Cloudinary. Verificá configuración y conexión.")



            if not _already_inserted_recent(kind, method, amount_val, due_dt, row_note):

                db.session.add(

                    CollectionPayment(

                        order_id=order_id,

                        owner_user_id=(None if _has_global_access() else _effective_user_id()),

                        kind=kind,

                        method=method,

                        amount=amount_val,

                        due_date=due_dt,

                        attachment_url=attach_url,

                        notes=row_note,

                    )

                )



            if due_dt and (max_due is None or due_dt > max_due):

                max_due = due_dt



    if not is_draft:

        payment_defs = [

            ("pm_efectivo", "EFECTIVO", False),

            ("pm_transferencia", "TRANSFERENCIA", False),

            ("pm_echeq", "E-CHEQ", False),

            ("pm_cheque_terceros", "CHEQUE_TERCEROS", False),

            ("pm_cheque_propio", "CHEQUE_PROPIO", False),

        ]

        for prefix, method, needs_attach in payment_defs:

            raw_amount = (request.form.get(f"{prefix}_amount") or "").strip()

            raw_date = (request.form.get(f"{prefix}_date") or "").strip()

            row_note = (request.form.get(f"{prefix}_notes") or "").strip() or None

            f = request.files.get(f"{prefix}_attachment")



            try:

                amount_val = _parse_amount(raw_amount)

            except Exception:

                amount_val = 0.0



            if not amount_val and not raw_date and not row_note and not (f and getattr(f, "filename", "")):

                continue



            # No crear pagos con monto 0

            try:

                if float(amount_val or 0.0) <= 0.009:

                    continue

            except Exception:

                continue



            due_dt = None

            if raw_date:

                try:

                    due_dt = _parse_datetime_like(raw_date)

                except Exception:

                    due_dt = None



            attach_url = _upload_file_to_cloudinary(f)

            # Adjunto opcional: si hay archivo y no se pudo subir, informar. Si no hay archivo, continuar.

            if getattr(f, "filename", "") and not attach_url:

                abort(500, "No se pudo subir el adjunto a Cloudinary. Verificá configuración y conexión.")



            if not _already_inserted_recent("PAYMENT", method, amount_val, due_dt, row_note):

                db.session.add(

                    CollectionPayment(

                        order_id=order_id,

                        owner_user_id=(None if _has_global_access() else _effective_user_id()),

                        kind="PAYMENT",

                        method=method,

                        amount=amount_val,

                        due_date=due_dt,

                        attachment_url=attach_url,

                        notes=row_note,

                    )

                )

            if due_dt and (max_due is None or due_dt > max_due):

                max_due = due_dt



    # Ítems de Nota de crédito y Retenciones (solo al confirmar, no en borrador)

    if not is_draft:

        try:

            for it in (nc_items or []):

                amt = float(it.get("amount") or 0.0)

                if amt <= 0.009:

                    continue

                concept = (it.get("concept") or "").strip()

                note_text = "Nota de crédito"

                if concept:

                    note_text = f"Nota de crédito - {concept}"

                db.session.add(

                    CollectionPayment(

                        order_id=order_id,

                        owner_user_id=(None if _has_global_access() else _effective_user_id()),

                        kind="CREDIT_NOTE",

                        method="NC",

                        amount=abs(amt),

                        due_date=None,

                        attachment_url=None,

                        notes=note_text,

                    )

                )

        except Exception:

            pass



        try:

            for it in (ret_items or []):

                amt = float(it.get("amount") or 0.0)

                if amt <= 0.009:

                    continue

                concept = (it.get("concept") or "").strip()

                note_text = "Retenciones"

                if concept:

                    note_text = f"Retenciones - {concept}"

                db.session.add(

                    CollectionPayment(

                        order_id=order_id,

                        owner_user_id=(None if _has_global_access() else _effective_user_id()),

                        kind="PAYMENT",

                        method="RETENCION",

                        amount=abs(amt),

                        due_date=None,

                        attachment_url=None,

                        notes=note_text,

                    )

                )

        except Exception:

            pass



    # Update collection summary

    try:

        candidates = []

        if getattr(coll, "monto", None) is not None:

            candidates.append(float(coll.monto))

        if getattr(o, "precio_final", None) is not None:

            candidates.append(float(o.precio_final))

        lg = getattr(o, "logistics", None)

        if lg is not None and getattr(lg, "precio", None) is not None:

            candidates.append(float(lg.precio))

        # Usar el monto autoritativo más alto disponible.

        # (Evita cerrar pedidos si en algún lado quedó un monto parcial/erróneo.)

        base_monto = 0.0

        try:

            positives = [float(v) for v in candidates if v is not None and float(v) > 0]

            base_monto = float(max(positives) if positives else 0.0)

        except Exception:

            base_monto = 0.0

    except Exception:

        base_monto = 0.0



    # Persistir monto SOLO si está vacío y se deduce de campos autoritativos (no del form).

    try:

        if not is_draft and float(base_monto or 0.0) > 0.009:

            if getattr(coll, "monto", None) is None or float(getattr(coll, "monto", 0) or 0) <= 0:

                coll.monto = float(base_monto)

    except Exception:

        pass

    # Recalcular totales con TODO lo guardado (histórico + nuevo), excluyendo DRAFT

    total_paid = 0.0

    total_credit = 0.0

    max_due_all = None

    methods_used = set()

    try:

        for p in (

            CollectionPayment.query

            .filter_by(order_id=order_id)

            .filter(CollectionPayment.kind != "DRAFT")

            .filter(CollectionPayment.voided_at.is_(None))

            .all()

        ):

            kind = (getattr(p, "kind", "") or "").strip().upper()

            amt = float(getattr(p, "amount", 0) or 0)

            if kind == "CREDIT_NOTE":

                total_credit += abs(amt)

            elif kind == "PAYMENT":

                total_paid += abs(amt)

                try:

                    m = (getattr(p, "method", None) or "").strip().upper()

                except Exception:

                    m = ""

                if m and m not in ("RETENCION", "NC", "DRAFT"):

                    methods_used.add(m)

            dd = getattr(p, "due_date", None)

            if dd and (max_due_all is None or dd > max_due_all):

                max_due_all = dd

    except Exception:

        total_paid = 0.0

        total_credit = 0.0

        max_due_all = None



    net_due = base_monto - total_credit

    # Vencimiento estimado: tomar el más lejano entre lo ingresado históricamente

    if max_due_all:

        coll.fecha_pago_estimada = max_due_all

    try:

        if is_draft:

            # borrador: nunca marcar como cobrado

            coll.fecha_cobro_efectiva = None

        else:

            # total adeudado luego de notas de crédito

            if base_monto <= 0.009:

                # Sin monto válido no se puede cerrar el pedido.

                coll.fecha_cobro_efectiva = None

            elif net_due <= 0:

                # no queda nada por cobrar

                if not coll.fecha_cobro_efectiva:

                    coll.fecha_cobro_efectiva = datetime.utcnow()

            elif total_paid >= net_due:

                if not coll.fecha_cobro_efectiva:

                    coll.fecha_cobro_efectiva = datetime.utcnow()

            else:

                # pago parcial: mantener pendiente

                coll.fecha_cobro_efectiva = None

    except Exception:

        pass

    # Si se cobro, asumir entrega efectiva (regla unica en _sync_entrega_desde_cobro)

    if not is_draft:

        _sync_entrega_desde_cobro(coll)



    # Si hay excedente (saldo a favor), requerir confirmación explícita

    try:

        if not is_draft:

            confirm_overpay = (request.form.get("confirm_overpay") == "1")

            excedente = float(total_paid or 0.0) - float(net_due or 0.0)

            if excedente > 0.009 and not confirm_overpay:

                abort(400, "Excedente / saldo a favor: requiere confirmación")

    except Exception:

        pass



    # Si es borrador, guardar un snapshot para reabrir desde "Pendientes"

    saved_draft = None

    try:

        if is_draft:

            snap = {

                "client_id": client_id,

                "company_id": company_id,

                "order_id": (order_id or None),

                "order_created_at": raw_created,

                "order_fecha_entrega_efectiva": raw_entrega,

                "order_fecha_pago_estimada": raw_venc,

                "order_monto": raw_monto,

                "order_credit_note": ("" if abs(nc_total_form) <= 0.009 else str(nc_total_form)),

                "order_retenciones": ("" if abs(ret_total_form) <= 0.009 else str(ret_total_form)),

                "nc_items": [

                    {

                        "amount": str(it.get("amount_raw") or ""),

                        "concept": str(it.get("concept") or ""),

                    }

                    for it in (nc_items or [])

                    if (str(it.get("amount_raw") or "").strip() or str(it.get("concept") or "").strip())

                ],

                "ret_items": [

                    {

                        "amount": str(it.get("amount_raw") or ""),

                        "concept": str(it.get("concept") or ""),

                    }

                    for it in (ret_items or [])

                    if (str(it.get("amount_raw") or "").strip() or str(it.get("concept") or "").strip())

                ],

                "order_notes": raw_notes,

                "pm": {},

                "rows": [],

            }

            try:

                for prefix in (

                    "pm_efectivo",

                    "pm_transferencia",

                    "pm_echeq",

                    "pm_cheque_terceros",

                    "pm_cheque_propio",

                ):

                    snap["pm"][prefix] = {

                        "amount": (request.form.get(f"{prefix}_amount") or "").strip(),

                        "date": (request.form.get(f"{prefix}_date") or "").strip(),

                    }

            except Exception:

                pass

            try:

                row_count = max(len(kinds), len(methods), len(amounts), len(dues)) if any([kinds, methods, amounts, dues]) else 0

                for i in range(row_count):

                    method = (methods[i] if i < len(methods) else "").strip()

                    raw_amount = (amounts[i] if i < len(amounts) else "").strip()

                    raw_due = (dues[i] if i < len(dues) else "").strip()

                    if not method and not raw_amount and not raw_due:

                        continue

                    prefix = None

                    try:

                        mp = {

                            "EFECTIVO": "pm_efectivo",

                            "TRANSFERENCIA": "pm_transferencia",

                            "E-CHEQ": "pm_echeq",

                            "CHEQUE_TERCEROS": "pm_cheque_terceros",

                            "CHEQUE_PROPIO": "pm_cheque_propio",

                        }

                        prefix = mp.get(method)

                    except Exception:

                        prefix = None

                    if not prefix:

                        continue

                    snap["rows"].append({"prefix": prefix, "amount": raw_amount, "date": raw_due})

            except Exception:

                pass



            drow = None

            if draft_id:

                try:

                    drow = CollectionDraft.query.get(int(draft_id))

                except Exception:

                    drow = None

                if drow is not None:

                    _require_owner(drow)

            if drow is None:

                try:

                    uid = (None if _has_global_access() else _effective_user_id())

                    qd = CollectionDraft.query.filter_by(owner_user_id=uid).filter(CollectionDraft.client_id == client_id).filter(CollectionDraft.company_id == company_id)

                    if order_id:

                        qd = qd.filter(CollectionDraft.order_id == order_id)

                    else:

                        qd = qd.filter(CollectionDraft.order_id.is_(None))

                    drow = qd.order_by(CollectionDraft.updated_at.desc()).first()

                except Exception:

                    drow = None

            if drow is None:

                drow = CollectionDraft(

                    owner_user_id=(None if _has_global_access() else _effective_user_id()),

                    client_id=client_id,

                    company_id=company_id,

                    order_id=(order_id or None),

                )

                db.session.add(drow)

            try:

                drow.order_id = (order_id or None)

            except Exception:

                pass

            try:

                drow.notes = json.dumps(snap, ensure_ascii=False)

            except Exception:

                pass

            saved_draft = drow

    except Exception:

        saved_draft = saved_draft



    def _map_forma_pago_detalle_to_enum(det: str):

        det = (det or "").strip().upper()

        if not det:

            return None

        if det == "EFECTIVO":

            try:

                return PaymentMethod.EFECTIVO

            except Exception:

                return None

        if det == "TRANSFERENCIA":

            try:

                return PaymentMethod.TRANSFERENCIA

            except Exception:

                return None

        if det in ("CHEQUE", "E-CHEQ", "CHEQUE_TERCEROS", "CHEQUE_PROPIO"):

            try:

                return PaymentMethod.CHEQUE

            except Exception:

                return None

        if det in ("VARIAS", "NO_SE_SABE"):

            try:

                return PaymentMethod.NO_SE_SABE

            except Exception:

                return None

        return None



    try:

        if (not is_draft) and methods_used:

            detalle = "VARIAS" if len(methods_used) > 1 else list(methods_used)[0]

            forma_enum = _map_forma_pago_detalle_to_enum(detalle)

            try:

                coll.forma_pago_detalle = detalle

            except Exception:

                pass

            try:

                coll.forma_pago = forma_enum

            except Exception:

                pass

            try:

                o.forma_pago_detalle = detalle

            except Exception:

                pass

            try:

                o.forma_pago = forma_enum

            except Exception:

                pass

            try:

                lg2 = getattr(o, "logistics", None)

                if lg2 is not None:

                    try:

                        lg2.forma_pago_detalle = detalle

                    except Exception:

                        pass

                    try:

                        lg2.forma_pago = forma_enum

                    except Exception:

                        pass

            except Exception:

                pass

    except Exception:

        pass



    db.session.commit()



    try:

        if draft_id:

            drow = CollectionDraft.query.get(int(draft_id))

            if drow is not None:

                _require_owner(drow)

                db.session.delete(drow)

                db.session.commit()

    except Exception:

        try:

            db.session.rollback()

        except Exception:

            pass



    # Para autoguardado de borradores desde JS

    try:

        if request.headers.get("X-Requested-With") == "XMLHttpRequest":

            if is_draft:

                try:

                    return jsonify({

                        "ok": True,

                        "order_id": (int(order_id) if order_id else None),

                        "draft_id": (int(getattr(saved_draft, "id", None) or 0) if saved_draft is not None else None),

                    })

                except Exception:

                    return jsonify({"ok": True, "order_id": (int(order_id) if order_id else None)})

            return jsonify({"ok": True, "order_id": int(order_id), "redirect": url_for("main.historial")})

    except Exception:

        pass



    # Post-guardar: volver a Historial (no a Deudas)

    try:

        _invalidate_notif_count_cache()

    except Exception:

        pass

    if not is_draft:

        return redirect(url_for("main.historial"))

    return redirect(url_for("main.deudas_pendientes"))





@bp.post("/cobranzas/<int:order_id>/cobrar")

def cobranzas_mark_cobrado(order_id: int):

    coll = Collection.query.filter_by(order_id=order_id).first_or_404()

    # No permitir marcar COBRADO si el pedido todavía no está entregado.

    try:

        if not getattr(coll, "fecha_entrega_efectiva", None):

            if request.headers.get("X-Requested-With") == "XMLHttpRequest":

                return jsonify({"ok": False, "error": "NO_ENTREGADO"}), 400

            abort(400)

    except Exception:

        pass

    if not _has_global_access():

        o = Order.query.get_or_404(order_id)

        _require_owner(o)

    else:

        o = Order.query.get(order_id)



    fp_det_applied = None



    # Completar forma de pago por defecto del cliente si está vacío

    try:

        fp_det = (getattr(coll, "forma_pago_detalle", None) or (getattr(getattr(coll, "forma_pago", None), "value", None) if getattr(coll, "forma_pago", None) else "") or "").strip().upper()

        if not fp_det and o is not None:

            try:

                cl = getattr(o, "client", None)

                if cl is None:

                    cl = Client.query.get(getattr(o, "client_id", None))

                if cl is not None:

                    fp_det = (getattr(cl, "forma_pago_habitual", None) or "").strip().upper()

            except Exception:

                pass

        if not fp_det and o is not None:

            try:

                comp = getattr(o, "company", None)

                if comp is None:

                    comp = Company.query.get(getattr(o, "company_id", None))

                if comp is not None:

                    fp_det = (getattr(comp, "forma_pago_default", None) or "").strip().upper()

            except Exception:

                pass



        if fp_det:

            coll.forma_pago_detalle = fp_det

            fp_det_applied = fp_det

            if fp_det in ("CHEQUE", "E-CHEQ", "CHEQUE_TERCEROS", "CHEQUE_PROPIO"):

                try:

                    coll.forma_pago = PaymentMethod.CHEQUE

                except Exception:

                    coll.forma_pago = None

            elif fp_det == "EFECTIVO":

                try:

                    coll.forma_pago = PaymentMethod.EFECTIVO

                except Exception:

                    coll.forma_pago = None

            elif fp_det == "TRANSFERENCIA":

                try:

                    coll.forma_pago = PaymentMethod.TRANSFERENCIA

                except Exception:

                    coll.forma_pago = None

            else:

                try:

                    coll.forma_pago = PaymentMethod(fp_det)

                except Exception:

                    pass

            # Mantener consistencia con la Orden para que el legajo quede vinculado.

            try:

                if o is not None:

                    o.forma_pago = getattr(coll, "forma_pago", None)

                    o.forma_pago_detalle = getattr(coll, "forma_pago_detalle", None)

            except Exception:

                pass

    except Exception:

        pass



    # Completar monto si falta (para que el pago "total" tenga base)

    try:

        if getattr(coll, "monto", None) is None or float(coll.monto or 0) <= 0:

            picked = None

            try:

                lg = LogisticsStatus.query.filter_by(order_id=order_id).first()

            except Exception:

                lg = None

            candidates = []

            try:

                if o is not None and getattr(o, "precio_final", None) is not None:

                    candidates.append(float(o.precio_final))

            except Exception:

                pass

            try:

                if lg is not None and getattr(lg, "precio", None) is not None:

                    candidates.append(float(lg.precio))

            except Exception:

                pass

            for v in candidates:

                try:

                    if v is not None and float(v) > 0:

                        picked = float(v)

                        break

                except Exception:

                    pass

            if picked is not None:

                coll.monto = picked

    except Exception:

        pass



    coll.fecha_cobro_efectiva = datetime.utcnow()



    # Si se marca COBRADO desde esta pantalla, asumir pago total (crear un PAYMENT si no existe)

    try:

        has_payment = (

            CollectionPayment.query

            .filter_by(order_id=order_id)

            .filter(CollectionPayment.kind != "DRAFT")

            .filter(CollectionPayment.voided_at.is_(None))

            .count()

        )

        if (has_payment or 0) == 0:

            amt = float(getattr(coll, "monto", 0) or 0)

            if amt > 0:

                p = CollectionPayment(

                    order_id=order_id,

                    owner_user_id=getattr(coll, "owner_user_id", None),

                    kind="PAYMENT",

                    method=(getattr(coll, "forma_pago_detalle", None) or ""),

                    amount=amt,

                    due_date=coll.fecha_cobro_efectiva,

                )

                db.session.add(p)

    except Exception:

        pass



    # Desde esta pantalla, si no hay fila de logística se crea para poder asumir la entrega

    logistics = None

    try:

        logistics = LogisticsStatus.query.filter_by(order_id=order_id).first()

        if logistics is None:

            logistics = LogisticsStatus(order_id=order_id)

            try:

                logistics.fecha_compra = getattr(o, "created_at", None) or datetime.utcnow()

            except Exception:

                logistics.fecha_compra = datetime.utcnow()

            try:

                logistics.forma_pago = getattr(o, "forma_pago", None)

            except Exception:

                pass

            try:

                logistics.forma_pago_detalle = getattr(o, "forma_pago_detalle", None)

            except Exception:

                pass

            try:

                logistics.precio = getattr(o, "precio_final", None)

            except Exception:

                pass

            # Si no existe logística, estimar fecha de entrega (para poder marcar efectiva “en la estimada”)

            try:

                demora = None

                try:

                    demora = int(getattr(getattr(o, "company", None), "demora_despacho_promedio_dias", None) or 0)

                except Exception:

                    demora = None

                base_dt = getattr(logistics, "fecha_compra", None) or datetime.utcnow()

                if demora is not None and demora > 0:

                    logistics.fecha_entrega_estimada = base_dt + timedelta(days=demora)

            except Exception:

                pass

            db.session.add(logistics)



    except Exception:

        pass

    # Si se cobro, asumir entrega efectiva (regla unica en _sync_entrega_desde_cobro)

    _sync_entrega_desde_cobro(coll, logistics)

    db.session.commit()

    try:

        _invalidate_notif_count_cache()

    except Exception:

        pass

    if request.headers.get("X-Requested-With") == "XMLHttpRequest":

        fp = ""

        try:

            fp = (fp_det_applied or getattr(coll, "forma_pago_detalle", None) or "")

        except Exception:

            fp = (fp_det_applied or "")

        return jsonify({"ok": True, "forma_pago_detalle": (fp or "")})

    return_to = (request.form.get("return_to") or "").strip()

    if return_to and return_to.startswith("/") and ("//" not in return_to):

        return redirect(return_to)

    return redirect(url_for("main.deudas_pendientes"))





@bp.post("/cobranzas/<int:order_id>/desmarcar")

def cobranzas_unmark_cobrado(order_id: int):

    coll = Collection.query.filter_by(order_id=order_id).first_or_404()

    if not _has_global_access():

        o = Order.query.get_or_404(order_id)

        _require_owner(o)

    coll.fecha_cobro_efectiva = None

    db.session.commit()

    try:

        _invalidate_notif_count_cache()

    except Exception:

        pass

    if request.headers.get("X-Requested-With") == "XMLHttpRequest":

        return jsonify({"ok": True})

    return redirect(url_for("main.deudas_pendientes"))





@bp.post("/cobranzas/desmarcar_automaticos")

def cobranzas_unmark_automaticos():

    # Revertir cobros marcados automáticamente: cobro_efectivo == entrega_efectiva

    try:

        q = (

            Collection.query

            .filter(Collection.fecha_cobro_efectiva.isnot(None))

            .filter(Collection.fecha_entrega_efectiva.isnot(None))

            .filter(Collection.fecha_cobro_efectiva == Collection.fecha_entrega_efectiva)

        )

        q.update({Collection.fecha_cobro_efectiva: None}, synchronize_session=False)

        db.session.commit()

    except Exception:

        try:

            db.session.rollback()

        except Exception:

            pass

    return redirect(url_for("main.deudas_pendientes"))





@bp.post("/cobranzas/<int:order_id>/update")

def cobranzas_update(order_id: int):

    coll = Collection.query.filter_by(order_id=order_id).first_or_404()

    o = Order.query.get_or_404(order_id)

    if not _has_global_access():

        _require_owner(o)

    monto_present = ("monto" in request.form)

    monto = request.form.get("monto", type=float)

    forma_pago_raw = request.form.get("forma_pago")

    forma_pago_enum = None

    forma_pago_detalle = None

    if forma_pago_raw is not None:

        v = (forma_pago_raw or "").strip().upper()

        if v == "":

            forma_pago_enum = None

            forma_pago_detalle = None

        else:

            forma_pago_detalle = v

            if v in ("CHEQUE", "E-CHEQ", "CHEQUE_TERCEROS", "CHEQUE_PROPIO"):

                try:

                    forma_pago_enum = PaymentMethod.CHEQUE

                except Exception:

                    forma_pago_enum = None

            elif v == "EFECTIVO":

                try:

                    forma_pago_enum = PaymentMethod.EFECTIVO

                except Exception:

                    forma_pago_enum = None

            elif v == "TRANSFERENCIA":

                try:

                    forma_pago_enum = PaymentMethod.TRANSFERENCIA

                except Exception:

                    forma_pago_enum = None

            elif v in ("VARIAS", "NO_SE_SABE"):

                try:

                    forma_pago_enum = PaymentMethod.NO_SE_SABE

                except Exception:

                    forma_pago_enum = None

            else:

                try:

                    forma_pago_enum = PaymentMethod(v)

                except Exception:

                    forma_pago_enum = None

    pago_estimado = request.form.get("pago_estimado")

    entrega_efectiva_raw = (request.form.get("fecha_entrega_efectiva") or "").strip()

    cobro_efectivo_raw = (request.form.get("fecha_cobro_efectiva") or "").strip()



    prev_entrega = None

    prev_pago = None

    try:

        prev_entrega = getattr(coll, "fecha_entrega_efectiva", None)

    except Exception:

        prev_entrega = None

    if monto_present:

        # Si el input se vacía, el hidden llega como "" y request.form.get(..., type=float) devuelve None.

        # En ese caso, persistimos NULL para permitir limpiar el monto.

        coll.monto = monto

    if forma_pago_raw is not None:

        coll.forma_pago = forma_pago_enum

        coll.forma_pago_detalle = forma_pago_detalle

    if pago_estimado is not None:

        coll.fecha_pago_estimada = _parse_datetime_like(pago_estimado) if pago_estimado else None

    if entrega_efectiva_raw is not None:

        try:

            coll.fecha_entrega_efectiva = _parse_datetime_like(entrega_efectiva_raw) if entrega_efectiva_raw else None

        except Exception:

            pass



    # Autocalcular vencimiento si cambió la entrega efectiva.

    # Regla solicitada: entrega efectiva y vencimiento deben quedar siempre conectados.

    try:

        new_entrega = getattr(coll, "fecha_entrega_efectiva", None)

        if new_entrega is not None:

            entrega_changed = False

            try:

                entrega_changed = (prev_entrega is None) or (prev_entrega.date() != new_entrega.date())

            except Exception:

                entrega_changed = True



            if entrega_changed:

                plazo_days = None

                try:

                    if getattr(o, "plazo_pago_dias", None) is not None:

                        plazo_days = int(o.plazo_pago_dias or 0)

                except Exception:

                    plazo_days = None

                if plazo_days is None:

                    try:

                        lnk = ClientCompanyLink.query.filter_by(client_id=int(o.client_id), company_id=int(o.company_id)).first()

                    except Exception:

                        lnk = None

                    try:

                        if lnk is not None and getattr(lnk, "plazo_pago_dias", None) is not None:

                            plazo_days = int(lnk.plazo_pago_dias or 0)

                    except Exception:

                        plazo_days = None

                if plazo_days is None:

                    try:

                        comp = Company.query.get(int(o.company_id)) if getattr(o, "company_id", None) else None

                    except Exception:

                        comp = None

                    try:

                        if comp is not None and getattr(comp, "plazo_pago_promedio_dias", None) is not None:

                            plazo_days = int(comp.plazo_pago_promedio_dias or 0)

                    except Exception:

                        plazo_days = None

                if plazo_days is None:

                    plazo_days = 30

                try:

                    coll.fecha_pago_estimada = new_entrega + timedelta(days=max(0, int(plazo_days or 0)))

                except Exception:

                    pass

    except Exception:

        pass

    if cobro_efectivo_raw is not None:

        try:

            coll.fecha_cobro_efectiva = _parse_datetime_like(cobro_efectivo_raw) if cobro_efectivo_raw else None

        except Exception:

            pass

    # Mantener consistencia con status si existe registro

    logistics = LogisticsStatus.query.filter_by(order_id=order_id).first()

    if logistics:

        if monto_present:

            logistics.precio = monto

        if forma_pago_raw is not None:

            logistics.forma_pago = forma_pago_enum

            logistics.forma_pago_detalle = forma_pago_detalle

        if entrega_efectiva_raw is not None:

            try:

                logistics.fecha_entrega_efectiva = _parse_datetime_like(entrega_efectiva_raw) if entrega_efectiva_raw else None

            except Exception:

                pass

    # Si se cobro, asumir entrega efectiva (regla unica en _sync_entrega_desde_cobro)

    _sync_entrega_desde_cobro(coll, logistics)

    # Mantener consistencia con la Orden

    order = Order.query.get(order_id)

    if order:

        if monto_present:

            order.precio_final = monto

        if forma_pago_raw is not None:

            order.forma_pago = forma_pago_enum

            order.forma_pago_detalle = forma_pago_detalle

    db.session.commit()

    if request.headers.get("X-Requested-With") == "XMLHttpRequest":

        try:

            entrega_iso = (coll.fecha_entrega_efectiva.date().isoformat() if getattr(coll, "fecha_entrega_efectiva", None) else "")

        except Exception:

            entrega_iso = ""

        try:

            pago_iso = (coll.fecha_pago_estimada.date().isoformat() if getattr(coll, "fecha_pago_estimada", None) else "")

        except Exception:

            pago_iso = ""

        try:

            cobro_iso = (coll.fecha_cobro_efectiva.date().isoformat() if getattr(coll, "fecha_cobro_efectiva", None) else "")

        except Exception:

            cobro_iso = ""

        return jsonify({

            "ok": True,

            "fecha_entrega_efectiva": entrega_iso,

            "fecha_pago_estimada": pago_iso,

            "fecha_cobro_efectiva": cobro_iso,

        })

    return_to = (request.form.get("return_to") or "").strip()

    if return_to and return_to.startswith("/") and ("//" not in return_to):

        return redirect(return_to)

    return redirect(url_for("main.deudas_pendientes"))





@bp.get("/historial")

def historial():

    sort = (request.args.get("sort") or "pedido").strip().lower()

    direction = (request.args.get("dir") or "desc").strip().lower()

    company_id_raw = (request.args.get("company_id") or "").strip()

    client_id_raw = (request.args.get("client_id") or "").strip()

    desde = (request.args.get("desde") or "").strip()

    hasta = (request.args.get("hasta") or "").strip()

    try:

        company_id = int(company_id_raw) if company_id_raw else None

    except Exception:

        company_id = None

    try:

        client_id = int(client_id_raw) if client_id_raw else None

    except Exception:

        client_id = None

    show_all = request.args.get("all") == "1"

    page = request.args.get("page", default=1, type=int) or 1

    per_page = request.args.get("per_page", default=10, type=int) or 10

    if page < 1:

        page = 1

    if per_page < 1:

        per_page = 10

    if per_page > 200:

        per_page = 200



    base = (

        Order.query

        .outerjoin(LogisticsStatus, LogisticsStatus.order_id == Order.id)

        .outerjoin(Collection, Collection.order_id == Order.id)

        .options(

            selectinload(Order.client),

            selectinload(Order.company),

            selectinload(Order.logistics),

            selectinload(Order.collection),

        )

    )

    base = base.filter(Order.deleted_at.is_(None))

    if company_id:

        base = base.filter(Order.company_id == company_id)

    if client_id:

        base = base.filter(Order.client_id == client_id)

    order_date_expr = func.coalesce(LogisticsStatus.fecha_compra, Order.created_at)

    if desde:

        try:

            d_from = _parse_datetime_like(desde)

            if d_from is not None:

                base = base.filter(order_date_expr >= d_from)

        except Exception:

            pass

    if hasta:

        try:

            d_to = _parse_datetime_like(hasta)

            if d_to is not None:

                base = base.filter(order_date_expr <= d_to)

        except Exception:

            pass



    if not _has_global_access():

        uid = _effective_user_id()

        if uid is not None:

            base = base.filter(Order.owner_user_id == uid)



    if sort == "cliente":

        base = base.join(Client, Order.client_id == Client.id)

        order_cols = (Client.apellido, Client.nombre, Order.id)

    elif sort == "empresa":

        base = base.join(Company, Order.company_id == Company.id)

        order_cols = (Company.nombre, Order.id)

    elif sort == "compra":

        order_cols = (func.coalesce(LogisticsStatus.fecha_compra, Order.created_at), Order.created_at, Order.id)

    elif sort == "entrega":

        order_cols = (LogisticsStatus.fecha_entrega_efectiva, Order.created_at, Order.id)

    elif sort == "cobro":

        order_cols = (Collection.fecha_cobro_efectiva, Order.created_at, Order.id)

    else:

        # pedido

        order_cols = (Order.created_at, Order.id)



    is_desc = direction != "asc"

    applied = []

    # SQLite no soporta NULLS LAST. Emular: ordenar primero por "es NULL" (0/1) asc,

    # y luego por el valor en asc/desc.

    for c in order_cols:

        try:

            applied.append(c.is_(None).asc())

        except Exception:

            pass

        applied.append(c.desc() if is_desc else c.asc())



    base = base.order_by(*applied)

    if not show_all:

        offset = (page - 1) * per_page

        rows = base.offset(offset).limit(per_page + 1).all()

        has_next = len(rows) > per_page

        if has_next:

            rows = rows[:per_page]

        has_prev = page > 1

        orders = rows

    else:

        orders = base.all()

        has_next = False

        has_prev = False



    return_to = request.full_path if hasattr(request, "full_path") else "/historial"

    return render_template(

        "historial.html",

        active="historial",

        orders=orders,

        page=page,

        per_page=per_page,

        has_next=has_next,

        has_prev=has_prev,

        sort=sort,

        direction=direction,

        show_all=show_all,

        return_to=return_to,

    )





@bp.get("/crm")

def crm():

    rows = []

    q = Client.query

    if not _has_global_access():

        uid = _effective_user_id()

        if uid is not None:

            q = q.filter(Client.owner_user_id == uid)

    clients = q.order_by(Client.apellido, Client.nombre).all()

    # Mapeo de etiquetas

    label_map = {

        RelationStatus.TRABAJA.value: "Trabaja",

        RelationStatus.TRABAJABA.value: "Trabajaba",

        RelationStatus.A_INCORPORAR.value: "A Incorporar",

    }

    clients = (

        q.options(selectinload(Client.links).selectinload(ClientCompanyLink.company))

        .order_by(Client.apellido, Client.nombre)

        .all()

    )



    pq = db.session.query(Order.client_id, Order.company_id)

    pq = pq.filter(Order.deleted_at.is_(None))

    if not _has_global_access():

        uid = _effective_user_id()

        if uid is not None:

            pq = pq.filter(Order.owner_user_id == uid)

    pairs = pq.distinct().all()

    company_ids_from_orders = set([int(coid) for _cid, coid in pairs if coid])

    pair_set = set([(int(cid), int(coid)) for cid, coid in pairs if cid and coid])



    last_map = {

        (int(r[0]), int(r[1])): r[2]

        for r in (

            db.session.query(Order.client_id, Order.company_id, func.max(Order.created_at))

            .filter(Order.deleted_at.is_(None))

            .filter(True if _has_global_access() else (Order.owner_user_id == _effective_user_id()))

            .group_by(Order.client_id, Order.company_id)

            .all()

        )

        if r[0] and r[1]

    }

    avg_map = {

        (int(r[0]), int(r[1])): r[2]

        for r in (

            db.session.query(Order.client_id, Order.company_id, func.avg(Order.precio_final))

            .filter(Order.deleted_at.is_(None))

            .filter(True if _has_global_access() else (Order.owner_user_id == _effective_user_id()))

            .group_by(Order.client_id, Order.company_id)

            .all()

        )

        if r[0] and r[1]

    }

    cobradas_map = {

        (int(r[0]), int(r[1])): int(r[2] or 0)

        for r in (

            db.session.query(Order.client_id, Order.company_id, func.count(Collection.id))

            .join(Collection, Collection.order_id == Order.id)

            .filter(Collection.fecha_cobro_efectiva.isnot(None))

            .filter(True if _has_global_access() else (Order.owner_user_id == _effective_user_id()))

            .group_by(Order.client_id, Order.company_id)

            .all()

        )

        if r[0] and r[1]

    }



    link_status = {}

    company_ids_from_links = set()

    for c in clients:

        for l in (c.links or []):

            if getattr(l, "company_id", None):

                company_ids_from_links.add(int(l.company_id))

                st_val = getattr(getattr(l, "status", None), "value", None)

                link_status[(int(c.id), int(l.company_id))] = label_map.get(st_val, "-")



    all_company_ids = sorted(company_ids_from_orders.union(company_ids_from_links))

    companies = Company.query.filter(Company.id.in_(all_company_ids)).all() if all_company_ids else []

    company_by_id = {int(co.id): co for co in companies}



    pairs_by_client = {}

    for cid, coid in pair_set:

        pairs_by_client.setdefault(cid, set()).add(coid)

    for key in link_status.keys():

        cid, coid = key

        pairs_by_client.setdefault(cid, set()).add(coid)



    for c in clients:

        company_ids = pairs_by_client.get(int(c.id), set())

        if not company_ids:

            rows.append({

                "cliente": f"{c.apellido} {c.nombre}",

                "empresa": "-",

                "ultima_compra": None,

                "cobradas": 0,

                "promedio": None,

                "categ": "-",

            })

            continue

        for cid in sorted(company_ids):

            comp = company_by_id.get(int(cid))

            if not comp:

                continue

            key = (int(c.id), int(cid))

            last_date = last_map.get(key)

            cobradas = cobradas_map.get(key, 0)

            avg_compra = avg_map.get(key)

            categ = link_status.get(key, "-")

            rows.append({

                "cliente": f"{c.apellido} {c.nombre}",

                "empresa": comp.nombre,

                "ultima_compra": last_date,

                "cobradas": int(cobradas),

                "promedio": float(avg_compra) if avg_compra is not None else None,

                "categ": categ,

            })



    return render_template("crm.html", active="crm", items=rows)





@bp.post("/pedidos/<int:order_id>/edit")

def pedidos_edit(order_id: int):

    o = Order.query.get_or_404(order_id)

    _require_owner(o)

    o.nota = request.form.get("nota") or o.nota

    o.descripcion = request.form.get("descripcion") or o.descripcion

    precio_final_raw = request.form.get("precio_final")

    precio_final = _parse_amount_like(precio_final_raw) if precio_final_raw is not None else None

    if precio_final_raw is not None:

        o.precio_final = precio_final

        # Sincronizar con logistics y collection

        if o.logistics:

            o.logistics.precio = precio_final

        if o.collection:

            o.collection.monto = precio_final

    forma_pago_raw = request.form.get("forma_pago")

    forma_pago = None if forma_pago_raw == "" else (PaymentMethod(forma_pago_raw) if forma_pago_raw else None)

    if forma_pago_raw is not None:

        o.forma_pago = forma_pago

        if o.logistics:

            o.logistics.forma_pago = forma_pago

        if o.collection:

            o.collection.forma_pago = forma_pago



    forma_pago_detalle_raw = request.form.get("forma_pago_detalle")

    if forma_pago_detalle_raw is not None:

        detail = (forma_pago_detalle_raw or "").strip().upper() or None

        o.forma_pago_detalle = detail

        if o.logistics:

            o.logistics.forma_pago_detalle = detail

        if o.collection:

            o.collection.forma_pago_detalle = detail

    db.session.commit()

    if request.headers.get("X-Requested-With") == "XMLHttpRequest":

        return jsonify({

            "ok": True,

            "order_id": o.id,

            "nota": o.nota or "",

            "descripcion": o.descripcion or "",

        })

    return redirect(url_for("main.historial"))





@bp.post("/pedidos/<int:order_id>/delete")

def pedidos_delete(order_id: int):

    o = Order.query.get_or_404(order_id)

    _require_owner(o)

    if getattr(o, "deleted_at", None) is None:

        o.deleted_at = datetime.utcnow()

        db.session.add(o)

    db.session.commit()



    return_to = (request.form.get("return_to") or "").strip()

    if return_to and return_to.startswith("/") and ("//" not in return_to):

        return redirect(return_to)

    ref = (request.headers.get("Referer") or "").strip()

    try:

        p = urlparse(ref).path

        if p and p.startswith("/") and ("//" not in p):

            return redirect(p)

    except Exception:

        pass

    return redirect(url_for("main.deudas_pendientes"))





@bp.get("/api/clientes/<int:client_id>/sucursales")

def api_client_branches(client_id: int):

    client = Client.query.get_or_404(client_id)

    _require_owner(client)

    rows = ClientBranch.query.filter_by(client_id=client_id).order_by(ClientBranch.nombre).all()

    # Compatibilidad: si no hay filas en ClientBranch pero el cliente tiene sucursal legacy,

    # crear una entrada básica para que aparezca en el combo de pedidos.

    if not rows and getattr(client, "sucursal", None):

        try:

            b = ClientBranch(client_id=client.id, nombre=client.sucursal)

            db.session.add(b)

            db.session.commit()

            rows = [b]

        except Exception:

            db.session.rollback()

    return jsonify([{"id": r.id, "label": r.nombre} for r in rows])





@bp.get("/api/pedidos/anteriores")

def api_prev_orders():

    client_id = request.args.get("client_id", type=int)

    company_id = request.args.get("company_id", type=int)

    limit = request.args.get("limit", default=10, type=int)

    if not client_id or not company_id:

        abort(400)



    client = Client.query.get_or_404(client_id)

    _require_owner(client)

    q = (

        Order.query

        .filter_by(client_id=client_id, company_id=company_id)

        .outerjoin(LogisticsStatus, LogisticsStatus.order_id == Order.id)

    )

    if not _has_global_access():

        uid = _effective_user_id()

        if uid is not None:

            q = q.filter(Order.owner_user_id == uid)

    # SQLite no soporta NULLS LAST. Emular: primero los que tienen fecha_compra, luego los NULL.

    try:

        q = q.order_by(LogisticsStatus.fecha_compra.is_(None).asc(), LogisticsStatus.fecha_compra.desc(), Order.created_at.desc())

    except Exception:

        q = q.order_by(Order.created_at.desc())

    q = q.limit(max(1, min(limit, 50)))

    items = []

    for o in q.all():

        lg = getattr(o, "logistics", None)

        fc = getattr(lg, "fecha_compra", None) if lg is not None else None

        monto = None

        try:

            if getattr(o, "precio_final", None) is not None:

                monto = float(o.precio_final or 0)

            elif lg is not None and getattr(lg, "precio", None) is not None:

                monto = float(lg.precio or 0)

        except Exception:

            monto = float(getattr(o, "precio_final", 0) or 0)

        items.append({

            "id": o.id,

            "created_at": o.created_at.isoformat() if o.created_at else None,

            "created_date": o.created_at.date().isoformat() if o.created_at else None,

            "fecha_compra": fc.isoformat() if fc else None,

            "fecha_compra_date": fc.date().isoformat() if fc else None,

            "nota": o.nota or "",

            "descripcion": o.descripcion or "",

            "precio_final": monto if monto is not None else 0.0,

            "forma_pago": getattr(o.forma_pago, "value", None),

            "forma_pago_detalle": getattr(o, "forma_pago_detalle", None),

        })

    return jsonify(items)





@bp.post("/clientes/<int:client_id>/sucursales")

def client_branch_add(client_id: int):

    client = Client.query.get_or_404(client_id)

    _require_owner(client)

    nombre = (request.form.get("nombre") or "").strip()

    if not nombre:

        abort(400)

    b = ClientBranch(client_id=client_id, nombre=nombre)

    db.session.add(b)

    db.session.commit()

    return redirect(url_for("main.clientes"))





@bp.post("/clientes/<int:client_id>/sucursales/<int:branch_id>/delete")

def client_branch_delete(client_id: int, branch_id: int):

    client = Client.query.get_or_404(client_id)

    _require_owner(client)

    b = ClientBranch.query.filter_by(id=branch_id, client_id=client_id).first_or_404()

    db.session.delete(b)

    db.session.commit()

    return redirect(url_for("main.clientes"))

    _require_owner(client)

    b = ClientBranch.query.filter_by(id=branch_id, client_id=client_id).first_or_404()

    db.session.delete(b)

    db.session.commit()

    return redirect(url_for("main.clientes"))

    db.session.delete(b)

    db.session.commit()

    return redirect(url_for("main.clientes"))

    return redirect(url_for("main.clientes"))

    db.session.delete(b)

    db.session.commit()

    return redirect(url_for("main.clientes"))

    db.session.commit()

    return redirect(url_for("main.clientes"))

