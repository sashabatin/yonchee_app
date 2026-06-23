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
from datetime import datetime, timedelta
from collections import defaultdict, deque
from typing import NamedTuple, Optional
from dotenv import load_dotenv

try:
    from opencensus.ext.azure.log_exporter import AzureLogHandler
except ImportError:
    AzureLogHandler = None

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler, CallbackQueryHandler,
    ContextTypes, filters, TypeHandler, ApplicationHandlerStop
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
        "lang_hint": "🌐 By default I detect the document's language automatically. If I get it wrong, or to always use a specific language, tap /language.",
        "start_free_quota": "🆓 You get {free_total} free requests every day. Check your balance anytime with /limits.",
        "bot_description": "Send a photo or PDF and I'll read the text aloud as a voice message. I detect the language automatically — or set one with /language. Made to help people with low vision.",
        "bot_short_description": "Photo/PDF → voice message. Automatic language detection. Helps people with low vision.",
        "feedback_prompt": "📝 Please type your feedback in the next message — I'll pass it on. (You can also use /feedback your text.)",
        "feedback_thanks": "🙏 Thank you for your feedback!",
        "support_menu": "💙 Support the project (donate)\n\nChoose a coffee cup pack to get bonus requests:",
        "support_pack_1": "☕ One cup · +50",
        "support_pack_2": "☕☕ Two cups · +150",
        "support_pack_3": "☕☕☕ Three cups · +300",
        "support_custom_button": "💰 Custom amount",
        "support_custom": "💡 Custom amount flow is next. For now, use coffee cup packs.",
        "support_bonus_activated": "✅ Bonus activated: +{bonus} requests until {bonus_until}.",
        "support_payment_pending": "⏳ Payment simulation is waiting for admin approval. You will be notified after review.",
        "support_request_rejected": "❌ Support request was not approved. You can try again later.",
        "precost_prompt": "⚠️ This file may consume about {cost} requests due to size/type. Continue?",
        "precost_continue_button": "✅ Continue",
        "precost_cancel_button": "✖️ Cancel",
        "precost_cancelled": "Canceled. File processing was not started.",
        "limits_status": "📊 Limits\n\nFree today: {free_left}/{free_total}\nBonus credits: {bonus_left}\nBonus valid until: {bonus_until}",
        "limit_reached": "⚠️ Today's free limit is exhausted and bonus credits are empty. Please come back tomorrow or support the project for extra requests.",
        "mission_intro": "☕ Funding accessibility\n\nYonchee was created to help people who find reading difficult. The name is connected to Ivan (Yonchee), whose eyesight worsened significantly in his youth, and that became the starting point for this product idea.",
        "mission_short_audio": "Yonchee helps people who find reading difficult by turning photos and PDFs into voice messages inside Telegram.",
        "onboarding_start_button": "🚀 Start",
        "onboarding_listen_button": "🔊 Listen to mission",
        "onboarding_support_button": "💙 Support the project (donate)",
        "onboarding_started": "Great. Send a photo or PDF, and I'll turn the text into a voice message.",
        "mission_audio_error": "I couldn't generate mission audio right now. Please try again in a bit.",
    },
    "ru": {
        "welcome": "👋 Добро пожаловать в бот Yonchee Text2Speech!",
        "help": (
            "Отправьте мне PDF или изображения (JPG, PNG, TIFF, BMP, WebP, до 17 МБ и 500 страниц).\n"
            "Я распознаю текст и пришлю его вам голосовым сообщением!\n\n"
            "⏱ Если ботом давно не пользовались, первый ответ может занять до 10 секунд — нужно «проснуться»."
        ),
        "unsupported_file": "❌ Неподдерживаемый тип файла или слишком большой файл. Пришлите PDF, JPEG, PNG, TIFF, BMP или WebP (до 17 МБ).",
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
        "lang_hint": "🌐 По умолчанию я определяю язык документа автоматически. Если язык определён неверно — или чтобы всегда использовать конкретный — нажмите /language.",
        "start_free_quota": "🆓 Каждый день у тебя есть {free_total} бесплатных запросов. Баланс всегда можно посмотреть через /limits.",
        "bot_description": "Пришлите фото или PDF, и я прочитаю текст вслух голосовым сообщением. Язык определяю автоматически — или задайте его через /language. Создан, чтобы помогать людям со слабым зрением.",
        "bot_short_description": "Фото/PDF → голосовое сообщение. Автоопределение языка. Помогает людям со слабым зрением.",
        "feedback_prompt": "📝 Напишите ваш отзыв следующим сообщением — я его передам. (Можно и так: /feedback ваш текст.)",
        "feedback_thanks": "🙏 Спасибо за отзыв!",
        "support_menu": "💙 Поддержать проект (донат)\n\nВыберите пакет «чашка кофе», чтобы получить бонусные запросы:",
        "support_pack_1": "☕ Одна чашка · +50",
        "support_pack_2": "☕☕ Две чашки · +150",
        "support_pack_3": "☕☕☕ Три чашки · +300",
        "support_custom_button": "💰 Своя сумма",
        "support_custom": "💡 Поток «своя сумма» будет следующим шагом. Пока используйте пакеты «чашка кофе».",
        "support_bonus_activated": "✅ Бонус активирован: +{bonus} запросов до {bonus_until}.",
        "support_payment_pending": "⏳ Имитация оплаты отправлена на апрув администратору. После проверки вы получите уведомление.",
        "support_request_rejected": "❌ Запрос поддержки не был одобрен. Попробуйте позже.",
        "precost_prompt": "⚠️ Из-за размера/типа этот файл может списать около {cost} запросов. Продолжить?",
        "precost_continue_button": "✅ Продолжить",
        "precost_cancel_button": "✖️ Отмена",
        "precost_cancelled": "Отменено. Обработка файла не запускалась.",
        "limits_status": "📊 Лимиты\n\nБесплатно сегодня: {free_left}/{free_total}\nБонусные запросы: {bonus_left}\nБонус действует до: {bonus_until}",
        "limit_reached": "⚠️ На сегодня бесплатный лимит исчерпан и бонусные запросы закончились. Возвращайтесь завтра или поддержите проект для дополнительных запросов.",
        "mission_intro": "☕ Финансирование доступности\n\nYonchee создан, чтобы помогать людям, которым сложно читать. Название связано с Иваном (Yonchee): в юности у него значительно ухудшилось зрение, и это стало отправной точкой идеи продукта.",
        # Audio-only (never displayed): Latin tokens are spelled phonetically so the
        # Russian neural voice doesn't mangle them (PDF -> "пи-ди-эф", brand -> "Йончи").
        "mission_short_audio": "Йончи помогает людям, которым сложно читать, превращая фото и пи-ди-эф в голосовые сообщения внутри Телеграма.",
        "onboarding_start_button": "🚀 Начать",
        "onboarding_listen_button": "🔊 Прослушать миссию",
        "onboarding_support_button": "💙 Поддержать проект (донат)",
        "onboarding_started": "Отлично. Отправьте фото или PDF, и я превращу текст в голосовое сообщение.",
        "mission_audio_error": "Сейчас не удалось сгенерировать аудио миссии. Попробуйте чуть позже.",
    },
    "uk": {
        "welcome": "👋 Ласкаво просимо до бота Yonchee Text2Speech!",
        "help": (
            "Надішліть мені PDF або зображення (JPG, PNG, TIFF, BMP, WebP, до 17 МБ і 500 сторінок).\n"
            "Я розпізнаю текст і надішлю його вам голосовим повідомленням!\n\n"
            "⏱ Якщо ботом давно не користувалися, перша відповідь може зайняти до 10 секунд — потрібно «прокинутися»."
        ),
        "unsupported_file": "❌ Непідтримуваний тип файлу або завеликий файл. Надішліть PDF, JPEG, PNG, TIFF, BMP або WebP (до 17 МБ).",
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
        "lang_hint": "🌐 За замовчуванням я визначаю мову документа автоматично. Якщо мову визначено неправильно — або щоб завжди використовувати певну — натисніть /language.",
        "start_free_quota": "🆓 Щодня в тебе є {free_total} безкоштовних запитів. Баланс завжди можна переглянути через /limits.",
        "bot_description": "Надішліть фото або PDF, і я прочитаю текст уголос голосовим повідомленням. Мову визначаю автоматично — або задайте її через /language. Створений, щоб допомагати людям зі слабким зором.",
        "bot_short_description": "Фото/PDF → голосове повідомлення. Автовизначення мови. Допомагає людям зі слабким зором.",
        "feedback_prompt": "📝 Напишіть ваш відгук наступним повідомленням — я його передам. (Можна й так: /feedback ваш текст.)",
        "feedback_thanks": "🙏 Дякуємо за відгук!",
        "support_menu": "💙 Підтримати проєкт (донат)\n\nОберіть пакет «чашка кави», щоб отримати бонусні запити:",
        "support_pack_1": "☕ Одна чашка · +50",
        "support_pack_2": "☕☕ Дві чашки · +150",
        "support_pack_3": "☕☕☕ Три чашки · +300",
        "support_custom_button": "💰 Своя сума",
        "support_custom": "💡 Потік «своя сума» буде наступним кроком. Поки використовуйте пакети «чашка кави».",
        "support_bonus_activated": "✅ Бонус активовано: +{bonus} запитів до {bonus_until}.",
        "support_payment_pending": "⏳ Імітацію оплати відправлено на апрув адміну. Після перевірки ви отримаєте повідомлення.",
        "support_request_rejected": "❌ Запит підтримки не було схвалено. Спробуйте пізніше.",
        "precost_prompt": "⚠️ Через розмір/тип цей файл може списати близько {cost} запитів. Продовжити?",
        "precost_continue_button": "✅ Продовжити",
        "precost_cancel_button": "✖️ Скасувати",
        "precost_cancelled": "Скасовано. Обробку файлу не запущено.",
    },
    "es": {
        "welcome": "👋 ¡Bienvenido al bot Yonchee Text2Speech!",
        "help": (
            "Envíame archivos PDF o imágenes (JPG, PNG, TIFF, BMP, WebP, hasta 17 MB y 500 páginas).\n"
            "¡Extraeré el texto y te lo enviaré como mensaje de voz!\n\n"
            "⏱ Si el bot no se ha usado en un tiempo, la primera respuesta puede tardar hasta 10 segundos en activarse."
        ),
        "unsupported_file": "❌ Tipo de archivo no compatible o archivo demasiado grande. Envía un PDF, JPEG, PNG, TIFF, BMP o WebP (máx. 17 MB).",
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
        "lang_hint": "🌐 Por defecto detecto el idioma del documento automáticamente. Si me equivoco, o para usar siempre un idioma concreto, toca /language.",
        "start_free_quota": "🆓 Tienes {free_total} solicitudes gratis cada día. Consulta tu saldo cuando quieras con /limits.",
        "bot_description": "Envía una foto o PDF y leeré el texto en voz alta como mensaje de voz. Detecto el idioma automáticamente — o configúralo con /language. Creado para ayudar a personas con baja visión.",
        "bot_short_description": "Foto/PDF → mensaje de voz. Detección automática de idioma. Ayuda a personas con baja visión.",
        "feedback_prompt": "📝 Escribe tu comentario en el siguiente mensaje — lo transmitiré. (También puedes usar /feedback tu texto.)",
        "feedback_thanks": "🙏 ¡Gracias por tu comentario!",
        "support_menu": "💙 Apoyar el proyecto (donar)\n\nElige un paquete tipo taza de cafe para obtener solicitudes extra:",
        "support_pack_1": "☕ Una taza · +50",
        "support_pack_2": "☕☕ Dos tazas · +150",
        "support_pack_3": "☕☕☕ Tres tazas · +300",
        "support_custom_button": "💰 Monto propio",
        "support_custom": "💡 El flujo de monto propio sera el siguiente paso. Por ahora usa los paquetes de taza de cafe.",
        "support_bonus_activated": "✅ Bono activado: +{bonus} solicitudes hasta {bonus_until}.",
        "support_payment_pending": "⏳ La simulacion de pago fue enviada para aprobacion del admin. Recibiras una notificacion despues de la revision.",
        "support_request_rejected": "❌ La solicitud de apoyo no fue aprobada. Intentalo mas tarde.",
        "precost_prompt": "⚠️ Este archivo puede consumir alrededor de {cost} solicitudes por tamano/tipo. Continuar?",
        "precost_continue_button": "✅ Continuar",
        "precost_cancel_button": "✖️ Cancelar",
        "precost_cancelled": "Cancelado. El procesamiento del archivo no se inicio.",
    },
    "de": {
        "welcome": "👋 Willkommen beim Yonchee Text2Speech-Bot!",
        "help": (
            "Schick mir PDFs oder Bilder (JPG, PNG, TIFF, BMP, WebP, bis zu 17 MB und 500 Seiten).\n"
            "Ich extrahiere den Text und sende ihn dir als Sprachnachricht!\n\n"
            "⏱ Wenn der Bot eine Weile nicht genutzt wurde, kann die erste Antwort bis zu 10 Sekunden dauern (Aufwachen)."
        ),
        "unsupported_file": "❌ Nicht unterstützter Dateityp oder Datei zu groß. Bitte sende eine PDF-, JPEG-, PNG-, TIFF-, BMP- oder WebP-Datei (max. 17 MB).",
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
        "lang_hint": "🌐 Standardmäßig erkenne ich die Sprache des Dokuments automatisch. Wenn ich falsch liege, oder für eine feste Sprache, tippe auf /language.",
        "start_free_quota": "🆓 Du hast jeden Tag {free_total} kostenlose Anfragen. Deinen Stand siehst du jederzeit mit /limits.",
        "bot_description": "Sende ein Foto oder PDF und ich lese den Text als Sprachnachricht vor. Die Sprache erkenne ich automatisch — oder lege sie mit /language fest. Für Menschen mit Sehbehinderung.",
        "bot_short_description": "Foto/PDF → Sprachnachricht. Automatische Spracherkennung. Für Menschen mit Sehbehinderung.",
        "feedback_prompt": "📝 Schreib dein Feedback in die nächste Nachricht — ich leite es weiter. (Oder nutze /feedback dein Text.)",
        "feedback_thanks": "🙏 Danke für dein Feedback!",
        "support_menu": "💙 Das Projekt unterstuetzen (Spende)\n\nWaehle ein Kaffeetassen-Paket fuer Bonus-Anfragen:",
        "support_pack_1": "☕ Eine Tasse · +50",
        "support_pack_2": "☕☕ Zwei Tassen · +150",
        "support_pack_3": "☕☕☕ Drei Tassen · +300",
        "support_custom_button": "💰 Eigener Betrag",
        "support_custom": "💡 Der Flow fuer eigenen Betrag kommt als naechster Schritt. Nutze vorerst die Kaffeetassen-Pakete.",
        "support_bonus_activated": "✅ Bonus aktiviert: +{bonus} Anfragen bis {bonus_until}.",
        "support_payment_pending": "⏳ Die Zahlungssimulation wartet auf Admin-Freigabe. Nach der Pruefung bekommst du eine Benachrichtigung.",
        "support_request_rejected": "❌ Die Support-Anfrage wurde nicht freigegeben. Bitte spaeter erneut versuchen.",
        "precost_prompt": "⚠️ Diese Datei kann wegen Groesse/Typ etwa {cost} Anfragen verbrauchen. Fortfahren?",
        "precost_continue_button": "✅ Fortfahren",
        "precost_cancel_button": "✖️ Abbrechen",
        "precost_cancelled": "Abgebrochen. Die Dateiverarbeitung wurde nicht gestartet.",
    },
    "fr": {
        "welcome": "👋 Bienvenue sur le bot Yonchee Text2Speech !",
        "help": (
            "Envoie-moi des PDF ou des images (JPG, PNG, TIFF, BMP, WebP, jusqu'à 17 Mo et 500 pages).\n"
            "J'extrais le texte et je te l'envoie sous forme de message vocal !\n\n"
            "⏱ Si le bot n'a pas été utilisé depuis un moment, la première réponse peut prendre jusqu'à 10 secondes (réveil)."
        ),
        "unsupported_file": "❌ Type de fichier non pris en charge ou fichier trop volumineux. Envoie un fichier PDF, JPEG, PNG, TIFF, BMP ou WebP (max. 17 Mo).",
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
        "lang_hint": "🌐 Par défaut, je détecte automatiquement la langue du document. Si je me trompe, ou pour toujours utiliser une langue précise, tape sur /language.",
        "start_free_quota": "🆓 Tu as {free_total} requêtes gratuites chaque jour. Vérifie ton solde à tout moment avec /limits.",
        "bot_description": "Envoie une photo ou un PDF et je lis le texte à voix haute en message vocal. Je détecte la langue automatiquement — ou définis-la avec /language. Conçu pour aider les personnes malvoyantes.",
        "bot_short_description": "Photo/PDF → message vocal. Détection automatique de la langue. Aide les personnes malvoyantes.",
        "feedback_prompt": "📝 Écris ton retour dans le prochain message — je le transmettrai. (Tu peux aussi utiliser /feedback ton texte.)",
        "feedback_thanks": "🙏 Merci pour ton retour !",
        "support_menu": "💙 Soutenir le projet (don)\n\nChoisis un pack tasse de cafe pour obtenir des requetes bonus :",
        "support_pack_1": "☕ Une tasse · +50",
        "support_pack_2": "☕☕ Deux tasses · +150",
        "support_pack_3": "☕☕☕ Trois tasses · +300",
        "support_custom_button": "💰 Montant perso",
        "support_custom": "💡 Le flux montant perso arrive ensuite. Pour l'instant, utilise les packs tasse de cafe.",
        "support_bonus_activated": "✅ Bonus active : +{bonus} requetes jusqu'au {bonus_until}.",
        "support_payment_pending": "⏳ La simulation de paiement attend la validation admin. Tu recevras une notification apres verification.",
        "support_request_rejected": "❌ La demande de soutien n'a pas ete approuvee. Reessaie plus tard.",
        "precost_prompt": "⚠️ Ce fichier peut consommer environ {cost} requetes selon sa taille/type. Continuer ?",
        "precost_continue_button": "✅ Continuer",
        "precost_cancel_button": "✖️ Annuler",
        "precost_cancelled": "Annule. Le traitement du fichier n'a pas demarre.",
    },
    "pl": {
        "welcome": "👋 Witaj w bocie Yonchee Text2Speech!",
        "help": (
            "Wyślij mi pliki PDF lub zdjęcia (JPG, PNG, TIFF, BMP, WebP, do 17 MB i 500 stron).\n"
            "Wyodrębnię tekst i odeślę go jako wiadomość głosową!\n\n"
            "⏱ Jeśli bot nie był używany przez jakiś czas, pierwsza odpowiedź może zająć do 10 sekund (wybudzanie)."
        ),
        "unsupported_file": "❌ Nieobsługiwany typ pliku lub plik zbyt duży. Wyślij plik PDF, JPEG, PNG, TIFF, BMP lub WebP (maks. 17 MB).",
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
        "lang_hint": "🌐 Domyślnie wykrywam język dokumentu automatycznie. Jeśli się pomylę, lub aby zawsze używać konkretnego języka, naciśnij /language.",
        "start_free_quota": "🆓 Każdego dnia masz {free_total} darmowych zapytań. Stan sprawdzisz w każdej chwili przez /limits.",
        "bot_description": "Wyślij zdjęcie lub PDF, a przeczytam tekst na głos jako wiadomość głosową. Język wykrywam automatycznie — lub ustaw go przez /language. Stworzony, by pomagać osobom słabowidzącym.",
        "bot_short_description": "Zdjęcie/PDF → wiadomość głosowa. Automatyczne wykrywanie języka. Pomaga osobom słabowidzącym.",
        "feedback_prompt": "📝 Napisz swoją opinię w następnej wiadomości — przekażę ją. (Możesz też użyć /feedback twój tekst.)",
        "feedback_thanks": "🙏 Dziękujemy za opinię!",
        "support_menu": "💙 Wesprzyj projekt (darowizna)\n\nWybierz pakiet kawa, aby otrzymac bonusowe zapytania:",
        "support_pack_1": "☕ Jedna kawa · +50",
        "support_pack_2": "☕☕ Dwie kawy · +150",
        "support_pack_3": "☕☕☕ Trzy kawy · +300",
        "support_custom_button": "💰 Wlasna kwota",
        "support_custom": "💡 Scenariusz wlasnej kwoty bedzie nastepnym krokiem. Na razie uzyj pakietow kawa.",
        "support_bonus_activated": "✅ Bonus aktywowany: +{bonus} zapytan do {bonus_until}.",
        "support_payment_pending": "⏳ Symulacja platnosci czeka na akceptacje admina. Po weryfikacji dostaniesz powiadomienie.",
        "support_request_rejected": "❌ Prosba o wsparcie nie zostala zatwierdzona. Sprobuj pozniej.",
        "precost_prompt": "⚠️ Ten plik moze zuzyc okolo {cost} zapytan przez rozmiar/typ. Kontynuowac?",
        "precost_continue_button": "✅ Kontynuuj",
        "precost_cancel_button": "✖️ Anuluj",
        "precost_cancelled": "Anulowano. Przetwarzanie pliku nie zostalo uruchomione.",
    },
    "pt": {
        "welcome": "👋 Bem-vindo ao bot Yonchee Text2Speech!",
        "help": (
            "Envie-me PDFs ou imagens (JPG, PNG, TIFF, BMP, WebP, até 17 MB e 500 páginas).\n"
            "Vou extrair o texto e enviá-lo a você como mensagem de voz!\n\n"
            "⏱ Se o bot não for usado há algum tempo, a primeira resposta pode levar até 10 segundos para acordar."
        ),
        "unsupported_file": "❌ Tipo de arquivo não suportado ou arquivo muito grande. Envie um arquivo PDF, JPEG, PNG, TIFF, BMP ou WebP (máx. 17 MB).",
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
        "lang_hint": "🌐 Por padrão, detecto o idioma do documento automaticamente. Se eu errar, ou para usar sempre um idioma específico, toque em /language.",
        "start_free_quota": "🆓 Você tem {free_total} pedidos grátis por dia. Veja seu saldo quando quiser com /limits.",
        "bot_description": "Envie uma foto ou PDF e eu leio o texto em voz alta como mensagem de voz. Detecto o idioma automaticamente — ou defina com /language. Feito para ajudar pessoas com baixa visão.",
        "bot_short_description": "Foto/PDF → mensagem de voz. Detecção automática de idioma. Ajuda pessoas com baixa visão.",
        "feedback_prompt": "📝 Escreva seu comentário na próxima mensagem — vou repassá-lo. (Você também pode usar /feedback seu texto.)",
        "feedback_thanks": "🙏 Obrigado pelo seu comentário!",
        "support_menu": "💙 Apoiar o projeto (doar)\n\nEscolha um pacote tipo xicara de cafe para receber pedidos bonus:",
        "support_pack_1": "☕ Uma xicara · +50",
        "support_pack_2": "☕☕ Duas xicaras · +150",
        "support_pack_3": "☕☕☕ Tres xicaras · +300",
        "support_custom_button": "💰 Valor proprio",
        "support_custom": "💡 O fluxo de valor proprio sera o proximo passo. Por enquanto use os pacotes de xicara de cafe.",
        "support_bonus_activated": "✅ Bonus ativado: +{bonus} pedidos ate {bonus_until}.",
        "support_payment_pending": "⏳ A simulacao de pagamento foi enviada para aprovacao do admin. Voce sera avisado apos a revisao.",
        "support_request_rejected": "❌ O pedido de apoio nao foi aprovado. Tente novamente mais tarde.",
        "precost_prompt": "⚠️ Este arquivo pode consumir cerca de {cost} pedidos por tamanho/tipo. Continuar?",
        "precost_continue_button": "✅ Continuar",
        "precost_cancel_button": "✖️ Cancelar",
        "precost_cancelled": "Cancelado. O processamento do arquivo nao foi iniciado.",
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


def t_lang(lang: str, key: str, **kwargs) -> str:
    """Resolve a localized string by language code directly."""
    code = (lang or "")[:2].lower()
    msg = MESSAGES.get(code, MESSAGES[DEFAULT_UI_LANG]).get(key) or MESSAGES[DEFAULT_UI_LANG][key]
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
ADMIN_USER_IDS = {x.strip() for x in os.environ.get("ADMIN_USER_IDS", "").split(",") if x.strip()}
# User IDs that bypass all daily/bonus limits (e.g. owner, testers). Admins are unlimited too.
UNLIMITED_USER_IDS = {x.strip() for x in os.environ.get("UNLIMITED_USER_IDS", "").split(",") if x.strip()}
OCR_FALLBACK = os.environ.get("OCR_FALLBACK", "llm").strip().lower()  # llm | tesseract
# Azure OpenAI (vision) for the LLM OCR fallback — reads scripts Azure Read can't
# (Georgian/Armenian). Falls back to Tesseract when these aren't set.
AZURE_OPENAI_ENDPOINT = os.environ.get("AZURE_OPENAI_ENDPOINT", "").strip()
AZURE_OPENAI_API_KEY = os.environ.get("AZURE_OPENAI_API_KEY", "").strip()
AZURE_OPENAI_DEPLOYMENT = os.environ.get("AZURE_OPENAI_DEPLOYMENT", "gpt-4.1-mini").strip()
AZURE_OPENAI_API_VERSION = os.environ.get("AZURE_OPENAI_API_VERSION", "2024-10-21").strip()
LLM_OCR_MAX_PDF_PAGES = int(os.environ.get("LLM_OCR_MAX_PDF_PAGES", "10"))
SUPPORT_PAYMENT_MODE = os.environ.get("SUPPORT_PAYMENT_MODE", "admin_stub").strip().lower()  # instant | admin_stub


def _is_admin(update) -> bool:
    """True only for ADMIN_USER_IDS, and only in a private (DM) chat.

    Fail-closed: returns False when ADMIN_USER_IDS is unset, when there's no
    effective user (e.g. channel posts), or when the action happens anywhere
    other than the admin's own private chat — so admin replies (which may list
    granted user IDs) can never surface in a group the bot was added to.
    """
    user = update.effective_user
    chat = update.effective_chat
    if not ADMIN_USER_IDS or user is None or chat is None:
        return False
    if chat.type != "private":
        return False
    return str(user.id) in ADMIN_USER_IDS


def _is_unlimited(user_id) -> bool:
    """True for IDs in UNLIMITED_USER_IDS (and any admin) — bypasses all quota limits."""
    uid = str(user_id)
    return uid in UNLIMITED_USER_IDS or uid in ADMIN_USER_IDS


# Bounded de-dupe of Telegram update_ids. With webhook + scale-to-zero, a cold start
# (~10-15 s) makes Telegram time out and re-deliver the same update several times; once
# the container is warm all the retries arrive and would each be processed. We drop any
# update_id we've already handled in this process.
_seen_update_ids = deque(maxlen=2048)
_seen_update_set = set()


async def _dedupe_update(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    upd_id = getattr(update, "update_id", None)
    if upd_id is None:
        return
    if upd_id in _seen_update_set:
        logger.info(f"Dropping duplicate update_id={upd_id}")
        raise ApplicationHandlerStop
    if len(_seen_update_ids) == _seen_update_ids.maxlen:
        _seen_update_set.discard(_seen_update_ids[0])  # evicted by the append below
    _seen_update_ids.append(upd_id)
    _seen_update_set.add(upd_id)


def log_usage(user_id: int, status: str, reason: str = None, language: str = None,
              ocr_pages: int = None, tts_chars: int = None, file_type: str = None,
              file_size_kb: int = None, duration_ms: int = None,
              cost_credits: int = None) -> None:
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
        "cost_credits": cost_credits,
    }
    dims.update({k: v for k, v in optional.items() if v is not None})
    logger.info("UsageMetrics", extra={"custom_dimensions": dims})


def log_feedback(user_id: int, ui_lang: str, text: str) -> None:
    """Emit a feedback event to App Insights (searchable in traces / the workbook)."""
    logger.info("UserFeedback", extra={"custom_dimensions": {
        "bot_env": BOT_ENV,
        "event_type": "feedback",
        "user_id": user_id,
        "language": ui_lang or "",
        "feedback": (text or "")[:1000],
    }})


def log_growth_event(user_id: int, event_type: str, source: str = None) -> None:
    """Emit growth/attribution events (e.g. /start deep-link source)."""
    dims = {
        "bot_env": BOT_ENV,
        "event_type": event_type,
        "user_id": user_id,
    }
    if source:
        dims["source"] = source[:100]
    logger.info("GrowthMetrics", extra={"custom_dimensions": dims})


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
FEEDBACK_TABLE_NAME = "feedback"
STORE_PARTITION = BOT_ENV or "user"  # isolate dev/prod data within one shared table
MAX_RECENT_LANGS = 3
FREE_DAILY_LIMIT = max(1, int(os.environ.get("FREE_DAILY_LIMIT", "10")))
# How long a pending /feedback prompt stays "armed" (survives scale-to-zero / replica
# switch via storage). Beyond this, a stray text message won't be captured as feedback.
FEEDBACK_WAIT_WINDOW_SEC = 3600


class _MemoryStore:
    def __init__(self):
        self._d = {}
        self._fb = []

    def get_user(self, user_id):
        return dict(self._d.get(user_id, {}))

    def set_quota_state(self, user_id, quota_day, daily_used, bonus_credits, bonus_until):
        u = self._d.setdefault(user_id, {})
        u["quota_day"] = quota_day or ""
        u["daily_used"] = str(max(0, int(daily_used or 0)))
        u["bonus_credits"] = str(max(0, int(bonus_credits or 0)))
        u["bonus_until"] = bonus_until or ""

    def set_unlimited(self, user_id, on):
        u = self._d.setdefault(user_id, {})
        if on:
            u["unlimited"] = "1"
        else:
            u.pop("unlimited", None)

    def list_unlimited_users(self):
        return [str(uid) for uid, u in self._d.items() if str(u.get("unlimited") or "") == "1"]

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

    def set_awaiting_feedback(self, user_id, on):
        u = self._d.setdefault(user_id, {})
        if on:
            u["awaiting_fb"] = int(time.time())
        else:
            u.pop("awaiting_fb", None)

    def add_feedback(self, user_id, username, ui_lang, text):
        self._fb.append({"user_id": str(user_id), "username": username or "",
                         "ui_lang": ui_lang or "", "text": text, "created": int(time.time() * 1000)})

    def list_recent_feedback(self, limit=10):
        return list(reversed(self._fb))[:limit]


class _TableStore:
    """Users in one table (PartitionKey=env, RowKey=user_id); feedback in another."""
    def __init__(self, connection_string):
        from azure.data.tables import TableServiceClient
        svc = TableServiceClient.from_connection_string(connection_string)
        svc.create_table_if_not_exists(USER_TABLE_NAME)
        svc.create_table_if_not_exists(FEEDBACK_TABLE_NAME)
        self._client = svc.get_table_client(USER_TABLE_NAME)
        self._fb_client = svc.get_table_client(FEEDBACK_TABLE_NAME)

    def get_user(self, user_id):
        from azure.core.exceptions import ResourceNotFoundError
        try:
            e = self._client.get_entity(STORE_PARTITION, str(user_id))
            return {"default_lang": e.get("default_lang") or "", "recent": e.get("recent") or "",
                    "awaiting_fb": e.get("awaiting_fb") or 0,
                    "quota_day": e.get("quota_day") or "",
                    "daily_used": e.get("daily_used") or "0",
                    "bonus_credits": e.get("bonus_credits") or "0",
                    "bonus_until": e.get("bonus_until") or "",
                    "unlimited": e.get("unlimited") or ""}
        except ResourceNotFoundError:
            return {}
        except Exception as ex:
            logger.warning(f"user_store.get_user failed: {ex!r}")
            return {}

    def _upsert(self, user_id, **fields):
        entity = {"PartitionKey": STORE_PARTITION, "RowKey": str(user_id)}
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

    def set_quota_state(self, user_id, quota_day, daily_used, bonus_credits, bonus_until):
        self._upsert(
            user_id,
            quota_day=quota_day or "",
            daily_used=str(max(0, int(daily_used or 0))),
            bonus_credits=str(max(0, int(bonus_credits or 0))),
            bonus_until=bonus_until or "",
        )

    def set_awaiting_feedback(self, user_id, on):
        # Persisted so a /feedback prompt survives scale-to-zero and replica switches.
        self._upsert(user_id, awaiting_fb=int(time.time()) if on else 0)

    def set_unlimited(self, user_id, on):
        self._upsert(user_id, unlimited="1" if on else "")

    def list_unlimited_users(self):
        try:
            items = self._client.query_entities(
                f"PartitionKey eq '{STORE_PARTITION}' and unlimited eq '1'")
            return [e.get("RowKey") for e in items]
        except Exception as ex:
            logger.warning(f"list_unlimited_users failed: {ex!r}")
            return []

    def add_feedback(self, user_id, username, ui_lang, text):
        ts = int(time.time() * 1000)
        entity = {
            "PartitionKey": STORE_PARTITION,
            "RowKey": f"{9999999999999 - ts}-{user_id}",  # reverse ts -> newest sorts first
            "user_id": str(user_id),
            "username": username or "",
            "ui_lang": ui_lang or "",
            "text": text,
            # Stored as a string: epoch ms exceeds Int32 and the SDK won't auto-promote
            # to Int64 (it raises), while Int64 reads back as an EntityProperty wrapper.
            # A numeric string sidesteps both and int()s cleanly for stats.
            "created": str(ts),
        }
        try:
            self._fb_client.create_entity(entity)
        except Exception as ex:
            logger.warning(f"add_feedback failed: {ex!r}")

    def list_recent_feedback(self, limit=10):
        try:
            items = list(self._fb_client.query_entities(
                f"PartitionKey eq '{STORE_PARTITION}'", results_per_page=limit))
            items.sort(key=lambda e: e.get("RowKey", ""))
            return items[:limit]
        except Exception as ex:
            logger.warning(f"list_recent_feedback failed: {ex!r}")
            return []


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
    "ka": {"lang_code": "ka-GE", "voice": "ka-GE-EkaNeural",       "name": "ქართული",   "flag": "🇬🇪"},
    "hy": {"lang_code": "hy-AM", "voice": "hy-AM-AnahitNeural",    "name": "Հայերեն",   "flag": "🇦🇲"},
}

