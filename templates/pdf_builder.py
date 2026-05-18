"""
Visual research brief: Guillermo Jaime Calderón — Grupo MIA / Social Global Leaders.
Reuses the same visual template established for the Plasencia brief.
"""

import os
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch

from reportlab.lib.pagesizes import LETTER
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from reportlab.lib.colors import HexColor, white, black
from reportlab.lib.enums import TA_LEFT, TA_CENTER, TA_JUSTIFY
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle,
    PageBreak, Image, HRFlowable,
)
from reportlab.pdfgen import canvas

_HERE = os.path.dirname(os.path.abspath(__file__))
OUT_DIR  = os.environ.get("PDF_OUT_DIR", os.path.join(_HERE, "..", "outputs", "golden"))
PDF_PATH = os.path.join(OUT_DIR, "BW_Project_Management_Visual_Brief.pdf")
TIMELINE_IMG  = os.path.join(OUT_DIR, "bwpm_timeline.png")
FOOTPRINT_IMG = os.path.join(OUT_DIR, "bwpm_footprint.png")
os.makedirs(OUT_DIR, exist_ok=True)

# Palette — keep consistent with prior brief
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

# ===============================================================
# 1) Career timeline
# ===============================================================
def build_timeline():
    roles = [
        # (label, milestone, start, end, color)
        ("Founded in DR",            "Launch — Santo Domingo",       2020, 2021, "#0F2D4A"),
        ("PMI ATP achieved",         "Authorized Training Partner",  2021, 2022, "#0E7C7B"),
        ("LATAM expansion",          "Panama, Jamaica clients",      2022, 2024, "#C9A55C"),
        ("ClickUp exclusive RD",     "Strategic platform partner",   2023, 2026.35, "#E26D5A"),
        ("BW Academy launch",        "Training arm — PMI-certified", 2023, 2026.35, "#7B8FA1"),
        ("50+ projects milestone",   "Global delivery footprint",    2024, 2026.35, "#0F2D4A"),
    ]

    fig, ax = plt.subplots(figsize=(10.5, 3.3), dpi=200)
    fig.patch.set_facecolor("white")

    y_positions = list(range(len(roles), 0, -1))
    bar_h = 0.55

    for y, (label, company, start, end, color) in zip(y_positions, roles):
        ax.barh(y, end - start, left=start, height=bar_h,
                color=color, edgecolor="white", linewidth=1.5, zorder=3)
        if (end - start) >= 1.5:
            ax.text(start + 0.08, y, company, va="center", ha="left",
                    fontsize=9.5, color="white", fontweight="bold", zorder=4)
        else:
            ax.text(end + 0.1, y, company, va="center", ha="left",
                    fontsize=9.5, color="#1F2937", fontweight="bold", zorder=4)
        ax.text(start, y - 0.42, label, va="top", ha="left",
                fontsize=8, color="#6B7280", style="italic", zorder=4)

    milestones = [
        (2020, "Founded",          "#0F2D4A", len(roles) + 0.55),
        (2023, "ClickUp partner",  "#E26D5A", len(roles) + 0.55),
    ]
    for x, txt, c, ypos in milestones:
        ax.axvline(x, ymin=0.05, ymax=0.95, color=c, linestyle=":",
                   linewidth=1.2, alpha=0.6, zorder=2)
        ax.annotate(txt, xy=(x, ypos), ha="center",
                    fontsize=8, color=c, fontweight="bold")

    ax.set_xlim(2019.5, 2026.7)
    ax.set_ylim(0.2, len(roles) + 1.1)
    ax.set_yticks([])
    ax.set_xticks([2020, 2021, 2022, 2023, 2024, 2025, 2026])
    ax.set_xticklabels(["2020", "2021", "2022", "2023", "2024", "2025", "2026"],
                       fontsize=9, color="#3A4A5C")
    ax.tick_params(axis="x", length=0, pad=6)
    for s in ["top", "right", "left"]:
        ax.spines[s].set_visible(False)
    ax.spines["bottom"].set_color("#D5DBE3")
    ax.grid(axis="x", color="#EEF1F5", linewidth=0.8, zorder=1)

    plt.tight_layout()
    plt.savefig(TIMELINE_IMG, dpi=200, bbox_inches="tight", facecolor="white")
    plt.close(fig)


