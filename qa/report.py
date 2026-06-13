"""Render a markdown report from a qa/run_ocr.py results JSON file.

    python qa/report.py qa/results/run-20260613-101500.json
    python qa/report.py                      # uses the most recent results file

Writes a .md next to the JSON and prints the path.
"""
import json
import sys
from pathlib import Path

QA_DIR = Path(__file__).resolve().parent
RESULTS_DIR = QA_DIR / "results"


def _fmt(v, spec=""):
    if v is None:
        return "—"
    return format(v, spec)


def render(data):
    s = data["summary"]
    rows = data["results"]
    lines = ["# QA OCR report", ""]

    acc = s.get("lang_accuracy")
    lines += [
        "## Summary",
        "",
        f"- Cases run: **{s['ran']}** / {s['total_cases']}"
        + (f" ({s['errors']} errors)" if s["errors"] else ""),
        f"- Language accuracy: **{_fmt(acc, '.0%')}** "
        f"({s['lang_correct']}/{s['lang_judged']} judged)",
        f"- Mean CER: **{_fmt(s.get('mean_cer'), '.3f')}** | "
        f"Mean WER: **{_fmt(s.get('mean_wer'), '.3f')}**",
        f"- Auto-detect would fire: **{s['would_auto_detect']}** / {s['ran']}",
        "",
        "## Per-case",
        "",
        "| Case | Exp | Got | OK | Conf | Cov | Auto | CER | WER | ms |",
        "|------|-----|-----|----|------|-----|------|-----|-----|----|",
    ]
    for r in rows:
        if r.get("error"):
            lines.append(f"| {r['id']} | {_fmt(r.get('expected_lang'))} | "
                         f"ERROR: {r['error']} | | | | | | | |")
            continue
        ok = {True: "✅", False: "❌", None: "—"}[r.get("lang_correct")]
        auto = "✅" if r.get("would_auto_detect") else "—"
        lines.append(
            f"| {r['id']} | {_fmt(r.get('expected_lang'))} | {_fmt(r.get('detected_lang'))} "
            f"| {ok} | {_fmt(r.get('confidence'), '.2f')} | {_fmt(r.get('coverage'), '.2f')} "
            f"| {auto} | {_fmt(r.get('cer'), '.3f')} | {_fmt(r.get('wer'), '.3f')} "
            f"| {_fmt(r.get('duration_ms'))} |"
        )

    # Group mistakes by language so regressions are easy to spot.
    misses = [r for r in rows if r.get("lang_correct") is False]
    if misses:
        lines += ["", "## Language mismatches", ""]
        for r in misses:
            lines.append(f"- **{r['id']}**: expected `{r.get('expected_lang')}`, "
                         f"got `{r.get('detected_lang')}` "
                         f"(conf {_fmt(r.get('confidence'), '.2f')}, "
                         f"cov {_fmt(r.get('coverage'), '.2f')})")
    return "\n".join(lines) + "\n"


def main():
    if len(sys.argv) > 1:
        path = Path(sys.argv[1])
    else:
        runs = sorted(RESULTS_DIR.glob("run-*.json"))
        if not runs:
            sys.exit("No results files in qa/results/. Run qa/run_ocr.py first.")
        path = runs[-1]
    if not path.exists():
        sys.exit(f"No such file: {path}")

    data = json.loads(path.read_text(encoding="utf-8"))
    md = render(data)
    out = path.with_suffix(".md")
    out.write_text(md, encoding="utf-8")
    print(f"Report written to {out}")


if __name__ == "__main__":
    main()
