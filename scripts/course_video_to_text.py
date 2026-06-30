"""Пайплайн «видео урока → текст + ключевые кадры» для встраивания методологии в RAG.

Кладёшь видео в architecture/reference/raw-inbox/course/inbox/ — запускаешь скрипт —
получаешь транскрипт (.txt + .srt) и ключевые кадры слайдов (.png). Само видео не нужно.

Запуск:  python -m scripts.course_video_to_text

Зависимости: ffmpeg (системный) + faster-whisper (pip). Скрипт проверяет их и, если чего-то
нет, печатает точную команду установки и останавливается.

Язык уроков — русский, поэтому модель large-v3 (мелкие на русском сильно врут).
Нет GPU → CPU/int8: час урока ≈ час+ обработки, это нормально для пакетного прогона.
"""
from __future__ import annotations

import glob
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

# ── ПАРАМЕТРЫ (правь тут или через env) ───────────────────────────────────────
SCENE_THRESHOLD = float(os.environ.get("SCENE_THRESHOLD", "0.3"))  # смена слайда (0..1)
# SLIDES_ONLY=1 → только ключевые кадры (без модели/транскрипции; не нужно скачивать 3 ГБ):
SLIDES_ONLY = bool(os.environ.get("SLIDES_ONLY"))
WHISPER_MODEL = "large-v3"     # точная для русского; "medium" — быстрее, но грубее
COMPUTE_TYPE = "int8"          # CPU-режим; на GPU можно "float16"
DEVICE = "cpu"                 # нет CUDA → cpu
LANG = "ru"
AUDIO_SR = 16000               # 16 kHz mono — то, что любит whisper
VIDEO_EXTS = (".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v")

ROOT = Path(__file__).resolve().parent.parent
COURSE = ROOT / "architecture" / "reference" / "raw-inbox" / "course"
# Видео можно держать НЕ на диске проекта (если C переполнен / это OneDrive):
#   задай папку через env COURSE_INBOX, напр.  set COURSE_INBOX=D:\course-video
INBOX = Path(os.environ["COURSE_INBOX"]) if os.environ.get("COURSE_INBOX") else COURSE / "inbox"
# Лёгкий результат (текст + кадры) всегда в проект:
TRANSCRIPTS = COURSE / "transcripts"
SLIDES = COURSE / "slides"
# Куда качать модель Whisper (~3 ГБ). По умолчанию — кэш HF; можно увести на другой диск:
#   set WHISPER_MODEL_DIR=D:\whisper-models
MODEL_DIR = os.environ.get("WHISPER_MODEL_DIR") or None


# ── Поиск ffmpeg/ffprobe (PATH может не обновиться после установки) ────────────

def _find_binary(name: str) -> str | None:
    # 1) PATH
    p = shutil.which(name) or shutil.which(name + ".exe")
    if p:
        return p
    # 2) переопределение через env (папка с бинарниками)
    env_dir = os.environ.get("FFMPEG_BIN")
    if env_dir:
        cand = Path(env_dir) / (name + (".exe" if os.name == "nt" else ""))
        if cand.exists():
            return str(cand)
    # 3) типовые места установки на Windows (winget Gyan + chocolatey)
    home = Path.home()
    patterns = [
        str(home / "AppData/Local/Microsoft/WinGet/Packages/Gyan.FFmpeg*/**/bin" / (name + ".exe")),
        "C:/ProgramData/chocolatey/bin/" + name + ".exe",
        "C:/ffmpeg/bin/" + name + ".exe",
    ]
    for pat in patterns:
        hits = glob.glob(pat, recursive=True)
        if hits:
            return hits[0]
    return None


def check_deps() -> tuple[str, str]:
    ffmpeg = _find_binary("ffmpeg")
    ffprobe = _find_binary("ffprobe")
    if not ffmpeg or not ffprobe:
        print("[СТОП] Не найден ffmpeg/ffprobe. Установи (PowerShell):")
        print("    winget install Gyan.FFmpeg")
        print("  (или: choco install ffmpeg -y  — от администратора)")
        print("  Затем перезапусти терминал. Можно указать папку с бинарниками в env FFMPEG_BIN.")
        sys.exit(1)
    try:
        import faster_whisper  # noqa: F401
    except ImportError:
        print("[СТОП] Не установлен faster-whisper. Установи:")
        print("    pip install faster-whisper")
        sys.exit(1)
    return ffmpeg, ffprobe


# ── Шаги обработки ────────────────────────────────────────────────────────────

def _run(cmd: list[str]) -> None:
    subprocess.run(cmd, check=True)


def extract_audio(ffmpeg: str, video: Path, wav: Path) -> None:
    _run([ffmpeg, "-i", str(video), "-vn", "-ac", "1", "-ar", str(AUDIO_SR),
          "-y", "-loglevel", "error", str(wav)])


