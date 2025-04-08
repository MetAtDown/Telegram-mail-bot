FROM python:3.13-slim

# Устанавливаем рабочую директорию
WORKDIR /app

# Устанавливаем необходимые системные зависимости, включая supervisor
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    supervisor \
    && rm -rf /var/lib/apt/lists/*

# Копируем файл с зависимостями и устанавливаем их
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt gunicorn

# Копируем файлы проекта
COPY src/ ./src/
COPY run_tools.py .
COPY README.md .

# Создаем необходимые директории
RUN mkdir -p data logs /etc/supervisor/conf.d

# Создаем конфигурацию supervisor
COPY supervisor.conf /etc/supervisor/conf.d/telegram-bot.conf

# Открываем порт для веб-админки
EXPOSE 5000

# Запускаем supervisor, который будет управлять обоими процессами
CMD ["supervisord", "-n"]