"""PDF renderer driven by a BriefData payload.

Adapted from the proven 4-page visual template at `templates/pdf_builder.py`.
Layout, palette, and component helpers are byte-identical to the v0 template —
only the data binding changed: every previously-hardcoded literal now reads
from the BriefData fields produced by the Author.

Graceful-degradation rules (CLAUDE.md rule 2):
  - badge value=None         → "INSUFFICIENT DATA" in muted gray
  - empty list for any section → section omitted entirely (header + body)
  - <4 stat blocks / cards   → rendered with whatever count is present
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from html import escape as _xml_escape
from itertools import cycle
from pathlib import Path
from typing import Callable


def _html(text: str | None) -> str:
    """Escape model-supplied text for safe embedding in ReportLab's
    XML-like markup. Preserves paragraph breaks (\n\n → <br/><br/>)."""
    if not text:
        return ""
    safe = _xml_escape(text, quote=False)
    return safe.replace("\n\n", "<br/><br/>").replace("\n", "<br/>")

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch

from reportlab.lib.pagesizes import LETTER
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from reportlab.lib.colors import HexColor, white
from reportlab.lib.enums import TA_LEFT, TA_CENTER, TA_JUSTIFY
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle,
    PageBreak, Image, HRFlowable, KeepTogether,
)
from reportlab.pdfgen import canvas

from account_research.schemas.brief import (
    ApproachItem,
    BriefData,
    ContactItem,
    DiscoveryQuestion,
    DnaCard,
    HeroBadge,
    IndustryChip,
    KeySignal,
    MethodologyNote,
    ScorecardRow,
    SourceRef,
    StatBlock,
    StrategicSignal,
    TimelineMilestone,
    WhoTheyAreCard,
)


# ===============================================================
# Palette — identical to the v0 template
# ===============================================================
NAVY     = HexColor("#0F2D4A")
NAVY_DK  = HexColor("#08172A")
ACCENT   = HexColor("#C9A55C")
TEAL     = HexColor("#0E7C7B")
CORAL    = HexColor("#E26D5A")
SLATE    = HexColor("#3A4A5C")
LIGHT    = HexColor("#F4F6F9")
LIGHT_2  = HexColor("#EAEEF4")
BORDER   = HexColor("#D5DBE3")
TEXT     = HexColor("#1F2937")
MUTED    = HexColor("#6B7280")
GREEN    = HexColor("#1F7A4D")
GREEN_BG = HexColor("#E6F2EC")
AMBER    = HexColor("#B7791F")
AMBER_BG = HexColor("#FBF1DE")
GRAY_BG  = HexColor("#EEF1F5")

_PALETTE_CYCLE = (NAVY, TEAL, ACCENT, CORAL, SLATE)
_HEX_CYCLE = ("#0F2D4A", "#0E7C7B", "#C9A55C", "#E26D5A", "#7B8FA1", "#3A4A5C")


# ===============================================================
# Styles
# ===============================================================
_styles = getSampleStyleSheet()

eyebrow = ParagraphStyle("Eyebrow", parent=_styles["Normal"],
    fontName="Helvetica-Bold", fontSize=9, leading=10,
    textColor=ACCENT, spaceAfter=2)

hero_name = ParagraphStyle("HeroName", parent=_styles["Title"],
    fontName="Helvetica-Bold", fontSize=26, leading=30,
    textColor=NAVY, spaceAfter=2, alignment=TA_LEFT)

hero_role = ParagraphStyle("HeroRole", parent=_styles["Normal"],
    fontName="Helvetica", fontSize=11, leading=14,
    textColor=SLATE, spaceAfter=6, alignment=TA_LEFT)

h1 = ParagraphStyle("H1", parent=_styles["Heading1"],
    fontName="Helvetica-Bold", fontSize=14, leading=17,
    textColor=NAVY, spaceBefore=4, spaceAfter=4)

body = ParagraphStyle("Body", parent=_styles["Normal"],
    fontName="Helvetica", fontSize=9.5, leading=13,
    textColor=TEXT, spaceAfter=4)

body_just = ParagraphStyle("BodyJ", parent=body, alignment=TA_JUSTIFY)

big_stat_num = ParagraphStyle("StatNum", parent=_styles["Normal"],
    fontName="Helvetica-Bold", fontSize=24, leading=26,
    textColor=NAVY, alignment=TA_CENTER, spaceAfter=2)

big_stat_lbl = ParagraphStyle("StatLbl", parent=_styles["Normal"],
    fontName="Helvetica-Bold", fontSize=7.5, leading=10,
    textColor=SLATE, alignment=TA_CENTER, spaceAfter=0)

card_title = ParagraphStyle("CardTitle", parent=_styles["Normal"],
    fontName="Helvetica-Bold", fontSize=10, leading=12,
    textColor=NAVY, spaceAfter=3)

card_body = ParagraphStyle("CardBody", parent=body,
    fontSize=9, leading=12, spaceAfter=0)

chip_style = ParagraphStyle("Chip", parent=_styles["Normal"],
    fontName="Helvetica-Bold", fontSize=8.5, leading=10,
    textColor=NAVY, alignment=TA_CENTER)


# ===============================================================
# Component helpers — byte-identical to v0 template
# ===============================================================
def _net_worth_badge(amount: str | None, label: str = "EST. REVENUE",
                     unit: str = "USD/yr", diameter: float = 1.15 * inch):
    """Hero badge. amount=None renders 'INSUFFICIENT DATA' in muted gray."""
    from reportlab.graphics.shapes import Drawing, Circle, String
    d = Drawing(diameter, diameter)
    r = diameter / 2
    fill = MUTED if amount is None else NAVY
    d.add(Circle(r, r, r, fillColor=fill, strokeColor=ACCENT, strokeWidth=2))
    d.add(String(r, r + 14, label,
                 fontName="Helvetica-Bold", fontSize=6.5,
                 fillColor=ACCENT, textAnchor="middle"))
    if amount is None:
        d.add(String(r, r - 4, "INSUFFICIENT",
                     fontName="Helvetica-Bold", fontSize=9,
                     fillColor=white, textAnchor="middle"))
        d.add(String(r, r - 16, "DATA",
                     fontName="Helvetica-Bold", fontSize=9,
                     fillColor=white, textAnchor="middle"))
    else:
        d.add(String(r, r - 4, amount,
                     fontName="Helvetica-Bold", fontSize=14,
                     fillColor=white, textAnchor="middle"))
        d.add(String(r, r - 18, unit,
                     fontName="Helvetica-Bold", fontSize=6.5,
                     fillColor=ACCENT, textAnchor="middle"))
    return d


def _stat_block(number: str, label: str, color=NAVY, bg=LIGHT):
    inner = Table(
        [[Paragraph(f'<font color="{color.hexval()}">{number}</font>', big_stat_num)],
         [Paragraph(label, big_stat_lbl)]],
        colWidths=[1.65 * inch],
    )
    inner.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), bg),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 8),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
        ("BOX", (0, 0), (-1, -1), 0.5, BORDER),
        ("LINEABOVE", (0, 0), (-1, 0), 3, color),
    ]))
    return inner


def _stat_row(stats: list[tuple[str, str, object]]):
    if not stats:
        return None
    cells = [_stat_block(n, l, c) for (n, l, c) in stats]
    t = Table([cells], colWidths=[1.78 * inch] * len(stats))
    t.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 3),
        ("RIGHTPADDING", (0, 0), (-1, -1), 3),
        ("TOPPADDING", (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
    ]))
    return t


def _chip(text: str, bg=LIGHT_2, fg=NAVY):
    p = Paragraph(f'<font color="{fg.hexval()}">{text}</font>', chip_style)
    t = Table([[p]], colWidths=[None])
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), bg),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("BOX", (0, 0), (-1, -1), 0.5, BORDER),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
    ]))
    return t


def _chip_grid(labels: list[str], cols: int = 4, col_w: float = 1.83 * inch):
    if not labels:
        return None
    rows = []
    for i in range(0, len(labels), cols):
        row = [_chip(x) for x in labels[i:i + cols]]
        while len(row) < cols:
            row.append("")
        rows.append(row)
    t = Table(rows, colWidths=[col_w] * cols)
    t.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 3),
        ("RIGHTPADDING", (0, 0), (-1, -1), 3),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]))
    return t


def _signal_meter(strength: int, color, max_: int = 5):
    from reportlab.graphics.shapes import Drawing, Rect
    cell_w, cell_h, gap = 14, 8, 3
    d = Drawing((cell_w + gap) * max_, cell_h + 2)
    for i in range(max_):
        c = color if i < strength else BORDER
        d.add(Rect(i * (cell_w + gap), 1, cell_w, cell_h,
                   fillColor=c, strokeColor=None))
    return d


def _signal_card(symbol: str, sym_color, text: str, bg=LIGHT, accent=NAVY):
    sym = Paragraph(
        f'<font color="{sym_color.hexval()}" size="13"><b>{symbol}</b></font>',
        ParagraphStyle("Sym", parent=body, fontSize=13, leading=15, alignment=TA_CENTER))
    body_p = Paragraph(text, card_body)
    inner = Table([[sym, body_p]], colWidths=[0.32 * inch, 6.95 * inch])
    inner.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("BACKGROUND", (0, 0), (-1, -1), bg),
        ("LINEBEFORE", (0, 0), (0, -1), 3, accent),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
    ]))
    return inner


def _hero_callout(text_paragraph, accent=ACCENT, bg=NAVY):
    inner = Table([[text_paragraph]], colWidths=[7.3 * inch])
    inner.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), bg),
        ("LEFTPADDING", (0, 0), (-1, -1), 16),
        ("RIGHTPADDING", (0, 0), (-1, -1), 16),
        ("TOPPADDING", (0, 0), (-1, -1), 14),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 14),
        ("LINEBEFORE", (0, 0), (0, -1), 5, accent),
    ]))
    return inner


def _section_header(label: str, accent=ACCENT):
    p = Paragraph(label.upper(), ParagraphStyle(
        "SectionEye", parent=eyebrow, textColor=accent, fontSize=9))
    rule = HRFlowable(width="100%", thickness=0.6, color=BORDER,
                      spaceBefore=2, spaceAfter=4)
    return [p, rule]


def _mini_card(title: str, body_text: str, accent=NAVY, bg=LIGHT):
    inner = Table([
        [Paragraph(title, card_title)],
        [Paragraph(body_text, card_body)],
    ], colWidths=[3.5 * inch])
    inner.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), bg),
        ("LEFTPADDING", (0, 0), (-1, -1), 10),
        ("RIGHTPADDING", (0, 0), (-1, -1), 10),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("LINEBEFORE", (0, 0), (0, -1), 3, accent),
    ]))
    return inner


def _card_row(cards: list, col_w: float = 3.7):
    if not cards:
        return None
    if len(cards) == 1:
        # single card spans the row
        t = Table([[cards[0]]], colWidths=[col_w * 2 * inch])
    else:
        t = Table([cards], colWidths=[col_w * inch] * len(cards))
    t.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 2),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
    ]))
    return t


# ===============================================================
# Matplotlib images — timeline + footprint
# ===============================================================
def _build_timeline_image(milestones: list[TimelineMilestone], out_path: Path) -> Path | None:
    if not milestones:
        return None

    years_end = max((m.end_year or m.start_year for m in milestones), default=datetime.now().year)
    years_start = min(m.start_year for m in milestones)
    span = max(years_end - years_start, 1)

    fig, ax = plt.subplots(figsize=(10.5, max(2.2, 0.55 * len(milestones) + 1)), dpi=200)
    fig.patch.set_facecolor("white")

    y_positions = list(range(len(milestones), 0, -1))
    bar_h = 0.55
    color_cycle = cycle(_HEX_CYCLE)

    for y, m in zip(y_positions, milestones):
        start = m.start_year
        end = m.end_year or (years_end + 0.35)
        color = m.color_hint or next(color_cycle)
        ax.barh(y, end - start, left=start, height=bar_h,
                color=color, edgecolor="white", linewidth=1.5, zorder=3)
        descr = m.description or ""
        if (end - start) >= 1.5 and descr:
            ax.text(start + 0.08, y, descr, va="center", ha="left",
                    fontsize=9.5, color="white", fontweight="bold", zorder=4)
        elif descr:
            ax.text(end + 0.1, y, descr, va="center", ha="left",
                    fontsize=9.5, color="#1F2937", fontweight="bold", zorder=4)
        ax.text(start, y - 0.42, m.label, va="top", ha="left",
                fontsize=8, color="#6B7280", style="italic", zorder=4)

    ax.set_xlim(years_start - 0.5, years_end + 0.7)
    ax.set_ylim(0.2, len(milestones) + 1.1)
    ax.set_yticks([])
    ticks = list(range(years_start, int(years_end) + 1))
    ax.set_xticks(ticks)
    ax.set_xticklabels([str(t) for t in ticks], fontsize=9, color="#3A4A5C")
    ax.tick_params(axis="x", length=0, pad=6)
    for s in ["top", "right", "left"]:
        ax.spines[s].set_visible(False)
    ax.spines["bottom"].set_color("#D5DBE3")
    ax.grid(axis="x", color="#EEF1F5", linewidth=0.8, zorder=1)

    plt.tight_layout()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    plt.savefig(str(out_path), dpi=200, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    return out_path


def _build_footprint_image(brief: BriefData, out_path: Path) -> Path | None:
    locs = brief.geographic_footprint
    if not locs:
        return None

    fig, ax = plt.subplots(figsize=(9, 3.0), dpi=200)
    fig.patch.set_facecolor("white")
    ax.set_xlim(0, 1); ax.set_ylim(0, 1)
    ax.axis("off")

    bg = FancyBboxPatch((0.04, 0.10), 0.92, 0.80,
                        boxstyle="round,pad=0.01,rounding_size=0.04",
                        linewidth=0, facecolor="#F4F6F9", zorder=1)
    ax.add_patch(bg)

    ax.text(0.5, 0.96, "GEOGRAPHIC DELIVERY FOOTPRINT", ha="center",
            fontsize=9, color="#6B7280", fontweight="bold")

    n = len(locs)
    color_cycle = cycle(_HEX_CYCLE)
    # spread across the inner box: x from 0.12 to 0.88, varying y
    for i, loc in enumerate(locs):
        x = 0.12 + (0.76 * i / max(n - 1, 1)) if n > 1 else 0.5
        y = 0.65 if i % 2 == 0 else 0.35
        color = next(color_cycle)
        count = loc.project_count
        label = loc.location + (" (HQ)" if loc.is_hq and "(HQ)" not in loc.location else "")
        ax.scatter([x], [y], s=800, color=color, alpha=0.18, zorder=2)
        size = 220 + (count or 0) * 5
        ax.scatter([x], [y], s=size, color=color, zorder=3,
                   edgecolor="white", linewidth=2)
        if count is not None:
            ax.text(x, y, str(count), ha="center", va="center",
                    fontsize=10, color="white", fontweight="bold", zorder=4)
        ax.annotate(label, xy=(x, y), xytext=(x, y - 0.14),
                    ha="center", fontsize=8.5, color="#1F2937", fontweight="bold")

    if any(loc.project_count is not None for loc in locs):
        footer = f"{sum((l.project_count or 0) for l in locs)} total projects cited across {n} locations"
    else:
        footer = f"{n} location(s) cited"
    ax.text(0.5, 0.03, footer, ha="center", fontsize=9, color="#3A4A5C", style="italic")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    plt.savefig(str(out_path), dpi=200, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    return out_path


# ===============================================================
# Page chrome
# ===============================================================
def _make_chrome_callback(footer_left: str) -> Callable:
    def draw(canv: canvas.Canvas, doc):
        canv.saveState()
        width, _ = LETTER
        canv.setStrokeColor(BORDER)
        canv.setLineWidth(0.5)
        canv.line(0.5 * inch, 0.5 * inch, width - 0.5 * inch, 0.5 * inch)
        canv.setFillColor(MUTED)
        canv.setFont("Helvetica", 8)
        canv.drawString(0.5 * inch, 0.32 * inch, footer_left)
        today = datetime.now(timezone.utc).strftime("%b %d, %Y")
        canv.drawRightString(width - 0.5 * inch, 0.32 * inch,
                             f"{today}  ·  Page {doc.page}")
        canv.restoreState()
    return draw


# ===============================================================
# Section builders — each returns a list of flowables OR [] when omitted
# ===============================================================
def _section_hero(brief: BriefData) -> list:
    badge_data: HeroBadge | None = brief.hero.badge
    if badge_data is None:
        badge = _net_worth_badge(None)
    else:
        badge = _net_worth_badge(
            badge_data.value,
            label=badge_data.label,
            unit=badge_data.unit or "",
        )

    role_text = brief.hero.tagline or ""
    hero_right = [
        Paragraph("ACCOUNT RESEARCH BRIEF", eyebrow),
        Paragraph(_html(brief.hero.name), hero_name),
    ]
    if role_text:
        hero_right.append(Paragraph(_html(role_text), hero_role))

    hero = Table([[badge, hero_right]], colWidths=[1.3 * inch, 6.1 * inch])
    hero.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
    ]))
    flowables = [hero, Spacer(1, 4)]
    if badge_data and badge_data.caveat:
        flowables.append(Paragraph(
            f'<font color="#6B7280" size="8"><i>{_html(badge_data.caveat)}</i></font>',
            ParagraphStyle("BadgeCaveat", parent=body, fontSize=8,
                           leading=10, textColor=MUTED, alignment=TA_LEFT)))
    flowables.append(HRFlowable(width="100%", thickness=1.5, color=ACCENT,
                                spaceBefore=4, spaceAfter=8))
    return flowables


def _section_quick_take(brief: BriefData) -> list:
    qt = brief.quick_take
    body_safe = _html(qt.body)
    parts = [
        '<font color="#C9A55C"><b>QUICK TAKE</b></font><br/><br/>',
        f'<font color="#FFFFFF" size="11">{body_safe}</font>',
    ]
    if qt.best_angle:
        parts.append(
            "<br/><br/>"
            '<font color="#C9A55C"><b>BEST ANGLE:</b></font> '
            f'<font color="#FFFFFF" size="11">{_html(qt.best_angle)}</font>'
        )
    qt_para = Paragraph("".join(parts),
        ParagraphStyle("QTHero", parent=body, fontSize=10.5, leading=14, textColor=white))
    return [_hero_callout(qt_para, accent=ACCENT, bg=NAVY), Spacer(1, 10)]


def _section_contacts(contacts: list[ContactItem]) -> list:
    """Page-1 outreach contacts panel (rendered below quick-take + best-angle).

    Each contact is a small card: name (bold) · title (muted) over two
    "icon line" rows for email and phone. Omitted entirely when the list
    is empty. A tiny caveat footer credits the source and reminds the
    operator to verify before outreach.
    """
    if not contacts:
        return []

    contact_name_style = ParagraphStyle(
        "ContactName", parent=body, fontSize=10, leading=12,
        textColor=NAVY, fontName="Helvetica-Bold", spaceAfter=1,
    )
    contact_title_style = ParagraphStyle(
        "ContactTitle", parent=body, fontSize=8.5, leading=10,
        textColor=MUTED, spaceAfter=4,
    )
    contact_meta_style = ParagraphStyle(
        "ContactMeta", parent=body, fontSize=8.5, leading=11,
        textColor=TEXT, spaceAfter=1,
    )

    def _card(c: ContactItem):
        rows: list = [[Paragraph(_html(c.name), contact_name_style)]]
        if c.title:
            rows.append([Paragraph(_html(c.title), contact_title_style)])
        if c.email:
            rows.append([Paragraph(
                f'<font color="#3A4A5C">✉&#160;&#160;</font>'
                f'<link href="mailto:{_xml_escape(c.email)}">'
                f'<font color="#0F2D4A">{_html(c.email)}</font></link>',
                contact_meta_style,
            )])
        if c.phone:
            rows.append([Paragraph(
                f'<font color="#3A4A5C">☎&#160;&#160;</font>'
                f'<font color="#1F2937">{_html(c.phone)}</font>',
                contact_meta_style,
            )])
        if c.linkedin_url:
            url = str(c.linkedin_url)
            rows.append([Paragraph(
                f'<font color="#3A4A5C">in&#160;&#160;</font>'
                f'<link href="{_xml_escape(url)}">'
                f'<font color="#0F2D4A">LinkedIn</font></link>',
                contact_meta_style,
            )])
        # If email/phone/linkedin all missing, hint that.
        if len(rows) == 1 + (1 if c.title else 0):
            rows.append([Paragraph(
                '<font color="#6B7280" size="8"><i>No public contact info — '
                'enrich before outreach.</i></font>',
                contact_meta_style,
            )])
        inner = Table(rows, colWidths=[2.36 * inch])
        inner.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), LIGHT),
            ("LEFTPADDING", (0, 0), (-1, -1), 10),
            ("RIGHTPADDING", (0, 0), (-1, -1), 10),
            ("TOPPADDING", (0, 0), (-1, -1), 6),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
            ("LINEBEFORE", (0, 0), (0, -1), 3, TEAL),
        ]))
        return inner

    cells = [_card(c) for c in contacts[:3]]
    # Pad to 3 columns so layout doesn't stretch when fewer than 3 contacts.
    while len(cells) < 3:
        cells.append("")
    grid = Table([cells], colWidths=[2.46 * inch, 2.46 * inch, 2.46 * inch])
    grid.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
    ]))

    source = contacts[0].source if contacts and contacts[0].source else "external provider"
    caveat = Paragraph(
        f'<font color="#6B7280" size="7.5"><i>Sourced via {_html(source)} '
        f'· verify before outreach</i></font>',
        ParagraphStyle("ContactCaveat", parent=body, fontSize=7.5,
                       leading=9, textColor=MUTED, alignment=TA_LEFT),
    )

    return [
        *_section_header("KEY CONTACTS"),
        grid,
        Spacer(1, 3),
        caveat,
        Spacer(1, 10),
    ]


def _section_stats(stats: list[StatBlock]) -> list:
    if not stats:
        return []
    palette_cycle = cycle(_PALETTE_CYCLE)
    rows = [(_html(s.value), _html(s.label), next(palette_cycle))
            for s in stats[:4]]
    row = _stat_row(rows)
    return [row, Spacer(1, 6)] if row else []


def _section_timeline(brief: BriefData, image_path: Path | None) -> list:
    if not brief.timeline or image_path is None:
        return []
    return [
        *_section_header("COMPANY TRAJECTORY"),
        Image(str(image_path), width=7.4 * inch, height=1.55 * inch),
        Spacer(1, 4),
    ]


def _cards_from(items: list[WhoTheyAreCard | DnaCard | StrategicSignal], palette) -> list:
    result = []
    palette_iter = cycle(palette)
    for item in items:
        result.append(_mini_card(_html(item.heading), _html(item.body),
                                 accent=next(palette_iter)))
    return result


def _section_who_they_are(cards: list[WhoTheyAreCard]) -> list:
    if not cards:
        return []
    rendered = _cards_from(cards, [NAVY, ACCENT, TEAL, CORAL])
    flowables = list(_section_header("WHO THEY ARE"))
    for i in range(0, len(rendered), 2):
        pair = rendered[i:i + 2]
        row = _card_row(pair)
        if row is not None:
            flowables.append(row)
    return flowables


def _section_dna(cards: list[DnaCard], header: str) -> list:
    if not cards:
        return []
    rendered = []
    palette_iter = cycle([TEAL, CORAL, NAVY, ACCENT])
    for c in cards:
        rendered.append(_mini_card(_html(c.trait), _html(c.detail),
                                   accent=next(palette_iter)))
    flowables = list(_section_header(header))
    for i in range(0, len(rendered), 2):
        pair = rendered[i:i + 2]
        row = _card_row(pair)
        if row is not None:
            flowables.append(row)
    return flowables


def _section_industries(industries: list[IndustryChip]) -> list:
    if not industries:
        return []
    labels = [_html(i.name) for i in industries]
    grid = _chip_grid(labels, cols=4)
    return [*_section_header("INDUSTRIES SERVED"), grid, Spacer(1, 14)] if grid else []


def _section_footprint(brief: BriefData, image_path: Path | None) -> list:
    if not brief.geographic_footprint or image_path is None:
        return []
    return [
        *_section_header("GEOGRAPHIC DELIVERY FOOTPRINT"),
        Image(str(image_path), width=7.0 * inch, height=2.3 * inch),
        Spacer(1, 10),
    ]


def _section_strategic_signals(signals: list[StrategicSignal]) -> list:
    if not signals:
        return []
    rendered = _cards_from(signals, [NAVY, TEAL, ACCENT, CORAL])
    flowables = list(_section_header("STRATEGIC SIGNALS  ·  CURRENT POSTURE"))
    for i in range(0, len(rendered), 2):
        pair = rendered[i:i + 2]
        row = _card_row(pair)
        if row is not None:
            flowables.append(row)
    return flowables


_SCORE_COLOR = {1: AMBER, 2: AMBER, 3: ACCENT, 4: NAVY, 5: GREEN}


def _section_scorecard(rows: list[ScorecardRow]) -> list:
    if not rows:
        return []

    def _row(label: str, strength: int, note: str):
        color = _SCORE_COLOR.get(strength, NAVY)
        meter = _signal_meter(strength, color)
        label_p = Paragraph(f"<b>{_html(label)}</b>", body)
        note_p = Paragraph(f'<font color="#6B7280" size="8.5">{_html(note)}</font>',
                           ParagraphStyle("Note", parent=body, fontSize=8.5,
                                          leading=11, textColor=MUTED))
        return [label_p, meter, note_p]

    score_rows = [_row(r.metric, r.score, r.rationale) for r in rows]
    scorecard = Table(score_rows, colWidths=[1.95 * inch, 1.6 * inch, 3.75 * inch])
    scorecard.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
        ("TOPPADDING", (0, 0), (-1, -1), 8),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
        ("LINEBELOW", (0, 0), (-1, -2), 0.4, BORDER),
        ("BACKGROUND", (0, 0), (-1, -1), LIGHT),
        ("BOX", (0, 0), (-1, -1), 0.5, BORDER),
    ]))
    return [*_section_header("ENGAGEMENT READINESS SCORECARD"),
            scorecard, Spacer(1, 10)]


_SIGNAL_SYMBOLS = (
    ("✔", GREEN, GREEN_BG, GREEN),
    ("✔", GREEN, GREEN_BG, GREEN),
    ("!",      AMBER, AMBER_BG, AMBER),
    ("?",      MUTED, GRAY_BG, MUTED),
)


def _section_key_signals(signals: list[KeySignal]) -> list:
    if not signals:
        return []
    flowables = list(_section_header("KEY SIGNALS"))
    for i, s in enumerate(signals):
        sym, sym_c, bg, accent = _SIGNAL_SYMBOLS[min(i, len(_SIGNAL_SYMBOLS) - 1)]
        flowables.append(_signal_card(
            sym, sym_c,
            f"<b>{_html(s.label)}:</b> {_html(s.body)}",
            bg=bg, accent=accent,
        ))
        flowables.append(Spacer(1, 4))
    flowables.append(Spacer(1, 6))
    return flowables


def _section_recommended_approach(items: list[ApproachItem]) -> list:
    if not items:
        return []
    chunks = []
    for i, it in enumerate(items):
        chunks.append(f'<font color="#C9A55C"><b>{_html(it.heading).upper()}</b></font><br/>')
        sep = "<br/><br/>" if i < len(items) - 1 else ""
        chunks.append(f'<font color="#FFFFFF" size="11">{_html(it.body)}</font>{sep}')
    para = Paragraph("".join(chunks),
        ParagraphStyle("RAHero", parent=body, fontSize=10.5, leading=15, textColor=white))
    return [*_section_header("RECOMMENDED APPROACH"),
            _hero_callout(para, accent=ACCENT, bg=NAVY_DK),
            Spacer(1, 6)]


def _section_discovery_questions(qs: list[DiscoveryQuestion]) -> list:
    if not qs:
        return []
    palette_iter = cycle([NAVY, TEAL, ACCENT, CORAL])

    def _q_card(num: str, text: str, color):
        num_p = Paragraph(
            f'<font color="#FFFFFF" size="14"><b>{num}</b></font>',
            ParagraphStyle("QNum", parent=body, fontSize=14,
                           alignment=TA_CENTER, textColor=white))
        text_p = Paragraph(text, body)
        num_cell = Table([[num_p]], colWidths=[0.4 * inch], rowHeights=[0.4 * inch])
        num_cell.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), color),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("LEFTPADDING", (0, 0), (-1, -1), 0),
            ("RIGHTPADDING", (0, 0), (-1, -1), 0),
            ("TOPPADDING", (0, 0), (-1, -1), 0),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
        ]))
        inner = Table([[num_cell, text_p]], colWidths=[0.5 * inch, 6.85 * inch])
        inner.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("BACKGROUND", (1, 0), (1, -1), LIGHT),
            ("LEFTPADDING", (0, 0), (0, -1), 0),
            ("LEFTPADDING", (1, 0), (1, -1), 12),
            ("RIGHTPADDING", (0, 0), (-1, -1), 12),
            ("TOPPADDING", (1, 0), (1, -1), 8),
            ("BOTTOMPADDING", (1, 0), (1, -1), 8),
        ]))
        return inner

    flowables = list(_section_header("DISCOVERY QUESTIONS"))
    for i, q in enumerate(qs, start=1):
        flowables.append(_q_card(str(i), _html(q.question), next(palette_iter)))
        flowables.append(Spacer(1, 4))
    return flowables


def _section_recap_stats(stats: list) -> list:
    if not stats:
        return []
    palette_cycle = cycle(_PALETTE_CYCLE)
    rows = [(_html(s.value), _html(s.label), next(palette_cycle))
            for s in stats[:4]]
    row = _stat_row(rows)
    return [*_section_header("BRIEF AT A GLANCE"), Spacer(1, 4), row, Spacer(1, 16)] if row else []


def _section_next_steps(steps: list) -> list:
    if not steps:
        return []
    body_html = "<br/><br/>".join(_html(s.body) for s in steps)
    para = Paragraph(
        f'<font color="#FFFFFF" size="11">{body_html}</font>',
        ParagraphStyle("NSHero", parent=body, fontSize=10.5, leading=15, textColor=white))
    return [*_section_header("NEXT STEPS"),
            _hero_callout(para, accent=ACCENT, bg=NAVY),
            Spacer(1, 16)]


def _section_sources(sources: list[SourceRef]) -> list:
    if not sources:
        return []
    src_card_style = ParagraphStyle("SrcCard", parent=body,
        fontSize=9, leading=12, textColor=NAVY, spaceAfter=0)
    src_url_style = ParagraphStyle("SrcUrl", parent=body,
        fontSize=7.5, leading=9, textColor=MUTED, spaceAfter=0)

    cells = []
    for src in sources:
        url = str(src.url)
        inner = Table([
            [Paragraph(f'<b><link href="{_xml_escape(url)}"><font color="#0F2D4A">{_html(src.title)}</font></link></b>',
                       src_card_style)],
            [Paragraph(_xml_escape(url[:60]) + ("&#8230;" if len(url) > 60 else ""), src_url_style)],
        ], colWidths=[3.55 * inch])
        inner.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), LIGHT),
            ("LEFTPADDING", (0, 0), (-1, -1), 8),
            ("RIGHTPADDING", (0, 0), (-1, -1), 8),
            ("TOPPADDING", (0, 0), (-1, -1), 6),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
            ("LINEBEFORE", (0, 0), (0, -1), 2, ACCENT),
        ]))
        cells.append(inner)
    if len(cells) % 2 == 1:
        cells.append("")
    rows = [cells[i:i + 2] for i in range(0, len(cells), 2)]
    src_table = Table(rows, colWidths=[3.7 * inch, 3.7 * inch])
    src_table.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    return [*_section_header("SOURCES"), src_table, Spacer(1, 16)]


def _section_methodology(notes: list[MethodologyNote], brief: BriefData) -> list:
    if not notes and brief.confidence_report is None:
        return []
    lines = []
    if notes:
        lines.append(
            "<b>Methodology:</b> " + " ".join(
                f"({_html(n.method_id)}) {_html(n.summary)}" for n in notes
            )
        )
    if brief.confidence_report is not None:
        cr = brief.confidence_report
        lines.append(
            f"<b>Confidence Report:</b> {cr.total_facts} facts, "
            f"{cr.verified} verified, {cr.estimated} estimated, "
            f"{cr.unverifiable} unverifiable, {cr.source_dead} source-dead."
        )
    html = '<br/><br/>'.join(lines)
    method_p = Paragraph(
        f'<font color="#6B7280" size="8.5"><i>{html}</i></font>',
        ParagraphStyle("Method", parent=body, fontSize=8.5, leading=11,
                       textColor=MUTED, alignment=TA_LEFT))
    method_box = Table([[method_p]], colWidths=[7.4 * inch])
    method_box.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), LIGHT_2),
        ("LEFTPADDING", (0, 0), (-1, -1), 12),
        ("RIGHTPADDING", (0, 0), (-1, -1), 12),
        ("TOPPADDING", (0, 0), (-1, -1), 10),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 10),
        ("BOX", (0, 0), (-1, -1), 0.5, BORDER),
    ]))
    return [method_box]


# ===============================================================
# Top-level builder
# ===============================================================
def build_brief(brief: BriefData, out_path: str | Path) -> Path:
    """Render `brief` to a 4-page PDF at `out_path`. Returns the resolved path."""
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    asset_dir = out_path.parent

    timeline_path = _build_timeline_image(
        brief.timeline, asset_dir / f"{out_path.stem}_timeline.png"
    )
    footprint_path = _build_footprint_image(
        brief, asset_dir / f"{out_path.stem}_footprint.png"
    )

    story: list = []

    # ---- Page 1
    story.extend(_section_hero(brief))
    story.extend(_section_quick_take(brief))
    story.extend(_section_contacts(brief.contacts))
    story.extend(_section_stats(brief.stats))
    story.extend(_section_timeline(brief, timeline_path))
    story.extend(_section_who_they_are(brief.who_they_are_cards))
    story.extend(_section_dna(brief.dna_cards,
                              header="COMPANY DNA" if brief.hero.entity_type.value == "company"
                                                   else "PERSONAL DNA"))

    story.append(PageBreak())

    # ---- Page 2
    story.extend(_section_industries(brief.industries))
    story.extend(_section_footprint(brief, footprint_path))
    story.extend(_section_strategic_signals(brief.strategic_signals))

    story.append(PageBreak())

    # ---- Page 3
    story.extend(_section_scorecard(brief.scorecard))
    story.extend(_section_key_signals(brief.key_signals))
    story.extend(_section_recommended_approach(brief.recommended_approach))
    story.extend(_section_discovery_questions(brief.discovery_questions))

    story.append(PageBreak())

    # ---- Page 4
    story.extend(_section_recap_stats(brief.recap_stats))
    story.extend(_section_next_steps(brief.next_steps))
    story.extend(_section_sources(brief.sources))
    story.extend(_section_methodology(brief.methodology, brief))

    footer_left = (
        f"ACCOUNT RESEARCH BRIEF  ·  {brief.hero.name}"
        + (f"  ·  {brief.hero.tagline}" if brief.hero.tagline else "")
    )
    chrome = _make_chrome_callback(footer_left)

    doc = SimpleDocTemplate(
        str(out_path), pagesize=LETTER,
        leftMargin=0.5 * inch, rightMargin=0.5 * inch,
        topMargin=0.5 * inch, bottomMargin=0.65 * inch,
        title=f"{brief.hero.name} — Visual Research Brief",
        author="Account Research",
    )
    doc.build(story, onFirstPage=chrome, onLaterPages=chrome)
    return out_path
