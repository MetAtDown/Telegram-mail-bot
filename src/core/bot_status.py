import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Tuple
import psutil

from src.utils.logger import get_logger
from src.config import settings

logger = get_logger("bot_status")

# --- НАСТРОЙКИ ---
# Получаем имя программы из настроек или используем 'bot' по умолчанию
SUPERVISOR_PROGRAM_NAME = getattr(settings, 'SUPERVISOR_BOT_PROGRAM_NAME', 'bot')
# Путь к старому PID файлу (только для fallback метода)
PID_FILE = Path(settings.DATA_DIR) / '.bot_pid'

# --- Хелпер для вызова supervisorctl ---

def _run_supervisorctl(action: str, program_name: str = SUPERVISOR_PROGRAM_NAME) -> Tuple[bool, str]:
    """
    Выполняет команду supervisorctl и возвращает кортеж (успех, вывод).
    Успех определяется не только кодом возврата, но и анализом вывода для start/stop.

    Args:
        action (str): Команда для supervisorctl (start, stop, status, restart).
        program_name (str): Имя программы в Supervisor.

    Returns:
        Tuple[bool, str]: Кортеж (True если успешно, False если ошибка, Строка с выводом stdout/stderr).
    """
    if not program_name:
        err_msg = "Имя программы Supervisor не задано (SUPERVISOR_BOT_PROGRAM_NAME в настройках)."
        logger.error(err_msg)
        return False, err_msg

    try:
        # Формируем команду
        command = ['supervisorctl', action, program_name]
        logger.info(f"Выполнение команды Supervisor: {' '.join(command)}")

        # Выполняем с таймаутом, не бросаем исключение при ошибке (check=False)
        result = subprocess.run(
            command,
            check=False,          # Не бросать исключение при ненулевом коде возврата
            capture_output=True,  # Захватывать stdout и stderr
            text=True,            # Работать с текстом (Python 3.7+)
            timeout=15            # Таймаут 15 секунд
        )

        # Объединяем stdout и stderr для анализа и логирования
        output = (result.stdout or "").strip()
        error_output = (result.stderr or "").strip()
        full_output = f"STDOUT:\n{output}\nSTDERR:\n{error_output}".strip()
        logger.debug(f"Supervisorctl {action} {program_name} raw output:\n{full_output}")

        # Определяем успех операции
        success = False
        if result.returncode == 0:
            # Команда выполнена без системной ошибки
            if action == 'status':
                 # Для status любой успешный вызов - это успех
                 success = True
            elif action == 'start':
                 if f"{program_name}: started" in output or "already started" in output:
                      success = True
                 # Если нет ошибки в stderr, тоже считаем успехом (может не быть вывода)
                 elif not error_output:
                      success = True
            elif action == 'stop':
                 if f"{program_name}: stopped" in output or "not running" in output:
                      success = True
                 elif not error_output:
                      success = True
            elif action == 'restart':
                 # Успешный restart должен содержать stopped и started
                 if "stopped" in output and "started" in output:
                      success = True
                 # Или если он только запустил остановленный процесс
                 elif "started" in output and ("not running" in output or "stopped" not in output):
                      success = True
                 elif not error_output:
                     success = True # Если нет ошибок, предполагаем успех
            else:
                 # Для других команд просто проверяем код возврата
                 success = True
        else:
            # Команда завершилась с ошибкой (ненулевой код возврата)
            # Проверяем особые случаи, которые могут быть не ошибками
             if action == 'stop' and "not running" in error_output:
                  success = True # Пытались остановить то, что не запущено - это нормально
             elif action == 'start' and "already started" in error_output:
                  success = True # Пытались запустить то, что уже запущено - нормально
             elif f"No such process group '{program_name}'" in error_output:
                  logger.error(f"Программа '{program_name}' не найдена в Supervisor.")
                  success = False # Явная ошибка конфигурации
             else:
                  success = False # Другая ошибка

        # Логируем ошибку, если операция не удалась
        if not success:
            logger.error(f"Команда 'supervisorctl {action} {program_name}' НЕ УДАЛАСЬ. Code: {result.returncode}, Output: {full_output}")

        # Возвращаем результат и полный вывод
        return success, full_output

    except FileNotFoundError:
        err_msg = f"Команда 'supervisorctl' не найдена. Убедитесь, что supervisor установлен и доступен в $PATH контейнера/системы."
        logger.error(err_msg)
        return False, err_msg
    except subprocess.TimeoutExpired:
        err_msg = f"Тайм-аут ({15} сек) выполнения команды 'supervisorctl {action} {program_name}'."
        logger.error(err_msg)
        return False, err_msg
    except Exception as e:
        err_msg = f"Неожиданная ошибка при выполнении 'supervisorctl {action} {program_name}': {e}"
        logger.error(err_msg, exc_info=True)
        return False, err_msg


