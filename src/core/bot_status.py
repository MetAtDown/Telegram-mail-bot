import json
import subprocess
import sys
import psutil
import signal
import functools
import threading
import time
import os
from datetime import datetime
from pathlib import Path
from typing import Dict, Tuple, Any, Optional, List, Set

from src.utils.logger import get_logger
from src.config import settings

# Настройка логирования
logger = get_logger("bot_status")

# Определение пути к файлу статуса бота
STATUS_FILE_PATH = Path(settings.DATA_DIR) / 'bot_status.json'

# Кэш для статуса процесса и меток времени
_cache_lock = threading.RLock()
_status_cache = {}
_cache_timestamp = 0
_cache_lifetime = 10  # секунд

# Блокировка для операций с файлами
_file_lock = threading.RLock()


def with_file_lock(func):
    """Декоратор для защиты операций с файлами"""

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        with _file_lock:
            return func(*args, **kwargs)

    return wrapper


def with_cache_lock(func):
    """Декоратор для защиты операций с кэшем"""

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        with _cache_lock:
            return func(*args, **kwargs)

    return wrapper


@with_file_lock
def update_status_file(status_data: Dict[str, Any]) -> bool:
    """
    Обновляет файл статуса бота атомарно.

    Args:
        status_data: Данные о статусе бота для записи в файл

    Returns:
        True в случае успешного обновления, False в случае ошибки
    """
    try:
        # Создаем директорию, если она не существует
        STATUS_FILE_PATH.parent.mkdir(parents=True, exist_ok=True)

        # Записываем во временный файл и затем переименовываем для атомарности
        temp_file = STATUS_FILE_PATH.with_suffix('.tmp')
        with open(temp_file, 'w', encoding='utf-8') as f:
            json.dump(status_data, f, ensure_ascii=False, indent=2)

        # Переименовываем файл (атомарная операция на большинстве файловых систем)
        temp_file.replace(STATUS_FILE_PATH)

        return True
    except Exception as e:
        logger.error(f"Ошибка при обновлении файла статуса: {e}")
        return False


def format_uptime(seconds: int) -> str:
    """
    Форматирует время работы бота в удобочитаемый формат.

    Args:
        seconds: Количество секунд работы

    Returns:
        Отформатированное время работы (ДД:ЧЧ:ММ:СС)
    """
    if seconds < 0:
        return "Неизвестно"

    try:
        days, remainder = divmod(seconds, 86400)
        hours, remainder = divmod(remainder, 3600)
        minutes, seconds = divmod(remainder, 60)

        if days > 0:
            return f"{days}д {hours:02}:{minutes:02}:{seconds:02}"
        else:
            return f"{hours:02}:{minutes:02}:{seconds:02}"
    except Exception as e:
        logger.error(f"Ошибка при форматировании времени работы: {e}")
        return "Неизвестно"


@with_cache_lock
def _get_cached_process_info() -> Tuple[bool, Optional[int], Optional[float]]:
    """
    Возвращает кэшированную информацию о процессе бота.

    Returns:
        Кортеж (бот_запущен, pid, время_создания_процесса)
    """
    global _status_cache, _cache_timestamp, _cache_lifetime

    current_time = time.time()
    if current_time - _cache_timestamp <= _cache_lifetime and _status_cache:
        return _status_cache.get('running', False), _status_cache.get('pid'), _status_cache.get('create_time')

    # Если кэш устарел, возвращаем None, чтобы вызвать обновление
    return None, None, None


@with_cache_lock
def _update_process_cache(running: bool, pid: Optional[int], create_time: Optional[float]) -> None:
    """
    Обновляет кэш информации о процессе.

    Args:
        running: Состояние бота (запущен/остановлен)
        pid: ID процесса
        create_time: Время создания процесса
    """
    global _status_cache, _cache_timestamp, _cache_lifetime

    _status_cache = {
        'running': running,
        'pid': pid,
        'create_time': create_time
    }
    _cache_timestamp = time.time()


def find_python_processes() -> List[Dict[str, Any]]:
    """
    Находит только процессы Python в системе.

    Returns:
        Список процессов Python с необходимой информацией
    """
    python_processes = []

    try:
        for proc in psutil.process_iter(['pid', 'name', 'cmdline', 'create_time']):
            try:
                # Проверяем, является ли процесс Python
                proc_info = proc.as_dict(['pid', 'name', 'cmdline', 'create_time'])
                if proc_info['name'] and (
                        'python' in proc_info['name'].lower() or 'python3' in proc_info['name'].lower()):
                    python_processes.append(proc_info)
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                continue
    except Exception as e:
        logger.error(f"Ошибка при поиске Python процессов: {e}")

    return python_processes


