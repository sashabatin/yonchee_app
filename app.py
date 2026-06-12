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
from collections import defaultdict
from dotenv import load_dotenv

try:
    from opencensus.ext.azure.log_exporter import AzureLogHandler
except ImportError:
    AzureLogHandler = None

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler, CallbackQueryHandler,
    ContextTypes, filters
)
from azure.ai.documentintelligence import DocumentIntelligenceClient
from azure.ai.documentintelligence.models import DocumentAnalysisFeature
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
# UI interface messages, keyed by 2-letter language code (from the Telegram client's
# language_code). English ("en") is the fallback for any missing language or key.
# NOTE: this is the *interface* language (auto-detected). The *content* language
# (for OCR/TTS) is still chosen by the user via the 1/2/3 menu below.
MESSAGES = {
    "en": {
        "welcome": "👋 Welcome to Yonchee Text2Speech bot!",
        "help": (
            "Send me PDFs or images (JPG, PNG, TIFF, BMP, WebP, up to 17 MB and 500 pages).\n"
            "I'll extract the text and send it to you as an audio message!\n\n"
            "⏱ If the bot hasn't been used in a while, the first response may take up to 10 seconds to wake up."
        ),
        "unsupported_file": "❌ Unsupported file type or file too large. Please send a PDF, JPEG, PNG, TIFF, BMP, or WebP file (max 17MB).",
        "ask_language": (
            "Which language is the text in?\n"
            "1: Ukrainian 🇺🇦\n"
            "2: Russian 🇷🇺\n"
            "3: English 🇬🇧\n"
            "Please reply with 1, 2, or 3."
        ),
        "lang_placeholder": "1: Ukrainian, 2: Russian, 3: English",
        "invalid_choice": "Invalid choice. Please reply with 1, 2, or 3.",
        "processing": "⏳ Processing your file and generating audio, please wait...",
        "no_text": "No text found in the document.",
        "synthesis_error": "An internal error occurred during speech synthesis. Please try again later.",
        "playback_tip": "Tip: Tap the 1x badge on the audio to change playback speed.",
        "generic_error": "❌ An internal error occurred while processing your request. Please try again later.",
        "analyzing": "🔎 Reading the document…",
        "detected_lang": "🔎 Detected language: {lang}.",
        "generating_audio": "⏳ Generating audio…",
        "choose_language": "🔊 Which language is the text in? Choose below:",
        "detected_choice": "✅ {lang} — detected",
        "using_default": "🗣 Using your saved language: {lang}.",
        "language_menu": "🌐 Choose a default language for audio. I'll always use it. Pick “Auto-detect” to go back to automatic.",
        "auto_button": "🔄 Auto-detect",
        "default_set": "✅ Default language set: {lang}.",
        "default_auto": "✅ Auto-detect enabled — I'll detect each document's language.",
    },
    "ru": {
        "welcome": "👋 Добро пожаловать в бот Yonchee Text2Speech!",
        "help": (
            "Отправьте мне PDF или изображения (JPG, PNG, TIFF, BMP, WebP, до 17 МБ и 500 страниц).\n"
            "Я распознаю текст и пришлю его вам голосовым сообщением!\n\n"
            "⏱ Если ботом давно не пользовались, первый ответ может занять до 10 секунд — нужно «проснуться»."
        ),
        "unsupported_file": "❌ Неподдерживаемый тип файла или слишком большой файл. Пришлите PDF, JPEG, PNG, TIFF, BMP или WebP (до 17 МБ).",
        "ask_language": (
            "На каком языке текст?\n"
            "1: Украинский 🇺🇦\n"
            "2: Русский 🇷🇺\n"
            "3: Английский 🇬🇧\n"
            "Ответьте 1, 2 или 3."
        ),
        "lang_placeholder": "1: Украинский, 2: Русский, 3: Английский",
        "invalid_choice": "Неверный выбор. Ответьте 1, 2 или 3.",
        "processing": "⏳ Обрабатываю файл и генерирую аудио, подождите...",
        "no_text": "В документе не найден текст.",
        "synthesis_error": "Произошла внутренняя ошибка при синтезе речи. Попробуйте позже.",
        "playback_tip": "Подсказка: нажмите на значок 1x у аудио, чтобы изменить скорость воспроизведения.",
        "generic_error": "❌ Произошла внутренняя ошибка при обработке запроса. Попробуйте позже.",
        "analyzing": "🔎 Читаю документ…",
        "detected_lang": "🔎 Определил язык: {lang}.",
        "generating_audio": "⏳ Генерирую аудио…",
        "choose_language": "🔊 На каком языке текст? Выберите ниже:",
        "detected_choice": "✅ {lang} — определён",
        "using_default": "🗣 Использую сохранённый язык: {lang}.",
        "language_menu": "🌐 Выберите язык озвучки по умолчанию. Я всегда буду его использовать. «Авто-определение» — вернуться к автоматическому выбору.",
        "auto_button": "🔄 Авто-определение",
        "default_set": "✅ Язык по умолчанию установлен: {lang}.",
        "default_auto": "✅ Включено авто-определение — буду определять язык каждого документа.",
    },
    "uk": {
        "welcome": "👋 Ласкаво просимо до бота Yonchee Text2Speech!",
        "help": (
            "Надішліть мені PDF або зображення (JPG, PNG, TIFF, BMP, WebP, до 17 МБ і 500 сторінок).\n"
            "Я розпізнаю текст і надішлю його вам голосовим повідомленням!\n\n"
            "⏱ Якщо ботом давно не користувалися, перша відповідь може зайняти до 10 секунд — потрібно «прокинутися»."
        ),
        "unsupported_file": "❌ Непідтримуваний тип файлу або завеликий файл. Надішліть PDF, JPEG, PNG, TIFF, BMP або WebP (до 17 МБ).",
        "ask_language": (
            "Якою мовою текст?\n"
            "1: Українська 🇺🇦\n"
            "2: Російська 🇷🇺\n"
            "3: Англійська 🇬🇧\n"
            "Надішліть 1, 2 або 3."
        ),
        "lang_placeholder": "1: Українська, 2: Російська, 3: Англійська",
        "invalid_choice": "Неправильний вибір. Надішліть 1, 2 або 3.",
        "processing": "⏳ Обробляю файл і генерую аудіо, зачекайте...",
        "no_text": "У документі не знайдено тексту.",
        "synthesis_error": "Сталася внутрішня помилка під час синтезу мовлення. Спробуйте пізніше.",
        "playback_tip": "Підказка: натисніть на позначку 1x на аудіо, щоб змінити швидкість відтворення.",
        "generic_error": "❌ Сталася внутрішня помилка під час обробки запиту. Спробуйте пізніше.",
        "analyzing": "🔎 Читаю документ…",
        "detected_lang": "🔎 Визначив мову: {lang}.",
        "generating_audio": "⏳ Генерую аудіо…",
        "choose_language": "🔊 Якою мовою текст? Оберіть нижче:",
        "detected_choice": "✅ {lang} — визначено",
        "using_default": "🗣 Використовую збережену мову: {lang}.",
        "language_menu": "🌐 Оберіть мову озвучення за замовчуванням. Я завжди її використовуватиму. «Авто-визначення» — повернутися до автоматичного вибору.",
        "auto_button": "🔄 Авто-визначення",
        "default_set": "✅ Мову за замовчуванням встановлено: {lang}.",
        "default_auto": "✅ Увімкнено авто-визначення — визначатиму мову кожного документа.",
    },
    "es": {
        "welcome": "👋 ¡Bienvenido al bot Yonchee Text2Speech!",
        "help": (
            "Envíame archivos PDF o imágenes (JPG, PNG, TIFF, BMP, WebP, hasta 17 MB y 500 páginas).\n"
            "¡Extraeré el texto y te lo enviaré como mensaje de voz!\n\n"
            "⏱ Si el bot no se ha usado en un tiempo, la primera respuesta puede tardar hasta 10 segundos en activarse."
        ),
        "unsupported_file": "❌ Tipo de archivo no compatible o archivo demasiado grande. Envía un PDF, JPEG, PNG, TIFF, BMP o WebP (máx. 17 MB).",
        "ask_language": (
            "¿En qué idioma está el texto?\n"
            "1: Ucraniano 🇺🇦\n"
            "2: Ruso 🇷🇺\n"
            "3: Inglés 🇬🇧\n"
            "Responde con 1, 2 o 3."
        ),
        "lang_placeholder": "1: Ucraniano, 2: Ruso, 3: Inglés",
        "invalid_choice": "Opción no válida. Responde con 1, 2 o 3.",
        "processing": "⏳ Procesando tu archivo y generando el audio, espera por favor...",
        "no_text": "No se encontró texto en el documento.",
        "synthesis_error": "Ocurrió un error interno durante la síntesis de voz. Inténtalo de nuevo más tarde.",
        "playback_tip": "Consejo: toca la insignia 1x del audio para cambiar la velocidad de reproducción.",
        "generic_error": "❌ Ocurrió un error interno al procesar tu solicitud. Inténtalo de nuevo más tarde.",
        "analyzing": "🔎 Leyendo el documento…",
        "detected_lang": "🔎 Idioma detectado: {lang}.",
        "generating_audio": "⏳ Generando el audio…",
        "choose_language": "🔊 ¿En qué idioma está el texto? Elige abajo:",
        "detected_choice": "✅ {lang} — detectado",
        "using_default": "🗣 Usando tu idioma guardado: {lang}.",
        "language_menu": "🌐 Elige un idioma predeterminado para el audio. Lo usaré siempre. Elige «Detección automática» para volver al modo automático.",
        "auto_button": "🔄 Detección automática",
        "default_set": "✅ Idioma predeterminado establecido: {lang}.",
        "default_auto": "✅ Detección automática activada: detectaré el idioma de cada documento.",
    },
    "de": {
        "welcome": "👋 Willkommen beim Yonchee Text2Speech-Bot!",
        "help": (
            "Schick mir PDFs oder Bilder (JPG, PNG, TIFF, BMP, WebP, bis zu 17 MB und 500 Seiten).\n"
            "Ich extrahiere den Text und sende ihn dir als Sprachnachricht!\n\n"
            "⏱ Wenn der Bot eine Weile nicht genutzt wurde, kann die erste Antwort bis zu 10 Sekunden dauern (Aufwachen)."
        ),
        "unsupported_file": "❌ Nicht unterstützter Dateityp oder Datei zu groß. Bitte sende eine PDF-, JPEG-, PNG-, TIFF-, BMP- oder WebP-Datei (max. 17 MB).",
        "ask_language": (
            "In welcher Sprache ist der Text?\n"
            "1: Ukrainisch 🇺🇦\n"
            "2: Russisch 🇷🇺\n"
            "3: Englisch 🇬🇧\n"
            "Bitte antworte mit 1, 2 oder 3."
        ),
        "lang_placeholder": "1: Ukrainisch, 2: Russisch, 3: Englisch",
        "invalid_choice": "Ungültige Auswahl. Bitte antworte mit 1, 2 oder 3.",
        "processing": "⏳ Deine Datei wird verarbeitet und das Audio erstellt, bitte warten...",
        "no_text": "Im Dokument wurde kein Text gefunden.",
        "synthesis_error": "Bei der Sprachsynthese ist ein interner Fehler aufgetreten. Bitte versuche es später erneut.",
        "playback_tip": "Tipp: Tippe auf das 1x-Symbol am Audio, um die Wiedergabegeschwindigkeit zu ändern.",
        "generic_error": "❌ Bei der Verarbeitung deiner Anfrage ist ein interner Fehler aufgetreten. Bitte versuche es später erneut.",
        "analyzing": "🔎 Dokument wird gelesen…",
        "detected_lang": "🔎 Erkannte Sprache: {lang}.",
        "generating_audio": "⏳ Audio wird erstellt…",
        "choose_language": "🔊 In welcher Sprache ist der Text? Bitte unten wählen:",
        "detected_choice": "✅ {lang} — erkannt",
        "using_default": "🗣 Ich verwende deine gespeicherte Sprache: {lang}.",
        "language_menu": "🌐 Wähle eine Standardsprache für die Audioausgabe. Ich verwende sie immer. Wähle „Automatisch erkennen“, um zur Automatik zurückzukehren.",
        "auto_button": "🔄 Automatisch erkennen",
        "default_set": "✅ Standardsprache festgelegt: {lang}.",
        "default_auto": "✅ Automatische Erkennung aktiviert – ich erkenne die Sprache jedes Dokuments.",
    },
    "fr": {
        "welcome": "👋 Bienvenue sur le bot Yonchee Text2Speech !",
        "help": (
            "Envoie-moi des PDF ou des images (JPG, PNG, TIFF, BMP, WebP, jusqu'à 17 Mo et 500 pages).\n"
            "J'extrais le texte et je te l'envoie sous forme de message vocal !\n\n"
            "⏱ Si le bot n'a pas été utilisé depuis un moment, la première réponse peut prendre jusqu'à 10 secondes (réveil)."
        ),
        "unsupported_file": "❌ Type de fichier non pris en charge ou fichier trop volumineux. Envoie un fichier PDF, JPEG, PNG, TIFF, BMP ou WebP (max. 17 Mo).",
        "ask_language": (
            "Dans quelle langue est le texte ?\n"
            "1 : Ukrainien 🇺🇦\n"
            "2 : Russe 🇷🇺\n"
            "3 : Anglais 🇬🇧\n"
            "Réponds par 1, 2 ou 3."
        ),
        "lang_placeholder": "1 : Ukrainien, 2 : Russe, 3 : Anglais",
        "invalid_choice": "Choix invalide. Réponds par 1, 2 ou 3.",
        "processing": "⏳ Traitement de ton fichier et génération de l'audio, patiente...",
        "no_text": "Aucun texte trouvé dans le document.",
        "synthesis_error": "Une erreur interne s'est produite lors de la synthèse vocale. Réessaie plus tard.",
        "playback_tip": "Astuce : appuie sur le badge 1x de l'audio pour changer la vitesse de lecture.",
        "generic_error": "❌ Une erreur interne s'est produite lors du traitement de ta demande. Réessaie plus tard.",
        "analyzing": "🔎 Lecture du document…",
        "detected_lang": "🔎 Langue détectée : {lang}.",
        "generating_audio": "⏳ Génération de l'audio…",
        "choose_language": "🔊 Dans quelle langue est le texte ? Choisis ci-dessous :",
        "detected_choice": "✅ {lang} — détectée",
        "using_default": "🗣 J'utilise ta langue enregistrée : {lang}.",
        "language_menu": "🌐 Choisis une langue par défaut pour l'audio. Je l'utiliserai toujours. Choisis « Détection automatique » pour revenir au mode automatique.",
        "auto_button": "🔄 Détection automatique",
        "default_set": "✅ Langue par défaut définie : {lang}.",
        "default_auto": "✅ Détection automatique activée — je détecterai la langue de chaque document.",
    },
    "pl": {
        "welcome": "👋 Witaj w bocie Yonchee Text2Speech!",
        "help": (
            "Wyślij mi pliki PDF lub zdjęcia (JPG, PNG, TIFF, BMP, WebP, do 17 MB i 500 stron).\n"
            "Wyodrębnię tekst i odeślę go jako wiadomość głosową!\n\n"
            "⏱ Jeśli bot nie był używany przez jakiś czas, pierwsza odpowiedź może zająć do 10 sekund (wybudzanie)."
        ),
        "unsupported_file": "❌ Nieobsługiwany typ pliku lub plik zbyt duży. Wyślij plik PDF, JPEG, PNG, TIFF, BMP lub WebP (maks. 17 MB).",
        "ask_language": (
            "W jakim języku jest tekst?\n"
            "1: Ukraiński 🇺🇦\n"
            "2: Rosyjski 🇷🇺\n"
            "3: Angielski 🇬🇧\n"
            "Odpowiedz 1, 2 lub 3."
        ),
        "lang_placeholder": "1: Ukraiński, 2: Rosyjski, 3: Angielski",
        "invalid_choice": "Nieprawidłowy wybór. Odpowiedz 1, 2 lub 3.",
        "processing": "⏳ Przetwarzam plik i generuję audio, poczekaj...",
        "no_text": "Nie znaleziono tekstu w dokumencie.",
        "synthesis_error": "Wystąpił wewnętrzny błąd podczas syntezy mowy. Spróbuj ponownie później.",
        "playback_tip": "Wskazówka: dotknij oznaczenia 1x przy audio, aby zmienić prędkość odtwarzania.",
        "generic_error": "❌ Wystąpił wewnętrzny błąd podczas przetwarzania żądania. Spróbuj ponownie później.",
        "analyzing": "🔎 Odczytuję dokument…",
        "detected_lang": "🔎 Wykryty język: {lang}.",
        "generating_audio": "⏳ Generuję audio…",
        "choose_language": "🔊 W jakim języku jest tekst? Wybierz poniżej:",
        "detected_choice": "✅ {lang} — wykryto",
        "using_default": "🗣 Używam zapisanego języka: {lang}.",
        "language_menu": "🌐 Wybierz domyślny język audio. Zawsze będę go używać. Wybierz „Wykrywanie automatyczne”, aby wrócić do trybu automatycznego.",
        "auto_button": "🔄 Wykrywanie automatyczne",
        "default_set": "✅ Ustawiono domyślny język: {lang}.",
        "default_auto": "✅ Włączono automatyczne wykrywanie — wykryję język każdego dokumentu.",
    },
    "pt": {
        "welcome": "👋 Bem-vindo ao bot Yonchee Text2Speech!",
        "help": (
            "Envie-me PDFs ou imagens (JPG, PNG, TIFF, BMP, WebP, até 17 MB e 500 páginas).\n"
            "Vou extrair o texto e enviá-lo a você como mensagem de voz!\n\n"
            "⏱ Se o bot não for usado há algum tempo, a primeira resposta pode levar até 10 segundos para acordar."
        ),
        "unsupported_file": "❌ Tipo de arquivo não suportado ou arquivo muito grande. Envie um arquivo PDF, JPEG, PNG, TIFF, BMP ou WebP (máx. 17 MB).",
        "ask_language": (
            "Em que idioma está o texto?\n"
            "1: Ucraniano 🇺🇦\n"
            "2: Russo 🇷🇺\n"
            "3: Inglês 🇬🇧\n"
            "Responda com 1, 2 ou 3."
        ),
        "lang_placeholder": "1: Ucraniano, 2: Russo, 3: Inglês",
        "invalid_choice": "Opção inválida. Responda com 1, 2 ou 3.",
        "processing": "⏳ Processando seu arquivo e gerando o áudio, aguarde...",
        "no_text": "Nenhum texto encontrado no documento.",
        "synthesis_error": "Ocorreu um erro interno durante a síntese de voz. Tente novamente mais tarde.",
        "playback_tip": "Dica: toque no selo 1x do áudio para alterar a velocidade de reprodução.",
        "generic_error": "❌ Ocorreu um erro interno ao processar sua solicitação. Tente novamente mais tarde.",
        "analyzing": "🔎 Lendo o documento…",
        "detected_lang": "🔎 Idioma detectado: {lang}.",
        "generating_audio": "⏳ Gerando o áudio…",
        "choose_language": "🔊 Em que idioma está o texto? Escolha abaixo:",
        "detected_choice": "✅ {lang} — detectado",
        "using_default": "🗣 Usando o seu idioma salvo: {lang}.",
        "language_menu": "🌐 Escolha um idioma padrão para o áudio. Vou usá-lo sempre. Escolha «Detecção automática» para voltar ao modo automático.",
        "auto_button": "🔄 Detecção automática",
        "default_set": "✅ Idioma padrão definido: {lang}.",
        "default_auto": "✅ Detecção automática ativada — vou detectar o idioma de cada documento.",
    },
}
DEFAULT_UI_LANG = "en"


