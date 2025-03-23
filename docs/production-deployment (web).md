# Руководство по развертыванию  Telegram_Mail-Bot (только web)

## Подготовка к продакшену

Перед развертыванием веб-интерфейса необходимо внести следующие изменения:

### 1. Настройка переменных окружения

Создайте или отредактируйте файл `.env` в корневой директории проекта:

```bash
# Основные настройки в .env
SECRET_KEY=ваш-надежный-случайный-ключ
ADMIN_USERNAME=имя_администратора
ADMIN_PASSWORD=пароль_для_входа
```

### 2. Изменения в коде

В `admin.py` рекомендуется внести следующие изменения:

```python
# Отключить вывод отладочной информации
app.debug = False

# Убедитесь, что настройки для HTTPS активированы
app.config['SESSION_COOKIE_SECURE'] = True
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
```

## Запуск в продакшен-режиме

### Использование Gunicorn (рекомендуется)

Для продакшен-среды рекомендуется использовать Gunicorn:

```bash
# Установите Gunicorn, если он не установлен
pip install gunicorn

# Запустите приложение с Gunicorn
gunicorn --bind 0.0.0.0:5000 --workers 4 --timeout 60 --access-logfile /путь/к/логам/access.log 'src.admin:app'
```

Параметры для Gunicorn:
- `--bind 0.0.0.0:5000` - привязка к порту 5000 на всех интерфейсах
- `--workers 4` - количество рабочих процессов (рекомендуется: 2 × количество_ядер + 1)
- `--timeout 60` - тайм-аут для запросов в секундах
- `--access-logfile` - путь к журналу доступа

### Настройка Nginx (прокси-сервер)

Для продакшен-среды рекомендуется использовать Nginx в качестве прокси:

```nginx
server {
    listen 80;
    server_name ваш-домен.ru;

    # Перенаправление на HTTPS
    return 301 https://$host$request_uri;
}

server {
    listen 443 ssl;
    server_name ваш-домен.ru;

    ssl_certificate /путь/к/вашему/сертификату.crt;
    ssl_certificate_key /путь/к/вашему/приватному/ключу.key;
    
    # Другие настройки SSL...

    location / {
        proxy_pass http://127.0.0.1:5000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    # Статические файлы
    location /static/ {
        alias /абсолютный/путь/к/static/;
        expires 1d;
    }
}
```

### Запуск как системный сервис

Создайте файл systemd для автоматического запуска:

```ini
[Unit]
Description=DEERAY TG BOT Веб-интерфейс
After=network.target

[Service]
User=ваш_пользователь
Group=ваша_группа
WorkingDirectory=/путь/к/директории/проекта
Environment="PATH=/путь/к/виртуальному/окружению/bin"
ExecStart=/путь/к/виртуальному/окружению/bin/gunicorn --bind 127.0.0.1:5000 --workers 4 --timeout 60 'src.admin:app'
Restart=always

[Install]
WantedBy=multi-user.target
```

Сохраните этот файл как `/etc/systemd/system/deeray-tg-bot.service` и выполните:

```bash
sudo systemctl enable deeray-tg-bot
sudo systemctl start deeray-tg-bot
```

## Проверка перед запуском

Убедитесь, что:

1. ✅ Все пути к директориям и файлам корректны и абсолютны
2. ✅ Настроен надежный пароль администратора
3. ✅ Используется безопасный секретный ключ
4. ✅ Настроено логирование
5. ✅ Установлены все требуемые зависимости
6. ✅ База данных доступна для записи

## Мониторинг и обслуживание

После запуска регулярно проверяйте:

1. Журналы на наличие ошибок: `journalctl -u deeray-tg-bot`
2. Статус сервиса: `systemctl status deeray-tg-bot`
3. Использование системных ресурсов: `top`, `htop` или другие инструменты мониторинга

### Резервное копирование

Настройте регулярное резервное копирование базы данных:

```bash
# Пример скрипта для резервного копирования
#!/bin/bash
BACKUP_DIR="/путь/к/резервным/копиям"
DATE=$(date +"%Y-%m-%d_%H-%M-%S")
cp /путь/к/продакшен/директории/deeray.db "$BACKUP_DIR/deeray_$DATE.db"
```

Добавьте этот скрипт в crontab для регулярного выполнения.

---

*Последнее обновление: 22 марта 2025*