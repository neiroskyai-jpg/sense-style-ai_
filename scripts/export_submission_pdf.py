"""Экспорт документов подачи в PDF — конкурс принимает только его.

    python scripts/export_submission_pdf.py            # все готовые
    python scripts/export_submission_pdf.py 04-cv      # конкретный

Работает через установленный Microsoft Office (COM): Word кладёт .docx в PDF, PowerPoint — .pptx.
Это не «печать в PDF» из HTML: вёрстка, таблицы и шрифты остаются ровно теми, что видно в Word,
поэтому итог совпадает с тем, что проверяли по числу страниц.

Только Windows с установленным Office. Файлы кладутся в submission/pdf-final/.
"""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent
DOCX = ROOT / "submission" / "docx"
PPTX = ROOT / "submission" / "pptx"
OUT = ROOT / "submission" / "pdf-final"

WD_FORMAT_PDF = 17
PP_FORMAT_PDF = 32

# Что реально уходит в заявку. Сценарий защиты и рабочие файлы (07, 08) — не подаются.
DOCS = ["01-описание-проекта", "04-cv", "05-мотивационное-письмо"]
DECKS = ["02-презентация-ИТМО", "02-презентация"]


def export_word(names):
    import win32com.client as com
    app = com.Dispatch("Word.Application")
    app.Visible = False
    done = []
    try:
        for name in names:
            src = DOCX / f"{name}.docx"
            if not src.exists():
                print(f"  ! нет файла: {src.relative_to(ROOT)}")
                continue
            doc = app.Documents.Open(str(src), False, True)
            dest = OUT / f"{name}.pdf"
            try:
                doc.SaveAs(str(dest), FileFormat=WD_FORMAT_PDF)
                done.append((name, doc.ComputeStatistics(2)))
            finally:
                doc.Close(0)
    finally:
        app.Quit()
    return done


def export_ppt(names):
    import win32com.client as com
    app = com.Dispatch("PowerPoint.Application")
    done = []
    try:
        for name in names:
            src = PPTX / f"{name}.pptx"
            if not src.exists():
                print(f"  ! нет файла: {src.relative_to(ROOT)}")
                continue
            pres = app.Presentations.Open(str(src), WithWindow=False)
            try:
                pres.SaveAs(str(OUT / f"{name}.pdf"), PP_FORMAT_PDF)
                done.append((name, pres.Slides.Count))
            finally:
                pres.Close()
    finally:
        app.Quit()
    return done


def main() -> int:
    try:
        import win32com.client  # noqa: F401
    except ImportError:
        print("Нужен pywin32:  pip install pywin32")
        return 1

    only = sys.argv[1:]
    docs = [d for d in DOCS if not only or d in only]
    decks = [d for d in DECKS if not only or d in only]

    OUT.mkdir(parents=True, exist_ok=True)
    for name, pages in export_word(docs):
        print(f"OK: {name}.pdf — {pages} стр.")
    for name, slides in export_ppt(decks):
        print(f"OK: {name}.pdf — {slides} слайдов")
    print(f"\nPDF — в {OUT.relative_to(ROOT)}/. Это то, что загружается в кабинет ИТМО.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
