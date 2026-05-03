"""
API Views for Amazon Listing Analyzer (Celery + Database)

File: api/views.py
"""

from rest_framework.decorators import api_view
from rest_framework.response import Response
from rest_framework import status
from .models import AnalysisTask, AnalysisResult
from .tasks import scrape_amazon_listing_task
import re
import re
import tempfile
import requests
from io import BytesIO

from django.http import FileResponse
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import (
    SimpleDocTemplate,
    Paragraph,
    Spacer,
    Table,
    TableStyle,
    Image,
    PageBreak,
    KeepTogether,
)


@api_view(['POST'])
def analyze_listing(request):
    """
    Start Amazon listing analysis
    
    POST /api/analyze/
    Body: {"asin": "B0DWMQDYSZ"}
    
    Returns: {"task_id": "uuid", "status": "pending"}
    """
    asin = request.data.get('asin', '').upper().strip()
    
    # Validate ASIN
    if not asin:
        return Response(
            {'error': 'ASIN is required'},
            status=status.HTTP_400_BAD_REQUEST
        )
    
    if not re.match(r'^B[A-Z0-9]{9}$', asin):
        return Response(
            {'error': 'Invalid ASIN format. Must be 10 characters starting with B'},
            status=status.HTTP_400_BAD_REQUEST
        )
    
    # Create task in database
    task = AnalysisTask.objects.create(
        asin=asin,
        status='pending',
        progress=0,
        message='Task created, queued for processing...'
    )
    
    # Queue Celery task (runs in background worker)
    scrape_amazon_listing_task.delay(str(task.id))
    
    return Response({
        'task_id': str(task.id),
        'status': task.status,
        'asin': asin,
    }, status=status.HTTP_202_ACCEPTED)


@api_view(['GET'])
def task_status(request, task_id):
    """
    Check task status and get results
    
    GET /api/task-status/{task_id}/
    
    Returns:
    - If in progress: {"status": "processing", "progress": 60, "message": "..."}
    - If complete: {"status": "completed", "progress": 100, "data": {...}}
    - If failed: {"status": "failed", "error": "..."}
    """
    try:
        task = AnalysisTask.objects.get(id=task_id)
    except AnalysisTask.DoesNotExist:
        return Response(
            {'error': 'Task not found'},
            status=status.HTTP_404_NOT_FOUND
        )
    
    response_data = {
        'status': task.status,
        'progress': task.progress,
        'message': task.message,
    }
    
    # If completed, include full results
    if task.status == 'completed':
        try:
            result = task.result
            response_data['data'] = {
                'your_product': result.your_product_data,
                'competitors': result.competitors_data,
                'analysis': result.analysis_text,
                'html_previews': {
                    'your_product': result.your_product_html,
                    'competitors': result.competitor_htmls,
                }
            }
        except AnalysisResult.DoesNotExist:
            response_data['message'] = 'Results not found'
    
    # If failed, include error
    if task.status == 'failed':
        response_data['error'] = task.error
    
    return Response(response_data)


@api_view(['GET'])
def analysis_history(request):
    """
    Get all past analyses
    
    GET /api/history/
    GET /api/history/?asin=B0DWMQDYSZ
    """
    asin = request.query_params.get('asin', None)
    
    query = AnalysisTask.objects.filter(status='completed')
    
    if asin:
        query = query.filter(asin=asin.upper())
    
    tasks = query.select_related('result')[:20]
    
    history = []
    for task in tasks:
        try:
            history.append({
                'task_id': str(task.id),
                'asin': task.asin,
                'created_at': task.created_at.isoformat(),
                'your_product_title': task.result.your_product_data.get('title', 'N/A'),
                'competitors_count': len(task.result.competitors_data),
            })
        except AnalysisResult.DoesNotExist:
            continue
    
    return Response({
        'count': len(history),
        'results': history
    })


