import os
import secrets
import logging
from pathlib import Path
from dotenv import load_dotenv

# Получение корневого каталога проекта
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

# Загрузка переменных окружения из файла .env
env_path = PROJECT_ROOT / '.env'
load_dotenv(dotenv_path=env_path)

# === Настройки логирования ===
# Словарь для безопасного преобразования строки в уровень логирования
LOG_LEVELS = {
    'DEBUG': logging.DEBUG,
    'INFO': logging.INFO,
    'WARNING': logging.WARNING,
    'ERROR': logging.ERROR,
    'CRITICAL': logging.CRITICAL,
}

# Читаем LOG_LEVEL из .env. Если его нет или значение некорректно,
# по умолчанию будет logging.INFO
_log_level_str = os.getenv('LOG_LEVEL', 'INFO').upper()
LOG_LEVEL = LOG_LEVELS.get(_log_level_str, logging.INFO)

# Настройки базы данных
DATA_DIR = PROJECT_ROOT / 'data'
DATA_DIR.mkdir(parents=True, exist_ok=True)
DATABASE_PATH = os.getenv('DATABASE_PATH', str(DATA_DIR / 'email_bot.db'))
if not os.path.isabs(DATABASE_PATH):
    DATABASE_PATH = str(PROJECT_ROOT / DATABASE_PATH)


# Настройки электронной почты
EMAIL_ACCOUNT = os.getenv('EMAIL_ACCOUNT')
EMAIL_PASSWORD = os.getenv('EMAIL_PASSWORD')
EMAIL_SERVER = os.getenv('EMAIL_SERVER', 'imap.yandex.ru')

# Настройки Telegram
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')

# Настройки LLM (Яндекс AI)
LLM_PROVIDER = os.getenv('LLM_PROVIDER', 'yandex_ai')
YANDEX_FOLDER_ID = os.getenv('YANDEX_FOLDER_ID')
YANDEX_OAUTH_TOKEN = os.getenv('YANDEX_OAUTH_TOKEN')
YANDEX_AI_BASE_URL = os.getenv('YANDEX_AI_BASE_URL', 'https://llm.api.cloud.yandex.net/foundationModels/v1')
YANDEX_AI_MODEL = os.getenv('YANDEX_AI_MODEL', 'yandexgpt-lite')
DEFAULT_SUMMARIZATION_PROMPT = os.getenv('DEFAULT_SUMMARIZATION_PROMPT', 'Summarize the following email report concisely, highlighting key information and action items:')


# Настройки веб-администрирования
ADMIN_USERNAME = os.getenv('ADMIN_USERNAME', 'admin')
ADMIN_PASSWORD = os.getenv('ADMIN_PASSWORD')
# Генерация случайного SECRET_KEY если не указан
SECRET_KEY = os.getenv('SECRET_KEY', secrets.token_hex(16))

# Системные настройки
CHECK_INTERVAL = int(os.getenv('CHECK_INTERVAL', 5))  # Минуты

# Пути к директориям
LOGS_DIR = PROJECT_ROOT / 'logs'
DATA_DIR = PROJECT_ROOT / 'data'
TEMPLATES_DIR = PROJECT_ROOT / 'src' / 'web' / 'templates'
STATIC_DIR = PROJECT_ROOT / 'src' / 'web' / 'static'

# Проверка и создание необходимых директорий
DATA_DIR.mkdir(parents=True, exist_ok=True)