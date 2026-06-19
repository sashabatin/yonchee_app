# QA toolkit

Automated black-box checks for the Yonchee bot's **OCR + content-language
detection**. Instead of manually feeding documents to the bot and eyeballing the
result, this runs a corpus of files through the bot's *real* extraction code and
scores it: which language was detected, how confident, and (where a reference
text exists) how accurate the OCR is.

It imports `extract_text` straight from [`../app.py`](../app.py), so it tests the
exact code path users hit — not a reimplementation. When the bot's OCR/detection
logic changes, re-run this to catch regressions.

It also includes a TTS voice smoke test, a synthetic-corpus generator, and a
Claude-assisted failure analysis (see below).

## Cost

Every case is a **real, billed Azure Document Intelligence call**. Keep the
corpus focused and use `--limit` / `--case` while iterating. Use `--dry-run` to
validate the manifest without spending anything.

## Setup

Needs the same Azure keys the bot uses. `app.py` loads them from the repo-root
`.env` automatically, so if the bot runs locally, this does too. A Telegram token
is **not** required (a dummy is injected — the bot is never started).

Requires `tesseract` + `poppler` on PATH only for the Georgian/Armenian fallback
cases (`pinned_lang` = `ka`/`hy`); pure Azure cases don't need them.

## Adding cases

1. Drop the source file under `corpus/images/` (e.g. `corpus/images/uk-note.jpg`).
   PDFs work too — the runner picks the file type from the extension.
2. (Optional, for CER/WER) Put the ground-truth text in
   `corpus/expected/<id>.txt`.
3. Add an entry to [`cases.json`](cases.json):

   ```json
   {
     "id": "uk-note",
     "file": "images/uk-note.jpg",
     "expected_lang": "uk",
     "expected_text_file": "expected/uk-note.txt",
     "pinned_lang": null,
     "notes": "handwritten, low contrast"
   }
   ```

   - `expected_lang` — 2-letter code the detector *should* return (drives the
     accuracy metric). Omit to skip language scoring for that case.
   - `expected_text_file` — omit if you don't have a reference text yet; the case
     still contributes to language-detection accuracy.
   - `pinned_lang` — set to `ka`/`hy` to exercise the Tesseract fallback path
     (mirrors a user who pinned that language via `/language`). Usually `null`.

`cases.json` and `corpus/expected/*.txt` are tracked in git; source images are
git-ignored by default (see [`.gitignore`](.gitignore)) since they can be large.

## Running

```bash
python qa/run_ocr.py                  # every case
python qa/run_ocr.py --limit 5        # first 5 (cost control)
python qa/run_ocr.py --case uk-note   # one case
python qa/run_ocr.py --filter photo   # only cases whose id contains "photo"
python qa/run_ocr.py --save-text      # also store extracted text (for analyze.py)
python qa/run_ocr.py --dry-run        # validate manifest, no Azure calls

python qa/report.py                   # markdown report from the latest run
python qa/report.py qa/results/run-YYYYMMDD-HHMMSS.json
```

Each run writes `qa/results/run-<timestamp>.json` (git-ignored). `report.py`
renders it to a markdown table next to the JSON.

## Generating a synthetic corpus

`generate_corpus.py` renders embedded multilingual text to images — clean,
mildly degraded, and harshly degraded (warp + heavy blur/noise/low-res, to find
where OCR breaks) — plus a few PDFs and mixed-language cases, each with exact
ground truth. Output goes under `corpus/gen/` and `cases.gen.json` (both
git-ignored, reproducible from the script); `run_ocr.py` merges them in.

```bash
python qa/generate_corpus.py                      # ~140 cases, seed 42
python qa/run_ocr.py --filter harsh --save-text   # run just the harsh variants
```

Note: clean rendered text is read near-perfectly by Azure — the **harsh** variants
are where the signal is. Still no substitute for real photos of physical pages.

## TTS voice smoke test

`run_tts.py` synthesizes a short sample per language through the bot's real
`synthesize_to_file()` and checks Azure returns completed audio — catching a
broken voice id / wrong locale / region-key problem. It does not judge quality.

```bash
python qa/run_tts.py                  # all sampled languages
python qa/run_tts.py --lang uk,el     # only these
```

## Claude-assisted analysis

`analyze.py` takes a `--save-text` results file, picks the cases that went wrong
(errors, language mismatches, high CER), pairs expected vs extracted text, and
asks Claude to cluster the failure modes by language and suggest fixes. Needs
`ANTHROPIC_API_KEY` in `.env`.

```bash
python qa/analyze.py qa/results/run-YYYYMMDD-HHMMSS.json
```

## What it measures

| Metric | Meaning |
|--------|---------|
| Language accuracy | detected `locale2` vs `expected_lang` |
| CER / WER | character / word error rate vs ground truth (normalized) |
| Confidence / coverage | the detector's own scores |
| Auto-detect would fire | would the bot skip the language menu? (thresholds in `app.py`) |
| Duration | wall-clock per case |

## Files

- `run_ocr.py` — corpus runner; imports `extract_text` from the bot.
- `run_tts.py` — TTS voice smoke test; imports `synthesize_to_file`.
- `generate_corpus.py` — synthetic multilingual corpus generator (Pillow).
- `metrics.py` — CER/WER via Levenshtein (no dependencies).
- `report.py` — results JSON → markdown.
- `analyze.py` — Claude-assisted failure analysis (needs `ANTHROPIC_API_KEY`).
- `cases.json` — hand-authored corpus manifest (+ `cases.gen.json`, generated).
- `corpus/` — `images/` + `expected/` (manual); `gen/` (generated, git-ignored).
- `results/` — generated run outputs (git-ignored).

WER is skipped for no-space scripts (zh/ja/th), where it is not meaningful; CER
covers those.
