FROM python:3.11-slim

WORKDIR /app

# System deps: ffmpeg for audio encoding. OCR for scripts Azure Read can't
# extract (Georgian/Armenian) now goes through Azure OpenAI vision, and PDFs
# are rasterized in-process via PyMuPDF — no tesseract/poppler needed.
RUN apt-get update && apt-get install -y \
        ffmpeg \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8000

CMD ["python", "app.py"]