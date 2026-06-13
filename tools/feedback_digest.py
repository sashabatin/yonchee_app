#!/usr/bin/env python
"""Generate a Claude-powered feedback digest from Table Storage and save it as a
dated Markdown file (e.g. into the growth repo's docs/feedback/ folder).

This runs locally (or on a future scheduled job) — it does NOT need the bot to be
running and does not import app.py (so it won't trip the bot's required-env checks).

Usage (PowerShell):
  $env:AZURE_STORAGE_CONNECTION_STRING = "<conn-string>"
  $env:ANTHROPIC_API_KEY = "<key>"
  python tools/feedback_digest.py --env prod --out "C:/.../yonchee-growth/docs/feedback"

The PartitionKey for feedback rows is the bot environment (dev/prod), so --env picks
which environment's feedback to analyze.
"""
import argparse
import datetime
import os
import sys

# Make the repo root importable so we can reuse the shared digest prompt.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import feedback_ai  # noqa: E402

FEEDBACK_TABLE_NAME = "feedback"


def fetch_feedback(connection_string, partition, limit=300):
    from azure.data.tables import TableServiceClient
    svc = TableServiceClient.from_connection_string(connection_string)
    client = svc.get_table_client(FEEDBACK_TABLE_NAME)
    items = list(client.query_entities(f"PartitionKey eq '{partition}'"))
    # RowKey is a reverse timestamp, so ascending sort = newest first.
    items.sort(key=lambda e: e.get("RowKey", ""))
    return items[:limit]


def main():
    ap = argparse.ArgumentParser(description="Generate a feedback digest and save it as Markdown.")
    ap.add_argument("--env", default="prod", help="Bot environment / PartitionKey (dev|prod). Default: prod")
    ap.add_argument("--out", required=True, help="Output directory (created if missing).")
    ap.add_argument("--limit", type=int, default=300, help="Max feedback items to analyze.")
    args = ap.parse_args()

    conn = os.environ.get("AZURE_STORAGE_CONNECTION_STRING")
    if not conn:
        sys.exit("ERROR: AZURE_STORAGE_CONNECTION_STRING is not set.")
    if not os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit("ERROR: ANTHROPIC_API_KEY is not set.")

    items = fetch_feedback(conn, args.env, args.limit)
    print(f"Fetched {len(items)} feedback item(s) for env='{args.env}'.")

    digest = feedback_ai.generate_digest(items)
    if digest is None:
        sys.exit("ERROR: digest generation failed (check ANTHROPIC_API_KEY / anthropic install).")

    os.makedirs(args.out, exist_ok=True)
    today = datetime.date.today().isoformat()
    path = os.path.join(args.out, f"feedback-digest-{args.env}-{today}.md")
    header = (
        f"# Разбор фидбека ({args.env}) — {today}\n\n"
        f"_Проанализировано сообщений: {len(items)}._\n\n"
        "---\n\n"
    )
    with open(path, "w", encoding="utf-8") as f:
        f.write(header + digest + "\n")
    print(f"Wrote {path}")


if __name__ == "__main__":
    main()