def _ts(sec: float) -> str:
    h = int(sec // 3600); m = int((sec % 3600) // 60)
    s = int(sec % 60); ms = int(round((sec - int(sec)) * 1000))
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def transcribe(model, wav: Path, txt: Path, srt: Path) -> int:
    segments, _info = model.transcribe(str(wav), language=LANG, vad_filter=True)
    n = 0
    with txt.open("w", encoding="utf-8") as ft, srt.open("w", encoding="utf-8") as fs:
        for seg in segments:
            line = (seg.text or "").strip()
            if not line:
                continue
            n += 1
            ft.write(line + "\n")
            fs.write(f"{n}\n{_ts(seg.start)} --> {_ts(seg.end)}\n{line}\n\n")
    return n


def extract_keyframes(ffmpeg: str, video: Path, out_dir: Path) -> int:
    out_dir.mkdir(parents=True, exist_ok=True)
    _run([ffmpeg, "-i", str(video),
          "-vf", f"select='gt(scene,{SCENE_THRESHOLD})'",
          "-fps_mode", "vfr", "-y", "-loglevel", "error",
          str(out_dir / "slide_%04d.png")])
    return len(list(out_dir.glob("slide_*.png")))


# ── Главный прогон ────────────────────────────────────────────────────────────

def main() -> None:
    ffmpeg, _ffprobe = check_deps()
    for d in (INBOX, TRANSCRIPTS, SLIDES):
        d.mkdir(parents=True, exist_ok=True)

    # рекурсивно — можно раскладывать видео по подпапкам-темам (напр. inbox/цветотипы/)
    videos = sorted(p for p in INBOX.rglob("*")
                    if p.is_file() and p.suffix.lower() in VIDEO_EXTS)
    if not videos:
        print(f"В inbox нет видео ({', '.join(VIDEO_EXTS)}). Положи файлы в:\n  {INBOX}")
        return

    # Режим «только кадры» — без модели и без скачивания 3 ГБ (для разбора слайдов).
    if SLIDES_ONLY:
        processed, skipped = 0, 0
        for video in videos:
            name = "_".join(video.relative_to(INBOX).with_suffix("").parts)
            sdir = SLIDES / name
            if sdir.exists() and any(sdir.glob("slide_*.png")):
                print(f"  ↷ пропуск (кадры есть): {video.name}")
                skipped += 1
                continue
            print(f"  ▶ кадры: {video.name}")
            try:
                frames = extract_keyframes(ffmpeg, video, sdir)
                print(f"    ✓ кадров слайдов: {frames}")
                processed += 1
            except Exception as e:  # noqa: BLE001
                print(f"    ✗ ошибка на {video.name}: {e}")
        print(f"\n[SLIDES_ONLY] кадры: обработано {processed}, пропущено {skipped} → {SLIDES}")
        return

    # модель грузим ОДИН раз (первый запуск качает large-v3 ~3 ГБ)
    print(f"Загружаю модель Whisper '{WHISPER_MODEL}' ({DEVICE}/{COMPUTE_TYPE})… "
          "первый раз это скачивание ~3 ГБ.")
    from faster_whisper import WhisperModel
    model = WhisperModel(WHISPER_MODEL, device=DEVICE, compute_type=COMPUTE_TYPE,
                         download_root=MODEL_DIR)

    processed, skipped = 0, 0
    for video in videos:
        # имя результата включает подпапку-тему: «цветотипы_20260525_111107»
        name = "_".join(video.relative_to(INBOX).with_suffix("").parts)
        txt = TRANSCRIPTS / f"{name}.txt"
        srt = TRANSCRIPTS / f"{name}.srt"
        if txt.exists():  # идемпотентность
            print(f"  ↷ пропуск (уже есть): {video.name}")
            skipped += 1
            continue
        print(f"  ▶ обрабатываю: {video.name}")
        with tempfile.TemporaryDirectory() as tmp:
            wav = Path(tmp) / f"{name}.wav"
            try:
                extract_audio(ffmpeg, video, wav)
                segs = transcribe(model, wav, txt, srt)
                frames = extract_keyframes(ffmpeg, video, SLIDES / name)
                print(f"    ✓ транскрипт: {segs} сегментов · кадров слайдов: {frames}")
                processed += 1
            except subprocess.CalledProcessError as e:
                print(f"    ✗ ошибка ffmpeg на {video.name}: {e}")
            except Exception as e:  # noqa: BLE001
                print(f"    ✗ ошибка на {video.name}: {e}")
                # частичный .txt без полноты удаляем, чтобы не пометить как готовый
                if txt.exists() and txt.stat().st_size == 0:
                    txt.unlink(missing_ok=True)

    print("\n── СВОДКА ─────────────────────────────")
    print(f"видео обработано : {processed}")
    print(f"пропущено (готовы): {skipped}")
    print(f"транскрипты      : {TRANSCRIPTS}")
    print(f"кадры слайдов    : {SLIDES}")
    print("Дальше: скажи Claude «загрузила» — он прочитает транскрипты и кадры,")
    print("вытащит правила в текст и встроит в методологию (RAG).")


if __name__ == "__main__":
    main()
