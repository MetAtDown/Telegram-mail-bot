#!/usr/bin/env python3
"""
Универсальный CLI инструмент для работы с базой данных и управления системой Email-Telegram бота.
Оптимизированная версия с улучшенной обработкой ошибок, управлением ресурсами и производительностью.
"""

import argparse
import sys
import time
import subprocess
import psutil
import os
import signal
import json
import threading
import sqlite3
import re
import queue
import contextlib
import functools
from datetime import datetime
from pathlib import Path
from tabulate import tabulate
from typing import Dict, List, Any, Optional, Tuple, Set, Union, Callable, Iterator

# Импортируем оптимизированный логгер
from src.utils.logger import get_logger
from src.db.tools import execute_query, get_common_queries
from src.config import settings
from src.utils.cache_manager import invalidate_caches

# Настройка логгера
logger = get_logger("db_tools_cli")

# Константы
PROCESS_CHECK_INTERVAL = 0.5  # секунды
MAX_RETRIES = 5
RETRY_DELAY = 2  # секунды
DB_CONNECTION_TIMEOUT = 10  # секунды
PROC_KILL_TIMEOUT = 10  # секунды
THREAD_POOL_SIZE = 4  # Размер пула потоков для асинхронных операций

# Кэш процессов с улучшенной блокировкой
_process_cache = {}
_process_cache_lock = threading.RLock()
_process_cache_timestamp = 0
_process_cache_lifetime = 5  # секунды

# Пул соединений с базой данных
_db_connection_pool = {}
_db_connection_pool_lock = threading.RLock()
_db_connection_pool_max_size = 5

# Пул потоков для асинхронных операций
_thread_pool = queue.Queue()
_active_threads = set()
_thread_pool_lock = threading.RLock()


def _initialize_thread_pool():
    """Инициализирует пул потоков для асинхронных операций."""
    with _thread_pool_lock:
        if len(_active_threads) > 0:
            return  # Пул уже инициализирован

        for _ in range(THREAD_POOL_SIZE):
            worker = threading.Thread(target=_thread_worker, daemon=True)
            worker.start()
            _active_threads.add(worker)


def _thread_worker():
    """Рабочий поток для выполнения асинхронных задач из пула."""
    while True:
        try:
            func, args, kwargs = _thread_pool.get()
            try:
                func(*args, **kwargs)
            except Exception as e:
                logger.error(f"Ошибка в рабочем потоке: {e}")
            finally:
                _thread_pool.task_done()
        except Exception as e:
            logger.error(f"Критическая ошибка в рабочем потоке: {e}")
            time.sleep(0.5)  # Предотвращаем перегрузку CPU при ошибках


def run_async(func: Callable, *args, **kwargs):
    """
    Запускает функцию асинхронно в пуле потоков.

    Args:
        func: Функция для асинхронного выполнения
        *args: Позиционные аргументы функции
        **kwargs: Именованные аргументы функции
    """
    _initialize_thread_pool()
    _thread_pool.put((func, args, kwargs))


def cleanup_resources():
    """
    Освобождает ресурсы перед завершением программы.
    """
    # Закрываем все соединения с базой данных
    with _db_connection_pool_lock:
        for db_path, connections in _db_connection_pool.items():
            for conn in connections:
                try:
                    conn.close()
                except Exception:
                    pass
        _db_connection_pool.clear()

    # Очищаем кэш процессов
    with _process_cache_lock:
        _process_cache.clear()

    # Другие операции по очистке ресурсов...
    logger.debug("Ресурсы освобождены")


@contextlib.contextmanager
def get_db_connection(db_path: str) -> Iterator[sqlite3.Connection]:
    """
    Получает соединение с базой данных из пула или создает новое.

    Args:
        db_path: Путь к файлу базы данных

    Yields:
        Соединение с базой данных
    """
    conn = None
    try:
        # Пытаемся получить соединение из пула
        with _db_connection_pool_lock:
            if db_path in _db_connection_pool and _db_connection_pool[db_path]:
                conn = _db_connection_pool[db_path].pop()

        # Если соединение из пула не доступно, создаем новое
        if conn is None:
            conn = sqlite3.connect(db_path, timeout=DB_CONNECTION_TIMEOUT)
            conn.row_factory = sqlite3.Row  # Возвращаем результаты как словари

        # Убеждаемся, что соединение рабочее
        try:
            conn.execute("SELECT 1")
        except sqlite3.Error:
            # Если соединение нерабочее, создаем новое
            conn.close()
            conn = sqlite3.connect(db_path, timeout=DB_CONNECTION_TIMEOUT)
            conn.row_factory = sqlite3.Row

        yield conn
    except sqlite3.Error as e:
        logger.error(f"Ошибка при работе с базой данных: {e}")
        if conn:
            try:
                conn.close()
            except:
                pass
        raise
    finally:
        # Возвращаем соединение в пул или закрываем его
        if conn:
            try:
                if conn.in_transaction:
                    try:
                        conn.rollback()
                    except:
                        pass

                with _db_connection_pool_lock:
                    if db_path not in _db_connection_pool:
                        _db_connection_pool[db_path] = []

                    # Ограничиваем размер пула
                    if len(_db_connection_pool[db_path]) < _db_connection_pool_max_size:
                        _db_connection_pool[db_path].append(conn)
                    else:
                        conn.close()
            except Exception as e:
                logger.debug(f"Ошибка при возврате соединения в пул: {e}")
                try:
                    conn.close()
                except:
                    pass


def format_results(results: List[Dict], headers: List[str], format_type: str = "grid") -> str:
    """
    Форматирует результаты запроса для вывода в консоль с защитой от ошибок.

    Args:
        results: Список словарей с результатами запроса
        headers: Список заголовков столбцов
        format_type: Тип формата таблицы

    Returns:
        Отформатированная строка с результатами
    """
    if not results:
        return "Запрос не вернул данных."

    try:
        # Преобразование списка словарей в список списков для tabulate
        rows = []
        for row in results:
            row_data = []
            for header in headers:
                if header in row:
                    # Преобразуем None в пустую строку
                    value = row[header]
                    if value is None:
                        value = ""
                    # Ограничиваем длину строк для лучшего отображения
                    if isinstance(value, str) and len(value) > 100:
                        value = value[:97] + "..."
                    row_data.append(value)
                else:
                    row_data.append("")
            rows.append(row_data)

        return tabulate(rows, headers=headers, tablefmt=format_type)
    except Exception as e:
        logger.error(f"Ошибка при форматировании результатов: {e}")
        # Запасной вариант, если tabulate не справился
        formatted = " | ".join(headers) + "\n"
        formatted += "-" * (sum(len(h) for h in headers) + 3 * len(headers)) + "\n"
        for row in results:
            row_values = []
            for header in headers:
                value = row.get(header, "")
                if value is None:
                    value = ""
                row_values.append(str(value))
            formatted += " | ".join(row_values) + "\n"
        return formatted


@functools.lru_cache(maxsize=128)
def _get_cmd_key(cmd: str) -> str:
    """
    Создает ключ поиска для командной строки, улучшая производительность кэша.

    Args:
        cmd: Командная строка или часть командной строки

    Returns:
        Оптимизированный ключ для поиска
    """
    # Нормализуем путь и удаляем несущественные части для поиска
    return os.path.basename(cmd.strip()).lower()


def refresh_process_cache() -> None:
    """
    Обновляет кэш процессов для более эффективного поиска.
    """
    global _process_cache, _process_cache_timestamp, _process_cache_lifetime

    current_time = time.time()
    with _process_cache_lock:
        # Проверяем, нужно ли обновлять кэш
        if current_time - _process_cache_timestamp <= _process_cache_lifetime and _process_cache:
            return

        # Очищаем и обновляем кэш
        new_cache = {}

        # Собираем информацию только о Python процессах для оптимизации
        for proc in psutil.process_iter(['pid', 'name', 'cmdline', 'create_time'], ad_value=None):
            try:
                proc_info = proc.info
                if not proc_info['name'] or not (
                        'python' in proc_info['name'].lower() or 'python3' in proc_info['name'].lower()):
                    continue

                # Пропускаем процессы без командной строки
                cmdline = proc_info.get('cmdline', [])
                if not cmdline:
                    continue

                # Используем более эффективное индексирование для поиска
                for cmd in cmdline:
                    if not cmd:
                        continue

                    cmd_key = _get_cmd_key(cmd)
                    if cmd_key not in new_cache:
                        new_cache[cmd_key] = []

                    # Храним только необходимый минимум информации
                    new_cache[cmd_key].append({
                        'pid': proc_info['pid'],
                        'cmdline': cmdline
                    })
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                continue

        # Обновляем кэш атомарно после его построения
        _process_cache = new_cache
        _process_cache_timestamp = current_time


def find_process_by_name(name: str) -> Optional[Dict]:
    """
    Находит процесс по имени файла, используя кэширование для оптимизации.

    Args:
        name: Имя файла или шаблон для поиска

    Returns:
        Информация о процессе или None, если процесс не найден
    """
    try:
        # Нормализуем поисковый шаблон
        name_key = _get_cmd_key(name)

        # Обновляем кэш процессов
        refresh_process_cache()

        # Ищем в кэше по полному совпадению ключа
        with _process_cache_lock:
            if name_key in _process_cache:
                for proc_info in _process_cache[name_key]:
                    if psutil.pid_exists(proc_info['pid']):
                        try:
                            # Проверяем, что процесс еще существует и соответствует поиску
                            proc = psutil.Process(proc_info['pid'])
                            if proc.is_running() and any(name in cmd for cmd in proc.cmdline() if cmd):
                                return {'pid': proc_info['pid'], 'cmdline': proc.cmdline()}
                        except (psutil.NoSuchProcess, psutil.AccessDenied):
                            continue

            # Ищем по частичному совпадению ключа
            for cache_key, proc_list in _process_cache.items():
                if name_key in cache_key:
                    for proc_info in proc_list:
                        if psutil.pid_exists(proc_info['pid']):
                            try:
                                proc = psutil.Process(proc_info['pid'])
                                if proc.is_running() and any(name in cmd for cmd in proc.cmdline() if cmd):
                                    return {'pid': proc_info['pid'], 'cmdline': proc.cmdline()}
                            except (psutil.NoSuchProcess, psutil.AccessDenied):
                                continue

        # Если не нашли в кэше, проводим полный поиск (более медленный)
        for proc in psutil.process_iter(['pid', 'name', 'cmdline']):
            try:
                if 'python' not in proc.name().lower() and 'python3' not in proc.name().lower():
                    continue

                cmdline = proc.cmdline()
                if any(name in cmd for cmd in cmdline if cmd):
                    return {'pid': proc.pid, 'cmdline': cmdline}
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                continue

        return None
    except Exception as e:
        logger.error(f"Ошибка при поиске процесса {name}: {e}")
        return None