def resolve_ui_lang(update: Update) -> str:
    """Pick the interface language from the Telegram client's language_code,
    falling back to English for unknown/unset values."""
    user = update.effective_user
    code = (user.language_code or "")[:2].lower() if user else ""
    return code if code in MESSAGES else DEFAULT_UI_LANG


def t(update: Update, key: str, **kwargs) -> str:
    """Return the localized UI string for `key` in the user's interface language,
    falling back to English. kwargs are applied via str.format if provided."""
    lang = resolve_ui_lang(update)
    msg = MESSAGES.get(lang, MESSAGES[DEFAULT_UI_LANG]).get(key) or MESSAGES[DEFAULT_UI_LANG][key]
    return msg.format(**kwargs) if kwargs else msg

DOCUMENT_INTELLIGENCE_ENDPOINT = os.environ["AZURE_FORM_RECOGNIZER_ENDPOINT"]
DOCUMENT_INTELLIGENCE_KEY = os.environ["AZURE_FORM_RECOGNIZER_KEY"]
SPEECH_API_KEY = os.environ["AZURE_SPEECH_API_KEY"]
SPEECH_REGION = os.environ["AZURE_REGION"]
TELEGRAM_API_TOKEN = os.environ["TELEGRAM_API_TOKEN"]
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "").rstrip("/")
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")
BOT_ENV = os.environ.get("BOT_ENV", "local")
AZURE_STORAGE_CONNECTION_STRING = os.environ.get("AZURE_STORAGE_CONNECTION_STRING", "")


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


