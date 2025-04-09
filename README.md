# Система пересылки Email в Telegram

[![Python](https://img.shields.io/badge/Python-3.13%2B-blue)](https://www.python.org/)
[![License](https://img.shields.io/badge/License-MIT-green)](LICENSE)

Система для мониторинга почтового ящика и пересылки писем в Telegram на основе фильтрации по теме письма. Позволяет настраивать индивидуальные фильтры тем для разных пользователей Telegram.

## Возможности

- ✉️ Автоматический мониторинг почты через IMAP (Yandex, Gmail и др.)
- 🔍 Фильтрация писем по теме
- 📱 Пересылка писем в Telegram с вложениями
- 👥 Поддержка нескольких пользователей с индивидуальными фильтрами
- 🖥️ Веб-интерфейс администратора
- 🛠️ Утилита командной строки для управления
- 📊 Мониторинг работы системы

## Требования

- Python 3.13 или новее
- Доступ к почтовому аккаунту с IMAP
- Telegram Bot Token

## Установка

### 1. Подготовка конфигурации
В nginx/nginx.conf:
```
server {
    listen 80;
    server_name your-domain.example.com;  # Указать свой домен
    
    # Перенаправление на HTTPS
    return 301 https://$host$request_uri;
}

server {
    listen 443 ssl;
    server_name your-domain.example.com;  # Указать свой домен
    
    # SSL сертификаты
    ssl_certificate /etc/nginx/ssl/cert.pem;
    ssl_certificate_key /etc/nginx/ssl/key.pem;
    
    # Базовые SSL настройки
    ssl_protocols TLSv1 TLSv1.1 TLSv1.2 TLSv1.3;
    ssl_ciphers HIGH:!aNULL:!MD5;
    
    # Остальные настройки без изменений...
}
```

В docker-compose.yml (для нестандартного порта):
```
services:
  nginx:
    # Остальные настройки...
    ports:
      - "80:80"
      - "8443:443"  # Если используется нестандартный порт (например, 8443)
    # Остальные настройки...
```

### 2. Развертывание системы

```
# Клонирование репозитория
git clone <url-репозитория>
cd <директория-проекта>

# Установка Docker
sudo apt update
sudo apt install -y docker.io docker-compose
sudo systemctl enable docker
sudo systemctl start docker

# Настройка SSL-сертификатов
mkdir -p nginx/ssl
# Размести сертификаты
cp /путь/к/сертификату.crt nginx/ssl/cert.pem
cp /путь/к/ключу.key nginx/ssl/key.pem

# Создание файла с учетными данными для Basic Auth
apt install apache2-utils
htpasswd -c nginx/.htpasswd admin

# Настройка переменных окружения
cp .env.example .env
nano .env
#

# Запуск системы
docker-compose up -d

# Проверка, что контейнеры запущены
docker-compose ps

# Проверка логов
docker-compose logs -f
```

### 3. Доступ к системе
Система будет доступна по следующим URL:

https://your-domain.example.com (если используется стандартный порт 443)

https://your-domain.example.com:8443 (если используется нестандартный порт)
