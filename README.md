# Yonchee Bot

Yonchee Bot is a Telegram bot designed to assist vision-impaired individuals by converting text from images or PDFs into audio format.

---

## Features

- Upload photos and PDFs for text extraction.
- Convert extracted text to speech.
- Receive audio files directly in Telegram.

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
On every push to the `dev` (and later `main`) branch:

1. **Build and push Docker image to ACR**
2. **Deploy updated image to Azure Container App**
3. **All credentials are managed via GitHub Actions secrets**

Azure deployment is fully automated and production-ready.

See [docs/DEPLOY.md](docs/DEPLOY.md) for full details, including:
- Manual deployment steps
- CI/CD workflow example
- Multi-environment (dev/main) setup
- Best practices and troubleshooting

---

## Best Practices

- Use secrets for all API keys and tokens.
- Monitor your bot using Azure Log Analytics and set up alerts.
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