import telebot
from telebot import types
import threading
import time
import queue
import functools
from typing import Dict, Any, List, Optional, Set, Callable
from datetime import datetime, timedelta
import logging

from src.config import settings
from src.utils.logger import get_logger

# Настройка логирования с ротацией
logger = get_logger("telegram_bot")

# Константы
MAX_RETRIES = 3
RETRY_DELAY = 2  # секунды
RECONNECT_DELAY = 5  # секунды
MAX_MESSAGE_QUEUE = 100
CACHE_REFRESH_INTERVAL = 300  # секунды (5 минут)


def with_retry(max_attempts: int = MAX_RETRIES, delay: int = RETRY_DELAY):
    """Декоратор для повторных попыток выполнения функции."""

    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            last_exception = None
            for attempt in range(max_attempts):
                try:
                    return func(*args, **kwargs)
                except Exception as e:
                    last_exception = e
                    if attempt < max_attempts - 1:
                        wait_time = delay * (2 ** attempt)  # Exponential backoff
                        logger.warning(
                            f"Ошибка при выполнении {func.__name__} (попытка {attempt + 1}/{max_attempts}): "
                            f"{e}. Повтор через {wait_time}с"
                        )
                        time.sleep(wait_time)
                    else:
                        logger.error(f"Не удалось выполнить {func.__name__} после {max_attempts} попыток: {e}")
            raise last_exception

        return wrapper

    return decorator


