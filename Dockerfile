FROM python:3.11-slim

WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# зависимости
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# исходники
COPY server ./server

# порт, который будет слушать uvicorn
ENV PORT=8080

CMD ["uvicorn", "server.main:app", "--host", "0.0.0.0", "--port", "8080"]