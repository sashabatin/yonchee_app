# Yonchee — Стратегия масштабирования и оптимизации затрат

> Документ составлен на основе анализа текущей архитектуры и целевых сценариев нагрузки.
> Горизонт планирования: 6–18 месяцев.

---

## 1. Текущее состояние (baseline)

### Стек
| Компонент | Текущее решение | Регион |
|---|---|---|
| OCR | Azure Document Intelligence `prebuilt-read` | northeurope |
| TTS | Azure Cognitive Services Speech (Neural) | northeurope |
| Хранилище пользователей | Azure Table Storage | northeurope |
| Compute | Azure Container Apps (0.25 vCPU / 0.5 GiB, min=0 max=2) | northeurope |
| Телеметрия | Azure Application Insights | northeurope |
| Деплой | GitHub Actions → ACR → Container Apps | — |

### Архитектурная особенность, критичная для роста
Весь pipeline (OCR → lang detect → TTS → Telegram) выполняется **синхронно внутри Telegram message handler-а**. При небольшой аудитории это нормально, но становится узким местом при конкуренции >20–30 одновременных пользователей.

---

## 2. Текущая структура затрат (оценка на единицу)

### Стоимость одного запроса (1 файл → голосовое сообщение)

| Сервис | Оценка | Комментарий |
|---|---|---|
| Azure DI OCR | ~$0.0015/стр. | S0: $1.50/1000 страниц |
| Azure Neural TTS | ~$0.032 | 2000 символов × $16/1M символов |
| Container compute | ~$0.0001 | при scale-to-zero очень дешево |
| Table Storage | negligible | ~$0/запрос |
| **Итого** | **~$0.034/запрос** | |

### Проекция по сценариям роста

| DAU | Запросы/день (×2 на пользователя) | Затраты/месяц (TTS dominates) |
|---|---|---|
| 1 000 | 2 000 | ~**$2 000** |
| 10 000 | 20 000 | ~**$20 000** |
| 100 000 | 200 000 | ~**$200 000** |

> **Вывод**: Azure Neural TTS — основной cost driver. При 10K DAU затраты уже около $20K/мес, что делает проект финансово неустойчивым без серьёзной оптимизации.

---

## 3. Критические узкие места при глобальном запуске

### 3.1 TTS — главный «пожиратель бюджета»
Azure Neural TTS стоит **$16/1M символов**. При среднем документе в 2000 символов это $0.032 за запрос — в 20x дороже OCR.

### 3.2 Один регион northeurope
- Латентность для пользователей из Азии, Латинской Америки, США: +100–300 мс только на сетевой hop
- Одна точка отказа
- При росте нагрузки — одна очередь обработки

### 3.3 Синхронный pipeline
```
Telegram update → OCR (1–5 сек) → TTS (1–3 сек) → Telegram sendVoice
```
При 50 одновременных пользователях и max-replicas=2 очередь будет копиться. Нужен async queue.

### 3.4 Нет кеширования
Если два пользователя отправляют один и тот же документ — OCR и TTS выполняются дважды.

### 3.5 Telegram API limits
Telegram не ограничивает ботов жёстко, но при высокой нагрузке важна политика retry и flood wait. Сейчас это не управляется явно.

---

## 4. Стратегия оптимизации затрат

### Приоритет 1 — TTS (экономия до 10x)

**Вариант A: Переход на `edge-tts`** *(самый дешевый, нулевые API-затраты)*

```
pip install edge-tts
```
- Использует тот же TTS-движок Microsoft Edge (SSML-совместимый)
- Те же Neural-голоса (Aria, Polina, Dmitry и т.д.)
- **Стоимость: $0** (неофициальный, без SLA)
- Риск: нет SLA, API может измениться
- Рекомендуется для: начального роста до 50K пользователей, где стоимость критична

**Вариант B: Google Cloud TTS Standard** *(официальный, 4× дешевле Azure)*
- Standard voices: **$4/1M символов** (vs $16 у Azure Neural)
- Качество ниже Neural, но приемлемо для большинства языков
- Все нужные языки покрыты
- Для масштаба: при 10K DAU → ~$5K/мес вместо $20K

**Вариант C: Самохостинг Kokoro TTS / XTTS-v2** *(нулевые маргинальные затраты)*
- Kokoro (нейросетевая, open-source, ~82M параметров)
- Поддерживает en/fr/de/es/pt/ja/ko/zh из коробки, другие через finetune
- Один A100 GPU ($1.50/час на Azure NC): обслуживает 100+ RPS
- При нагрузке 10K DAU ~500 запросов/час: 1 GPU A100 = ~$1 080/мес всего
- Критический минус: не покрывает uk, ru, ka, hy, kk — придётся держать Azure Speech как fallback

**Рекомендуемая стратегия TTS:**

