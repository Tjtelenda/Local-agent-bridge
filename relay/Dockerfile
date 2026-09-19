FROM python:3.11-slim

WORKDIR /srv/relay

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py meta_adapter.py .

ENV PYTHONDONTWRITEBYTECODE=1 \
    TASK_TIMEOUT_SECONDS=30 \
    TASK_TTL_SECONDS=300 \
    CODE_TTL_SECONDS=600 \
    PORT=8000

EXPOSE 8000

CMD ["sh", "-c", "uvicorn app:app --host 0.0.0.0 --port ${PORT:-8000}"]
