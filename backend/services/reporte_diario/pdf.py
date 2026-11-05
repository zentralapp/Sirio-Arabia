"""PDF del reporte diario con fpdf2 + Poppins (puro: sin DB ni Flask).

Dos pasos:
1. `armar_secciones(reporte)` arma, en orden y según el modo del reporte
   (lunes EXTENDIDO, martes a domingo DEL_DIA), lo que se dibuja: títulos,
   columnas y celdas YA formateadas en castellano ("1.234,56", DD/MM/YYYY,
   "Sin monto"), el total solo donde corresponde y, el lunes, el total
   general. Es lo que se testea.
2. `render_pdf(reporte, generado_en)` lo dibuja en A4 apaisado y devuelve
   los bytes (todo en memoria).
"""
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from fpdf import FPDF
from fpdf.fonts import FontFace

from backend.services.reporte_diario.clasificar import total_general, totales
from backend.services.reporte_diario.fechas import EXTENDIDO, TZ_NEGOCIO
from backend.services.reporte_diario.modelo import (
    SECCIONES_POR_MODO,
    FilaCobranza,
    FilaCumple,
    Reporte,
    TotalGeneral,
)
from backend.utils.formato import DIAS_ABREV, fmt_dia_mes, fmt_fecha_es, fmt_plata_es

FUENTES = Path(__file__).resolve().parent / "fuentes"
FAMILIA = "Poppins"

SIN_MONTO = "Sin monto"
SIN_REGISTROS = "Sin registros"
SIN_NOVEDADES = "Sin novedades"
TITULO_TOTAL_GENERAL = "Total general de deuda vigente"
REMITENTE = "Arabia Representaciones"
CREADOR = "Sirio-Arabia CRM"

_TZ = ZoneInfo(TZ_NEGOCIO)

_IZQ, _DER, _CENTRO = "LEFT", "RIGHT", "CENTER"

# (título de columna, ancho relativo, alineación)
_COL_CUMPLE = (
    ("Nombre", 50, _IZQ),
    ("Puesto", 40, _IZQ),
    ("Cliente", 60, _IZQ),
    ("Empresa", 50, _IZQ),
    ("Fecha", 18, _CENTRO),
)
_COL_CLIENTE = (("Cliente", 52, _IZQ), ("Empresa", 38, _IZQ), ("Pedido #", 18, _CENTRO), ("Vencimiento", 22, _CENTRO))
_COL_DIAS = (("Días de atraso", 16, _CENTRO),)
_COL_ESTADO = (("Estado", 22, _CENTRO),)
_COL_PLATA = (("Monto", 26, _DER), ("Pagado", 24, _DER), ("NC", 20, _DER), ("Saldo", 26, _DER))

_ESTADOS = {"ATRASADO": "ATRASADO", "A_COBRAR": "A COBRAR", "EN_CAMINO": "EN CAMINO"}


@dataclass(frozen=True)
class Seccion:
    titulo: str
    columnas: tuple[str, ...]
    anchos: tuple[int, ...]
    alineacion: tuple[str, ...]
    filas: tuple[tuple[str, ...], ...]
    total: str | None = None  # None = la sección no lleva total
    vacia_texto: str = SIN_REGISTROS

    @property
    def conteo(self) -> int:
        return len(self.filas)

    @property
    def encabezado(self) -> str:
        return f"{self.titulo} ({self.conteo})"


@dataclass(frozen=True)
class BloqueTotal:
    """Una línea destacada (el total general del lunes), no una tabla."""

    texto: str
    titulo: str = TITULO_TOTAL_GENERAL


# ---------------------------------------------------------------- contenido


def _con_dia(fecha) -> str:
    return f"{DIAS_ABREV[fecha.weekday()]} {fmt_fecha_es(fecha)}"


def _fila_cumple(c: FilaCumple) -> tuple[str, ...]:
    return (c.nombre, c.puesto or "", c.cliente, c.empresa or "", fmt_dia_mes(c.ocurrencia or c.fecha))