# Unicode script ranges that map 1:1 to a language. Used to infer the content
# language directly from the OCR text, which is far more reliable than Azure's
# per-line guess for these distinct alphabets (e.g. Georgian was misread as Thai).
SCRIPT_RANGES = [
    ("ka", ((0x10A0, 0x10FF),)),                       # Georgian
    ("hy", ((0x0530, 0x058F),)),                       # Armenian
    ("el", ((0x0370, 0x03FF), (0x1F00, 0x1FFF))),      # Greek
    ("he", ((0x0590, 0x05FF),)),                       # Hebrew
    ("th", ((0x0E00, 0x0E7F),)),                       # Thai
    ("hi", ((0x0900, 0x097F),)),                       # Devanagari
    ("ko", ((0xAC00, 0xD7A3), (0x1100, 0x11FF))),      # Hangul
    ("ja", ((0x3040, 0x30FF),)),                        # Japanese kana
    ("han", ((0x4E00, 0x9FFF),)),                      # CJK Han (zh, or ja if kana present)
]

# Languages shown as buttons in the manual picker (target markets). Auto-detect
# can still pick any language in VOICE_MAP beyond this list.
MENU_LANGS = ["en", "uk", "ru", "es", "de", "fr", "pl", "pt", "it", "nl", "tr", "kk", "ka", "hy"]