def safely_terminate_process(pid: int, timeout: int = PROC_KILL_TIMEOUT) -> bool:
    """
    Безопасно завершает процесс с обработкой ошибок и таймаутом.

    Args:
        pid: ID процесса
        timeout: Таймаут в секундах перед принудительным завершением

    Returns:
        True если процесс успешно завершен, иначе False
    """
    try:
        # Проверяем, существует ли процесс
        if not psutil.pid_exists(pid):
            return True

        # Получаем объект процесса
        proc = psutil.Process(pid)

        # Проверяем дочерние процессы
        children = []
        try:
            children = proc.children(recursive=True)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            children = []

        # Отправляем SIGTERM (более мягкий сигнал)
        logger.info(f"Отправка SIGTERM процессу {pid}")

        try:
            proc.terminate()
        except psutil.NoSuchProcess:
            return True
        except Exception as e:
            logger.warning(f"Ошибка при отправке SIGTERM процессу {pid}: {e}")
            # Продолжаем выполнение, чтобы попробовать SIGKILL

        # Ждем завершения с таймаутом
        try:
            proc.wait(timeout=timeout / 2)
            logger.info(f"Процесс {pid} завершен по SIGTERM")
            terminated = True
        except psutil.TimeoutExpired:
            terminated = False
        except psutil.NoSuchProcess:
            terminated = True

        # Если процесс всё еще работает, отправляем SIGKILL
        if not terminated:
            if psutil.pid_exists(pid):
                logger.warning(f"Процесс {pid} не ответил на SIGTERM, отправка SIGKILL")
                try:
                    proc.kill()
                except (psutil.NoSuchProcess, Exception) as e:
                    if isinstance(e, psutil.NoSuchProcess):
                        return True
                    logger.error(f"Ошибка при отправке SIGKILL процессу {pid}: {e}")

                # Ждем еще раз
                try:
                    proc.wait(timeout=timeout / 2)
                    killed = True
                except (psutil.TimeoutExpired, psutil.NoSuchProcess):
                    killed = not psutil.pid_exists(pid)

                if not killed:
                    logger.error(f"Не удалось завершить процесс {pid} даже с SIGKILL")
                    return False

        # Завершаем дочерние процессы
        if children:
            logger.info(f"Завершение {len(children)} дочерних процессов для {pid}")

            # Сначала пробуем мягкое завершение
            for child in children:
                try:
                    if psutil.pid_exists(child.pid):
                        child.terminate()
                except psutil.NoSuchProcess:
                    continue
                except Exception as e:
                    logger.warning(f"Ошибка при завершении дочернего процесса {child.pid}: {e}")

            # Даем время на завершение
            _, alive = psutil.wait_procs(children, timeout=timeout / 4)

            # Убиваем оставшиеся
            for child in alive:
                try:
                    if psutil.pid_exists(child.pid):
                        child.kill()
                except (psutil.NoSuchProcess, Exception):
                    continue

        return True
    except psutil.NoSuchProcess:
        # Процесс уже не существует
        return True
    except Exception as e:
        logger.error(f"Ошибка при завершении процесса {pid}: {e}")
        # Проверяем, существует ли процесс после всех ошибок
        return not psutil.pid_exists(pid)


def start_web_interface(host: str = '0.0.0.0', port: int = 5000, background: bool = True) -> bool:
    try:
        # Проверяем, не запущен ли уже веб-интерфейс
        web_process = find_process_by_name('admin.py')
        if web_process:
            logger.info(f"Веб-интерфейс уже запущен (PID: {web_process['pid']})")
            return True

        # Определяем путь к скрипту веб-интерфейса
        script_paths = [
            Path(__file__).parent.parent / 'web' / 'admin.py',
            Path('web') / 'admin.py',
            Path(settings.DATA_DIR).parent / 'web' / 'admin.py'
        ]

        script_path = None
        for path in script_paths:
            if path.exists():
                script_path = path
                break

        if not script_path:
            logger.error("Файл веб-интерфейса не найден")
            return False

        cmd = ['python', str(script_path)]
        if host:
            cmd.extend(['--host', host])
        if port:
            cmd.extend(['--port', str(port)])

        if background:
            # Создаем директорию для PID файла, если ее нет
            pid_dir = Path(settings.DATA_DIR)
            pid_dir.mkdir(parents=True, exist_ok=True)
            pid_file = pid_dir / '.web_pid'

            kwargs = {}
            if os.name != 'nt':  # На Unix-подобных системах
                kwargs.update(
                    start_new_session=True,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    stdin=subprocess.DEVNULL,
                    close_fds=True
                )
            else:  # На Windows
                from subprocess import DETACHED_PROCESS
                kwargs.update(
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    stdin=subprocess.DEVNULL,
                    creationflags=DETACHED_PROCESS
                )

            process = subprocess.Popen(cmd, **kwargs)

            # Сохраняем PID в файл
            with open(pid_file, 'w') as f:
                f.write(str(process.pid))

            logger.info(f"Запуск веб-интерфейса (PID: {process.pid})")

            # Проверяем запуск процесса с повторными попытками
            for _ in range(6):
                time.sleep(PROCESS_CHECK_INTERVAL)
                if psutil.pid_exists(process.pid):
                    if process.poll() is None:
                        return True
                    else:
                        logger.error(f"Веб-интерфейс завершился с кодом {process.returncode}")
                        return False

            logger.error("Таймаут при запуске веб-интерфейса")
            return False
        else:
            # Запуск в текущем процессе (блокирующий режим)
            return subprocess.call(cmd) == 0
    except Exception as e:
        logger.error(f"Ошибка при запуске веб-интерфейса: {e}")
        return False


def stop_web_interface() -> bool:
    """
    Останавливает веб-интерфейс администратора с улучшенной обработкой ошибок.

    Returns:
        True если веб-интерфейс успешно остановлен или не был запущен, иначе False
    """
    pid = None
    try:
        # Пытаемся найти PID в файле
        pid_file = Path(settings.DATA_DIR) / '.web_pid'
        if pid_file.exists():
            try:
                with open(pid_file, 'r') as f:
                    pid = int(f.read().strip())

                # Пытаемся завершить процесс
                if pid and psutil.pid_exists(pid):
                    if safely_terminate_process(pid):
                        logger.info(f"Веб-интерфейс остановлен (PID: {pid})")
                    else:
                        logger.error(f"Не удалось остановить веб-интерфейс (PID: {pid})")
                        return False

                # Удаляем файл с PID в любом случае
                pid_file.unlink(missing_ok=True)
                return True
            except (ValueError, IOError) as e:
                # Проблема с чтением или форматом PID файла
                logger.warning(f"Проблема с PID файлом: {e}")
                pid_file.unlink(missing_ok=True)

        # Если не удалось найти PID в файле, ищем процесс по имени
        web_process = find_process_by_name('admin.py')
        if web_process:
            try:
                pid = web_process['pid']
                if safely_terminate_process(pid):
                    logger.info(f"Веб-интерфейс остановлен (PID: {pid})")
                else:
                    logger.error(f"Не удалось остановить веб-интерфейс (PID: {pid})")
                    return False
                return True
            except Exception as e:
                logger.error(f"Ошибка при остановке веб-интерфейса по имени процесса: {e}")
                return False

        logger.info("Веб-интерфейс не запущен")
        return True  # Считаем успешным, если интерфейс и так не запущен
    except Exception as e:
        logger.error(f"Ошибка при остановке веб-интерфейса: {e}")
        return False


def start_bot(restart: bool = False) -> bool:
    try:
        # Проверяем, не запущен ли уже бот
        bot_process = find_process_by_name('system.py')
        if bot_process and not restart:
            logger.info("Бот уже запущен")
            return True

        # Если нужен перезапуск, останавливаем текущий процесс
        if bot_process and restart:
            safely_terminate_process(bot_process['pid'])
            time.sleep(1)  # Даем процессу время на завершение

        # Находим файл бота
        bot_script = Path(__file__).parent.parent / 'core' / 'system.py'
        if not bot_script.exists():
            bot_script = Path('src') / 'core' / 'system.py'
            if not bot_script.exists():
                logger.error("Файл бота не найден")
                return False

        # Настраиваем аргументы запуска для полного отсоединения процесса
        kwargs = {}
        if os.name != 'nt':  # На Unix-подобных системах
            kwargs.update(
                start_new_session=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
                close_fds=True
            )
        else:  # На Windows
            from subprocess import DETACHED_PROCESS
            kwargs.update(
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
                creationflags=DETACHED_PROCESS
            )

        process = subprocess.Popen(['python', str(bot_script)], **kwargs)

        # Остальной код без изменений
        for _ in range(10):
            time.sleep(PROCESS_CHECK_INTERVAL)
            if psutil.pid_exists(process.pid) and process.poll() is None:
                pid_file = Path(settings.DATA_DIR) / '.bot_pid'
                with open(pid_file, 'w') as f:
                    f.write(str(process.pid))
                logger.info(f"Бот успешно запущен (PID: {process.pid})")
                return True

        logger.error("Бот не смог запуститься")
        return False
    except Exception as e:
        logger.error(f"Ошибка при запуске бота: {e}")
        return False


def stop_bot() -> bool:
    """
    Останавливает бота с улучшенной обработкой ошибок.

    Returns:
        True если бот успешно остановлен, иначе False
    """
    try:
        # Импортируем функцию остановки бота из модуля bot_status
        try:
            from src.core.bot_status import stop_bot as stop_bot_process
            from src.core.bot_status import is_bot_running

            # Проверяем, запущен ли бот
            if not is_bot_running():
                logger.info("Бот не запущен, останавливать нечего")
                return True

            # Останавливаем бота через API
            if stop_bot_process():
                # Проверяем, что бот действительно остановился
                for _ in range(10):  # Увеличено количество проверок
                    time.sleep(PROCESS_CHECK_INTERVAL)
                    if not is_bot_running():
                        logger.info("Бот успешно остановлен")
                        return True

                logger.warning("Бот все еще обнаруживается в списке процессов после остановки через API")
            else:
                logger.error("Не удалось остановить бота через API")
        except ImportError:
            logger.warning("Не удалось импортировать модуль bot_status, используем прямую остановку")

        # Если API не сработало или не доступно, пытаемся остановить процесс напрямую
        bot_process = find_process_by_name('system.py')
        if bot_process:
            pid = bot_process['pid']
            logger.info(f"Попытка завершить процесс бота (PID: {pid}) напрямую")
            if safely_terminate_process(pid):
                logger.info(f"Процесс бота (PID: {pid}) успешно завершен напрямую")

                # Удаляем файл с PID, если он есть
                pid_file = Path(settings.DATA_DIR) / '.bot_pid'
                if pid_file.exists():
                    pid_file.unlink(missing_ok=True)

                return True
            else:
                logger.error(f"Не удалось завершить процесс бота (PID: {pid}) напрямую")
                return False
        else:
            logger.info("Процесс бота не найден в списке процессов")
            return True  # Считаем успешным, если процесс бота не найден
    except Exception as e:
        logger.error(f"Ошибка при остановке бота: {e}")
        return False


def restart_bot() -> bool:
    """
    Перезапускает бота, останавливая его, если он уже запущен.

    Returns:
        True если бот успешно перезапущен, иначе False
    """
    try:
        logger.info("Перезапуск бота...")

        # Останавливаем бота
        stop_bot()

        # Даем время на завершение процессов
        time.sleep(2)

        # Запускаем бота заново
        return start_bot(restart=True)
    except Exception as e:
        logger.error(f"Ошибка при перезапуске бота: {e}")
        return False


def execute_db_query_safe(db_path: str, query: str, params: Optional[tuple] = None) -> Tuple[
    bool, List[Dict], List[str], Optional[str]]:
    """
    Безопасное выполнение SQL-запроса с повторными попытками и таймаутом.

    Args:
        db_path: Путь к файлу базы данных
        query: SQL-запрос
        params: Параметры для подстановки

    Returns:
        Кортеж (успех, результаты, заголовки, ошибка)
    """
    for attempt in range(MAX_RETRIES):
        try:
            with get_db_connection(db_path) as conn:
                cursor = conn.cursor()
                cursor.execute(query, params or ())

                if query.strip().upper().startswith(('INSERT', 'UPDATE', 'DELETE')):
                    conn.commit()
                    invalidate_caches()

                # Получаем данные и заголовки
                results = [dict(row) for row in cursor.fetchall()]
                headers = [column[0] for column in cursor.description] if cursor.description else []

                return True, results, headers, None
        except sqlite3.OperationalError as e:
            if "database is locked" in str(e) and attempt < MAX_RETRIES - 1:
                wait_time = RETRY_DELAY * (2 ** attempt)
                logger.warning(f"База данных заблокирована, повторная попытка через {wait_time}с...")
                time.sleep(wait_time)
            else:
                logger.error(f"Ошибка при выполнении запроса: {e}")
                return False, [], [], f"Ошибка базы данных: {e}"
        except Exception as e:
            logger.error(f"Неожиданная ошибка при выполнении запроса: {e}")
            return False, [], [], f"Неожиданная ошибка: {e}"

    return False, [], [], "Превышено максимальное количество попыток выполнения запроса"


