FROM python:3.13-slim

# Устанавливаем рабочую директорию
WORKDIR /app

# Устанавливаем необходимые системные зависимости, включая supervisor
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    supervisor \
    libpango-1.0-0 \
    libcairo2 \
    libgirepository1.0-dev \
    libpangoft2-1.0-0 \
    libffi-dev \
    shared-mime-info \
    fonts-dejavu-core \
    fonts-noto-color-emoji \
    && rm -rf /var/lib/apt/lists/*

# Копируем файл с зависимостями и устанавливаем их
COPY requirements.txt .
# Добавляем gunicorn сюда, если он не в requirements.txt
RUN pip install --no-cache-dir -r requirements.txt gunicorn

# Копируем файлы проекта
COPY src/ ./src/
COPY run_tools.py .
COPY README.md .

# Создаем необходимые директории
RUN mkdir -p data logs /etc/supervisor/conf.d

# Создаем конфигурацию supervisor
COPY supervisor.conf /etc/supervisor/conf.d/telegram-bot.conf

# Открываем порт для веб-админки (если она есть)
EXPOSE 5000

# Запускаем supervisor, который будет управлять обоими процессами
CMD ["supervisord", "-n"]