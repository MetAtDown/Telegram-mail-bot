import logging
import sys
from typing import Optional

# Импортируем наши централизованные настройки
from src.config import settings

# Этот фильтр будет пропускать только сообщения с уровнем ниже WARNING
class InfoFilter(logging.Filter):
    def filter(self, record):
        return record.levelno < logging.WARNING

def setup_logger(
    name: str,
    level: int,  # <-- Убрали значение по умолчанию, теперь уровень обязателен
    format_string: Optional[str] = None
) -> logging.Logger:
    """
    Настройка логгера для вывода в stdout (INFO) и stderr (WARNINGS и выше).
    """
    logger = logging.getLogger(name)

    # Проверяем, может логгер уже идеально настроен
    if logger.handlers and logger.level == level:
        return logger

    logger.setLevel(level)

    if format_string is None:
        format_string = '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    formatter = logging.Formatter(format_string)

    # --- Обработчик для stdout (INFO, DEBUG) ---
    stdout_handler = logging.StreamHandler(sys.stdout)
    stdout_handler.setFormatter(formatter)
    stdout_handler.addFilter(InfoFilter())

    # --- Обработчик для stderr (WARNING, ERROR, CRITICAL) ---
    stderr_handler = logging.StreamHandler(sys.stderr)
    stderr_handler.setFormatter(formatter)
    stderr_handler.setLevel(logging.WARNING)

    # Очищаем старые и добавляем новые обработчики
    if logger.hasHandlers():
        logger.handlers.clear()

    logger.addHandler(stdout_handler)
    logger.addHandler(stderr_handler)

    logger.propagate = False

    return logger

def get_logger(
    name: str,
    level: Optional[int] = None # <-- Принимаем уровень как опциональный
) -> logging.Logger:
    """
    Получение настроенного логгера.
    Если уровень не передан явно, он будет взят из глобальных настроек.
    """
    # Если уровень не был передан при вызове, берем его из settings.py
    if level is None:
        level = settings.LOG_LEVEL

    return setup_logger(name, level=level)