# --- Основные функции ---

def start_bot() -> bool:
    """Запускает бота через Supervisor."""
    logger.info(f"Отправка команды Supervisor на запуск программы '{SUPERVISOR_PROGRAM_NAME}'...")
    success, output = _run_supervisorctl('start')
    if success:
        logger.info(f"Запрос на запуск успешно отправлен Supervisor'у. Ответ: {output}")
    # Ошибка уже залогирована в _run_supervisorctl
    return success

def stop_bot() -> bool:
    """Останавливает бота через Supervisor."""
    logger.info(f"Отправка команды Supervisor на остановку программы '{SUPERVISOR_PROGRAM_NAME}'...")
    success, output = _run_supervisorctl('stop')
    if success:
        logger.info(f"Запрос на остановку успешно отправлен Supervisor'у. Ответ: {output}")
        # Даем Supervisor'у время на выполнение остановки
        time.sleep(1.5)
        # Удаляем старый PID файл, если он вдруг остался
        PID_FILE.unlink(missing_ok=True)
    # Ошибка уже залогирована в _run_supervisorctl
    return success

def restart_bot() -> bool:
    """Перезапускает бота через Supervisor."""
    logger.info(f"Отправка команды Supervisor на перезапуск программы '{SUPERVISOR_PROGRAM_NAME}'...")
    success, output = _run_supervisorctl('restart')
    if success:
        logger.info(f"Запрос на перезапуск успешно отправлен Supervisor'у. Ответ: {output}")
        time.sleep(1) # Небольшая пауза после запроса
    # Ошибка уже залогирована в _run_supervisorctl
    return success

