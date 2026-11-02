"""PDF generation and email sending for survey submissions."""

import os
import smtplib
from datetime import datetime, timezone
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from io import BytesIO

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import (
    HRFlowable,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)


def _build_pdf(survey: dict, form_data: dict, student_name: str, student_email: str) -> bytes:
    buf = BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=letter,
        leftMargin=0.75 * inch,
        rightMargin=0.75 * inch,
        topMargin=0.75 * inch,
        bottomMargin=0.75 * inch,
    )

    base = getSampleStyleSheet()
    title_style = ParagraphStyle('STitle', parent=base['Title'], fontSize=16, spaceAfter=4)
    meta_style = ParagraphStyle('SMeta', parent=base['Normal'], fontSize=9, textColor=colors.grey)
    h2_style = ParagraphStyle('SH2', parent=base['Heading2'], fontSize=12, spaceBefore=14, spaceAfter=4)
    h3_style = ParagraphStyle('SH3', parent=base['Heading3'], fontSize=10, spaceBefore=8, spaceAfter=2,
                               textColor=colors.HexColor('#444444'))
    q_style = ParagraphStyle('SQ', parent=base['Normal'], fontSize=9, leading=13, spaceBefore=6)
    ans_style = ParagraphStyle('SAns', parent=base['Normal'], fontSize=9, leading=13,
                                leftIndent=16, textColor=colors.HexColor('#1a5276'))
    note_style = ParagraphStyle('SNote', parent=base['Italic'], fontSize=8, textColor=colors.HexColor('#7d6608'),
                                 spaceBefore=4, spaceAfter=4)

    story = []

    # Header
    story.append(Paragraph(survey['title'], title_style))
    submitted_at = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')
    story.append(Paragraph(f'Student: {student_name}  ({student_email})', meta_style))
    story.append(Paragraph(f'Submitted: {submitted_at}', meta_style))
    story.append(Spacer(1, 8))
    story.append(HRFlowable(width='100%', thickness=1, color=colors.HexColor('#cccccc')))
    story.append(Spacer(1, 8))

    for section in survey['sections']:
        story.append(Paragraph(section['title'], h2_style))
        if section.get('description'):
            story.append(Paragraph(section['description'], note_style))

        for q in section.get('questions', []):
            qtype = q.get('type')

            if qtype == 'heading':
                story.append(Paragraph(q['label'], h3_style))

            elif qtype == 'display':
                story.append(Paragraph(f"<b>{q['label']}:</b> {q.get('value', '')}", q_style))

            elif qtype in ('text', 'number'):
                answer = form_data.get(q['id'], '').strip() or '(not answered)'
                story.append(Paragraph(q['label'], q_style))
                story.append(Paragraph(f'→ {answer}', ans_style))

            elif qtype == 'textarea':
                answer = form_data.get(q['id'], '').strip() or '(not answered)'
                story.append(Paragraph(q['label'], q_style))
                story.append(Paragraph(f'→ {answer}', ans_style))

            elif qtype == 'radio':
                answer = form_data.get(q['id'], '(not answered)')
                story.append(Paragraph(q['label'], q_style))
                story.append(Paragraph(f'→ {answer}', ans_style))

            elif qtype == 'checkbox':
                values = form_data.getlist(q['id']) if hasattr(form_data, 'getlist') else (
                    form_data.get(q['id'], []) if isinstance(form_data.get(q['id']), list)
                    else ([form_data[q['id']]] if q['id'] in form_data else [])
                )
                answer = ', '.join(values) if values else '(none selected)'
                story.append(Paragraph(q['label'], q_style))
                story.append(Paragraph(f'→ {answer}', ans_style))

            elif qtype == 'likert_group':
                story.append(Paragraph(q.get('label', 'Hint Quality Rating'), q_style))
                table_data = [['Statement', '1', '2', '3', '4', '5', 'Rating']]
                for stmt in q.get('statements', []):
                    val = form_data.get(stmt['id'], '—')
                    row = [Paragraph(stmt['text'], ParagraphStyle('TC', parent=base['Normal'], fontSize=8, leading=11)),
                           '○', '○', '○', '○', '○', val]
                    if val.isdigit():
                        row[int(val)] = '●'
                    table_data.append(row)

                col_widths = [3.8 * inch] + [0.28 * inch] * 5 + [0.48 * inch]
                t = Table(table_data, colWidths=col_widths)
                t.setStyle(TableStyle([
                    ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#2c3e50')),
                    ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
                    ('FONTSIZE', (0, 0), (-1, 0), 8),
                    ('ALIGN', (1, 0), (-1, -1), 'CENTER'),
                    ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
                    ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, colors.HexColor('#f8f9fa')]),
                    ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor('#dddddd')),
                    ('FONTSIZE', (0, 1), (-1, -1), 8),
                    ('TOPPADDING', (0, 0), (-1, -1), 4),
                    ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
                ]))
                story.append(t)

            story.append(Spacer(1, 4))

        story.append(Spacer(1, 6))

    # ── Hints feedback section ──────────────────────────────────────────────
    useful_hints = (
        form_data.getlist("useful_hints")
        if hasattr(form_data, "getlist")
        else form_data.get("useful_hints", [])
    )
    hints_reason = form_data.get("useful_hints_reason", "").strip()

    if useful_hints or hints_reason:
        story.append(Paragraph("Hint Feedback", h2_style))
        if useful_hints:
            story.append(Paragraph("Hints marked as useful:", q_style))
            for i, ht in enumerate(useful_hints, 1):
                story.append(Paragraph(f"  {i}. {ht}", ans_style))
        else:
            story.append(Paragraph("No hints selected as useful.", q_style))
        if hints_reason:
            story.append(Paragraph("Why useful:", q_style))
            story.append(Paragraph(f"→ {hints_reason}", ans_style))
        story.append(Spacer(1, 6))

    # ── AI assistant feedback section ───────────────────────────────────────
    llm_helpful = form_data.get("llm_output_helpful", "").strip()
    llm_hallucination = form_data.get("llm_hallucination", "").strip()
    llm_evidence = form_data.get("llm_hallucination_evidence", "").strip()
    agent_helpful_unhelpful = form_data.get("agent_helpful_unhelpful", "").strip()
    agent_teaches_concepts_rating = form_data.get("agent_teaches_concepts_rating", "").strip()
    agent_effectiveness_vs_llms_rating = form_data.get("agent_effectiveness_vs_llms_rating", "").strip()

    if llm_helpful or llm_hallucination or llm_evidence or agent_helpful_unhelpful:
        story.append(Paragraph("AI Assistant Feedback", h2_style))
        if agent_helpful_unhelpful:
            story.append(Paragraph("How was the agent helpful/unhelpful in solving the challenges?", q_style))
            story.append(Paragraph(f"→ {agent_helpful_unhelpful}", ans_style))
            story.append(Paragraph("This tutoring agent taught me underlying concepts and guided me toward the right answer, rather than simply giving away hints.", q_style))
            story.append(Paragraph(f"→ {agent_teaches_concepts_rating or '(not answered)'}", ans_style))
            story.append(Paragraph("This tutoring agent helped me solve CTF challenges more effectively than commercial LLMs (e.g., GPT, Claude).", q_style))
            story.append(Paragraph(f"→ {agent_effectiveness_vs_llms_rating or '(not answered)'}", ans_style))
        story.append(Paragraph("Overall, the AI assistant's output was helpful. (1 = Strongly Disagree, 5 = Strongly Agree)", q_style))
        story.append(Paragraph(f"→ {llm_helpful or '(not answered)'}", ans_style))
        story.append(Paragraph("The AI assistant provided correct and truthful information. (1 = Strongly Disagree, 5 = Strongly Agree)", q_style))
        story.append(Paragraph(f"→ {llm_hallucination or '(not answered)'}", ans_style))
        if llm_evidence:
            story.append(Paragraph("Chat excerpt evidence:", q_style))
            story.append(Paragraph(llm_evidence.replace("\n", "<br/>"), ans_style))
        story.append(Spacer(1, 6))

    doc.build(story)
    buf.seek(0)
    return buf.read()