@api_view(['GET'])
def get_analysis(request, task_id):
    """
    Get specific analysis result
    
    GET /api/analysis/{task_id}/
    """
    try:
        task = AnalysisTask.objects.get(id=task_id)
        result = task.result
    except (AnalysisTask.DoesNotExist, AnalysisResult.DoesNotExist):
        return Response(
            {'error': 'Analysis not found'},
            status=status.HTTP_404_NOT_FOUND
        )
    
    return Response({
        'task_id': str(task.id),
        'asin': result.asin,
        'created_at': result.created_at.isoformat(),
        'data': {
            'your_product': result.your_product_data,
            'competitors': result.competitors_data,
            'analysis': result.analysis_text,
            'html_previews': {
                'your_product': result.your_product_html,
                'competitors': result.competitor_htmls,
            }
        }
    })


@api_view(['GET'])
def health_check(request):
    """Health check"""
    return Response({
        'status': 'ok',
        'message': 'API is running'
    })


def build_pdf_styles():
    base = getSampleStyleSheet()

    return {
        "cover_title": ParagraphStyle(
            "cover_title",
            parent=base["Title"],
            fontName="Helvetica-Bold",
            fontSize=28,
            leading=34,
            textColor=colors.HexColor("#111827"),
            alignment=TA_CENTER,
            spaceAfter=6,
        ),
        "cover_subtitle": ParagraphStyle(
            "cover_subtitle",
            parent=base["BodyText"],
            fontName="Helvetica",
            fontSize=11,
            leading=16,
            textColor=colors.HexColor("#6B7280"),
            alignment=TA_CENTER,
        ),
        "section_title": ParagraphStyle(
            "section_title",
            parent=base["Heading1"],
            fontName="Helvetica-Bold",
            fontSize=18,
            leading=24,
            textColor=colors.HexColor("#4C1D95"),
            spaceBefore=10,
            spaceAfter=8,
        ),
        "subsection_title": ParagraphStyle(
            "subsection_title",
            parent=base["Heading2"],
            fontName="Helvetica-Bold",
            fontSize=13,
            leading=18,
            textColor=colors.HexColor("#111827"),
            spaceBefore=8,
            spaceAfter=5,
        ),
        "body": ParagraphStyle(
            "body",
            parent=base["BodyText"],
            fontName="Helvetica",
            fontSize=9.5,
            leading=14,
            textColor=colors.HexColor("#374151"),
            spaceAfter=5,
        ),
        "small": ParagraphStyle(
            "small",
            parent=base["BodyText"],
            fontName="Helvetica",
            fontSize=8,
            leading=11,
            textColor=colors.HexColor("#6B7280"),
        ),
        "label": ParagraphStyle(
            "label",
            parent=base["BodyText"],
            fontName="Helvetica-Bold",
            fontSize=8,
            leading=10,
            textColor=colors.HexColor("#6B7280"),
        ),
        "white": ParagraphStyle(
            "white",
            parent=base["BodyText"],
            fontName="Helvetica-Bold",
            fontSize=9,
            leading=12,
            textColor=colors.white,
        ),
        "table_cell": ParagraphStyle(
            "table_cell",
            parent=base["BodyText"],
            fontName="Helvetica",
            fontSize=8,
            leading=11,
            textColor=colors.HexColor("#374151"),
        ),
        "table_head": ParagraphStyle(
            "table_head",
            parent=base["BodyText"],
            fontName="Helvetica-Bold",
            fontSize=8,
            leading=10,
            textColor=colors.white,
        ),
        "analysis_h2": ParagraphStyle(
            "analysis_h2",
            parent=base["Heading1"],
            fontName="Helvetica-Bold",
            fontSize=17,
            leading=22,
            textColor=colors.HexColor("#4C1D95"),
            spaceBefore=14,
            spaceAfter=8,
        ),
        "analysis_h3": ParagraphStyle(
            "analysis_h3",
            parent=base["Heading2"],
            fontName="Helvetica-Bold",
            fontSize=12.5,
            leading=16,
            textColor=colors.HexColor("#111827"),
            spaceBefore=8,
            spaceAfter=5,
        ),
        "bullet": ParagraphStyle(
            "bullet",
            parent=base["BodyText"],
            fontName="Helvetica",
            fontSize=9.5,
            leading=14,
            leftIndent=14,
            firstLineIndent=-8,
            textColor=colors.HexColor("#374151"),
            spaceAfter=5,
        ),
        "numbered": ParagraphStyle(
            "numbered",
            parent=base["BodyText"],
            fontName="Helvetica",
            fontSize=9.5,
            leading=14,
            leftIndent=18,
            firstLineIndent=-14,
            textColor=colors.HexColor("#374151"),
            spaceAfter=6,
        ),
        "callout": ParagraphStyle(
            "callout",
            parent=base["BodyText"],
            fontName="Helvetica",
            fontSize=9.5,
            leading=14,
            textColor=colors.HexColor("#1E3A8A"),
            backColor=colors.HexColor("#EFF6FF"),
            borderColor=colors.HexColor("#BFDBFE"),
            borderWidth=0.6,
            borderPadding=7,
            spaceAfter=7,
        ),
    }