```
0 → 5K DAU:    Azure Neural Speech (текущее)
5K → 30K DAU:  edge-tts PRIMARY, Azure Neural fallback для ka/hy
30K+ DAU:      Kokoro/XTTS для en/es/de/fr/pl + Azure Speech для ua/ru/ka
```

---

### Приоритет 2 — Кеширование OCR и TTS результатов

Документы повторяются (учебники, книги, стандартные формы). Кеш даже на 20% hit rate экономит 20% API-затрат.

```
hash(file_bytes) → { ocr_text, lang, audio_blob_url }
TTL: 7 дней для текста, 30 дней для аудио
Хранилище: Azure Blob Storage (дешево: ~$0.018/GB/мес)
```

Реализация:
- Добавить `file_hash` (SHA-256 первых 64KB) перед OCR
- Azure Table Storage: `PartitionKey=hash[:2], RowKey=hash` → `ocr_text, detected_lang, audio_blob`
- Azure Blob Storage bucket `tts-cache` для .ogg файлов
- Срок хранения: 7–30 дней

---

### Приоритет 3 — Снижение единичной стоимости OCR

| Вариант | Стоимость | Плюсы | Минусы |
|---|---|---|---|
| Azure DI prebuilt-read (текущее) | $1.50/1000 стр. | Лучшее качество (подтверждено QA) | Дорого |
| Azure DI Free F0 | 0 (500 стр/мес) | Бесплатно | Жёсткий cap |
| Google Cloud Document AI | $1.50/1000 стр. | Аналогичное | Нет преимущества |
| Tesseract (self-hosted) | $0 | Бесплатно | CER ~5× хуже на деградированных |
| Azure Computer Vision OCR | $1.00/1000 транз. | Чуть дешевле | Хуже DI на сложных docs |

> Вывод: для простых чистых изображений можно добавить pre-screening — если image quality score высокий, отправлять в Tesseract; только низкое качество → Azure DI. Ожидаемая экономия: 30–50% OCR-затрат.

---

## 5. Архитектура для глобального запуска

### Фаза 1: 0 → 10K пользователей (текущий приоритет)

**Изменения compute минимальны.** Фокус на cost:

```
[Сейчас]
Telegram → Container Apps (northeurope) → Azure DI + Azure Speech

[Фаза 1]  
Telegram → Container Apps (northeurope) → Azure DI + edge-tts PRIMARY
                                         → Azure Speech FALLBACK (ka/hy/kk)
                                         + SHA-256 OCR/TTS cache (Azure Blob)
```

Действия:
1. Внедрить `edge-tts` как primary TTS provider с fallback на Azure Speech
2. Добавить file-hash кеш (Azure Blob + Table)
3. Поднять `max-replicas` до 5–10 для пиковой нагрузки
4. Настроить autoscale по HTTP requests в Container Apps

---

### Фаза 2: 10K → 100K пользователей

**Async queue архитектура:**

```
Telegram Webhook
     │
     ▼
Container Apps "bot-gateway" (легкий: принять update, enqueue, ответить "обрабатываю...")
     │
     ▼
Azure Storage Queue / Service Bus
     │
     ▼
Container Apps "workers" (autoscale 0→N по длине очереди)
     │
     ├─→ Azure DI OCR
     ├─→ TTS (edge-tts / Kokoro)
     └─→ Azure Blob cache
     │
     ▼
Telegram Bot API (sendVoice)
```

**Мульти-регион:**

| Регион | Целевая аудитория | Компоненты |
|---|---|---|
| `northeurope` (уже есть) | Европа, Украина, Россия | Bot gateway + workers + DI + Speech |
| `eastus` | Северная Америка | Bot gateway + workers + DI + Speech |
| `southeastasia` | Азия, Ближний Восток | Bot gateway + workers + DI + Speech |

Маршрутизация: по `user_id % num_regions` (детерминированная, без geo-lookup) — у Telegram нет IP-роутинга от сервера.

**Azure Table Storage → Cosmos DB:**
- При 100K пользователях Table Storage начинает давать latency spikes
- Cosmos DB с 400 RU/s (~$23/мес) покрывает ~400 операций/сек
- Один аккаунт, мульти-регионная репликация

---

### Фаза 3: 100K+ пользователей

```
┌─────────────────────────────────────────────────────┐
│                  Azure Front Door                    │
│         (WAF + global load balancing + CDN)          │
└────────────┬───────────────────────┬────────────────┘
             │                       │
    ┌────────▼───────┐     ┌─────────▼──────┐
    │  Region EU     │     │  Region US/Asia │
    │  (northeurope) │     │  (eastus / SEA) │
    └────────┬───────┘     └─────────┬───────┘
             │                       │
    ┌────────▼───────┐     ┌─────────▼──────┐
    │ AKS cluster    │     │ AKS cluster    │
    │ bot-gateway    │     │ bot-gateway    │
    │ ocr-workers    │     │ ocr-workers    │
    │ tts-workers    │     │ tts-workers    │
    └────────┬───────┘     └─────────┬──────┘
             │                       │
    ┌────────▼───────────────────────▼──────┐
    │          Cosmos DB (multi-region)      │
    │          Redis Cache (session)         │
    │          Azure Blob (audio cache)      │
    └────────────────────────────────────────┘
```