def repair_database(db_path: str) -> bool:
    """
    Восстанавливает базу данных после сбоев.

    Args:
        db_path: Путь к файлу базы данных

    Returns:
        True если база данных успешно восстановлена, иначе False
    """
    try:
        logger.info(f"Проверка и восстановление базы данных: {db_path}")

        # Создаем резервную копию базы перед восстановлением
        db_file = Path(db_path)
        if db_file.exists():
            backup_path = db_file.with_suffix(f".backup_{int(time.time())}.db")
            import shutil
            shutil.copy2(db_file, backup_path)
            logger.info(f"Создана резервная копия базы данных: {backup_path}")

        # Проверяем целостность базы данных
        with get_db_connection(db_path) as conn:
            cursor = conn.cursor()

            # Проверяем целостность
            cursor.execute("PRAGMA integrity_check;")
            result = cursor.fetchone()

            if result[0] != "ok":
                logger.warning(f"Обнаружены проблемы с целостностью базы данных: {result[0]}")

                # Восстанавливаем базу
                cursor.execute("VACUUM;")
                conn.commit()

                # Проверяем снова
                cursor.execute("PRAGMA integrity_check;")
                result = cursor.fetchone()

                if result[0] != "ok":
                    logger.error("Не удалось восстановить целостность базы данных")
                    return False
                else:
                    logger.info("База данных успешно восстановлена")
            else:
                logger.info("База данных в порядке")

            return True

    except Exception as e:
        logger.error(f"Ошибка при восстановлении базы данных: {e}")
        return False


def get_system_status(detailed: bool = False) -> Dict[str, Any]:
    """
    Получает расширенный статус системы (бот, веб-интерфейс, ресурсы).

    Args:
        detailed: Если True, включает подробную информацию о системе

    Returns:
        Словарь с информацией о статусе компонентов системы и ресурсах
    """
    try:
        # Проверяем статус бота
        bot_running = False
        bot_pid = None
        bot_uptime = "Неизвестно"
        bot_uptime_seconds = 0

        # Пытаемся получить статус через API
        try:
            from src.core.bot_status import get_bot_status
            bot_status = get_bot_status(bypass_cache=True)
            bot_running = bot_status.get('running', False)
            bot_pid = bot_status.get('pid')
            bot_uptime = bot_status.get('uptime', 'Неизвестно')
            bot_uptime_seconds = bot_status.get('uptime_seconds', 0)
        except ImportError:
            # Если API не доступно, проверяем процесс
            bot_process = find_process_by_name('system.py')
            if bot_process:
                bot_running = True
                bot_pid = bot_process['pid']
                try:
                    proc = psutil.Process(bot_pid)
                    create_time = proc.create_time()
                    uptime_seconds = time.time() - create_time
                    hours, remainder = divmod(uptime_seconds, 3600)
                    minutes, seconds = divmod(remainder, 60)
                    bot_uptime = f"{int(hours)}ч {int(minutes)}м {int(seconds)}с"
                    bot_uptime_seconds = uptime_seconds
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass

        # Проверяем статус веб-интерфейса
        web_running = False
        web_pid = None

        # Пытаемся найти PID в файле
        pid_file = Path(settings.DATA_DIR) / '.web_pid'
        if pid_file.exists():
            try:
                with open(pid_file, 'r') as f:
                    web_pid = int(f.read().strip())

                # Проверяем, существует ли процесс с таким PID
                web_running = psutil.pid_exists(web_pid)
            except (ValueError, IOError, psutil.Error):
                web_running = False

        # Если не удалось найти PID в файле, ищем процесс по имени
        if not web_running:
            web_process = find_process_by_name('admin.py')
            if web_process:
                web_running = True
                web_pid = web_process['pid']

        # Собираем базовую информацию
        status_data = {
            'web_running': web_running,
            'web_pid': web_pid,
            'bot_running': bot_running,
            'bot_pid': bot_pid,
            'bot_uptime': bot_uptime,
            'bot_uptime_seconds': bot_uptime_seconds
        }

        # Если требуется детальная информация, добавляем данные о ресурсах
        if detailed:
            # Добавляем информацию о системных ресурсах
            status_data['system'] = {
                'cpu_percent': psutil.cpu_percent(interval=0.1),
                'memory_percent': psutil.virtual_memory().percent,
                'disk_percent': psutil.disk_usage('/').percent,
                'system_time': datetime.now().isoformat()
            }

            # Если бот запущен, добавляем информацию о его процессе
            if bot_running and bot_pid:
                try:
                    bot_proc = psutil.Process(bot_pid)
                    status_data['bot_resources'] = {
                        'cpu_percent': bot_proc.cpu_percent(interval=0.1),
                        'memory_percent': bot_proc.memory_percent(),
                        'memory_mb': bot_proc.memory_info().rss / (1024 * 1024),
                        'threads': bot_proc.num_threads(),
                        'status': bot_proc.status()
                    }
                except (psutil.NoSuchProcess, psutil.AccessDenied) as e:
                    status_data['bot_resources'] = {'error': str(e)}

            # Если веб-интерфейс запущен, добавляем информацию о его процессе
            if web_running and web_pid:
                try:
                    web_proc = psutil.Process(web_pid)
                    status_data['web_resources'] = {
                        'cpu_percent': web_proc.cpu_percent(interval=0.1),
                        'memory_percent': web_proc.memory_percent(),
                        'memory_mb': web_proc.memory_info().rss / (1024 * 1024),
                        'threads': web_proc.num_threads(),
                        'status': web_proc.status()
                    }
                except (psutil.NoSuchProcess, psutil.AccessDenied) as e:
                    status_data['web_resources'] = {'error': str(e)}

            # Добавляем информацию о базе данных
            try:
                db_file = Path(settings.DATABASE_PATH)
                if db_file.exists():
                    status_data['database'] = {
                        'size_mb': db_file.stat().st_size / (1024 * 1024),
                        'last_modified': datetime.fromtimestamp(db_file.stat().st_mtime).isoformat()
                    }

                    # Получаем количество записей в основных таблицах
                    with get_db_connection(str(db_file)) as conn:
                        cursor = conn.cursor()

                        # Получаем список таблиц
                        cursor.execute("SELECT name FROM sqlite_master WHERE type='table';")
                        tables = [row[0] for row in cursor.fetchall()]

                        # Собираем статистику по таблицам
                        table_stats = {}
                        for table in tables:
                            cursor.execute(f"SELECT COUNT(*) FROM {table};")
                            table_stats[table] = cursor.fetchone()[0]

                        status_data['database']['tables'] = table_stats

            except Exception as e:
                status_data['database'] = {'error': str(e)}

        return status_data
    except Exception as e:
        logger.error(f"Ошибка при получении статуса системы: {e}")
        return {
            'web_running': False,
            'web_pid': None,
            'bot_running': False,
            'bot_uptime': 'Неизвестно',
            'bot_uptime_seconds': 0,
            'error': str(e)
        }


def clear_old_logs(days: int = 30) -> Tuple[int, List[str]]:
    """
    Очищает старые лог-файлы.

    Args:
        days: Количество дней, за которые сохраняются логи

    Returns:
        Кортеж (количество удаленных файлов, список путей к удаленным файлам)
    """
    try:
        logger.info(f"Очистка логов старше {days} дней")

        log_dir = Path('logs')
        if not log_dir.exists():
            logger.warning(f"Директория логов не найдена: {log_dir}")
            return 0, []

        deleted_count = 0
        deleted_files = []
        cutoff_time = time.time() - (days * 24 * 60 * 60)

        # Обрабатываем файлы пакетами для уменьшения нагрузки
        batch_size = 100
        file_count = 0
        processed_files = set()

        for log_pattern in ['*.log*', '*.txt']:
            for log_file in log_dir.glob(log_pattern):
                try:
                    if log_file in processed_files:
                        continue

                    processed_files.add(log_file)

                    if log_file.is_file():
                        # Проверяем, старше ли файл указанного количества дней
                        try:
                            file_stat = log_file.stat()
                            if file_stat.st_mtime < cutoff_time:
                                try:
                                    log_file.unlink()
                                    deleted_count += 1
                                    deleted_files.append(str(log_file))

                                    # Даем системе передышку после обработки пакета файлов
                                    file_count += 1
                                    if file_count >= batch_size:
                                        time.sleep(0.1)  # Малая задержка для снижения нагрузки
                                        file_count = 0
                                except PermissionError:
                                    # Пропускаем файлы, к которым нет доступа
                                    continue
                                except Exception as e:
                                    logger.error(f"Ошибка при удалении старого лога {log_file}: {e}")
                        except (OSError, FileNotFoundError):
                            # Пропускаем файлы с ошибками доступа
                            continue
                except Exception as e:
                    logger.error(f"Ошибка при обработке файла {log_file}: {e}")

        logger.info(f"Удалено {deleted_count} устаревших лог-файлов")
        return deleted_count, deleted_files
    except Exception as e:
        logger.error(f"Ошибка при очистке старых логов: {e}")
        return 0, []


def optimize_database(db_path: str) -> bool:
    """
    Оптимизирует базу данных для повышения производительности.

    Args:
        db_path: Путь к файлу базы данных

    Returns:
        True если база данных успешно оптимизирована, иначе False
    """
    try:
        logger.info(f"Оптимизация базы данных: {db_path}")

        with get_db_connection(db_path) as conn:
            cursor = conn.cursor()

            # Анализируем базу данных
            cursor.execute("PRAGMA analysis_limit=1000;")
            cursor.execute("PRAGMA optimize;")

            # Сжимаем базу данных
            cursor.execute("VACUUM;")

            # Перестраиваем индексы
            cursor.execute("REINDEX;")

            conn.commit()
            logger.info("База данных успешно оптимизирована")
            return True

    except Exception as e:
        logger.error(f"Ошибка при оптимизации базы данных: {e}")
        return False


def check_email_connection() -> Tuple[bool, Optional[str]]:
    """
    Проверяет подключение к почтовому серверу.

    Returns:
        Кортеж (успех, сообщение об ошибке)
    """
    try:
        import imaplib

        logger.info(f"Проверка подключения к почтовому серверу {settings.EMAIL_SERVER}...")

        # Используем контекстный менеджер для правильного закрытия соединения
        with contextlib.closing(imaplib.IMAP4_SSL(settings.EMAIL_SERVER)) as mail:
            mail.login(settings.EMAIL_ACCOUNT, settings.EMAIL_PASSWORD)
            mail.select("inbox")

            # Проверяем, что можем получить список писем
            status, messages = mail.search(None, 'ALL')
            if status != "OK":
                return False, f"Ошибка при поиске писем: {status}"

            return True, None
    except Exception as e:
        logger.error(f"Ошибка при проверке подключения к почте: {e}")
        return False, str(e)


def check_telegram_connection() -> Tuple[bool, Optional[str]]:
    """
    Проверяет подключение к Telegram API.

    Returns:
        Кортеж (успех, сообщение об ошибке)
    """
    try:
        import telebot

        logger.info("Проверка подключения к Telegram API...")

        # Используем таймаут для предотвращения зависания
        bot = telebot.TeleBot(settings.TELEGRAM_TOKEN, threaded=False)
        me = bot.get_me()

        return True, f"Подключено к боту @{me.username} (ID: {me.id})"
    except Exception as e:
        logger.error(f"Ошибка при проверке подключения к Telegram API: {e}")
        return False, str(e)