def clean_pdf_text(value):
    if value is None:
        return ""

    value = str(value)
    value = value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    value = value.replace("₹", "Rs.")
    value = value.replace("–", "-").replace("—", "-")
    value = re.sub(r"\s+", " ", value)

    return value.strip()

def markdown_to_reportlab(text):
    """
    Convert simple markdown into ReportLab Paragraph XML.

    Supports:
    - **bold**
    - *italic*
    - `code`
    - safe escaped text
    """
    if text is None:
        return ""

    text = str(text)

    # Preserve markdown first with placeholders so escaping does not break tags.
    placeholders = []

    def stash(value):
        key = f"@@MD{len(placeholders)}@@"
        placeholders.append((key, value))
        return key

    # Code first
    text = re.sub(
        r"`([^`]+)`",
        lambda m: stash(
            f'<font name="Courier" backColor="#F3F4F6" color="#6D28D9">'
            f'{clean_pdf_text(m.group(1))}'
            f'</font>'
        ),
        text,
    )

    # Bold
    text = re.sub(
        r"\*\*([^*]+)\*\*",
        lambda m: stash(f"<b>{clean_pdf_text(m.group(1))}</b>"),
        text,
    )

    # Italic - avoid catching **bold**
    text = re.sub(
        r"(?<!\*)\*([^*\n]+)\*(?!\*)",
        lambda m: stash(f"<i>{clean_pdf_text(m.group(1))}</i>"),
        text,
    )

    text = clean_pdf_text(text)

    for key, value in placeholders:
        text = text.replace(key, value)

    return text


def md_paragraph(text, style):
    return Paragraph(markdown_to_reportlab(text), style)

def paragraph(text, style):
    return Paragraph(clean_pdf_text(text), style)


def build_meta_table(result, your_product, competitors, styles):
    data = [
        [
            paragraph("ASIN", styles["label"]),
            paragraph(result.asin, styles["body"]),
            paragraph("Created", styles["label"]),
            paragraph(result.created_at.strftime("%d %b %Y, %I:%M %p"), styles["body"]),
        ],
        [
            paragraph("Product", styles["label"]),
            paragraph(your_product.get("title", "N/A")[:120], styles["body"]),
            paragraph("Competitors", styles["label"]),
            paragraph(str(len(competitors)), styles["body"]),
        ],
    ]

    table = Table(data, colWidths=[70, 220, 70, 140])
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#F8FAFC")),
        ("BOX", (0, 0), (-1, -1), 0.8, colors.HexColor("#E5E7EB")),
        ("INNERGRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#E5E7EB")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
        ("TOPPADDING", (0, 0), (-1, -1), 8),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
    ]))

    return table


def build_summary_table(your_product, competitors, styles):
    data = [
        [
            paragraph("Your ASIN", styles["label"]),
            paragraph("Competitors", styles["label"]),
            paragraph("Rating", styles["label"]),
            paragraph("Price", styles["label"]),
        ],
        [
            paragraph(your_product.get("asin", "N/A"), styles["body"]),
            paragraph(str(len(competitors)), styles["body"]),
            paragraph(f"{your_product.get('rating', 'N/A')} stars", styles["body"]),
            paragraph(your_product.get("price", "N/A"), styles["body"]),
        ],
    ]

    table = Table(data, colWidths=[130, 130, 130, 130])
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#4C1D95")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("BACKGROUND", (0, 1), (-1, 1), colors.HexColor("#FFFFFF")),
        ("BOX", (0, 0), (-1, -1), 0.8, colors.HexColor("#E5E7EB")),
        ("INNERGRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#E5E7EB")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 9),
        ("RIGHTPADDING", (0, 0), (-1, -1), 9),
        ("TOPPADDING", (0, 0), (-1, -1), 9),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 9),
    ]))

    return table