def build_footprint():
    cities = [
        # (label, x, y, count, color, label_offset_y)
        ("Dominican Republic (HQ)",   0.30, 0.65, 50, "#0F2D4A", -0.14),
        ("Panama",                    0.20, 0.30, 5,  "#0E7C7B", -0.14),
        ("Jamaica",                   0.42, 0.50, 3,  "#C9A55C", -0.14),
        ("LATAM (regional)",          0.60, 0.75, 8,  "#E26D5A", -0.14),
        ("Global (remote delivery)",  0.78, 0.40, 4,  "#7B8FA1", -0.14),
    ]

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

    for label, x, y, count, color, dy in cities:
        ax.scatter([x], [y], s=800, color=color, alpha=0.18, zorder=2)
        size = 220 + count * 5
        ax.scatter([x], [y], s=size, color=color, zorder=3,
                   edgecolor="white", linewidth=2)
        ax.text(x, y, str(count), ha="center", va="center",
                fontsize=10, color="white", fontweight="bold", zorder=4)
        ax.annotate(label, xy=(x, y), xytext=(x, y + dy),
                    ha="center", fontsize=8.5, color="#1F2937",
                    fontweight="bold")

    ax.text(0.5, 0.03,
            "50+ projects delivered  ·  10+ industries  ·  Remote-capable globally",
            ha="center", fontsize=9, color="#3A4A5C", style="italic")

    plt.savefig(FOOTPRINT_IMG, dpi=200, bbox_inches="tight", facecolor="white")
    plt.close(fig)


build_timeline()
build_footprint()


# ===============================================================
# 2) Page chrome
# ===============================================================
def draw_page_chrome(canv: canvas.Canvas, doc):
    canv.saveState()
    width, height = LETTER
    canv.setStrokeColor(BORDER)
    canv.setLineWidth(0.5)
    canv.line(0.5 * inch, 0.5 * inch, width - 0.5 * inch, 0.5 * inch)
    canv.setFillColor(MUTED)
    canv.setFont("Helvetica", 8)
    canv.drawString(0.5 * inch, 0.32 * inch,
                    "ACCOUNT RESEARCH BRIEF  ·  BW Project Management  ·  Santo Domingo, Dominican Republic")
    canv.drawRightString(width - 0.5 * inch, 0.32 * inch,
                         f"May 8, 2026  ·  Page {doc.page}")
    canv.restoreState()


# ===============================================================
# 3) Styles (identical to template)
# ===============================================================
styles = getSampleStyleSheet()

eyebrow = ParagraphStyle("Eyebrow", parent=styles["Normal"],
    fontName="Helvetica-Bold", fontSize=9, leading=10,
    textColor=ACCENT, spaceAfter=2)

hero_name = ParagraphStyle("HeroName", parent=styles["Title"],
    fontName="Helvetica-Bold", fontSize=26, leading=30,
    textColor=NAVY, spaceAfter=2, alignment=TA_LEFT)

hero_role = ParagraphStyle("HeroRole", parent=styles["Normal"],
    fontName="Helvetica", fontSize=11, leading=14,
    textColor=SLATE, spaceAfter=6, alignment=TA_LEFT)

h1 = ParagraphStyle("H1", parent=styles["Heading1"],
    fontName="Helvetica-Bold", fontSize=14, leading=17,
    textColor=NAVY, spaceBefore=4, spaceAfter=4)

body = ParagraphStyle("Body", parent=styles["Normal"],
    fontName="Helvetica", fontSize=9.5, leading=13,
    textColor=TEXT, spaceAfter=4)

body_just = ParagraphStyle("BodyJ", parent=body, alignment=TA_JUSTIFY)

big_stat_num = ParagraphStyle("StatNum", parent=styles["Normal"],
    fontName="Helvetica-Bold", fontSize=24, leading=26,
    textColor=NAVY, alignment=TA_CENTER, spaceAfter=2)

big_stat_lbl = ParagraphStyle("StatLbl", parent=styles["Normal"],
    fontName="Helvetica-Bold", fontSize=7.5, leading=10,
    textColor=SLATE, alignment=TA_CENTER, spaceAfter=0)

