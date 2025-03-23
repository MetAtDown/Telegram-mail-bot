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

1. Клонируйте репозиторий:
   ```bash
   git clone https://github.com/username/email-telegram-forwarder.git
   cd email-telegram-forwarder
   ```

2. Создайте виртуальное окружение (рекомендуется для работы `scripts\:` `win_start_system.bat` or `linux_start_system.sh`):
   ```bash
   python -m venv venv
   source venv/bin/activate  # Для Linux/Mac
   # или
   venv\Scripts\activate     # Для Windows
   ```

3. Установите зависимости:
   ```bash
   pip install -r requirements.txt
   ```

4. Создайте файл `.env` в корне проекта:
   ```
   # Database settings
   DATABASE_PATH=data/email_bot.db
   
   # Email settings
   EMAIL_ACCOUNT=example@yandex.ru
   EMAIL_PASSWORD=imap-pass
   
   # Telegram settings
   TELEGRAM_TOKEN=your-telegram-bot-token

   # Web admin settings
   ADMIN_USERNAME=log-admin
   ADMIN_PASSWORD=log-pass
   SECRET_KEY=random-secret-key
   
   # System settings
   LOG_LEVEL=INFO, ERROR, WARNING, DEBUG # (choose one)
   CHECK_INTERVAL= 5 # how many minutes between checks
   ```


## Запуск

Ознакомьтесь с инструкцией:
```bash
python run_tools.py --help
```
И запускайте систему:
```bash
python run_tools.py system start
```

Веб-интерфейс администратора будет доступен по адресу: `http://localhost:5000`

## Использование

### Настройка пользователей и фильтров

1. Войдите в веб-интерфейс администратора с учетными данными из `.env`
2. Перейдите в раздел "Пользователи" и добавьте нового пользователя, указав Chat ID в Telegram
3. Откройте страницу пользователя и добавьте темы/шаблоны для отслеживания

### Использование CLI

Просмотр статуса системы:
```bash
python run_tools.py system status
```

Добавление пользователя через CLI:
```bash
python run_tools.py admin add-user 123456789 --status Enable
```

Добавление темы для пользователя:
```bash
python run_tools.py admin add-subject 123456789 "Важный отчет"
```

Интерактивная оболочка:
```bash
python run_tools.py system shell
```

### Команды Telegram-бота

- `/start` - Начать работу с ботом
- `/status` - Проверить свой статус и список тем
- `/reports` - Показать список отслеживаемых тем
- `/enable` - Включить уведомления
- `/disable` - Отключить уведомления
- `/help` - Показать справку

## Структура проекта

```
Telegram_Mail-Bot/
├── data/                         # Data files directory
├── docs/                         # Documentation
├── logs/                         # Log files directory
├── scripts/                      # Scripts directory
├── src/                          # Source code
│   ├── cli/                      # Command line interface components
│   │   ├── __init__.py
│   │   └── tools.py
│   ├── config/                   # Configuration files
│   │   ├── __init__.py
│   │   └── settings.py
│   ├── core/                     # Core functionality
│   │   ├── __init__.py
│   │   ├── bot_status.py
│   │   ├── email_handler.py
│   │   ├── system.py
│   │   └── telegram_handler.py
│   ├── db/                       # Database operations
│   │   ├── __init__.py
│   │   ├── manager.py
│   │   └── tools.py
│   ├── utils/                    # Utility functions
│   │   ├── __init__.py
│   │   ├── cache_manager.py
│   │   └── logger.py
│   └── web/                      # Web interface
│       ├── static/               # Static assets
│       │   ├── css/
│       │   ├── img/
│       │   ├── js/
│       │   └── manifest.webmanifest
│       ├── templates/            # HTML templates
│       │   ├── includes/
│       │   │   ├── header.html
│       │   │   └── sidebar.html
│       │   ├── 404.html
│       │   ├── 500.html
│       │   ├── add_user.html
│       │   ├── base.html
│       │   ├── bot_status.html
│       │   ├── confirm_bulk_subjects.html
│       │   ├── confirm_duplicate_subject.html
│       │   ├── confirm_edit_subject.html
│       │   ├── help.html
│       │   ├── index.html
│       │   ├── login.html
│       │   ├── sql_console.html
│       │   ├── user_details.html
│       │   └── users.html
│       ├── __init__.py
│       └── admin.py
├── .env                          # Environment variables
├── .gitignore                    # Git ignore file
├── LICENSE                       # License file
├── README.md                     # Project documentation
├── requirements.txt              # Python dependencies
└── run_tools.py                  # Tool runner script

```

## Поддержка и решение проблем

### Проверка статуса и логов
```bash
# Проверка статуса системы
python run_tools.py system status --detailed

# Просмотр последних ошибок
python run_tools.py system errors

# Тестирование соединений
python run_tools.py system check-connections
```