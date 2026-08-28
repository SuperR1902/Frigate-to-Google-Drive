FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py .

RUN mkdir -p /app/db /app/tmp /app/logs /app/credentials

ENV DB_PATH=/app/db/events.db \
    TMP_DIR=/app/tmp \
    LOG_FILE=/app/logs/app.log \
    SERVICE_ACCOUNT_FILE=/app/credentials/service_account.json

CMD ["python3", "main.py"]