def build_product_section(product, styles, label):
    story = []

    story.append(Paragraph(clean_pdf_text(label), styles["subsection_title"]))

    image_url = product.get("image_url")
    image_flowable = build_remote_image(image_url, width=120, height=120)

    details = [
        [paragraph("Title", styles["label"]), paragraph(product.get("title", "N/A"), styles["body"])],
        [paragraph("ASIN", styles["label"]), paragraph(product.get("asin", "N/A"), styles["body"])],
        [paragraph("Price", styles["label"]), paragraph(product.get("price", "N/A"), styles["body"])],
        [paragraph("Rating", styles["label"]), paragraph(product.get("rating", "N/A"), styles["body"])],
        [paragraph("Reviews", styles["label"]), paragraph(product.get("reviews_count", "N/A"), styles["body"])],
    ]

    detail_table = Table(details, colWidths=[60, 330])
    detail_table.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#FFFFFF")),
        ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor("#E5E7EB")),
        ("INNERGRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#E5E7EB")),
    ]))

    row = [[image_flowable if image_flowable else paragraph("No image", styles["small"]), detail_table]]

    wrapper = Table(row, colWidths=[140, 390])
    wrapper.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
        ("TOPPADDING", (0, 0), (-1, -1), 8),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
        ("BOX", (0, 0), (-1, -1), 0.8, colors.HexColor("#E5E7EB")),
        ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#F9FAFB")),
    ]))

    story.append(wrapper)
    story.append(Spacer(1, 8))

    bullets = product.get("bullets", []) or []
    if bullets:
        story.append(Paragraph("Key Bullets", styles["subsection_title"]))

        for bullet in bullets[:5]:
            story.append(Paragraph(f"• {markdown_to_reportlab(bullet)}", styles["bullet"]))

    return story

def build_remote_image(url, width=150, height=150):
    if not url:
        return None

    try:
        response = requests.get(url, timeout=12)
        response.raise_for_status()

        image_buffer = BytesIO(response.content)

        img = Image(image_buffer)
        img._restrictSize(width, height)

        return img
    except Exception as e:
        print(f"[PDF IMAGE ERROR] {e}")
        return None


def build_image_comparison_section(original_url, improved_url, styles):
    original_img = build_remote_image(original_url, width=230, height=230)
    improved_img = build_remote_image(improved_url, width=230, height=230)

    data = [
        [
            paragraph("Original Product Image", styles["label"]),
            paragraph("AI Improved Ecommerce Image", styles["label"]),
        ],
        [
            original_img if original_img else paragraph("Original image not available", styles["small"]),
            improved_img if improved_img else paragraph("Improved image not available", styles["small"]),
        ],
    ]

    table = Table(data, colWidths=[260, 260])
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#4C1D95")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("BACKGROUND", (0, 1), (-1, 1), colors.white),
        ("BOX", (0, 0), (-1, -1), 0.8, colors.HexColor("#E5E7EB")),
        ("INNERGRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#E5E7EB")),
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 10),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 10),
    ]))

    return [table]

