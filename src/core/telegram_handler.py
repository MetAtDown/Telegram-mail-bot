import telebot
from telebot import types
import threading
import time
import queue
import functools
import gc
from src.core.summarization import SummarizationManager
from typing import Dict, List
from src.core.email_handler import EmailTelegramForwarder
from src.config import settings
from src.utils.logger import get_logger

# Настройка логирования с ротацией
logger = get_logger("telegram_bot")  # Убедитесь, что get_logger настроен на DEBUG для отладки, INFO для прода

# Константы
MAX_RETRIES = 3
RETRY_DELAY = 2  # секунды
RECONNECT_DELAY = 5  # секунды
MAX_MESSAGE_QUEUE = 100
CACHE_REFRESH_INTERVAL = 300  # секунды (5 минут)
DELIVERY_MODE_TEXT = 'text'
DELIVERY_MODE_HTML = 'html'
DELIVERY_MODE_SMART = 'smart'
DELIVERY_MODE_PDF = 'pdf'
DEFAULT_DELIVERY_MODE = DELIVERY_MODE_SMART


# --- ПРЕФИКСЫ ДЛЯ КАСТОМНЫХ CALLBACK_DATA ---
REPORT_CONFIG_PREFIX = "rcfgidx_"  # Report Config by Index
SUBJECT_MODE_PREFIX = "smode_"  # Subject Mode
# префиксы для суммаризации
SUBJECT_SUMMARY_PREFIX = "ssum_"  # Включение/выключение суммаризации для отчета
SUBJECT_ORIG_PREFIX = "sorig_"  # Настройка отправки оригинала вместе с суммаризацией
CLOSE_REPORTS_PREFIX = "close_reports"




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
                        wait_time = delay * (2 ** attempt)
                        logger.warning(
                            f"Ошибка при выполнении {func.__name__} (попытка {attempt + 1}/{max_attempts}): "
                            f"{e}. Повтор через {wait_time}с"
                        )
                        time.sleep(wait_time)
                    else:
                        logger.error(f"Не удалось выполнить {func.__name__} после {max_attempts} попыток: {e}")
            if last_exception:  # Добавлено чтобы не было UnboundLocalError если max_attempts = 0 или 1
                raise last_exception
            return None  # Или другое значение по умолчанию, если функция должна что-то возвращать при неудаче

        return wrapper

    return decorator