# When a user has pinned one of these languages, we pass it to Azure Read as a
# `locale` hint, which helps recognition on degraded images (e.g. it stops badly-
# photographed Cyrillic from being read as Latin). Restricted to locales the qa
# harness confirmed Azure Read handles well: Kazakh ('kk') is excluded because the
# hint made it *worse* (read as Khmer), and ka/hy go through the fallback OCR
# engine, so a Read locale never applies to them.
OCR_LOCALE_HINT_LANGS = {"en", "uk", "ru", "es", "de", "fr", "pl", "pt", "it", "nl", "tr"}

# Auto-proceed to synthesis only when detection is at least this confident and
# the dominant language covers at least this fraction of the text; else we ask.
AUTO_DETECT_MIN_CONFIDENCE = 0.6
AUTO_DETECT_MIN_COVERAGE = 0.6
PRECOST_CONFIRM_MIN_COST = max(2, int(os.environ.get("PRECOST_CONFIRM_MIN_COST", "2")))

# Dev support packages (mock accrual now; real billing via Telegram Stars next).
SUPPORT_PACKS = {
    "coffee1": {"bonus": 50, "days": 30},
    "coffee2": {"bonus": 150, "days": 30},
    "coffee3": {"bonus": 300, "days": 30},
}


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


def detect_script_language(text):
    """Infer language from the dominant Unicode script of the OCR text.

    Reliable for scripts that map 1:1 to a language (Georgian, Armenian, Greek,
    Hebrew, Thai, Devanagari, Hangul, Japanese kana). Returns a 2-letter code,
    or None for shared scripts (Latin/Cyrillic/Arabic) where the script can't
    distinguish the language — those defer to Azure's language detection.
    """
    counts = defaultdict(int)
    letters = 0
    for ch in text:
        if not ch.isalpha():
            continue
        letters += 1
        o = ord(ch)
        for lang, ranges in SCRIPT_RANGES:
            if any(lo <= o <= hi for lo, hi in ranges):
                counts[lang] += 1
                break
    if letters == 0 or not counts:
        return None
    # CJK Han is shared: Japanese if any kana is present, otherwise Chinese.
    if counts.get("han"):
        if counts.get("ja"):
            counts["ja"] += counts.pop("han")
        else:
            counts["zh"] = counts.pop("han")
    best = max(counts, key=counts.get)
    if counts[best] / letters >= 0.5:
        return best
    return None


