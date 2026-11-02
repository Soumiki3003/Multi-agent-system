"""PDF export for instructor-facing student chat transcripts."""

from io import BytesIO

from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import HRFlowable, Paragraph, SimpleDocTemplate, Spacer

from app.models import ChatMessage


def build_chat_transcript_pdf(
    course_name: str,
    student_name: str,
    student_email: str,
    ctf_label: str,
    messages: list[ChatMessage],
) -> bytes:
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
    title_style = ParagraphStyle('CTitle', parent=base['Title'], fontSize=16, spaceAfter=4)
    meta_style = ParagraphStyle('CMeta', parent=base['Normal'], fontSize=9, textColor=colors.grey)
    speaker_style = ParagraphStyle('CSpeaker', parent=base['Normal'], fontSize=8, spaceBefore=10,
                                    textColor=colors.HexColor('#666666'))
    student_msg_style = ParagraphStyle('CStudentMsg', parent=base['Normal'], fontSize=9, leading=13,
                                        leftIndent=16, textColor=colors.HexColor('#1a5276'))
    assistant_msg_style = ParagraphStyle('CAssistantMsg', parent=base['Normal'], fontSize=9, leading=13,
                                          leftIndent=16, textColor=colors.HexColor('#333333'))

    story = [
        Paragraph(f"{course_name} — {ctf_label} Chat Transcript", title_style),
        Paragraph(f'Student: {student_name}  ({student_email})', meta_style),
        Paragraph(f'{len(messages)} message{"s" if len(messages) != 1 else ""}', meta_style),
        Spacer(1, 8),
        HRFlowable(width='100%', thickness=1, color=colors.HexColor('#cccccc')),
        Spacer(1, 8),
    ]

    if not messages:
        story.append(Paragraph("No chat messages recorded for this challenge.", meta_style))

    for msg in messages:
        speaker = "Student" if msg.role == "user" else "Assistant"
        style = student_msg_style if msg.role == "user" else assistant_msg_style
        content = (
            msg.content.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace("\n", "<br/>")
        )
        story.append(Paragraph(speaker, speaker_style))
        story.append(Paragraph(content, style))

    doc.build(story)
    buf.seek(0)
    return buf.read()