class EmailBotHandler:
    def __init__(self, db_manager=None):
        self.telegram_token = settings.TELEGRAM_TOKEN
        if not self.telegram_token:
            logger.error("Не найден токен Telegram в настройках")
            raise ValueError("Отсутствует токен Telegram в настройках")

        if db_manager is None:
            from src.db.manager import DatabaseManager
            self.db_manager = DatabaseManager()
        else:
            self.db_manager = db_manager

        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.message_queue = queue.Queue(maxsize=MAX_MESSAGE_QUEUE)


        self.client_data = {}
        self.client_data_timestamp = 0
        self.user_states = {}
        self.user_states_timestamp = 0

        self.message_report_context_cache: Dict[
            int, List[tuple[str, str]]] = {}  # message_id -> list of (subject, mode)
        self.MAX_CONTEXT_CACHE_SIZE = 50  # Максимальный размер кэша контекста

        self.last_activity = {}
        self.running = False
        self.polling_thread = None
        self.message_thread = None
        self.bot = self._initialize_bot()
        self.register_handlers()
        self.reload_client_data()

    def _initialize_bot(self) -> telebot.TeleBot:
        try:
            bot = telebot.TeleBot(
                self.telegram_token,
                threaded=False,
                #num_threads=4, АХАХАХАХАХХАХАХАХАХАХАХАХ УТЕЧКА ИЗ ЗА ЭТОЙ ТЕМЫ PIZDEC КЛАУД Я ТЕБЯ НЕНАВИЖУ
                parse_mode="Markdown"  # Режим по умолчанию для send_message, если не указан явно
            )
            logger.info("Telegram бот успешно инициализирован")
            return bot
        except Exception as e:
            logger.error(f"Ошибка при инициализации Telegram бота: {e}")
            raise

    def _clear_cache_for_subject(self, chat_id: str, subject: str) -> None:
        """Очищает кэш для конкретной темы пользователя"""
        cache_key = f'subject_summarization_settings_{chat_id}_{subject}'
        self.db_manager._clear_cache(cache_key)

        # Также очищаем кэш статуса суммаризации
        cache_key_status = f'subject_summarization_status_{chat_id}_{subject}'
        self.db_manager._clear_cache(cache_key_status)

    def reload_client_data(self) -> None:
        current_time = time.time()
        logger.debug("Перезагрузка данных о состояниях пользователей...")
        if (current_time - self.user_states_timestamp) < CACHE_REFRESH_INTERVAL and self.user_states:
            logger.debug("Используем кэшированные данные о состояниях пользователей")
            return
        try:
            with self.lock:
                self.user_states = self.db_manager.get_all_users()
                self.user_states_timestamp = current_time
                logger.info(f"Загружены статусы для {len(self.user_states)} пользователей")
        except Exception as e:
            logger.error(f"Ошибка при загрузке данных о состояниях пользователей: {e}", exc_info=True)
            if not self.user_states:
                logger.error("Не удалось загрузить состояния пользователей и кэш пуст.")
            else:
                logger.warning("Используем устаревшие данные о состояниях пользователей из кэша.")

    @with_retry()
    def update_client_status(self, chat_id: str, status: bool) -> bool:
        try:
            result = self.db_manager.update_user_status(chat_id, status)
            if result:
                with self.lock:
                    self.user_states[str(chat_id)] = status  # Убедимся, что chat_id - строка
                logger.info(f"Обновлен статус для chat_id {chat_id}: {'Enable' if status else 'Disable'}")
                return True
            logger.warning(f"Не удалось обновить статус для chat_id {chat_id}")
            return False
        except Exception as e:
            logger.error(f"Ошибка при обновлении статуса клиента {chat_id}: {e}")
            raise

    def get_main_menu_keyboard(self) -> types.ReplyKeyboardMarkup:
        if hasattr(self, '_main_menu_keyboard_cached'):
            return self._main_menu_keyboard_cached

        markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
        markup.add(types.KeyboardButton('📊 Статус'), types.KeyboardButton('📋 Мои отчеты'))
        markup.add(types.KeyboardButton('✅ Вкл. уведомления'), types.KeyboardButton('❌ Выкл. уведомления'))
        markup.add(types.KeyboardButton('❓ Помощь'))
        self._main_menu_keyboard_cached = markup
        return markup

    def get_subject_delivery_mode_keyboard(self, original_message_id: int, report_index: int,
                                           current_mode: str) -> types.InlineKeyboardMarkup:
        """
        Создает Inline-клавиатуру для выбора режима доставки.
        Callback_data: "smode_<original_message_id>_<report_index>_<mode_code>"
        """
        keyboard = types.InlineKeyboardMarkup(row_width=1)
        logger.debug(
            f"Создание клавиатуры выбора режима для original_msg_id={original_message_id}, rpt_idx={report_index}, current_mode='{current_mode}'")

        def get_button_text(mode_code: str, text: str) -> str:
            return f"✅ {text}" if mode_code == current_mode else text

        modes = [
            (DELIVERY_MODE_SMART, "Авто (Текст / PDF)"),
            (DELIVERY_MODE_TEXT, "Только текст"),
            (DELIVERY_MODE_HTML, "Только HTML файл"),
            (DELIVERY_MODE_PDF, "Только PDF файл"),
        ]

        for mode_code, mode_text in modes:
            callback_data_str = f"{SUBJECT_MODE_PREFIX}{original_message_id}_{report_index}_{mode_code}"
            logger.debug(f"Callback для режима '{mode_code}': {callback_data_str} (длина {len(callback_data_str)})")
            if len(callback_data_str) > 64:
                logger.warning(f"Длина callback_data '{callback_data_str}' превышает 64 байта!")

            keyboard.add(types.InlineKeyboardButton(
                get_button_text(mode_code, mode_text),
                callback_data=callback_data_str
            ))

        # Добавляем кнопку "Назад к настройкам отчета"
        back_button = types.InlineKeyboardButton(
            "⬅️ Назад к настройкам отчета",
            callback_data=f"{REPORT_CONFIG_PREFIX}{report_index}"
        )
        keyboard.add(back_button)

        return keyboard

    def get_status_message(self, chat_id: str, user_name: str) -> str:
        logger.debug(f"Формирование статуса для {chat_id} ({user_name})")
        try:
            is_registered = self.db_manager.is_user_registered(chat_id)
            if not is_registered:
                logger.info(f"Пользователь {chat_id} не зарегистрирован.")
                return (f"Здравствуйте, {user_name}!\n\nВаш Chat ID: `{chat_id}`\n\n"
                        "Вы пока не зарегистрированы в системе.\n"
                        "Для настройки получения отчетов обратитесь к администратору.")

            is_enabled = self.db_manager.get_user_status(chat_id)
            subjects_info = self.db_manager.get_user_subjects(chat_id)
            subjects_count = len(subjects_info) if subjects_info else 0
            status_text = "включены" if is_enabled else "отключены"
            reports_text = f"У вас настроено отчетов: {subjects_count} шт." if subjects_count > 0 else "У вас пока нет настроенных отчетов."

            return (f"Здравствуйте, {user_name}!\n\nВаш Chat ID: `{chat_id}`\n\n"
                    f"Ваш статус в системе: *Зарегистрирован*\n"
                    f"Получение уведомлений: *{status_text.upper()}*\n\n"
                    f"{reports_text}\n\n"
                    f"Для просмотра и настройки ваших отчетов используйте команду /reports или кнопку '📋 Мои отчеты'.")
        except Exception as e:
            logger.error(f"Ошибка при формировании сообщения о статусе для {chat_id}: {e}", exc_info=True)
            return (f"Здравствуйте, {user_name}!\n\nВаш Chat ID: `{chat_id}`\n\n"
                    "Произошла ошибка при получении информации о вашем статусе.\n"
                    "Пожалуйста, попробуйте позже или обратитесь к администратору.")

    def _clear_old_context_cache_entries(self):
        """Очищает старые записи из кэша контекста, если он превышает лимит."""
        with self.lock:  # Защита доступа к кэшу
            if len(self.message_report_context_cache) > self.MAX_CONTEXT_CACHE_SIZE:
                num_to_remove = len(self.message_report_context_cache) - self.MAX_CONTEXT_CACHE_SIZE
                keys_to_remove = list(self.message_report_context_cache.keys())[:num_to_remove]
                for key in keys_to_remove:
                    del self.message_report_context_cache[key]
                    logger.debug(f"Удален старый контекст из кэша для message_id {key} (достигнут лимит).")
                logger.info(
                    f"Очищено {num_to_remove} записей из кэша контекста. Текущий размер: {len(self.message_report_context_cache)}")

    def register_handlers(self) -> None:
        @self.bot.message_handler(commands=['start'])
        def handle_start(message: types.Message) -> None:
            self._update_user_activity(message.chat.id)
            chat_id = str(message.chat.id)
            user_name = message.from_user.first_name
            welcome_message = (
                f"Добро пожаловать, {user_name}!\n\nВаш Chat ID: `{chat_id}`\n\n"
                "Этот бот пересылает письма из почты в Telegram по настроенным темам.\n\n"
                "Используйте кнопки меню или команду /help для получения справки."
            )
            self._queue_message(chat_id, welcome_message, reply_markup=self.get_main_menu_keyboard())
            logger.info(f"Пользователь {chat_id} ({user_name}) запустил бота")

        @self.bot.message_handler(commands=['status'])
        def handle_status(message: types.Message) -> None:
            self._update_user_activity(message.chat.id)
            chat_id = str(message.chat.id)
            user_name = message.from_user.first_name
            status_message = self.get_status_message(chat_id, user_name)
            self._queue_message(chat_id, status_message, parse_mode='Markdown')  # Убедимся что Markdown парсится
            logger.info(f"Пользователь {chat_id} ({user_name}) запросил статус")

        @self.bot.message_handler(commands=['reports'])
        def handle_show_reports(message: types.Message) -> None:
            self._update_user_activity(message.chat.id)
            chat_id = str(message.chat.id)
            user_name = message.from_user.first_name
            logger.info(f"Пользователь {chat_id} ({user_name}) запросил список отчетов (/reports)")

            try:
                subjects_with_modes = self.db_manager.get_user_subjects(chat_id)
                if not subjects_with_modes:
                    self._queue_message(chat_id, "У вас пока нет настроенных отчетов.\n"
                                                 "Для настройки отчетов обратитесь к администратору.")
                    return

                title_part = EmailTelegramForwarder.escape_markdown_v2("📋 Ваши настроенные отчеты:")
                response_text = title_part + "\n\n"
                mode_display_map = {
                    DELIVERY_MODE_TEXT: "Текст", DELIVERY_MODE_HTML: "HTML",
                    DELIVERY_MODE_PDF: "PDF", DELIVERY_MODE_SMART: "Авто (Текст/PDF)"}
                keyboard = types.InlineKeyboardMarkup(row_width=1)
                
                # Получаем информацию о суммаризации для отчетов
                summarization_manager = SummarizationManager()
                subjects_with_summary = {}
                
                for subject, _ in subjects_with_modes:
                    subjects_with_summary[subject] = summarization_manager.get_report_summarization_status(chat_id, subject)

                for index, (subject, mode) in enumerate(subjects_with_modes):
                    safe_subject_content = EmailTelegramForwarder.escape_markdown_v2(subject)
                    subject_line = f"▪️ *{safe_subject_content}*"
                    mode_text_display = mode_display_map.get(mode, mode.capitalize())
                    mode_prefix_escaped = EmailTelegramForwarder.escape_markdown_v2("- Режим:")
                    mode_line = f"   {mode_prefix_escaped} `{mode_text_display}`"
                    
                    # Добавляем информацию о суммаризации
                    summary_status = subjects_with_summary.get(subject, False)
                    summary_prefix_escaped = EmailTelegramForwarder.escape_markdown_v2("- Саммари:")
                    summary_line = f"   {summary_prefix_escaped} `{'✅' if summary_status else '❌'}`"
                    
                    response_text += f"{subject_line}\n{mode_line}\n{summary_line}\n\n"

                    button_text_subject = f"{subject[:35]}{'...' if len(subject) > 35 else ''}"  # Немного короче для кнопки

                    callback_data_str = f"{REPORT_CONFIG_PREFIX}{index}"
                    logger.debug(
                        f"Для отчета '{subject}' (индекс {index}) callback_data='{callback_data_str}' (длина {len(callback_data_str)})")
                    if len(callback_data_str) > 64:
                        logger.warning(f"Длина callback_data '{callback_data_str}' превышает 64 байта!")

                    button = types.InlineKeyboardButton(
                        f"⚙️ {button_text_subject}",
                        callback_data=callback_data_str
                    )
                    keyboard.add(button)

                explanation_part = EmailTelegramForwarder.escape_markdown_v2(
                    "Нажмите, чтобы изменить настройки конкретного отчета.")
                response_text += explanation_part

                button_close = types.InlineKeyboardButton(
                    "❌ Закрыть",
                    callback_data=CLOSE_REPORTS_PREFIX
                )
                keyboard.add(button_close)

                # Отправляем сообщение напрямую, чтобы получить message_id для кэша
                try:
                    sent_message = self.bot.send_message(
                        chat_id, response_text, reply_markup=keyboard, parse_mode='MarkdownV2')

                    # Кэшируем список subjects_with_modes для этого сообщения
                    with self.lock:  # Защита доступа к кэшу
                        self.message_report_context_cache[sent_message.message_id] = list(subjects_with_modes)
                    logger.info(
                        f"Сообщение /reports (ID: {sent_message.message_id}) с кнопками отправлено. Контекст сохранен.")
                    self._clear_old_context_cache_entries()  # Очистка старых записей
                except Exception as send_ex:
                    logger.error(f"Ошибка при прямой отправке /reports для {chat_id}: {send_ex}", exc_info=True)
                    self._queue_message(chat_id, "Произошла ошибка при отображении списка отчетов.")

            except Exception as e:
                logger.error(f"Ошибка в handle_show_reports для {chat_id}: {e}", exc_info=True)
                self._queue_message(chat_id, "Произошла ошибка при получении списка ваших отчетов.")

        @self.bot.callback_query_handler(func=lambda call: call.data.startswith(REPORT_CONFIG_PREFIX))
        def handle_report_config_callback(call: types.CallbackQuery):
            """Обработчик для отображения меню настройки отчета"""
            self._update_user_activity(call.message.chat.id)
            chat_id = str(call.message.chat.id)
            original_message_id = call.message.message_id  # ID сообщения, где была нажата кнопка "Настроить"

            logger.info(f"Получен callback '{call.data}' от {chat_id} для сообщения {original_message_id}")

            try:
                mode_display_map = {
                    DELIVERY_MODE_TEXT: "Текст",
                    DELIVERY_MODE_HTML: "HTML",
                    DELIVERY_MODE_PDF: "PDF",
                    DELIVERY_MODE_SMART: "Авто (Текст/PDF)"
                }
                report_index_str = call.data.replace(REPORT_CONFIG_PREFIX, "")
                if not report_index_str.isdigit():
                    logger.warning(f"Некорректный индекс в callback_data: '{call.data}'")
                    self.bot.answer_callback_query(call.id, "Ошибка: Неверные данные.", show_alert=True)
                    return
                report_index = int(report_index_str)

                with self.lock:  # Защита доступа к кэшу
                    cached_reports_list = self.message_report_context_cache.get(original_message_id)

                if cached_reports_list is None:
                    logger.warning(f"Не найден кэш для msg_id {original_message_id} (чат {chat_id}).")
                    self.bot.answer_callback_query(call.id, "Данные устарели. Запросите /reports снова.",
                                                   show_alert=True)
                    try:
                        self.bot.edit_message_reply_markup(chat_id, original_message_id)
                    except Exception:
                        pass
                    return

                if not (0 <= report_index < len(cached_reports_list)):
                    logger.warning(
                        f"Индекс {report_index} вне диапазона для кэша (длина {len(cached_reports_list)}) msg_id {original_message_id}.")
                    self.bot.answer_callback_query(call.id, "Ошибка: Отчет не найден.", show_alert=True)
                    return

                subject_to_configure, current_mode = cached_reports_list[report_index]
                logger.info(
                    f"Настройка отчета '{subject_to_configure}' (индекс {report_index}), текущий режим: {current_mode}")

                # Получаем статус суммаризации для этого отчета
                summarization_manager = SummarizationManager()
                summary_enabled = summarization_manager.get_report_summarization_status(chat_id, subject_to_configure)

                # Получаем настройки пользователя (нужны для флага отправки оригинала)
                user_settings = self.db_manager.get_user_summarization_settings(chat_id)
                allow_summarization_control = user_settings.get('allow_summarization', False)
                subject_settings = self.db_manager.get_subject_summarization_settings(chat_id, subject_to_configure)
                send_original = subject_settings.get('send_original', True)


                # Получаем настройки режима доставки пользователя
                delivery_settings = self.db_manager.get_user_delivery_settings(chat_id)
                allow_delivery_mode_selection = delivery_settings.get('allow_delivery_mode_selection', False)

                # Формируем клавиатуру настроек
                keyboard = types.InlineKeyboardMarkup(row_width=1)

                # Кнопка для режима доставки - добавляем только если разрешен выбор формата
                if allow_delivery_mode_selection:
                    button_delivery_text = f"🔄 Режим доставки: {mode_display_map.get(current_mode, current_mode.capitalize())}"
                    button_delivery = types.InlineKeyboardButton(
                        button_delivery_text,
                        callback_data=f"rcfg_delivery_{original_message_id}_{report_index}"
                    )
                    keyboard.add(button_delivery)

                # Кнопка включения/выключения суммаризации - добавляем только если разрешено управление
                if allow_summarization_control:
                    summary_status = "✅ Вкл" if summary_enabled else "❌ Выкл"
                    button_summary = types.InlineKeyboardButton(
                        f"📝 Суммаризация: {summary_status}",
                        callback_data=f"{SUBJECT_SUMMARY_PREFIX}{original_message_id}_{report_index}"
                    )
                    keyboard.add(button_summary)

                    # Кнопка настройки отправки оригинала (только если суммаризация включена)
                    if summary_enabled:
                        button_original = types.InlineKeyboardButton(
                            f"📄 Отправка оригинала: {'✅ Вкл' if send_original else '❌ Выкл'}",
                            callback_data=f"{SUBJECT_ORIG_PREFIX}{original_message_id}_{report_index}"
                        )
                        keyboard.add(button_original)


                # Кнопка возврата к списку
                button_back = types.InlineKeyboardButton(
                    "⬅️ Назад к списку отчетов",
                    callback_data=f"back_to_reports"
                )
                keyboard.add(button_back)

                # Формируем текст сообщения
                safe_subject_escaped = EmailTelegramForwarder.escape_markdown_v2(subject_to_configure)
                response_parts = [
                    EmailTelegramForwarder.escape_markdown_v2("⚙️ Настройка отчета:"),
                    f"`{safe_subject_escaped}`",
                    "*" + EmailTelegramForwarder.escape_markdown_v2("Текущие настройки:") + "*",
                    EmailTelegramForwarder.escape_markdown_v2(
                        f"• Режим доставки: {mode_display_map.get(current_mode, current_mode.capitalize())}"),
                    EmailTelegramForwarder.escape_markdown_v2(
                        f"• Суммаризация: {'Включена' if summary_enabled else 'Отключена'}")
                ]

                if summary_enabled:
                    response_parts.append(EmailTelegramForwarder.escape_markdown_v2(
                        f"• Отправка оригинала: {'Вкл' if send_original else 'Выкл'}")
                    )

                # Добавляем примечания об ограничениях
                notes = []
                if not allow_delivery_mode_selection:
                    notes.append("⚠️ Выбор формата доставки отключен администратором.")
                if not allow_summarization_control:
                    notes.append("⚠️ Управление суммаризацией отключено администратором.")

                if notes:
                    response_parts.append("\n" + EmailTelegramForwarder.escape_markdown_v2("\n".join(notes)))

                response_text = "\n\n".join(response_parts)

                try:
                    self.bot.edit_message_text(
                        response_text, chat_id, original_message_id,
                        reply_markup=keyboard, parse_mode='MarkdownV2')
                    self.bot.answer_callback_query(call.id)
                except telebot.apihelper.ApiTelegramException as api_ex:
                    logger.error(
                        f"Ошибка API при редактировании сообщения {original_message_id} (настройка отчета): {api_ex}",
                        exc_info=True)
                    self.bot.answer_callback_query(call.id, "Ошибка отображения. Попробуйте /reports снова.",
                                                   show_alert=True)
                except Exception as edit_e:
                    logger.error(
                        f"Не удалось отредактировать сообщение {original_message_id} (настройка отчета): {edit_e}",
                        exc_info=True)
                    self.bot.answer_callback_query(call.id, "Ошибка обновления. Попробуйте /reports снова.",
                                                   show_alert=True)

            except Exception as e:
                logger.error(f"Ошибка в handle_report_config_callback ({call.data}): {e}", exc_info=True)
                try:
                    self.bot.answer_callback_query(call.id, "Внутренняя ошибка.", show_alert=True)
                except Exception:
                    pass

        @self.bot.callback_query_handler(func=lambda call: call.data == CLOSE_REPORTS_PREFIX)
        def handle_close_reports(call: types.CallbackQuery):
            """Обработчик кнопки закрытия списка отчетов"""
            self._update_user_activity(call.message.chat.id)
            chat_id = str(call.message.chat.id)
            message_id = call.message.message_id

            try:
                # Удаляем клавиатуру, сохраняя текст сообщения
                self.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=message_id,
                    text="📋 Меню отчетов закрыто.",
                    reply_markup=None
                )
                self.bot.answer_callback_query(call.id, "Меню закрыто")

                # Удаляем из кэша, так как сообщение больше не активно
                with self.lock:
                    if message_id in self.message_report_context_cache:
                        del self.message_report_context_cache[message_id]
                        logger.info(f"Кэш для сообщения {message_id} очищен после закрытия.")

            except Exception as e:
                logger.error(f"Ошибка при закрытии меню отчетов: {e}", exc_info=True)
                self.bot.answer_callback_query(call.id, "Произошла ошибка при закрытии меню")

        @self.bot.callback_query_handler(func=lambda call: call.data.startswith(SUBJECT_MODE_PREFIX))
        def handle_subject_mode_callback(call: types.CallbackQuery):
            self._update_user_activity(call.message.chat.id)
            chat_id = str(call.message.chat.id)
            current_message_id_mode_selection = call.message.message_id  # ID сообщения с кнопками выбора режима

            logger.info(
                f"Получен callback '{call.data}' от {chat_id} для сообщения {current_message_id_mode_selection}")

            try:
                parts = call.data.replace(SUBJECT_MODE_PREFIX, "").split('_')
                if len(parts) != 3:  # original_msg_id, report_idx, new_mode
                    logger.warning(f"Некорректный формат callback_data для выбора режима: '{call.data}'")
                    self.bot.answer_callback_query(call.id, "Ошибка: Неверный формат данных.", show_alert=True)
                    return

                original_message_id_str, report_index_str, new_mode = parts
                if not (original_message_id_str.isdigit() and report_index_str.isdigit() and new_mode):
                    logger.warning(f"Ошибка валидации частей callback_data: {parts}")
                    self.bot.answer_callback_query(call.id, "Ошибка: Некорректные данные.", show_alert=True)
                    return

                original_message_id = int(original_message_id_str)
                report_index = int(report_index_str)

                with self.lock:  # Защита доступа к кэшу
                    cached_reports_list = self.message_report_context_cache.get(original_message_id)

                if cached_reports_list is None:
                    logger.warning(f"Не найден кэш для original_msg_id {original_message_id} (смена режима).")
                    self.bot.answer_callback_query(call.id, "Данные устарели. Запросите /reports.", show_alert=True)
                    try:
                        self.bot.edit_message_text("Данные устарели. Начните с /reports.", chat_id,
                                                   current_message_id_mode_selection, reply_markup=None)
                    except Exception:
                        pass
                    return

                if not (0 <= report_index < len(cached_reports_list)):
                    logger.warning(
                        f"Индекс {report_index} вне диапазона для кэша (длина {len(cached_reports_list)}) original_msg_id {original_message_id}.")
                    self.bot.answer_callback_query(call.id, "Ошибка: Отчет не найден.", show_alert=True)
                    return

                subject_to_update, old_mode_from_cache = cached_reports_list[report_index]
                logger.info(f"Смена режима для '{subject_to_update}' на '{new_mode}' (старый: {old_mode_from_cache})")

                if new_mode == old_mode_from_cache:
                    self.bot.answer_callback_query(call.id, f"Режим '{new_mode.capitalize()}' уже установлен.")
                    # Можно отредактировать сообщение, подтвердив, что режим не изменился
                    return

                if self.db_manager.update_subject_delivery_mode(chat_id, subject_to_update, new_mode):
                    mode_display_map = {
                        DELIVERY_MODE_TEXT: "Текст", DELIVERY_MODE_HTML: "HTML",
                        DELIVERY_MODE_PDF: "PDF", DELIVERY_MODE_SMART: "Авто (Текст/PDF)"}
                    mode_text_display = mode_display_map.get(new_mode, new_mode.capitalize())
                    self.bot.answer_callback_query(call.id, f"Режим изменен на: {mode_text_display}")

                    safe_subject_escaped = EmailTelegramForwarder.escape_markdown_v2(subject_to_update)
                    response_text_success = (
                        f"✅ *Режим доставки для отчета:*\n`{safe_subject_escaped}`\n\n"
                        f"*Успешно изменен на:* `{EmailTelegramForwarder.escape_markdown_v2(mode_text_display)}`")
                    try:
                        self.bot.edit_message_text(
                            response_text_success, chat_id, current_message_id_mode_selection,
                            reply_markup=None, parse_mode='MarkdownV2')

                        # Удаляем из кэша, так как жизненный цикл этого сообщения завершен
                        with self.lock:
                            if original_message_id in self.message_report_context_cache:
                                cached_reports_list = self.message_report_context_cache[original_message_id]
                                if 0 <= report_index < len(cached_reports_list):
                                    subject, _ = cached_reports_list[report_index]  # Берем тему, обновляем режим
                                    cached_reports_list[report_index] = (subject, new_mode)
                        # Используем тот же подход, что и для суммаризации - меняем call.data и вызываем обработчик
                        call.data = f"{REPORT_CONFIG_PREFIX}{report_index}"
                        # Вызываем обработчик конфигурации отчета для возврата в меню настроек
                        handle_report_config_callback(call)
                    except Exception as edit_ex:
                        logger.warning(
                            f"Не удалось отредактировать сообщение {current_message_id_mode_selection} (смена режима): {edit_ex}. Отправляем новое.")
                        self._queue_message(chat_id, response_text_success, parse_mode='MarkdownV2')

                    logger.info(f"Режим для '{subject_to_update}' успешно изменен на {new_mode}.")
                else:
                    self.bot.answer_callback_query(call.id, "⚠️ Ошибка изменения режима в БД.", show_alert=True)
                    logger.warning(f"Ошибка БД при смене режима для '{subject_to_update}' на {new_mode}")
            except Exception as e:
                logger.error(f"Ошибка в handle_subject_mode_callback ({call.data}): {e}", exc_info=True)
                try:
                    self.bot.answer_callback_query(call.id, "Внутренняя ошибка.", show_alert=True)
                except Exception:
                    pass

        @self.bot.callback_query_handler(func=lambda call: call.data.startswith("rcfg_delivery_"))
        def handle_delivery_mode_selection(call: types.CallbackQuery):
            """Обработчик выбора настройки режима доставки"""
            self._update_user_activity(call.message.chat.id)
            chat_id = str(call.message.chat.id)
            
            try:
                # Извлекаем параметры из callback_data
                _, _, original_message_id, report_index = call.data.split('_')
                original_message_id = int(original_message_id)
                report_index = int(report_index)

                # Проверяем, разрешено ли пользователю менять режим доставки
                delivery_settings = self.db_manager.get_user_delivery_settings(chat_id)
                if not delivery_settings.get('allow_delivery_mode_selection', True):
                    self.bot.answer_callback_query(
                        call.id,
                        "Выбор формата отправки отключен. Обратитесь к администратору для настройки.",
                        show_alert=True
                    )
                    return
                
                with self.lock:
                    cached_reports_list = self.message_report_context_cache.get(original_message_id)
                    
                if not cached_reports_list or report_index >= len(cached_reports_list):
                    self.bot.answer_callback_query(call.id, "Данные устарели. Запросите /reports снова.", show_alert=True)
                    return
                    
                subject, current_mode = cached_reports_list[report_index]
                
                # Показываем клавиатуру выбора режима доставки
                mode_keyboard = self.get_subject_delivery_mode_keyboard(original_message_id, report_index, current_mode)
                
                self.bot.edit_message_text(
                    f"Выберите режим доставки для отчета:\n\n`{subject}`",
                    chat_id, call.message.message_id,
                    reply_markup=mode_keyboard, parse_mode='Markdown'
                )
                self.bot.answer_callback_query(call.id)
                
            except Exception as e:
                logger.error(f"Ошибка в handle_delivery_mode_selection: {e}", exc_info=True)
                self.bot.answer_callback_query(call.id, "Произошла ошибка", show_alert=True)

        @self.bot.callback_query_handler(func=lambda call: call.data == "dummy_action")
        def handle_dummy_action(call: types.CallbackQuery):
            """Обработчик для неактивных информационных кнопок"""
            self.bot.answer_callback_query(
                call.id,
                "Эта настройка управляется администратором. Обратитесь к нему для изменения.",
                show_alert=True
            )

        @self.bot.callback_query_handler(func=lambda call: call.data.startswith(SUBJECT_SUMMARY_PREFIX))
        def handle_summary_toggle(call: types.CallbackQuery):
            """Обработчик переключения суммаризации для отчета"""
            self._update_user_activity(call.message.chat.id)
            chat_id = str(call.message.chat.id)

            try:
                # Проверяем, разрешено ли пользователю менять настройки суммаризации
                user_settings = self.db_manager.get_user_summarization_settings(chat_id)
                if not user_settings.get('allow_summarization', False):
                    self.bot.answer_callback_query(
                        call.id,
                        "Управление суммаризацией отключено администратором. Обратитесь к администратору для настройки.",
                        show_alert=True
                    )
                    return

                # Извлекаем параметры из callback_data
                parts = call.data.replace(SUBJECT_SUMMARY_PREFIX, "").split('_')
                if len(parts) != 2:
                    self.bot.answer_callback_query(call.id, "Некорректный формат данных", show_alert=True)
                    return

                original_message_id = int(parts[0])
                report_index = int(parts[1])

                with self.lock:
                    cached_reports_list = self.message_report_context_cache.get(original_message_id)

                if not cached_reports_list or report_index >= len(cached_reports_list):
                    self.bot.answer_callback_query(call.id, "Данные устарели. Запросите /reports снова.",
                                                   show_alert=True)
                    return

                subject, _ = cached_reports_list[report_index]

                # Получаем текущий статус и инвертируем его
                summarization_manager = SummarizationManager()
                current_status = summarization_manager.get_report_summarization_status(chat_id, subject)
                new_status = not current_status

                # Сохраняем новый статус
                if summarization_manager.toggle_report_summarization(chat_id, subject, new_status):
                    status_text = "включена" if new_status else "отключена"
                    self.bot.answer_callback_query(call.id, f"Суммаризация {status_text}")

                    # ВАЖНО: Создаем новую копию вызова для обновления UI с обновленными данными
                    # и очищаем кэш, чтобы гарантировать загрузку актуальных данных
                    self._clear_cache_for_subject(chat_id, subject)

                    callback_data = f"{REPORT_CONFIG_PREFIX}{report_index}"

                    # Модифицируем существующий объект вызова
                    call.data = callback_data

                    # Обрабатываем обновленный вызов напрямую
                    handle_report_config_callback(call)
                else:
                    self.bot.answer_callback_query(call.id, "Не удалось изменить настройку", show_alert=True)

            except Exception as e:
                logger.error(f"Ошибка в handle_summary_toggle: {e}", exc_info=True)
                self.bot.answer_callback_query(call.id, "Произошла ошибка", show_alert=True)

        @self.bot.callback_query_handler(func=lambda call: call.data.startswith(SUBJECT_ORIG_PREFIX))
        def handle_original_toggle(call: types.CallbackQuery):
            """Обработчик переключения отправки оригинала с суммаризацией"""
            self._update_user_activity(call.message.chat.id)
            chat_id = str(call.message.chat.id)

            try:
                # Проверяем, разрешено ли пользователю менять настройки суммаризации
                user_settings = self.db_manager.get_user_summarization_settings(chat_id)
                if not user_settings.get('allow_summarization', False):
                    self.bot.answer_callback_query(
                        call.id,
                        "Управление суммаризацией отключено администратором. Обратитесь к администратору для настройки.",
                        show_alert=True
                    )
                    return

                # Извлекаем параметры из callback_data
                parts = call.data.replace(SUBJECT_ORIG_PREFIX, "").split('_')
                if len(parts) != 2:
                    self.bot.answer_callback_query(call.id, "Некорректный формат данных", show_alert=True)
                    return

                original_message_id = int(parts[0])
                report_index = int(parts[1])

                with self.lock:
                    cached_reports_list = self.message_report_context_cache.get(original_message_id)

                if not cached_reports_list or report_index >= len(cached_reports_list):
                    self.bot.answer_callback_query(call.id, "Данные устарели. Запросите /reports снова.",
                                                   show_alert=True)
                    return

                subject, _ = cached_reports_list[report_index]

                # Получаем текущие настройки темы
                subject_settings = self.db_manager.get_subject_summarization_settings(chat_id, subject)
                current_send_original = subject_settings.get('send_original', True)
                new_send_original = not current_send_original
                prompt_id = subject_settings.get('prompt_id')

                logger.info(
                    f"Переключение отправки оригинала для {chat_id}, тема '{subject}': {current_send_original} -> {new_send_original}")

                # Сохраняем новые настройки
                if self.db_manager.update_subject_summarization_settings(chat_id, subject, prompt_id,
                                                                         new_send_original):
                    status_text = "включена" if new_send_original else "отключена"
                    self.bot.answer_callback_query(call.id, f"Отправка оригинала {status_text}")

                    # Явно очищаем кэши всех связанных объектов
                    self._clear_cache_for_subject(chat_id, subject)

                    # Обновляем значение в кэше (если используется)
                    with self.lock:
                        if hasattr(self, 'subject_settings_cache'):
                            cache_key = f'subject_summarization_settings_{chat_id}_{subject}'
                            if cache_key in self.subject_settings_cache:
                                self.subject_settings_cache[cache_key]['send_original'] = new_send_original

                    # Принудительно получаем свежие данные перед обновлением интерфейса
                    # Это важно, так как мы хотим быть уверены, что новое значение будет использовано
                    fresh_settings = self.db_manager.get_subject_summarization_settings(chat_id, subject)
                    actual_send_original = fresh_settings.get('send_original', True)
                    logger.info(f"Актуальное значение отправки оригинала после обновления: {actual_send_original}")

                    # Модифицируем существующий call.data для вызова обновления интерфейса
                    call.data = f"{REPORT_CONFIG_PREFIX}{report_index}"

                    # Обрабатываем обновленный вызов напрямую для обновления интерфейса
                    handle_report_config_callback(call)
                else:
                    self.bot.answer_callback_query(call.id, "Не удалось изменить настройку", show_alert=True)

            except Exception as e:
                logger.error(f"Ошибка в handle_original_toggle: {e}", exc_info=True)
                self.bot.answer_callback_query(call.id, "Произошла ошибка", show_alert=True)

        @self.bot.callback_query_handler(func=lambda call: call.data == "back_to_reports")
        def handle_back_to_reports(call: types.CallbackQuery):
            """Обработчик возврата к списку отчетов"""
            self._update_user_activity(call.message.chat.id)
            chat_id = str(call.message.chat.id)
            original_message_id = call.message.message_id

            logger.info(f"Запрос на возврат к списку отчетов от {chat_id}, message_id: {original_message_id}")

            try:
                # Отвечаем на запрос сразу, чтобы не было "бесконечной загрузки"
                self.bot.answer_callback_query(call.id)

                # Получаем список отчетов пользователя
                subjects_with_modes = self.db_manager.get_user_subjects(chat_id)
                if not subjects_with_modes:
                    self.bot.edit_message_text(
                        "У вас пока нет настроенных отчетов.\nДля настройки отчетов обратитесь к администратору.",
                        chat_id, original_message_id
                    )
                    return

                # Формируем текст и клавиатуру в ТОЧНОСТИ как в handle_show_reports
                title_part = EmailTelegramForwarder.escape_markdown_v2("📋 Ваши настроенные отчеты:")
                response_text = title_part + "\n\n"

                mode_display_map = {
                    DELIVERY_MODE_TEXT: "Текст",
                    DELIVERY_MODE_HTML: "HTML",
                    DELIVERY_MODE_PDF: "PDF",
                    DELIVERY_MODE_SMART: "Авто (Текст/PDF)"
                }
                keyboard = types.InlineKeyboardMarkup(row_width=1)

                # Получаем информацию о суммаризации для отчетов
                summarization_manager = SummarizationManager()
                subjects_with_summary = {}

                for subject, _ in subjects_with_modes:
                    subjects_with_summary[subject] = summarization_manager.get_report_summarization_status(chat_id,
                                                                                                           subject)

                for index, (subject, mode) in enumerate(subjects_with_modes):
                    safe_subject_content = EmailTelegramForwarder.escape_markdown_v2(subject)
                    subject_line = f"▪️ *{safe_subject_content}*"
                    mode_text_display = mode_display_map.get(mode, mode.capitalize())
                    mode_prefix_escaped = EmailTelegramForwarder.escape_markdown_v2("- Режим:")
                    mode_line = f"   {mode_prefix_escaped} `{mode_text_display}`"

                    # Добавляем информацию о суммаризации
                    summary_status = subjects_with_summary.get(subject, False)
                    summary_prefix_escaped = EmailTelegramForwarder.escape_markdown_v2("- Саммари:")
                    summary_line = f"   {summary_prefix_escaped} `{'✅' if summary_status else '❌'}`"

                    response_text += f"{subject_line}\n{mode_line}\n{summary_line}\n\n"

                    button_text_subject = f"{subject[:35]}{'...' if len(subject) > 35 else ''}"

                    # callback_data: "rcfgidx_<index>"
                    callback_data_str = f"{REPORT_CONFIG_PREFIX}{index}"
                    button = types.InlineKeyboardButton(
                        f"⚙️ {button_text_subject}",
                        callback_data=callback_data_str
                    )
                    keyboard.add(button)

                explanation_part = EmailTelegramForwarder.escape_markdown_v2(
                    "Нажмите, чтобы изменить настройки конкретного отчета.")
                response_text += explanation_part

                button_close = types.InlineKeyboardButton(
                    "❌ Закрыть",
                    callback_data=CLOSE_REPORTS_PREFIX
                )
                keyboard.add(button_close)

                # Редактируем текущее сообщение вместо отправки нового
                self.bot.edit_message_text(
                    response_text, chat_id, original_message_id,
                    reply_markup=keyboard, parse_mode='MarkdownV2')

                # Обновляем кэш
                with self.lock:
                    self.message_report_context_cache[original_message_id] = list(subjects_with_modes)

                logger.info(f"Список отчетов успешно обновлен для {chat_id}")

            except telebot.apihelper.ApiTelegramException as api_ex:
                logger.error(f"Ошибка API при возврате к списку отчетов: {api_ex}", exc_info=True)
                try:
                    self.bot.send_message(
                        chat_id,
                        "Не удалось вернуться к списку отчетов. Пожалуйста, используйте команду /reports."
                    )
                except Exception:
                    pass

            except Exception as e:
                logger.error(f"Ошибка при возврате к списку отчетов: {e}", exc_info=True)
                try:
                    self.bot.send_message(
                        chat_id,
                        "Произошла ошибка. Пожалуйста, используйте команду /reports."
                    )
                except Exception:
                    pass

        @self.bot.message_handler(commands=['enable'])
        def handle_enable(message: types.Message) -> None:
            self._update_user_activity(message.chat.id)
            chat_id = str(message.chat.id)
            if self.update_client_status(chat_id, True):
                self._queue_message(chat_id, "Уведомления включены.")
            else:
                self._queue_message(chat_id, "Не удалось включить уведомления.")

        @self.bot.message_handler(commands=['disable'])
        def handle_disable(message: types.Message) -> None:
            self._update_user_activity(message.chat.id)
            chat_id = str(message.chat.id)
            if self.update_client_status(chat_id, False):
                self._queue_message(chat_id, "Уведомления отключены.")
            else:
                self._queue_message(chat_id, "Не удалось отключить уведомления.")

        @self.bot.message_handler(commands=['help'])
        def handle_help(message: types.Message) -> None:
            self._update_user_activity(message.chat.id)
            logger.info(f"Пользователь {message.chat.id} запросил помощь (/help)")
            help_message = (
                "🤖 *Доступные команды:*\n\n"
                "/start \\- Начать работу / Показать меню\n"
                "/status \\- Показать ваш статус\n"
                "/reports \\- Ваши отчеты и настройки\n"
                "/enable \\- Включить уведомления\n"
                "/disable \\- Отключить уведомления\n"
                "/help \\- Показать это сообщение\n\n"

                "🪄 **Суммаризация\\: Получайте суть\\, а не стены текста\\!**\n"
                "Наш искусственный интеллект анализирует входящие отчеты и письма\\, выделяя из них только ключевую информацию\\. Вы получаете краткую и емкую выжимку главного\\, экономя ваше время и силы\\.\n\n"
                "В настройках каждого отчета вы можете\\:\n"
                "🔹 Включить или отключить эту функцию\\.\n"
                "🔹 Выбрать\\, нужен ли вам полный текст документа вместе с его кратким содержанием\\.\n\n"

                "📎 **Формат Доставки\\: Как вам удобно\\!**\n"
                "Выберите предпочтительный способ получения отчетов\\:\n"
                "🔸 **Авто \\(Текст/PDF\\)\\:** Бот сам подберет оптимальный формат для каждого сообщения\\.\n"
                "🔸 **Только Текст\\:** Идеально для быстрого ознакомления и копирования информации\\.\n"
                "🔸 **Только PDF\\:** Удобно для сохранения\\, печати и официальной документации\\.\n"
                "🔸 **Только HTML\\:** Для просмотра в браузере с сохранением оригинальной структуры и форматирования\\.\n\n"

                "Используйте кнопки основного меню\\."
            )
            self._queue_message(str(message.chat.id), help_message, parse_mode='MarkdownV2')

        @self.bot.message_handler(func=lambda message: message.text in ['📊 Статус', '📋 Мои отчеты',
                                                                        '✅ Вкл. уведомления', '❌ Выкл. уведомления',
                                                                        '❓ Помощь'])
        def handle_menu_buttons(message: types.Message) -> None:
            self._update_user_activity(message.chat.id)
            logger.debug(f"Обработка кнопки меню: '{message.text}' от {message.chat.id}")
            if message.text == '📊 Статус':
                handle_status(message)
            elif message.text == '📋 Мои отчеты':
                handle_show_reports(message)
            elif message.text == '✅ Вкл. уведомления':
                handle_enable(message)
            elif message.text == '❌ Выкл. уведомления':
                handle_disable(message)
            elif message.text == '❓ Помощь':
                handle_help(message)

        @self.bot.message_handler(func=lambda message: True)
        def handle_other_messages(message: types.Message) -> None:
            self._update_user_activity(message.chat.id)
            self._queue_message(str(message.chat.id),
                                "Я не понимаю этой команды. Используйте /help для списка команд.")

    def _update_user_activity(self, chat_id_any_type) -> None:
        chat_id = str(chat_id_any_type)  # Приводим к строке на всякий случай
        with self.lock:
            self.last_activity[chat_id] = time.time()

    def _handle_command_error(self, message: types.Message, error: Exception) -> None:
        try:
            chat_id = str(message.chat.id)
            logger.error(f"Ошибка при обработке команды от {chat_id}: {error}", exc_info=True)  # Логируем ошибку
            self.bot.send_message(chat_id, "Произошла ошибка. Попробуйте позже.")
        except Exception as e:
            logger.error(f"Ошибка при обработке ошибки команды: {e}")

    def _queue_message(self, chat_id: str, text: str, **kwargs) -> None:
        try:
            if self.message_queue.qsize() >= MAX_MESSAGE_QUEUE:
                logger.warning(f"Очередь сообщений переполнена, сообщение для {chat_id} может быть отброшено.")
            self.message_queue.put((str(chat_id), text, kwargs), block=False)  # chat_id к строке
        except queue.Full:
            logger.error(f"Очередь сообщений переполнена, сообщение для {chat_id} отброшено: {text[:50]}...")
        except Exception as e:
            logger.error(f"Ошибка при добавлении сообщения в очередь для {chat_id}: {e}")

    def _message_worker(self) -> None:
        logger.info("Запущен поток обработки исходящих сообщений")
        while not self.stop_event.is_set():
            try:
                chat_id, text, kwargs = self.message_queue.get(timeout=1)
                try:
                    self.bot.send_message(chat_id, text, **kwargs)
                except Exception as e:  # Можно добавить @with_retry сюда, если send_message часто падает
                    logger.error(f"Не удалось отправить сообщение для {chat_id} из очереди: {e}")
                    # Можно рассмотреть логику возврата сообщения в очередь или отбрасывания
                finally:
                    self.message_queue.task_done()
            except queue.Empty:
                continue
            except Exception as e:
                logger.error(f"Ошибка в _message_worker: {e}", exc_info=True)
                time.sleep(1)  # Небольшая пауза при общей ошибке
        logger.info("Поток обработки исходящих сообщений завершен")

    def _setup_bot_commands(self) -> None:
        try:
            commands = [
                types.BotCommand("start", "🚀 Начать работу"),
                types.BotCommand("status", "📊 Мой статус"),
                types.BotCommand("reports", "📋 Мои отчеты и доставка"),
                types.BotCommand("enable", "✅ Вкл. уведомления"),
                types.BotCommand("disable", "❌ Выкл. уведомления"),
                types.BotCommand("help", "❓ Помощь")
            ]

            @with_retry(max_attempts=3, delay=5)
            def set_commands_with_retry():
                self.bot.set_my_commands(commands)

            set_commands_with_retry()
            logger.info("Команды бота успешно настроены.")
        except Exception as e:  # Если with_retry не справился
            logger.error(f"Не удалось настроить команды бота после всех попыток: {e}")

    def _polling_worker(self) -> None:
        logger.info("Запущен поток опроса Telegram API")
        while not self.stop_event.is_set():
            try:
                self.bot.polling(none_stop=True, interval=1, timeout=30)
                if not self.stop_event.is_set():  # Если polling завершился сам по себе
                    logger.warning("Опрос Telegram API неожиданно завершился, перезапуск...")
                    time.sleep(RECONNECT_DELAY)
            except telebot.apihelper.ApiTelegramException as api_ex:
                logger.error(f"Ошибка API Telegram в потоке опроса: {api_ex.error_code} - {api_ex.description}")

                # специальная обработка ошибки 409 (конфликт)
                if api_ex.error_code == 409:
                    logger.warning(
                        "Обнаружен конфликт соединений (ошибка 409). Принудительное освобождение ресурсов...")

                    # Явно говорим системе остановиться и попробовать перезапуститься
                    self.stop_event.set()  # Выходим из цикла polling

                elif api_ex.error_code == 401 or api_ex.error_code == 403:  # Unauthorized or Forbidden
                    logger.critical("Критическая ошибка авторизации бота! Проверьте токен. Остановка бота.")
                    self.stop_event.set()  # Останавливаем бота
                    break

                logger.info(f"Перезапуск опроса Telegram API через {RECONNECT_DELAY} секунд...")
                time.sleep(RECONNECT_DELAY)
            except Exception as e:
                if not self.stop_event.is_set():
                    logger.error(f"Непредвиденная ошибка в потоке опроса Telegram API: {e}", exc_info=True)
                    logger.info(f"Перезапуск опроса Telegram API через {RECONNECT_DELAY} секунд...")
                    time.sleep(RECONNECT_DELAY)
                else:
                    logger.info("Поток опроса Telegram API завершается по флагу stop_event...")
                    break
        logger.info("Поток опроса Telegram API завершен")

    def start(self) -> None:
        logger.info("Запуск Telegram бота...")
        try:
            self._setup_bot_commands()
            self.stop_event.clear()
            self.running = True

            self.message_thread = threading.Thread(target=self._message_worker, name="MessageWorkerThread", daemon=True)
            self.message_thread.start()

            self.polling_thread = threading.Thread(target=self._polling_worker, name="PollingThread", daemon=True)
            self.polling_thread.start()

            logger.info("Бот запущен и готов к работе.")
        except Exception as e:
            logger.critical(f"Критическая ошибка при запуске бота: {e}", exc_info=True)
            self.stop()  # Попытка корректно остановить то, что могло запуститься
            raise

    def stop(self) -> None:
        """Надежная остановка Telegram бота с принудительным закрытием соединений."""
        logger.info("Остановка Telegram бота...")
        if not self.running and self.stop_event.is_set():
            logger.info("Бот уже остановлен или в процессе остановки.")
            return

        self.stop_event.set()
        self.running = False

        try:
            if hasattr(self.bot, 'stop_polling'):  # Убедимся, что метод существует
                self.bot.stop_polling()  # Это должно прервать self.bot.polling()
            logger.info("Команда остановки опроса отправлена.")

            try:
                if hasattr(self.bot, 'session') and self.bot.session:
                    logger.debug("Закрытие сессии бота...")
                    self.bot.session.close()
            except Exception as se:
                logger.error(f"Ошибка при закрытии сессии: {se}")

        except Exception as e:
            logger.error(f"Ошибка при вызове bot.stop_polling(): {e}")

        # Ждем завершения потоков
        if self.polling_thread and self.polling_thread.is_alive():
            logger.debug("Ожидание завершения потока опроса...")
            self.polling_thread.join(timeout=RECONNECT_DELAY + 2)  # Даем время на завершение
            if self.polling_thread.is_alive():
                logger.warning("Поток опроса Telegram API не завершился за таймаут.")

        if self.message_thread and self.message_thread.is_alive():
            logger.debug("Ожидание завершения потока сообщений...")
            self.message_queue.join()  # Ждем, пока все элементы очереди не будут обработаны (task_done)
            self.message_thread.join(timeout=5)  # Затем ждем сам поток
            if self.message_thread.is_alive():
                logger.warning("Поток обработки сообщений не завершился за таймаут.")

        # Очищаем очередь сообщений на всякий случай (если join не сработал идеально)
        drained_count = 0
        while not self.message_queue.empty():
            try:
                self.message_queue.get_nowait()
                self.message_queue.task_done()
                drained_count += 1
            except queue.Empty:
                break
        if drained_count > 0:
            logger.info(f"Принудительно очищено {drained_count} сообщений из очереди при остановке.")

        logger.info("Telegram бот успешно остановлен.")

    def is_alive(self) -> bool:
        return (self.running and
                self.polling_thread is not None and self.polling_thread.is_alive() and
                self.message_thread is not None and self.message_thread.is_alive())

    def restart(self) -> bool:
        """Перезапуск Telegram бота с гарантированным освобождением ресурсов."""
        logger.info("Перезапуск Telegram бота...")

        # 1. Останавливаем текущего бота
        self.stop()

        # 2. Увеличиваем паузу для надежного освобождения ресурсов
        delay_time = RECONNECT_DELAY * 2  # Увеличенное время ожидания
        logger.info(f"Ждем {delay_time} секунд для освобождения ресурсов...")
        time.sleep(delay_time)

        # 3. Вызываем сборщик мусора для очистки неиспользуемых объектов
        gc.collect()

        try:
            # 4. Сброс всех состояний и кэшей
            with self.lock:
                self.message_report_context_cache.clear()
                # Убедимся, что все остальные важные кэши тоже очищены
                self.client_data = {}
                self.client_data_timestamp = 0
                self.user_states = {}
                self.user_states_timestamp = 0
                self.last_activity = {}

            # 5. Убедимся, что старого бота больше нет
            old_bot = self.bot
            self.bot = None
            del old_bot  # Явное удаление старого бота

            # 6. Пересоздаем основные компоненты
            logger.info("Создание нового экземпляра бота...")
            self.bot = self._initialize_bot()

            # 7. Регистрируем обработчики и загружаем данные
            logger.info("Настройка обработчиков и данных...")
            self.register_handlers()
            self.reload_client_data()

            # 8. Запускаем все заново
            logger.info("Запуск бота...")
            self.start()

            # 9. Улучшенная проверка успешности запуска
            check_delay = 3  # Даем больше времени на запуск
            logger.info(f"Проверка состояния через {check_delay} секунд...")
            time.sleep(check_delay)

            if self.is_alive():
                logger.info("Telegram бот успешно перезапущен.")
                return True
            else:
                logger.error("Бот не запустился после перезапуска.")
                return False
        except Exception as e:
            logger.error(f"Ошибка при перезапуске Telegram бота: {e}", exc_info=True)
            return False


def main():
    bot_handler = None
    try:
        bot_handler = EmailBotHandler()
        bot_handler.start()
        while True:
            try:
                time.sleep(60)
                if not bot_handler.is_alive():
                    logger.warning("Бот не отвечает, попытка перезапуска...")
                    if not bot_handler.restart():
                        logger.error("Не удалось перезапустить бота. Остановка основного цикла.")
                        break  # Выход из цикла, если перезапуск не удался
            except KeyboardInterrupt:
                logger.info("Получено Ctrl+C, завершение работы бота...")
                break
            except Exception as e:  # Ловим ошибки в основном цикле мониторинга
                logger.error(f"Ошибка в основном цикле мониторинга: {e}", exc_info=True)
                time.sleep(10)  # Пауза перед следующей проверкой
    except ValueError as ve:  # Например, ошибка отсутствия токена
        logger.critical(f"Ошибка конфигурации при инициализации бота: {ve}")
    except Exception as e:
        logger.critical(f"Критическая неперехваченная ошибка на верхнем уровне: {e}", exc_info=True)
    finally:
        if bot_handler:
            logger.info("Завершение работы: остановка обработчика бота.")
            bot_handler.stop()
        logger.info("Программа завершена.")


if __name__ == "__main__":
    main()