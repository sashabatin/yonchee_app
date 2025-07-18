import os
import tempfile
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Updater, CommandHandler, MessageHandler, Filters, CallbackContext
from azure.ai.formrecognizer import DocumentAnalysisClient
from azure.core.credentials import AzureKeyCredential
from azure.cognitiveservices.speech import SpeechConfig, SpeechSynthesizer

# Load environment variables from .env file
load_dotenv()

# Azure credentials
FORM_RECOGNIZER_ENDPOINT = os.getenv("AZURE_FORM_RECOGNIZER_ENDPOINT")
FORM_RECOGNIZER_KEY = os.getenv("AZURE_FORM_RECOGNIZER_KEY")
SPEECH_API_KEY = os.getenv("AZURE_SPEECH_API_KEY")
SPEECH_REGION = os.getenv("AZURE_REGION")  # Use the region directly
TELEGRAM_API_TOKEN = os.getenv("TELEGRAM_API_TOKEN")

# Initialize clients
form_recognizer_client = DocumentAnalysisClient(
    endpoint=FORM_RECOGNIZER_ENDPOINT,
    credential=AzureKeyCredential(FORM_RECOGNIZER_KEY)
)

speech_config = SpeechConfig(subscription=SPEECH_API_KEY, region=SPEECH_REGION)
synthesizer = SpeechSynthesizer(speech_config=speech_config)

def start(update: Update, context: CallbackContext):
    update.message.reply_text("📸 Send me a photo or PDF, and I'll convert the text to speech!")

def handle_file(update: Update, context: CallbackContext):
    # Step 1: Download user file
    file = update.message.document or update.message.photo[-1]
    file_path = file.get_file().download(custom_path=tempfile.mktemp())
    
    # Initialize audio_path to None
    audio_path = None
    
    try:
        # Step 2: Perform OCR using Document Intelligence
        with open(file_path, "rb") as document:
            poller = form_recognizer_client.begin_read(document, 0, language="en")
            result = poller.result()

            extracted_text = ""
            for page_result in result.analyze_result.read_results:
                for line in page_result.lines:
                    extracted_text += line.text + "\n"

        if not extracted_text.strip():
            update.message.reply_text("No text found in the document.")
            return

        # Step 3: Convert text to speech
        audio_path = f"{tempfile.mktemp()}.mp3"
        synthesizer.speak_text_to_file(extracted_text, audio_path)

        # Step 4: Send audio file back to user
        with open(audio_path, "rb") as audio_file:
            update.message.reply_audio(audio_file)

    except Exception as e:
        update.message.reply_text(f"Error: {str(e)}")
    
    finally:
        # Cleanup
        os.remove(file_path)
        if audio_path and os.path.exists(audio_path):
            os.remove(audio_path)

def main():
    # Bot setup
    updater = Updater(TELEGRAM_API_TOKEN)
    dp = updater.dispatcher
    
    # Handlers
    dp.add_handler(CommandHandler("start", start))
    dp.add_handler(MessageHandler(Filters.document | Filters.photo, handle_file))
    
    updater.start_polling()
    updater.idle()

if __name__ == "__main__":
    main()