source_p = ParagraphStyle("Source", parent=body,
    fontSize=8.5, leading=11, textColor=SLATE, spaceAfter=2)

card_title = ParagraphStyle("CardTitle", parent=styles["Normal"],
    fontName="Helvetica-Bold", fontSize=10, leading=12,
    textColor=NAVY, spaceAfter=3)

card_body = ParagraphStyle("CardBody", parent=body,
    fontSize=9, leading=12, spaceAfter=0)

chip_style = ParagraphStyle("Chip", parent=styles["Normal"],
    fontName="Helvetica-Bold", fontSize=8.5, leading=10,
    textColor=NAVY, alignment=TA_CENTER)


# ===============================================================
# 4) Components (identical to template)
# ===============================================================
def net_worth_badge(amount="$1–3M", diameter=1.15 * inch):
    """For company briefs, this badge shows estimated annual revenue —
    the prime real estate goes to actionable financial data."""
    from reportlab.graphics.shapes import Drawing, Circle, String
    d = Drawing(diameter, diameter)
    r = diameter / 2
    d.add(Circle(r, r, r, fillColor=NAVY, strokeColor=ACCENT, strokeWidth=2))
    # Top eyebrow
    d.add(String(r, r + 14, "EST. REVENUE",
                 fontName="Helvetica-Bold", fontSize=6.5,
                 fillColor=ACCENT, textAnchor="middle"))
    # Main figure
    d.add(String(r, r - 4, amount,
                 fontName="Helvetica-Bold", fontSize=14,
                 fillColor=white, textAnchor="middle"))
    # Bottom unit
    d.add(String(r, r - 18, "USD/yr",
                 fontName="Helvetica-Bold", fontSize=6.5,
                 fillColor=ACCENT, textAnchor="middle"))
    return d


def stat_block(number, label, color=NAVY, bg=LIGHT):
    inner = Table(
        [[Paragraph(f'<font color="{color.hexval()}">{number}</font>',
                    big_stat_num)],
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


def stat_row(stats):
    cells = [stat_block(n, l, c) for (n, l, c) in stats]
    t = Table([cells], colWidths=[1.78 * inch] * len(stats))
    t.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 3),
        ("RIGHTPADDING", (0, 0), (-1, -1), 3),
        ("TOPPADDING", (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
    ]))
    return t


def chip(text, bg=LIGHT_2, fg=NAVY):
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


def chip_grid(labels, cols=6, col_w=1.18 * inch):
    rows = []
    for i in range(0, len(labels), cols):
        row = [chip(x) for x in labels[i:i + cols]]
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


def signal_meter(strength, color, max_=5):
    from reportlab.graphics.shapes import Drawing, Rect
    cell_w, cell_h, gap = 14, 8, 3
    d = Drawing((cell_w + gap) * max_, cell_h + 2)
    for i in range(max_):
        c = color if i < strength else BORDER
        d.add(Rect(i * (cell_w + gap), 1, cell_w, cell_h,
                   fillColor=c, strokeColor=None))
    return d


def signal_card(symbol, sym_color, text, bg=LIGHT, accent=NAVY):
    sym = Paragraph(
        f'<font color="{sym_color.hexval()}" size="13"><b>{symbol}</b></font>',
        ParagraphStyle("Sym", parent=body, fontSize=13, leading=15,
                       alignment=TA_CENTER))
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


def hero_callout(text_paragraph, accent=ACCENT, bg=NAVY):
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


def section_header(label, accent=ACCENT):
    p = Paragraph(label.upper(), ParagraphStyle(
        "SectionEye", parent=eyebrow, textColor=accent, fontSize=9))
    rule = HRFlowable(width="100%", thickness=0.6, color=BORDER,
                      spaceBefore=2, spaceAfter=4)
    return [p, rule]


def mini_card(title, body_text, accent=NAVY, bg=LIGHT):
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


def card_row(cards, col_w=3.7):
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
# 5) Story
# ===============================================================
story = []

# ---------- PAGE 1 ----------
hero_left = net_worth_badge("$1–3M")
hero_right = [
    Paragraph("ACCOUNT RESEARCH BRIEF", eyebrow),
    Paragraph("BW Project Management", hero_name),
    Paragraph("Project Management Consultancy  ·  Santo Domingo, DR  ·  bwpm.pro", hero_role),
]
hero = Table([[hero_left, hero_right]],
             colWidths=[1.3 * inch, 6.1 * inch])
