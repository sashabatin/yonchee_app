"""QA runner: feed the corpus through the bot's real OCR + language detection
and score the results.

This imports `extract_text` from the bot's app.py, so it exercises the *exact*
code path users hit — not a copy. Each case is run through Azure Document
Intelligence (the same call the bot makes), and we record the detected language,
confidence/coverage, and — where a ground-truth text exists — CER/WER.

Cost note: every case is a real, billed Azure OCR call. Keep the corpus focused
and use --limit while iterating.

Usage:
    python qa/run_ocr.py                  # run every case in cases.json
    python qa/run_ocr.py --limit 5        # only the first 5 cases
    python qa/run_ocr.py --case uk-note   # a single case by id
    python qa/run_ocr.py --dry-run        # validate the manifest, no OCR/cost
"""
import argparse
import json
import os
import sys
import time
from pathlib import Path

# The console output uses ✓/✗ marks; the default Windows code page (cp1252)
# can't encode them, so force UTF-8 (no-op where it's already UTF-8).
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

QA_DIR = Path(__file__).resolve().parent
REPO_ROOT = QA_DIR.parent
CASES_FILE = QA_DIR / "cases.json"
RESULTS_DIR = QA_DIR / "results"

# The bot's app.py validates env vars and builds Azure clients at import time.
# It needs a TELEGRAM_API_TOKEN to load, but the QA path never starts the bot —
# supply a dummy so import succeeds even if one isn't in the environment/.env.
# (Azure OCR keys must be real and are loaded from the repo .env by app.py.)
os.environ.setdefault("TELEGRAM_API_TOKEN", "qa-dummy-token")
os.environ.setdefault("BOT_ENV", "qa")
sys.path.insert(0, str(REPO_ROOT))


# Scripts written without word spaces: WER (word error rate) is meaningless for
# them — the reference is one "word" while OCR splits per character — so we skip
# it and rely on CER. CER stays valid everywhere.
NO_SPACE_LANGS = {"zh", "ja", "th"}


def _file_type_for(path: Path) -> str:
    """Map an extension to the file_type extract_text expects."""
    return "pdf" if path.suffix.lower() == ".pdf" else "image"


def load_cases():
    """Load hand-authored cases.json plus, if present, the generated
    cases.gen.json (from generate_corpus.py)."""
    cases = []
    if CASES_FILE.exists():
        cases += json.loads(CASES_FILE.read_text(encoding="utf-8")).get("cases", [])
    gen = QA_DIR / "cases.gen.json"
    if gen.exists():
        cases += json.loads(gen.read_text(encoding="utf-8")).get("cases", [])
    if not cases:
        sys.exit(f"No cases found. Add to {CASES_FILE.name} or run generate_corpus.py. "
                 "See qa/README.md.")
    return cases


def resolve_case(case):
    """Return (source_path, expected_text or None), resolving paths relative to
    corpus/. Raises ValueError with a clear message on a bad manifest entry."""
    rel = case.get("file")
    if not rel:
        raise ValueError(f"case {case.get('id')!r} has no 'file'")
    src = (QA_DIR / "corpus" / rel)
    if not src.exists():
        raise ValueError(f"case {case.get('id')!r}: missing file {src}")
    expected_text = None
    exp_rel = case.get("expected_text_file")
    if exp_rel:
        exp_path = QA_DIR / "corpus" / exp_rel
        if not exp_path.exists():
            raise ValueError(f"case {case.get('id')!r}: missing expected text {exp_path}")
        expected_text = exp_path.read_text(encoding="utf-8")
    return src, expected_text


def _quiet_sdk_logs():
    """app.py sets the root logger to INFO; the Azure/HTTP SDKs log every request
    at INFO, which buries the QA output. Raise their threshold to WARNING."""
    import logging
    for name in ("azure", "azure.core.pipeline.policies.http_logging_policy",
                 "httpx", "httpcore", "urllib3"):
        logging.getLogger(name).setLevel(logging.WARNING)


