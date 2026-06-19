"""Unit checks for app.normalize_ocr_text — focused on numbers/codes that used to
get corrupted when a hyphen landed at an OCR line break.

Pure-function tests (no Azure calls), but importing app.py validates env and
builds clients, so we inject a dummy Telegram token first (Azure keys come from
.env). Run:  python qa/test_normalize.py
"""
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
os.environ.setdefault("TELEGRAM_API_TOKEN", "qa-dummy-token")
os.environ.setdefault("BOT_ENV", "qa")
sys.path.insert(0, str(REPO_ROOT))

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

from app import normalize_ocr_text, prepare_tts_text  # noqa: E402

# (description, input, substring that MUST be present, substring that MUST be absent)
CASES = [
    ("word hyphenation joins", "hyphen-\nation works", "hyphenation", "hyphen ation"),
    ("cyrillic word hyphenation joins", "сло-\nво тут", "слово", "сло во"),
    ("numeric code not merged", "норматив СН 2.02.05-\n2020 утверждён",
     "2.02.05-2020", "2.02.052020"),
    ("numeric range not merged", "диапазон 10-\n15 метров", "10-15", "1015"),
    ("decimal+unit survives", "площадь 106.4\nм² всего", "106.4", "1064"),
    ("plain line break becomes space", "первая строка\nвторая строка",
     "строка вторая", "строкавторая"),
]


# prepare_tts_text: (description, locale, input, must-have, must-be-absent)
TTS_CASES = [
    ("uk expands м2 to words", "uk", "площа 106.4 м2 всього",
     "квадратних метрів", "м2"),
    ("uk expands м3 to words", "uk", "обсяг 408.0 м3", "кубічних метрів", "м3"),
    ("uk spells grouped millions", "uk", "ціна 1 250 000 грн",
     "мільйон", "1 250 000"),
    ("uk spells plain large number", "uk", "ціна 1250000 грн", "мільйон", "1250000"),
    ("uk keeps phone groups", "uk", "тел +380 67 123-45-67", "123-45-67", "мільйон"),
    ("uk leaves 4-digit year alone", "uk", "у 2025 році", "2025", "тисячі"),
    ("uk leaves time/decimals alone", "uk", "о 14:30, площа 106.4", "14:30", "сто"),
    ("ru is left unchanged", "ru", "площа 106.4 м2, 1 250 000", "1 250 000", "квадратних"),
    ("en is left unchanged", "en", "area 106.4 m2", "m2", "квадратних"),
]


def main():
    failures = 0
    for desc, raw, must_have, must_not in CASES:
        out = normalize_ocr_text(raw)
        ok = (must_have in out) and (must_not not in out)
        print(f"  {'✓' if ok else '✗'} {desc}")
        if not ok:
            failures += 1
            print(f"      input : {raw!r}\n      output: {out!r}")
    for desc, loc, raw, must_have, must_not in TTS_CASES:
        out = prepare_tts_text(raw, loc)
        ok = (must_have in out) and (must_not not in out)
        print(f"  {'✓' if ok else '✗'} {desc}")
        if not ok:
            failures += 1
            print(f"      input : {raw!r}\n      output: {out!r}")
    total = len(CASES) + len(TTS_CASES)
    print(f"\n{total - failures}/{total} passed")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