hero.setStyle(TableStyle([
    ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
    ("LEFTPADDING", (0, 0), (-1, -1), 0),
    ("RIGHTPADDING", (0, 0), (-1, -1), 0),
    ("TOPPADDING", (0, 0), (-1, -1), 0),
    ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
]))
story.append(hero)
story.append(Spacer(1, 4))
story.append(Paragraph(
    '<font color="#6B7280" size="8"><i>Revenue estimated from 4 yrs in '
    "operation, 50+ projects delivered, PMI ATP + ClickUp partner status, "
    "and typical LATAM PM-consultancy benchmarks. Privately held; not "
    "publicly disclosed.</i></font>",
    ParagraphStyle("NWCaveat", parent=body, fontSize=8, leading=10,
                   textColor=MUTED, alignment=TA_LEFT)))
story.append(HRFlowable(width="100%", thickness=1.5, color=ACCENT,
                        spaceBefore=4, spaceAfter=8))

qt_para = Paragraph(
    '<font color="#C9A55C"><b>QUICK TAKE</b></font><br/><br/>'
    '<font color="#FFFFFF" size="11">'
    "Dominican PM consultancy founded 2020. PMI Authorized Training "
    "Partner and the <b>exclusive ClickUp Partner in RD</b> &mdash; a "
    "defensible distribution edge. 50+ projects across tech, retail, "
    "finance, airlines &amp; banking; active in RD, Panama, Jamaica."
    "<br/><br/>"
    '<font color="#C9A55C"><b>BEST ANGLE:</b></font> Lead with the '
    "ClickUp/MS Dynamics 365 implementation pillar &mdash; their "
    "highest-margin, most differentiated offering. Generic PM consulting "
    "is commoditized; platform impl. is where they win."
    '</font>',
    ParagraphStyle("QTHero", parent=body, fontSize=10.5, leading=14,
                   textColor=white))
story.append(hero_callout(qt_para, accent=ACCENT, bg=NAVY))
story.append(Spacer(1, 10))

story.append(stat_row([
    ("2020",    "FOUNDED<br/>IN RD",            NAVY),
    ("50+",     "PROJECTS<br/>DELIVERED",       TEAL),
    ("10+",     "INDUSTRIES<br/>SERVED",        ACCENT),
    ("4+",      "COUNTRIES<br/>ACTIVE",         CORAL),
]))
story.append(Spacer(1, 6))

story.extend(section_header("COMPANY TRAJECTORY"))
story.append(Image(TIMELINE_IMG, width=7.4 * inch, height=1.55 * inch))
story.append(Spacer(1, 4))

story.extend(section_header("WHO THEY ARE"))
story.append(card_row([
    mini_card("\u2691  Origin &amp; Positioning",
              "Founded 2020 in Santo Domingo. Tagline: <i>&ldquo;Tu socio "
              "en la gestión de proyectos.&rdquo;</i> Positions as a "
              "strategic partner, not a vendor &mdash; PMI-certified team, "
              "agile/hybrid/predictive methodologies.",
              accent=NAVY),
    mini_card("\u2605  Defensible Moat",
              "<b>Exclusive ClickUp Partner in RD</b> (sole distribution "
              "right) + PMI Authorized Training Partner + Microsoft "
              "Dynamics 365 implementer. The platform pillar is hard to "
              "replicate locally.",
              accent=ACCENT),
]))

story.extend(section_header("COMPANY DNA"))
story.append(card_row([
    mini_card("\u26C2  Service Mix",
              "<b>Project delivery</b> (managed end-to-end) &middot; "
              "<b>Consulting</b> (PMO setup) &middot; <b>Staff aug</b> "
              "(certified PMs embedded) &middot; <b>Platform impl.</b> "
              "(ClickUp, MS Dynamics 365) &middot; <b>BW Academy</b> "
              "(PMI-certified training).",
              accent=TEAL),
    mini_card("\u2660  Methodology &amp; Culture",
              "Agile, Hybrid, Predictive frameworks. Discovery-first "
              "engagement style. Real-time visibility &amp; KPI dashboards. "
              "Reachable via Instagram (@bwpm.rd, 830 followers) and "
              "bwpm.pro &mdash; signals a small, founder-led, hands-on team.",
              accent=CORAL),
]))

