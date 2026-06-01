import asyncio
import os
import tempfile
import logging
import traceback
import re
import sys
import platform
import time
import html
from dotenv import load_dotenv

try:
    from opencensus.ext.azure.log_exporter import AzureLogHandler
except ImportError:
    AzureLogHandler = None

from telegram import Update, ReplyKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes,
    filters, ConversationHandler
)
from azure.ai.documentintelligence import DocumentIntelligenceClient
from azure.core.credentials import AzureKeyCredential
from azure.cognitiveservices.speech import (
    SpeechConfig, SpeechSynthesizer, AudioConfig, ResultReason, CancellationReason
)
# --- Env setup ---
REQUIRED_VARS = [
    "AZURE_FORM_RECOGNIZER_ENDPOINT",
    "AZURE_FORM_RECOGNIZER_KEY",
    "AZURE_SPEECH_API_KEY",
    "AZURE_REGION",
    "TELEGRAM_API_TOKEN"
]
load_dotenv()
for v in REQUIRED_VARS:
    if not os.getenv(v):
        print(f"ERROR: Missing required environment variable: {v}", file=sys.stderr)
        exit(1)

# --- Logging setup with sensitive info filter ---
class SensitiveDataFilter(logging.Filter):
    _PATTERN = re.compile(r'(https://api\.telegram\.org/bot)([0-9]+:[A-Za-z0-9_-]+)')

    def filter(self, record):
        record.msg = self._redact(record.msg)
        if record.args:
            if isinstance(record.args, dict):
                record.args = {k: self._redact(v) for k, v in record.args.items()}
            elif isinstance(record.args, tuple):
                record.args = tuple(self._redact(a) for a in record.args)
        # Pre-format to catch tokens inside non-string args (e.g. httpx.URL objects)
        try:
            formatted = record.getMessage()
            redacted = self._redact(formatted)
            if redacted != formatted:
                record.msg = redacted
                record.args = None
        except Exception:
            pass
        return True

    def _redact(self, value):
        if isinstance(value, str):
            return self._PATTERN.sub(r'\1<REDACTED>', value)
        if isinstance(value, bytes):
            return self._PATTERN.sub(r'\1<REDACTED>', value.decode('utf-8', errors='ignore')).encode()
        return value

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
_log_filter = SensitiveDataFilter()
logging.getLogger().addFilter(_log_filter)
for handler in logging.getLogger().handlers:
    handler.addFilter(_log_filter)
# httpx/httpcore log full URLs — filter those loggers directly too
for _lib in ('httpx', 'httpcore', 'telegram'):
    logging.getLogger(_lib).addFilter(_log_filter)

if AzureLogHandler:
    instrumentation_key = os.getenv("APPINSIGHTS_INSTRUMENTATIONKEY")
    if instrumentation_key:
        logger.addHandler(AzureLogHandler(connection_string=f"InstrumentationKey={instrumentation_key}"))

# --- Constants and clients ---
HELP_MESSAGE = (
    "Send me PDFs or images (JPG, PNG, TIFF, BMP, WebP, up to 17 MB and 500 pages).\n"
    "I'll extract the text and send it to you as an audio message!\n\n"
    "⏱ If the bot hasn't been used in a while, the first response may take up to 10 seconds to wake up."
)

DOCUMENT_INTELLIGENCE_ENDPOINT = os.environ["AZURE_FORM_RECOGNIZER_ENDPOINT"]
DOCUMENT_INTELLIGENCE_KEY = os.environ["AZURE_FORM_RECOGNIZER_KEY"]
SPEECH_API_KEY = os.environ["AZURE_SPEECH_API_KEY"]
SPEECH_REGION = os.environ["AZURE_REGION"]
TELEGRAM_API_TOKEN = os.environ["TELEGRAM_API_TOKEN"]
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "").rstrip("/")
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")
BOT_ENV = os.environ.get("BOT_ENV", "local")


def log_usage(user_id: int, status: str, reason: str = None, language: str = None,
              ocr_pages: int = None, tts_chars: int = None, file_type: str = None,
              file_size_kb: int = None, duration_ms: int = None) -> None:
    """Emit a structured usage record to App Insights (lands in the traces table).

    Every record carries `status` (success|failure); failures also carry `reason`.
    Optional fields are omitted when None so the dashboard only sees real values.
    """
    dims = {
        "bot_env": BOT_ENV,
        "event_type": "file_processed",
        "status": status,
        "user_id": user_id,
    }
    optional = {
        "reason": reason,
        "language": language,
        "ocr_pages": ocr_pages,
        "tts_chars": tts_chars,
        "file_type": file_type,
        "file_size_kb": file_size_kb,
        "duration_ms": duration_ms,
    }
    dims.update({k: v for k, v in optional.items() if v is not None})
    logger.info("UsageMetrics", extra={"custom_dimensions": dims})