# --- User preference store (Azure Table Storage, with in-memory fallback) ---
# Persists each user's pinned default language and recently-used languages so the
# bot can skip the menu on repeat use. Falls back to an in-memory dict when no
# connection string is configured (local dev) — the bot still works, just without
# cross-restart memory.
USER_TABLE_NAME = "users"
MAX_RECENT_LANGS = 3


class _MemoryStore:
    def __init__(self):
        self._d = {}

    def get_user(self, user_id):
        return dict(self._d.get(user_id, {}))

    def set_default_lang(self, user_id, locale2):
        u = self._d.setdefault(user_id, {})
        if locale2:
            u["default_lang"] = locale2
        else:
            u.pop("default_lang", None)

    def add_recent_lang(self, user_id, locale2):
        u = self._d.setdefault(user_id, {})
        recent = [x for x in u.get("recent", "").split(",") if x]
        recent = [locale2] + [x for x in recent if x != locale2]
        u["recent"] = ",".join(recent[:MAX_RECENT_LANGS])


class _TableStore:
    """One entity per user: PartitionKey='user', RowKey=str(user_id)."""
    def __init__(self, connection_string):
        from azure.data.tables import TableServiceClient
        svc = TableServiceClient.from_connection_string(connection_string)
        svc.create_table_if_not_exists(USER_TABLE_NAME)
        self._client = svc.get_table_client(USER_TABLE_NAME)

    def get_user(self, user_id):
        from azure.core.exceptions import ResourceNotFoundError
        try:
            e = self._client.get_entity("user", str(user_id))
            return {"default_lang": e.get("default_lang") or "", "recent": e.get("recent") or ""}
        except ResourceNotFoundError:
            return {}
        except Exception as ex:
            logger.warning(f"user_store.get_user failed: {ex!r}")
            return {}

    def _upsert(self, user_id, **fields):
        entity = {"PartitionKey": "user", "RowKey": str(user_id)}
        entity.update(fields)
        try:
            self._client.upsert_entity(entity)  # default mode merges fields
        except Exception as ex:
            logger.warning(f"user_store.upsert failed: {ex!r}")

    def set_default_lang(self, user_id, locale2):
        self._upsert(user_id, default_lang=locale2 or "")

    def add_recent_lang(self, user_id, locale2):
        u = self.get_user(user_id)
        recent = [x for x in u.get("recent", "").split(",") if x]
        recent = [locale2] + [x for x in recent if x != locale2]
        self._upsert(user_id, recent=",".join(recent[:MAX_RECENT_LANGS]))


