import hashlib
import secrets

from src.utils.logger import get_logger
from src.config import settings

logger = get_logger("auth")


def hash_password(password: str) -> str:
    """Создает хеш пароля с солью."""
    salt = secrets.token_hex(8)
    pw_hash = hashlib.sha256(f"{password}{salt}".encode()).hexdigest()
    return f"{salt}:{pw_hash}"


def verify_password(stored_hash: str, password: str) -> bool:
    """Проверяет соответствие пароля хэшу."""
    salt, pw_hash = stored_hash.split(":")
    return pw_hash == hashlib.sha256(f"{password}{salt}".encode()).hexdigest()


def init_admin_users(db_manager) -> None:
    """Инициализирует базу пользователей админки."""
    with db_manager.get_connection() as conn:
        cursor = conn.cursor()

        # Создание таблицы пользователей
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS admin_users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_login TIMESTAMP,
            is_active BOOLEAN DEFAULT 1
        )
        ''')

        # Создание таблицы логов активности
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS activity_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            action TEXT NOT NULL,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            ip_address TEXT,
            resource TEXT,
            FOREIGN KEY (user_id) REFERENCES admin_users(id)
        )
        ''')

        # Добавление стандартного админа если нет пользователей
        cursor.execute("SELECT COUNT(*) FROM admin_users")
        if cursor.fetchone()[0] == 0:
            admin_username = settings.ADMIN_USERNAME
            admin_password = settings.ADMIN_PASSWORD

            if admin_username and admin_password:
                password_hash = hash_password(admin_password)
                cursor.execute(
                    "INSERT INTO admin_users (username, password_hash, role) VALUES (?, ?, ?)",
                    (admin_username, password_hash, "admin")
                )
                logger.info(f"Создан стандартный администратор: {admin_username}")

        conn.commit()


def log_activity(db_manager, user_id: int, action: str, ip_address: str = None, resource: str = None) -> None:
    """Логирует действия пользователя."""
    with db_manager.get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO activity_log (user_id, action, ip_address, resource) VALUES (?, ?, ?, ?)",
            (user_id, action, ip_address, resource)
        )
        conn.commit()