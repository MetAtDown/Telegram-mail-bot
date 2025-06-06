import sys
import threading
import time
import json
import signal
import os
import gc
import queue
import psutil
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Any, Optional, List

from src.utils.logger import get_logger
from src.config import settings

# Настройка логгера с ротацией
logger = get_logger("email_telegram_system")


class EmailTelegramSystem:
    def __init__(self):
        """Инициализация системы для работы с почтой и телеграм-ботом."""
        self.db_manager = None
        self.forwarder = None
        self.bot_handler = None
        self.forwarder_thread = None
        self.bot_thread = None
        self.running = False
        self.start_time = datetime.now()

        # Мониторинг и контроль
        self.monitor_thread = None
        self.status_update_thread = None
        self.threads_health = {}
        self.stop_event = threading.Event()

        # Очереди для межпоточного взаимодействия
        self.command_queue = queue.Queue()

        # Блокировки для доступа к общим ресурсам
        self.status_lock = threading.RLock()

        # Новые атрибуты для контроля перезапусков
        self._bot_restarting = False  # Флаг текущего перезапуска
        self._bot_restart_count = 0  # Счетчик перезапусков
        self._last_bot_restart = datetime.min  # Время последнего перезапуска

        # Проверка и создание директории для данных
        self._ensure_data_directory()

    def _ensure_data_directory(self) -> None:
        """Проверяет и создает директорию для данных."""
        try:
            data_dir = Path(settings.DATA_DIR)
            data_dir.mkdir(parents=True, exist_ok=True)
            logger.debug(f"Директория данных проверена: {data_dir}")
        except Exception as e:
            logger.error(f"Ошибка при создании директории данных: {e}")
            # Если не удается создать основную директорию, используем временную
            settings.DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
            Path(settings.DATA_DIR).mkdir(parents=True, exist_ok=True)
            logger.info(f"Использование альтернативной директории данных: {settings.DATA_DIR}")

    def update_status_file(self) -> None:
        """Обновляет файл статуса системы атомарно."""
        try:
            with self.status_lock:
                status_file = Path(settings.DATA_DIR) / 'bot_status.json'

                # Рассчитываем время работы системы
                current_time = datetime.now()
                start_time = getattr(self, 'start_time', current_time)
                uptime = current_time - start_time

                # Проверка работоспособности потоков
                forwarder_active = self.forwarder_thread is not None and self.forwarder_thread.is_alive()
                bot_active = self.bot_thread is not None and self.bot_thread.is_alive()

                # Формируем данные статуса
                status_data = {
                    'running': self.running,
                    'forwarder_active': forwarder_active,
                    'bot_active': bot_active,
                    'updated_at': current_time.isoformat(),
                    'start_time': start_time.timestamp(),
                    'uptime_seconds': int(uptime.total_seconds()),
                    'uptime': str(timedelta(seconds=int(uptime.total_seconds()))),
                    'pid': os.getpid(),
                    'threads': {
                        'total': threading.active_count(),
                        'health': self.threads_health
                    },
                    'memory_usage': {
                        'percent': psutil.Process(os.getpid()).memory_percent(),
                        'rss': psutil.Process(os.getpid()).memory_info().rss // (1024 * 1024)  # МБ
                    }
                }

                # Записываем во временный файл и атомарно переименовываем
                temp_file = status_file.with_suffix('.tmp')
                with open(temp_file, 'w', encoding='utf-8') as f:
                    json.dump(status_data, f, ensure_ascii=False, indent=2)

                # Атомарное переименование файла
                temp_file.replace(status_file)

                logger.debug("Файл статуса обновлен")
        except Exception as e:
            logger.error(f"Ошибка при обновлении файла статуса: {e}")

    def _status_update_worker(self) -> None:
        """Поток для регулярного обновления файла статуса."""
        logger.info("Запущен поток обновления статуса")
        update_interval = 30  # секунд

        while not self.stop_event.is_set():
            try:
                self.update_status_file()

                # Ожидаем с проверкой события остановки
                self.stop_event.wait(update_interval)
            except Exception as e:
                logger.error(f"Ошибка в потоке обновления статуса: {e}")
                # Продолжаем работу даже при ошибке
                time.sleep(5)

        logger.info("Поток обновления статуса завершен")

    def start_forwarder(self) -> bool:
        """
        Запуск обработчика почты в отдельном потоке с мониторингом состояния.

        Returns:
            True если обработчик запущен успешно, иначе False
        """
        try:
            logger.info("Запуск сервиса проверки почты...")
            from src.core.email_handler import EmailTelegramForwarder

            # Создаем новый экземпляр форвардера
            self.forwarder = EmailTelegramForwarder(
                db_manager=self.db_manager
            )

            # Проверка соединений перед запуском
            connections = self.forwarder.test_connections()

            if not connections["mail"]:
                logger.error("Не удалось подключиться к почтовому серверу. Проверьте настройки.")
                return False

            if not connections["telegram"]:
                logger.error("Не удалось подключиться к Telegram API. Проверьте токен.")
                return False

            # Запуск планировщика в отдельном потоке с обработкой исключений
            self.forwarder_thread = threading.Thread(
                target=self._forwarder_worker,
                name="EmailForwarderThread",
                daemon=True
            )
            self.forwarder_thread.start()

            # Обновляем статус потока
            self.threads_health["forwarder"] = {
                "status": "started",
                "last_active": datetime.now().isoformat()
            }

            logger.info("Сервис проверки почты успешно запущен")
            return True
        except Exception as e:
            logger.error(f"Ошибка при запуске сервиса проверки почты: {e}")
            logger.exception(e)
            return False

    def _forwarder_worker(self) -> None:
        """Безопасная обертка для запуска форвардера с обработкой исключений."""
        try:
            logger.info("Поток форвардера запущен")

            # Обновляем состояние
            with self.status_lock:
                self.threads_health["forwarder"] = {
                    "status": "running",
                    "last_active": datetime.now().isoformat()
                }

            # Запускаем планировщик
            self.forwarder.start_scheduler(settings.CHECK_INTERVAL)
        except Exception as e:
            logger.error(f"Критическая ошибка в потоке форвардера: {e}")
            logger.exception(e)

            # Обновляем состояние
            with self.status_lock:
                self.threads_health["forwarder"] = {
                    "status": "error",
                    "last_active": datetime.now().isoformat(),
                    "error": str(e)
                }

            # Пытаемся перезапустить форвардер, если система еще работает
            if self.running and not self.stop_event.is_set():
                logger.info("Попытка перезапуска форвардера...")
                self.command_queue.put(("restart_forwarder", {}))

    def start_telegram_bot(self) -> bool:
        """
        Запуск Telegram бота в отдельном потоке с мониторингом состояния.

        Returns:
            True если бот запущен успешно, иначе False
        """
        try:
            logger.info("Запуск Telegram бота...")
            from src.core.telegram_handler import EmailBotHandler

            # Создаем новый экземпляр обработчика бота
            self.bot_handler = EmailBotHandler(
                db_manager=self.db_manager
            )

            # Запуск бота в отдельном потоке с обработкой исключений
            self.bot_thread = threading.Thread(
                target=self._bot_worker,
                name="TelegramBotThread",
                daemon=True
            )
            self.bot_thread.start()

            # Обновляем статус потока
            self.threads_health["telegram_bot"] = {
                "status": "started",
                "last_active": datetime.now().isoformat()
            }

            logger.info("Telegram бот успешно запущен")
            return True
        except Exception as e:
            logger.error(f"Ошибка при запуске Telegram бота: {e}")
            logger.exception(e)
            return False

    def _bot_worker(self) -> None:
        """Безопасная обертка для запуска бота с обработкой исключений."""
        try:
            logger.info("Поток Telegram бота запущен")

            # Обновляем состояние
            with self.status_lock:
                self.threads_health["telegram_bot"] = {
                    "status": "running",
                    "last_active": datetime.now().isoformat()
                }

            # Запускаем бота
            self.bot_handler.start()
        except Exception as e:
            logger.error(f"Критическая ошибка в потоке Telegram бота: {e}")
            logger.exception(e)

            # Обновляем состояние
            with self.status_lock:
                self.threads_health["telegram_bot"] = {
                    "status": "error",
                    "last_active": datetime.now().isoformat(),
                    "error": str(e)
                }

            # Пытаемся перезапустить бота, если система еще работает
            if self.running and not self.stop_event.is_set():
                logger.info("Попытка перезапуска Telegram бота...")
                self.command_queue.put(("restart_bot", {}))

    def validate_env_variables(self) -> bool:
        """
        Проверка наличия всех необходимых переменных окружения.

        Returns:
            True если все переменные найдены, иначе False
        """
        required_vars = ['TELEGRAM_TOKEN', 'EMAIL_ACCOUNT', 'EMAIL_PASSWORD']
        missing_vars = []

        for var in required_vars:
            if not getattr(settings, var, None):
                missing_vars.append(var)

        if missing_vars:
            logger.critical(f"Отсутствуют обязательные переменные окружения: {', '.join(missing_vars)}")
            return False

        return True

    def _init_database(self) -> bool:
        """
        Инициализация менеджера базы данных с обработкой ошибок.

        Returns:
            True если инициализация прошла успешно, иначе False
        """
        try:
            from src.db.manager import DatabaseManager
            self.db_manager = DatabaseManager()
            logger.info("Менеджер базы данных успешно инициализирован")
            return True
        except Exception as e:
            logger.error(f"Ошибка при инициализации менеджера базы данных: {e}")
            logger.exception(e)
            return False

    def start(self) -> bool:
        """
        Запуск всей системы с мониторингом компонентов.

        Returns:
            True если система запущена успешно, иначе False
        """
        logger.info("Запуск системы пересылки почты в Telegram...")

        try:
            # Проверяем наличие дубликатов и завершаем их
            self._check_duplicate_processes()

            # Проверка переменных окружения
            if not self.validate_env_variables():
                return False

            # Инициализация менеджера базы данных
            if not self._init_database():
                return False

            # Запускаем поток обновления статуса
            self.stop_event.clear()
            self.status_update_thread = threading.Thread(
                target=self._status_update_worker,
                name="StatusUpdateThread",
                daemon=True
            )
            self.status_update_thread.start()

            # Запуск форвардера почты
            forwarder_started = self.start_forwarder()

            # Запуск Telegram бота
            bot_started = self.start_telegram_bot()

            if forwarder_started and bot_started:
                self.running = True
                self.start_time = datetime.now()

                # Запуск мониторинга
                self.monitor_thread = threading.Thread(
                    target=self._monitor_worker,
                    name="MonitorThread",
                    daemon=True
                )
                self.monitor_thread.start()

                # Запуск обработчика команд
                self.command_thread = threading.Thread(
                    target=self._command_worker,
                    name="CommandThread",
                    daemon=True
                )
                self.command_thread.start()

                # Обновляем файл статуса
                self.update_status_file()

                logger.info("Система успешно запущена")
                return True
            else:
                logger.error("Не удалось запустить систему полностью")
                self.stop()
                return False

        except Exception as e:
            logger.critical(f"Критическая ошибка при запуске системы: {e}")
            logger.exception(e)
            self.stop()
            return False

    def stop(self) -> None:
        """Остановка системы с корректным завершением всех компонентов."""
        logger.info("Остановка системы...")

        # Устанавливаем флаг остановки
        self.stop_event.set()
        self.running = False

        try:
            # Останавливаем компоненты
            self._stop_components()

            # Ждем завершения всех потоков
            self._wait_for_threads()

            # Обновляем статус перед закрытием
            self.update_status_file()

            # Закрываем соединение с базой данных
            if self.db_manager:
                try:
                    self.db_manager.shutdown()
                except Exception as e:
                    logger.error(f"Ошибка при закрытии соединения с базой данных: {e}")
        except Exception as e:
            logger.error(f"Ошибка при остановке системы: {e}")
        finally:
            # Принудительно вызываем сборщик мусора
            gc.collect()
            logger.info("Система остановлена")

    def _stop_components(self) -> None:
        """Останавливает все компоненты системы."""
        # Останавливаем форвардер
        if self.forwarder:
            try:
                logger.info("Остановка форвардера...")
                self.forwarder.shutdown()
            except Exception as e:
                logger.error(f"Ошибка при остановке форвардера: {e}")

        # Останавливаем бот (если есть метод stop)
        if self.bot_handler and hasattr(self.bot_handler, 'stop'):
            try:
                logger.info("Остановка Telegram бота...")
                self.bot_handler.stop()
            except Exception as e:
                logger.error(f"Ошибка при остановке Telegram бота: {e}")

    def _wait_for_threads(self) -> None:
        """Ожидает корректного завершения всех потоков."""
        threads_to_wait = []

        # Добавляем потоки в список ожидания
        if self.forwarder_thread and self.forwarder_thread.is_alive():
            threads_to_wait.append(("forwarder", self.forwarder_thread))

        if self.bot_thread and self.bot_thread.is_alive():
            threads_to_wait.append(("bot", self.bot_thread))

        if self.monitor_thread and self.monitor_thread.is_alive():
            threads_to_wait.append(("monitor", self.monitor_thread))

        if self.status_update_thread and self.status_update_thread.is_alive():
            threads_to_wait.append(("status_update", self.status_update_thread))

        # Проверяем, существует ли command_thread, перед добавлением
        if hasattr(self, 'command_thread') and self.command_thread and self.command_thread.is_alive():
            threads_to_wait.append(("command", self.command_thread))

        # Ждем завершения потоков с таймаутом
        for name, thread in threads_to_wait:
            logger.info(f"Ожидание завершения потока {name}...")
            thread.join(timeout=5)
            if thread.is_alive():
                logger.warning(f"Поток {name} не завершился за отведенное время")

    def _restart_forwarder(self) -> bool:
        """
        Перезапускает форвардер почты.

        Returns:
            True если перезапуск успешен, иначе False
        """
        logger.info("Перезапуск форвардера почты...")

        # Останавливаем текущий форвардер
        if self.forwarder:
            try:
                self.forwarder.shutdown()
            except Exception as e:
                logger.error(f"Ошибка при остановке форвардера: {e}")

        # Ждем завершения потока
        if self.forwarder_thread and self.forwarder_thread.is_alive():
            self.forwarder_thread.join(timeout=5)
            if self.forwarder_thread.is_alive():
                logger.warning("Поток форвардера не завершился за отведенное время")

        # Сброс объектов
        self.forwarder = None
        self.forwarder_thread = None

        # Вызываем сборщик мусора
        gc.collect()

        # Запускаем новый форвардер
        return self.start_forwarder()

    def _restart_bot(self) -> bool:
        """Перезапускает Telegram бота с улучшенной обработкой ресурсов."""
        # Атомарно устанавливаем флаг перезапуска, чтобы избежать гонок условий
        with self.status_lock:
            if getattr(self, '_bot_restarting', False):
                logger.warning("Перезапуск бота уже выполняется, пропускаем...")
                return False
            self._bot_restarting = True

        try:
            logger.info("Перезапуск Telegram бота...")
            import time
            import gc

            # Остановка текущего бота со всеми ресурсами
            if self.bot_handler and hasattr(self.bot_handler, 'stop'):
                try:
                    logger.info("Остановка текущего экземпляра бота...")
                    self.bot_handler.stop()
                except Exception as e:
                    logger.error(f"Ошибка при остановке Telegram бота: {e}")

            # Ожидание завершения всех связанных потоков
            if self.bot_thread and self.bot_thread.is_alive():
                logger.info("Ожидание завершения потока бота...")
                self.bot_thread.join(timeout=20)  # Увеличенный таймаут для гарантии завершения
                if self.bot_thread.is_alive():
                    logger.warning("Поток Telegram бота не завершился за отведенное время")

            # Сброс объектов
            old_bot = self.bot_handler
            self.bot_handler = None
            self.bot_thread = None
            del old_bot  # Явное удаление старой ссылки

            # Вызов сборщика мусора
            logger.debug("Вызов сборщика мусора для освобождения ресурсов...")
            gc.collect()

            # Пауза перед созданием нового экземпляра
            delay = 10  # Увеличенная пауза
            logger.info(f"Ожидание {delay} секунд перед созданием нового экземпляра...")
            time.sleep(delay)

            # Запуск нового бота
            logger.info("Запуск нового экземпляра Telegram бота...")
            success = self.start_telegram_bot()

            if success:
                logger.info("Бот успешно перезапущен")
            else:
                logger.error("Не удалось перезапустить бота")

            return success
        except Exception as e:
            logger.error(f"Ошибка при перезапуске бота: {e}", exc_info=True)
            return False
        finally:
            # Снимаем флаг перезапуска
            with self.status_lock:
                self._bot_restarting = False
                logger.debug("Флаг перезапуска снят")

    def _command_worker(self) -> None:
        """Обработчик команд для межпоточного взаимодействия."""
        logger.info("Запущен обработчик команд")

        while not self.stop_event.is_set():
            try:
                # Получаем команду из очереди с таймаутом
                try:
                    command, params = self.command_queue.get(timeout=1)
                except queue.Empty:
                    continue

                # Обрабатываем команду
                logger.info(f"Получена команда: {command}")

                if command == "restart_forwarder":
                    self._restart_forwarder()
                elif command == "restart_bot":
                    self._restart_bot()
                elif command == "stop_system":
                    self.stop()
                else:
                    logger.warning(f"Неизвестная команда: {command}")

                # Отмечаем команду как выполненную
                self.command_queue.task_done()
            except Exception as e:
                logger.error(f"Ошибка в обработчике команд: {e}")
                time.sleep(1)

        logger.info("Обработчик команд завершен")

    def _monitor_worker(self) -> None:
        """Поток мониторинга состояния системы с функциями самовосстановления."""
        logger.info("Запущен мониторинг системы")
        check_interval = 60  # секунд

        while not self.stop_event.is_set() and self.running:
            try:
                # Проверяем состояние потоков
                self._check_threads_health()

                # Проверяем потребление ресурсов
                self._check_resource_usage()

                # Проверяем, не "зависли" ли компоненты
                self._check_components_activity()

                # Ожидаем с проверкой события остановки
                self.stop_event.wait(check_interval)
            except Exception as e:
                logger.error(f"Ошибка в мониторинге системы: {e}")
                # В случае ошибки делаем более длительную паузу
                time.sleep(300)

        logger.info("Мониторинг системы завершен")

    def _check_duplicate_processes(self) -> None:
        """Проверяет наличие дублирующихся процессов бота и завершает их."""
        try:
            import psutil

            current_process = psutil.Process()
            current_pid = current_process.pid
            bot_processes = []

            # Ищем все процессы Python, которые могут быть нашим ботом
            for proc in psutil.process_iter(['pid', 'name', 'cmdline']):
                try:
                    # Ищем процессы по ключевым словам в командной строке
                    if 'python' in proc.info['name'].lower() and proc.info['cmdline']:
                        cmdline = ' '.join(proc.info['cmdline'])
                        if 'system.py' in cmdline and proc.info['pid'] != current_pid:
                            bot_processes.append(proc)
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue

            # Если найдены дублирующиеся процессы, логируем и пытаемся их завершить
            if bot_processes:
                logger.warning(f"Обнаружено {len(bot_processes)} дублирующихся процессов бота.")
                for proc in bot_processes:
                    try:
                        logger.info(f"Завершение дублирующегося процесса бота PID={proc.info['pid']}")
                        proc.terminate()  # Более мягкое завершение
                    except Exception as e:
                        logger.error(f"Не удалось завершить процесс {proc.info['pid']}: {e}")

        except ImportError:
            logger.debug("Модуль psutil не установлен. Проверка дублирующихся процессов отключена.")
        except Exception as e:
            logger.error(f"Ошибка при проверке дублирующихся процессов: {e}")

    def _check_threads_health(self) -> None:
        """Проверяет состояние потоков и перезапускает их при необходимости."""
        current_time = datetime.now()

        # Проверяем время последнего перезапуска и ограничиваем частоту
        last_restart = getattr(self, '_last_bot_restart', datetime.min)
        min_restart_interval = timedelta(minutes=10)  # Минимум 10 минут между перезапусками

        if (current_time - last_restart) < min_restart_interval:
            return  # Пропускаем проверку, если недавно был перезапуск

        # Проверяем поток форвардера
        if self.forwarder_thread is None or not self.forwarder_thread.is_alive():
            logger.warning("Поток форвардера почты остановлен. Перезапуск...")
            success = self._restart_forwarder()

            with self.status_lock:
                if success:
                    self.threads_health["forwarder"] = {
                        "status": "restarted",
                        "last_active": current_time.isoformat(),
                        "restart_count": self.threads_health.get("forwarder", {}).get("restart_count", 0) + 1
                    }
                else:
                    self.threads_health["forwarder"] = {
                        "status": "failed",
                        "last_active": current_time.isoformat(),
                        "restart_count": self.threads_health.get("forwarder", {}).get("restart_count", 0) + 1
                    }

        # Проверяем поток Telegram бота
        if self.bot_thread is None or not self.bot_thread.is_alive():
            logger.warning("Поток Telegram бота остановлен. Перезапуск...")
            success = self._restart_bot()

            # Запоминаем время этого перезапуска
            self._last_bot_restart = current_time

            with self.status_lock:
                if success:
                    self.threads_health["telegram_bot"] = {
                        "status": "restarted",
                        "last_active": current_time.isoformat(),
                        "restart_count": self.threads_health.get("telegram_bot", {}).get("restart_count", 0) + 1
                    }
                else:
                    self.threads_health["telegram_bot"] = {
                        "status": "failed",
                        "last_active": current_time.isoformat(),
                        "restart_count": self.threads_health.get("telegram_bot", {}).get("restart_count", 0) + 1
                    }

    def _check_resource_usage(self) -> None:
        """Проверяет потребление ресурсов и принимает меры при необходимости."""
        try:

            process = psutil.Process()

            # Отслеживаем количество перезапусков бота
            restart_count = getattr(self, '_bot_restart_count', 0)
            last_restart = getattr(self, '_last_bot_restart', datetime.min)
            now = datetime.now()

            # Ограничиваем частоту перезапусков (не чаще 1 раза в 10 минут)
            if (now - last_restart).total_seconds() < 600:  # 600 секунд = 10 минут
                return

            # Проверяем потребление памяти
            memory_percent = process.memory_percent()
            if memory_percent > 75:  # Если используется более 75% доступной памяти
                logger.warning(f"Высокое потребление памяти: {memory_percent:.1f}%. Выполняем сборку мусора.")
                gc.collect()

                # Проверяем улучшилась ли ситуация
                new_memory_percent = process.memory_percent()
                if new_memory_percent > 70:  # Если все еще высокое потребление
                    logger.warning(
                        f"Потребление памяти остается высоким: {new_memory_percent:.1f}%. Планируем перезапуск бота.")
                    self._bot_restart_count = restart_count + 1
                    self._last_bot_restart = now
                    self.command_queue.put(("restart_bot", {}))

            # Проверяем загрузку CPU
            cpu_percent = process.cpu_percent(interval=0.5)
            if cpu_percent > 80:  # Если загрузка CPU более 80%
                logger.warning(f"Высокая загрузка CPU: {cpu_percent:.1f}%. Проверяем потоки.")

                # Логируем информацию о потоках
                thread_count = threading.active_count()
                logger.info(f"Активных потоков: {thread_count}")

                # Если загрузка критически высокая и прошло достаточно времени с последнего перезапуска
                if cpu_percent > 90 and (now - last_restart).total_seconds() > 1200:  # 20 минут
                    logger.warning("Критически высокая загрузка CPU. Планируем перезапуск бота.")
                    self._bot_restart_count = restart_count + 1
                    self._last_bot_restart = now
                    self.command_queue.put(("restart_bot", {}))

        except ImportError:
            logger.warning("Модуль psutil не установлен. Мониторинг ресурсов отключен.")
        except Exception as e:
            logger.error(f"Ошибка при проверке потребления ресурсов: {e}")

    def _check_components_activity(self) -> None:
        """Проверяет, не "зависли" ли компоненты, по их последней активности."""
        # В будущем можно реализовать механизм проверки активности компонентов
        # на основе их последней успешной операции (например, проверки почты или обработки сообщения)
        pass

