"""Claude-assisted analysis of a QA OCR run.

Takes a results JSON produced by `run_ocr.py --save-text`, selects the cases that
actually went wrong (extraction errors, language mismatches, or high CER), pairs
the expected text with what the bot extracted, and asks Claude to cluster the
failure modes by language and suggest probable causes and concrete fixes.

Mirrors feedback_ai.py: the anthropic SDK is imported lazily and a missing key /
SDK degrades gracefully. Needs ANTHROPIC_API_KEY (in .env or the environment).

Usage:
    python qa/analyze.py                       # newest run-*.json
    python qa/analyze.py qa/results/run-XXatime.json
    python qa/analyze.py --cer-threshold 0.03 --max-cases 30
"""
import argparse
import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

QA_DIR = Path(__file__).resolve().parent
REPO_ROOT = QA_DIR.parent
RESULTS_DIR = QA_DIR / "results"
MODEL = os.environ.get("FEEDBACK_DIGEST_MODEL", "claude-sonnet-4-6")

SYSTEM_PROMPT = (
    "Ты — инженер по качеству OCR-конвейера Telegram-бота, который превращает фото/PDF "
    "с текстом в озвучку (OCR через Azure Document Intelligence, затем TTS). Тебе дают "
    "проблемные тест-кейсы: ожидаемый текст (эталон) и текст, который реально извлёк OCR, "
    "плюс определённый язык и метрики (CER/WER). Твоя задача: сгруппировать ошибки по "
    "типам и языкам (напр.: перепутаны цифры, потеряны диакритики, ё→е, обрезаны края "
    "кадра, неверно определён язык, пустой результат), оценить вероятную причину каждого "
    "класса и предложить конкретные действия для инженера (что проверить/поправить в коде "
    "бота или в пайплайне). Отделяй системные проблемы от артефактов синтетической "
    "деградации (обрезка краёв из-за warp — это особенность теста, а не бага бота). "
    "Если сигнала мало — скажи честно. Отвечай по-русски, GitHub-Markdown, кратко и по делу."
)


def select_cases(results, cer_threshold, max_cases):
    """Pick the cases worth analyzing: errors, language mismatches, high CER."""
    def severity(r):
        if r.get("error"):
            return 3.0
        if r.get("lang_correct") is False:
            return 2.0 + (r.get("cer") or 0)
        return r.get("cer") or 0
    bad = [r for r in results
           if r.get("error")
           or r.get("lang_correct") is False
           or (r.get("cer") is not None and r["cer"] >= cer_threshold)]
    bad.sort(key=severity, reverse=True)
    return bad[:max_cases]


def format_cases(cases):
    blocks = []
    for r in cases:
        head = (f"### {r['id']}  (язык: ожид={r.get('expected_lang')} / "
                f"OCR={r.get('detected_lang')}, CER={r.get('cer')}, "
                f"WER={r.get('wer')}, conf={r.get('confidence')}, cov={r.get('coverage')})")
        if r.get("error"):
            blocks.append(head + f"\nОШИБКА: {r['error']}")
            continue
        exp = (r.get("expected_text") or "")[:400]
        got = (r.get("ocr_text") or "")[:400]
        blocks.append(head + f"\nНОТКИ: {r.get('notes','')}\n"
                      f"ЭТАЛОН: {exp!r}\nOCR:    {got!r}")
    return "\n\n".join(blocks)


def analyze(data, cer_threshold, max_cases):
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        return None, "ANTHROPIC_API_KEY не задан — добавь его в .env, чтобы включить Claude-анализ."
    cases = select_cases(data.get("results", []), cer_threshold, max_cases)
    if not cases:
        return None, "Проблемных кейсов не найдено (нет ошибок, расхождений языка и CER выше порога)."
    try:
        from anthropic import Anthropic
    except ImportError:
        return None, "Пакет anthropic не установлен."

    s = data.get("summary", {})
    user_content = (
        f"Сводка прогона: точность языка {s.get('lang_correct')}/{s.get('lang_judged')}, "
        f"средний CER {s.get('mean_cer')}, ошибок {s.get('errors')}.\n\n"
        f"Ниже {len(cases)} худших кейсов (эталон vs то, что извлёк OCR):\n\n"
        f"{format_cases(cases)}\n\n"
        "Сгруппируй ошибки по типам и языкам, оцени вероятные причины и дай "
        "приоритизированный список конкретных действий. В конце — блок «Топ-3, что делать»."
    )
    try:
        client = Anthropic(api_key=api_key)
        msg = client.messages.create(
            model=MODEL, max_tokens=4000, system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_content}],
        )
        text = "".join(b.text for b in msg.content if getattr(b, "type", None) == "text").strip()
        return text, None
    except Exception as ex:
        return None, f"Сбой вызова Claude: {ex!r}"


def main():
    ap = argparse.ArgumentParser(description="Claude analysis of a QA OCR run.")
    ap.add_argument("results", nargs="?", help="results JSON (default: newest run-*.json)")
    ap.add_argument("--cer-threshold", type=float, default=0.05)
    ap.add_argument("--max-cases", type=int, default=25)
    args = ap.parse_args()

    load_dotenv(REPO_ROOT / ".env")
    if args.results:
        path = Path(args.results)
    else:
        runs = sorted(RESULTS_DIR.glob("run-*.json"))
        if not runs:
            sys.exit("No run-*.json in qa/results/. Run run_ocr.py --save-text first.")
        path = runs[-1]
    data = json.loads(path.read_text(encoding="utf-8"))
    if not any(r.get("ocr_text") for r in data.get("results", [])):
        print("WARNING: this run has no saved text — re-run with --save-text for diffs.\n")

    digest, err = analyze(data, args.cer_threshold, args.max_cases)
    if digest is None:
        sys.exit(f"Analysis unavailable: {err}")
    out = path.with_name(path.stem + "-analysis.md")
    out.write_text(digest, encoding="utf-8")
    print(digest)
    print(f"\n---\nSaved to {out.relative_to(REPO_ROOT)}")


if __name__ == "__main__":
    main()
