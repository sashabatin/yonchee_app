import os
import tempfile
import logging
import traceback
import time
from dotenv import load_dotenv
from telegram import Update, ReplyKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters,
    ConversationHandler
)
from azure.ai.documentintelligence import DocumentIntelligenceClient
from azure.core.credentials import AzureKeyCredential
from azure.cognitiveservices.speech import SpeechConfig, SpeechSynthesizer, AudioConfig, ResultReason

# Enable Azure SDK HTTP logging for debugging
logging.basicConfig(level=logging.INFO)
logging.getLogger("azure.core.pipeline.policies.http_logging_policy").setLevel(logging.INFO)

# Load environment variables from .env file
load_dotenv()

DOCUMENT_INTELLIGENCE_ENDPOINT = os.getenv("AZURE_FORM_RECOGNIZER_ENDPOINT")
DOCUMENT_INTELLIGENCE_KEY = os.getenv("AZURE_FORM_RECOGNIZER_KEY")
SPEECH_API_KEY = os.getenv("AZURE_SPEECH_API_KEY")
SPEECH_REGION = os.getenv("AZURE_REGION")
TELEGRAM_API_TOKEN = os.getenv("TELEGRAM_API_TOKEN")

print("Document Intelligence Endpoint:", DOCUMENT_INTELLIGENCE_ENDPOINT)
print("Document Intelligence Key (first 6, last 4):", DOCUMENT_INTELLIGENCE_KEY[:6] + "..." + DOCUMENT_INTELLIGENCE_KEY[-4:])

doc_client = DocumentIntelligenceClient(
    endpoint=DOCUMENT_INTELLIGENCE_ENDPOINT,
    credential=AzureKeyCredential(DOCUMENT_INTELLIGENCE_KEY)
)
speech_config = SpeechConfig(subscription=SPEECH_API_KEY, region=SPEECH_REGION)

LANG_OPTIONS = {
    "1": {"lang_code": "uk-UA", "voice": "uk-UA-PolinaNeural", "desc": "Ukrainian"},
    "2": {"lang_code": "ru-RU", "voice": "ru-RU-DmitryNeural", "desc": "Russian"},
    "3": {"lang_code": "en-US", "voice": "en-US-AriaNeural", "desc": "English"},
}
LANG_CHOICE = 0

async def prompt_for_file(update: Update):
    """Prompt the user to send a file."""
    await update.message.reply_text(
        "📥 Please send a photo or PDF to convert to speech."
    )

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Welcome to Yonchee T-t-S bot!\n"
        "Send me a photo or PDF, and I'll convert the text to speech.\n"
        "After sending a file, you'll be asked to choose the language."
    )
    await prompt_for_file(update)

async def ask_language(update: Update, context: ContextTypes.DEFAULT_TYPE):
    file = update.message.document or update.message.photo[-1]
    tg_file = await file.get_file()
    file_path = tempfile.mktemp()
    await tg_file.download_to_drive(file_path)
    context.user_data['file_path'] = file_path

    reply_markup = ReplyKeyboardMarkup(
        [["1", "2", "3"]],
        one_time_keyboard=True,
        resize_keyboard=True,
        input_field_placeholder="1: Ukrainian, 2: Russian, 3: English"
    )
    await update.message.reply_text(
        "Which language is the text in?\n"
        "1: Ukrainian 🇺🇦\n"
        "2: Russian 🇷🇺\n"
        "3: English 🇬🇧\n"
        "Please reply with 1, 2, or 3.",
        reply_markup=reply_markup
    )
    return LANG_CHOICE

async def process_language(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lang_choice = update.message.text.strip()
    file_path = context.user_data.get('file_path')
    audio_path = None

    if lang_choice not in LANG_OPTIONS:
        await update.message.reply_text("Invalid choice. Please reply with 1, 2, or 3.")
        return LANG_CHOICE

    lang_info = LANG_OPTIONS[lang_choice]
    lang_code = lang_info["lang_code"]
    voice = lang_info["voice"]

    try:
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
            await prompt_for_file(update)
            return ConversationHandler.END

        # SSML for language (no speed)
        ssml = f"""
<speak version="1.0" xmlns="http://www.w3.org/2001/10/synthesis" xml:lang="{lang_code}">
  <voice name="{voice}">
    {extracted_text}
  </voice>
</speak>
"""
        audio_path = f"{tempfile.mktemp()}.mp3"
        audio_config = AudioConfig(filename=audio_path)
        synthesizer_with_file = SpeechSynthesizer(speech_config=speech_config, audio_config=audio_config)
        result = synthesizer_with_file.speak_ssml_async(ssml).get()

        if result.reason != ResultReason.SynthesizingAudioCompleted:
            print("Speech synthesis error details:", result.error_details)
            await update.message.reply_text(f"Speech synthesis failed: {result.error_details}")
            await prompt_for_file(update)
            return ConversationHandler.END

        if not os.path.exists(audio_path) or os.path.getsize(audio_path) == 0:
            await update.message.reply_text("Speech synthesis failed or resulted in empty audio.")
            await prompt_for_file(update)
            return ConversationHandler.END

        # Send audio, then delete file after it's closed
        try:
            with open(audio_path, "rb") as audio_file:
                await update.message.reply_audio(audio_file)
        finally:
            # Wait a bit and retry deletion if needed (Windows may lock the file briefly)
            for _ in range(5):
                try:
                    if os.path.exists(audio_path):
                        os.remove(audio_path)
                    break
                except PermissionError:
                    time.sleep(0.2)

        # Prompt for next file after successful run
        await prompt_for_file(update)

    except Exception as e:
        await update.message.reply_text(f"Error: {str(e)}")
        print("Exception details:")
        traceback.print_exc()
        await prompt_for_file(update)
    finally:
        if file_path and os.path.exists(file_path):
            os.remove(file_path)
    return ConversationHandler.END

def main():
    app = ApplicationBuilder().token(TELEGRAM_API_TOKEN).build()
    app.add_handler(CommandHandler("start", start))

    conv_handler = ConversationHandler(
        entry_points=[MessageHandler(filters.Document.ALL | filters.PHOTO, ask_language)],
        states={
            LANG_CHOICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, process_language)],
        },
        fallbacks=[],
    )
    app.add_handler(conv_handler)
    app.run_polling()

if __name__ == "__main__":
    main()