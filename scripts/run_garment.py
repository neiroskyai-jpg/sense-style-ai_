"""«Брать / не брать»: фото вещи → вердикт по Формуле стиля клиентки.

Пример:
    python -m scripts.run_garment вещь.jpg
"""
import argparse
import json
import sys
from pathlib import Path

from core.pipeline import evaluate_garment
from scripts.run_eval import DEMO_DIAGNOSIS


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    ap = argparse.ArgumentParser(description="фото вещи → брать/заменить/пропустить")
    ap.add_argument("photo", help="фото вещи (магазин/шкаф)")
    ap.add_argument("--diagnosis", default=None, help="JSON диагностики (иначе демо-кейс)")
    ap.add_argument("--mode", default="dev", choices=["dev", "final"])
    args = ap.parse_args()

    diag = (json.loads(Path(args.diagnosis).read_text(encoding="utf-8"))
            if args.diagnosis else DEMO_DIAGNOSIS)
    verdict = evaluate_garment(args.photo, diag, mode=args.mode)
    print(json.dumps(verdict, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