def classify_file_type(mime_type: str) -> str:
    if mime_type == "application/pdf":
        return "pdf"
    if mime_type and mime_type.startswith("image/"):
        return "image"
    return "other"

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

SUPPORTED_MIME = {
    "image/jpeg", "image/png", "image/tiff", "image/bmp",
    "image/webp", "application/pdf"
}
SUPPORTED_EXTENSIONS = {
    ".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp", ".webp", ".pdf"
}
MAX_SIZE = 17 * 1024 * 1024

# --- Util functions ---
def normalize_ocr_text(raw_text: str) -> str:
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
    return '\n\n'.join([fix_paragraph(p) for p in text.split('\n\n')]).strip()

def escape_ssml(text: str) -> str:
    return html.escape(text)

def convert_mp3_to_ogg(mp3_path: str, ogg_path: str) -> None:
    import subprocess
    result = subprocess.run(
        ['ffmpeg', '-y', '-i', mp3_path, '-c:a', 'libopus', '-b:a', '64k', ogg_path],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )
    if result.returncode != 0:
        logger.error(f"ffmpeg error: {result.stderr.decode(errors='ignore')}")
        raise RuntimeError("Audio conversion failed.")

def is_supported_file(file) -> bool:
    mtype = getattr(file, "mime_type", None)
    file_name = getattr(file, "file_name", None)
    file_size = getattr(file, "file_size", 0)
    is_telegram_photo = hasattr(file, "file_id") and not file_name and (mtype is None or mtype == "image/jpeg")
    if is_telegram_photo:
        mtype = "image/jpeg"
    if not mtype and file_name:
        _, ext = os.path.splitext(file_name.lower())
        if ext == ".jpg" or ext == ".jpeg":
            mtype = "image/jpeg"
        elif ext == ".png":
            mtype = "image/png"
        elif ext in (".tif", ".tiff"):
            mtype = "image/tiff"
        elif ext == ".bmp":
            mtype = "image/bmp"
        elif ext == ".webp":
            mtype = "image/webp"
        elif ext == ".pdf":
            mtype = "application/pdf"
    return (mtype in SUPPORTED_MIME) and (file_size <= MAX_SIZE)

def remove_temp_file(path):
    if path and os.path.exists(path):
        try:
            os.remove(path)
        except Exception as cleanup_exc:
            logger.warning(f"Failed to remove temp file {path}: {cleanup_exc}")
            # Retry once on Windows after a short delay
            if platform.system().lower().startswith("win"):
                time.sleep(0.5)
                try:
                    os.remove(path)
                except Exception as cleanup_exc2:
                    logger.warning(f"[Retry] Failed to remove temp file {path}: {cleanup_exc2}")

async def _keep_typing(bot, chat_id: int, stop: asyncio.Event) -> None:
    while not stop.is_set():
        try:
            await bot.send_chat_action(chat_id=chat_id, action="typing")
        except Exception:
            break
        await asyncio.sleep(4)

# --- Command handlers ---
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    logger.info(f"User {user_id} requested help")
    await update.message.reply_text(HELP_MESSAGE)

async def prompt_for_file(update: Update) -> None:
    await update.message.reply_text(HELP_MESSAGE)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    lang_code = update.effective_user.language_code[:2] if update.effective_user.language_code else "Unknown"
    logger.info(f"UserStartedBot: user_id={user_id}, lang={lang_code}")
    await update.message.reply_text(
        f"👋 Welcome to Yonchee Text2Speech bot!\n{HELP_MESSAGE}"
    )

