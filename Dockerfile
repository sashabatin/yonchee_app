FROM python:3.11-slim

WORKDIR /app

# System deps: ffmpeg (audio), tesseract + Georgian/Armenian data and poppler
# (fallback OCR for scripts Azure Read can't extract).
RUN apt-get update && apt-get install -y \
        ffmpeg \
        tesseract-ocr tesseract-ocr-kat tesseract-ocr-hye \
        poppler-utils \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8000

CMD ["python", "app.py"]