class EmailBotHandler:
    def __init__(self, db_manager=None):
        """
        Инициализация обработчика телеграм-бота с улучшенной обработкой ошибок и ресурсов.

        Args:
            db_manager: Экземпляр менеджера базы данных
        """
        # Загрузка настроек
        self.telegram_token = settings.TELEGRAM_TOKEN

        if not self.telegram_token:
            logger.error("Не найден токен Telegram в настройках")
            raise ValueError("Отсутствует токен Telegram в настройках")

        # Устанавливаем менеджер базы данных
        if db_manager is None:
            from src.db.manager import DatabaseManager
            self.db_manager = DatabaseManager()
        else:
            self.db_manager = db_manager

        # Инициализация объектов синхронизации
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.message_queue = queue.Queue(maxsize=MAX_MESSAGE_QUEUE)
        self.pending_responses = {}

        # Словарь для кэширования данных о клиентах с временем последнего обновления
        self.client_data = {}
        self.client_data_timestamp = 0

        # Словарь для хранения состояний пользователей
        self.user_states = {}
        self.user_states_timestamp = 0

        # Отслеживание активности пользователей для оптимизации ресурсов
        self.last_activity = {}

        # Флаги и потоки
        self.running = False
        self.polling_thread = None
        self.message_thread = None

        # Инициализация Telegram бота с оптимизированными настройками
        self.bot = self._initialize_bot()

        # Регистрация обработчиков команд
        self.register_handlers()

        # Загрузка данных о клиентах
        self.reload_client_data()

    def _initialize_bot(self) -> telebot.TeleBot:
        """
        Инициализация бота с оптимальными настройками.

        Returns:
            Инициализированный экземпляр TeleBot
        """
        try:
            # Настройка telebot для оптимальной работы
            # Удалён параметр timeout, который не поддерживается текущей версией библиотеки
            bot = telebot.TeleBot(
                self.telegram_token,
                threaded=True,  # Включаем многопоточность
                num_threads=4,  # Оптимальное количество потоков
                parse_mode="Markdown"  # Устанавливаем режим разметки по умолчанию
            )

            logger.info("Telegram бот успешно инициализирован")
            return bot
        except Exception as e:
            logger.error(f"Ошибка при инициализации Telegram бота: {e}")
            raise

    def reload_client_data(self) -> None:
        """Загрузка данных о клиентах из базы данных с кэшированием."""
        current_time = time.time()

        # Проверяем, нужно ли обновлять кэш
        if (current_time - self.client_data_timestamp) < CACHE_REFRESH_INTERVAL and self.client_data:
            logger.debug("Используем кэшированные данные о клиентах")
            return

        try:
            with self.lock:
                # Получаем данные о клиентах
                self.client_data = self.db_manager.get_all_client_data()
                self.client_data_timestamp = current_time

                # Получаем состояния пользователей
                self.user_states = self.db_manager.get_all_users()
                self.user_states_timestamp = current_time

                logger.info(f"Загружены данные для {len(self.client_data)} пользователей")
        except Exception as e:
            logger.error(f"Ошибка при загрузке данных о клиентах: {e}")

            # Если кэш уже есть, используем его
            if not self.client_data:
                logger.error("Не удалось загрузить данные о клиентах и кэш пуст")
                raise
            else:
                logger.warning("Используем устаревшие данные о клиентах из кэша")

    @with_retry()
    def update_client_status(self, chat_id: str, status: bool) -> bool:
        """
        Обновление статуса клиента в базе данных с обработкой ошибок и повторными попытками.

        Args:
            chat_id: Идентификатор чата пользователя
            status: Новый статус (True - включен, False - отключен)

        Returns:
            True если статус успешно обновлен, иначе False
        """
        try:
            result = self.db_manager.update_user_status(chat_id, status)
            if result:
                # Обновляем и в локальном кэше
                with self.lock:
                    self.user_states[chat_id] = status
                logger.info(f"Обновлен статус для chat_id {chat_id}: {'Enable' if status else 'Disable'}")
                return True
            else:
                logger.warning(f"Не удалось обновить статус для chat_id {chat_id}")
                return False
        except Exception as e:
            logger.error(f"Ошибка при обновлении статуса клиента: {e}")
            # Переподнимаем исключение для обработки декоратором @with_retry
            raise

    def get_main_menu_keyboard(self) -> types.ReplyKeyboardMarkup:
        """
        Создание клавиатуры главного меню (кэшируем для оптимизации).

        Returns:
            Объект клавиатуры для Telegram
        """
        try:
            # Создаем клавиатуру как атрибут класса при первом вызове
            if not hasattr(self, '_main_menu_keyboard'):
                markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
                btn_status = types.KeyboardButton('📊 Статус')
                btn_reports = types.KeyboardButton('📋 Мои отчеты')
                btn_enable = types.KeyboardButton('✅ Включить уведомления')
                btn_disable = types.KeyboardButton('❌ Отключить уведомления')
                btn_help = types.KeyboardButton('❓ Помощь')
                markup.add(btn_status, btn_reports, btn_enable, btn_disable, btn_help)
                self._main_menu_keyboard = markup

            return self._main_menu_keyboard
        except Exception as e:
            logger.error(f"Ошибка при создании клавиатуры: {e}")
            # Возвращаем пустую клавиатуру в случае ошибки
            return types.ReplyKeyboardMarkup(resize_keyboard=True)

    def get_status_message(self, chat_id: str, user_name: str) -> str:
        """
        Формирование сообщения о статусе пользователя с актуальными данными.

        Args:
            chat_id: Идентификатор чата пользователя
            user_name: Имя пользователя

        Returns:
            Форматированное сообщение о статусе пользователя
        """
        try:
            # Проверяем, не устарели ли данные
            if time.time() - self.client_data_timestamp > CACHE_REFRESH_INTERVAL:
                self.reload_client_data()

            # Получаем данные из кэша или базы данных
            subjects = self.db_manager.get_user_subjects(chat_id)
            is_enabled = self.db_manager.get_user_status(chat_id)

            # Обновляем локальный кэш
            with self.lock:
                self.user_states[chat_id] = is_enabled

            status = "включены" if is_enabled else "отключены"

            if subjects:
                status_message = (
                    f"Здравствуйте, {user_name}!\n\n"
                    f"Ваш Chat ID: `{chat_id}`\n\n"
                    f"Вы зарегистрированы в системе.\n"
                    f"Уведомления {status}.\n\n"
                )

                if subjects:
                    status_message += "Ваши отчеты:\n"
                    for i, subject in enumerate(subjects, 1):
                        status_message += f"{i}. {subject}\n"
                else:
                    status_message += "У вас пока нет настроенных отчетов."
            else:
                status_message = (
                    f"Здравствуйте, {user_name}!\n\n"
                    f"Ваш Chat ID: `{chat_id}`\n\n"
                    "Вы не зарегистрированы в системе или у вас нет настроенных отчетов.\n"
                    "Для настройки отчетов обратитесь к администратору."
                )

            return status_message
        except Exception as e:
            logger.error(f"Ошибка при формировании сообщения о статусе: {e}")
            return (
                f"Здравствуйте, {user_name}!\n\n"
                f"Ваш Chat ID: `{chat_id}`\n\n"
                "Произошла ошибка при получении информации о вашем статусе.\n"
                "Пожалуйста, попробуйте позже или обратитесь к администратору."
            )

    def register_handlers(self) -> None:
        """Регистрация обработчиков команд бота с оптимизацией обработки."""

        # Создаем обработчики с учетом возможных ошибок

        @self.bot.message_handler(commands=['start'])
        def handle_start(message: types.Message) -> None:
            self._update_user_activity(message.chat.id)
            try:
                chat_id = str(message.chat.id)
                user_name = message.from_user.first_name

                welcome_message = (
                    f"Добро пожаловать, {user_name}!\n\n"
                    f"Ваш Chat ID: `{chat_id}`\n\n"
                    "Этот бот пересылает письма из почты в Telegram по настроенным темам.\n\n"
                    "Используйте кнопки меню или команду /help для получения справки."
                )

                # Используем очередь для отправки сообщений
                self._queue_message(chat_id, welcome_message, reply_markup=self.get_main_menu_keyboard())
                logger.info(f"Пользователь {chat_id} ({user_name}) запустил бота")
            except Exception as e:
                logger.error(f"Ошибка при обработке команды /start: {e}")
                self._handle_command_error(message, e)

        @self.bot.message_handler(commands=['status'])
        def handle_status(message: types.Message) -> None:
            self._update_user_activity(message.chat.id)
            try:
                chat_id = str(message.chat.id)
                user_name = message.from_user.first_name

                status_message = self.get_status_message(chat_id, user_name)
                self._queue_message(chat_id, status_message)
                logger.info(f"Пользователь {chat_id} запросил статус")
            except Exception as e:
                logger.error(f"Ошибка при обработке команды /status: {e}")
                self._handle_command_error(message, e)

        @self.bot.message_handler(commands=['reports'])
        def handle_show_reports(message: types.Message) -> None:
            self._update_user_activity(message.chat.id)
            try:
                chat_id = str(message.chat.id)

                # Проверяем кэш и при необходимости обновляем
                if time.time() - self.client_data_timestamp > CACHE_REFRESH_INTERVAL:
                    self.reload_client_data()

                # Получаем актуальные данные из БД
                subjects = self.db_manager.get_user_subjects(chat_id)
                is_enabled = self.db_manager.get_user_status(chat_id)

                if subjects:
                    status = "включены" if is_enabled else "отключены"

                    reports_message = (
                        "Ваши отчеты:\n\n"
                        f"Уведомления {status}\n\n"
                    )

                    for i, subject in enumerate(subjects, 1):
                        reports_message += f"{i}. {subject}\n"

                    self._queue_message(chat_id, reports_message)
                    logger.info(f"Пользователь {chat_id} запросил список отчетов")
                else:
                    self._queue_message(
                        chat_id,
                        "У вас нет настроенных отчетов.\n"
                        "Для настройки отчетов обратитесь к администратору."
                    )
            except Exception as e:
                logger.error(f"Ошибка при обработке команды /reports: {e}")
                self._handle_command_error(message, e)

        @self.bot.message_handler(commands=['enable'])
        def handle_enable(message: types.Message) -> None:
            self._update_user_activity(message.chat.id)
            try:
                chat_id = str(message.chat.id)

                # Проверяем, есть ли пользователь в базе
                subjects = self.db_manager.get_user_subjects(chat_id)

                if subjects:
                    if self.update_client_status(chat_id, True):
                        self._queue_message(
                            chat_id,
                            "Уведомления включены. Теперь вы будете получать письма по настроенным отчетам."
                        )
                        logger.info(f"Пользователь {chat_id} включил уведомления")
                    else:
                        self._queue_message(
                            chat_id,
                            "Не удалось включить уведомления. Пожалуйста, попробуйте позже."
                        )
                else:
                    self._queue_message(
                        chat_id,
                        "У вас нет настроенных отчетов.\n"
                        "Для настройки отчетов обратитесь к администратору."
                    )
            except Exception as e:
                logger.error(f"Ошибка при обработке команды /enable: {e}")
                self._handle_command_error(message, e)

        @self.bot.message_handler(commands=['disable'])
        def handle_disable(message: types.Message) -> None:
            self._update_user_activity(message.chat.id)
            try:
                chat_id = str(message.chat.id)

                # Проверяем, есть ли пользователь в базе
                subjects = self.db_manager.get_user_subjects(chat_id)

                if subjects:
                    if self.update_client_status(chat_id, False):
                        self._queue_message(
                            chat_id,
                            "Уведомления отключены. Вы больше не будете получать письма по настроенным отчетам."
                        )
                        logger.info(f"Пользователь {chat_id} отключил уведомления")
                    else:
                        self._queue_message(
                            chat_id,
                            "Не удалось отключить уведомления. Пожалуйста, попробуйте позже."
                        )
                else:
                    self._queue_message(
                        chat_id,
                        "У вас нет настроенных отчетов.\n"
                        "Для настройки отчетов обратитесь к администратору."
                    )
            except Exception as e:
                logger.error(f"Ошибка при обработке команды /disable: {e}")
                self._handle_command_error(message, e)

        @self.bot.message_handler(commands=['help'])
        def handle_help(message: types.Message) -> None:
            self._update_user_activity(message.chat.id)
            try:
                help_message = (
                    "Доступные команды:\n\n"
                    "/start - Начать работу с ботом\n"
                    "/status - Показать ваш статус и список отчетов\n"
                    "/reports - Показать список ваших отчетов\n"
                    "/enable - Включить уведомления\n"
                    "/disable - Отключить уведомления\n"
                    "/help - Показать это сообщение\n\n"
                    "Вы также можете использовать кнопки меню внизу экрана."
                )
                self._queue_message(str(message.chat.id), help_message)
            except Exception as e:
                logger.error(f"Ошибка при обработке команды /help: {e}")
                self._handle_command_error(message, e)

        @self.bot.message_handler(func=lambda message: message.text in ['📊 Статус', '📋 Мои отчеты',
                                                                        '✅ Включить уведомления',
                                                                        '❌ Отключить уведомления',
                                                                        '❓ Помощь'])
        def handle_menu_buttons(message: types.Message) -> None:
            self._update_user_activity(message.chat.id)
            try:
                chat_id = str(message.chat.id)
                user_name = message.from_user.first_name

                if message.text == '📊 Статус':
                    status_message = self.get_status_message(chat_id, user_name)
                    self._queue_message(chat_id, status_message)
                    logger.info(f"Пользователь {chat_id} запросил статус через меню")

                elif message.text == '📋 Мои отчеты':
                    handle_show_reports(message)

                elif message.text == '✅ Включить уведомления':
                    handle_enable(message)

                elif message.text == '❌ Отключить уведомления':
                    handle_disable(message)

                elif message.text == '❓ Помощь':
                    handle_help(message)
            except Exception as e:
                logger.error(f"Ошибка при обработке кнопки меню: {e}")
                self._handle_command_error(message, e)

        @self.bot.message_handler(func=lambda message: True)
        def handle_other_messages(message: types.Message) -> None:
            self._update_user_activity(message.chat.id)
            try:
                chat_id = str(message.chat.id)
                self._queue_message(
                    chat_id,
                    "Я не понимаю этой команды. Используйте кнопки меню или /help для получения списка доступных команд."
                )
            except Exception as e:
                logger.error(f"Ошибка при обработке неизвестного сообщения: {e}")
                self._handle_command_error(message, e)

    def _update_user_activity(self, chat_id) -> None:
        """
        Обновляет время последней активности пользователя.

        Args:
            chat_id: ID чата пользователя
        """
        with self.lock:
            self.last_activity[str(chat_id)] = time.time()

    def _handle_command_error(self, message: types.Message, error: Exception) -> None:
        """
        Обрабатывает ошибки при выполнении команд.

        Args:
            message: Объект сообщения
            error: Исключение
        """
        try:
            chat_id = str(message.chat.id)
            error_message = "Произошла ошибка при обработке вашего запроса. Пожалуйста, попробуйте позже."

            # Если это критическая ошибка, пытаемся отправить напрямую
            try:
                self.bot.send_message(chat_id, error_message)
            except Exception:
                logger.error(f"Не удалось отправить сообщение об ошибке пользователю {chat_id}")
        except Exception as e:
            logger.error(f"Ошибка при обработке ошибки выполнения команды: {e}")

    def _queue_message(self, chat_id: str, text: str, **kwargs) -> None:
        """
        Ставит сообщение в очередь для асинхронной отправки.

        Args:
            chat_id: ID чата получателя
            text: Текст сообщения
            **kwargs: Дополнительные параметры для send_message
        """
        try:
            # Проверяем, не переполнена ли очередь
            if self.message_queue.qsize() >= MAX_MESSAGE_QUEUE:
                logger.warning("Очередь сообщений переполнена, возможна потеря сообщений")

            self.message_queue.put((chat_id, text, kwargs), block=False)
        except queue.Full:
            logger.error(f"Очередь сообщений переполнена, сообщение для {chat_id} отброшено")
        except Exception as e:
            logger.error(f"Ошибка при добавлении сообщения в очередь: {e}")

    def _message_worker(self) -> None:
        """Рабочий поток для отправки сообщений из очереди."""
        logger.info("Запущен поток обработки сообщений")

        while not self.stop_event.is_set():
            try:
                # Получаем сообщение из очереди с таймаутом
                try:
                    chat_id, text, kwargs = self.message_queue.get(timeout=1)
                except queue.Empty:
                    continue

                # Отправляем сообщение с повторными попытками
                for attempt in range(MAX_RETRIES):
                    try:
                        self.bot.send_message(chat_id, text, **kwargs)
                        break
                    except Exception as e:
                        if attempt < MAX_RETRIES - 1:
                            wait_time = RETRY_DELAY * (2 ** attempt)
                            logger.warning(
                                f"Ошибка при отправке сообщения (попытка {attempt + 1}/{MAX_RETRIES}): {e}. "
                                f"Повтор через {wait_time}с"
                            )
                            time.sleep(wait_time)
                        else:
                            logger.error(f"Не удалось отправить сообщение после {MAX_RETRIES} попыток: {e}")

                # Отмечаем задачу как выполненную
                self.message_queue.task_done()
            except Exception as e:
                logger.error(f"Ошибка в обработчике сообщений: {e}")
                time.sleep(1)

        logger.info("Обработчик сообщений завершен")

    def _setup_bot_commands(self) -> None:
        """Безопасная установка команд для подсказок в интерфейсе."""
        try:
            commands = [
                types.BotCommand("start", "Начать работу с ботом"),
                types.BotCommand("status", "Показать ваш статус и список отчетов"),
                types.BotCommand("reports", "Показать список ваших отчетов"),
                types.BotCommand("enable", "Включить уведомления"),
                types.BotCommand("disable", "Отключить уведомления"),
                types.BotCommand("help", "Показать справку")
            ]

            for attempt in range(MAX_RETRIES):
                try:
                    self.bot.set_my_commands(commands)
                    logger.info("Команды бота успешно настроены")
                    break
                except Exception as e:
                    if attempt < MAX_RETRIES - 1:
                        wait_time = RETRY_DELAY * (2 ** attempt)
                        logger.warning(
                            f"Ошибка при настройке команд бота (попытка {attempt + 1}/{MAX_RETRIES}): {e}. "
                            f"Повтор через {wait_time}с"
                        )
                        time.sleep(wait_time)
                    else:
                        logger.error(f"Не удалось настроить команды бота после {MAX_RETRIES} попыток: {e}")
        except Exception as e:
            logger.error(f"Не удалось настроить команды бота: {e}")
            # Ошибка не критичная, бот продолжит работать

    def _polling_worker(self) -> None:
        """Безопасный поток для длительного опроса с автоматическим восстановлением."""
        logger.info("Запущен поток опроса Telegram API")

        while not self.stop_event.is_set():
            try:
                # Запускаем опрос с обработкой исключений
                self.bot.polling(none_stop=True, interval=1, timeout=30)

                # Если polling завершился без исключения, но флаг остановки не установлен,
                # значит, произошла неожиданная остановка - перезапускаем
                if not self.stop_event.is_set():
                    logger.warning("Опрос Telegram API неожиданно завершился, перезапуск...")
                    time.sleep(RECONNECT_DELAY)
            except Exception as e:
                if not self.stop_event.is_set():
                    logger.error(f"Ошибка в потоке опроса Telegram API: {e}")
                    logger.info("Перезапуск опроса Telegram API через 5 секунд...")
                    time.sleep(RECONNECT_DELAY)
                else:
                    logger.info("Поток опроса Telegram API завершается...")
                    break

        logger.info("Поток опроса Telegram API завершен")

    def start(self) -> None:
        """Запуск бота с мониторингом и восстановлением соединения."""
        logger.info("Запуск Telegram бота...")

        try:
            # Устанавливаем команды для подсказок в интерфейсе
            self._setup_bot_commands()

            # Сбрасываем флаг остановки
            self.stop_event.clear()
            self.running = True

            # Запускаем поток обработки сообщений
            self.message_thread = threading.Thread(
                target=self._message_worker,
                name="MessageWorkerThread",
                daemon=True
            )
            self.message_thread.start()

            # Запускаем поток опроса Telegram API
            self.polling_thread = threading.Thread(
                target=self._polling_worker,
                name="PollingThread",
                daemon=True
            )
            self.polling_thread.start()

            logger.info("Бот запущен и готов к работе")
        except Exception as e:
            logger.critical(f"Критическая ошибка при запуске бота: {e}")
            self.stop()
            raise

    def stop(self) -> None:
        """Корректное завершение работы бота."""
        logger.info("Остановка Telegram бота...")

        # Устанавливаем флаг остановки
        self.stop_event.set()
        self.running = False

        try:
            # Останавливаем опрос Telegram API
            self.bot.stop_polling()

            # Ждем завершения потоков
            if self.polling_thread and self.polling_thread.is_alive():
                self.polling_thread.join(timeout=5)
                if self.polling_thread.is_alive():
                    logger.warning("Поток опроса Telegram API не завершился за отведенное время")

            if self.message_thread and self.message_thread.is_alive():
                self.message_thread.join(timeout=5)
                if self.message_thread.is_alive():
                    logger.warning("Поток обработки сообщений не завершился за отведенное время")

            # Очищаем очередь сообщений
            while not self.message_queue.empty():
                try:
                    self.message_queue.get_nowait()
                    self.message_queue.task_done()
                except queue.Empty:
                    break

            logger.info("Telegram бот успешно остановлен")
        except Exception as e:
            logger.error(f"Ошибка при остановке Telegram бота: {e}")

    def is_alive(self) -> bool:
        """
        Проверяет, работает ли бот.

        Returns:
            True если бот работает, иначе False
        """
        return (self.running and
                self.polling_thread is not None and
                self.polling_thread.is_alive() and
                self.message_thread is not None and
                self.message_thread.is_alive())

    def restart(self) -> bool:
        """
        Перезапускает бота с полной очисткой ресурсов.

        Returns:
            True если перезапуск успешен, иначе False
        """
        logger.info("Перезапуск Telegram бота...")

        # Останавливаем бота
        self.stop()

        # Делаем паузу для корректного освобождения ресурсов
        time.sleep(3)

        try:
            # Очищаем все кэши и состояния
            with self.lock:
                self.client_data = {}
                self.client_data_timestamp = 0
                self.user_states = {}
                self.user_states_timestamp = 0
                self.last_activity = {}

            # Пересоздаем экземпляр бота
            self.bot = self._initialize_bot()

            # Регистрируем обработчики заново
            self.register_handlers()

            # Перезагружаем данные о клиентах
            self.reload_client_data()

            # Запускаем бота
            self.start()

            # Проверяем, запустился ли бот
            for attempt in range(3):
                if self.is_alive():
                    logger.info("Telegram бот успешно перезапущен")
                    return True
                time.sleep(1)

            logger.error("Не удалось перезапустить Telegram бота")
            return False
        except Exception as e:
            logger.error(f"Ошибка при перезапуске Telegram бота: {e}")
            return False


def main():
    """Основная функция для запуска бота с обработкой исключений."""
    bot_handler = None
    try:
        # Создание и запуск бота
        bot_handler = EmailBotHandler()
        bot_handler.start()

        # Бесконечный цикл для поддержания работы бота и проверки его состояния
        while True:
            try:
                time.sleep(60)  # Проверка каждую минуту

                # Проверяем, работает ли бот
                if not bot_handler.is_alive():
                    logger.warning("Бот не отвечает, выполняется перезапуск...")
                    bot_handler.restart()
            except KeyboardInterrupt:
                logger.info("Получено прерывание, завершение работы бота...")
                break
            except Exception as e:
                logger.error(f"Ошибка в основном цикле мониторинга: {e}")
                time.sleep(5)
    except KeyboardInterrupt:
        logger.info("Программа остановлена пользователем")
    except Exception as e:
        logger.critical(f"Критическая ошибка при запуске бота: {e}")
        logger.exception(e)
    finally:
        # Корректное завершение работы
        if bot_handler:
            try:
                bot_handler.stop()
            except Exception as e:
                logger.error(f"Ошибка при завершении работы бота: {e}")


if __name__ == "__main__":
    main()