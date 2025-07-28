import os
import tempfile
import logging
import traceback
import re
from dotenv import load_dotenv
from telegram import Update, ReplyKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters,
    ConversationHandler
)
from azure.ai.documentintelligence import DocumentIntelligenceClient
from azure.core.credentials import AzureKeyCredential
from azure.cognitiveservices.speech import SpeechConfig, SpeechSynthesizer, AudioConfig, ResultReason, CancellationReason

# Logging setup
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

HELP_MESSAGE = (
    "Send me PDFs or images (JPG, PNG, TIFF, BMP, up to 17 MB and 500 pages).\n"
    "I'll extract the text and send it to you as an audio message!"
)

# Load environment variables
load_dotenv()
DOCUMENT_INTELLIGENCE_ENDPOINT = os.getenv("AZURE_FORM_RECOGNIZER_ENDPOINT")
DOCUMENT_INTELLIGENCE_KEY = os.getenv("AZURE_FORM_RECOGNIZER_KEY")
SPEECH_API_KEY = os.getenv("AZURE_SPEECH_API_KEY")
SPEECH_REGION = os.getenv("AZURE_REGION")
TELEGRAM_API_TOKEN = os.getenv("TELEGRAM_API_TOKEN")

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

def normalize_ocr_text(raw_text):
    # ... as before ...
    text = raw_text.replace('\r\n', '\n').replace('\r', '\n')
    text = re.sub(r'-\s*\n\s*', '', text)
    text = re.sub(r'(?<!\n)\n(?!\n)', ' ', text)
    text = re.sub(r'\n{3,}', '\n\n', text)
    text = re.sub(r' +', ' ', text)
    def fix_paragraph(p):
        p = p.strip()
        if p and p[-1] not in '.!?…:;':
            return p + '.'
        return p
    paragraphs = [fix_paragraph(p) for p in text.split('\n\n')]
    text = '\n\n'.join(paragraphs)
    return text.strip()

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    logger.info(f"User {user_id} requested help")
    await update.message.reply_text(HELP_MESSAGE)

async def prompt_for_file(update: Update):
    await update.message.reply_text(HELP_MESSAGE)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    logger.info(f"User {user_id} started the bot")
    await update.message.reply_text(
        f"👋 Welcome to Yonchee Text2Speech bot!\n{HELP_MESSAGE}"
    )

async def ask_language(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    logger.info(f"User {user_id} sent a file or photo")
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

def convert_mp3_to_ogg(mp3_path, ogg_path):
    import subprocess
    result = subprocess.run(
        ['ffmpeg', '-y', '-i', mp3_path, '-c:a', 'libopus', '-b:a', '64k', ogg_path],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )
    if result.returncode != 0:
        logger.error(f"ffmpeg error: {result.stderr.decode()}")
        raise RuntimeError("Audio conversion failed.")

async def process_language(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    lang_choice = update.message.text.strip()
    file_path = context.user_data.get('file_path')
    audio_path = None
    ogg_path = None

    if lang_choice not in LANG_OPTIONS:
        logger.info(f"User {user_id} made invalid language choice: {lang_choice}")
        await update.message.reply_text("Invalid choice. Please reply with 1, 2, or 3.")
        return LANG_CHOICE

    processing_message = await update.message.reply_text(
        "⏳ Processing your file and generating audio, please wait..."
    )

    lang_info = LANG_OPTIONS[lang_choice]
    lang_code = lang_info["lang_code"]
    voice = lang_info["voice"]

    try:
        with open(file_path, "rb") as f:
            poller = doc_client.begin_analyze_document("prebuilt-read", f)
            result = poller.result()
            extracted_text = ""
            for page in result.pages:
                for line in page.lines:
                    extracted_text += line.content + "\n"

        normalized_text = normalize_ocr_text(extracted_text)

        if not normalized_text.strip():
            logger.info(f"User {user_id} uploaded a file with no detectable text")
            await update.message.reply_text("No text found in the document.")
            await prompt_for_file(update)
            await processing_message.delete()
            return ConversationHandler.END

        ssml = f"""
<speak version="1.0" xmlns="http://www.w3.org/2001/10/synthesis" xml:lang="{lang_code}">
  <voice name="{voice}">
    {normalized_text}
  </voice>
</speak>
"""
        audio_path = f"{tempfile.mktemp()}.mp3"
        audio_config = AudioConfig(filename=audio_path)
        synthesizer_with_file = SpeechSynthesizer(speech_config=speech_config, audio_config=audio_config)
        result = synthesizer_with_file.speak_ssml_async(ssml).get()

        if result.reason != ResultReason.SynthesizingAudioCompleted:
            error_message = "Speech synthesis failed."
            if result.reason == ResultReason.Canceled:
                cancellation_details = result.cancellation_details
                error_message += f" Reason: {cancellation_details.reason}."
                if cancellation_details.reason == CancellationReason.Error:
                    error_message += f" Error details: {cancellation_details.error_details}"
            logger.error(f"Speech synthesis error for user {user_id}: {error_message}")
            await update.message.reply_text(error_message)
            await prompt_for_file(update)
            await processing_message.delete()
            return ConversationHandler.END

        ogg_path = f"{tempfile.mktemp()}.ogg"
        convert_mp3_to_ogg(audio_path, ogg_path)

        with open(ogg_path, "rb") as voice_file:
            await update.message.reply_voice(voice_file)
        await update.message.reply_text("Tip: Tap the 1x badge on the audio to change playback speed.")

        # Show the instructions again after audio is sent
        await update.message.reply_text(HELP_MESSAGE)

        logger.info(f"User {user_id} processed a file with language choice {lang_choice}")

    except Exception as e:
        logger.error(f"Exception for user {user_id}: {str(e)}")
        logger.error(traceback.format_exc())
        await update.message.reply_text(f"Error: {str(e)}")
        await prompt_for_file(update)
    finally:
        try:
            await processing_message.delete()
        except Exception:
            pass
        for path in [file_path, audio_path, ogg_path]:
            if path and os.path.exists(path):
                try:
                    os.remove(path)
                except Exception:
                    pass
    return ConversationHandler.END

def main():
    app = ApplicationBuilder().token(TELEGRAM_API_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))

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