def find_bot_process() -> Tuple[bool, Optional[int], Optional[float]]:
    """Находит процесс бота и возвращает его PID и время создания."""
    # Сначала проверяем кэш
    cached_running, cached_pid, cached_create_time = _get_cached_process_info()
    if cached_running is not None and cached_pid:
        try:
            proc = psutil.Process(cached_pid)
            # Проверяем, это тот же процесс по времени создания
            if cached_create_time and abs(proc.create_time() - cached_create_time) < 0.1:
                return True, cached_pid, cached_create_time
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass  # Процесс не существует или недоступен

    try:
        python_processes = find_python_processes()
        logger.debug(f"Найдено {len(python_processes)} Python процессов")

        for proc_info in python_processes:
            try:
                pid = proc_info['pid']
                cmdline = proc_info.get('cmdline', [])

                # Формируем строку для поиска
                cmd_str = ' '.join([str(cmd) for cmd in cmdline if cmd])
                logger.debug(f"PID {pid}: {cmd_str[:150]}")

                # ИСПРАВЛЕНИЕ: Более широкий поиск
                if 'system.py' in cmd_str:
                    logger.info(f"Найден процесс бота с PID={pid}: {cmd_str[:150]}")
                    create_time = proc_info.get('create_time', time.time())
                    _update_process_cache(True, pid, create_time)
                    return True, pid, create_time
            except Exception as e:
                logger.debug(f"Ошибка при проверке процесса {proc_info.get('pid')}: {e}")
                continue

        return False, None, None
    except Exception as e:
        logger.error(f"Ошибка при поиске процесса бота: {e}")
        return False, None, None


def get_bot_status(bypass_cache: bool = False) -> Dict[str, Any]:
    """
    Получает статус бота путем проверки запущенных процессов.
    Оптимизировано с использованием кэширования.

    Args:
        bypass_cache: Игнорировать кэшированные данные и выполнить проверку процессов

    Returns:
        Словарь со статусом бота и дополнительной информацией
    """
    now = datetime.now()

    try:
        # Проверяем бота через процессы
        bot_running, pid, create_time = find_bot_process()

        # Определяем время начала работы
        start_time = None
        uptime_seconds = 0
        uptime_str = "Неизвестно"

        # Проверяем наличие файла статуса
        if STATUS_FILE_PATH.exists():
            try:
                with open(STATUS_FILE_PATH, 'r', encoding='utf-8') as f:
                    status_data = json.load(f)

                # Если бот запущен, проверяем время запуска
                if bot_running:
                    if create_time:
                        # Используем время создания процесса
                        start_time = create_time
                    elif status_data.get('start_time'):
                        # Или берем из сохраненного файла
                        start_time = status_data.get('start_time')
                    else:
                        # Или используем текущее время
                        start_time = now.timestamp()
                else:
                    # Если бот остановлен, сбрасываем время
                    start_time = None
            except Exception as e:
                logger.error(f"Ошибка при чтении файла статуса: {e}")
                # При ошибке чтения файла - стандартные значения
                if bot_running and create_time:
                    start_time = create_time
                else:
                    start_time = None
        else:
            # Если файла нет, но бот запущен - используем время создания процесса
            if bot_running and create_time:
                start_time = create_time
            else:
                start_time = None

        # Вычисляем uptime
        if bot_running and start_time:
            uptime_seconds = int(now.timestamp() - start_time)
            uptime_str = format_uptime(uptime_seconds)

        # Создаем новые данные о статусе
        status_data = {
            'running': bot_running,
            'forwarder_active': bot_running,
            'bot_active': bot_running,
            'updated_at': now.isoformat(),
            'start_time': start_time,
            'uptime_seconds': uptime_seconds,
            'uptime': uptime_str,
            'pid': pid
        }

        # Сохраняем новые данные в файл
        update_status_file(status_data)

        return {
            'running': bot_running,
            'forwarder_active': bot_running,
            'bot_active': bot_running,
            'last_check': now.isoformat(),
            'uptime': uptime_str,
            'uptime_seconds': uptime_seconds,
            'pid': pid,
            'status_source': 'process_check'
        }

    except Exception as e:
        logger.error(f"Ошибка при получении статуса бота: {e}")
        return {
            'running': False,
            'forwarder_active': False,
            'bot_active': False,
            'last_check': now.isoformat(),
            'uptime': 'Неизвестно',
            'uptime_seconds': 0,
            'error': str(e),
            'status_source': 'error'
        }