story.append(PageBreak())

# ---------- PAGE 2 ----------
story.extend(section_header("SERVICES &amp; OFFERINGS"))
story.append(Paragraph("Strategic Project Management &amp; Platform Implementation", h1))
story.append(Paragraph(
    "Boutique consultancy that combines PMI-certified project delivery "
    "with platform implementation (ClickUp, MS Dynamics 365) and "
    "PMI-authorized training (BW Academy). Differentiation comes from the "
    "exclusive ClickUp partnership in Dominican Republic &mdash; few "
    "competitors can bundle methodology + platform + training under one roof.",
    body_just))
story.append(Spacer(1, 10))

story.append(stat_row([
    ("PMI ATP",  "AUTHORIZED<br/>TRAINING PARTNER",  NAVY),
    ("ClickUp",  "EXCLUSIVE<br/>RD PARTNER",         TEAL),
    ("MS 365",   "DYNAMICS<br/>IMPLEMENTER",         ACCENT),
    ("3",        "METHODOLOGIES<br/>(AGILE/HYB/PRED)",CORAL),
]))
story.append(Spacer(1, 16))

story.extend(section_header("INDUSTRIES SERVED"))
story.append(chip_grid([
    "Technology", "Retail", "Finance", "Banking",
    "Airlines", "Construction", "Energy", "Insurance",
    "Public Sector", "Telco", "Manufacturing", "E-commerce",
], cols=4, col_w=1.83 * inch))
story.append(Spacer(1, 14))

story.extend(section_header("GEOGRAPHIC DELIVERY FOOTPRINT"))
story.append(Image(FOOTPRINT_IMG, width=7.0 * inch, height=2.3 * inch))
story.append(Spacer(1, 10))

story.extend(section_header("STRATEGIC SIGNALS  ·  CURRENT POSTURE"))
story.append(card_row([
    mini_card("ClickUp = Distribution Moat",
              "Exclusive RD partnership gives them deal flow whenever "
              "ClickUp expands LATAM. Co-marketing leverage with a "
              "well-funded SaaS partner.",
              accent=NAVY),
    mini_card("Training as Revenue Engine",
              "BW Academy (PMI ATP) adds a recurring training revenue "
              "stream plus a top-of-funnel lead-gen channel. Common "
              "playbook for consultancies entering the &ldquo;learn-then-"
              "implement&rdquo; sales motion.",
              accent=TEAL),
]))
story.append(card_row([
    mini_card("Cross-Border Reach for a 4-yr-old Firm",
              "Active in RD, Panama, Jamaica plus remote-capable LATAM/"
              "global. Indicates either nearshore-export ambition or "
              "anchor clients with multi-country footprint.",
              accent=ACCENT),
    mini_card("Founder-Led, Small Team",
              "Instagram following ~830, narrow leadership profile "
              "online. Decisions are likely fast; bureaucracy minimal. "
              "Good for partnership pilots, harder for very large RFPs.",
              accent=CORAL),
]))

story.append(PageBreak())

# ---------- PAGE 3 ----------
story.extend(section_header("ENGAGEMENT READINESS SCORECARD"))

def scorecard_row(label, strength, color, note):
    meter = signal_meter(strength, color)
    label_p = Paragraph(f"<b>{label}</b>", body)
    note_p  = Paragraph(f'<font color="#6B7280" size="8.5">{note}</font>',
                        ParagraphStyle("Note", parent=body, fontSize=8.5,
                                       leading=11, textColor=MUTED))
    return [label_p, meter, note_p]

