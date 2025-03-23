import os
import secrets
from pathlib import Path
from dotenv import load_dotenv

# Получение корневого каталога проекта
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

# Загрузка переменных окружения из файла .env
env_path = PROJECT_ROOT / '.env'
load_dotenv(dotenv_path=env_path)

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

# Настройки веб-администрирования
ADMIN_USERNAME = os.getenv('ADMIN_USERNAME', 'admin')
ADMIN_PASSWORD = os.getenv('ADMIN_PASSWORD')
# Генерация случайного SECRET_KEY если не указан
SECRET_KEY = os.getenv('SECRET_KEY', secrets.token_hex(16))

# Системные настройки
LOG_LEVEL = os.getenv('LOG_LEVEL', 'INFO')
CHECK_INTERVAL = int(os.getenv('CHECK_INTERVAL', 5))  # Минуты

# Пути к директориям
LOGS_DIR = PROJECT_ROOT / 'logs'
DATA_DIR = PROJECT_ROOT / 'data'
TEMPLATES_DIR = PROJECT_ROOT / 'src' / 'web' / 'templates'
STATIC_DIR = PROJECT_ROOT / 'src' / 'web' / 'static'

# Проверка и создание необходимых директорий
LOGS_DIR.mkdir(parents=True, exist_ok=True)
DATA_DIR.mkdir(parents=True, exist_ok=True)