# A secondary language must cover at least this share of the page before we treat
# it as genuinely multilingual (and read each part with its own voice). Below this,
# a stray foreign word or a mis-tagged span is folded into the dominant language.
MULTI_LANG_MIN_SHARE = 0.15


def build_language_segments(result, dominant):
    """Turn Azure's per-span language detection into ordered (locale2, text)
    segments so a multilingual page (e.g. Russian prose with French quotes) can be
    read with the right voice per span instead of one voice for everything.

    Returns None for the common monolingual page — the caller then uses the
    single-voice path unchanged. Only languages with a TTS voice are honored;
    untagged gaps inherit the surrounding language so a block isn't split by a
    separator. Every character of result.content is preserved.
    """
    content = getattr(result, "content", None) or ""
    langs = getattr(result, "languages", None) or []
    n = len(content)
    if not n or not dominant:
        return None
    owner = [None] * n
    share = defaultdict(int)
    for lang in langs:
        loc = ((getattr(lang, "locale", "") or "")[:2]).lower()
        if loc not in VOICE_MAP:
            continue
        for s in (getattr(lang, "spans", None) or []):
            off = max(0, s.offset or 0)
            end = min(n, off + (s.length or 0))
            for i in range(off, end):
                if owner[i] is None:
                    owner[i] = loc
                    share[loc] += 1
    # Untagged chars (whitespace/punctuation between runs) inherit the preceding
    # language; a leading gap takes the dominant one.
    last = dominant
    for i in range(n):
        if owner[i] is None:
            owner[i] = last
        else:
            last = owner[i]
    if len({*owner}) < 2:
        return None
    if not any(loc != dominant and share.get(loc, 0) >= MULTI_LANG_MIN_SHARE * n
               for loc in set(owner)):
        return None
    segments = []
    start = 0
    for i in range(1, n + 1):
        if i == n or owner[i] != owner[start]:
            seg = content[start:i]
            if seg.strip():
                segments.append((owner[start], seg))
            start = i
    return segments if len(segments) > 1 else None


# --- Fallback OCR for languages Azure Read can't extract (e.g. Georgian) ---
FALLBACK_LANGS = {"ka", "hy"}        # routed to the fallback OCR engine
TESSERACT_LANG = {"ka": "kat", "hy": "hye"}