- **AKS** вместо Container Apps: больше контроля над GPU-нодами для self-hosted TTS
- **Azure Front Door**: WAF защита + geo-routing + CDN для статики
- **Redis** (Azure Cache for Redis Basic tier ~$17/мес): кеш сессий, quota в памяти (не Table Storage per request)
- **Kokoro TTS** на GPU-нодах AKS: покрывает 80% трафика, Azure Speech только для экзотических языков

---

## 6. CDN — где применимо

Telegram-бот не обслуживает HTTP-запросы от клиентов напрямую, поэтому классический CDN (Cloudflare, Azure CDN) **не применим к основному пайплайну**.

Где CDN полезен:
1. **Кеш аудиофайлов**: если добавить веб-плеер (Landing Page) — CDN раздаёт OGG/MP3 файлы
2. **Webhook endpoint**: Azure Front Door перед Container Apps снимает DDoS нагрузку
3. **Статический сайт** (yonchee.com или бот-лендинг): Vercel/Cloudflare Pages бесплатно

---

## 7. Платёжная интеграция для монетизации при росте

Текущий `SUPPORT_PAYMENT_MODE=admin_stub` — заглушка. При глобальном запуске:

| Способ | Регион | Комиссия | Сложность |
|---|---|---|---|
| Telegram Stars (нативный) | Global | ~30% | Низкая (уже задизайнен UI) |
| Stripe | EU/US | 1.4–2.9% | Средняя |
| LiqPay / Fondy | UA | 2% | Средняя |
| PayPal | Global | 3.5–5% | Средняя |

> Telegram Stars — **приоритет**: нулевая интеграционная сложность, пользователь не покидает Telegram, подходит глобально. Комиссия высокая, но устраняет весь платёжный compliance overhead.

---

## 8. Дорожная карта (приоритизация)

### Сейчас (до 5K пользователей)
- [ ] **[HIGH]** Заменить Azure Neural TTS на `edge-tts` как primary (ka/hy/kk → Azure fallback)
- [ ] **[HIGH]** SHA-256 file cache: пропускать OCR+TTS для уже обработанных документов
- [ ] **[MED]** Поднять `max-replicas` до 8, настроить HTTP scaling rule в Container Apps

### Ближайшие 3 месяца (5K → 30K)
- [ ] Async queue (Azure Storage Queue + worker replicas)
- [ ] Redis для quota/session (убрать Table Storage per-request reads)
- [ ] Telegram Stars payment integration
- [ ] Базовый мониторинг unit-economics (cost per request via App Insights)

### 6–12 месяцев (30K → 100K+)
- [ ] Второй регион (eastus или southeastasia в зависимости от реального географического распределения пользователей из Application Insights)
- [ ] Cosmos DB вместо Table Storage
- [ ] Kokoro/XTTS self-hosted TTS на GPU для en/es/de/fr/pl (покрывает ~60% трафика)
- [ ] Azure Front Door + WAF

---

## 9. Оценка ROI основных оптимизаций

| Оптимизация | Сложность | Экономия при 10K DAU |
|---|---|---|
| edge-tts вместо Azure Neural | Низкая (1–2 дня) | ~$18K/мес |
| File hash OCR cache (20% hit) | Средняя (3–5 дней) | ~$600/мес OCR + $3.6K TTS |
| Redis quota cache | Средняя (2–3 дня) | latency, не $$ |
| Async queue | Высокая (1–2 недели) | UX, not $$ напрямую |
| Self-hosted Kokoro TTS | Высокая (2–3 недели) | ~$15K/мес при 10K DAU |

> **Quick win с наибольшим ROI: `edge-tts`**. Два дня работы → экономия потенциально $18K/мес при 10K DAU.

---

## 10. Риски и mitigation

| Риск | Вероятность | Mitigation |
|---|---|---|
| edge-tts API изменится/закроется | Средняя | Azure Speech остаётся как fallback, миграция обратно — 1 день |
| Azure DI pricing вырастет | Низкая | Абстракция `extract_text()` уже готова для смены провайдера |
| Telegram bot API лимиты при 100K+ | Средняя | Нужен retry с exponential backoff, flood wait handling |
| Один регион упадёт | Низкая сейчас | При 2+ регионах: DNS failover через Azure Traffic Manager |
| GDPR/данные пользователей в ЕС | Актуально для EU-пользователей | Хранение данных в northeurope уже ОК для EU; для US трафика — проверить |

---

*Документ версии 1.0 — июнь 2026. Обновлять по мере изменения unit-economics и реальных данных из Application Insights.*