def submit_survey(
    survey: dict,
    form_data,
    student_name: str,
    student_email: str,
    evidence_file: bytes | None = None,
    evidence_filename: str | None = None,
) -> None:
    """Generate PDF and email it. Raises on failure."""
    pdf_bytes = _build_pdf(survey, form_data, student_name, student_email)

    smtp_user = os.environ['SMTP_USER']
    smtp_password = os.environ['SMTP_PASSWORD']
    smtp_to = os.environ['SMTP_TO']

    subject = f"[CTF Survey] {survey['title']} — {student_name} ({student_email})"
    filename = f"survey_{survey['id']}_{student_email.split('@')[0]}.pdf"

    msg = MIMEMultipart()
    msg['From'] = smtp_user
    msg['To'] = smtp_to
    msg['Reply-To'] = student_email
    msg['Subject'] = subject

    body = (
        f"Survey submission received.\n\n"
        f"Survey:   {survey['title']}\n"
        f"Student:  {student_name} ({student_email})\n"
        f"Submitted: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}\n\n"
        f"Responses are attached as a PDF."
    )
    msg.attach(MIMEText(body, 'plain'))

    attachment = MIMEApplication(pdf_bytes, _subtype='pdf')
    attachment.add_header('Content-Disposition', 'attachment', filename=filename)
    msg.attach(attachment)

    if evidence_file and evidence_filename:
        ext = evidence_filename.rsplit('.', 1)[-1].lower() if '.' in evidence_filename else 'bin'
        subtype = 'octet-stream'
        if ext in ('png', 'jpg', 'jpeg'):
            subtype = ext
        elif ext == 'pdf':
            subtype = 'pdf'
        evidence_attachment = MIMEApplication(evidence_file, _subtype=subtype)
        evidence_attachment.add_header('Content-Disposition', 'attachment', filename=evidence_filename)
        msg.attach(evidence_attachment)

    with smtplib.SMTP('smtp.gmail.com', 587) as server:
        server.starttls()
        server.login(smtp_user, smtp_password)
        server.send_message(msg)