def _build_user_store():
    if not AZURE_STORAGE_CONNECTION_STRING:
        logger.info("No AZURE_STORAGE_CONNECTION_STRING — using in-memory user store.")
        return _MemoryStore()
    try:
        store = _TableStore(AZURE_STORAGE_CONNECTION_STRING)
        logger.info("User store: Azure Table Storage.")
        return store
    except Exception as ex:
        logger.error(f"Table store init failed ({ex!r}); using in-memory store.")
        return _MemoryStore()


user_store = _build_user_store()

# Detected OCR locale (2-letter) -> Azure Neural TTS voice + display name/flag.
# Drives both auto-pick (from OCR language detection) and the manual picker.
VOICE_MAP = {
    # Curated target-market languages (also shown as menu buttons):
    "en": {"lang_code": "en-US", "voice": "en-US-AriaNeural",      "name": "English",    "flag": "🇬🇧"},
    "uk": {"lang_code": "uk-UA", "voice": "uk-UA-PolinaNeural",    "name": "Українська", "flag": "🇺🇦"},
    "ru": {"lang_code": "ru-RU", "voice": "ru-RU-DmitryNeural",    "name": "Русский",    "flag": "🇷🇺"},
    "es": {"lang_code": "es-ES", "voice": "es-ES-ElviraNeural",    "name": "Español",    "flag": "🇪🇸"},
    "de": {"lang_code": "de-DE", "voice": "de-DE-KatjaNeural",     "name": "Deutsch",    "flag": "🇩🇪"},
    "fr": {"lang_code": "fr-FR", "voice": "fr-FR-DeniseNeural",    "name": "Français",   "flag": "🇫🇷"},
    "pl": {"lang_code": "pl-PL", "voice": "pl-PL-AgnieszkaNeural", "name": "Polski",     "flag": "🇵🇱"},
    "pt": {"lang_code": "pt-PT", "voice": "pt-PT-RaquelNeural",    "name": "Português",  "flag": "🇵🇹"},
    "it": {"lang_code": "it-IT", "voice": "it-IT-ElsaNeural",      "name": "Italiano",   "flag": "🇮🇹"},
    "nl": {"lang_code": "nl-NL", "voice": "nl-NL-ColetteNeural",   "name": "Nederlands", "flag": "🇳🇱"},
    "tr": {"lang_code": "tr-TR", "voice": "tr-TR-EmelNeural",      "name": "Türkçe",     "flag": "🇹🇷"},
    "kk": {"lang_code": "kk-KZ", "voice": "kk-KZ-AigulNeural",     "name": "Қазақша",    "flag": "🇰🇿"},
    # Extra languages reachable via auto-detect (not shown in the manual menu):
    "ar": {"lang_code": "ar-EG", "voice": "ar-EG-SalmaNeural",     "name": "العربية",    "flag": "🇪🇬"},
    "cs": {"lang_code": "cs-CZ", "voice": "cs-CZ-VlastaNeural",    "name": "Čeština",    "flag": "🇨🇿"},
    "da": {"lang_code": "da-DK", "voice": "da-DK-ChristelNeural",  "name": "Dansk",      "flag": "🇩🇰"},
    "el": {"lang_code": "el-GR", "voice": "el-GR-AthinaNeural",    "name": "Ελληνικά",   "flag": "🇬🇷"},
    "fi": {"lang_code": "fi-FI", "voice": "fi-FI-SelmaNeural",     "name": "Suomi",      "flag": "🇫🇮"},
    "he": {"lang_code": "he-IL", "voice": "he-IL-HilaNeural",      "name": "עברית",      "flag": "🇮🇱"},
    "hi": {"lang_code": "hi-IN", "voice": "hi-IN-SwaraNeural",     "name": "हिन्दी",      "flag": "🇮🇳"},
    "hu": {"lang_code": "hu-HU", "voice": "hu-HU-NoemiNeural",     "name": "Magyar",     "flag": "🇭🇺"},
    "id": {"lang_code": "id-ID", "voice": "id-ID-GadisNeural",     "name": "Indonesia",  "flag": "🇮🇩"},
    "ja": {"lang_code": "ja-JP", "voice": "ja-JP-NanamiNeural",    "name": "日本語",      "flag": "🇯🇵"},
    "ko": {"lang_code": "ko-KR", "voice": "ko-KR-SunHiNeural",     "name": "한국어",      "flag": "🇰🇷"},
    "ro": {"lang_code": "ro-RO", "voice": "ro-RO-AlinaNeural",     "name": "Română",     "flag": "🇷🇴"},
    "sv": {"lang_code": "sv-SE", "voice": "sv-SE-SofieNeural",     "name": "Svenska",    "flag": "🇸🇪"},
    "th": {"lang_code": "th-TH", "voice": "th-TH-PremwadeeNeural", "name": "ไทย",        "flag": "🇹🇭"},
    "vi": {"lang_code": "vi-VN", "voice": "vi-VN-HoaiMyNeural",    "name": "Tiếng Việt", "flag": "🇻🇳"},
    "zh": {"lang_code": "zh-CN", "voice": "zh-CN-XiaoxiaoNeural",  "name": "中文",        "flag": "🇨🇳"},
}

