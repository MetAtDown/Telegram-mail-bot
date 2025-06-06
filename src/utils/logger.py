import logging
import logging.handlers
import sys
import threading
import time
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Optional, Union, Set

# Получение корневого каталога проекта
project_root = Path(__file__).parent.parent.parent
logs_dir = project_root / 'logs'

# Глобальная блокировка для очистки логов
_log_cleanup_lock = threading.RLock()
_cleanup_thread = None
_last_cleanup_time = 0
_cleanup_interval = 86400  # Очищать логи не чаще раза в день

# Кэшируем существование директории для логов
_logs_dir_exists = False


@lru_cache(maxsize=32)
def get_logger_path(name: str, timestamp: str = None) -> Path:
    """
    Получает путь к файлу лога с кэшированием для уменьшения операций с файловой системой.

    Args:
        name: Имя логгера
        timestamp: Временная метка для имени файла

    Returns:
        Путь к файлу лога
    """
    if timestamp is None:
        timestamp = datetime.now().strftime('%Y%m%d')
    return logs_dir / f"{name}_{timestamp}.log"


def ensure_logs_directory() -> bool:
    """
    Проверяет и создает директорию для логов при необходимости.

    Returns:
        True, если директория существует или была создана
    """
    global _logs_dir_exists

    if _logs_dir_exists:
        return True

    if not logs_dir.exists():
        try:
            logs_dir.mkdir(parents=True, exist_ok=True)
            _logs_dir_exists = True
            return True
        except Exception as e:
            print(f"Ошибка при создании директории для логов: {e}")
            return False
    else:
        _logs_dir_exists = True
        return True


def clean_old_logs_async(days: int = 30) -> None:
    """
    Асинхронно удаляет старые файлы логов в отдельном потоке.

    Args:
        days: Количество дней, после которых лог считается устаревшим
    """
    global _cleanup_thread, _last_cleanup_time

    # Проверяем не слишком ли часто запускаем очистку
    current_time = time.time()
    if current_time - _last_cleanup_time < _cleanup_interval:
        return

    # Проверяем не запущен ли уже поток очистки
    with _log_cleanup_lock:
        if _cleanup_thread and _cleanup_thread.is_alive():
            return

        # Запускаем новый поток для очистки
        _cleanup_thread = threading.Thread(
            target=clean_old_logs,
            args=(days,),
            daemon=True
        )
        _cleanup_thread.start()
        _last_cleanup_time = current_time


def clean_old_logs(days: int = 30) -> None:
    """
    Удаляет старые файлы логов, которые старше указанного количества дней.

    Args:
        days: Количество дней, после которых лог считается устаревшим
    """
    if not ensure_logs_directory():
        return

    now = time.time()
    cutoff_time = now - days * 86400
    deleted_count = 0

    # Используем set для быстрого поиска уже удаленных файлов
    processed_files: Set[Path] = set()

    # Обрабатываем файлы пакетами для уменьшения нагрузки
    batch_size = 100
    file_count = 0

    try:
        for filepath in logs_dir.glob('*.log*'):
            if filepath in processed_files:
                continue

            if filepath.is_file():
                # Проверяем, старше ли файл указанного количества дней
                try:
                    file_stat = filepath.stat()
                    if file_stat.st_mtime < cutoff_time:
                        try:
                            filepath.unlink()
                            deleted_count += 1
                            processed_files.add(filepath)

                            # Даем системе передышку после обработки пакета файлов
                            file_count += 1
                            if file_count >= batch_size:
                                time.sleep(0.1)  # Малая задержка для снижения нагрузки
                                file_count = 0
                        except PermissionError:
                            # Пропускаем файлы, к которым нет доступа
                            continue
                        except Exception as e:
                            print(f"Ошибка при удалении старого лога {filepath}: {e}")
                except (OSError, FileNotFoundError):
                    # Пропускаем файлы с ошибками доступа
                    continue
    except Exception as e:
        print(f"Ошибка при очистке старых логов: {e}")

    if deleted_count > 0:
        print(f"Удалено {deleted_count} устаревших лог-файлов из {logs_dir}")


def setup_logger(
        name: str,
        log_file: Optional[Union[str, Path]] = None,
        level: int = logging.INFO,
        max_size_mb: int = 5,
        backup_count: int = 3,
        format_string: str = None
) -> logging.Logger:
    """
    Настройка логгера с ротацией файлов и автоматической очисткой старых логов.

    Args:
        name: Имя логгера
        log_file: Путь к файлу логов
        level: Уровень логирования
        max_size_mb: Максимальный размер файла лога в MB перед ротацией
        backup_count: Количество сохраняемых предыдущих файлов лога
        format_string: Строка форматирования для логов

    Returns:
        Настроенный логгер
    """
    # Если путь к файлу лога не указан, создаем его в директории logs с именем логгера
    if log_file is None:
        ensure_logs_directory()
        timestamp = datetime.now().strftime('%Y%m%d')
        log_file = get_logger_path(name, timestamp)

    # Асинхронная очистка старых логов
    clean_old_logs_async(days=30)

    # Получаем или создаем логгер
    logger = logging.getLogger(name)

    # Если логгер уже настроен с обработчиками, возвращаем его
    if logger.handlers and logger.level == level:
        return logger

    # Устанавливаем уровень логирования
    logger.setLevel(level)

    # Создаем форматтер для логов
    if format_string is None:
        format_string = '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    formatter = logging.Formatter(format_string)

    # Создаем обработчик для ротации файлов
    # max_bytes = max_size_mb * 1024 * 1024 (размер в байтах)
    try:
        file_handler = logging.handlers.RotatingFileHandler(
            log_file, maxBytes=max_size_mb * 1024 * 1024,
            backupCount=backup_count, encoding='utf-8'
        )
        file_handler.setFormatter(formatter)
    except (PermissionError, OSError) as e:
        # В случае ошибки доступа к файлу, используем null handler
        print(f"Ошибка при создании файлового обработчика логов: {e}")
        file_handler = logging.NullHandler()

    # Обработчик для вывода в консоль
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)

    # Удаляем существующие обработчики и добавляем новые
    if logger.hasHandlers():
        logger.handlers.clear()

    logger.addHandler(file_handler)
    logger.addHandler(console_handler)

    # Устанавливаем свойство propagate в False, чтобы избежать дублирования записей
    logger.propagate = False

    return logger


def get_logger(
        name: str,
        log_file: Optional[Union[str, Path]] = None,
        level: int = logging.INFO,
        max_size_mb: int = 5,
        backup_count: int = 3
) -> logging.Logger:
    """
    Получение настроенного логгера.

    Args:
        name: Имя логгера
        log_file: Путь к файлу лога (если None, создается автоматически)
        level: Уровень логирования
        max_size_mb: Максимальный размер файла лога в MB перед ротацией
        backup_count: Количество сохраняемых предыдущих файлов лога

    Returns:
        Логгер
    """
    return setup_logger(name, log_file, level, max_size_mb, backup_count)