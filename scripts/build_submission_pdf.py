"""Сборка документов подачи ИТМО в print-ready HTML → PDF.

Зачем: материалы конкурса лежат в `submission/*.md`. Жюри нужен PDF. Без pandoc/LaTeX надёжный
путь — аккуратный HTML под печать (A4, типографика, разрывы страниц): открыть в браузере и
«Печать → Сохранить как PDF». Результат кладём в `submission/pdf/`.

ЗАПУСК:
    python scripts/build_submission_pdf.py            # 01-описание + 02-презентация
    python scripts/build_submission_pdf.py --all      # все .md из submission/
    python scripts/build_submission_pdf.py 04-cv.md   # конкретный файл

Затем: открыть `submission/pdf/<имя>.html` → Ctrl/Cmd+P → «Сохранить как PDF», поля «по умолчанию».
"""
from __future__ import annotations
import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "submission"
OUT = SRC / "pdf"
DEFAULT = ["01-описание-проекта.md", "02-презентация.md"]


def _md_to_html(md: str) -> str:
    """Markdown → HTML. Используем библиотеку `markdown` (в requirements); фолбэк — минимальный
    конвертер для нашего подмножества (заголовки, списки, таблицы, жирный, цитаты, код)."""
    try:
        import markdown  # объявлен в requirements
        return markdown.markdown(md, extensions=["tables", "fenced_code", "sane_lists"])
    except Exception:  # noqa: BLE001 — нет библиотеки в этом окружении → свой конвертер
        return _fallback_md(md)