def run_tesseract_ocr(file_path, file_type, locale2):
    """OCR via local Tesseract (Georgian/Armenian language data in the image)."""
    import subprocess
    import glob
    lang = TESSERACT_LANG.get(locale2, "eng")
    images = [file_path]
    tmp_imgs = []
    if file_type == "pdf":
        prefix = tempfile.mktemp()
        r = subprocess.run(["pdftoppm", "-r", "300", "-png", file_path, prefix],
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if r.returncode != 0:
            logger.error(f"pdftoppm error: {r.stderr.decode(errors='ignore')[:200]}")
            raise RuntimeError("PDF rasterization failed")
        tmp_imgs = sorted(glob.glob(prefix + "*.png"))
        images = tmp_imgs or [file_path]
    texts = []
    try:
        for img in images:
            r = subprocess.run(["tesseract", img, "stdout", "-l", lang],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            if r.returncode != 0:
                logger.warning(f"tesseract error: {r.stderr.decode(errors='ignore')[:200]}")
                continue
            texts.append(r.stdout.decode("utf-8", errors="ignore"))
    finally:
        for p in tmp_imgs:
            remove_temp_file(p)
    return "\n".join(texts)


def _azure_openai_configured():
    return bool(AZURE_OPENAI_ENDPOINT and AZURE_OPENAI_API_KEY)


def _img_mime(data: bytes) -> str:
    """Sniff an image's MIME type from its magic bytes (for the vision data URL)."""
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    return "image/png"


def _pdf_to_png_bytes(file_path, max_pages):
    """Rasterize a PDF to PNG page images via PyMuPDF (no poppler dependency)."""
    import fitz  # PyMuPDF
    zoom = 200 / 72  # ~200 DPI is plenty for OCR
    mat = fitz.Matrix(zoom, zoom)
    out = []
    doc = fitz.open(file_path)
    try:
        for page in doc[:max_pages]:
            out.append(page.get_pixmap(matrix=mat).tobytes("png"))
    finally:
        doc.close()
    return out


LLM_OCR_PROMPT = (
    "You are a precise OCR engine. Transcribe ALL text in the image(s) exactly as "
    "written. Do not translate, summarize, correct, or add any commentary. "
    "Preserve the natural reading order; for multi-column layouts read the left "
    "column top-to-bottom first, then the next column. Group the transcription "
    "into segments by language. Return ONLY valid JSON of the form "
    '{"segments":[{"lang":"<ISO 639-1 code>","text":"<verbatim text>"}]}. '
    "Start a new segment every time the language changes."
)


def _parse_llm_segments(raw):
    """Parse the model's JSON reply into [(locale2, text)]; tolerant of code fences
    and surrounding prose. Returns [] if nothing parseable is found."""
    import json
    s = (raw or "").strip()
    i, j = s.find("{"), s.rfind("}")
    if i == -1 or j == -1 or j < i:
        return []
    try:
        data = json.loads(s[i:j + 1])
    except Exception:
        return []
    segs = []
    for item in (data.get("segments") or []):
        loc = str(item.get("lang", "")).strip().lower()[:2]
        txt = item.get("text", "")
        if loc and isinstance(txt, str) and txt.strip():
            segs.append((loc, txt))
    return segs


def run_llm_ocr(file_path, file_type, locale2):
    """OCR via Azure OpenAI vision (gpt-4.1-mini): reads scripts Azure Read can't
    (Georgian, Armenian, ...) and returns language-tagged segments so a mixed page
    is read with the right voice per language. Returns (text, raw_segments), or
    ('', None) when Azure OpenAI isn't configured or the call fails."""
    if not _azure_openai_configured():
        logger.warning("OCR_FALLBACK=llm but Azure OpenAI is not configured "
                       "(set AZURE_OPENAI_ENDPOINT / AZURE_OPENAI_API_KEY).")
        return "", None
    try:
        import base64
        from openai import AzureOpenAI
        if file_type == "pdf":
            images = [(png, "image/png") for png in
                      _pdf_to_png_bytes(file_path, LLM_OCR_MAX_PDF_PAGES)]
        else:
            with open(file_path, "rb") as f:
                data = f.read()
            images = [(data, _img_mime(data))]
        content = [{"type": "text", "text": LLM_OCR_PROMPT}]
        for img, mime in images:
            b64 = base64.b64encode(img).decode("ascii")
            content.append({"type": "image_url",
                            "image_url": {"url": f"data:{mime};base64,{b64}"}})
        client = AzureOpenAI(azure_endpoint=AZURE_OPENAI_ENDPOINT,
                             api_key=AZURE_OPENAI_API_KEY,
                             api_version=AZURE_OPENAI_API_VERSION)
        resp = client.chat.completions.create(
            model=AZURE_OPENAI_DEPLOYMENT,
            messages=[{"role": "user", "content": content}],
            temperature=0,
            max_tokens=8000,
            response_format={"type": "json_object"},
        )
        segs = _parse_llm_segments(resp.choices[0].message.content or "")
        return "\n\n".join(t for _, t in segs), segs
    except Exception as ex:
        logger.error(f"LLM OCR failed: {ex!r}")
        return "", None


def _segments_from_raw(raw_segments, fallback_lang):
    """Normalize LLM raw segments into (dominant_locale2, segments_or_None).

    Drops empty/voiceless spans, normalizes each like the Azure path, picks the
    language with the most text as dominant, and returns None for `segments` when
    only one language remains (so the single-voice path is used)."""
    norm = []
    for loc, txt in (raw_segments or []):
        t = normalize_ocr_text(txt or "")
        if t.strip() and loc in VOICE_MAP:
            norm.append((loc, t))
    if not norm:
        return fallback_lang, None
    totals = defaultdict(int)
    for loc, t in norm:
        totals[loc] += len(t)
    dominant = max(totals, key=totals.get)
    segments = norm if len(totals) > 1 else None
    return dominant, segments


def run_fallback_ocr(file_path, file_type, locale2):
    """Run the configured fallback OCR engine for an Azure-unsupported language.
    Returns (text, raw_segments) — raw_segments is None for the Tesseract engine."""
    if OCR_FALLBACK == "llm" and _azure_openai_configured():
        return run_llm_ocr(file_path, file_type, locale2)
    return run_tesseract_ocr(file_path, file_type, locale2), None

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
    # Join words hyphenated across a line break — but only with letters on both
    # sides, so numeric codes/ranges (e.g. "2.02.05-2020", "10-15") aren't merged
    # into "2.02.052020". A hyphen at a line break next to a digit keeps the
    # hyphen and just drops the break.
    text = re.sub(r'(?<=[^\W\d_])-\s*\n\s*(?=[^\W\d_])', '', text)
    text = re.sub(r'-\s*\n\s*(?=\d)', '-', text)
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


def _utc_today() -> str:
    return time.strftime("%Y-%m-%d", time.gmtime())


def _add_days(iso_day: str, days: int) -> str:
    base = datetime.strptime(iso_day, "%Y-%m-%d").date()
    return (base + timedelta(days=max(0, int(days)))).isoformat()


def _to_int(value, default=0):
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return default


def _load_quota(user_id: int):
    """Load and normalize user's quota state, auto-resetting day/expired bonus."""
    raw = user_store.get_user(user_id) or {}
    today = _utc_today()
    quota_day = (raw.get("quota_day") or "").strip()
    daily_used = max(0, _to_int(raw.get("daily_used"), 0))
    bonus_credits = max(0, _to_int(raw.get("bonus_credits"), 0))
    bonus_until = (raw.get("bonus_until") or "").strip()

    changed = False
    if quota_day != today:
        quota_day = today
        daily_used = 0
        changed = True
    if bonus_credits > 0 and bonus_until and bonus_until < today:
        bonus_credits = 0
        bonus_until = ""
        changed = True

    if changed:
        try:
            user_store.set_quota_state(user_id, quota_day, daily_used, bonus_credits, bonus_until)
        except Exception as ex:
            logger.warning(f"set_quota_state failed: {ex!r}")

    unlimited = _is_unlimited(user_id) or str(raw.get("unlimited") or "") == "1"
    if unlimited:
        return {
            "quota_day": quota_day,
            "daily_used": daily_used,
            "free_left": "∞",
            "free_total": "∞",
            "bonus_credits": "∞",
            "bonus_until": bonus_until or "-",
            "unlimited": True,
        }

    free_left = max(0, FREE_DAILY_LIMIT - daily_used)
    return {
        "quota_day": quota_day,
        "daily_used": daily_used,
        "free_left": free_left,
        "free_total": FREE_DAILY_LIMIT,
        "bonus_credits": bonus_credits,
        "bonus_until": bonus_until,
        "unlimited": False,
    }


def _consume_quota(user_id: int, cost: int = 1):
    """Spend daily free quota first, then bonus credits. Returns (ok, snapshot)."""
    snap = _load_quota(user_id)
    if cost <= 0 or snap.get("unlimited"):
        return True, snap
    free_left = snap["free_left"]
    bonus = snap["bonus_credits"]

    if free_left + bonus < cost:
        return False, snap

    if free_left >= cost:
        snap["daily_used"] += cost
    else:
        from_free = free_left
        snap["daily_used"] += from_free
        snap["bonus_credits"] = max(0, bonus - (cost - from_free))

    snap["free_left"] = max(0, FREE_DAILY_LIMIT - snap["daily_used"])
    try:
        user_store.set_quota_state(
            user_id,
            snap["quota_day"],
            snap["daily_used"],
            snap["bonus_credits"],
            snap["bonus_until"],
        )
    except Exception as ex:
        logger.warning(f"set_quota_state failed: {ex!r}")
    return True, snap


def _grant_bonus_credits(user_id: int, bonus: int, days: int):
    """Add bonus credits and extend bonus validity window."""
    bonus = max(0, int(bonus or 0))
    days = max(0, int(days or 0))
    if bonus <= 0:
        return _load_quota(user_id)

    snap = _load_quota(user_id)
    today = _utc_today()
    cur_until = (snap.get("bonus_until") or "").strip()
    if cur_until and cur_until >= today:
        start = cur_until
    else:
        start = today
    new_until = _add_days(start, days) if days > 0 else (cur_until or "")
    new_bonus = snap.get("bonus_credits", 0) + bonus

    try:
        user_store.set_quota_state(
            user_id,
            snap["quota_day"],
            snap["daily_used"],
            new_bonus,
            new_until,
        )
    except Exception as ex:
        logger.warning(f"set_quota_state failed: {ex!r}")
    return _load_quota(user_id)


def _estimate_request_cost(file_type: str, file_size_kb: Optional[int]) -> int:
    """Rough pre-cost estimate used for upfront confirmation on heavy files."""
    size = int(file_size_kb or 0)
    if file_type == "pdf":
        if size >= 5000:
            return 3
        if size >= 1200:
            return 2
        return 1
    if file_type == "image":
        if size >= 5000:
            return 2
    return 1


def _build_support_admin_keyboard(user_id: int, pack_key: str, ui_lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Approve", callback_data=f"supadm:approve:{user_id}:{pack_key}:{ui_lang}"),
            InlineKeyboardButton("✖️ Reject", callback_data=f"supadm:reject:{user_id}:{pack_key}:{ui_lang}"),
        ]
    ])

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
    # ReplyKeyboardRemove clears the legacy 1/2/3 language reply-keyboard left over
    # from an older version (current UI uses inline buttons). No-op for new users.
    await update.message.reply_text(t(update, "help"), reply_markup=ReplyKeyboardRemove())


async def limits_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    snap = _load_quota(update.effective_user.id)
    await update.message.reply_text(
        t(
            update,
            "limits_status",
            free_left=snap["free_left"],
            free_total=snap["free_total"],
            bonus_left=snap["bonus_credits"],
            bonus_until=(snap["bonus_until"] or "-"),
        )
    )


def build_support_keyboard(update: Update) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(t(update, "support_pack_1"), callback_data="sup:pack:coffee1")],
        [InlineKeyboardButton(t(update, "support_pack_2"), callback_data="sup:pack:coffee2")],
        [InlineKeyboardButton(t(update, "support_pack_3"), callback_data="sup:pack:coffee3")],
        [InlineKeyboardButton(t(update, "support_custom_button"), callback_data="sup:custom")],
    ]
    return InlineKeyboardMarkup(rows)


async def support_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    log_growth_event(update.effective_user.id, event_type="support_view")
    await update.message.reply_text(t(update, "support_menu"), reply_markup=build_support_keyboard(update))

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    lang_code = update.effective_user.language_code[:2] if update.effective_user.language_code else "Unknown"
    start_source = (context.args[0].strip() if context.args else "")[:100]
    logger.info(f"UserStartedBot: user_id={user_id}, lang={lang_code}, source={start_source or '-'}")
    log_growth_event(user_id, event_type="onboarding_start", source=start_source or None)
    await update.message.reply_text(
        f"{t(update, 'welcome')}\n\n{t(update, 'mission_intro')}\n\n"
        f"{t(update, 'start_free_quota', free_total=FREE_DAILY_LIMIT)}\n\n{t(update, 'lang_hint')}",
        reply_markup=build_onboarding_keyboard(update),
    )


def build_onboarding_keyboard(update: Update) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(t(update, "onboarding_start_button"), callback_data="onb:start")],
        [InlineKeyboardButton(t(update, "onboarding_listen_button"), callback_data="onb:listen")],
        [InlineKeyboardButton(t(update, "onboarding_support_button"), callback_data="onb:support")],
    ])

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


class OcrResult(NamedTuple):
    """Result of OCR + content-language detection — the bot's core extraction
    step decoupled from Telegram, so it can be reused (e.g. by the qa toolkit)."""
    text: str                   # normalized OCR text ('' when none found)
    ocr_pages: Optional[int]    # page count (None for the fallback OCR engine)
    locale2: Optional[str]      # detected content language, or None
    confidence: float           # detection confidence (text-length-weighted)
    coverage: float             # fraction of text in the dominant language
    script_lang: Optional[str]  # language inferred from a distinct script, if any
    used_fallback: bool         # True when the Tesseract/LLM fallback engine ran
    # Ordered (locale2, text) spans for a multilingual page, else None. When set,
    # synthesis reads each span with its own voice instead of one voice for all.
    segments: Optional[list] = None