def check_maintenance_mode():
    maintenance_file = "/app/data/.maintenance_mode"
    if os.path.exists(maintenance_file):
        try:
            with open(maintenance_file, 'r') as f:
                timestamp = int(f.read().strip())
            # Если файл старше 15 минут, игнорируем его
            if time.time() - timestamp > 900:
                os.remove(maintenance_file)
                return False
            return True
        except:
            return True
    return False

def signal_handler(sig: int, frame) -> None:
    """
    Обработчик сигналов для корректного завершения работы.

    Args:
        sig: Номер сигнала
        frame: Текущий фрейм выполнения
    """
    global system
    if system:
        logger.info(f"Получен сигнал {sig}, выполняется корректное завершение работы...")
        system.stop()
    sys.exit(0)


# Глобальная переменная для доступа из обработчика сигналов
system = None


def main() -> None:
    """Основная функция для запуска системы."""
    global system

    try:
        # Регистрация обработчиков сигналов
        signal.signal(signal.SIGTERM, signal_handler)
        signal.signal(signal.SIGINT, signal_handler)

        # Инициализация и запуск системы
        system = EmailTelegramSystem()

        if system.start():
            # Основной цикл
            try:
                # Проверка файла обслуживания
                while system.running:
                    if check_maintenance_mode():
                        logger.info("Работа приостановлена из-за обслуживания БД")
                        time.sleep(5)
                        continue

                    time.sleep(1)
            except KeyboardInterrupt:
                logger.info("Работа программы прервана пользователем")
                system.stop()
        else:
            logger.error("Не удалось запустить систему")
            sys.exit(1)
    except Exception as e:
        logger.critical(f"Критическая ошибка: {e}")
        logger.exception(e)
        if system:
            system.stop()
        sys.exit(1)


if __name__ == "__main__":
    main()