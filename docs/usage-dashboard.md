# Usage Dashboard (OCR pages + TTS characters)

A single Azure Workbook that tracks the two billable cost drivers of the bot —
**OCR pages** (Document Intelligence) and **TTS characters** (Speech) — with a
dropdown to switch between **dev** and **prod**.

## How the data gets there

On every successfully processed file, the bot emits a structured log record
(`log_usage()` in [app.py](../app.py)) to Application Insights with these custom
dimensions:

| Dimension     | Meaning                                  |
| ------------- | ---------------------------------------- |
| `bot_env`     | `dev` or `prod` (from the `BOT_ENV` var) |
| `event_type`  | always `file_processed`                  |
| `language`    | Ukrainian / Russian / English            |
| `ocr_pages`   | pages analyzed by Document Intelligence  |
| `tts_chars`   | characters synthesized by Speech         |
| `user_id`     | Telegram user id                         |

These land in the `traces` table under `customDimensions`. Both dev and prod
send to the **same** Application Insights resource (same
`APPINSIGHTS_INSTRUMENTATIONKEY`), which is why one filterable dashboard works.

## How to import the Workbook (one-time)

1. Azure Portal → your **Application Insights** resource → **Workbooks** (left menu).
2. Click **+ New**, then the **`</>` Advanced Editor** button (top toolbar).
3. Delete the placeholder JSON, paste the full contents of
   [usage-dashboard.workbook.json](usage-dashboard.workbook.json), and click **Apply**.
4. Click **Done Editing**, then **Save** (give it a name like *Yonchee Usage*).
   Save it to the same resource group so both you and CI can find it.

Once saved, use the **Environment** pill at the top to switch between `dev` and
`prod`, and the **Time range** pill to change the window.

> The `dev`/`prod` values only appear in the dropdown after each environment has
> processed at least one file (the dropdown is populated from real data).