def get_bot_status(bypass_cache=False) -> dict: # bypass_cache не используется с supervisorctl
    """
    Получает статус бота через Supervisor.
    При ошибках supervisorctl пытается использовать резервный метод (PID файл).
    """
    now_iso = datetime.now().isoformat()
    status_info = {
        'running': False,
        'forwarder_active': False, # Считаем эквивалентным running
        'bot_active': False,       # Считаем эквивалентным running
        'pid': None,
        'uptime': 'Неизвестно',
        'uptime_seconds': 0,
        'last_check': now_iso,
        'error': None,
        'status_source': 'supervisor' # Источник по умолчанию
    }

    # Выполняем команду supervisorctl status
    success, output = _run_supervisorctl('status')

    # --- Обработка ошибок supervisorctl и Fallback ---
    if not success:
        if "'supervisorctl' не найдена" in output:
            status_info['error'] = "supervisorctl не найден"
            status_info['status_source'] = 'pid_fallback_supervisor_missing'
            logger.warning(f"{status_info['error']}. Используется резервный метод (PID).")
            status_info.update(get_bot_status_pid_fallback()) # Обновляем словарь результатом fallback
            return status_info # Возвращаем результат fallback
        elif f"No such process group '{SUPERVISOR_PROGRAM_NAME}'" in output \
          or f"no such process group" in output: # Доп. проверка
            status_info['error'] = f"Программа '{SUPERVISOR_PROGRAM_NAME}' не найдена в Supervisor."
            status_info['status_source'] = 'pid_fallback_supervisor_no_program'
            logger.error(status_info['error'])
            # Пытаемся найти процесс по PID файлу в этом случае
            status_info.update(get_bot_status_pid_fallback())
            return status_info # Возвращаем результат fallback
        else:
            # Другая ошибка supervisorctl
            status_info['error'] = f"Ошибка supervisorctl status: {output}"
            status_info['status_source'] = 'supervisor_error'
            logger.error(status_info['error'])
            logger.warning("Попытка использовать резервный метод (PID).")
            status_info.update(get_bot_status_pid_fallback())
            # Добавляем информацию об исходной ошибке supervisor
            if status_info.get('error'): # Если fallback тоже дал ошибку
                 status_info['error'] = f"Supervisor Error: {output}; Fallback Error: {status_info['error']}"
            else:
                 status_info['error'] = f"Supervisor Error: {output}"
                 status_info['status_source'] = 'pid_fallback_supervisor_error'
            return status_info

    # --- Парсинг успешного вывода supervisorctl status ---
    try:
        status_line = ""
        # Ищем строку, начинающуюся с имени нашей программы
        for line in output.splitlines():
             # Используем strip() для удаления лишних пробелов в начале/конце
             stripped_line = line.strip()
             if stripped_line.startswith(SUPERVISOR_PROGRAM_NAME):
                  # Найдена строка статуса для нашей программы
                  status_line = stripped_line
                  break # Достаточно первой найденной строки

        if not status_line:
             status_info['error'] = f"Не найдена строка статуса для '{SUPERVISOR_PROGRAM_NAME}' в выводе supervisorctl."
             status_info['status_source'] = 'supervisor_parse_error'
             logger.error(status_info['error'])
             # Можно попробовать fallback
             logger.warning("Попытка использовать резервный метод (PID).")
             status_info.update(get_bot_status_pid_fallback())
             if status_info.get('error'):
                 status_info['error'] = f"Supervisor Parse Error; Fallback Error: {status_info['error']}"
             else:
                 status_info['error'] = f"Supervisor Parse Error"
                 status_info['status_source'] = 'pid_fallback_supervisor_parse_error'
             return status_info

        logger.debug(f"Найдена строка статуса Supervisor: {status_line}")

        # Определяем статус и извлекаем PID и uptime
        parts = status_line.split()
        # Первый элемент - имя программы, второй - статус
        current_status = parts[1] if len(parts) > 1 else "UNKNOWN"

        if current_status == "RUNNING":
            status_info['running'] = True
            status_info['forwarder_active'] = True
            status_info['bot_active'] = True
            # Ищем PID
            try:
                pid_index = parts.index('pid') + 1
                # Убираем запятую, если она есть
                status_info['pid'] = int(parts[pid_index].replace(',', ''))
            except (ValueError, IndexError):
                logger.warning(f"Не удалось извлечь PID из строки статуса: {status_line}")
            # Ищем uptime
            try:
                uptime_index = parts.index('uptime') + 1
                status_info['uptime'] = " ".join(parts[uptime_index:])
            except (ValueError, IndexError):
                logger.warning(f"Не удалось извлечь uptime из строки статуса: {status_line}")

        elif current_status in ["STARTING", "BACKOFF"]:
            # Процесс запускается или в состоянии ожидания перед перезапуском
            status_info['running'] = False # Считаем нестабильным
            status_info['error'] = f"Статус Supervisor: {current_status}"
        elif current_status in ["STOPPING", "STOPPED", "EXITED", "FATAL"]:
            # Процесс остановлен или завершился с ошибкой
            status_info['running'] = False
        else:
            # Неизвестный статус
            status_info['running'] = False
            status_info['error'] = f"Неизвестный статус Supervisor: {current_status}"
            logger.warning(status_info['error'])

    except Exception as e:
        status_info['error'] = f"Ошибка парсинга вывода supervisorctl: {e}"
        status_info['status_source'] = 'supervisor_parse_exception'
        logger.error(status_info['error'], exc_info=True)
        # Можно попробовать fallback
        logger.warning("Попытка использовать резервный метод (PID).")
        status_info.update(get_bot_status_pid_fallback())
        if status_info.get('error'):
            status_info['error'] = f"Supervisor Parse Exception: {e}; Fallback Error: {status_info['error']}"
        else:
            status_info['error'] = f"Supervisor Parse Exception: {e}"
            status_info['status_source'] = 'pid_fallback_supervisor_parse_exception'

    return status_info