def build_analysis_markdown_section(text, styles):
    story = []
    lines = text.splitlines()

    table_buffer = []

    def flush_table():
        nonlocal table_buffer

        if table_buffer:
            table = build_markdown_table(table_buffer, styles)
            if table:
                story.append(table)
                story.append(Spacer(1, 8))
            table_buffer = []

    for raw_line in lines:
        line = raw_line.strip()

        if not line:
            flush_table()
            story.append(Spacer(1, 4))
            continue

        # Markdown table
        if line.startswith("|") and line.endswith("|"):
            table_buffer.append(line)
            continue

        flush_table()

        # Horizontal separator
        if line in ["---", "***", "___"]:
            story.append(Spacer(1, 8))
            continue

        # H2 markdown
        if line.startswith("## "):
            title = line.replace("## ", "", 1).strip()
            story.append(Paragraph(markdown_to_reportlab(title), styles["analysis_h2"]))
            continue

        # H3 markdown
        if line.startswith("### "):
            title = line.replace("### ", "", 1).strip()
            story.append(Paragraph(markdown_to_reportlab(title), styles["analysis_h3"]))
            continue

        # Plain heading-like line ending with colon
        # Example: Recommended Bullets (Benefit-First Format):
        if (
            line.endswith(":")
            and len(line) <= 90
            and not line.startswith("-")
            and not re.match(r"^\d+\.\s+", line)
        ):
            story.append(Paragraph(markdown_to_reportlab(line), styles["analysis_h3"]))
            continue

        # Numbered points
        numbered_match = re.match(r"^(\d+)\.\s+(.*)$", line)
        if numbered_match:
            number = numbered_match.group(1)
            content = numbered_match.group(2)

            story.append(
                Paragraph(
                    f"<b>{number}.</b> {markdown_to_reportlab(content)}",
                    styles["numbered"],
                )
            )
            continue

        # Bullet points
        if line.startswith("- "):
            content = line[2:].strip()
            story.append(
                Paragraph(
                    f"• {markdown_to_reportlab(content)}",
                    styles["bullet"],
                )
            )
            continue

        # Strong callouts
        lowered = line.lower()
        if (
            lowered.startswith("analysis:")
            or lowered.startswith("strategy:")
            or lowered.startswith("risk:")
            or lowered.startswith("action:")
            or lowered.startswith("verdict:")
            or lowered.startswith("recommendation:")
        ):
            story.append(Paragraph(markdown_to_reportlab(line), styles["callout"]))
            continue

        # Normal paragraph with markdown formatting
        story.append(Paragraph(markdown_to_reportlab(line), styles["body"]))

    flush_table()

    return story

def build_markdown_table(rows, styles):
    clean_rows = []

    for row in rows:
        # Skip markdown separator row:
        # |-----|-----|
        if re.match(r"^\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)+\|?$", row):
            continue

        cells = [
            cell.strip()
            for cell in row.split("|")
            if cell.strip()
        ]

        if cells:
            clean_rows.append(cells)

    if not clean_rows:
        return None

    max_cols = max(len(row) for row in clean_rows)

    normalized = []

    for row_index, row in enumerate(clean_rows):
        normalized_row = row + [""] * (max_cols - len(row))
        style = styles["table_head"] if row_index == 0 else styles["table_cell"]

        normalized.append([
            Paragraph(markdown_to_reportlab(cell), style)
            for cell in normalized_row
        ])

    available_width = 520
    col_width = available_width / max_cols

    table = Table(
        normalized,
        colWidths=[col_width] * max_cols,
        repeatRows=1,
    )

    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#4C1D95")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("BACKGROUND", (0, 1), (-1, -1), colors.HexColor("#FFFFFF")),
        ("BOX", (0, 0), (-1, -1), 0.7, colors.HexColor("#E5E7EB")),
        ("INNERGRID", (0, 0), (-1, -1), 0.35, colors.HexColor("#E5E7EB")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
    ]))

    return table

def build_reviews_section(your_product, competitors, styles):
    story = []

    story.append(Paragraph("Your Product Reviews", styles["subsection_title"]))
    story.extend(build_review_cards(your_product.get("reviews", []), styles))

    for index, competitor in enumerate(competitors, 1):
        story.append(Spacer(1, 10))
        story.append(Paragraph(f"Competitor #{index} Reviews - {clean_pdf_text(competitor.get('asin', 'N/A'))}", styles["subsection_title"]))
        story.extend(build_review_cards(competitor.get("reviews", []), styles))

    return story


def build_review_cards(reviews, styles):
    story = []

    if not reviews:
        story.append(Paragraph("No visible top reviews were available for this ASIN.", styles["body"]))
        return story

    for review in reviews[:10]:
        title = review.get("title", "Review")
        rating = review.get("rating", "N/A")
        body = review.get("body", "")
        verified = "Verified" if review.get("verified") else ""

        data = [
            [
                md_paragraph(f"{title} - {rating} stars", styles["label"]),
                paragraph(verified, styles["label"]),
            ],
            [
                md_paragraph(body, styles["body"]), 
                "",
            ],
        ]

        table = Table(data, colWidths=[420, 100])
        table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#F3E8FF")),
            ("BACKGROUND", (0, 1), (-1, 1), colors.HexColor("#FFFFFF")),
            ("BOX", (0, 0), (-1, -1), 0.6, colors.HexColor("#E9D5FF")),
            ("SPAN", (0, 1), (1, 1)),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), 7),
            ("RIGHTPADDING", (0, 0), (-1, -1), 7),
            ("TOPPADDING", (0, 0), (-1, -1), 7),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
        ]))

        story.append(table)
        story.append(Spacer(1, 6))

    return story

