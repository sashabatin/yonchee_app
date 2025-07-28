FROM python:3.11-slim

WORKDIR /app

# Install system dependencies for ffmpeg
RUN apt-get update && apt-get install -y ffmpeg && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# --- NEW: Add ARG and flexible .env copy step
ARG ENV_FILE=.env
COPY ${ENV_FILE} /app/.env

CMD ["python", "app.py"]