# Languages shown as buttons in the manual picker (target markets). Auto-detect
# can still pick any language in VOICE_MAP beyond this list.
MENU_LANGS = ["en", "uk", "ru", "es", "de", "fr", "pl", "pt", "it", "nl", "tr", "kk"]

# Auto-proceed to synthesis only when detection is at least this confident and
# the dominant language covers at least this fraction of the text; else we ask.
AUTO_DETECT_MIN_CONFIDENCE = 0.6
AUTO_DETECT_MIN_COVERAGE = 0.6


def detect_dominant_language(result):
    """Aggregate AnalyzeResult.languages into a single dominant language.

    Returns (locale2, confidence, coverage):
    - locale2: 2-letter code (e.g. 'fr') or None if detection is unavailable
    - confidence: text-length-weighted mean confidence for that language
    - coverage: fraction of detected text that is in that language
    """
    langs = getattr(result, "languages", None) or []
    weighted_conf = defaultdict(float)
    length = defaultdict(float)
    total = 0.0
    for lang in langs:
        locale2 = ((lang.locale or "")[:2]).lower()
        if not locale2:
            continue
        span_len = float(sum((s.length or 0) for s in (lang.spans or []))) or 1.0
        conf = float(lang.confidence or 0.0)
        weighted_conf[locale2] += span_len * conf
        length[locale2] += span_len
        total += span_len
    if not length or total <= 0:
        return None, 0.0, 0.0
    best = max(length, key=lambda k: length[k])
    confidence = weighted_conf[best] / length[best] if length[best] else 0.0
    coverage = length[best] / total
    return best, confidence, coverage

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
    await update.message.reply_text(t(update, "help"))

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    lang_code = update.effective_user.language_code[:2] if update.effective_user.language_code else "Unknown"
    logger.info(f"UserStartedBot: user_id={user_id}, lang={lang_code}")
    await update.message.reply_text(
        f"{t(update, 'welcome')}\n{t(update, 'help')}"
    )

