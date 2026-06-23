"""Unit checks for the LLM-OCR parsing/segment helpers (pure, no Azure calls).

The live Azure OpenAI path (run_llm_ocr) needs a provisioned gpt-4o-mini
deployment and is validated separately via run_ocr.py once configured. Here we
only exercise the deterministic parsing/segment-building logic.

Run:  python qa/test_llm_ocr.py
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

from app import _parse_llm_segments, _segments_from_raw, _img_mime  # noqa: E402


def run():
    failures = []

    def check(name, cond):
        print(f"  {'✓' if cond else '✗'} {name}")
        if not cond:
            failures.append(name)

    # _parse_llm_segments: clean JSON
    clean = '{"segments":[{"lang":"ka","text":"გამარჯობა"},{"lang":"en","text":"Hello"}]}'
    p = _parse_llm_segments(clean)
    check("parses clean json", p == [("ka", "გამარჯობა"), ("en", "Hello")])

    # tolerant of code fences + surrounding prose
    fenced = "Here you go:\n```json\n" + clean + "\n```\nDone."
    check("parses fenced json", _parse_llm_segments(fenced) == p)

    # garbage / empty -> []
    check("garbage -> []", _parse_llm_segments("not json at all") == [])
    check("empty -> []", _parse_llm_segments("") == [])

    # drops empty/whitespace text and missing lang
    messy = '{"segments":[{"lang":"ka","text":"  "},{"lang":"","text":"x"},{"lang":"hy","text":"Բարև"}]}'
    check("drops empty/no-lang spans", _parse_llm_segments(messy) == [("hy", "Բարև")])

    # _segments_from_raw: single known language -> (lang, None)
    dom, segs = _segments_from_raw([("ka", "გამარჯობა მსოფლიო")], "ka")
    check("single lang -> no segments", dom == "ka" and segs is None)

    # two known languages -> dominant by text length + segments preserved
    dom, segs = _segments_from_raw(
        [("en", "short"), ("ka", "გაცილებით უფრო გრძელი ქართული ტექსტი აქ")], "ka")
    check("two langs -> dominant ka", dom == "ka")
    check("two langs -> segments built", segs is not None and len(segs) == 2)

    # voiceless locale dropped; if only that remains -> fallback + None
    dom, segs = _segments_from_raw([("xx", "unknown script")], "ka")
    check("voiceless folds to fallback", dom == "ka" and segs is None)

    # _img_mime sniffing
    check("jpeg magic", _img_mime(b"\xff\xd8\xff\xe0\x00\x10JFIF") == "image/jpeg")
    check("png magic", _img_mime(b"\x89PNG\r\n\x1a\n....") == "image/png")
    check("webp magic", _img_mime(b"RIFF\x00\x00\x00\x00WEBPVP8 ") == "image/webp")

    print()
    if failures:
        print(f"FAILED: {len(failures)} — {', '.join(failures)}")
        return 1
    print("All LLM-OCR helper tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(run())