def is_bot_running() -> bool:
    """
    Быстрая проверка, запущен ли бот (через get_bot_status).

    Returns:
        True если бот запущен (статус RUNNING в Supervisor), иначе False.
    """
    status = get_bot_status()
    # Проверяем именно 'running', т.к. get_bot_status его выставляет
    return status.get('running', False)

# Резервный метод (Fallback)
def get_bot_status_pid_fallback() -> dict:
    """
    Резервный метод получения статуса через PID файл.
    Используется, если supervisorctl недоступен или не находит программу.
    """
    status_info = {
        'running': False, 'pid': None, 'uptime': 'Неизвестно',
        'uptime_seconds': 0, 'last_check': datetime.now().isoformat(), 'error': None,
        'status_source': 'pid_file' # Явно указываем источник
    }
    if not PID_FILE.exists():
         status_info['error'] = f"PID файл не найден: {PID_FILE}"
         return status_info

    try:
        pid_str = PID_FILE.read_text().strip()
        if not pid_str:
            status_info['error'] = "PID файл пуст."
            return status_info

        pid = int(pid_str)
        if psutil.pid_exists(pid):
            p = psutil.Process(pid)
            # Добавим проверку имени процесса или командной строки для надежности
            expected_cmd_part = 'system.py'
            try:
                 cmdline = ' '.join(p.cmdline())
                 proc_name = p.name().lower()
                 # Проверяем, что это python и содержит нужный скрипт
                 if ('python' in proc_name or 'python3' in proc_name) and expected_cmd_part in cmdline:
                      status_info['running'] = True
                      status_info['pid'] = pid
                      create_time = datetime.fromtimestamp(p.create_time())
                      uptime_delta = datetime.now() - create_time
                      status_info['uptime_seconds'] = int(uptime_delta.total_seconds())
                      # Форматируем uptime
                      seconds = status_info['uptime_seconds']
                      days, rem = divmod(seconds, 86400)
                      hours, rem = divmod(rem, 3600)
                      minutes, sec = divmod(rem, 60)
                      if days > 0:
                          status_info['uptime'] = f"{int(days)}д {int(hours):02}:{int(minutes):02}:{int(sec):02}"
                      else:
                          status_info['uptime'] = f"{int(hours):02}:{int(minutes):02}:{int(sec):02}"
                 else:
                      status_info['error'] = f"PID {pid} найден, но процесс не соответствует ожидаемому боту (Имя: {proc_name}, CMD: {cmdline[:50]}...)"
                      status_info['running'] = False # Явно указываем, что это не тот процесс
            except (psutil.AccessDenied, OSError) as e:
                 status_info['error'] = f"Нет доступа к информации о процессе {pid}: {e}"
        else:
            status_info['error'] = f"Процесс с PID {pid} из файла не найден."
            # Удаляем старый PID файл
            try:
                PID_FILE.unlink()
                logger.info(f"Удален устаревший PID файл: {PID_FILE}")
            except OSError as e:
                logger.warning(f"Не удалось удалить устаревший PID файл {PID_FILE}: {e}")

    except ValueError:
        status_info['error'] = f"Некорректное значение PID в файле: {PID_FILE}"
    except FileNotFoundError:
         status_info['error'] = f"PID файл не найден: {PID_FILE}"
    except (psutil.Error, IOError) as e:
        status_info['error'] = f"Ошибка доступа к процессу или файлу PID: {e}"
        logger.warning(f"Ошибка доступа при проверке PID файла: {e}")
    except Exception as e:
        status_info['error'] = f"Неожиданная ошибка при проверке PID файла: {e}"
        logger.error(status_info['error'], exc_info=True)

    return status_info