def extract_text(file_path: str, file_type: str, pinned_lang: str = None) -> OcrResult:
    """Run OCR and detect the content language for a local file.

    Mirrors what the bot does in handle_file, without Telegram coupling:
    pinned_lang in FALLBACK_LANGS routes to the fallback OCR engine (no
    detection); otherwise Azure Read extracts the text and the language is
    inferred by script first, then by Azure's per-line detection.
    """
    if pinned_lang in FALLBACK_LANGS:
        raw, raw_segments = run_fallback_ocr(file_path, file_type, pinned_lang)
        text = normalize_ocr_text(raw or "")
        if not text.strip():
            return OcrResult("", None, None, 0.0, 0.0, None, True)
        dominant, segments = _segments_from_raw(raw_segments, pinned_lang)
        return OcrResult(text, None, dominant, 1.0, 1.0, None, True, segments)

    # A pinned language on the allowlist is passed to Azure Read as a locale hint,
    # which helps recognition on hard/degraded images. Omitted in the auto-detect
    # path (pinned_lang is None), where the language isn't known yet, and for
    # languages where the hint doesn't help (see OCR_LOCALE_HINT_LANGS).
    analyze_kwargs = {"features": [DocumentAnalysisFeature.LANGUAGES]}
    if pinned_lang in OCR_LOCALE_HINT_LANGS:
        analyze_kwargs["locale"] = pinned_lang
    with open(file_path, "rb") as f:
        poller = doc_client.begin_analyze_document("prebuilt-read", f, **analyze_kwargs)
        result = poller.result()
    ocr_pages = len(result.pages)
    extracted_text = ""
    for page in result.pages:
        for line in page.lines:
            extracted_text += line.content + "\n"
    normalized_text = normalize_ocr_text(extracted_text)
    locale2 = None
    if normalized_text.strip():
        script_lang = detect_script_language(normalized_text)
        if script_lang and script_lang in VOICE_MAP:
            # A distinct script is authoritative — Azure's per-line guess is
            # unreliable for these (e.g. Georgian was detected as Thai).
            locale2, conf, coverage = script_lang, 1.0, 1.0
        else:
            locale2, conf, coverage = detect_dominant_language(result)

    segments = build_language_segments(result, locale2) if locale2 else None
    distinct_langs = len({loc for loc, _ in segments}) if segments else (1 if locale2 else 0)

    if not normalized_text.strip() or locale2 not in VOICE_MAP or distinct_langs >= 3:
        # Azure Read found nothing, or what it found isn't one of our supported
        # languages, or it split the page into 3+ "languages" (real multilingual
        # pages are pairs — ru+fr, ka+en; 3-way splits are the signature of a
        # script it can't read at all turning into confidently-wrong garbage,
        # e.g. a Georgian page coming back as Thai+English+Indonesian). Either
        # way, give the LLM engine a shot before trusting this result. No-op
        # cost when LLM isn't configured.
        if OCR_FALLBACK == "llm" and _azure_openai_configured():
            raw, raw_segments = run_llm_ocr(file_path, file_type, pinned_lang)
            text = normalize_ocr_text(raw or "")
            if text.strip():
                dominant, rescued_segments = _segments_from_raw(raw_segments, pinned_lang or "en")
                return OcrResult(text, ocr_pages, dominant, 1.0, 1.0, None, True, rescued_segments)
        if not normalized_text.strip():
            return OcrResult("", ocr_pages, None, 0.0, 0.0, None, False)

    return OcrResult(normalized_text, ocr_pages, locale2, conf, coverage,
                     script_lang, False, segments)


async def _safe_edit_text(message, text, **kwargs):
    """Edit a status message without letting a Telegram edit failure abort the
    flow. A cold-start update redelivery can leave a just-sent status message
    momentarily non-editable ("Message can't be edited") — but a failed cosmetic
    status update must never stop us from delivering the OCR audio. Falls back to
    a fresh message so any attached keyboard/menu still reaches the user."""
    try:
        await message.edit_text(text, **kwargs)
    except Exception as ex:
        logger.warning(f"status edit_text failed, sending fresh message: {ex!r}")
        try:
            await message.chat.send_message(text, **kwargs)
        except Exception as ex2:
            logger.warning(f"status fallback send_message failed: {ex2!r}")


async def _process_file_payload(
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        file_id: str,
        file_type: str,
        file_size_kb: Optional[int],
        cost_credits: int,
) -> None:
    """Download → OCR/detect → synthesize flow for an accepted file payload."""
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    # reply_markup also clears the legacy 1/2/3 language reply-keyboard from older
    # versions so it disappears on the user's next file (no-op if they never had it).
    status_message = await context.bot.send_message(
        chat_id, t(update, "analyzing"), reply_markup=ReplyKeyboardRemove())
    stop_typing = asyncio.Event()
    typing_task = asyncio.create_task(_keep_typing(context.bot, chat_id, stop_typing))
    file_path = None
    t0 = time.monotonic()
    try:
        tg_file = await context.bot.get_file(file_id)
        file_path = tempfile.mktemp()
        await tg_file.download_to_drive(file_path)

        prefs = user_store.get_user(user_id)
        default_lang = (prefs.get("default_lang") or "").strip()

        if default_lang in FALLBACK_LANGS:
            ocr = await asyncio.to_thread(extract_text, file_path, file_type, default_lang)
            normalized_text = ocr.text
            ocr_ms = round((time.monotonic() - t0) * 1000)
            if not normalized_text.strip():
                await _safe_edit_text(status_message,t(update, "no_text"))
                await context.bot.send_message(chat_id, t(update, "help"))
                log_usage(user_id, status="failure", reason="no_text_fallback",
                          file_type=file_type, file_size_kb=file_size_kb, duration_ms=ocr_ms,
                          cost_credits=cost_credits)
                return
            context.user_data["ocr_job"] = {
                "text": normalized_text, "ocr_pages": ocr.ocr_pages, "ocr_ms": ocr_ms,
                "file_type": file_type, "file_size_kb": file_size_kb, "cost_credits": cost_credits,
            }
            info = VOICE_MAP[default_lang]
            await _safe_edit_text(status_message,
                t(update, "using_default").format(lang=f'{info["flag"]} {info["name"]}')
            )
            stop_typing.set()
            await synthesize_and_send(update, context, default_lang, status_message=None)
            return

        hint_lang = default_lang if default_lang in OCR_LOCALE_HINT_LANGS else None
        ocr = extract_text(file_path, file_type, pinned_lang=hint_lang)
        normalized_text = ocr.text
        ocr_pages = ocr.ocr_pages
        ocr_ms = round((time.monotonic() - t0) * 1000)

        if not normalized_text.strip():
            logger.info(f"User {user_id} uploaded a file with no detectable text")
            await _safe_edit_text(status_message,t(update, "no_text"))
            await context.bot.send_message(chat_id, t(update, "help"))
            log_usage(user_id, status="failure", reason="no_text", ocr_pages=ocr_pages,
                      file_type=file_type, file_size_kb=file_size_kb, duration_ms=ocr_ms,
                      cost_credits=cost_credits)
            return

        locale2, conf, coverage = ocr.locale2, ocr.confidence, ocr.coverage
        logger.info(f"User {user_id}: script={ocr.script_lang} lang={locale2} conf={conf:.2f} coverage={coverage:.2f}")
        context.user_data["ocr_job"] = {
            "text": normalized_text, "ocr_pages": ocr_pages, "ocr_ms": ocr_ms,
            "file_type": file_type, "file_size_kb": file_size_kb, "cost_credits": cost_credits,
            "segments": ocr.segments,
        }

        if default_lang in VOICE_MAP:
            info = VOICE_MAP[default_lang]
            await _safe_edit_text(status_message,
                t(update, "using_default").format(lang=f'{info["flag"]} {info["name"]}')
            )
            stop_typing.set()
            await synthesize_and_send(update, context, default_lang, status_message=None)
        elif ocr.segments:
            # Genuinely multilingual page: read every part with its own voice
            # instead of forcing one language (or sending the user to the menu).
            seg_locs = []
            for loc, _ in ocr.segments:
                if loc not in seg_locs:
                    seg_locs.append(loc)
            label = " + ".join(f'{VOICE_MAP[l]["flag"]} {VOICE_MAP[l]["name"]}'
                               for l in seg_locs if l in VOICE_MAP)
            await _safe_edit_text(status_message,t(update, "detected_lang").format(lang=label))
            stop_typing.set()
            await synthesize_and_send(update, context, locale2, status_message=None,
                                      use_segments=True)
        elif (locale2 in VOICE_MAP and conf >= AUTO_DETECT_MIN_CONFIDENCE
                and coverage >= AUTO_DETECT_MIN_COVERAGE):
            info = VOICE_MAP[locale2]
            await _safe_edit_text(status_message,
                t(update, "detected_lang").format(lang=f'{info["flag"]} {info["name"]}')
            )
            stop_typing.set()
            await synthesize_and_send(update, context, locale2, status_message=None)
        else:
            recent = [c for c in prefs.get("recent", "").split(",") if c]
            await _safe_edit_text(status_message,
                t(update, "choose_language"),
                reply_markup=build_language_keyboard(update, locale2, recent)
            )
    except Exception as e:
        logger.error(f"OCR/handle exception for user {user_id}: {e!r}")
        logger.error(traceback.format_exc())
        try:
            await _safe_edit_text(status_message,t(update, "generic_error"))
        except Exception:
            await context.bot.send_message(chat_id, t(update, "generic_error"))
        await context.bot.send_message(chat_id, t(update, "help"))
        log_usage(user_id, status="failure", reason="ocr_exception",
                  file_type=file_type, file_size_kb=file_size_kb,
                  duration_ms=round((time.monotonic() - t0) * 1000),
                  cost_credits=cost_credits)
    finally:
        stop_typing.set()
        typing_task.cancel()
        try:
            await typing_task
        except asyncio.CancelledError:
            pass
        remove_temp_file(file_path)