def start_bot() -> bool:
    """Запуск бота, если он не запущен."""
    try:
        # Проверяем, запущен ли уже бот
        bot_running, _, _ = find_bot_process()
        if bot_running:
            logger.info("Бот уже запущен, повторный запуск не требуется")
            return True

        # Формируем путь к скрипту запуска
        script_path = Path(__file__).parent.parent / 'core' / 'system.py'
        abs_script_path = str(script_path.resolve())
        logger.info(f"Запуск бота с абсолютным путем: {abs_script_path}")

        # Проверяем существование файла
        if not script_path.exists():
            logger.error(f"Файл скрипта не найден: {abs_script_path}")
            return False

        # Используем полный путь к python.exe
        python_exe = sys.executable
        logger.info(f"Используемый интерпретатор Python: {python_exe}")

        # Получаем текущую директорию проекта
        project_dir = Path(__file__).resolve().parent.parent.parent
        logger.info(f"Директория проекта: {project_dir}")

        # Запуск процесса
        try:
            # На Windows используем другой метод создания процесса
            startupinfo = None
            if os.name == 'nt':
                import subprocess
                startupinfo = subprocess.STARTUPINFO()
                startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW

            process = subprocess.Popen(
                [python_exe, abs_script_path],
                cwd=str(project_dir),  # Установка рабочей директории
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                stdin=subprocess.DEVNULL,
                startupinfo=startupinfo,  # Только для Windows
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == 'nt' else 0
            )

            # Ждем немного, чтобы процесс успел запуститься
            time.sleep(3)

            # Проверка на немедленное завершение
            if process.poll() is not None:
                stdout, stderr = process.communicate(timeout=5)
                logger.error(f"Процесс завершился с кодом: {process.returncode}")
                logger.error(f"Вывод: {stderr.decode('utf-8', errors='ignore')}")
                return False

            # Активное ожидание появления процесса
            pid = process.pid
            logger.info(f"Запущен процесс с PID: {pid}")

            # Проверяем через find_bot_process и также напрямую
            for attempt in range(10):  # Увеличено до 10 попыток
                bot_running, found_pid, _ = find_bot_process()
                if bot_running:
                    logger.info(f"Бот успешно запущен, обнаружен с PID: {found_pid}")
                    return True

                # Проверяем, существует ли процесс с нашим PID
                if psutil.pid_exists(pid):
                    logger.info(f"Процесс {pid} существует, но не определяется как бот")

                time.sleep(1)

            logger.error("Бот не обнаружен в списке процессов после запуска")
            return False

        except Exception as e:
            logger.error(f"Ошибка при запуске процесса: {e}")
            return False

    except Exception as e:
        logger.error(f"Ошибка при запуске бота: {e}")
        return False


def stop_bot() -> bool:
    """Останавливает бота, если он запущен."""
    try:
        bot_running, pid, _ = find_bot_process()
        logger.info(f"Попытка остановки бота (запущен: {bot_running}, PID: {pid})")

        if bot_running and pid:
            # Попытка корректного завершения
            logger.info(f"Отправка SIGTERM процессу {pid}")
            try:
                os.kill(pid, signal.SIGTERM)

                # Ждем завершения
                for _ in range(10):  # 10 секунд ожидания
                    time.sleep(1)
                    if not psutil.pid_exists(pid):
                        logger.info(f"Процесс {pid} успешно завершен")
                        break

                # Если процесс все еще существует, завершаем принудительно
                if psutil.pid_exists(pid):
                    logger.warning(f"Процесс {pid} не завершился. Отправка SIGKILL")
                    os.kill(pid, signal.SIGKILL)
                    time.sleep(1)
            except ProcessLookupError:
                logger.info(f"Процесс {pid} уже завершен")
            except Exception as e:
                logger.error(f"Ошибка при отправке сигнала процессу {pid}: {e}")
                return False

            # Обновляем статус файл
            now = datetime.now()
            status_data = {
                'running': False,
                'forwarder_active': False,
                'bot_active': False,
                'updated_at': now.isoformat(),
                'start_time': None,
                'uptime_seconds': 0,
                'uptime': 'Неизвестно'
            }
            update_status_file(status_data)

            # Сбрасываем кэш
            _update_process_cache(False, None, None)
            return True

        logger.warning("Попытка остановки бота, который не запущен")
        return True  # Считаем успешным, если бот и так не запущен
    except Exception as e:
        logger.error(f"Ошибка при остановке бота: {e}")
        return False

def restart_bot() -> bool:
    """
    Перезапускает бота с подтверждением запуска.

    Returns:
        True если бот успешно перезапущен, иначе False
    """
    logger.info("Перезапуск бота...")

    # Останавливаем бота
    stop_successful = stop_bot()
    if not stop_successful:
        logger.warning("Не удалось корректно остановить бота, продолжаем с запуском")

    # Делаем паузу для корректного освобождения ресурсов
    time.sleep(3)

    # Запускаем бота
    start_successful = start_bot()
    if not start_successful:
        logger.error("Не удалось запустить бота после остановки")
        return False

    # Проверяем, действительно ли бот запущен
    for i in range(5):  # Проверяем 5 раз с интервалом 1 секунда
        bot_running, _, _ = find_bot_process()
        if bot_running:
            logger.info("Бот успешно перезапущен")
            return True
        time.sleep(1)

    logger.error("Бот не обнаружен в списке процессов после запуска")
    return False


def is_bot_running() -> bool:
    """
    Быстрая проверка, запущен ли бот.

    Returns:
        True если бот запущен, иначе False
    """
    bot_running, _, _ = find_bot_process()
    return bot_running