def _fila_cobranza(f: FilaCobranza, *, dias: bool, estado: bool) -> tuple[str, ...]:
    celdas = [
        f.cliente,
        f.empresa or "",
        str(f.order_id),
        fmt_fecha_es(f.vencimiento) if f.vencimiento else "",
    ]
    if dias:
        celdas.append(str(f.dias_atraso) if f.dias_atraso is not None else "")
    if estado:
        celdas.append(_ESTADOS.get(f.estado, f.estado.replace("_", " ")))
    saldo = f.saldo_efectivo
    celdas += [
        SIN_MONTO if f.sin_monto else fmt_plata_es(f.saldo.monto),
        fmt_plata_es(f.saldo.pagado),
        fmt_plata_es(f.saldo.nc),
        SIN_MONTO if saldo is None else fmt_plata_es(saldo),
    ]
    return tuple(celdas)


def _seccion(titulo, columnas, filas, total=None) -> Seccion:
    filas = tuple(filas)
    return Seccion(
        titulo=titulo,
        columnas=tuple(c[0] for c in columnas),
        anchos=tuple(c[1] for c in columnas),
        alineacion=tuple(c[2] for c in columnas),
        filas=filas,
        # Vacía: solo "Sin registros", sin un "Total saldo: 0,00" que no dice nada.
        total=total if filas else None,
    )


def _cobranzas(titulo, filas, *, dias=False, estado=True, total=None) -> Seccion:
    columnas = _COL_CLIENTE + (_COL_DIAS if dias else ()) + (_COL_ESTADO if estado else ()) + _COL_PLATA
    return _seccion(titulo, columnas, (_fila_cobranza(f, dias=dias, estado=estado) for f in filas), total)


def _fin_hoy(reporte: Reporte) -> date:
    return reporte.fin_hoy or reporte.hoy


def _titulos(reporte: Reporte) -> dict[str, str]:
    """Título de cada sección. Si "hoy" llega hasta el domingo (viernes), lo dice."""
    con_finde = _fin_hoy(reporte) > reporte.hoy
    return {
        "cumples_hoy": "Cumpleaños de hoy y del fin de semana" if con_finde else "Cumpleaños de hoy",
        "cumples_semana": "Cumpleaños de la semana",
        "vencen_hoy": "Cobranzas que vencen hoy y el fin de semana" if con_finde else "Cobranzas que vencen hoy",
        "vencen_semana": "Cobranzas que vencen en la semana",
        "atrasadas": "ATRASADAS",
        "vencen_mas_adelante": (
            f"Cobranzas que vencen más adelante (después del domingo {fmt_dia_mes(reporte.domingo)})"
        ),
        "sin_vencimiento": "Sin fecha de vencimiento",
        "parciales": "Pagos parciales",
        "pagadas_sin_registrar": "Pagadas sin registrar cobro",
    }


def texto_total_general(t: TotalGeneral) -> str:
    texto = f"{TITULO_TOTAL_GENERAL}: ${fmt_plata_es(t.monto)}"
    if t.sin_monto == 1:
        texto += " (1 pedido sin monto cargado no incluido)"
    elif t.sin_monto > 1:
        texto += f" ({t.sin_monto} pedidos sin monto cargado no incluidos)"
    return texto