async def handle_file(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Entry point for document/photo uploads with pre-cost guard for heavy files."""
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    logger.info(f"User {user_id} sent a file or photo")
    file = update.message.document or update.message.photo[-1]
    mime_type = getattr(file, "mime_type", None)
    file_size = getattr(file, "file_size", None)
    file_type = classify_file_type(mime_type if mime_type else "image/jpeg")
    file_size_kb = round(file_size / 1024) if file_size else None
    file_id = getattr(file, "file_id", "")
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

    cost = _estimate_request_cost(file_type, file_size_kb)
    if cost >= PRECOST_CONFIRM_MIN_COST:
        context.user_data["pending_precost"] = {
            "file_id": file_id,
            "file_type": file_type,
            "file_size_kb": file_size_kb,
            "cost": cost,
        }
        log_growth_event(user_id, event_type="precost_prompt_shown", source=str(cost))
        await update.message.reply_text(
            t(update, "precost_prompt").format(cost=cost),
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton(t(update, "precost_continue_button"), callback_data="pre:ok"),
                InlineKeyboardButton(t(update, "precost_cancel_button"), callback_data="pre:cancel"),
            ]])
        )
        return

    ok, snap = _consume_quota(user_id, cost=cost)
    if not ok:
        await update.message.reply_text(t(update, "limit_reached"))
        await update.message.reply_text(
            t(
                update,
                "limits_status",
                free_left=snap["free_left"],
                free_total=snap["free_total"],
                bonus_left=snap["bonus_credits"],
                bonus_until=(snap["bonus_until"] or "-"),
            )
        )
        log_usage(user_id, status="failure", reason="quota_exceeded",
                  file_type=file_type, file_size_kb=file_size_kb, cost_credits=cost)
        return

    await _process_file_payload(update, context, file_id=file_id, file_type=file_type,
                                file_size_kb=file_size_kb, cost_credits=cost)


async def on_precost_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    action = (query.data or "").split(":", 1)[-1]
    pending = context.user_data.get("pending_precost")
    if not pending:
        await context.bot.send_message(update.effective_chat.id, t(update, "help"))
        return

    user_id = update.effective_user.id
    if action != "ok":
        log_growth_event(user_id, event_type="precost_cancel")
        context.user_data.pop("pending_precost", None)
        await context.bot.send_message(update.effective_chat.id, t(update, "precost_cancelled"))
        return

    cost = int(pending.get("cost") or 1)
    ok, snap = _consume_quota(user_id, cost=cost)
    if not ok:
        context.user_data.pop("pending_precost", None)
        await context.bot.send_message(update.effective_chat.id, t(update, "limit_reached"))
        await context.bot.send_message(
            update.effective_chat.id,
            t(
                update,
                "limits_status",
                free_left=snap["free_left"],
                free_total=snap["free_total"],
                bonus_left=snap["bonus_credits"],
                bonus_until=(snap["bonus_until"] or "-"),
            )
        )
        log_usage(user_id, status="failure", reason="quota_exceeded",
                  file_type=pending.get("file_type"), file_size_kb=pending.get("file_size_kb"),
                  cost_credits=cost)
        return

    log_growth_event(user_id, event_type="precost_confirm", source=str(cost))
    context.user_data.pop("pending_precost", None)
    await _process_file_payload(
        update,
        context,
        file_id=pending.get("file_id") or "",
        file_type=pending.get("file_type") or "other",
        file_size_kb=pending.get("file_size_kb"),
        cost_credits=cost,
    )

def prepare_tts_text(text: str, locale2: str) -> str:
    """Light, language-specific text cleanup before synthesis.

    Ukrainian only (ru/en already sound fine, so we don't touch them):
    - The OCR flattens м²→м2 / м³→м3, which the uk voice reads as "ем два"; we
      expand those to words.
    - The uk voice reads large numbers digit by digit ("один два п'ять нуль…")
      and ignores <say-as>, so we spell them out as words ("один мільйон двісті
      п'ятдесят тисяч") via num2words. Times/dates/codes/phones/years are left
      alone (only standalone integers of 5+ digits, or space-grouped millions).
    """
    if locale2 == "uk":
        text = re.sub(r'(?<=\d)\s*м\s*[²2](?![\d²³])', ' квадратних метрів', text)
        text = re.sub(r'(?<=\d)\s*м\s*[³3](?![\d²³])', ' кубічних метрів', text)
        text = _spell_large_numbers(text, "uk")
    return text


def _spell_large_numbers(text: str, lang: str) -> str:
    """Replace large integers (grouped "1 250 000" or plain "1250000") with their
    word form so a voice can't read them digit by digit. num2words is imported
    lazily and missing it degrades gracefully (number left as-is)."""
    try:
        from num2words import num2words
    except ImportError:
        return text

    def repl(m):
        try:
            return num2words(int(m.group(0).replace(' ', '')), lang=lang)
        except Exception:
            return m.group(0)

    text = re.sub(r'(?<!\d)\d{1,3}(?: \d{3}){2,}(?!\d)', repl, text)  # grouped millions
    text = re.sub(r'(?<![\d.:+\-])\d{5,}(?![\d.:\-])', repl, text)     # plain 5+ digits
    return text


def _voice_ssml_block(seg_text: str, seg_locale: str) -> str:
    """One <voice> element for a span, with language-specific text cleanup."""
    info = VOICE_MAP.get(seg_locale) or VOICE_MAP["en"]
    body = escape_ssml(prepare_tts_text(seg_text, seg_locale))
    return f'  <voice name="{info["voice"]}">\n    {body}\n  </voice>'


def synthesize_to_file(text: str, locale2: str, out_path: str, segments=None):
    """Synthesize `text` to an MP3 at out_path using the voice for locale2
    (English fallback). Returns the Azure SpeechSynthesisResult so callers can
    inspect result.reason / cancellation_details. No Telegram coupling, so it's
    reusable by the qa toolkit.

    `segments` (list of (locale2, text)) renders a multilingual page with one
    voice per span; when None, the whole text is read with the locale2 voice."""
    dom = VOICE_MAP.get(locale2) or VOICE_MAP["en"]
    if segments and len(segments) > 1:
        voices = "\n".join(_voice_ssml_block(seg_text, seg_loc)
                           for seg_loc, seg_text in segments)
    else:
        voices = _voice_ssml_block(text, locale2)
    ssml = f"""
<speak version="1.0" xmlns="http://www.w3.org/2001/10/synthesis" xml:lang="{dom['lang_code']}">
{voices}
</speak>
"""
    synthesizer = SpeechSynthesizer(
        speech_config=speech_config, audio_config=AudioConfig(filename=out_path)
    )
    result = synthesizer.speak_ssml_async(ssml).get()
    del synthesizer
    return result


async def synthesize_and_send(update: Update, context: ContextTypes.DEFAULT_TYPE,
                              locale2: str, status_message=None,
                              use_segments: bool = False) -> None:
    """Synthesize the stored OCR text into a voice message in the chosen language.
    Shared by the auto-detect path and the manual inline-picker callback.

    use_segments reads a multilingual page with one voice per language span (auto
    path only); a manually-picked or pinned language always reads in one voice."""
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    job = context.user_data.get("ocr_job")
    if not job:
        # Stale callback (e.g. after a scale-to-zero restart) — nothing to synthesize.
        await context.bot.send_message(chat_id, t(update, "help"))
        return

    info = VOICE_MAP.get(locale2) or VOICE_MAP["en"]
    lang_label = f'{info["flag"]} {info["name"]}'
    normalized_text = job["text"]
    ocr_pages = job.get("ocr_pages")
    ocr_ms = job.get("ocr_ms", 0)
    file_type = job.get("file_type")
    file_size_kb = job.get("file_size_kb")
    cost_credits = job.get("cost_credits")

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
                await _safe_edit_text(status_message,t(update, "generating_audio").format(lang=lang_label))
            except Exception:
                status_message = None
        if status_message is None:
            status_message = await context.bot.send_message(
                chat_id, t(update, "generating_audio").format(lang=lang_label)
            )

        audio_path = f"{tempfile.mktemp()}.mp3"
        segments = job.get("segments") if use_segments else None
        result = synthesize_to_file(normalized_text, locale2, audio_path, segments=segments)

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
                      duration_ms=elapsed_ms(), cost_credits=cost_credits)
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
                  duration_ms=elapsed_ms(), cost_credits=cost_credits)

    except Exception as e:
        logger.error(f"Exception for user {user_id}: {e!r}")
        logger.error(traceback.format_exc())
        await context.bot.send_message(chat_id, t(update, "generic_error"))
        await context.bot.send_message(chat_id, t(update, "help"))
        log_usage(user_id, status="failure", reason="exception", language=info["name"],
                  ocr_pages=ocr_pages, file_type=file_type, file_size_kb=file_size_kb,
                  duration_ms=elapsed_ms(), cost_credits=cost_credits)
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


async def on_onboarding_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    action = (query.data or "").split(":", 1)[-1]

    if action == "start":
        await context.bot.send_message(update.effective_chat.id, t(update, "onboarding_started"))
        await context.bot.send_message(update.effective_chat.id, t(update, "help"))
        return

    if action == "support":
        log_growth_event(update.effective_user.id, event_type="support_view", source="onboarding")
        await context.bot.send_message(
            update.effective_chat.id,
            t(update, "support_menu"),
            reply_markup=build_support_keyboard(update),
        )
        return

    if action == "listen":
        locale2 = resolve_ui_lang(update)
        if locale2 not in VOICE_MAP:
            locale2 = "en"
        audio_path = f"{tempfile.mktemp()}.mp3"
        ogg_path = f"{tempfile.mktemp()}.ogg"
        try:
            await asyncio.to_thread(synthesize_to_file, t(update, "mission_short_audio"), locale2, audio_path)
            await asyncio.to_thread(convert_mp3_to_ogg, audio_path, ogg_path)
            with open(ogg_path, "rb") as voice_file:
                await context.bot.send_voice(chat_id=update.effective_chat.id, voice=voice_file)
        except Exception as ex:
            logger.warning(f"mission audio failed: {ex!r}")
            await context.bot.send_message(update.effective_chat.id, t(update, "mission_audio_error"))
        finally:
            remove_temp_file(audio_path)
            remove_temp_file(ogg_path)


async def on_support_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    data = query.data or ""
    parts = data.split(":")
    if len(parts) < 2 or parts[0] != "sup":
        return

    user_id = update.effective_user.id
    if parts[1] == "custom":
        log_growth_event(user_id, event_type="support_custom_click")
        await context.bot.send_message(update.effective_chat.id, t(update, "support_custom"))
        return

    if len(parts) == 3 and parts[1] == "pack":
        pack_key = parts[2]
        pack = SUPPORT_PACKS.get(pack_key)
        if not pack:
            return
        ui_lang = resolve_ui_lang(update)
        log_growth_event(user_id, event_type="support_pack_click", source=pack_key)
        if SUPPORT_PAYMENT_MODE == "admin_stub":
            log_growth_event(user_id, event_type="support_request_created", source=pack_key)
            await context.bot.send_message(update.effective_chat.id, t(update, "support_payment_pending"))
            admin_text = (
                f"Support approval requested\n"
                f"User: {user_id}\n"
                f"Pack: {pack_key}\n"
                f"Bonus: +{pack['bonus']} / {pack['days']} days"
            )
            for aid in ADMIN_USER_IDS:
                try:
                    await context.bot.send_message(
                        chat_id=int(aid),
                        text=admin_text,
                        reply_markup=_build_support_admin_keyboard(user_id, pack_key, ui_lang),
                    )
                except Exception as ex:
                    logger.warning(f"support admin notify failed ({aid}): {ex!r}")
            return

        snap = _grant_bonus_credits(user_id, bonus=pack["bonus"], days=pack["days"])
        log_growth_event(user_id, event_type="support_bonus_granted", source=pack_key)
        await context.bot.send_message(
            update.effective_chat.id,
            t(update, "support_bonus_activated").format(
                bonus=pack["bonus"],
                bonus_until=(snap["bonus_until"] or "-"),
            ),
        )
        await context.bot.send_message(
            update.effective_chat.id,
            t(
                update,
                "limits_status",
                free_left=snap["free_left"],
                free_total=snap["free_total"],
                bonus_left=snap["bonus_credits"],
                bonus_until=(snap["bonus_until"] or "-"),
            ),
        )


async def on_support_admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    if not _is_admin(update):
        await context.bot.send_message(update.effective_chat.id, t(update, "help"))
        return
    parts = (query.data or "").split(":")
    if len(parts) != 5 or parts[0] != "supadm":
        return
    action, user_id_str, pack_key, ui_lang = parts[1], parts[2], parts[3], parts[4]
    pack = SUPPORT_PACKS.get(pack_key)
    if not pack:
        return
    try:
        target_user_id = int(user_id_str)
    except ValueError:
        return

    if action == "approve":
        snap = _grant_bonus_credits(target_user_id, bonus=pack["bonus"], days=pack["days"])
        log_growth_event(target_user_id, event_type="support_bonus_granted", source=f"admin:{pack_key}")
        await context.bot.send_message(
            target_user_id,
            t_lang(ui_lang, "support_bonus_activated").format(
                bonus=pack["bonus"],
                bonus_until=(snap["bonus_until"] or "-"),
            )
        )
        await context.bot.send_message(
            target_user_id,
            t_lang(
                ui_lang,
                "limits_status",
                free_left=snap["free_left"],
                free_total=snap["free_total"],
                bonus_left=snap["bonus_credits"],
                bonus_until=(snap["bonus_until"] or "-"),
            )
        )
        await query.edit_message_text(f"Approved: user {target_user_id}, pack {pack_key}.")
        return

    if action == "reject":
        log_growth_event(target_user_id, event_type="support_request_rejected", source=f"admin:{pack_key}")
        await context.bot.send_message(target_user_id, t_lang(ui_lang, "support_request_rejected"))
        await query.edit_message_text(f"Rejected: user {target_user_id}, pack {pack_key}.")


def _build_unlimited_list(ui_chrome: bool = True):
    """Return (text, keyboard) listing store-granted unlimited users with revoke buttons.

    Note: IDs granted via the ADMIN_USER_IDS / UNLIMITED_USER_IDS env vars are also
    unlimited but are not listed here — they're managed by deployment config, not buttons.
    """
    try:
        ids = list(user_store.list_unlimited_users())
    except Exception as ex:
        logger.warning(f"list_unlimited_users failed: {ex!r}")
        ids = []
    rows = [[InlineKeyboardButton(f"🚫 Revoke {uid}", callback_data=f"unlim:revoke:{uid}")]
            for uid in ids]
    if ids:
        text = "♾ Unlimited users (tap to revoke):\n" + "\n".join(f"• {u}" for u in ids)
    else:
        text = "♾ No unlimited users granted yet."
    if ui_chrome:
        text += "\n\nGrant access with:  /unlimited <user_id>"
    return text, (InlineKeyboardMarkup(rows) if rows else None)


async def unlimited_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Admin-only: grant/list unlimited (no-limit) access. /unlimited <id> grants;
    /unlimited alone lists current grants with inline revoke buttons."""
    if not _is_admin(update):
        await update.message.reply_text(t(update, "help"))
        return
    arg = context.args[0].strip() if context.args else ""
    if arg:
        if not arg.isdigit():
            await update.message.reply_text("Usage: /unlimited <numeric user_id>")
            return
        try:
            user_store.set_unlimited(int(arg), True)
        except Exception as ex:
            logger.warning(f"set_unlimited grant failed: {ex!r}")
            await update.message.reply_text("⚠️ Failed to grant, try again.")
            return
        log_growth_event(int(arg), event_type="unlimited_granted", source="admin")
        await update.message.reply_text(
            f"✅ Unlimited access granted to {arg}.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton(f"🚫 Revoke {arg}", callback_data=f"unlim:revoke:{arg}")
            ]]),
        )
        return
    text, kb = _build_unlimited_list()
    await update.message.reply_text(text, reply_markup=kb)