def parse_log_file(log_path: str, max_lines: int = 100, error_only: bool = False) -> List[Dict[str, Any]]:
    """
    Парсит лог-файл и возвращает структурированные данные.

    Args:
        log_path: Путь к лог-файлу
        max_lines: Максимальное количество строк для возврата
        error_only: Если True, возвращает только ошибки

    Returns:
        Список словарей с данными лога
    """
    try:
        log_file = Path(log_path)
        if not log_file.exists():
            logger.warning(f"Лог-файл не найден: {log_file}")
            return []

        # Регулярное выражение для парсинга строк лога
        log_pattern = r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3}) - (\w+) - (\w+) - (.*)'

        # Используем оптимизированное чтение файла с конца
        log_entries = []

        # Для больших файлов используем более эффективный метод чтения
        if log_file.stat().st_size > 1024 * 1024 * 10:  # Если файл больше 10 МБ
            with open(log_file, 'rb') as f:
                # Перемещаемся к концу файла
                f.seek(0, 2)
                file_size = f.tell()

                # Определяем размер блока для чтения (10 МБ или размер файла, если он меньше)
                block_size = min(10 * 1024 * 1024, file_size)

                # Пропускаем части файла, начиная с конца
                lines = []
                bytes_read = 0

                while bytes_read < file_size and len(lines) < max_lines * 10:
                    bytes_to_read = min(block_size, file_size - bytes_read)
                    f.seek(file_size - bytes_read - bytes_to_read)
                    block = f.read(bytes_to_read).decode('utf-8', errors='replace')
                    lines = block.split('\n') + lines
                    bytes_read += bytes_to_read

                # Анализируем строки
                for line in lines:
                    match = re.match(log_pattern, line)
                    if match:
                        timestamp, logger_name, level, message = match.groups()

                        # Если нужны только ошибки, фильтруем
                        if error_only and level.lower() not in ['error', 'critical', 'exception']:
                            continue

                        log_entries.append({
                            'timestamp': timestamp,
                            'logger': logger_name,
                            'level': level,
                            'message': message
                        })

                        if len(log_entries) >= max_lines:
                            break
        else:
            # Для небольших файлов читаем целиком
            with open(log_file, 'r', encoding='utf-8', errors='replace') as f:
                lines = f.readlines()

                # Ограничиваем количество строк для анализа
                if len(lines) > max_lines * 10:
                    lines = lines[-max_lines * 10:]

                for line in lines:
                    match = re.match(log_pattern, line)
                    if match:
                        timestamp, logger_name, level, message = match.groups()

                        # Если нужны только ошибки, фильтруем
                        if error_only and level.lower() not in ['error', 'critical', 'exception']:
                            continue

                        log_entries.append({
                            'timestamp': timestamp,
                            'logger': logger_name,
                            'level': level,
                            'message': message
                        })

        # Возвращаем последние max_lines записей
        return log_entries[-max_lines:]
    except Exception as e:
        logger.error(f"Ошибка при парсинге лог-файла {log_path}: {e}")
        return []


@functools.lru_cache(maxsize=1)
def get_latest_log_file() -> Optional[Path]:
    """
    Находит последний лог-файл с кэшированием.

    Returns:
        Путь к последнему лог-файлу или None, если файл не найден
    """
    try:
        log_dir = Path('logs')
        if not log_dir.exists():
            logger.warning(f"Директория логов не найдена: {log_dir}")
            return None

        # Ищем все лог-файлы и сортируем по времени последнего изменения
        log_files = list(log_dir.glob('*.log'))
        if not log_files:
            logger.warning("Лог-файлы не найдены")
            return None

        # Сортируем по времени модификации (самый свежий в конце)
        log_files.sort(key=lambda x: x.stat().st_mtime)

        return log_files[-1]
    except Exception as e:
        logger.error(f"Ошибка при поиске последнего лог-файла: {e}")
        return None


def show_error_logs(max_errors: int = 10) -> List[Dict[str, Any]]:
    """
    Показывает последние ошибки из лог-файлов.

    Args:
        max_errors: Максимальное количество ошибок для возврата

    Returns:
        Список словарей с данными об ошибках
    """
    try:
        latest_log = get_latest_log_file()
        if not latest_log:
            return []

        # Парсим лог-файл и получаем только ошибки
        return parse_log_file(str(latest_log), max_lines=max_errors, error_only=True)
    except Exception as e:
        logger.error(f"Ошибка при получении ошибок из логов: {e}")
        return []


def get_database_stats(db_path: str) -> Dict[str, Any]:
    """
    Получает статистику по базе данных.

    Args:
        db_path: Путь к файлу базы данных

    Returns:
        Словарь со статистикой
    """
    try:
        db_file = Path(db_path)
        if not db_file.exists():
            return {"error": "База данных не найдена"}

        stats = {
            "size_mb": db_file.stat().st_size / (1024 * 1024),
            "last_modified": datetime.fromtimestamp(db_file.stat().st_mtime).isoformat(),
            "tables": {}
        }

        # Получаем статистику по таблицам
        with get_db_connection(str(db_file)) as conn:
            cursor = conn.cursor()

            # Получаем список таблиц
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table';")
            tables = [row[0] for row in cursor.fetchall()]

            # Собираем статистику по таблицам
            for table in tables:
                cursor.execute(f"SELECT COUNT(*) FROM {table};")
                stats["tables"][table] = {
                    "rows": cursor.fetchone()[0]
                }

                # Получаем информацию о столбцах
                cursor.execute(f"PRAGMA table_info({table});")
                columns = [row[1] for row in cursor.fetchall()]
                stats["tables"][table]["columns"] = columns

        return stats
    except Exception as e:
        logger.error(f"Ошибка при получении статистики базы данных: {e}")
        return {"error": str(e)}


def cleanup_minimal_resources():
    """Очищает только ресурсы CLI без влияния на фоновые процессы"""
    # Закрываем только соединения с базой данных из текущего процесса
    with _db_connection_pool_lock:
        for db_path, connections in _db_connection_pool.items():
            for conn in connections:
                try:
                    conn.close()
                except Exception:
                    pass
        _db_connection_pool.clear()

    logger.debug("Минимальные ресурсы освобождены")


def handle_exit(stop_services=False):
    print("Завершение работы CLI...")

    if stop_services:
        print("Останавливаются все сервисы...")
        stop_web_interface()
        stop_bot()

    # Очищаем ресурсы
    cleanup_resources()

    # Принудительно останавливаем все потоки
    with _thread_pool_lock:
        for thread in _active_threads:
            try:
                if thread.is_alive():
                    # Помечаем поток как демон, чтобы он не блокировал выход
                    thread._daemon = True
            except:
                pass

    print("Выход из программы.")

    # Используем комбинацию методов для гарантированного завершения
    import os, signal
    try:
        # На Unix системах - отправляем сигнал прерывания текущему процессу
        if os.name != 'nt':
            os.kill(os.getpid(), signal.SIGTERM)
        else:
            # На Windows используем стандартный выход
            import sys
            sys.exit(0)
    finally:
        # Если всё остальное не сработало
        os._exit(0)


