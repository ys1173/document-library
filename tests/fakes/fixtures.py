"""Builds a small realistic corpus."""

from __future__ import annotations

from pathlib import Path

GUIDE_TABLE = [["Node", "IP", "Port"], ["smf-1", "10.0.0.1", "8805"], ["smf-2", "10.0.0.2", "8805"]]


def make_guide_pdf(path: Path, extra: str = "") -> None:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.platypus import (
        PageBreak,
        Paragraph,
        Preformatted,
        SimpleDocTemplate,
        Spacer,
        Table,
        TableStyle,
    )

    styles = getSampleStyleSheet()
    table = Table(GUIDE_TABLE, style=TableStyle([("GRID", (0, 0), (-1, -1), 0.75, colors.black),
                                                 ("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey)]))
    story = [
        Paragraph("5G Core Ops Guide", styles["Title"]),
        Paragraph("4 SMF", styles["Heading1"]),
        Paragraph("4.2 Restarting SMF pods", styles["Heading2"]),
        Paragraph("Restart SMF pods one at a time and wait for readiness before continuing to the next pod. " + extra,
                  styles["BodyText"]),
        Preformatted("interface ge-0/0/1\n    unit 0\n        family inet address 10.0.0.1/24", styles["Code"]),
        Spacer(1, 12),
        table,
        PageBreak(),
        Paragraph("5 UPF", styles["Heading1"]),
        Paragraph("The UPF restart procedure drains user plane sessions gracefully before the upgrade starts.",
                  styles["BodyText"]),
    ]
    SimpleDocTemplate(str(path), pagesize=A4, title="5G Core Ops Guide").build(story)


def make_scanned_pdf(path: Path, lines: tuple[str, ...] = ("Scanned MOP: restart the AMF pods",
                                                          "Maintenance window starts at 02:00")) -> None:
    from PIL import Image, ImageDraw, ImageFont
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfgen import canvas

    image = Image.new("RGB", (1240, 1754), "white")
    draw = ImageDraw.Draw(image)
    font = None
    for candidate in ("/System/Library/Fonts/Helvetica.ttc", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"):
        if Path(candidate).exists():
            font = ImageFont.truetype(candidate, 44)
            break
    if font is None:
        font = ImageFont.load_default(size=44)
    for index, line in enumerate(lines):
        draw.text((60, 80 + index * 100), line, fill="black", font=font)
    pdf = canvas.Canvas(str(path), pagesize=A4)
    width, height = A4
    pdf.drawImage(ImageReader(image), 0, 0, width=width, height=height)
    pdf.showPage()
    pdf.save()


def make_docx(path: Path) -> None:
    import docx

    doc = docx.Document()
    doc.add_heading("MOP Upgrade", 1)
    doc.add_heading("Preconditions", 2)
    doc.add_paragraph("Confirm the configuration backup is complete before the maintenance window.")
    table = doc.add_table(rows=2, cols=2)
    table.cell(0, 0).text, table.cell(0, 1).text = "Step", "Owner"
    table.cell(1, 0).text, table.cell(1, 1).text = "Backup", "NOC"
    doc.save(path)


def make_xlsx(path: Path) -> None:
    import openpyxl

    book = openpyxl.Workbook()
    sheet = book.active
    sheet.title = "IP Plan"
    sheet.append(["Site", "VLAN", "Subnet"])
    sheet.append(["TYO1", 100, "10.1.0.0/24"])
    ports = book.create_sheet("Ports")
    ports.append(["Device", "Port"])
    ports.append(["sw1", "Gi0/1"])
    book.save(path)


def make_pptx(path: Path) -> None:
    from pptx import Presentation

    deck = Presentation()
    for title, body in (("Design Overview", "N4 interface design notes"), ("Failover", "Failover uses a standby UPF")):
        slide = deck.slides.add_slide(deck.slide_layouts[1])
        slide.shapes.title.text = title
        slide.placeholders[1].text = body
    deck.save(path)


def make_corpus(root: Path) -> None:
    (root / "Vendors").mkdir(parents=True, exist_ok=True)
    (root / "MOPs").mkdir(exist_ok=True)
    (root / "Archive").mkdir(exist_ok=True)
    make_guide_pdf(root / "Vendors" / "Guide.pdf")
    make_docx(root / "MOPs" / "Upgrade.docx")
    make_xlsx(root / "IP Plan.xlsx")
    make_pptx(root / "Design.pptx")
    (root / "router.cfg").write_text("interface ge-0/0/1\n    unit 0\n        family inet\n", encoding="utf-8")
    (root / "notes.md").write_text("# Notes\n\n## On-call\n\nPage the on-call team for quokka alarms.\n", encoding="utf-8")
    (root / "ja.txt").write_text("SMFポッドの再起動手順について説明します。\n", encoding="utf-8")
    (root / "~$lock.docx").write_bytes(b"owner lock file")
    (root / "Archive" / "old.md").write_text("# Old\n\narchivedterm\n", encoding="utf-8")


def make_mixed_pdf(path: Path) -> None:
    """Page 1 is born-digital text; page 2 is a scan with no text layer."""
    from PIL import Image, ImageDraw, ImageFont
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfgen import canvas

    pdf = canvas.Canvas(str(path), pagesize=A4)
    width, height = A4
    for index in range(6):
        pdf.drawString(60, 760 - index * 20, f"Native text layer line {index}: restart the gateway after the config push.")
    pdf.showPage()
    image = Image.new("RGB", (1240, 1754), "white")
    ImageDraw.Draw(image).text((60, 80), "Scanned second page about AMF pods", fill="black", font=ImageFont.load_default(size=44))
    pdf.drawImage(ImageReader(image), 0, 0, width=width, height=height)
    pdf.showPage()
    pdf.save()


REFERENCE_MARKERS = {1: "aardvark", 2: "bobcat", 3: "chinchilla", 4: "dugong", 5: "echidna", 6: "ferret"}


def make_reference_pdf(path: Path) -> None:
    """Six pages, with a word on each that appears nowhere else, so citations can be checked against known answers."""
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.platypus import PageBreak, Paragraph, SimpleDocTemplate

    styles = getSampleStyleSheet()
    story = []
    for page, word in REFERENCE_MARKERS.items():
        story.append(Paragraph(f"{page} Chapter on {word}", styles["Heading1"]))
        story.append(Paragraph(f"The procedure for {word} equipment is described on this page of the manual. " * 4, styles["BodyText"]))
        if page < len(REFERENCE_MARKERS):
            story.append(PageBreak())
    SimpleDocTemplate(str(path), pagesize=A4, title="Reference Manual").build(story)


def make_spanning_pdf(path: Path) -> None:
    """One section with no headings after the first, running across pages, with a marker on each page."""
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.platypus import PageBreak, Paragraph, SimpleDocTemplate

    styles = getSampleStyleSheet()
    SimpleDocTemplate(str(path), pagesize=A4, title="Span").build([
        Paragraph("1 Long Section", styles["Heading1"]),
        Paragraph("Short start with spanstartmarker in it, long enough for the text layer to count as readable.", styles["BodyText"]),
        PageBreak(),
        Paragraph("Short end with spanendmarker in it, also long enough for the text layer to count as readable.", styles["BodyText"]),
    ])