def run(cases, dry_run=False, save_text=False, pin_expected=False):
    import metrics  # local module
    results = []
    if not dry_run:
        import app  # bot module — triggers env validation + Azure client setup
        _quiet_sdk_logs()

    for case in cases:
        cid = case.get("id", "?")
        row = {"id": cid, "expected_lang": case.get("expected_lang")}
        try:
            src, expected_text = resolve_case(case)
        except ValueError as ex:
            row["error"] = str(ex)
            results.append(row)
            print(f"  ✗ {cid}: {ex}")
            continue

        row["file"] = case.get("file")
        row["has_ground_truth"] = expected_text is not None
        if dry_run:
            row["status"] = "validated"
            results.append(row)
            print(f"  ✓ {cid}: manifest ok")
            continue

        t0 = time.monotonic()
        # --pin-expected simulates a user who pinned their language via /language,
        # so extract_text passes it to Azure Read as a locale hint (A/B testing).
        pinned = case.get("pinned_lang")
        if pin_expected and case.get("expected_lang"):
            pinned = case["expected_lang"]
        try:
            ocr = app.extract_text(str(src), _file_type_for(src), pinned_lang=pinned)
        except Exception as ex:
            row["error"] = f"{type(ex).__name__}: {ex}"
            row["duration_ms"] = round((time.monotonic() - t0) * 1000)
            results.append(row)
            print(f"  ✗ {cid}: {row['error']}")
            continue
        row["duration_ms"] = round((time.monotonic() - t0) * 1000)

        row.update({
            "detected_lang": ocr.locale2,
            "confidence": round(ocr.confidence, 3),
            "coverage": round(ocr.coverage, 3),
            "script_lang": ocr.script_lang,
            "ocr_pages": ocr.ocr_pages,
            "used_fallback": ocr.used_fallback,
            "text_len": len(ocr.text),
            "seg_langs": [s[0] for s in ocr.segments] if ocr.segments else None,
        })
        exp_lang = case.get("expected_lang")
        row["lang_correct"] = (exp_lang == ocr.locale2) if exp_lang else None
        # Would the bot auto-proceed, or fall back to the language menu?
        row["would_auto_detect"] = bool(
            ocr.locale2 in app.VOICE_MAP
            and ocr.confidence >= app.AUTO_DETECT_MIN_CONFIDENCE
            and ocr.coverage >= app.AUTO_DETECT_MIN_COVERAGE
        )
        if expected_text is not None:
            row["cer"] = metrics.cer(expected_text, ocr.text)
            lang_hint = case.get("expected_lang") or ocr.locale2
            row["wer"] = (None if lang_hint in NO_SPACE_LANGS
                          else metrics.wer(expected_text, ocr.text))
        if save_text:
            # Stored (truncated) so analyze.py can diff expected vs actual.
            row["ocr_text"] = ocr.text[:4000]
            if expected_text is not None:
                row["expected_text"] = expected_text[:4000]

        mark = "✓" if row.get("lang_correct") in (True, None) else "✗"
        cer_s = f" cer={row['cer']:.3f}" if row.get("cer") is not None else ""
        seg_s = f" segs={'+'.join(row['seg_langs'])}" if row.get("seg_langs") else ""
        print(f"  {mark} {cid}: lang={ocr.locale2} (exp {exp_lang}) "
              f"conf={ocr.confidence:.2f} cov={ocr.coverage:.2f}{cer_s}{seg_s}")
        results.append(row)

    return results


def summarize(results):
    scored = [r for r in results if "detected_lang" in r]
    lang_judged = [r for r in scored if r.get("lang_correct") is not None]
    cer_vals = [r["cer"] for r in scored if r.get("cer") is not None]
    wer_vals = [r["wer"] for r in scored if r.get("wer") is not None]
    errors = [r for r in results if r.get("error")]
    return {
        "total_cases": len(results),
        "ran": len(scored),
        "errors": len(errors),
        "lang_judged": len(lang_judged),
        "lang_correct": sum(1 for r in lang_judged if r["lang_correct"]),
        "lang_accuracy": (sum(1 for r in lang_judged if r["lang_correct"]) / len(lang_judged)
                          if lang_judged else None),
        "would_auto_detect": sum(1 for r in scored if r.get("would_auto_detect")),
        "mean_cer": (sum(cer_vals) / len(cer_vals)) if cer_vals else None,
        "mean_wer": (sum(wer_vals) / len(wer_vals)) if wer_vals else None,
    }


def main():
    ap = argparse.ArgumentParser(description="Run the QA OCR/language corpus.")
    ap.add_argument("--limit", type=int, help="run only the first N cases")
    ap.add_argument("--case", help="run only the case with this id")
    ap.add_argument("--filter", dest="substr",
                    help="run only cases whose id contains this substring (e.g. 'photo')")
    ap.add_argument("--dry-run", action="store_true",
                    help="validate the manifest without calling Azure (no cost)")
    ap.add_argument("--save-text", action="store_true",
                    help="store extracted (and expected) text in the results, for analyze.py")
    ap.add_argument("--pin-expected", action="store_true",
                    help="pass each case's expected_lang to OCR as a locale hint (A/B test)")
    args = ap.parse_args()

    cases = load_cases()
    if args.case:
        cases = [c for c in cases if c.get("id") == args.case]
        if not cases:
            sys.exit(f"No case with id {args.case!r}")
    if args.substr:
        cases = [c for c in cases if args.substr in c.get("id", "")]
        if not cases:
            sys.exit(f"No cases whose id contains {args.substr!r}")
    if args.limit:
        cases = cases[:args.limit]
    if not cases:
        sys.exit("No cases to run. Add some to cases.json (see qa/README.md).")

    print(f"Running {len(cases)} case(s){' (dry run)' if args.dry_run else ''}…")
    results = run(cases, dry_run=args.dry_run, save_text=args.save_text,
                  pin_expected=args.pin_expected)
    summary = summarize(results)

    print("\nSummary:")
    if summary["lang_accuracy"] is not None:
        print(f"  Language accuracy: {summary['lang_correct']}/{summary['lang_judged']} "
              f"({summary['lang_accuracy']:.0%})")
    if summary["mean_cer"] is not None:
        print(f"  Mean CER: {summary['mean_cer']:.3f}  |  Mean WER: {summary['mean_wer']:.3f}")
    print(f"  Auto-detect would fire: {summary['would_auto_detect']}/{summary['ran']}")
    if summary["errors"]:
        print(f"  Errors: {summary['errors']}")

    if not args.dry_run:
        RESULTS_DIR.mkdir(exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        out = RESULTS_DIR / f"run-{stamp}.json"
        out.write_text(json.dumps(
            {"summary": summary, "results": results}, ensure_ascii=False, indent=2),
            encoding="utf-8")
        print(f"\nResults written to {out.relative_to(REPO_ROOT)}")
        print(f"Render a report with: python qa/report.py {out.relative_to(REPO_ROOT)}")


if __name__ == "__main__":
    main()
