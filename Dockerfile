FROM python:3.11-slim

WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY server ./server

# Запускаем uvicorn на том порту, который Railway передаёт через переменную PORT
CMD uvicorn server.main:app --host 0.0.0.0 --port ${PORT}