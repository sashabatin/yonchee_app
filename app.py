import os
import tempfile
import logging
import traceback
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters
from azure.ai.documentintelligence import DocumentIntelligenceClient
from azure.core.credentials import AzureKeyCredential
from azure.cognitiveservices.speech import SpeechConfig, SpeechSynthesizer, AudioConfig  # <-- Add AudioConfig

# Enable Azure SDK HTTP logging
logging.basicConfig(level=logging.DEBUG)
logging.getLogger("azure.core.pipeline.policies.http_logging_policy").setLevel(logging.DEBUG)

# Load environment variables from .env file
load_dotenv()

# Azure and Telegram credentials
DOCUMENT_INTELLIGENCE_ENDPOINT = os.getenv("AZURE_FORM_RECOGNIZER_ENDPOINT")
DOCUMENT_INTELLIGENCE_KEY = os.getenv("AZURE_FORM_RECOGNIZER_KEY")
SPEECH_API_KEY = os.getenv("AZURE_SPEECH_API_KEY")
SPEECH_REGION = os.getenv("AZURE_REGION")
TELEGRAM_API_TOKEN = os.getenv("TELEGRAM_API_TOKEN")

# Print endpoint and partial key for debugging
print("Document Intelligence Endpoint:", DOCUMENT_INTELLIGENCE_ENDPOINT)
print("Document Intelligence Key (first 6, last 4):", DOCUMENT_INTELLIGENCE_KEY[:6] + "..." + DOCUMENT_INTELLIGENCE_KEY[-4:])

# Initialize Azure clients
doc_client = DocumentIntelligenceClient(
    endpoint=DOCUMENT_INTELLIGENCE_ENDPOINT,
    credential=AzureKeyCredential(DOCUMENT_INTELLIGENCE_KEY)
)
speech_config = SpeechConfig(subscription=SPEECH_API_KEY, region=SPEECH_REGION)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📸 Send me a photo or PDF, and I'll convert the text to speech!"
    )

async def handle_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    file = update.message.document or update.message.photo[-1]
    tg_file = await file.get_file()
    file_path = tempfile.mktemp()
    audio_path = None

    try:
        await tg_file.download_to_drive(file_path)
        # OCR with Azure Document Intelligence
        with open(file_path, "rb") as f:
            poller = doc_client.begin_analyze_document("prebuilt-read", f)
            result = poller.result()
            extracted_text = ""
            for page in result.pages:
                for line in page.lines:
                    extracted_text += line.content + "\n"

        if not extracted_text.strip():
            await update.message.reply_text("No text found in the document.")
            return

        # Text-to-speech (CORRECTED)
        audio_path = f"{tempfile.mktemp()}.mp3"
        audio_config = AudioConfig(filename=audio_path)
        synthesizer_with_file = SpeechSynthesizer(speech_config=speech_config, audio_config=audio_config)
        result = synthesizer_with_file.speak_text_async(extracted_text).get()

        with open(audio_path, "rb") as audio_file:
            await update.message.reply_audio(audio_file)

    except Exception as e:
        await update.message.reply_text(f"Error: {str(e)}")
        print("Exception details:")
        traceback.print_exc()
    finally:
        if os.path.exists(file_path):
            os.remove(file_path)
        if audio_path and os.path.exists(audio_path):
            os.remove(audio_path)

def main():
    app = ApplicationBuilder().token(TELEGRAM_API_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.Document.ALL | filters.PHOTO, handle_file))
    app.run_polling()

if __name__ == "__main__":
    main()