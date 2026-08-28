FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY templates ./templates
COPY static ./static

# Persist the SQLite database (WAL) on a volume, not inside the image.
ENV VICOBA_DB=/data/vicoba.db \
    PYTHONUNBUFFERED=1
VOLUME ["/data"]

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