scorecard = Table([
    scorecard_row("Reachability",          5, GREEN,
                  "Active website, Instagram (@bwpm.rd), email-able"),
    scorecard_row("Decision Speed",        5, GREEN,
                  "Small founder-led firm; minimal bureaucracy"),
    scorecard_row("Partnership Openness",  4, NAVY,
                  "Already a ClickUp + PMI + MS partner &mdash; structurally open"),
    scorecard_row("Buying Power",          2, AMBER,
                  "Small consultancy budget; better as a partner than a buyer"),
    scorecard_row("Reseller Potential",    5, ACCENT,
                  "If you have tools/services they can resell to clients &mdash; ideal"),
    scorecard_row("Scale Headroom",        3, ACCENT,
                  "4 yrs in, 50+ projects, multi-country &mdash; ready to grow but not enterprise"),
], colWidths=[1.95 * inch, 1.6 * inch, 3.75 * inch])
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
story.append(scorecard)
story.append(Spacer(1, 10))

story.extend(section_header("KEY SIGNALS"))
story.append(signal_card(
    "\u2714", GREEN,
    "<b>Distribution moat:</b> Exclusive ClickUp Partner in RD = a "
    "channel relationship that&rsquo;s hard for newcomers to replicate.",
    bg=GREEN_BG, accent=GREEN))
story.append(Spacer(1, 4))
story.append(signal_card(
    "\u2714", GREEN,
    "<b>Multi-platform implementer</b> (ClickUp + MS Dynamics 365) opens "
    "cross-sell and co-implementation opportunities.",
    bg=GREEN_BG, accent=GREEN))
story.append(Spacer(1, 4))
story.append(signal_card(
    "!", AMBER,
    "<b>Small-firm budget reality</b> &mdash; better positioned as a "
    "<i>partner/reseller</i> than as a direct buyer of expensive tooling.",
    bg=AMBER_BG, accent=AMBER))
story.append(Spacer(1, 4))
story.append(signal_card(
    "?", MUTED,
    "<b>Discovery gaps:</b> exact team size, founder names &amp; bios, "
    "anchor clients, real revenue, growth rate, M&amp;A or capital "
    "raise plans.",
    bg=GRAY_BG, accent=MUTED))

story.append(Spacer(1, 10))

story.extend(section_header("RECOMMENDED APPROACH"))
ra_para = Paragraph(
    '<font color="#C9A55C"><b>BEST ENTRY POINT</b></font><br/>'
    '<font color="#FFFFFF" size="11">Contact via bwpm.pro contact form, '
    "Instagram (@bwpm.rd), or LinkedIn company page. Warm intros via the "
    "ClickUp LATAM partner channel or PMI Chapter Dominican Republic land "
    "fastest.</font><br/><br/>"
    '<font color="#C9A55C"><b>OPENING HOOK</b></font><br/>'
    '<font color="#FFFFFF" size="11">Frame as a <b>partner/co-sell</b> '
    "play, not a vendor pitch. Reference their ClickUp + PMI + MS "
    "positioning specifically. If you offer complementary tools or services "
    "their clients buy, lead with reseller economics, not list price."
    "</font>",
    ParagraphStyle("RAHero", parent=body, fontSize=10.5, leading=15,
                   textColor=white))
story.append(hero_callout(ra_para, accent=ACCENT, bg=NAVY_DK))
story.append(Spacer(1, 6))

story.extend(section_header("DISCOVERY QUESTIONS"))

def q_card(num, text, color):
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
    inner = Table([[num_cell, text_p]],
                  colWidths=[0.5 * inch, 6.85 * inch])
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

story.append(q_card("1",
    "&ldquo;Of the 50+ projects you&rsquo;ve delivered, which mix has "
    "grown fastest &mdash; managed delivery, platform implementation, or "
    "BW Academy training?&rdquo;",
    NAVY))
story.append(Spacer(1, 4))
story.append(q_card("2",
    "&ldquo;Where is the ClickUp partnership taking you next "
    "&mdash; deeper in RD or expanding regionally through their LATAM "
    "channel?&rdquo;", TEAL))
story.append(Spacer(1, 4))
story.append(q_card("3",
    "&ldquo;What&rsquo;s the biggest constraint on growth right now "
    "&mdash; finding certified PMs, lead flow, or scaling delivery "
    "without losing quality?&rdquo;",
    ACCENT))

story.append(PageBreak())

# ---------- PAGE 4 ----------
story.extend(section_header("BRIEF AT A GLANCE"))
story.append(Spacer(1, 4))