def draw_pdf_footer(canvas, doc):
    canvas.saveState()

    width, height = A4

    canvas.setStrokeColor(colors.HexColor("#E5E7EB"))
    canvas.line(36, 30, width - 36, 30)

    canvas.setFont("Helvetica", 8)
    canvas.setFillColor(colors.HexColor("#6B7280"))
    canvas.drawString(36, 18, "Pixii Assignment - Amazon Listing Analyzer")
    canvas.drawRightString(width - 36, 18, f"Page {doc.page}")

    canvas.restoreState()

@api_view(["GET"])
def download_analysis_pdf(request, task_id):
    """
    Download completed analysis report as PDF.

    GET /api/analysis/{task_id}/download-pdf/
    """
    try:
        task = AnalysisTask.objects.get(id=task_id)
        result = task.result
    except (AnalysisTask.DoesNotExist, AnalysisResult.DoesNotExist):
        return Response(
            {"error": "Analysis not found"},
            status=status.HTTP_404_NOT_FOUND,
        )

    buffer = BytesIO()

    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        rightMargin=36,
        leftMargin=36,
        topMargin=42,
        bottomMargin=42,
        title=f"Amazon Listing Report - {result.asin}",
    )

    styles = build_pdf_styles()
    story = []

    your_product = result.your_product_data or {}
    competitors = result.competitors_data or []
    analysis_text = result.analysis_text or ""

    # Cover
    story.append(Paragraph("Amazon Listing Analyzer", styles["cover_title"]))
    story.append(Spacer(1, 8))
    story.append(Paragraph("AI-powered product listing, competitor, review, and image analysis", styles["cover_subtitle"]))
    story.append(Spacer(1, 24))

    story.append(build_meta_table(result, your_product, competitors, styles))
    story.append(Spacer(1, 24))

    # Summary cards
    story.append(Paragraph("Executive Summary", styles["section_title"]))
    story.append(Spacer(1, 8))
    story.append(build_summary_table(your_product, competitors, styles))
    story.append(Spacer(1, 20))

    # Product Overview
    story.append(Paragraph("Your Product", styles["section_title"]))
    story.append(Spacer(1, 8))
    story.extend(build_product_section(your_product, styles, label="Your Product"))
    story.append(PageBreak())

    # Competitors
    story.append(Paragraph("Competitor Comparison", styles["section_title"]))
    story.append(Spacer(1, 8))

    for index, competitor in enumerate(competitors, 1):
        story.extend(build_product_section(competitor, styles, label=f"Competitor #{index}"))
        story.append(Spacer(1, 14))

    story.append(PageBreak())

    # AI Analysis
    story.append(Paragraph("AI Optimization Recommendations", styles["section_title"]))
    story.append(Spacer(1, 8))
    story.extend(build_analysis_markdown_section(analysis_text, styles))

    # Reviews
    story.append(PageBreak())
    story.append(Paragraph("Customer Review Snapshot", styles["section_title"]))
    story.append(Spacer(1, 8))
    story.extend(build_reviews_section(your_product, competitors, styles))

    # Improved image
    improved_image_url = your_product.get("improved_image_url")
    if improved_image_url:
        story.append(PageBreak())
        story.append(Paragraph("AI Creative Upgrade", styles["section_title"]))
        story.append(Spacer(1, 8))
        story.append(Paragraph("Improved ecommerce hero image generated from your original product image.", styles["body"]))
        story.append(Spacer(1, 12))

        image_story = build_image_comparison_section(
            original_url=your_product.get("image_url"),
            improved_url=improved_image_url,
            styles=styles,
        )
        story.extend(image_story)

    doc.build(
        story,
        onFirstPage=draw_pdf_footer,
        onLaterPages=draw_pdf_footer,
    )

    buffer.seek(0)

    filename = f"amazon_listing_report_{result.asin}.pdf"

    return FileResponse(
        buffer,
        as_attachment=True,
        filename=filename,
        content_type="application/pdf",
    )