async def on_unlimited_admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    if not _is_admin(update):
        await context.bot.send_message(update.effective_chat.id, t(update, "help"))
        return
    parts = (query.data or "").split(":")
    if len(parts) != 3 or parts[0] != "unlim" or not parts[2].isdigit():
        return
    action, uid = parts[1], parts[2]
    on = (action == "grant")
    try:
        user_store.set_unlimited(int(uid), on)
    except Exception as ex:
        logger.warning(f"set_unlimited toggle failed: {ex!r}")
        await query.answer("Failed, try again.", show_alert=True)
        return
    log_growth_event(int(uid), event_type=("unlimited_granted" if on else "unlimited_revoked"),
                     source="admin")
    text, kb = _build_unlimited_list()
    await query.edit_message_text(text, reply_markup=kb)


async def feedback_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = " ".join(context.args).strip() if context.args else ""
    if text:
        await _save_feedback(update, context, text)
    else:
        try:
            user_store.set_awaiting_feedback(update.effective_user.id, True)
        except Exception as ex:
            logger.warning(f"set_awaiting_feedback failed: {ex!r}")
        await update.message.reply_text(t(update, "feedback_prompt"))


async def _save_feedback(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    user = update.effective_user
    ui_lang = resolve_ui_lang(update)
    try:
        user_store.add_feedback(user.id, user.username or user.full_name, ui_lang, text[:4000])
    except Exception as ex:
        logger.warning(f"store feedback failed: {ex!r}")
    log_feedback(user.id, ui_lang, text)
    logger.info(f"Feedback received from user {user.id}")
    await update.message.reply_text(t(update, "feedback_thanks"))


async def on_text_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Capture feedback when it's awaited; otherwise gently nudge with the help text.

    The "awaiting" flag lives in storage (not in-memory user_data) so it survives
    scale-to-zero and webhook routing to a different replica between the /feedback
    prompt and the user's reply.
    """
    user_id = update.effective_user.id
    awaiting = False
    try:
        aw = int(user_store.get_user(user_id).get("awaiting_fb") or 0)
        awaiting = aw > 0 and (int(time.time()) - aw) < FEEDBACK_WAIT_WINDOW_SEC
    except Exception as ex:
        logger.warning(f"awaiting_fb check failed: {ex!r}")
    if awaiting:
        text = (update.message.text or "").strip()
        if text:
            try:
                user_store.set_awaiting_feedback(user_id, False)
            except Exception as ex:
                logger.warning(f"clear awaiting_fb failed: {ex!r}")
            await _save_feedback(update, context, text)
            return
    # Also clears the legacy 1/2/3 language reply-keyboard if the user still has it
    # (tapping those stale buttons sends plain text and lands here).
    await update.message.reply_text(t(update, "help"), reply_markup=ReplyKeyboardRemove())


async def feedback_recent_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Owner-only: show the latest feedback. Enabled by the ADMIN_USER_IDS env var."""
    if not _is_admin(update):
        await update.message.reply_text(t(update, "help"))
        return
    items = user_store.list_recent_feedback(10)
    if not items:
        await update.message.reply_text("No feedback yet.")
        return
    lines = [
        f"• [{e.get('ui_lang', '?')}] @{e.get('username', '')} ({e.get('user_id', '')}): {e.get('text', '')}"
        for e in items
    ]
    await update.message.reply_text(("🗒 Recent feedback:\n\n" + "\n\n".join(lines))[:4000])


async def feedback_stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Owner-only: aggregate counts over stored feedback (no LLM, instant)."""
    if not _is_admin(update):
        await update.message.reply_text(t(update, "help"))
        return
    items = user_store.list_recent_feedback(500)
    if not items:
        await update.message.reply_text("No feedback yet.")
        return
    by_lang = defaultdict(int)
    by_day = defaultdict(int)
    users = set()
    for e in items:
        users.add(str(e.get("user_id", "")))
        by_lang[e.get("ui_lang") or "?"] += 1
        created = e.get("created")
        if created:
            try:
                day = time.strftime("%Y-%m-%d", time.gmtime(int(created) / 1000))
                by_day[day] += 1
            except (ValueError, TypeError, OSError):
                pass
    lang_line = ", ".join(f"{k}:{v}" for k, v in sorted(by_lang.items(), key=lambda x: -x[1]))
    day_lines = "\n".join(f"  {d}: {c}" for d, c in sorted(by_day.items(), reverse=True)[:7])
    msg = (
        f"📊 Feedback stats\n\n"
        f"Total: {len(items)}\n"
        f"Unique users: {len(users)}\n\n"
        f"By UI language: {lang_line}\n\n"
        f"By day (last 7):\n{day_lines or '  —'}"
    )
    await update.message.reply_text(msg[:4000])


async def feedback_digest_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Owner-only: cluster feedback into prioritized improvement tasks via Claude."""
    if not _is_admin(update):
        await update.message.reply_text(t(update, "help"))
        return
    items = user_store.list_recent_feedback(300)
    await update.message.reply_text("⏳ Готовлю разбор фидбека…")
    import feedback_ai
    try:
        digest = await asyncio.to_thread(feedback_ai.generate_digest, items)
    except Exception as ex:
        logger.warning(f"feedback_digest failed: {ex!r}")
        digest = None
    if digest is None:
        await update.message.reply_text(
            "⚠️ Разбор недоступен: не настроен ANTHROPIC_API_KEY (или сбой API). "
            "Сырой фидбек смотри через /feedback_recent."
        )
        return
    for i in range(0, len(digest), 4000):
        await update.message.reply_text(digest[i:i + 4000])

# --- Main entrypoint ---
async def _set_bot_descriptions(bot) -> None:
    """Set the localized bot profile description (shown before a user taps Start)
    and the short description, for each supported UI language."""
    try:
        await bot.set_my_description(MESSAGES["en"]["bot_description"])
        await bot.set_my_short_description(MESSAGES["en"]["bot_short_description"])
        for lang in MESSAGES:
            await bot.set_my_description(MESSAGES[lang]["bot_description"], language_code=lang)
            await bot.set_my_short_description(MESSAGES[lang]["bot_short_description"], language_code=lang)
    except Exception as ex:
        logger.warning(f"set_my_description failed: {ex!r}")


async def _post_init(application) -> None:
    """Register the slash-command menu so /language is discoverable, and set the
    bot descriptions in the background (they aren't needed to serve requests, so
    we don't block cold-start readiness on them)."""
    from telegram import BotCommand, BotCommandScopeChat
    public_cmds = [
        BotCommand("start", "Start / how it works"),
        BotCommand("help", "How to use the bot"),
        BotCommand("limits", "Show today's free and bonus limits"),
        BotCommand("donate", "Donate to get bonus requests"),
        BotCommand("language", "Set audio language (or auto-detect)"),
        BotCommand("feedback", "Send feedback / report an issue"),
    ]
    try:
        await application.bot.set_my_commands(public_cmds)
    except Exception as ex:
        logger.warning(f"set_my_commands failed: {ex!r}")
    # Admins also see the owner-only commands in their personal menu (scoped by chat),
    # so they're discoverable without exposing them to regular users.
    admin_cmds = public_cmds + [
        BotCommand("unlimited", "Admin: grant/list unlimited access"),
        BotCommand("feedback_recent", "Admin: last 10 feedback"),
        BotCommand("feedback_stats", "Admin: feedback stats"),
        BotCommand("feedback_digest", "Admin: AI improvement digest"),
    ]
    for uid in ADMIN_USER_IDS:
        try:
            await application.bot.set_my_commands(
                admin_cmds, scope=BotCommandScopeChat(chat_id=int(uid)))
        except Exception as ex:
            logger.warning(f"set_my_commands (admin {uid}) failed: {ex!r}")
    asyncio.create_task(_set_bot_descriptions(application.bot))


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
    # Runs before everything (group=-1): drop duplicate webhook re-deliveries.
    app.add_handler(TypeHandler(Update, _dedupe_update), group=-1)
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("limits", limits_command))
    app.add_handler(CommandHandler("donate", support_command))
    app.add_handler(CommandHandler("language", language_command))
    app.add_handler(CommandHandler("feedback", feedback_command))
    app.add_handler(CommandHandler("unlimited", unlimited_command))
    app.add_handler(CommandHandler("feedback_recent", feedback_recent_command))
    app.add_handler(CommandHandler("feedback_stats", feedback_stats_command))
    app.add_handler(CommandHandler("feedback_digest", feedback_digest_command))
    app.add_handler(MessageHandler(filters.Document.ALL | filters.PHOTO, handle_file))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text_message))
    app.add_handler(CallbackQueryHandler(on_onboarding_callback, pattern=r"^onb:"))
    app.add_handler(CallbackQueryHandler(on_precost_callback, pattern=r"^pre:"))
    app.add_handler(CallbackQueryHandler(on_support_callback, pattern=r"^sup:"))
    app.add_handler(CallbackQueryHandler(on_support_admin_callback, pattern=r"^supadm:"))
    app.add_handler(CallbackQueryHandler(on_unlimited_admin_callback, pattern=r"^unlim:"))
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