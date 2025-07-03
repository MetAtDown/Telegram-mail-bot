import logging
import sys
from typing import Optional

def setup_logger(
    name: str,
    level: int = logging.INFO,
    format_string: Optional[str] = None
) -> logging.Logger:
    """
    Настройка логгера для вывода в консоль (stdout).

    Args:
        name: Имя логгера.
        level: Уровень логирования (например, logging.INFO, logging.DEBUG).
        format_string: Строка форматирования для логов.

    Returns:
        Настроенный экземпляр логгера.
    """
    # Получаем или создаем логгер
    logger = logging.getLogger(name)

    # Если логгер уже настроен с обработчиками, возвращаем его,
    # чтобы избежать дублирования вывода.
    if logger.handlers and logger.level == level:
        return logger

    # Устанавливаем уровень логирования
    logger.setLevel(level)

    # Создаем форматтер для логов
    if format_string is None:
        format_string = '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    formatter = logging.Formatter(format_string)

    # Создаем обработчик для вывода в консоль (stdout)
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)

    # Удаляем существующие обработчики (если есть) и добавляем новый консольный
    if logger.hasHandlers():
        logger.handlers.clear()

    logger.addHandler(console_handler)

    # Устанавливаем свойство propagate в False, чтобы избежать дублирования
    # записей в родительских логгерах (например, в root logger).
    logger.propagate = False

    return logger


def get_logger(
    name: str,
    level: int = logging.INFO
) -> logging.Logger:
    """
    Упрощенная функция для получения настроенного логгера.

    Args:
        name: Имя логгера.
        level: Уровень логирования.

    Returns:
        Экземпляр логгера.
    """
    return setup_logger(name, level=level)
