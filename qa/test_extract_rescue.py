"""Regression test for the 3-way-language-split rescue trigger in extract_text.

Reproduces a real prod failure: a Georgian page that Azure Read can't actually
read came back split into three *supported* languages (Thai/English/
Indonesian) with high confidence — none of them Georgian — because Azure's
per-line classifier guessed wrong on every line. The old rescue only fired on
empty text or an unsupported dominant locale, so this page sailed through and
got rendered with three wrong voices instead of running the LLM-OCR rescue.

Mocks doc_client.begin_analyze_document and run_llm_ocr (no live Azure calls).
Run:  python qa/test_extract_rescue.py
"""
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parent.parent
os.environ.setdefault("TELEGRAM_API_TOKEN", "qa-dummy-token")
os.environ.setdefault("BOT_ENV", "qa")
sys.path.insert(0, str(REPO_ROOT))

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

import app  # noqa: E402


class _Span:
    def __init__(self, offset, length):
        self.offset = offset
        self.length = length


class _Lang:
    def __init__(self, locale, confidence, spans):
        self.locale = locale
        self.confidence = confidence
        self.spans = [_Span(o, l) for o, l in spans]


class _Line:
    def __init__(self, content):
        self.content = content


class _Page:
    def __init__(self, content):
        self.lines = [_Line(content)]


class _Result:
    def __init__(self, content, languages):
        self.content = content
        self.languages = languages
        self.pages = [_Page(content)]


class _Poller:
    def __init__(self, result):
        self._result = result

    def result(self):
        return self._result


def run():
    failures = []

    def check(name, cond):
        print(f"  {'✓' if cond else '✗'} {name}")
        if not cond:
            failures.append(name)

    fake_path = tempfile.mktemp()
    Path(fake_path).write_bytes(b"\x00")

    # A garbage-but-plausible 3-way split: Azure tags three equal-length runs
    # as th/en/id, none of them Georgian — detect_script_language finds no
    # dominant script (plain Latin-range filler), so it can't override.
    seg_th, seg_en, seg_id = "T" * 10, "E" * 10, "I" * 10
    content = seg_th + seg_en + seg_id
    bad_result = _Result(content, [
        _Lang("th-TH", 0.91, [(0, 10)]),
        _Lang("en-US", 0.85, [(10, 10)]),
        _Lang("id-ID", 0.80, [(20, 10)]),
    ])

    rescued_text = "ნამდვილი ქართული ტექსტი"

    with patch.object(app.doc_client, "begin_analyze_document",
                       return_value=_Poller(bad_result)), \
         patch.object(app, "run_llm_ocr",
                       return_value=(rescued_text, [("ka", rescued_text)])), \
         patch.object(app, "OCR_FALLBACK", "llm"), \
         patch.object(app, "_azure_openai_configured", return_value=True):
        result = app.extract_text(fake_path, "image")

    check("3-way split triggers the LLM rescue", result.text.startswith(rescued_text))
    check("rescued result is tagged ka", result.locale2 == "ka")
    check("rescued result marked used_fallback", result.used_fallback is True)

    # Sanity: a genuine 2-language page (ru+fr) must NOT be rescued away —
    # the >=3 trigger must not catch ordinary multilingual pages.
    ru, fr = "Р" * 30, "F" * 10
    good_result = _Result(ru + fr, [
        _Lang("ru-RU", 0.99, [(0, 30)]),
        _Lang("fr-FR", 0.95, [(30, 10)]),
    ])
    with patch.object(app.doc_client, "begin_analyze_document",
                       return_value=_Poller(good_result)), \
         patch.object(app, "run_llm_ocr", side_effect=AssertionError("should not be called")), \
         patch.object(app, "OCR_FALLBACK", "llm"), \
         patch.object(app, "_azure_openai_configured", return_value=True):
        result2 = app.extract_text(fake_path, "image")

    check("2-language page is not rescued", result2.locale2 == "ru" and result2.segments is not None)

    os.remove(fake_path)
    print()
    if failures:
        print(f"FAILED: {len(failures)} — {', '.join(failures)}")
        return 1
    print("All extract_text rescue tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(run())
