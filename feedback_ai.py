"""Turn raw user feedback into a prioritized, actionable improvement digest via Claude.

Shared by the bot's /feedback_digest command and the local tools/feedback_digest.py
script, so the prompt lives in exactly one place. Pure and dependency-light: the
anthropic SDK is imported lazily, so importing this module never fails even when the
SDK or API key is absent (callers get None and degrade gracefully).
"""
import os
import logging

logger = logging.getLogger(__name__)

# Sonnet is the sweet spot for this clustering/summarization job (strong, cheap).
DIGEST_MODEL = os.environ.get("FEEDBACK_DIGEST_MODEL", "claude-sonnet-4-6")
DIGEST_MAX_ITEMS = 300

SYSTEM_PROMPT = (
    "You are a product analyst for a Telegram bot that converts photos and PDFs of text "
    "into spoken audio (OCR -> text-to-speech). Its users are mainly visually impaired "
    "people who rely on screen readers. You receive raw user-feedback messages. "
    "Cluster them into themes, then produce a concise, prioritized list of concrete "
    "improvement tasks an engineer can act on directly. For each theme state how many "
    "users mentioned it, and tag the type: [БАГ] (bug), [ФИЧА] (feature request), or "
    "[UX]. Separate clearly actionable items from vague sentiment. If the feedback is "
    "sparse or low-signal, say so honestly instead of inventing tasks. "
    "Respond in Russian (the maintainer reads Russian). Use GitHub-flavored Markdown."
)


def _format_items(items):
    lines = []
    for e in items[:DIGEST_MAX_ITEMS]:
        lang = e.get("ui_lang") or "?"
        txt = (e.get("text") or "").strip().replace("\n", " ")
        if txt:
            lines.append(f"- [{lang}] {txt}")
    return "\n".join(lines)


def generate_digest(items, api_key=None, model=None):
    """Return a Markdown digest string, or None if the digest can't be produced
    (no API key / SDK not installed / API error). Empty feedback returns a friendly note."""
    api_key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        logger.info("feedback_ai: ANTHROPIC_API_KEY not set — digest unavailable.")
        return None
    items = list(items or [])
    if not items:
        return "Фидбека пока нет — анализировать нечего."
    try:
        from anthropic import Anthropic
    except ImportError:
        logger.warning("feedback_ai: anthropic package not installed — digest unavailable.")
        return None

    user_content = (
        f"Вот {len(items)} сообщений обратной связи от пользователей бота "
        "(в квадратных скобках — язык интерфейса пользователя):\n\n"
        f"{_format_items(items)}\n\n"
        "Сгруппируй по темам и выдай приоритизированный список конкретных задач "
        "по улучшению. Для каждой темы укажи, сколько пользователей её затронули, "
        "и помечай тип: [БАГ] / [ФИЧА] / [UX]. В конце — короткий блок «Что делать "
        "в первую очередь» с топ-3 задачами."
    )
    try:
        client = Anthropic(api_key=api_key)
        msg = client.messages.create(
            model=model or DIGEST_MODEL,
            max_tokens=2000,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_content}],
        )
        return "".join(b.text for b in msg.content if getattr(b, "type", None) == "text").strip()
    except Exception as ex:
        logger.warning(f"feedback_ai: digest generation failed: {ex!r}")
        return None