def build_language_keyboard(update: Update, detected_locale, recent=None) -> InlineKeyboardMarkup:
    """Inline keyboard: detected language pinned first (if recognized), then the
    user's recently-used languages, then the curated target-market languages,
    2 per row, de-duplicated. No typing required — built for screen-reader users
    who navigate by tapping buttons."""
    rows = []
    seen = set()
    if detected_locale in VOICE_MAP:
        info = VOICE_MAP[detected_locale]
        rows.append([InlineKeyboardButton(
            t(update, "detected_choice").format(lang=f'{info["flag"]} {info["name"]}'),
            callback_data=f"lang:{detected_locale}")])
        seen.add(detected_locale)
    ordered = []
    for code in (list(recent or []) + MENU_LANGS):
        if code in VOICE_MAP and code not in seen:
            seen.add(code)
            ordered.append(code)
    row = []
    for code in ordered:
        info = VOICE_MAP[code]
        row.append(InlineKeyboardButton(f'{info["flag"]} {info["name"]}', callback_data=f"lang:{code}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return InlineKeyboardMarkup(rows)


async def handle_file(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Entry point: download → OCR (with language detection) → auto-synthesize if
    the language is confidently detected, otherwise show the language picker."""
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    logger.info(f"User {user_id} sent a file or photo")
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
        await update.message.reply_text(t(update, "unsupported_file"))
        log_usage(user_id, status="failure", reason="unsupported_file",
                  file_type=file_type, file_size_kb=file_size_kb)
        return

    status_message = await update.message.reply_text(t(update, "analyzing"))
    stop_typing = asyncio.Event()
    typing_task = asyncio.create_task(_keep_typing(context.bot, chat_id, stop_typing))
    file_path = None
    t0 = time.monotonic()
    try:
        tg_file = await file.get_file()
        file_path = tempfile.mktemp()
        await tg_file.download_to_drive(file_path)
        with open(file_path, "rb") as f:
            poller = doc_client.begin_analyze_document(
                "prebuilt-read", f, features=[DocumentAnalysisFeature.LANGUAGES]
            )
            result = poller.result()
        ocr_pages = len(result.pages)
        extracted_text = ""
        for page in result.pages:
            for line in page.lines:
                extracted_text += line.content + "\n"
        normalized_text = normalize_ocr_text(extracted_text)
        ocr_ms = round((time.monotonic() - t0) * 1000)

        if not normalized_text.strip():
            logger.info(f"User {user_id} uploaded a file with no detectable text")
            await status_message.edit_text(t(update, "no_text"))
            await update.message.reply_text(t(update, "help"))
            log_usage(user_id, status="failure", reason="no_text", ocr_pages=ocr_pages,
                      file_type=file_type, file_size_kb=file_size_kb, duration_ms=ocr_ms)
            return

        locale2, conf, coverage = detect_dominant_language(result)
        logger.info(f"User {user_id}: detected lang={locale2} conf={conf:.2f} coverage={coverage:.2f}")
        context.user_data["ocr_job"] = {
            "text": normalized_text, "ocr_pages": ocr_pages, "ocr_ms": ocr_ms,
            "file_type": file_type, "file_size_kb": file_size_kb,
        }

        prefs = user_store.get_user(user_id)
        default_lang = (prefs.get("default_lang") or "").strip()
        if default_lang in VOICE_MAP:
            # User pinned a language via /language — skip detection and the menu.
            info = VOICE_MAP[default_lang]
            await status_message.edit_text(
                t(update, "using_default").format(lang=f'{info["flag"]} {info["name"]}')
            )
            stop_typing.set()
            await synthesize_and_send(update, context, default_lang, status_message=None)
        elif (locale2 in VOICE_MAP and conf >= AUTO_DETECT_MIN_CONFIDENCE
                and coverage >= AUTO_DETECT_MIN_COVERAGE):
            info = VOICE_MAP[locale2]
            await status_message.edit_text(
                t(update, "detected_lang").format(lang=f'{info["flag"]} {info["name"]}')
            )
            stop_typing.set()
            await synthesize_and_send(update, context, locale2, status_message=None)
        else:
            recent = [c for c in prefs.get("recent", "").split(",") if c]
            await status_message.edit_text(
                t(update, "choose_language"),
                reply_markup=build_language_keyboard(update, locale2, recent)
            )
    except Exception as e:
        logger.error(f"OCR/handle exception for user {user_id}: {e!r}")
        logger.error(traceback.format_exc())
        try:
            await status_message.edit_text(t(update, "generic_error"))
        except Exception:
            await update.message.reply_text(t(update, "generic_error"))
        await update.message.reply_text(t(update, "help"))
        log_usage(user_id, status="failure", reason="ocr_exception",
                  file_type=file_type, file_size_kb=file_size_kb,
                  duration_ms=round((time.monotonic() - t0) * 1000))
    finally:
        stop_typing.set()
        typing_task.cancel()
        try:
            await typing_task
        except asyncio.CancelledError:
            pass
        remove_temp_file(file_path)

async def synthesize_and_send(update: Update, context: ContextTypes.DEFAULT_TYPE,
                              locale2: str, status_message=None) -> None:
    """Synthesize the stored OCR text into a voice message in the chosen language.
    Shared by the auto-detect path and the manual inline-picker callback."""
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    job = context.user_data.get("ocr_job")
    if not job:
        # Stale callback (e.g. after a scale-to-zero restart) — nothing to synthesize.
        await context.bot.send_message(chat_id, t(update, "help"))
        return

    info = VOICE_MAP.get(locale2) or VOICE_MAP["en"]
    lang_code = info["lang_code"]
    voice = info["voice"]
    lang_label = f'{info["flag"]} {info["name"]}'
    normalized_text = job["text"]
    ocr_pages = job.get("ocr_pages")
    ocr_ms = job.get("ocr_ms", 0)
    file_type = job.get("file_type")
    file_size_kb = job.get("file_size_kb")

    audio_path = None
    ogg_path = None
    t0 = time.monotonic()
    stop_typing = asyncio.Event()
    typing_task = asyncio.create_task(_keep_typing(context.bot, chat_id, stop_typing))

    def elapsed_ms():
        return ocr_ms + round((time.monotonic() - t0) * 1000)

    try:
        if status_message is not None:
            try:
                await status_message.edit_text(t(update, "generating_audio").format(lang=lang_label))
            except Exception:
                status_message = None
        if status_message is None:
            status_message = await context.bot.send_message(
                chat_id, t(update, "generating_audio").format(lang=lang_label)
            )

        escaped_text = escape_ssml(normalized_text)
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
            await context.bot.send_message(chat_id, t(update, "synthesis_error"))
            await context.bot.send_message(chat_id, t(update, "help"))
            log_usage(user_id, status="failure", reason="synthesis_error", language=info["name"],
                      ocr_pages=ocr_pages, file_type=file_type, file_size_kb=file_size_kb,
                      duration_ms=elapsed_ms())
            return

        ogg_path = f"{tempfile.mktemp()}.ogg"
        convert_mp3_to_ogg(audio_path, ogg_path)

        with open(ogg_path, "rb") as voice_file:
            await context.bot.send_voice(chat_id=chat_id, voice=voice_file)
        await context.bot.send_message(chat_id, t(update, "playback_tip"))
        await context.bot.send_message(chat_id, t(update, "help"))
        logger.info(f"User {user_id} processed a file in language {locale2}")
        log_usage(user_id, status="success", language=info["name"], ocr_pages=ocr_pages,
                  tts_chars=len(normalized_text), file_type=file_type, file_size_kb=file_size_kb,
                  duration_ms=elapsed_ms())

    except Exception as e:
        logger.error(f"Exception for user {user_id}: {e!r}")
        logger.error(traceback.format_exc())
        await context.bot.send_message(chat_id, t(update, "generic_error"))
        await context.bot.send_message(chat_id, t(update, "help"))
        log_usage(user_id, status="failure", reason="exception", language=info["name"],
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
            if status_message is not None:
                await status_message.delete()
        except Exception:
            pass
        for path in [audio_path, ogg_path]:
            remove_temp_file(path)
        context.user_data.pop("ocr_job", None)


async def on_language_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    data = query.data or ""
    if not data.startswith("lang:"):
        return
    locale2 = data.split(":", 1)[1]
    if locale2 in VOICE_MAP:
        try:
            user_store.add_recent_lang(update.effective_user.id, locale2)
        except Exception as ex:
            logger.warning(f"add_recent_lang failed: {ex!r}")
    await synthesize_and_send(update, context, locale2, status_message=query.message)


def build_default_lang_keyboard(update: Update) -> InlineKeyboardMarkup:
    """Picker for the /language command: 'Auto-detect' plus the curated languages."""
    rows = [[InlineKeyboardButton(t(update, "auto_button"), callback_data="setlang:auto")]]
    row = []
    for code in MENU_LANGS:
        info = VOICE_MAP[code]
        row.append(InlineKeyboardButton(f'{info["flag"]} {info["name"]}', callback_data=f"setlang:{code}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return InlineKeyboardMarkup(rows)


async def language_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        t(update, "language_menu"), reply_markup=build_default_lang_keyboard(update)
    )


async def on_setlang_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    data = query.data or ""
    if not data.startswith("setlang:"):
        return
    choice = data.split(":", 1)[1]
    user_id = update.effective_user.id
    if choice == "auto":
        try:
            user_store.set_default_lang(user_id, "")
        except Exception as ex:
            logger.warning(f"set_default_lang failed: {ex!r}")
        await query.edit_message_text(t(update, "default_auto"))
    elif choice in VOICE_MAP:
        try:
            user_store.set_default_lang(user_id, choice)
        except Exception as ex:
            logger.warning(f"set_default_lang failed: {ex!r}")
        info = VOICE_MAP[choice]
        await query.edit_message_text(
            t(update, "default_set").format(lang=f'{info["flag"]} {info["name"]}')
        )

# --- Main entrypoint ---
async def _post_init(application) -> None:
    """Register the slash-command menu so /language is discoverable."""
    from telegram import BotCommand
    try:
        await application.bot.set_my_commands([
            BotCommand("start", "Start / how it works"),
            BotCommand("help", "How to use the bot"),
            BotCommand("language", "Set audio language (or auto-detect)"),
        ])
    except Exception as ex:
        logger.warning(f"set_my_commands failed: {ex!r}")


def main() -> None:
    app = (
        ApplicationBuilder()
        .token(TELEGRAM_API_TOKEN)
        .read_timeout(60)
        .write_timeout(60)
        .connect_timeout(15)
        .post_init(_post_init)
        .build()
    )
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("language", language_command))
    app.add_handler(MessageHandler(filters.Document.ALL | filters.PHOTO, handle_file))
    app.add_handler(CallbackQueryHandler(on_setlang_callback, pattern=r"^setlang:"))
    app.add_handler(CallbackQueryHandler(on_language_callback, pattern=r"^lang:"))

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