FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1     PYTHONUNBUFFERED=1     TZ=Europe/Istanbul

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

# Tek worker: scheduler'ın iki kez çalışmasını önler
CMD ["gunicorn", "-w", "1", "-b", "0.0.0.0:8000", "app:app"]
