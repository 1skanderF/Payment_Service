FROM python:3.12

WORKDIR /app

# Копируем зависимости сначала для кэширования
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Копируем код
COPY app/ ./app/

# Переменные окружения
ENV PYTHONPATH=/app
ENV PROVIDER_URL=http://provider-simulator:8081

# Порт
EXPOSE 8080

# Запуск
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1"]