def armar_secciones(reporte: Reporte) -> tuple[Seccion | BloqueTotal, ...]:
    """Secciones del modo del reporte, en orden, con celdas ya formateadas.

    El orden es el de SECCIONES_POR_MODO. Totales al pie SOLO en vencen hoy,
    semana, ATRASADAS, más adelante y parciales (excluyen "Sin monto"; las
    pagadas sin registrar nunca están en esas secciones), y solo si la sección
    tiene filas: vacía muestra únicamente "Sin registros". "Sin fecha de
    vencimiento" solo aparece si tiene filas. El lunes, el total general va
    justo antes del lugar de la regularidad.
    """
    tot = {k: fmt_plata_es(v) for k, v in totales(reporte).items()}
    titulo = _titulos(reporte)

    def cumples(nombre):
        return _seccion(titulo[nombre], _COL_CUMPLE, map(_fila_cumple, getattr(reporte, nombre)))

    def cobranzas(nombre, **opciones):
        return _cobranzas(titulo[nombre], getattr(reporte, nombre), **opciones)

    armar = {
        "cumples_hoy": lambda: cumples("cumples_hoy"),
        "cumples_semana": lambda: cumples("cumples_semana"),
        "vencen_hoy": lambda: cobranzas("vencen_hoy", total=tot["vencen_hoy"]),
        "vencen_semana": lambda: cobranzas("vencen_semana", total=tot["vencen_semana"]),
        "atrasadas": lambda: cobranzas("atrasadas", dias=True, total=tot["atrasadas"]),
        "vencen_mas_adelante": lambda: cobranzas("vencen_mas_adelante", total=tot["vencen_mas_adelante"]),
        "sin_vencimiento": lambda: cobranzas("sin_vencimiento"),
        "parciales": lambda: cobranzas("parciales", estado=False, total=tot["parciales"]),
        "pagadas_sin_registrar": lambda: cobranzas("pagadas_sin_registrar", estado=False),
    }

    partes: list[Seccion | BloqueTotal] = []
    for nombre in SECCIONES_POR_MODO[reporte.modo]:
        if nombre == "regularidad":
            # El total general va justo antes; la tabla de regularidad todavía no se dibuja.
            partes.append(BloqueTotal(texto_total_general(total_general(reporte))))
            continue
        if nombre == "sin_vencimiento" and not reporte.sin_vencimiento:
            continue
        partes.append(armar[nombre]())
    return tuple(partes)


def subtitulo(reporte: Reporte) -> str:
    """Fecha de negocio y, si corresponde, el rango que cubre el reporte."""
    hoy, fin_hoy = reporte.hoy, _fin_hoy(reporte)
    if reporte.modo == EXTENDIDO:
        return f"{_con_dia(hoy)} · resumen semanal hasta {_con_dia(reporte.domingo)}"
    if fin_hoy > hoy:
        dias = " y ".join(DIAS_ABREV[(hoy + timedelta(days=n)).weekday()] for n in range(1, (fin_hoy - hoy).days + 1))
        return f"{_con_dia(hoy)} · incluye {dias} hasta {_con_dia(fin_hoy)}"
    return _con_dia(hoy)


def titulo_documento(reporte: Reporte) -> str:
    return f"Reporte diario – {_con_dia(reporte.hoy)}"


# ---------------------------------------------------------------- dibujo

_TINTA = (33, 37, 41)
_GRIS = (108, 117, 125)
_ACENTO = (31, 78, 121)
_FONDO_ENCABEZADO = (226, 234, 242)
_FONDO_TOTAL = (245, 245, 245)


class _Documento(FPDF):
    def __init__(self, subtitulo: str, pie: str):
        super().__init__(orientation="L", unit="mm", format="A4")
        self._subtitulo = subtitulo
        self._pie = pie
        self.add_font(FAMILIA, style="", fname=str(FUENTES / "Poppins-Regular.ttf"))
        self.add_font(FAMILIA, style="B", fname=str(FUENTES / "Poppins-SemiBold.ttf"))
        self.set_margins(12, 12, 12)
        self.set_auto_page_break(True, margin=15)

    def header(self):
        self.set_text_color(*_ACENTO)
        self.set_font(FAMILIA, "B", 14)
        self.cell(0, 7, f"{REMITENTE} — Reporte diario", new_x="LMARGIN", new_y="NEXT")
        self.set_text_color(*_GRIS)
        self.set_font(FAMILIA, "", 9.5)
        self.cell(0, 5, self._subtitulo, new_x="LMARGIN", new_y="NEXT")
        self.set_draw_color(*_ACENTO)
        self.set_line_width(0.4)
        self.line(self.l_margin, self.get_y() + 1.5, self.w - self.r_margin, self.get_y() + 1.5)
        self.ln(5)
        self.set_text_color(*_TINTA)

    def footer(self):
        self.set_y(-11)
        self.set_font(FAMILIA, "", 8)
        self.set_text_color(*_GRIS)
        ancho = (self.w - self.l_margin - self.r_margin) / 2
        self.cell(ancho, 5, self._pie, align="L")
        self.cell(ancho, 5, f"Página {self.page_no()} de {{nb}}", align="R")
        self.set_text_color(*_TINTA)


