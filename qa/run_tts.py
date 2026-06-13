"""QA runner for text-to-speech: a smoke test over the bot's TTS voices.

For each language it synthesizes a short sample via the bot's real
`synthesize_to_file()` and checks that Azure returns completed audio — catching a
broken/renamed voice id, a wrong locale, or a region/key problem before users do.
It does NOT judge audio quality (that needs a human ear); it verifies the voice
pipeline works and reports audio size/duration per language.

Cost: TTS is billed per character, so samples are deliberately short.

Usage:
    python qa/run_tts.py                 # all languages with a sample text
    python qa/run_tts.py --lang uk,el    # only these
"""
import argparse
import json
import os
import sys
import time
from pathlib import Path

QA_DIR = Path(__file__).resolve().parent
REPO_ROOT = QA_DIR.parent
OUT_DIR = QA_DIR / "results" / "tts"

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

os.environ.setdefault("TELEGRAM_API_TOKEN", "qa-dummy-token")
os.environ.setdefault("BOT_ENV", "qa")
sys.path.insert(0, str(REPO_ROOT))


def main():
    ap = argparse.ArgumentParser(description="Smoke-test the bot's TTS voices.")
    ap.add_argument("--lang", help="comma-separated 2-letter codes (default: all sampled)")
    ap.add_argument("--numbers", action="store_true",
                    help="synthesize number-heavy samples (time/date/decimals/codes) to "
                         "listen for digits sounding off; default langs ru,uk,ka,en")
    args = ap.parse_args()

    import logging
    import app
    from azure.cognitiveservices.speech import ResultReason
    from generate_corpus import TEXTS, NUMBER_TEXTS
    for n in ("azure", "httpx", "httpcore", "urllib3"):
        logging.getLogger(n).setLevel(logging.WARNING)

    samples = NUMBER_TEXTS if args.numbers else TEXTS
    default_langs = ["ru", "uk", "ka", "en"] if args.numbers else list(TEXTS)
    langs = [c.strip() for c in args.lang.split(",")] if args.lang else default_langs
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    results = []
    print(f"Synthesizing {len(langs)} language(s)…")
    for lang in langs:
        row = {"lang": lang}
        if lang not in app.VOICE_MAP:
            row["error"] = "no voice in VOICE_MAP"
            results.append(row)
            print(f"  ✗ {lang}: no voice in VOICE_MAP")
            continue
        sample = samples.get(lang, "Hello, this is a test.")
        text = sample[0] if isinstance(sample, list) else sample  # TEXTS=list, NUMBER_TEXTS=str
        row.update({"voice": app.VOICE_MAP[lang]["voice"], "chars": len(text)})
        out = OUT_DIR / (f"num-{lang}.mp3" if args.numbers else f"{lang}.mp3")
        t0 = time.monotonic()
        try:
            res = app.synthesize_to_file(text, lang, str(out))
        except Exception as ex:
            row["error"] = f"{type(ex).__name__}: {ex}"
            results.append(row)
            print(f"  ✗ {lang}: {row['error']}")
            continue
        row["duration_ms"] = round((time.monotonic() - t0) * 1000)
        ok = res.reason == ResultReason.SynthesizingAudioCompleted
        row["ok"] = ok
        if ok:
            row["audio_bytes"] = out.stat().st_size if out.exists() else 0
            dur = getattr(res, "audio_duration", None)
            if dur is not None:
                row["audio_seconds"] = round(dur.total_seconds(), 2)
            print(f"  ✓ {lang}: {row['voice']}  {row.get('audio_seconds','?')}s  "
                  f"{row['audio_bytes']}B  ({row['duration_ms']}ms)")
        else:
            detail = getattr(getattr(res, "cancellation_details", None), "error_details", "")
            row["error"] = f"{res.reason}: {detail}"
            print(f"  ✗ {lang}: {row['error']}")
        results.append(row)

    ok_n = sum(1 for r in results if r.get("ok"))
    print(f"\nSummary: {ok_n}/{len(results)} voices OK")
    bad = [r["lang"] for r in results if not r.get("ok")]
    if bad:
        print(f"  Failed: {', '.join(bad)}")
    stamp = time.strftime("%Y%m%d-%H%M%S")
    out_json = QA_DIR / "results" / f"tts-{stamp}.json"
    out_json.write_text(json.dumps(
        {"summary": {"ok": ok_n, "total": len(results)}, "results": results},
        ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Results: {out_json.relative_to(REPO_ROOT)}  |  audio in {OUT_DIR.relative_to(REPO_ROOT)}")


if __name__ == "__main__":
    main()
