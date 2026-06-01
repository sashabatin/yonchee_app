# Usage Dashboard (cost, usage, users, reliability, performance, infra)

A single Azure Workbook with a **dev/prod** filter, covering:

- **💰 Estimated cost** — OCR pages + TTS chars converted to dollars
- **📈 Usage volume** — totals, cumulative running totals, per-language breakdown
- **👥 Users** — unique users, active-users-per-day, top users
- **✅ Reliability** — success rate, failures by reason, success/fail over time
- **⏱ Performance** — processing latency p50/p95/max (excludes cold start)
- **📄 File insights** — file type + size distribution
- **🖥 Infrastructure** — replica count (scale-to-zero), CPU, memory (Azure Monitor)

## How the data gets there

On every terminal outcome (success *or* failure), the bot emits a structured log
record (`log_usage()` in [app.py](../app.py)) to Application Insights with these
custom dimensions:

| Dimension       | Meaning                                                        |
| --------------- | -------------------------------------------------------------- |
| `bot_env`       | `dev` or `prod` (from the `BOT_ENV` var)                       |
| `event_type`    | always `file_processed`                                        |
| `status`        | `success` or `failure`                                         |
| `reason`        | failure reason: `unsupported_file` / `no_text` / `synthesis_error` / `exception` |
| `language`      | Ukrainian / Russian / English                                  |
| `ocr_pages`     | pages analyzed by Document Intelligence                        |
| `tts_chars`     | characters synthesized by Speech                               |
| `file_type`     | `pdf` / `image`                                                |
| `file_size_kb`  | uploaded file size in KB                                       |
| `duration_ms`   | processing time (OCR+TTS+upload), excludes cold start          |
| `user_id`       | Telegram user id                                               |

These land in the `traces` table under `customDimensions`. Both dev and prod
send to the **same** Application Insights resource (same
`APPINSIGHTS_INSTRUMENTATIONKEY`), which is why one filterable dashboard works.

> Records written before this telemetry was added have no `status` field; the
> dashboard treats a missing `status` as success (via `status != 'failure'`) so
> historical data still counts.

## Cost estimate assumptions

The cost tiles use **public list prices** (edit the multipliers in the workbook
queries if your tier/region differs):

| Service                         | Price used        | Per unit        |
| ------------------------------- | ----------------- | --------------- |
| Document Intelligence (read)    | $1.50 / 1k pages  | `$0.0015/page`  |
| Azure Neural TTS                | $16 / 1M chars    | `$0.000016/char`|

These are **estimates**, not your actual invoice (commitment tiers, free grants,
and regional pricing all shift the real number).

## How to import the Workbook (one-time)

1. Azure Portal → your **Application Insights** resource → **Workbooks** (left menu).
2. Click **+ New**, then the **`</>` Advanced Editor** button (top toolbar).
3. Delete the placeholder JSON, paste the full contents of
   [usage-dashboard.workbook.json](usage-dashboard.workbook.json), and click **Apply**.
4. Click **Done Editing**, then **Save** (give it a name like *Yonchee Usage*).
   Save it to the same resource group so both you and CI can find it.

Once saved, use the **Environment** pill at the top to switch between `dev` and
`prod`, and the **Time range** pill to change the window. The **Infrastructure**
section has its own **Container App** selector (platform metrics are read per
resource, separate from the App Insights `bot_env` tag).

> The `dev`/`prod` values only appear in the Environment dropdown after each
> environment has processed at least one file (the dropdown is populated from
> real data). The Infrastructure charts work immediately since they read the
> Container App resource directly.

> **If a metric chart looks empty or wrong:** the infra charts use Average
> aggregation by default. Click the chart's edit pencil to switch aggregation
> (e.g. Max for replica count) if you prefer — the resource IDs are already
> wired to the North Europe `younchee-bot-rg` apps.