def _dibujar_seccion(pdf: _Documento, s: Seccion) -> None:
    # Que el título no quede huérfano al pie de una página.
    if pdf.will_page_break(22):
        pdf.add_page()
    pdf.set_font(FAMILIA, "B", 11)
    pdf.set_text_color(*_ACENTO)
    pdf.cell(0, 7, s.encabezado, new_x="LMARGIN", new_y="NEXT")
    pdf.set_text_color(*_TINTA)

    if not s.filas:
        pdf.set_font(FAMILIA, "", 9)
        pdf.set_text_color(*_GRIS)
        pdf.cell(0, 6, s.vacia_texto, new_x="LMARGIN", new_y="NEXT")
        pdf.set_text_color(*_TINTA)
        pdf.ln(3)
        return

    pdf.set_font(FAMILIA, "", 8.5)
    with pdf.table(
        col_widths=s.anchos,
        text_align=s.alineacion,
        width=pdf.epw,
        line_height=4.2,
        padding=(0.8, 1.5),
        repeat_headings=1,
        headings_style=FontFace(emphasis="BOLD", fill_color=_FONDO_ENCABEZADO, color=_ACENTO),
        borders_layout="HORIZONTAL_LINES",
        v_align="MIDDLE",
    ) as tabla:
        tabla.row(s.columnas)
        for fila in s.filas:
            tabla.row(fila)
        if s.total is not None:
            estilo = FontFace(emphasis="BOLD", fill_color=_FONDO_TOTAL)
            fila = tabla.row()
            fila.cell("Total saldo", align=_DER, colspan=len(s.columnas) - 1, style=estilo)
            fila.cell(s.total, align=_DER, style=estilo)
    pdf.ln(4)


def _dibujar_total(pdf: _Documento, b: BloqueTotal) -> None:
    if pdf.will_page_break(12):
        pdf.add_page()
    pdf.set_font(FAMILIA, "B", 10.5)
    pdf.set_fill_color(*_FONDO_TOTAL)
    pdf.cell(0, 8, b.texto, fill=True, new_x="LMARGIN", new_y="NEXT")
    pdf.ln(4)


def render_pdf(reporte: Reporte, generado_en: datetime) -> bytes:
    """Dibuja el reporte y devuelve los bytes del PDF.

    `generado_en` es la hora REAL de generación y debe tener zona horaria
    (se muestra en ART en el pie).
    """
    if generado_en.tzinfo is None:
        raise ValueError("generado_en debe tener zona horaria")
    generado_art = generado_en.astimezone(_TZ)

    pie = f"Generado {generado_art:%d/%m/%Y %H:%M} ART"
    pdf = _Documento(subtitulo(reporte), pie)
    pdf.set_title(titulo_documento(reporte))
    pdf.set_author(REMITENTE)
    pdf.set_creator(CREADOR)
    pdf.set_creation_date(generado_en)
    pdf.add_page()

    if reporte.sin_novedades:
        pdf.set_font(FAMILIA, "B", 12)
        pdf.set_fill_color(*_FONDO_ENCABEZADO)
        pdf.cell(0, 10, SIN_NOVEDADES, align="C", fill=True, new_x="LMARGIN", new_y="NEXT")
        pdf.ln(4)

    for parte in armar_secciones(reporte):
        if isinstance(parte, BloqueTotal):
            _dibujar_total(pdf, parte)
        else:
            _dibujar_seccion(pdf, parte)

    return bytes(pdf.output())
