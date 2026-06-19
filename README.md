# Yonchee Bot

Yonchee Bot is a Telegram bot designed to assist vision-impaired individuals by converting text from images or PDFs into audio format.

---

## Features

- Upload photos and PDFs (JPG, PNG, TIFF, BMP, WebP, PDF — up to 17 MB / 500 pages) for text extraction.
- OCR via **Azure Document Intelligence** (prebuilt-read).
- Choose the text language — **Ukrainian 🇺🇦 / Russian 🇷🇺 / English 🇬🇧**.
- Convert extracted text to speech via **Azure Neural TTS** and receive an audio message directly in Telegram.

---

## Architecture

- **Runtime:** Azure Container Apps, running in **webhook mode** with **scale-to-zero** (`min-replicas 0`). The container sleeps when idle and wakes on the first incoming Telegram update (~10–15 s cold start), which keeps idle cost near zero.
- **Environments:** two independent bots/containers — `yonchee-bot-dev` (deployed from the `dev` branch) and `yonchee-bot-prod` (deployed from `main`). Test on dev, then PR to `main`.
- **Observability:** both environments emit structured usage telemetry to a shared Application Insights resource, visualized in one filterable Workbook. See [docs/usage-dashboard.md](docs/usage-dashboard.md).

---

## Quick Start (Local Development)

1. **Clone the repository:**
   ```bash
   git clone <repository-url>
   cd yonchee-bot
   ```

2. **Install dependencies:**
   ```bash
   pip install -r requirements.txt
   ```

3. **Set up your `.env` file** with the required API keys and tokens:
   ```
   AZURE_FORM_RECOGNIZER_ENDPOINT=...
   AZURE_FORM_RECOGNIZER_KEY=...
   AZURE_SPEECH_API_KEY=...
   AZURE_REGION=...
   TELEGRAM_API_TOKEN=...
   ```

4. **Install ffmpeg and add it to your PATH** (see below).

5. **Run the bot:**
   ```bash
   python app.py
   ```
   With no `WEBHOOK_URL` set, the bot runs in **long-polling mode** — ideal for local development.

### Environment variables

| Variable                         | Required | Purpose                                                                 |
| -------------------------------- | -------- | ----------------------------------------------------------------------- |
| `TELEGRAM_API_TOKEN`             | ✅       | Telegram bot token (from BotFather).                                    |
| `AZURE_FORM_RECOGNIZER_ENDPOINT` | ✅       | Document Intelligence endpoint.                                         |
| `AZURE_FORM_RECOGNIZER_KEY`      | ✅       | Document Intelligence key.                                              |
| `AZURE_SPEECH_API_KEY`           | ✅       | Speech service key.                                                     |
| `AZURE_REGION`                   | ✅       | Speech service region.                                                  |
| `WEBHOOK_URL`                    | ⬜       | Public HTTPS base URL. If set, the bot runs in **webhook mode** (prod). Unset → polling. |
| `WEBHOOK_SECRET`                 | ⬜       | Optional `secret_token` Telegram must echo on each webhook call.        |
| `BOT_ENV`                        | ⬜       | `dev` / `prod` tag attached to usage telemetry (defaults to `local`).   |
| `APPINSIGHTS_INSTRUMENTATIONKEY` | ⬜       | Enables Application Insights logging + usage dashboard.                 |

---

## How to Add ffmpeg to Windows PATH

1. **Extract ffmpeg**  
   After downloading, extract the ffmpeg archive to a folder (e.g., `C:\ffmpeg`).

2. **Locate the `bin` Directory**  
   Example:  
   `C:\ffmpeg\ffmpeg-<version>-essentials_build\bin`

3. **Copy the Full Path**  
   Copy the full path to the `bin` folder.

4. **Add the Path to Windows Environment Variables**
   - Press <kbd>Win</kbd> + <kbd>S</kbd> and type `environment variables`, then select **Edit the system environment variables**.
   - In the **System Properties** window, click **Environment Variables...**.
   - In the **System variables** section, scroll down and select the `Path` variable, then click **Edit...**.
   - Click **New** and paste the path to the `bin` folder you copied earlier.
   - Click **OK** on all windows to save and close.

5. **Verify Installation**
   - Open a new Command Prompt window and run:
     ```
     ffmpeg -version
     ```
   - You should see version information for ffmpeg if everything is set up correctly.

**Note:**  
Adding ffmpeg to your PATH allows your bot to use ffmpeg from anywhere on your system.

---

## Deploying Yonchee Bot to Azure Container Apps

We use GitHub Actions for CI/CD automation.  
On every push to `dev` or `main` ([.github/workflows/main.yml](.github/workflows/main.yml)):

1. **Build and push Docker image to ACR** (`:dev` or `:prod` tag)
2. **Deploy to the matching Azure Container App** — enables external ingress, sets `min-replicas 0` (scale-to-zero), and forces a new revision via `--revision-suffix run-<run_number>`
3. **Registers the Telegram webhook** for that bot (with `secret_token`)
4. **All credentials are managed via GitHub Actions secrets** (per-environment, e.g. `TELEGRAM_API_TOKEN_DEV` / `TELEGRAM_API_TOKEN_PROD`, `WEBHOOK_SECRET_DEV` / `WEBHOOK_SECRET_PROD`)

> Note: `paths-ignore` skips builds for `**/*.md` and `docs/**`, so documentation changes never trigger a deploy.

See [docs/DEPLOY.md](docs/DEPLOY.md) for full details, including:
- Manual deployment steps
- CI/CD workflow example
- Multi-environment (dev/main) setup
- Best practices and troubleshooting

---

## Monitoring & Usage Dashboard

Both bots emit structured usage telemetry (per request: status, OCR pages, TTS characters, file type/size, processing latency) to Application Insights. A single importable Azure Workbook visualizes cost, usage, users, reliability, performance, and infrastructure metrics — filterable by `dev`/`prod`.

See [docs/usage-dashboard.md](docs/usage-dashboard.md) for the dashboard and import steps.

---

## Related Growth/Monetization Project

This bot repository is operational/technical. Product strategy, monetization, GTM, and marketing materials are maintained in a separate docs-only project:

- `C:\Users\OleksandrBatyn\OneDrive - EPAM\work\yonchee-growth\`
- Primary handoff/context file: `C:\Users\OleksandrBatyn\OneDrive - EPAM\work\yonchee-growth\HANDOFF.md`

Use that repository for business decisions and launch planning; use this repository for bot implementation and operations.

---

## Best Practices

- Use secrets for all API keys and tokens.
- Monitor your bot using the usage dashboard and set up alerts in Application Insights.
- For production, use managed identities and automate deployment with GitHub Actions.

---

## Useful Links

- [Azure Container Apps Documentation](https://learn.microsoft.com/en-us/azure/container-apps/)
- [GitHub Actions Documentation](https://docs.github.com/en/actions)
- [Azure CLI Reference](https://learn.microsoft.com/en-us/cli/azure/containerapp)
- [Azure Container Registry Documentation](https://learn.microsoft.com/en-us/azure/container-registry/)

---

## Troubleshooting

- If you have issues with ffmpeg, verify it is on your PATH.
- For Azure deployment errors, check your GitHub Actions logs and Azure Portal logs.
- For full troubleshooting and deployment details, see [docs/DEPLOY.md](docs/DEPLOY.md).

---