story.append(stat_row([
    ("2020",   "FOUNDED",                       NAVY),
    ("50+",    "PROJECTS<br/>DELIVERED",        TEAL),
    ("4+",     "COUNTRIES<br/>ACTIVE",          ACCENT),
    ("3",      "STRATEGIC<br/>PARTNERSHIPS",    CORAL),
]))
story.append(Spacer(1, 16))

story.extend(section_header("NEXT STEPS"))
ns_para = Paragraph(
    '<font color="#FFFFFF" size="11">'
    "<b>Connect enrichment + CRM</b> to upgrade this brief: founder &amp; "
    "leadership names, exact team size, verified revenue, anchor client "
    "list, and any prior touchpoints (ClickUp LATAM, PMI Chapter RD, "
    "Microsoft Dynamics RD partner ecosystem). Then re-run for a sharper "
    "picture."
    "</font>",
    ParagraphStyle("NSHero", parent=body, fontSize=10.5, leading=15,
                   textColor=white))
story.append(hero_callout(ns_para, accent=ACCENT, bg=NAVY))
story.append(Spacer(1, 16))

story.extend(section_header("SOURCES"))
sources = [
    ("BW Project Management &mdash; Homepage",
     "https://bwpm.pro/"),
    ("BW Project Management &mdash; About",
     "https://bwpm.pro/about/"),
    ("BW Project Management &mdash; Gestión Integral",
     "https://bwpm.pro/servicios/gestion-integral-de-proyectos"),
    ("BW Project Management &mdash; Consultoría",
     "https://bwpm.pro/servicios/consultoria-en-gestion-de-proyectos"),
    ("Instagram &mdash; @bwpm.rd",
     "https://www.instagram.com/bwpm.rd/"),
    ("PMI Authorized Training Partner Directory",
     "https://www.pmi.org/"),
    ("ClickUp Partner Program",
     "https://clickup.com/partners"),
    ("Microsoft Dynamics 365 Partners",
     "https://partner.microsoft.com/"),
]

src_card_style = ParagraphStyle("SrcCard", parent=body,
    fontSize=9, leading=12, textColor=NAVY, spaceAfter=0)
src_url_style = ParagraphStyle("SrcUrl", parent=body,
    fontSize=7.5, leading=9, textColor=MUTED, spaceAfter=0)

src_cells = []
for label, url in sources:
    inner = Table([
        [Paragraph(f'<b><link href="{url}"><font color="#0F2D4A">{label}</font></link></b>',
                   src_card_style)],
        [Paragraph(url[:60] + ("&hellip;" if len(url) > 60 else ""),
                   src_url_style)],
    ], colWidths=[3.55 * inch])
    inner.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), LIGHT),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("LINEBEFORE", (0, 0), (0, -1), 2, ACCENT),
    ]))
    src_cells.append(inner)

if len(src_cells) % 2 == 1:
    src_cells.append("")

src_rows = [src_cells[i:i + 2] for i in range(0, len(src_cells), 2)]
src_table = Table(src_rows, colWidths=[3.7 * inch, 3.7 * inch])
src_table.setStyle(TableStyle([
    ("VALIGN", (0, 0), (-1, -1), "TOP"),
    ("LEFTPADDING", (0, 0), (-1, -1), 0),
    ("RIGHTPADDING", (0, 0), (-1, -1), 0),
    ("TOPPADDING", (0, 0), (-1, -1), 4),
    ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
]))
story.append(src_table)
story.append(Spacer(1, 16))

method_p = Paragraph(
    '<font color="#6B7280" size="8.5"><i>'
    "<b>Methodology:</b> Generated using the Account Research skill. "
    "Sources are public web search results as of May 8, 2026. No CRM "
    "or enrichment connectors were attached for this run. Information "
    "is best-effort and should be verified before high-stakes outreach."
    "</i></font>",
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
story.append(method_box)


# ===============================================================
# 6) Build
# ===============================================================
doc = SimpleDocTemplate(
    PDF_PATH, pagesize=LETTER,
    leftMargin=0.5 * inch, rightMargin=0.5 * inch,
    topMargin=0.5 * inch, bottomMargin=0.65 * inch,
    title="BW Project Management — Visual Research Brief",
    author="Account Research",
)
doc.build(story, onFirstPage=draw_page_chrome, onLaterPages=draw_page_chrome)
print(f"PDF written to: {PDF_PATH}")
