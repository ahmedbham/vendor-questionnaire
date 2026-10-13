import io

from openpyxl import Workbook, load_workbook


def import_questions(data: bytes) -> list[dict]:
    """Read the first sheet. Columns: Section, Question, [Type: text|yesno|longtext]."""
    ws = load_workbook(io.BytesIO(data), read_only=True).active
    questions = []
    for i, row in enumerate(ws.iter_rows(values_only=True)):
        if i == 0 or not row or len(row) < 2 or not row[1]:
            continue
        qtype = str(row[2]).strip().lower() if len(row) > 2 and row[2] else "longtext"
        if qtype not in ("text", "yesno", "longtext"):
            qtype = "longtext"
        questions.append({
            "id": f"q{len(questions) + 1}",
            "section": str(row[0] or "General").strip(),
            "text": str(row[1]).strip(),
            "type": qtype,
        })
    return questions


def export_responses(vendor: dict, questions: list[dict], response: dict) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.append(["Vendor", vendor["name"], "Status", response.get("status", "draft")])
    ws.append(["Section", "Question", "Answer"])
    answers = response.get("answers", {})
    for q in questions:
        ws.append([q["section"], q["text"], answers.get(q["id"], "")])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