def _inline(s: str) -> str:
    s = (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
    s = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", s)
    s = re.sub(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)", r"<em>\1</em>", s)
    s = re.sub(r"`(.+?)`", r"<code>\1</code>", s)
    s = re.sub(r"\[(.+?)\]\((.+?)\)", r'<a href="\2">\1</a>', s)
    return s


def _fallback_md(md: str) -> str:
    out, lines, i = [], md.split("\n"), 0
    while i < len(lines):
        ln = lines[i]
        if re.match(r"^\s*\|.*\|\s*$", ln) and i + 1 < len(lines) and re.match(r"^\s*\|[\s:|-]+\|\s*$", lines[i + 1]):
            head = [c.strip() for c in ln.strip().strip("|").split("|")]
            out.append("<table><thead><tr>" + "".join(f"<th>{_inline(c)}</th>" for c in head) + "</tr></thead><tbody>")
            i += 2
            while i < len(lines) and re.match(r"^\s*\|.*\|\s*$", lines[i]):
                cells = [c.strip() for c in lines[i].strip().strip("|").split("|")]
                out.append("<tr>" + "".join(f"<td>{_inline(c)}</td>" for c in cells) + "</tr>")
                i += 1
            out.append("</tbody></table>")
            continue
        m = re.match(r"^(#{1,4})\s+(.*)$", ln)
        if m:
            lvl = len(m.group(1))
            out.append(f"<h{lvl}>{_inline(m.group(2))}</h{lvl}>")
            i += 1
            continue
        if re.match(r"^\s*[-*]\s+", ln):
            out.append("<ul>")
            while i < len(lines) and re.match(r"^\s*[-*]\s+", lines[i]):
                out.append("<li>" + _inline(re.sub(r"^\s*[-*]\s+", "", lines[i])) + "</li>")
                i += 1
            out.append("</ul>")
            continue
        if re.match(r"^\s*\d+\.\s+", ln):
            out.append("<ol>")
            while i < len(lines) and re.match(r"^\s*\d+\.\s+", lines[i]):
                out.append("<li>" + _inline(re.sub(r"^\s*\d+\.\s+", "", lines[i])) + "</li>")
                i += 1
            out.append("</ol>")
            continue
        if ln.startswith(">"):
            out.append("<blockquote>" + _inline(ln.lstrip("> ").rstrip()) + "</blockquote>")
            i += 1
            continue
        if ln.strip() == "---":
            out.append("<hr>")
            i += 1
            continue
        if ln.strip():
            out.append("<p>" + _inline(ln) + "</p>")
        i += 1
    return "\n".join(out)


_TEMPLATE = """<!doctype html><html lang=ru><head><meta charset=utf-8>
<title>{title}</title>
<style>
  @page {{ size: A4; margin: 18mm 16mm; }}
  * {{ box-sizing: border-box; }}
  body {{ font-family: Georgia, 'Times New Roman', serif; color: #1c1c1c; line-height: 1.5;
    max-width: 780px; margin: 0 auto; padding: 24px; font-size: 12pt; }}
  h1 {{ font-size: 23pt; line-height: 1.15; margin: 0 0 4px; }}
  h2 {{ font-size: 16pt; margin: 26px 0 8px; border-bottom: 1px solid #ddd; padding-bottom: 4px;
    page-break-after: avoid; }}
  h3 {{ font-size: 13pt; margin: 18px 0 6px; page-break-after: avoid; }}
  h4 {{ font-size: 12pt; margin: 14px 0 4px; }}
  p, li {{ orphans: 3; widows: 3; }}
  ul, ol {{ padding-left: 22px; margin: 8px 0; }}
  li {{ margin: 4px 0; }}
  table {{ border-collapse: collapse; width: 100%; margin: 12px 0; font-size: 10.5pt;
    page-break-inside: avoid; }}
  th, td {{ border: 1px solid #ccc; padding: 6px 9px; text-align: left; vertical-align: top; }}
  th {{ background: #f3f0ea; font-family: -apple-system, 'Segoe UI', sans-serif; }}
  blockquote {{ border-left: 3px solid #5D2230; margin: 12px 0; padding: 4px 0 4px 16px;
    color: #444; font-style: italic; }}
  code {{ font-family: Consolas, monospace; background: #f3f0ea; padding: 1px 5px; border-radius: 4px;
    font-size: 10.5pt; }}
  pre {{ background: #f3f0ea; padding: 12px; border-radius: 8px; overflow-x: auto; page-break-inside: avoid; }}
  hr {{ border: 0; border-top: 1px solid #ddd; margin: 20px 0; }}
  a {{ color: #5D2230; }}
  .foot {{ margin-top: 30px; padding-top: 12px; border-top: 1px solid #ddd; font-size: 9pt; color: #999; }}
  @media screen {{ body {{ background: #f6f4ef; }} .sheet {{ background:#fff; box-shadow:0 2px 20px rgba(0,0,0,.08);
    padding: 40px 48px; border-radius: 6px; }} .hint {{ max-width:780px; margin: 14px auto; color:#8a8175;
    font-size: 13px; font-family: sans-serif; }} }}
  @media print {{ .hint {{ display: none; }} .sheet {{ box-shadow: none; padding: 0; }} }}
</style></head><body>
<p class=hint>Печать → «Сохранить как PDF». Поля — по умолчанию, фон включать не нужно.</p>
<div class=sheet>
{body}
<div class=foot>Sense Style AI · документ подачи ИТМО · собрано из {src}</div>
</div>
</body></html>"""


def build(name: str) -> Path:
    src = SRC / name
    # Служебные заметки в <!-- --> — для .md, в документ для жюри не идут (см. build_submission_docx.py).
    md = re.sub(r"<!--.*?-->", "", src.read_text(encoding="utf-8"), flags=re.S)
    m = re.search(r"^#\s+(.+)$", md, re.M)
    title = m.group(1).strip() if m else name
    html = _TEMPLATE.format(title=title, body=_md_to_html(md), src=name)
    OUT.mkdir(parents=True, exist_ok=True)
    dest = OUT / (Path(name).stem + ".html")
    dest.write_text(html, encoding="utf-8")
    return dest


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("files", nargs="*", help="конкретные .md из submission/ (по умолчанию — описание+презентация)")
    ap.add_argument("--all", action="store_true", help="все .md из submission/")
    args = ap.parse_args()

    if args.all:
        targets = sorted(p.name for p in SRC.glob("*.md"))
    elif args.files:
        targets = args.files
    else:
        targets = DEFAULT

    for name in targets:
        if not (SRC / name).exists():
            print(f"  ! нет файла: submission/{name}", file=sys.stderr)
            continue
        dest = build(name)
        # ASCII: «✓»/«→» роняют печать в cp1251-консоли Windows уже после записи файла.
        print(f"OK: {name} -> {dest.relative_to(ROOT)}")
    print("\nОткрой HTML в браузере и Ctrl/Cmd+P -> «Сохранить как PDF».")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
