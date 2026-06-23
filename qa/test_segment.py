"""Unit checks for app.build_language_segments and the multi-voice SSML path.

Pure-function tests (no Azure calls): we fake an Azure AnalyzeResult with a
`content` string and per-language `spans`, the same shape the Read model returns.
Importing app.py validates env and builds clients, so inject a dummy Telegram
token first (Azure keys come from .env). Run:  python qa/test_segment.py
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

from app import build_language_segments, synthesize_to_file, VOICE_MAP  # noqa: E402


class _Span:
    def __init__(self, offset, length):
        self.offset = offset
        self.length = length


class _Lang:
    def __init__(self, locale, spans):
        self.locale = locale
        self.spans = [_Span(o, l) for o, l in spans]


class _Result:
    def __init__(self, content, languages):
        self.content = content
        self.languages = languages


def _ru_fr():
    ru = "Анна Павловна улыбнулась и обещала заняться Пьером. "  # 51 chars
    fr = "Eh bien, mon prince, Gênes n'est plus qu'une apanage."   # 53 chars
    content = ru + fr
    result = _Result(content, [
        _Lang("ru", [(0, len(ru))]),
        _Lang("fr", [(len(ru), len(fr))]),
    ])
    return content, ru, fr, result


def run():
    failures = []

    def check(name, cond):
        mark = "✓" if cond else "✗"
        print(f"  {mark} {name}")
        if not cond:
            failures.append(name)

    # 1. Monolingual page -> None (single-voice path unchanged).
    text = "The library opened a new reading room this spring."
    mono = _Result(text, [_Lang("en", [(0, len(text))])])
    check("monolingual returns None", build_language_segments(mono, "en") is None)

    # 2. Russian + French -> two ordered segments, no text lost.
    content, ru, fr, result = _ru_fr()
    segs = build_language_segments(result, "ru")
    check("ru+fr returns segments", segs is not None and len(segs) == 2)
    if segs:
        check("ru+fr order and locales", [s[0] for s in segs] == ["ru", "fr"])
        check("ru+fr preserves all text", "".join(s[1] for s in segs) == content)

    # 3. Tiny secondary language (< MULTI_LANG_MIN_SHARE) -> folded, None.
    base = "Это полностью русский текст про городскую библиотеку и читателей. " * 2
    tiny = base + "OK"
    res3 = _Result(tiny, [_Lang("ru", [(0, len(base))]), _Lang("en", [(len(base), 2)])])
    check("tiny secondary stays monolingual", build_language_segments(res3, "ru") is None)

    # 4. Unknown locale (no voice) folded into dominant -> None.
    res4 = _Result(content, [_Lang("ru", [(0, len(ru))]), _Lang("xx", [(len(ru), len(fr))])])
    check("unknown locale folds to None", build_language_segments(res4, "ru") is None)

    # 5. Per-span voice blocks use the right (distinct) voice per language.
    import app as _app
    blk_ru = _app._voice_ssml_block(ru, "ru")
    blk_fr = _app._voice_ssml_block(fr, "fr")
    check("ru voice block uses ru voice", VOICE_MAP["ru"]["voice"] in blk_ru)
    check("fr voice block uses fr voice", VOICE_MAP["fr"]["voice"] in blk_fr)
    check("ru/fr voices differ", VOICE_MAP["ru"]["voice"] != VOICE_MAP["fr"]["voice"])

    print()
    if failures:
        print(f"FAILED: {len(failures)} — {', '.join(failures)}")
        return 1
    print("All segment tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(run())