def check_user_exists(db_path: str, chat_id: str) -> bool:
    """
    Проверяет, существует ли пользователь с указанным chat_id.

    Args:
        db_path: Путь к файлу базы данных
        chat_id: ID чата пользователя

    Returns:
        True если пользователь существует, иначе False
    """
    try:
        with get_db_connection(db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM users WHERE chat_id = ?", (chat_id,))
            count = cursor.fetchone()[0]
            return count > 0
    except Exception as e:
        logger.error(f"Ошибка при проверке существования пользователя: {e}")
        return False


def check_subject_exists(db_path: str, chat_id: str, subject: str) -> bool:
    """
    Проверяет, существует ли тема у указанного пользователя.

    Args:
        db_path: Путь к файлу базы данных
        chat_id: ID чата пользователя
        subject: Название темы

    Returns:
        True если тема существует у пользователя, иначе False
    """
    try:
        with get_db_connection(db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM subjects WHERE chat_id = ? AND subject = ?", (chat_id, subject))
            count = cursor.fetchone()[0]
            return count > 0
    except Exception as e:
        logger.error(f"Ошибка при проверке существования темы: {e}")
        return False


def is_subject_used_by_others(db_path: str, chat_id: str, subject: str) -> bool:
    """
    Проверяет, используется ли тема другими пользователями.

    Args:
        db_path: Путь к файлу базы данных
        chat_id: ID чата пользователя (исключаем его из проверки)
        subject: Название темы

    Returns:
        True если тема используется другими пользователями, иначе False
    """
    try:
        with get_db_connection(db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM subjects WHERE chat_id != ? AND subject = ?", (chat_id, subject))
            count = cursor.fetchone()[0]
            return count > 0
    except Exception as e:
        logger.error(f"Ошибка при проверке использования темы другими пользователями: {e}")
        return False


def add_user(db_path: str, chat_id: str, status: str = "Enable") -> bool:
    """
    Добавляет нового пользователя.

    Args:
        db_path: Путь к файлу базы данных
        chat_id: ID чата пользователя
        status: Статус пользователя (Enable/Disable)

    Returns:
        True если пользователь успешно добавлен, иначе False
    """
    try:
        # Проверяем, существует ли пользователь
        if check_user_exists(db_path, chat_id):
            logger.info(f"Пользователь {chat_id} уже существует")
            return True

        with get_db_connection(db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("INSERT INTO users (chat_id, status) VALUES (?, ?)", (chat_id, status))
            conn.commit()
            logger.info(f"Пользователь {chat_id} успешно добавлен со статусом {status}")
            invalidate_caches()
            return True
    except Exception as e:
        logger.error(f"Ошибка при добавлении пользователя: {e}")
        return False


def delete_user(db_path: str, chat_id: str) -> bool:
    """
    Удаляет пользователя и все его темы.

    Args:
        db_path: Путь к файлу базы данных
        chat_id: ID чата пользователя

    Returns:
        True если пользователь успешно удален, иначе False
    """
    try:
        # Проверяем, существует ли пользователь
        if not check_user_exists(db_path, chat_id):
            logger.info(f"Пользователь {chat_id} не существует")
            return False

        with get_db_connection(db_path) as conn:
            # Начинаем транзакцию
            cursor = conn.cursor()

            # Удаляем темы пользователя
            cursor.execute("DELETE FROM subjects WHERE chat_id = ?", (chat_id,))
            subjects_deleted = cursor.rowcount

            # Удаляем самого пользователя
            cursor.execute("DELETE FROM users WHERE chat_id = ?", (chat_id,))
            user_deleted = cursor.rowcount > 0

            # Подтверждаем транзакцию
            conn.commit()

            logger.info(f"Пользователь {chat_id} успешно удален. Удалено тем: {subjects_deleted}")
            invalidate_caches()
            return user_deleted
    except Exception as e:
        logger.error(f"Ошибка при удалении пользователя: {e}")
        return False


def set_user_status(db_path: str, chat_id: str, status: str) -> bool:
    """
    Изменяет статус пользователя.

    Args:
        db_path: Путь к файлу базы данных
        chat_id: ID чата пользователя
        status: Новый статус (Enable/Disable)

    Returns:
        True если статус успешно изменен, иначе False
    """
    try:
        # Проверяем, существует ли пользователь
        if not check_user_exists(db_path, chat_id):
            logger.info(f"Пользователь {chat_id} не существует")
            return False

        with get_db_connection(db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("UPDATE users SET status = ? WHERE chat_id = ?", (status, chat_id))
            conn.commit()
            logger.info(f"Статус пользователя {chat_id} изменен на {status}")
            invalidate_caches()
            return True
    except Exception as e:
        logger.error(f"Ошибка при изменении статуса пользователя: {e}")
        return False


def add_subject(db_path: str, chat_id: str, subject: str) -> bool:
    """
    Добавляет тему для пользователя.

    Args:
        db_path: Путь к файлу базы данных
        chat_id: ID чата пользователя
        subject: Название темы

    Returns:
        True если тема успешно добавлена, иначе False
    """
    try:
        # Проверяем, существует ли пользователь
        if not check_user_exists(db_path, chat_id):
            logger.info(f"Пользователь {chat_id} не существует, создаем нового пользователя")
            if not add_user(db_path, chat_id, "Enable"):
                logger.error(f"Не удалось создать пользователя {chat_id}")
                return False

        # Проверяем, существует ли уже такая тема у пользователя
        if check_subject_exists(db_path, chat_id, subject):
            logger.info(f"Тема '{subject}' уже существует у пользователя {chat_id}")
            return True

        with get_db_connection(db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("INSERT INTO subjects (chat_id, subject) VALUES (?, ?)", (chat_id, subject))
            conn.commit()
            logger.info(f"Тема '{subject}' успешно добавлена для пользователя {chat_id}")
            invalidate_caches()
            return True
    except Exception as e:
        logger.error(f"Ошибка при добавлении темы: {e}")
        return False


def delete_subject_by_id(db_path: str, subject_id: int) -> bool:
    """
    Удаляет тему по её ID.

    Args:
        db_path: Путь к файлу базы данных
        subject_id: ID темы

    Returns:
        True если тема успешно удалена, иначе False
    """
    try:
        with get_db_connection(db_path) as conn:
            cursor = conn.cursor()

            # Проверяем, существует ли тема с таким ID
            cursor.execute("SELECT COUNT(*) FROM subjects WHERE id = ?", (subject_id,))
            if cursor.fetchone()[0] == 0:
                logger.info(f"Тема с ID {subject_id} не существует")
                return False

            cursor.execute("DELETE FROM subjects WHERE id = ?", (subject_id,))
            conn.commit()
            logger.info(f"Тема с ID {subject_id} успешно удалена")
            invalidate_caches()
            return True
    except Exception as e:
        logger.error(f"Ошибка при удалении темы по ID: {e}")
        return False


def delete_subject_by_name(db_path: str, chat_id: str, subject: str) -> bool:
    """
    Удаляет тему у указанного пользователя.

    Args:
        db_path: Путь к файлу базы данных
        chat_id: ID чата пользователя
        subject: Название темы

    Returns:
        True если тема успешно удалена, иначе False
    """
    try:
        with get_db_connection(db_path) as conn:
            cursor = conn.cursor()

            # Проверяем, существует ли тема у пользователя
            if not check_subject_exists(db_path, chat_id, subject):
                logger.info(f"Тема '{subject}' не существует у пользователя {chat_id}")
                return False

            cursor.execute("DELETE FROM subjects WHERE chat_id = ? AND subject = ?", (chat_id, subject))
            conn.commit()
            logger.info(f"Тема '{subject}' успешно удалена у пользователя {chat_id}")
            invalidate_caches()
            return True
    except Exception as e:
        logger.error(f"Ошибка при удалении темы по имени: {e}")
        return False


def run_shell(db_path: str, format_type: str = "grid", host: str = '0.0.0.0', port: int = 5000):
    """
    Запускает интерактивную оболочку для управления системой.

    Args:
        db_path: Путь к файлу базы данных
        format_type: Формат вывода таблицы
        host: IP-адрес для веб-интерфейса
        port: Порт для веб-интерфейса
    """
    print("===========================================")
    print("Интерактивная оболочка управления системой")
    print("===========================================")
    print("Команды управления системой:")
    print("  start       - Запустить бота и веб-интерфейс")
    print("  stop        - Остановить бота и веб-интерфейс")
    print("  restart     - Перезапустить бота и веб-интерфейс")
    print("  status      - Проверить статус системы")
    print("  detail      - Проверить статус системы с подробностями")
    print("  start-web   - Запустить только веб-интерфейс")
    print("  start-bot   - Запустить только бота")
    print("  stop-web    - Остановить только веб-интерфейс")
    print("  stop-bot    - Остановить только бота")
    print("  restart-bot - Перезапустить только бота")
    print("  check       - Проверить подключения (почта, Telegram)")
    print("  optimize    - Оптимизировать систему")
    print("  errors      - Показать последние ошибки из логов")
    print("  debug       - Показать отладочную информацию системы")
    print("SQL команды:")
    print("  sql [запрос] - Выполнить SQL запрос")
    print("  tables      - Показать список таблиц")
    print("  users       - Показать список пользователей")
    print("  subjects [chat_id] - Показать темы пользователя")
    print("  add-user [chat_id] [status] - Добавить пользователя (status: Enable/Disable)")
    print("  set-status [chat_id] [status] - Изменить статус пользователя (Enable/Disable)")
    print("  delete-user [chat_id] - Удалить пользователя и все его темы")
    print("  add-subject [chat_id] [тема] - Добавить тему для пользователя")
    print("  delete-subject [ID] - Удалить тему по ID")
    print("  delete-subject-by-name [chat_id] [тема] - Удалить тему по имени у пользователя")
    print("Общие команды:")
    print("  help        - Показать справку")
    print("  exit        - Выйти из оболочки")
    print("===========================================")

    while True:
        try:
            cmd_input = input("\n> ").strip()
            cmd_parts = cmd_input.split(None, 2)
            cmd = cmd_parts[0] if cmd_parts else ""
            args_str = cmd_parts[1:] if len(cmd_parts) > 1 else []

            if cmd == 'exit':
                # Проверяем запущены ли сервисы
                status = get_system_status()
                if status['web_running'] or status['bot_running']:
                    running_services = []
                    if status['web_running']:
                        running_services.append("веб-интерфейс")
                    if status['bot_running']:
                        running_services.append("бот")

                    services_str = " и ".join(running_services)
                    confirm = input(f"Запущены сервисы: {services_str}. Хотите остановить их перед выходом? (y/n): ")

                    # Инвертированная логика - "y" означает остановить сервисы
                    handle_exit(stop_services=(confirm.lower() in ['y', 'yes', 'да']))
                else:
                    handle_exit(stop_services=False)
                return

            elif cmd == 'help':
                print("Доступные команды:")
                print("  start      - Запустить бота и веб-интерфейс")
                print("  stop       - Остановить бота и веб-интерфейс")
                print("  restart    - Перезапустить бота и веб-интерфейс")
                print("  status     - Проверить статус системы")
                print("  detail     - Проверить статус системы с подробностями")
                print("  start-web  - Запустить только веб-интерфейс")
                print("  start-bot  - Запустить только бота")
                print("  stop-web   - Остановить только веб-интерфейс")
                print("  stop-bot   - Остановить только бота")
                print("  restart-bot - Перезапустить только бота")
                print("  check      - Проверить подключения (почта, Telegram)")
                print("  optimize   - Оптимизировать систему")
                print("  errors     - Показать последние ошибки из логов")
                print("  debug      - Показать отладочную информацию системы")
                print("  help       - Показать справку")
                print("  exit       - Выйти из оболочки")
                print("  users      - Показать список пользователей")
                print("  tables     - Показать список таблиц")
                print("  subjects [chat_id] - Показать темы пользователя")
                print("  add-user [chat_id] [status] - Добавить пользователя (status: Enable/Disable)")
                print("  set-status [chat_id] [status] - Изменить статус пользователя (Enable/Disable)")
                print("  delete-user [chat_id] - Удалить пользователя и все его темы")
                print("  add-subject [chat_id] [тема] - Добавить тему для пользователя")
                print("  delete-subject [ID] - Удалить тему по ID")
                print("  delete-subject-by-name [chat_id] [тема] - Удалить тему по имени у пользователя")
                print("  sql [запрос] - Выполнить SQL запрос (SQLite)")

            elif cmd == 'start':
                print("Запуск системы...")

                web_started = start_web_interface(host=host, port=port)
                if web_started:
                    print("Веб-интерфейс успешно запущен.")
                else:
                    print("Ошибка при запуске веб-интерфейса.")

                bot_started = start_bot()
                if bot_started:
                    print("Бот успешно запущен.")
                else:
                    print("Ошибка при запуске бота.")

                print(f"Статус запуска: веб - {'OK' if web_started else 'ОШИБКА'}, "
                      f"бот - {'OK' if bot_started else 'ОШИБКА'}")
            elif cmd == 'stop':
                print("Остановка системы...")

                web_stopped = stop_web_interface()
                if web_stopped:
                    print("Веб-интерфейс успешно остановлен.")
                else:
                    print("Веб-интерфейс не был запущен или произошла ошибка при остановке.")

                bot_stopped = stop_bot()
                if bot_stopped:
                    print("Бот успешно остановлен.")
                else:
                    print("Бот не был запущен или произошла ошибка при остановке.")

                print(f"Статус остановки: веб - {'OK' if web_stopped else 'ОШИБКА'}, "
                      f"бот - {'OK' if bot_stopped else 'ОШИБКА'}")
            elif cmd == 'restart':
                print("Перезапуск системы...")

                # Останавливаем компоненты
                print("Останавливаем компоненты...")
                web_stopped = stop_web_interface()
                bot_stopped = stop_bot()

                print(f"Статус остановки: веб - {'OK' if web_stopped else 'ОШИБКА'}, "
                      f"бот - {'OK' if bot_stopped else 'ОШИБКА'}")

                # Делаем паузу для корректного завершения процессов
                time.sleep(2)

                # Запускаем компоненты
                print("Запускаем компоненты...")
                web_started = start_web_interface(host=host, port=port)
                bot_started = start_bot(restart=True)

                print(f"Статус запуска: веб - {'OK' if web_started else 'ОШИБКА'}, "
                      f"бот - {'OK' if bot_started else 'ОШИБКА'}")
            elif cmd == 'status':
                print("Проверка статуса системы...")

                status = get_system_status(detailed=False)

                print(f"Веб-интерфейс: {'ЗАПУЩЕН' if status['web_running'] else 'ОСТАНОВЛЕН'}")
                if status['web_running'] and status['web_pid']:
                    print(f"  PID: {status['web_pid']}")

                print(f"Бот: {'ЗАПУЩЕН' if status['bot_running'] else 'ОСТАНОВЛЕН'}")
                if status['bot_running']:
                    print(f"  Время работы: {status['bot_uptime']}")

                if 'error' in status:
                    print(f"Ошибка: {status['error']}")
            elif cmd == 'detail':
                print("Проверка статуса системы с подробностями...")

                status = get_system_status(detailed=True)

                print(f"Веб-интерфейс: {'ЗАПУЩЕН' if status['web_running'] else 'ОСТАНОВЛЕН'}")
                if status['web_running'] and status['web_pid']:
                    print(f"  PID: {status['web_pid']}")

                print(f"Бот: {'ЗАПУЩЕН' if status['bot_running'] else 'ОСТАНОВЛЕН'}")
                if status['bot_running']:
                    print(f"  Время работы: {status['bot_uptime']}")

                if 'error' in status:
                    print(f"Ошибка: {status['error']}")

                print("\nДетальная информация:")

                if 'system' in status:
                    print("\nСистемные ресурсы:")
                    print(f"  CPU: {status['system']['cpu_percent']}%")
                    print(f"  Память: {status['system']['memory_percent']}%")
                    print(f"  Диск: {status['system']['disk_percent']}%")

                if 'bot_resources' in status:
                    print("\nРесурсы бота:")
                    if 'error' in status['bot_resources']:
                        print(f"  Ошибка: {status['bot_resources']['error']}")
                    else:
                        print(f"  CPU: {status['bot_resources']['cpu_percent']}%")
                        print(
                            f"  Память: {status['bot_resources']['memory_percent']}% ({status['bot_resources']['memory_mb']:.2f} МБ)")
                        print(f"  Потоки: {status['bot_resources']['threads']}")
                        print(f"  Статус процесса: {status['bot_resources']['status']}")

                if 'web_resources' in status:
                    print("\nРесурсы веб-интерфейса:")
                    if 'error' in status['web_resources']:
                        print(f"  Ошибка: {status['web_resources']['error']}")
                    else:
                        print(f"  CPU: {status['web_resources']['cpu_percent']}%")
                        print(
                            f"  Память: {status['web_resources']['memory_percent']}% ({status['web_resources']['memory_mb']:.2f} МБ)")
                        print(f"  Потоки: {status['web_resources']['threads']}")
                        print(f"  Статус процесса: {status['web_resources']['status']}")

                if 'database' in status:
                    print("\nИнформация о базе данных:")
                    if 'error' in status['database']:
                        print(f"  Ошибка: {status['database']['error']}")
                    else:
                        print(f"  Размер: {status['database']['size_mb']:.2f} МБ")
                        print(f"  Последнее изменение: {status['database']['last_modified']}")

                        if 'tables' in status['database']:
                            print("  Таблицы:")
                            for table, count in status['database']['tables'].items():
                                print(f"    {table}: {count} записей")
            elif cmd == 'start-web':
                print("Запуск веб-интерфейса...")

                if start_web_interface(host=host, port=port):
                    print("Веб-интерфейс успешно запущен.")
                else:
                    print("Ошибка при запуске веб-интерфейса.")
            elif cmd == 'stop-web':
                print("Остановка веб-интерфейса...")

                if stop_web_interface():
                    print("Веб-интерфейс успешно остановлен.")
                else:
                    print("Веб-интерфейс не был запущен или произошла ошибка при остановке.")
            elif cmd == 'start-bot':
                print("Запуск бота...")

                if start_bot():
                    print("Бот успешно запущен.")
                else:
                    print("Ошибка при запуске бота.")
            elif cmd == 'stop-bot':
                print("Остановка бота...")

                if stop_bot():
                    print("Бот успешно остановлен.")
                else:
                    print("Бот не был запущен или произошла ошибка при остановке.")
            elif cmd == 'restart-bot':
                print("Перезапуск бота...")

                if restart_bot():
                    print("Бот успешно перезапущен.")
                else:
                    print("Ошибка при перезапуске бота.")
            elif cmd == 'check':
                print("Проверка подключений...")

                # Проверка подключения к почте
                print("\nПроверка подключения к почте:")
                email_ok, email_message = check_email_connection()
                print(f"  Статус: {'OK' if email_ok else 'ОШИБКА'}")
                if not email_ok and email_message:
                    print(f"  Ошибка: {email_message}")
                elif email_ok:
                    print(f"  Сервер: {settings.EMAIL_SERVER}")
                    print(f"  Аккаунт: {settings.EMAIL_ACCOUNT}")

                # Проверка подключения к Telegram
                print("\nПроверка подключения к Telegram API:")
                telegram_ok, telegram_message = check_telegram_connection()
                print(f"  Статус: {'OK' if telegram_ok else 'ОШИБКА'}")
                if not telegram_ok and telegram_message:
                    print(f"  Ошибка: {telegram_message}")
                elif telegram_ok and telegram_message:
                    print(f"  {telegram_message}")

                # Итоговый статус
                if email_ok and telegram_ok:
                    print("\nВсе подключения работают корректно.")
                else:
                    print("\nОбнаружены проблемы с подключениями.")
            elif cmd == 'errors':
                count = 10  # По умолчанию показываем 10 ошибок
                if args_str and args_str[0].isdigit():
                    count = int(args_str[0])

                print(f"Показ последних {count} ошибок из логов...")

                errors = show_error_logs(max_errors=count)
                if not errors:
                    print("Ошибок не найдено или логи недоступны.")
                else:
                    print(f"Найдено {len(errors)} ошибок:")
                    for i, error in enumerate(errors, 1):
                        print(f"\n{i}. [{error['timestamp']}] {error['level']} - {error['logger']}")
                        print(f"   {error['message']}")
            elif cmd == 'optimize':
                print("Оптимизация системы...")

                print("\nОчистка старых логов...")
                deleted_count, _ = clear_old_logs(days=30)
                print(f"Удалено {deleted_count} старых лог-файлов.")

                print("\nВосстановление базы данных...")
                db_repaired = repair_database(str(db_path))
                print(f"База данных {'успешно восстановлена' if db_repaired else 'не удалось восстановить'}.")

                print("\nОптимизация базы данных...")
                db_optimized = optimize_database(str(db_path))
                print(
                    f"База данных {'успешно оптимизирована' if db_optimized else 'не удалось оптимизировать'}.")
            elif cmd == 'debug':
                print("Отладочная информация системы:")

                # Информация о процессах Python
                print("\nПроцессы Python:")
                refresh_process_cache()
                python_processes = []
                for proc in psutil.process_iter(['pid', 'name', 'cmdline', 'create_time']):
                    try:
                        proc_info = proc.as_dict(['pid', 'name', 'cmdline', 'create_time'])
                        if proc_info['name'] and (
                                'python' in proc_info['name'].lower() or 'python3' in proc_info[
                            'name'].lower()):
                            python_processes.append(proc_info)
                    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                        continue

                if python_processes:
                    for i, proc in enumerate(python_processes, 1):
                        cmd = " ".join(proc.get('cmdline', []))
                        print(f"  {i}. PID: {proc['pid']}, CMD: {cmd[:80]}{'...' if len(cmd) > 80 else ''}")
                else:
                    print("  Процессы Python не найдены.")

                # Информация о БД
                print("\nСтатистика базы данных:")
                db_stats = get_database_stats(str(db_path))
                if 'error' in db_stats:
                    print(f"  Ошибка: {db_stats['error']}")
                else:
                    print(f"  Размер: {db_stats['size_mb']:.2f} МБ")
                    print(f"  Последнее изменение: {db_stats['last_modified']}")
                    print("  Таблицы:")
                    for table, stats in db_stats.get('tables', {}).items():
                        print(f"    {table}: {stats['rows']} записей")

                # Информация о логах
                latest_log = get_latest_log_file()
                print("\nИнформация о логах:")
                if latest_log:
                    print(f"  Последний лог: {latest_log}")
                    print(f"  Размер: {latest_log.stat().st_size / 1024:.2f} КБ")
                    print(
                        f"  Последнее изменение: {datetime.fromtimestamp(latest_log.stat().st_mtime).isoformat()}")
                else:
                    print("  Лог-файлы не найдены.")

                # Информация о настройках
                print("\nНастройки:")
                print(f"  База данных: {settings.DATABASE_PATH}")
                print(f"  Директория данных: {settings.DATA_DIR}")
                print(f"  Почтовый сервер: {settings.EMAIL_SERVER}")
                print(f"  Аккаунт почты: {settings.EMAIL_ACCOUNT}")
                print(f"  Интервал проверки: {settings.CHECK_INTERVAL} мин.")

            elif cmd == 'sql':
                if len(args_str) < 1:
                    print("Ошибка: SQL запрос не указан")
                    continue
                sql_query = args_str[0] if len(args_str) == 1 else " ".join(args_str)
                success, results, headers, error = execute_db_query_safe(str(db_path), sql_query)
                if success and results:
                    print(format_results(results, headers, format_type))
                else:
                    print(error or "Запрос выполнен успешно, но не вернул данных.")
            elif cmd == 'tables':
                success, results, headers, error = execute_db_query_safe(
                    str(db_path), "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name;")
                if success and results:
                    print("Список таблиц в базе данных:")
                    print(format_results(results, headers, format_type))
                else:
                    print("Таблицы не найдены.")
            elif cmd == 'users':
                success, results, headers, error = execute_db_query_safe(str(db_path), """
                                                   SELECT u.chat_id, u.status, COUNT(s.id) as subject_count 
                                                   FROM users u 
                                                   LEFT JOIN subjects s ON u.chat_id = s.chat_id 
                                                   GROUP BY u.chat_id;
                                               """)
                if success and results:
                    print("Список пользователей:")
                    print(format_results(results, headers, format_type))
                else:
                    print("Пользователей не найдено.")
            elif cmd == 'subjects':
                if not args_str:
                    print("Ошибка: не указан chat_id пользователя")
                    continue

                chat_id = args_str[0]
                success, results, headers, error = execute_db_query_safe(str(db_path), """
                                        SELECT * FROM subjects WHERE chat_id = ?;
                                    """, params=(chat_id,))

                if success and results:
                    print(f"Темы пользователя {chat_id}:")
                    print(format_results(results, headers, format_type))
                else:
                    print(error or f"У пользователя {chat_id} нет тем.")

            elif cmd == 'add-user':
                if not args_str:
                    print("Ошибка: не указан chat_id пользователя")
                    continue

                chat_id = args_str[0]
                status = 'Enable'
                if len(args_str) > 1:
                    status = args_str[1]

                if add_user(str(db_path), chat_id, status):
                    print(f"Пользователь {chat_id} успешно добавлен со статусом {status}.")
                else:
                    print(f"Не удалось добавить пользователя {chat_id}.")

            elif cmd == 'set-status':
                if len(args_str) < 2:
                    print("Ошибка: недостаточно аргументов")
                    print("Использование: set-status [chat_id] [Enable/Disable]")
                    continue

                chat_id = args_str[0]
                status = args_str[1]

                if status not in ['Enable', 'Disable']:
                    print("Ошибка: статус должен быть Enable или Disable")
                    continue

                if set_user_status(str(db_path), chat_id, status):
                    print(f"Статус пользователя {chat_id} успешно изменен на {status}.")
                else:
                    print(f"Не удалось изменить статус пользователя {chat_id}.")

            elif cmd == 'delete-user':
                if not args_str:
                    print("Ошибка: не указан chat_id пользователя")
                    continue

                chat_id = args_str[0]

                # Запрашиваем подтверждение
                confirm = input(f"Вы уверены, что хотите удалить пользователя {chat_id} и все его темы? (y/n): ")
                if confirm.lower() not in ['y', 'yes', 'да']:
                    print("Операция отменена.")
                    continue

                if delete_user(str(db_path), chat_id):
                    print(f"Пользователь {chat_id} и все его темы успешно удалены.")
                else:
                    print(f"Не удалось удалить пользователя {chat_id}.")

            elif cmd == 'add-subject':
                if len(args_str) < 2:
                    print("Ошибка: недостаточно аргументов")
                    print("Использование: add-subject [chat_id] [тема]")
                    continue

                chat_id = args_str[0]
                subject = args_str[1]

                # Проверяем, существует ли такая тема у пользователя
                if check_subject_exists(str(db_path), chat_id, subject):
                    print(f"Тема '{subject}' уже существует у пользователя {chat_id}.")
                    continue

                # Проверяем, используется ли такая тема другими пользователями
                if is_subject_used_by_others(str(db_path), chat_id, subject):
                    confirm = input(
                        f"Тема '{subject}' уже используется другими пользователями. Хотите все равно добавить? (y/n): ")
                    if confirm.lower() not in ['y', 'yes', 'да']:
                        print("Операция отменена.")
                        continue

                if add_subject(str(db_path), chat_id, subject):
                    print(f"Тема '{subject}' успешно добавлена для пользователя {chat_id}.")
                else:
                    print(f"Не удалось добавить тему '{subject}' для пользователя {chat_id}.")

            elif cmd == 'delete-subject':
                if not args_str or not args_str[0].isdigit():
                    print("Ошибка: не указан корректный ID темы")
                    continue

                subject_id = int(args_str[0])

                # Запрашиваем подтверждение
                confirm = input(f"Вы уверены, что хотите удалить тему с ID {subject_id}? (y/n): ")
                if confirm.lower() not in ['y', 'yes', 'да']:
                    print("Операция отменена.")
                    continue

                if delete_subject_by_id(str(db_path), subject_id):
                    print(f"Тема с ID {subject_id} успешно удалена.")
                else:
                    print(f"Не удалось удалить тему с ID {subject_id}.")

            elif cmd == 'delete-subject-by-name':
                if len(args_str) < 2:
                    print("Ошибка: недостаточно аргументов")
                    print("Использование: delete-subject-by-name [chat_id] [тема]")
                    continue

                chat_id = args_str[0]
                subject = args_str[1]

                # Проверяем, существует ли такая тема у пользователя
                if not check_subject_exists(str(db_path), chat_id, subject):
                    print(f"Тема '{subject}' не существует у пользователя {chat_id}.")
                    continue

                # Запрашиваем подтверждение
                confirm = input(f"Вы уверены, что хотите удалить тему '{subject}' у пользователя {chat_id}? (y/n): ")
                if confirm.lower() not in ['y', 'yes', 'да']:
                    print("Операция отменена.")
                    continue

                if delete_subject_by_name(str(db_path), chat_id, subject):
                    print(f"Тема '{subject}' успешно удалена у пользователя {chat_id}.")
                else:
                    print(f"Не удалось удалить тему '{subject}' у пользователя {chat_id}.")

            elif cmd == 'repair-db':
                print("Восстановление базы данных...")
                if repair_database(str(db_path)):
                    print("База данных успешно восстановлена.")
                else:
                    print("Ошибка при восстановлении базы данных.")

            elif cmd == 'backup-db':
                backup_path = f"backup_{int(time.time())}.db"
                if len(args_str) > 0:
                    backup_path = args_str[0]

                try:
                    import shutil
                    shutil.copy2(db_path, backup_path)
                    print(f"Резервная копия базы данных успешно создана: {backup_path}")
                except Exception as e:
                    print(f"Ошибка при создании резервной копии базы данных: {e}")

            elif cmd == 'clear-logs':
                days = 30
                if args_str and args_str[0].isdigit():
                    days = int(args_str[0])

                print(f"Очистка логов старше {days} дней...")
                deleted_count, deleted_files = clear_old_logs(days=days)

                if deleted_count > 0:
                    print(f"Успешно удалено {deleted_count} старых лог-файлов.")
                    if len(deleted_files) <= 5:  # Показываем только если их немного
                        for f in deleted_files:
                            print(f"  - {f}")
                else:
                    print("Старые лог-файлы не найдены или не удалены.")

            elif cmd == 'reload':
                print("Перезагрузка настроек и кэшей...")

                # Очищаем кэш процессов
                with _process_cache_lock:
                    _process_cache.clear()
                    _process_cache_timestamp = 0

                # Очищаем кэш лог-файлов
                get_latest_log_file.cache_clear()

                # Перезагружаем настройки (добавлю в будущем)
                try:
                    from src.config import reload_settings
                    reload_settings()
                    print("Настройки успешно перезагружены.")
                except (ImportError, AttributeError):
                    print("Функция перезагрузки настроек не найдена.")

                    print("Перезагрузка завершена.")
            else:
                print(f"Неизвестная команда: {cmd}")
                print("Введите 'help' для списка команд")
        except KeyboardInterrupt:
            print("\nПрерывание работы. Используйте 'exit' для выхода.")
        except Exception as e:
            print(f"Ошибка: {e}")
            logger.exception("Ошибка в интерактивной оболочке")


# Завершение main() функции
def main() -> None:
    """Основная функция для CLI."""
    # Регистрируем обработчик для корректного освобождения ресурсов при выходе
    import atexit
    atexit.register(cleanup_resources)

    parser = argparse.ArgumentParser(
        description='Универсальный инструмент для работы с базой данных и управления системой Email-Telegram бота')

    # Общие аргументы
    parser.add_argument('--db', type=str, default=str(settings.DATABASE_PATH),
                        help='Путь к файлу базы данных')
    parser.add_argument('--format', type=str, choices=['grid', 'simple', 'plain', 'github'], default='grid',
                        help='Формат вывода таблицы')

    # Подпарсеры для разных режимов
    subparsers = parser.add_subparsers(dest='mode', help='Режим работы')

    # === Режим запросов (db_viewer) ===
    query_parser = subparsers.add_parser('query', help='Режим запросов к БД')
    query_parser.add_argument('--sql', type=str, help='SQL-запрос для выполнения')

    # Подрежимы для query
    query_subparsers = query_parser.add_subparsers(dest='query_command', help='Команды запросов')

    # Команда tables - список таблиц
    query_subparsers.add_parser('tables', help='Показать список таблиц')

    # Команда common - показать готовый запрос
    common_parser = query_subparsers.add_parser('common', help='Выполнить готовый запрос')
    common_parser.add_argument('query_name', type=str, help='Название готового запроса')
    common_parser.add_argument('--param', type=str, help='Параметр для подстановки')

    # === Режим администрирования (admin_tool) ===
    admin_parser = subparsers.add_parser('admin', help='Режим администрирования')

    # Подрежимы для admin
    admin_subparsers = admin_parser.add_subparsers(dest='admin_command', help='Команды администрирования')

    # Команда list-users - список пользователей
    admin_subparsers.add_parser('list-users', help='Показать список пользователей')

    # Команда add-user - добавить пользователя
    add_user_parser = admin_subparsers.add_parser('add-user', help='Добавить пользователя')
    add_user_parser.add_argument('chat_id', type=str, help='ID чата пользователя')
    add_user_parser.add_argument('--status', type=str, choices=['Enable', 'Disable'], default='Enable',
                                 help='Статус пользователя')

    # Команда set-status - изменить статус пользователя
    set_status_parser = admin_subparsers.add_parser('set-status', help='Изменить статус пользователя')
    set_status_parser.add_argument('chat_id', type=str, help='ID чата пользователя')
    set_status_parser.add_argument('status', type=str, choices=['Enable', 'Disable'],
                                   help='Новый статус пользователя')

    # Команда delete-user - удалить пользователя
    delete_user_parser = admin_subparsers.add_parser('delete-user', help='Удалить пользователя')
    delete_user_parser.add_argument('chat_id', type=str, help='ID чата пользователя')

    # Команда list-subjects - список тем пользователя
    list_subjects_parser = admin_subparsers.add_parser('list-subjects', help='Показать список тем пользователя')
    list_subjects_parser.add_argument('chat_id', type=str, help='ID чата пользователя')

    # Команда add-subject - добавить тему для пользователя
    add_subject_parser = admin_subparsers.add_parser('add-subject', help='Добавить тему для пользователя')
    add_subject_parser.add_argument('chat_id', type=str, help='ID чата пользователя')
    add_subject_parser.add_argument('subject', type=str, help='Тема для отслеживания')

    # Команда delete-subject - удалить тему пользователя по ID
    delete_subject_parser = admin_subparsers.add_parser('delete-subject',
                                                        help='Удалить тему пользователя по ID')
    delete_subject_parser.add_argument('id', type=int, help='ID темы')

    # Команда delete-subject-by-name - удалить тему пользователя по имени
    delete_subject_name_parser = admin_subparsers.add_parser('delete-subject-by-name',
                                                             help='Удалить тему пользователя по имени')
    delete_subject_name_parser.add_argument('chat_id', type=str, help='ID чата пользователя')
    delete_subject_name_parser.add_argument('subject', type=str, help='Название темы')

    # === Режим управления системой ===
    system_parser = subparsers.add_parser('system', help='Управление системой')

    # Подрежимы для system
    system_subparsers = system_parser.add_subparsers(dest='system_command', help='Команды управления системой')

    # Команда start - запустить всю систему
    start_parser = system_subparsers.add_parser('start', help='Запустить бота и веб-интерфейс')
    start_parser.add_argument('--host', type=str, default='0.0.0.0', help='IP адрес для веб-интерфейса')
    start_parser.add_argument('--port', type=int, default=5000, help='Порт для веб-интерфейса')

    # Команда stop - остановить всю систему
    system_subparsers.add_parser('stop', help='Остановить бота и веб-интерфейс')

    # Команда status - статус системы
    status_parser = system_subparsers.add_parser('status', help='Проверить статус системы')
    status_parser.add_argument('--detailed', action='store_true', help='Показать детальную информацию')

    # Команда restart - перезапустить всю систему
    restart_parser = system_subparsers.add_parser('restart', help='Перезапустить бота и веб-интерфейс')
    restart_parser.add_argument('--host', type=str, default='0.0.0.0', help='IP адрес для веб-интерфейса')
    restart_parser.add_argument('--port', type=int, default=5000, help='Порт для веб-интерфейса')

    # Команда start-web - запустить только веб-интерфейс
    start_web_parser = system_subparsers.add_parser('start-web', help='Запустить только веб-интерфейс')
    start_web_parser.add_argument('--host', type=str, default='0.0.0.0', help='IP адрес для веб-интерфейса')
    start_web_parser.add_argument('--port', type=int, default=5000, help='Порт для веб-интерфейса')

    # Команда stop-web - остановить только веб-интерфейс
    system_subparsers.add_parser('stop-web', help='Остановить только веб-интерфейс')

    # Команда start-bot - запустить только бота
    system_subparsers.add_parser('start-bot', help='Запустить только бота')

    # Команда stop-bot - остановить только бота
    system_subparsers.add_parser('stop-bot', help='Остановить только бота')

    # Команда restart-bot - перезапустить только бота
    system_subparsers.add_parser('restart-bot', help='Перезапустить только бота')

    # Команда check-connections - проверить подключения
    system_subparsers.add_parser('check-connections', help='Проверить подключения к почте и Telegram')

    # Команда errors - показать последние ошибки
    errors_parser = system_subparsers.add_parser('errors', help='Показать последние ошибки из логов')
    errors_parser.add_argument('--count', type=int, default=10, help='Количество ошибок для показа')

    # Команда optimize - оптимизировать систему
    optimize_parser = system_subparsers.add_parser('optimize', help='Оптимизировать систему')
    optimize_parser.add_argument('--clear-logs', action='store_true', help='Очистить старые логи')
    optimize_parser.add_argument('--days', type=int, default=30, help='Возраст логов для очистки (дни)')
    optimize_parser.add_argument('--optimize-db', action='store_true', help='Оптимизировать базу данных')
    optimize_parser.add_argument('--repair-db', action='store_true', help='Восстановить базу данных')

    # Команда shell - интерактивная оболочка
    shell_parser = system_subparsers.add_parser('shell', help='Запустить интерактивную оболочку')
    shell_parser.add_argument('--host', type=str, default='0.0.0.0',
                              help='IP адрес для веб-интерфейса (по умолчанию)')
    shell_parser.add_argument('--port', type=int, default=5000, help='Порт для веб-интерфейса (по умолчанию)')

    # Разбор аргументов
    args = parser.parse_args()

    # Получаем абсолютный путь к БД
    db_path = Path(args.db).absolute()

    # Действия в зависимости от режима
    if args.mode == 'query':
        if args.sql:
            # Прямое выполнение SQL-запроса
            success, results, headers, error = execute_db_query_safe(str(db_path), args.sql)
            if success and results:
                print(format_results(results, headers, args.format))
            else:
                print(error or "Запрос выполнен успешно, но не вернул данных.")
        elif args.query_command == 'tables':
            # Вывод списка таблиц
            success, results, headers, error = execute_db_query_safe(
                str(db_path), "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name;")
            if success and results:
                print("Список таблиц в базе данных:")
                print(format_results(results, headers, args.format))
            else:
                print(error or "Таблицы не найдены.")
        elif args.query_command == 'common':
            # Выполнение готового запроса
            common_queries = get_common_queries()
            if args.query_name in common_queries:
                query = common_queries[args.query_name]
                params = (args.param,) if args.param else None
                success, results, headers, error = execute_db_query_safe(str(db_path), query, params=params)
                if success and results:
                    print(format_results(results, headers, args.format))
                else:
                    print(error or "Запрос выполнен успешно, но не вернул данных.")
            else:
                print(f"Готовый запрос '{args.query_name}' не найден.")
                print("Доступные запросы:")
                for name in common_queries:
                    print(f"  - {name}")
        else:
            query_parser.print_help()

    elif args.mode == 'admin':
        # Для команд администрирования используем функции управления БД
        try:
            if args.admin_command == 'list-users':
                success, results, headers, error = execute_db_query_safe(str(db_path), """
                                            SELECT u.chat_id, u.status, COUNT(s.id) as subject_count 
                                            FROM users u 
                                            LEFT JOIN subjects s ON u.chat_id = s.chat_id 
                                            GROUP BY u.chat_id;
                                        """)
                if success and results:
                    print("Список пользователей:")
                    print(format_results(results, headers, args.format))
                else:
                    print(error or "Пользователей не найдено.")

            elif args.admin_command == 'add-user':
                if add_user(str(db_path), args.chat_id, args.status):
                    print(f"Пользователь {args.chat_id} успешно добавлен со статусом {args.status}.")
                else:
                    print(f"Не удалось добавить пользователя {args.chat_id}.")

            elif args.admin_command == 'set-status':
                if set_user_status(str(db_path), args.chat_id, args.status):
                    print(f"Статус пользователя {args.chat_id} успешно изменен на {args.status}.")
                else:
                    print(f"Не удалось изменить статус пользователя {args.chat_id}.")

            elif args.admin_command == 'delete-user':
                # Запрашиваем подтверждение
                confirm = input(
                    f"Вы уверены, что хотите удалить пользователя {args.chat_id} и все его темы? (y/n): ")
                if confirm.lower() not in ['y', 'yes', 'да']:
                    print("Операция отменена.")
                    return

                if delete_user(str(db_path), args.chat_id):
                    print(f"Пользователь {args.chat_id} и все его темы успешно удалены.")
                else:
                    print(f"Не удалось удалить пользователя {args.chat_id}.")

            elif args.admin_command == 'list-subjects':
                success, results, headers, error = execute_db_query_safe(str(db_path), """
                                            SELECT * FROM subjects WHERE chat_id = ?;
                                        """, params=(args.chat_id,))

                if success and results:
                    print(f"Темы пользователя {args.chat_id}:")
                    print(format_results(results, headers, args.format))
                else:
                    print(error or f"У пользователя {args.chat_id} нет тем.")

            elif args.admin_command == 'add-subject':
                # Проверяем, существует ли такая тема у пользователя
                if check_subject_exists(str(db_path), args.chat_id, args.subject):
                    print(f"Тема '{args.subject}' уже существует у пользователя {args.chat_id}.")
                    return

                # Проверяем, используется ли такая тема другими пользователями
                if is_subject_used_by_others(str(db_path), args.chat_id, args.subject):
                    confirm = input(
                        f"Тема '{args.subject}' уже используется другими пользователями. Хотите все равно добавить? (y/n): ")
                    if confirm.lower() not in ['y', 'yes', 'да']:
                        print("Операция отменена.")
                        return

                if add_subject(str(db_path), args.chat_id, args.subject):
                    print(f"Тема '{args.subject}' успешно добавлена для пользователя {args.chat_id}.")
                else:
                    print(f"Не удалось добавить тему '{args.subject}' для пользователя {args.chat_id}.")

            elif args.admin_command == 'delete-subject':
                # Запрашиваем подтверждение
                confirm = input(f"Вы уверены, что хотите удалить тему с ID {args.id}? (y/n): ")
                if confirm.lower() not in ['y', 'yes', 'да']:
                    print("Операция отменена.")
                    return

                if delete_subject_by_id(str(db_path), args.id):
                    print(f"Тема с ID {args.id} успешно удалена.")
                else:
                    print(f"Не удалось удалить тему с ID {args.id}.")

            elif args.admin_command == 'delete-subject-by-name':
                # Проверяем, существует ли такая тема у пользователя
                if not check_subject_exists(str(db_path), args.chat_id, args.subject):
                    print(f"Тема '{args.subject}' не существует у пользователя {args.chat_id}.")
                    return

                # Запрашиваем подтверждение
                confirm = input(
                    f"Вы уверены, что хотите удалить тему '{args.subject}' у пользователя {args.chat_id}? (y/n): ")
                if confirm.lower() not in ['y', 'yes', 'да']:
                    print("Операция отменена.")
                    return

                if delete_subject_by_name(str(db_path), args.chat_id, args.subject):
                    print(f"Тема '{args.subject}' успешно удалена у пользователя {args.chat_id}.")
                else:
                    print(f"Не удалось удалить тему '{args.subject}' у пользователя {args.chat_id}.")
            else:
                admin_parser.print_help()

        except Exception as e:
            print(f"Ошибка: {e}")
            logger.exception("Ошибка в режиме администрирования")

    elif args.mode == 'system':
        # Режим управления системой
        if args.system_command == 'start':
            print("Запуск системы...")

            web_started = start_web_interface(host=args.host, port=args.port)
            if web_started:
                print("Веб-интерфейс успешно запущен.")
            else:
                print("Ошибка при запуске веб-интерфейса.")

            bot_started = start_bot()
            if bot_started:
                print("Бот успешно запущен.")
            else:
                print("Ошибка при запуске бота.")

            print(f"Статус запуска: веб - {'OK' if web_started else 'ОШИБКА'}, "
                  f"бот - {'OK' if bot_started else 'ОШИБКА'}")

        elif args.system_command == 'stop':
            print("Остановка системы...")

            web_stopped = stop_web_interface()
            if web_stopped:
                print("Веб-интерфейс успешно остановлен.")
            else:
                print("Веб-интерфейс не был запущен или произошла ошибка при остановке.")

            bot_stopped = stop_bot()
            if bot_stopped:
                print("Бот успешно остановлен.")
            else:
                print("Бот не был запущен или произошла ошибка при остановке.")

            print(f"Статус остановки: веб - {'OK' if web_stopped else 'ОШИБКА'}, "
                  f"бот - {'OK' if bot_stopped else 'ОШИБКА'}")

        elif args.system_command == 'status':
            print("Проверка статуса системы...")

            status = get_system_status(detailed=args.detailed)

            print(f"Веб-интерфейс: {'ЗАПУЩЕН' if status['web_running'] else 'ОСТАНОВЛЕН'}")
            if status['web_running'] and status['web_pid']:
                print(f"  PID: {status['web_pid']}")

            print(f"Бот: {'ЗАПУЩЕН' if status['bot_running'] else 'ОСТАНОВЛЕН'}")
            if status['bot_running']:
                print(f"  Время работы: {status['bot_uptime']}")

            if 'error' in status:
                print(f"Ошибка: {status['error']}")

            # Если запрошена детальная информация, показываем дополнительные данные
            if args.detailed:
                print("\nДетальная информация:")

                if 'system' in status:
                    print("\nСистемные ресурсы:")
                    print(f"  CPU: {status['system']['cpu_percent']}%")
                    print(f"  Память: {status['system']['memory_percent']}%")
                    print(f"  Диск: {status['system']['disk_percent']}%")

                if 'bot_resources' in status:
                    print("\nРесурсы бота:")
                    if 'error' in status['bot_resources']:
                        print(f"  Ошибка: {status['bot_resources']['error']}")
                    else:
                        print(f"  CPU: {status['bot_resources']['cpu_percent']}%")
                        print(
                            f"  Память: {status['bot_resources']['memory_percent']}% ({status['bot_resources']['memory_mb']:.2f} МБ)")
                        print(f"  Потоки: {status['bot_resources']['threads']}")
                        print(f"  Статус процесса: {status['bot_resources']['status']}")

                if 'web_resources' in status:
                    print("\nРесурсы веб-интерфейса:")
                    if 'error' in status['web_resources']:
                        print(f"  Ошибка: {status['web_resources']['error']}")
                    else:
                        print(f"  CPU: {status['web_resources']['cpu_percent']}%")
                        print(
                            f"  Память: {status['web_resources']['memory_percent']}% ({status['web_resources']['memory_mb']:.2f} МБ)")
                        print(f"  Потоки: {status['web_resources']['threads']}")
                        print(f"  Статус процесса: {status['web_resources']['status']}")

                if 'database' in status:
                    print("\nИнформация о базе данных:")
                    if 'error' in status['database']:
                        print(f"  Ошибка: {status['database']['error']}")
                    else:
                        print(f"  Размер: {status['database']['size_mb']:.2f} МБ")
                        print(f"  Последнее изменение: {status['database']['last_modified']}")

                        if 'tables' in status['database']:
                            print("  Таблицы:")
                            for table, count in status['database']['tables'].items():
                                print(f"    {table}: {count} записей")

        elif args.system_command == 'restart':
            print("Перезапуск системы...")

            # Останавливаем компоненты
            print("Останавливаем компоненты...")
            web_stopped = stop_web_interface()
            bot_stopped = stop_bot()

            print(f"Статус остановки: веб - {'OK' if web_stopped else 'ОШИБКА'}, "
                  f"бот - {'OK' if bot_stopped else 'ОШИБКА'}")

            # Делаем паузу для корректного завершения процессов
            time.sleep(2)

            # Запускаем компоненты
            print("Запускаем компоненты...")
            web_started = start_web_interface(host=args.host, port=args.port)
            bot_started = start_bot(restart=True)

            print(f"Статус запуска: веб - {'OK' if web_started else 'ОШИБКА'}, "
                  f"бот - {'OK' if bot_started else 'ОШИБКА'}")

        elif args.system_command == 'start-web':
            print("Запуск веб-интерфейса...")

            if start_web_interface(host=args.host, port=args.port):
                print("Веб-интерфейс успешно запущен.")
            else:
                print("Ошибка при запуске веб-интерфейса.")

        elif args.system_command == 'stop-web':
            print("Остановка веб-интерфейса...")

            if stop_web_interface():
                print("Веб-интерфейс успешно остановлен.")
            else:
                print("Веб-интерфейс не был запущен или произошла ошибка при остановке.")

        elif args.system_command == 'start-bot':
            print("Запуск бота...")

            if start_bot():
                print("Бот успешно запущен.")
            else:
                print("Ошибка при запуске бота.")

        elif args.system_command == 'stop-bot':
            print("Остановка бота...")

            if stop_bot():
                print("Бот успешно остановлен.")
            else:
                print("Бот не был запущен или произошла ошибка при остановке.")

        elif args.system_command == 'restart-bot':
            print("Перезапуск бота...")

            if restart_bot():
                print("Бот успешно перезапущен.")
            else:
                print("Ошибка при перезапуске бота.")

        elif args.system_command == 'check-connections':
            print("Проверка подключений...")

            # Проверка подключения к почте
            print("\nПроверка подключения к почте:")
            email_ok, email_message = check_email_connection()
            print(f"  Статус: {'OK' if email_ok else 'ОШИБКА'}")
            if not email_ok and email_message:
                print(f"  Ошибка: {email_message}")
            elif email_ok:
                print(f"  Сервер: {settings.EMAIL_SERVER}")
                print(f"  Аккаунт: {settings.EMAIL_ACCOUNT}")

            # Проверка подключения к Telegram
            print("\nПроверка подключения к Telegram API:")
            telegram_ok, telegram_message = check_telegram_connection()
            print(f"  Статус: {'OK' if telegram_ok else 'ОШИБКА'}")
            if not telegram_ok and telegram_message:
                print(f"  Ошибка: {telegram_message}")
            elif telegram_ok and telegram_message:
                print(f"  {telegram_message}")

            # Итоговый статус
            if email_ok and telegram_ok:
                print("\nВсе подключения работают корректно.")
            else:
                print("\nОбнаружены проблемы с подключениями.")

        elif args.system_command == 'errors':
            print(f"Показ последних {args.count} ошибок из логов...")

            errors = show_error_logs(max_errors=args.count)
            if not errors:
                print("Ошибок не найдено или логи недоступны.")
            else:
                print(f"Найдено {len(errors)} ошибок:")
                for i, error in enumerate(errors, 1):
                    print(f"\n{i}. [{error['timestamp']}] {error['level']} - {error['logger']}")
                    print(f"   {error['message']}")

        elif args.system_command == 'optimize':
            print("Оптимизация системы...")

            # Очистка старых логов
            if args.clear_logs:
                print(f"\nОчистка логов старше {args.days} дней...")
                deleted_count, deleted_files = clear_old_logs(days=args.days)
                if deleted_count > 0:
                    print(f"Успешно удалено {deleted_count} старых лог-файлов.")
                else:
                    print("Старые лог-файлы не найдены или не удалены.")

            # Восстановление базы данных
            if args.repair_db:
                print("\nВосстановление базы данных...")
                if repair_database(str(db_path)):
                    print("База данных успешно восстановлена.")
                else:
                    print("Ошибка при восстановлении базы данных.")

            # Оптимизация базы данных
            if args.optimize_db:
                print("\nОптимизация базы данных...")
                if optimize_database(str(db_path)):
                    print("База данных успешно оптимизирована.")
                else:
                    print("Ошибка при оптимизации базы данных.")

            # Если не указаны опции, запускаем все оптимизации
            if not (args.clear_logs or args.repair_db or args.optimize_db):
                print("\nЗапуск всех оптимизаций...")

                print("\nОчистка старых логов...")
                deleted_count, _ = clear_old_logs(days=30)
                print(f"Удалено {deleted_count} старых лог-файлов.")

                print("\nВосстановление базы данных...")
                db_repaired = repair_database(str(db_path))
                print(f"База данных {'успешно восстановлена' if db_repaired else 'не удалось восстановить'}.")

                print("\nОптимизация базы данных...")
                db_optimized = optimize_database(str(db_path))
                print(
                    f"База данных {'успешно оптимизирована' if db_optimized else 'не удалось оптимизировать'}.")

        elif args.system_command == 'shell':
            try:
                run_shell(str(db_path), args.format, args.host, args.port)
            except KeyboardInterrupt:
                # Обрабатываем Ctrl+C для корректного выхода из оболочки
                print("\nЗавершение работы...")
                handle_exit()
            except Exception as e:
                logger.error(f"Ошибка в интерактивной оболочке: {e}")
                print(f"Ошибка: {e}")
                handle_exit()
        else:
            system_parser.print_help()

    else:
        # Если режим не указан, выводим справку
        parser.print_help()
        print("\nДоступные готовые запросы:")
        try:
            for name in get_common_queries():
                print(f"  - {name}")
        except Exception as e:
            print(f"Ошибка при получении списка готовых запросов: {e}")


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nПрерывание работы...")
        cleanup_resources()
        sys.exit(0)
    except Exception as e:
        logger.exception(f"Неожиданная ошибка: {e}")
        print(f"Критическая ошибка: {e}")
        cleanup_resources()
        sys.exit(1)