async def ask_language(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    logger.info(f"User {user_id} sent a file or photo")
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    file = update.message.document or update.message.photo[-1]
    mime_type = getattr(file, "mime_type", None)
    file_size = getattr(file, "file_size", None)
    file_type = classify_file_type(mime_type if mime_type else "image/jpeg")
    file_size_kb = round(file_size / 1024) if file_size else None
    logger.info(
        f"File attributes for user {user_id}: "
        f"type={type(file)}, "
        f"file_name={getattr(file,'file_name',None)}, "
        f"mime_type={mime_type}, "
        f"file_size={file_size}"
    )
    if not is_supported_file(file):
        await update.message.reply_text(
            "❌ Unsupported file type or file too large. Please send a PDF, JPEG, PNG, TIFF, BMP, or WebP file (max 17MB)."
        )
        log_usage(user_id, status="failure", reason="unsupported_file",
                  file_type=file_type, file_size_kb=file_size_kb)
        return ConversationHandler.END

    tg_file = await file.get_file()
    file_path = tempfile.mktemp()
    await tg_file.download_to_drive(file_path)
    context.user_data['file_path'] = file_path
    context.user_data['file_type'] = file_type
    context.user_data['file_size_kb'] = file_size_kb

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

async def process_language(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
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

    stop_typing = asyncio.Event()
    typing_task = asyncio.create_task(_keep_typing(context.bot, update.effective_chat.id, stop_typing))

    lang_info = LANG_OPTIONS[lang_choice]
    lang_code = lang_info["lang_code"]
    voice = lang_info["voice"]
    file_type = context.user_data.get('file_type')
    file_size_kb = context.user_data.get('file_size_kb')
    t0 = time.monotonic()
    ocr_pages = None

    def elapsed_ms():
        return round((time.monotonic() - t0) * 1000)

    try:
        with open(file_path, "rb") as f:
            poller = doc_client.begin_analyze_document("prebuilt-read", f)
            result = poller.result()
            ocr_pages = len(result.pages)
            extracted_text = ""
            for page in result.pages:
                for line in page.lines:
                    extracted_text += line.content + "\n"

        normalized_text = normalize_ocr_text(extracted_text)
        escaped_text = escape_ssml(normalized_text)

        if not normalized_text.strip():
            logger.info(f"User {user_id} uploaded a file with no detectable text")
            await update.message.reply_text("No text found in the document.")
            await prompt_for_file(update)
            await processing_message.delete()
            log_usage(user_id, status="failure", reason="no_text", language=lang_info["desc"],
                      ocr_pages=ocr_pages, file_type=file_type, file_size_kb=file_size_kb,
                      duration_ms=elapsed_ms())
            return ConversationHandler.END

        ssml = f"""
<speak version="1.0" xmlns="http://www.w3.org/2001/10/synthesis" xml:lang="{lang_code}">
  <voice name="{voice}">
    {escaped_text}
  </voice>
</speak>
"""
        audio_path = f"{tempfile.mktemp()}.mp3"
        audio_config = AudioConfig(filename=audio_path)
        synthesizer_with_file = SpeechSynthesizer(speech_config=speech_config, audio_config=audio_config)
        result = synthesizer_with_file.speak_ssml_async(ssml).get()
        del synthesizer_with_file

        if result.reason != ResultReason.SynthesizingAudioCompleted:
            error_message = "Speech synthesis failed."
            if result.reason == ResultReason.Canceled:
                cancellation_details = result.cancellation_details
                error_message += f" Reason: {cancellation_details.reason}."
                if cancellation_details.reason == CancellationReason.Error:
                    error_message += f" Error details: {cancellation_details.error_details}"
            logger.error(f"Speech synthesis error for user {user_id}: {error_message}")
            await update.message.reply_text("An internal error occurred during speech synthesis. Please try again later.")
            await prompt_for_file(update)
            await processing_message.delete()
            log_usage(user_id, status="failure", reason="synthesis_error", language=lang_info["desc"],
                      ocr_pages=ocr_pages, file_type=file_type, file_size_kb=file_size_kb,
                      duration_ms=elapsed_ms())
            return ConversationHandler.END

        ogg_path = f"{tempfile.mktemp()}.ogg"
        convert_mp3_to_ogg(audio_path, ogg_path)

        with open(ogg_path, "rb") as voice_file:
            await update.message.reply_voice(voice_file)
        await update.message.reply_text("Tip: Tap the 1x badge on the audio to change playback speed.")
        await update.message.reply_text(HELP_MESSAGE)
        logger.info(f"User {user_id} processed a file with language choice {lang_choice}")
        log_usage(user_id, status="success", language=lang_info["desc"], ocr_pages=ocr_pages,
                  tts_chars=len(normalized_text), file_type=file_type, file_size_kb=file_size_kb,
                  duration_ms=elapsed_ms())

    except Exception as e:
        logger.error(f"Exception for user {user_id}: {e!r}")
        logger.error(traceback.format_exc())
        await update.message.reply_text("❌ An internal error occurred while processing your request. Please try again later.")
        await prompt_for_file(update)
        log_usage(user_id, status="failure", reason="exception", language=lang_info["desc"],
                  ocr_pages=ocr_pages, file_type=file_type, file_size_kb=file_size_kb,
                  duration_ms=elapsed_ms())
    finally:
        stop_typing.set()
        typing_task.cancel()
        try:
            await typing_task
        except asyncio.CancelledError:
            pass
        try:
            await processing_message.delete()
        except Exception:
            pass
        for path in [file_path, audio_path, ogg_path]:
            remove_temp_file(path)
    return ConversationHandler.END

# --- Main entrypoint ---
def main() -> None:
    app = (
        ApplicationBuilder()
        .token(TELEGRAM_API_TOKEN)
        .read_timeout(60)
        .write_timeout(60)
        .connect_timeout(15)
        .build()
    )
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

    if WEBHOOK_URL:
        logger.info(f"Starting in webhook mode, port 8000")
        app.run_webhook(
            listen="0.0.0.0",
            port=8000,
            url_path=TELEGRAM_API_TOKEN,
            webhook_url=f"{WEBHOOK_URL}/{TELEGRAM_API_TOKEN}",
            secret_token=WEBHOOK_SECRET or None,
        )
    else:
        logger.info("Starting in polling mode")
        app.run_polling()

if __name__ == "__main__":
    main()