import time
import imaplib
import email
import re
import telebot
import uuid
import schedule
import tempfile
import os
import threading
import queue
import heapq # Добавлен для планировщика
import shutil # Добавлен для надежной очистки
from functools import lru_cache
from typing import Dict, List, Tuple, Any, Optional, Set
from email.header import decode_header
from bs4 import BeautifulSoup, NavigableString, Tag
import html
import datetime
from collections import defaultdict
from contextlib import contextmanager # Добавлен для контекстного менеджера
from weasyprint import HTML as WeasyHTML
from src.config import settings
from src.utils.logger import get_logger

# Настройка логирования
logger = get_logger("email_bot")

# Константы
MAX_RETRIES = 3
RETRY_DELAY = 2  # секунды
CONNECTION_TIMEOUT = 30  # секунды
MAX_BATCH_SIZE = 20  # максимальное количество писем для обработки за раз
MAX_WORKERS = 3  # количество рабочих потоков для обработки писем
DELIVERY_MODE_TEXT = 'text'
DELIVERY_MODE_HTML = 'html'
DELIVERY_MODE_SMART = 'smart'
DELIVERY_MODE_PDF = 'pdf'
DEFAULT_DELIVERY_MODE = DELIVERY_MODE_SMART


# --- НОВЫЙ КЛАСС: Контекстный менеджер для временных файлов ---
class TemporaryFileManager:
    """
    Контекстный менеджер для безопасного создания и автоматической очистки
    временной директории и файлов внутри нее.
    """
    def __init__(self, prefix: str = "email_fwd_"):
        self.prefix = prefix
        self.temp_dir = None

    def __enter__(self) -> str:
        """Создает временную директорию при входе в контекст."""
        try:
            self.temp_dir = tempfile.mkdtemp(prefix=self.prefix)
            logger.debug(f"Создана временная директория: {self.temp_dir}")
            return self.temp_dir
        except Exception as e:
            logger.error(f"Ошибка при создании временной директории: {e}", exc_info=True)
            raise # Передаем исключение дальше, чтобы прервать операцию

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Гарантированно удаляет временную директорию при выходе из контекста."""
        if self.temp_dir and os.path.exists(self.temp_dir):
            try:
                shutil.rmtree(self.temp_dir)
                logger.debug(f"Временная директория удалена: {self.temp_dir}")
            except Exception as e:
                # Логируем ошибку очистки ОЧЕНЬ подробно
                logger.error(
                    f"Критическая ошибка: Не удалось удалить временную директорию {self.temp_dir}: {e}",
                    exc_info=True
                )
                # Не пробрасываем исключение дальше, чтобы не маскировать
                # возможное исходное исключение (exc_type), если оно было.
        # Возвращаем False, чтобы исключения, возникшие внутри блока with,
        # распространялись дальше обычным образом.
        return False

# --- НОВЫЙ КЛАСС: Планировщик отложенных отправок ---
class DelayedSendScheduler:
    """
    Управляет отложенными вызовами функции отправки сообщений,
    используя один поток для избежания создания множества Timer'ов.
    """
    def __init__(self, forwarder_instance, stop_event: threading.Event):
        self.forwarder = forwarder_instance
        self.scheduled_tasks = []  # Используем heapq для эффективности
        self.lock = threading.RLock()
        self.new_task_event = threading.Event() # Сигнал о новой задаче или остановке
        self.stop_event = stop_event # Внешний сигнал для остановки
        self.worker_thread = None
        self._started = False

    def schedule(self, delay_seconds: float, chat_id: str, email_data: Dict[str, Any]):
        """Добавляет задачу в очередь на отложенную отправку."""
        if not self._started:
             logger.warning("Планировщик не запущен, задача не будет добавлена.")
             return

        send_time = time.time() + delay_seconds
        with self.lock:
            heapq.heappush(self.scheduled_tasks, (send_time, chat_id, email_data))
            logger.debug(f"Задача для {chat_id} запланирована на {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(send_time))}")
        # Сигнализируем рабочему потоку, что появилась новая задача
        self.new_task_event.set()

    def _worker_loop(self):
        """Основной цикл рабочего потока планировщика."""
        logger.info("Запущен рабочий поток планировщика отложенных отправок.")
        while not self.stop_event.is_set():
            wait_time = None
            tasks_to_run = []

            with self.lock:
                # Проверяем задачи, готовые к выполнению
                now = time.time()
                while self.scheduled_tasks and self.scheduled_tasks[0][0] <= now:
                    send_time, chat_id, email_data = heapq.heappop(self.scheduled_tasks)
                    tasks_to_run.append((chat_id, email_data))
                    logger.debug(f"Извлечена задача для {chat_id}, запланированная на {send_time:.2f}")

                # Определяем время ожидания до следующей задачи
                if self.scheduled_tasks:
                    next_run_time = self.scheduled_tasks[0][0]
                    wait_time = max(0, next_run_time - now)

            # Выполняем готовые задачи вне блокировки
            if tasks_to_run:
                logger.info(f"Запуск {len(tasks_to_run)} отложенных задач.")
                for chat_id, email_data in tasks_to_run:
                    try:
                        # Используем метод _send_to_telegram_now для фактической отправки
                        self.forwarder._send_to_telegram_now(chat_id, email_data)
                    except Exception as e:
                        logger.error(f"Ошибка при выполнении отложенной задачи для {chat_id}: {e}", exc_info=True)

            # Ожидаем следующей задачи или сигнала об остановке/новой задаче
            # Если wait_time is None (нет задач), ждем бесконечно (но просыпаемся по событию)
            self.new_task_event.wait(timeout=wait_time)
            # Сбрасываем событие после пробуждения, чтобы не крутиться в цикле без дела
            self.new_task_event.clear()

        logger.info("Рабочий поток планировщика отложенных отправок остановлен.")

    def start(self):
        """Запускает рабочий поток планировщика."""
        if self.worker_thread is None or not self.worker_thread.is_alive():
            self.stop_event.clear() # Убедимся, что стоп-сигнал снят
            self.new_task_event.clear()
            self.worker_thread = threading.Thread(
                target=self._worker_loop,
                name="DelayedSendWorker",
                daemon=True
            )
            self.worker_thread.start()
            self._started = True
            logger.info("Планировщик отложенных отправок запущен.")

    def stop(self):
        """Останавливает рабочий поток планировщика."""
        if self._started:
            self._started = False
            # self.stop_event.set() # Используем внешний stop_event
            self.new_task_event.set() # Разбудить поток, чтобы он проверил stop_event
            if self.worker_thread and self.worker_thread.is_alive():
                self.worker_thread.join(timeout=5)
                if self.worker_thread.is_alive():
                     logger.warning("Поток планировщика не завершился вовремя.")
            logger.info("Планировщик отложенных отправок остановлен.")
        # Очищаем задачи при остановке
        with self.lock:
            self.scheduled_tasks = []


class EmailTelegramForwarder:
    def __init__(self, db_manager=None):
        """
        Инициализация форвардера писем в Telegram.
        Args:
            db_manager: Экземпляр менеджера базы данных
        """
        # ... (существующая загрузка настроек) ...
        self.email_account = settings.EMAIL_ACCOUNT
        self.password = settings.EMAIL_PASSWORD
        self.telegram_token = settings.TELEGRAM_TOKEN
        self.email_server = settings.EMAIL_SERVER
        self.check_interval = settings.CHECK_INTERVAL

        if not all([self.email_account, self.password, self.telegram_token]):
            logger.error("Не все обязательные параметры найдены в настройках")
            raise ValueError("Отсутствуют обязательные параметры в настройках")

        if db_manager is None:
            from src.db.manager import DatabaseManager
            self.db_manager = DatabaseManager()
        else:
            self.db_manager = db_manager

        self.bot = telebot.TeleBot(self.telegram_token, threaded=True)
        self.client_data = {}
        self.user_states = {}
        self.email_queue = queue.Queue()
        self.workers = []
        self.stop_event = threading.Event() # Используется и планировщиком
        self._mail_connection = None
        self._mail_lock = threading.RLock()
        self._last_connection_time = 0
        self._connection_idle_timeout = 300
        self._subject_patterns = {}
        self._message_timestamps = {}
        self._rate_limit_lock = threading.RLock()
        self._max_messages_per_minute = 20
        self.subject_prefixes = ["[deeray.com] ", "Re: ", "Fwd: ", "Fw: "]

        # --- ИНИЦИАЛИЗАЦИЯ ПЛАНИРОВЩИКА ---
        self.delayed_sender = DelayedSendScheduler(self, self.stop_event)

        self.reload_client_data()

    # ... (методы reload_client_data, _get_mail_connection, connect_to_mail,
    #      get_all_unseen_emails, decode_mime_header, extract_email_content,
    #      mark_as_unread, extract_email_body, extract_attachments, clean_subject,
    #      format_email_body, check_subject_match, _check_rate_limit - остаются без изменений) ...

    def escape_markdown_v2(self, text: str) -> str:
        """
        Экранирует специальные символы для режима parse_mode='MarkdownV2' Telegram.

        Args:
            text: Исходный текст.

        Returns:
            Текст с экранированными символами.
        """
        if not isinstance(text, str):
            text = str(text)  # На случай если передали не строку

        # Символы, которые нужно экранировать в MarkdownV2
        # _ * [ ] ( ) ~ ` > # + - = | { } . !
        escape_chars = r'_*[]()~`>#+-=|{}.!'
        # Заменяем каждый спецсимвол на него же с предваряющим обратным слэшем
        return re.sub(f'([{re.escape(escape_chars)}])', r'\\\1', text)


    def reload_client_data(self) -> None:
        """Загрузка данных о клиентах из базы данных с оптимизацией кэширования."""
        try:
            # Получаем все темы и связанные с ними chat_id и статусы
            self.client_data = self.db_manager.get_all_subjects()

            # Предварительно обрабатываем шаблоны для быстрого сопоставления
            self._subject_patterns = {}
            for subject_pattern, clients in self.client_data.items():
                subject_lower = subject_pattern.lower()
                # --- ИСПРАВЛЕНИЕ: Убедимся, что clients это список словарей ---
                if isinstance(clients, list):
                    for client in clients:
                        # Проверяем, что client это словарь и содержит 'enabled'
                        if isinstance(client, dict) and client.get("enabled", False):
                            if subject_lower not in self._subject_patterns:
                                self._subject_patterns[subject_lower] = []
                            self._subject_patterns[subject_lower].append((subject_pattern, client["chat_id"]))
                        elif not isinstance(client, dict):
                             logger.warning(f"Некорректный формат данных клиента для темы '{subject_pattern}': {client}")
                else:
                    logger.warning(f"Некорректный формат данных для темы '{subject_pattern}': {clients}")


            unique_subjects = len(self.client_data)
            total_records = sum(len(clients) for clients in self.client_data.values() if isinstance(clients, list)) # Безопасный подсчет

            # Получаем состояния всех пользователей
            self.user_states = self.db_manager.get_all_users()

            logger.info(f"Загружено {unique_subjects} уникальных тем и {total_records} записей из базы данных")
        except Exception as e:
            logger.error(f"Ошибка при загрузке данных о клиентах: {e}", exc_info=True)
            # Если не удалось загрузить данные, продолжаем работу с имеющимися данными
            logger.info("Продолжение работы с имеющимися данными клиентов")

    def _get_mail_connection(self) -> imaplib.IMAP4_SSL:
        """
        Получение соединения с почтовым сервером с пулингом соединений.
        Returns:
            Объект соединения с почтовым сервером
        """
        with self._mail_lock:
            current_time = time.time()

            # Проверяем, не истек ли таймаут соединения
            if (self._mail_connection is not None and
                    current_time - self._last_connection_time > self._connection_idle_timeout):
                try:
                    logger.debug(f"Закрытие неактивного соединения ({self._connection_idle_timeout}с) с почтовым сервером...")
                    self._mail_connection.close()
                    self._mail_connection.logout()
                    logger.debug("Неактивное соединение закрыто.")
                except Exception as close_err:
                    logger.warning(f"Ошибка при закрытии неактивного соединения: {close_err}")
                    # Все равно сбрасываем, чтобы создать новое
                finally:
                    self._mail_connection = None


            # Создаем новое соединение, если необходимо
            if self._mail_connection is None:
                logger.info("Почтовое соединение отсутствует, создаем новое...")
                for attempt in range(MAX_RETRIES):
                    try:
                        mail = imaplib.IMAP4_SSL(self.email_server, timeout=CONNECTION_TIMEOUT)
                        mail.login(self.email_account, self.password)
                        mail.select("inbox")
                        self._mail_connection = mail
                        self._last_connection_time = current_time
                        logger.info("Успешное подключение к почтовому серверу")
                        break
                    except Exception as e:
                        if attempt < MAX_RETRIES - 1:
                            wait_time = RETRY_DELAY * (2 ** attempt)  # Exponential backoff
                            logger.warning(
                                f"Ошибка при подключении к почтовому серверу (попытка {attempt + 1}/{MAX_RETRIES}): {e}. Повтор через {wait_time}с")
                            time.sleep(wait_time)
                        else:
                            logger.error(
                                f"Не удалось подключиться к почтовому серверу после {MAX_RETRIES} попыток: {e}")
                            raise
            else:
                # Обновляем время последнего использования
                self._last_connection_time = current_time

                # Проверяем, что соединение все еще активно
                try:
                    # logger.debug("Проверка активности существующего почтового соединения (noop)...")
                    status, _ = self._mail_connection.noop()
                    if status != 'OK':
                         # Используем другое исключение, чтобы отличить от сетевых ошибок
                        raise imaplib.IMAP4.abort(f"Соединение неактивно (статус {status})")
                    # logger.debug("Соединение активно.")
                except (imaplib.IMAP4.abort, imaplib.IMAP4.error, ConnectionResetError, BrokenPipeError) as e:
                    logger.warning(f"Соединение с почтовым сервером прервано: {e}. Пересоздание...")
                    try:
                        self._mail_connection.close()
                        self._mail_connection.logout()
                    except Exception as close_err:
                         logger.warning(f"Ошибка при закрытии прерванного соединения: {close_err}")
                    finally:
                        self._mail_connection = None
                    # Рекурсивный вызов для создания нового соединения (ОСТОРОЖНО с глубиной рекурсии)
                    # Лучше сделать это итеративно в вызывающем коде или ограничить глубину
                    # Но для простоты пока оставим так, т.к. ошибка не должна повторяться бесконечно
                    return self._get_mail_connection()


            # Добавим проверку типа на всякий случай перед возвратом
            if not isinstance(self._mail_connection, imaplib.IMAP4_SSL):
                 logger.error("Критическая ошибка: _mail_connection не является объектом IMAP4_SSL после инициализации!")
                 raise TypeError("Не удалось получить действительное IMAP соединение")

            return self._mail_connection

    def connect_to_mail(self) -> imaplib.IMAP4_SSL:
        """ Подключение к почтовому серверу (обертка для обратной совместимости). """
        return self._get_mail_connection()

    def get_all_unseen_emails(self, mail: imaplib.IMAP4_SSL) -> List[bytes]:
        """ Получение всех непрочитанных писем с ограничением количества. """
        try:
            status, messages = mail.search(None, 'UNSEEN')
            if status != "OK":
                logger.warning(f"Проблема при поиске непрочитанных писем (статус: {status})")
                return []

            msg_ids = messages[0].split()
            total_msgs = len(msg_ids)

            # Ограничиваем количество писем для обработки за один раз
            if total_msgs > MAX_BATCH_SIZE:
                logger.info(
                    f"Найдено {total_msgs} непрочитанных писем, ограничиваем до {MAX_BATCH_SIZE} для текущей обработки")
                # Берем самые *новые* непрочитанные письма
                msg_ids_to_process = msg_ids[-MAX_BATCH_SIZE:]
            else:
                logger.info(f"Найдено {len(msg_ids)} непрочитанных писем")
                msg_ids_to_process = msg_ids

            return msg_ids_to_process
        except (imaplib.IMAP4.error, imaplib.IMAP4.abort) as e:
            logger.error(f"Ошибка IMAP при получении непрочитанных писем: {e}. Соединение может быть недействительным.")
            # Явно сбросим соединение, чтобы при следующем вызове оно пересоздалось
            with self._mail_lock:
                if self._mail_connection == mail: # Убедимся, что это то же соединение
                    try:
                        mail.close()
                        mail.logout()
                    except: pass
                    self._mail_connection = None
            return []
        except Exception as e:
            logger.error(f"Непредвиденная ошибка при получении непрочитанных писем: {e}", exc_info=True)
            return []

    @lru_cache(maxsize=128)
    def decode_mime_header(self, header: str) -> str:
        """ Декодирование MIME-заголовков с кэшированием. """
        # ... (без изменений) ...
        try:
            decoded_parts = decode_header(header)
            decoded_str = ""

            for part, encoding in decoded_parts:
                if isinstance(part, bytes):
                    # Проверяем наличие кодировки и используем utf-8 как fallback
                    charset = encoding if encoding else 'utf-8'
                    try:
                        decoded_str += part.decode(charset, errors='replace')
                    except LookupError: # Если кодировка неизвестна
                        logger.warning(f"Неизвестная кодировка '{charset}', используем 'utf-8' с заменой.")
                        decoded_str += part.decode('utf-8', errors='replace')
                else:
                    decoded_str += str(part)

            return decoded_str
        except Exception as e:
            logger.error(f"Ошибка при декодировании заголовка: {e}")
            # Возвращаем исходный заголовок в случае ошибки декодирования
            return header if isinstance(header, str) else str(header)


    def extract_email_content(self, mail: imaplib.IMAP4_SSL, msg_id: bytes) -> Optional[Dict[str, Any]]:
        """ Извлечение содержимого письма по его ID. """
        # ... (без изменений, кроме логирования) ...
        try:
            # Получаем письмо целиком (используем PEEK, чтобы не менять флаг \Seen)
            logger.debug(f"Извлечение полного содержимого письма {msg_id.decode()}...")
            status, msg_data = mail.fetch(msg_id, "(BODY.PEEK[])")
            if status != "OK" or not msg_data or not msg_data[0] or not isinstance(msg_data[0], tuple) or len(msg_data[0]) < 2:
                logger.warning(f"Не удалось получить тело письма {msg_id.decode()} (статус: {status}, данные: {msg_data})")
                return None

            # Парсим письмо
            raw_email = msg_data[0][1]
            if not isinstance(raw_email, bytes):
                 logger.warning(f"Некорректный тип данных для raw_email письма {msg_id.decode()}: {type(raw_email)}")
                 return None

            email_message = email.message_from_bytes(raw_email)
            logger.debug(f"Письмо {msg_id.decode()} успешно распарсено.")

            # Извлекаем тему
            subject = self.decode_mime_header(email_message.get("Subject", "Без темы"))
            subject = self.clean_subject(subject)

            # Извлекаем отправителя
            from_header = self.decode_mime_header(email_message.get("From", "Неизвестный отправитель"))

            # Извлекаем дату
            date_header = self.decode_mime_header(email_message.get("Date", ""))

            # Проверяем совпадение по теме (перенесено из extract_email_content для ясности)
            # Эта проверка теперь делается в process_emails перед вызовом extract_email_content

            # Извлекаем тело и HTML
            body, content_type, raw_html_body = self.extract_email_body(email_message)
            attachments = self.extract_attachments(email_message)
            logger.debug(f"Извлечено тело (тип: {content_type}, html: {'да' if raw_html_body else 'нет'}) и {len(attachments)} вложений для письма {msg_id.decode()}.")


            return {
                "subject": subject,
                "from": from_header,
                "date": date_header,
                "body": body,
                "content_type": content_type,
                "raw_html_body": raw_html_body,
                "id": msg_id, # Сохраняем ID как bytes
                "attachments": attachments,
                # "has_match": True, # Это поле больше не нужно здесь
                # "matching_subjects": matching_subjects # Передаем отдельно
            }
        except (imaplib.IMAP4.error, imaplib.IMAP4.abort) as e:
             logger.error(f"Ошибка IMAP при извлечении содержимого письма {msg_id.decode()}: {e}")
             # Сбрасываем соединение
             with self._mail_lock:
                if self._mail_connection == mail:
                    try: mail.close(); mail.logout()
                    except: pass
                    self._mail_connection = None
             return None
        except Exception as e:
            logger.error(f"Непредвиденная ошибка при извлечении содержимого письма {msg_id.decode()}: {e}", exc_info=True)
            return None

    def mark_as_unread(self, mail: imaplib.IMAP4_SSL, msg_id: bytes) -> None:
        """ Отметить письмо как непрочитанное. """
        # ... (без изменений) ...
        for attempt in range(MAX_RETRIES):
            try:
                logger.debug(f"Попытка {attempt+1} отметить письмо {msg_id.decode()} как непрочитанное...")
                status, _ = mail.store(msg_id, '-FLAGS', '\\Seen')
                if status == 'OK':
                    logger.debug(f"Письмо {msg_id.decode()} успешно отмечено как непрочитанное")
                    return
                else:
                    logger.warning(f"Не удалось отметить письмо {msg_id.decode()} как непрочитанное (статус: {status})")
                    # Не повторяем попытку, если статус не OK, возможно проблема с ID или сервером

            except (imaplib.IMAP4.error, imaplib.IMAP4.abort) as e:
                if attempt < MAX_RETRIES - 1:
                    wait_time = RETRY_DELAY * (2 ** attempt)
                    logger.warning(
                        f"Ошибка IMAP при отметке письма {msg_id.decode()} как непрочитанного (попытка {attempt + 1}/{MAX_RETRIES}): {e}. Повтор через {wait_time}с")
                    time.sleep(wait_time)
                    # Попробуем переподключиться перед следующей попыткой
                    try: self._get_mail_connection()
                    except: logger.error("Не удалось переподключиться к почте во время retry.")
                else:
                    logger.error(
                        f"Не удалось отметить письмо {msg_id.decode()} как непрочитанное после {MAX_RETRIES} попыток: {e}")
            except Exception as e:
                 logger.error(f"Непредвиденная ошибка при отметке письма {msg_id.decode()} как непрочитанного: {e}", exc_info=True)
                 # Прерываем попытки при неожиданной ошибке
                 return

    def extract_email_body(self, email_message: email.message.Message) -> Tuple[str, str, Optional[str]]:
        """ Извлечение тела письма с сохранением raw HTML. """
        # ... (без изменений) ...
        body = None
        content_type = "text/plain"
        html_body = None
        plain_body = None
        raw_html_body = None

        try:
            if email_message.is_multipart():
                for part in email_message.walk():
                    # Пропускаем вложения и multipart
                    if part.get_content_maintype() == 'multipart' or part.get('Content-Disposition', '').startswith('attachment'):
                        continue

                    current_content_type = part.get_content_type()
                    charset = part.get_content_charset() or "utf-8"
                    payload = part.get_payload(decode=True)

                    if payload is None: continue # Пропускаем части без содержимого

                    # Обработка text/plain
                    if current_content_type == "text/plain" and plain_body is None:
                        try:
                            plain_body = payload.decode(charset, errors="replace")
                        except LookupError:
                             logger.warning(f"Неизвестная кодировка '{charset}' для text/plain, используем utf-8.")
                             plain_body = payload.decode('utf-8', errors="replace")
                        except Exception as e_dec:
                            logger.error(f"Ошибка декодирования text/plain: {e_dec}")

                    # Обработка text/html
                    elif current_content_type == "text/html" and html_body is None:
                        try:
                            html_body = payload.decode(charset, errors="replace")
                            raw_html_body = html_body # Сохраняем сырой HTML
                        except LookupError:
                            logger.warning(f"Неизвестная кодировка '{charset}' для text/html, используем utf-8.")
                            html_body = payload.decode('utf-8', errors="replace")
                            raw_html_body = html_body
                        except Exception as e_dec:
                            logger.error(f"Ошибка декодирования text/html: {e_dec}")

            else: # Если письмо не multipart
                charset = email_message.get_content_charset() or "utf-8"
                payload = email_message.get_payload(decode=True)
                if payload:
                    try:
                        body = payload.decode(charset, errors="replace")
                        content_type = email_message.get_content_type()
                        if content_type == "text/html":
                            raw_html_body = body
                            html_body = body # Для логики выбора ниже
                        elif content_type == "text/plain":
                            plain_body = body # Для логики выбора ниже
                    except LookupError:
                         logger.warning(f"Неизвестная кодировка '{charset}' для non-multipart, используем utf-8.")
                         body = payload.decode('utf-8', errors="replace")
                         # Пытаемся определить тип еще раз
                         content_type = email_message.get_content_type()
                         if content_type == "text/html": raw_html_body = body; html_body = body
                         elif content_type == "text/plain": plain_body = body
                    except Exception as e_dec:
                        logger.error(f"Ошибка декодирования non-multipart: {e_dec}")

            # Выбираем тело письма: приоритет plain тексту, затем html, затем body (из non-multipart)
            final_body = plain_body if plain_body is not None else html_body if html_body is not None else body
            final_content_type = "text/plain" if plain_body is not None else "text/html" if html_body is not None else content_type

            if final_body is None:
                final_body = "⚠ Не удалось получить содержимое письма"
                final_content_type = "text/plain"

            # Убедимся, что raw_html_body существует только если был найден HTML
            if final_content_type != "text/html":
                 raw_html_body = None

            return final_body, final_content_type, raw_html_body

        except Exception as e:
            logger.error(f"Ошибка при извлечении тела письма: {e}", exc_info=True)
            return "⚠ Ошибка обработки содержимого письма", "text/plain", None

    def extract_attachments(self, email_message: email.message.Message) -> List[Dict[str, Any]]:
        """ Извлечение вложений из письма. """
        # ... (без изменений) ...
        attachments = []
        processed_parts = set() # Для предотвращения дублирования из-за walk()

        if not email_message.is_multipart():
            return attachments

        try:
            for part in email_message.walk():
                part_id = id(part)
                if part_id in processed_parts: continue
                processed_parts.add(part_id)

                # Пропускаем составные части и сообщения, если это не основной контент
                if part.is_multipart(): continue
                # if part.get_content_maintype() in ('multipart', 'message'): continue # Старая проверка

                # Проверяем наличие имени файла и Content-Disposition
                filename = part.get_filename()
                content_disposition = part.get('Content-Disposition', '')

                # Более гибкая проверка на вложения
                is_attachment = bool(filename) or ('attachment' in content_disposition)

                # Пропускаем, если это не вложение (и не inline изображение/ресурс, который тоже может иметь filename)
                # Может потребоваться более сложная логика, если нужно обрабатывать inline иначе
                if not is_attachment and not ('inline' in content_disposition):
                    continue

                # Если имя файла не определено, но есть disposition, попробуем извлечь имя из disposition
                if not filename and ('attachment' in content_disposition or 'inline' in content_disposition):
                    # Пытаемся извлечь имя из Content-Disposition
                    filename_match = re.search(r'filename\*?=(?:(["\'])(.*?)\1|([^;\s]+))', content_disposition, re.IGNORECASE)
                    if filename_match:
                        # Предпочитаем filename* (RFC 5987) если есть, иначе обычный filename
                        encoded_name = filename_match.group(2) or filename_match.group(3)
                        if encoded_name:
                             # Простая эвристика для декодирования (может потребовать улучшения)
                             if encoded_name.lower().startswith("utf-8''"):
                                 try: filename = email.utils.unquote(encoded_name.split("''", 1)[1])
                                 except: filename = encoded_name # Fallback
                             else: filename = encoded_name
                        else: # Fallback если имя не найдено в disposition
                             filename = f"attachment_{uuid.uuid4().hex[:8]}.bin"

                    else: # Если имя не найдено в disposition
                        filename = f"attachment_{uuid.uuid4().hex[:8]}.bin"


                # Если все проверки пройдены, но имя файла всё равно не определено (маловероятно)
                if not filename:
                    filename = f"attachment_{uuid.uuid4().hex[:8]}.bin"

                # Декодируем имя файла, если оно было получено из get_filename()
                filename = self.decode_mime_header(filename)

                # Получаем содержимое вложения
                try:
                    content = part.get_payload(decode=True)
                except Exception as payload_err:
                    logger.error(f"Ошибка при получении payload для '{filename}': {payload_err}")
                    continue # Пропускаем это вложение

                # Если содержимое равно None или пустое, пропускаем
                if content is None or len(content) == 0:
                    logger.warning(f"Вложение '{filename}' не имеет содержимого или оно пустое, пропускаем")
                    continue

                # Получаем тип содержимого
                content_type = part.get_content_type()

                logger.info(f"Найдено вложение: {filename}, тип: {content_type}, размер: {len(content)} байт")

                attachments.append({
                    'filename': filename,
                    'content': content, # Храним как байты
                    'content_type': content_type
                })

            logger.info(f"Всего найдено вложений: {len(attachments)}")
            return attachments
        except Exception as e:
            logger.error(f"Ошибка при извлечении вложений: {e}", exc_info=True)
            return []

    def clean_subject(self, subject: str) -> str:
        """ Очистка темы от префиксов. """
        # ... (без изменений) ...
        try:
            original_subject = subject
            # Проверяем, что subject это строка
            if not isinstance(subject, str):
                 subject = str(subject)

            subject = subject.strip()
            cleaned = False

            # Итеративно удаляем префиксы
            while True:
                 found_prefix = False
                 for prefix in self.subject_prefixes:
                      if subject.lower().startswith(prefix.lower()):
                           subject = subject[len(prefix):].strip()
                           found_prefix = True
                           cleaned = True
                           break # Начинаем проверку префиксов заново с укороченной строки
                 if not found_prefix:
                      break # Ни один префикс не найден, выходим из цикла

            return subject
        except Exception as e:
            logger.error(f"Ошибка при очистке темы письма ('{original_subject}'): {e}")
            return original_subject # Возвращаем исходную в случае ошибки

    def format_email_body(self, body: str, content_type: str) -> str:
        """
        Форматирует тело письма (HTML -> Текст), корректно обрабатывая <br> и <p>
        на основе известной структуры генерации HTML.
        """
        logger.debug(
            f"Форматирование тела (v3). Content-Type: {content_type}. Исходная длина: {len(body)}")
        final_text = ""
        try:
            # Только для HTML контента
            if content_type == "text/html":
                try:
                    # Раскодирование HTML сущностей
                    try:
                        unescaped_body = html.unescape(body)
                    except Exception as ue:
                        logger.warning(f"Ошибка при html.unescape: {ue}. Используем исходный body.")
                        unescaped_body = body

                    soup = BeautifulSoup(unescaped_body, 'html.parser')

                    # --- Основная логика парсинга ---
                    # Ищем основные контейнеры - ячейки таблицы
                    # Предполагаем, что основной контент находится в <td>
                    content_cells = soup.find_all('td')
                    processed_parts = []

                    if not content_cells:
                         # Если нет <td>, пробуем обработать весь body как один блок
                         logger.warning("Не найдены теги <td>, попытка обработки всего body.")
                         content_cells = [soup] # Обрабатываем весь суп как один "блок"

                    for cell in content_cells:
                        # Внутри каждой ячейки обрабатываем теги
                        current_cell_parts = []
                        for element in cell.descendants: # Идем по всем вложенным элементам
                            if isinstance(element, NavigableString):
                                # Просто текст - добавляем, убирая лишние пробелы по краям
                                text = str(element).strip()
                                if text: # Добавляем только непустой текст
                                    current_cell_parts.append(text)
                            elif isinstance(element, Tag):
                                # Обрабатываем теги
                                if element.name == 'br':
                                    # Заменяем <br> на перенос строки
                                    # Добавляем перенос, только если предыдущий элемент не был переносом
                                    # Или если это первый элемент
                                    if not current_cell_parts or current_cell_parts[-1] != '\n':
                                         current_cell_parts.append('\n')
                                elif element.name == 'p':
                                    # Проверяем, пустой ли тег <p>
                                    p_text = element.get_text(strip=True)
                                    if not p_text:
                                        # Пустой <p></p> - добавляем двойной перенос для отступа
                                        # Убедимся, что не добавляем лишние переносы подряд
                                        while current_cell_parts and current_cell_parts[-1] == '\n':
                                             current_cell_parts.pop() # Убираем предыдущие \n
                                        if current_cell_parts: # Добавляем только если список не пуст
                                             current_cell_parts.append('\n\n')
                                    else:
                                        pass # Текст из <p> добавится через NavigableString

                                elif element.name == 'a':
                                    # Обработка ссылок: "текст (URL)"
                                    href = element.get('href', '').strip()
                                    link_text = element.get_text(separator=' ', strip=True)
                                    if href:
                                        if not link_text or link_text == href:
                                            current_cell_parts.append(href)
                                        else:
                                            current_cell_parts.append(f"{link_text} ({href})")
                                        # Добавляем перенос после ссылки, чтобы она была на отдельной строке
                                        if not current_cell_parts or current_cell_parts[-1] != '\n':
                                            current_cell_parts.append('\n')
                                    elif link_text:
                                         current_cell_parts.append(link_text)
                                # Игнорируем другие теги (th, table, a и т.д., т.к. обрабатываем их контент)


                        # Собираем текст из частей ячейки
                        cell_text = "".join(current_cell_parts)
                        processed_parts.append(cell_text)

                    # Объединяем текст из всех обработанных ячеек/частей
                    # Добавляем разделитель между частями, если их больше одной
                    final_text = "\n---\n".join(part.strip() for part in processed_parts if part.strip())


                except Exception as parse_err:
                    logger.error(f"Ошибка парсинга HTML BeautifulSoup (v3): {parse_err}. Попытка вернуть исходный текст.", exc_info=True)
                    final_text = body.strip() # Fallback

            # Если содержимое в plain text
            elif content_type == "text/plain":
                final_text = body.strip()
            else:
                logger.warning(f"Обработка неизвестного content_type: {content_type}. Используем исходный текст.")
                final_text = body.strip()

            # --- Логика удаления "Explore in Superset" ---
            # Применяем к уже полученному final_text
            lines = final_text.splitlines()
            filtered_lines = []
            skip_next_line = False
            explore_removed = False

            for line in lines:
                line_stripped = line.strip()

                if skip_next_line:
                    skip_next_line = False
                    # logger.debug(f"Пропущена строка после 'Explore in Superset': '{line_stripped[:100]}...'")
                    continue

                if line_stripped == "Explore in Superset":
                    skip_next_line = True
                    explore_removed = True
                    # logger.debug("Найдена строка 'Explore in Superset', будет удалена вместе со следующей.")
                    continue

                filtered_lines.append(line)

            if explore_removed:
                logger.debug("Удалена строка 'Explore in Superset' и следующая за ней (если была).")

            # Собираем текст обратно после фильтрации Superset
            final_text = "\n".join(filtered_lines)

            # --- Финальная очистка переносов ---
            # Сжимаем 3 и более переносов до 2
            final_text = re.sub(r'\n{3,}', '\n\n', final_text)
            # Убираем пробелы/табы в КОНЦЕ строк
            final_text = "\n".join([line.rstrip() for line in final_text.splitlines()])
            # Убираем пустые строки в начале/конце
            final_text = final_text.strip()

            logger.debug(f"Тело отформатировано (v3 - descendants). Итоговая длина: {len(final_text)}")
            return final_text

        except Exception as e:
            logger.error(f"Критическая ошибка в format_email_body (v3): {e}", exc_info=True)
            truncated_body = body[:1000] + "..." if body and len(body) > 1000 else body if body else ""
            return f"⚠️ Ошибка обработки содержимого письма (см. логи).\n\n{truncated_body}"

    def check_subject_match(self, email_subject: str) -> List[Tuple[str, str]]:
        """ Проверка соответствия темы письма шаблонам клиентов. """
        # ... (без изменений) ...
        matching_subjects = []
        # Проверяем, что email_subject строка
        if not isinstance(email_subject, str):
             logger.warning(f"Некорректный тип темы письма: {type(email_subject)}. Преобразование в строку.")
             email_subject = str(email_subject)

        email_subject_lower = email_subject.lower()
        processed_patterns = set() # Чтобы не добавлять дубликаты из-за подстрок

        # Сначала проверяем точные совпадения (быстрее)
        if email_subject_lower in self._subject_patterns:
            for pattern, chat_id in self._subject_patterns[email_subject_lower]:
                 match_tuple = (pattern, chat_id)
                 if match_tuple not in processed_patterns:
                      matching_subjects.append(match_tuple)
                      processed_patterns.add(match_tuple)

        # Затем проверяем вхождения подстрок (медленнее)
        # TODO: Оптимизация - если точное совпадение найдено, нужно ли искать подстроки?
        #       Зависит от требований. Пока оставляем как есть.
        for pattern_lower, patterns_data in self._subject_patterns.items():
            # Пропускаем шаблоны, которые уже проверены на точное совпадение
            if pattern_lower == email_subject_lower:
                continue

            # Проверяем, является ли шаблон подстрокой темы письма
            if pattern_lower in email_subject_lower:
                for pattern, chat_id in patterns_data:
                     match_tuple = (pattern, chat_id)
                     if match_tuple not in processed_patterns:
                          matching_subjects.append(match_tuple)
                          processed_patterns.add(match_tuple)

        if matching_subjects:
            logger.info(f"Тема '{email_subject}' совпала с {len(matching_subjects)} шаблонами.")
        else:
            logger.debug(f"Тема '{email_subject}' не совпала ни с одним шаблоном.")

        return matching_subjects

    def _check_rate_limit(self, chat_id: str) -> bool:
        """ Проверка ограничения частоты сообщений для конкретного чата. """
        # ... (без изменений) ...
        with self._rate_limit_lock:
            current_time = time.time()

            # Удаляем устаревшие метки времени (старше 60 секунд)
            if chat_id in self._message_timestamps:
                self._message_timestamps[chat_id] = [
                    ts for ts in self._message_timestamps[chat_id]
                    if current_time - ts < 60
                ]

            # Проверяем, не превышен ли лимит сообщений
            if chat_id in self._message_timestamps and len(
                    self._message_timestamps[chat_id]) >= self._max_messages_per_minute:
                # Логируем только если это первый раз, когда лимит достигнут для этого чата за последнее время
                last_limit_log_key = f"ratelimit_log_{chat_id}"
                now = time.time()
                last_log_time = getattr(self, last_limit_log_key, 0)
                if now - last_log_time > 60: # Логируем не чаще раза в минуту
                     logger.warning(
                        f"Достигнут лимит сообщений для чата {chat_id}: {self._max_messages_per_minute} сообщений в минуту")
                     setattr(self, last_limit_log_key, now)
                return False

            # Добавляем новую метку времени
            if chat_id not in self._message_timestamps:
                self._message_timestamps[chat_id] = []
            self._message_timestamps[chat_id].append(current_time)

            return True

    def send_to_telegram(self, chat_id: str, email_data: Dict[str, Any]) -> bool:
        """
        Точка входа для отправки письма. Проверяет rate limit и либо отправляет
        сразу (_send_to_telegram_now), либо ставит в очередь планировщика.
        """
        # Проверяем ограничение частоты
        if not self._check_rate_limit(chat_id):
            # Откладываем отправку, если лимит превышен
            logger.warning(f"Rate limit достигнут для чата {chat_id}. Планирование отправки через 60 секунд.")
            # Используем новый планировщик
            self.delayed_sender.schedule(60.0, chat_id, email_data)
            return False  # Возвращаем False, так как отправка не произошла сейчас

        # Если лимит не превышен, отправляем немедленно
        try:
            return self._send_to_telegram_now(chat_id, email_data)
        except Exception as e:
             logger.error(f"Непредвиденная ошибка при немедленной отправке в Telegram для {chat_id}: {e}", exc_info=True)
             return False

    def _send_to_telegram_now(self, chat_id: str, email_data: Dict[str, Any]) -> bool:
        """
        (Финальная версия PDF v2 + Авто-ширина + Улучшенный шрифт)
        Непосредственная отправка данных письма в Telegram (Текст/HTML/PDF).
        Для PDF генерируется свой HTML с извлечением данных и улучшенными стилями.
        НЕ проверяет rate limit (за это отвечает DelayedSendScheduler).
        """
        # --- КОНСТАНТЫ РЕЖИМОВ ---
        DELIVERY_MODE_TEXT = 'text'; DELIVERY_MODE_HTML = 'html'; DELIVERY_MODE_SMART = 'smart';
        DELIVERY_MODE_PDF = 'pdf'; DEFAULT_DELIVERY_MODE = DELIVERY_MODE_SMART
        TELEGRAM_MAX_LEN = 4096 # Макс. длина сообщения Telegram

        try:
            # --- 1. Получение режима доставки ---
            user_delivery_mode = DEFAULT_DELIVERY_MODE
            try:
                if hasattr(self, 'db_manager') and self.db_manager:
                    retrieved_mode = self.db_manager.get_user_delivery_mode(chat_id)
                    if retrieved_mode in [DELIVERY_MODE_TEXT, DELIVERY_MODE_HTML, DELIVERY_MODE_SMART, DELIVERY_MODE_PDF]:
                        user_delivery_mode = retrieved_mode
                    else: logger.warning(f"Некорректный режим '{retrieved_mode}' для {chat_id}, используем default.")
                else: logger.warning(f"db_manager отсутствует для {chat_id}. Используется Default режим.")
            except Exception as db_err: logger.error(f"Ошибка получения режима доставки для {chat_id}: {db_err}. Используем Default.", exc_info=True)

            # --- 2. Подготовка контента ---
            body = email_data.get("body", "")
            content_type = email_data.get("content_type", "text/plain")
            raw_html_body = email_data.get("raw_html_body") # Сырой HTML для PDF/HTML файла
            formatted_body = self.format_email_body(body, content_type) # Очищенный текст для текстового режима
            has_attachments = bool(email_data.get("attachments"))
            message_length = len(formatted_body) # Длина очищенного текста

            # --- 3. Определение стратегии отправки ---
            should_send_file = False; file_format_to_send = None
            if raw_html_body: # Если есть HTML версия письма
                if user_delivery_mode == DELIVERY_MODE_HTML: should_send_file = True; file_format_to_send = 'html'
                elif user_delivery_mode == DELIVERY_MODE_PDF: should_send_file = True; file_format_to_send = 'pdf'
                elif user_delivery_mode == DELIVERY_MODE_SMART and message_length >= TELEGRAM_MAX_LEN:
                    # В умном режиме отправляем файл, если текст не влезает в сообщение
                    should_send_file = True; file_format_to_send = 'pdf'
                    logger.info(f"Smart режим ({chat_id}): Текст ({message_length} зн.) > лимита ({TELEGRAM_MAX_LEN}). Отправка как PDF.")
            else: # Если HTML версии нет
                if user_delivery_mode in [DELIVERY_MODE_HTML, DELIVERY_MODE_PDF]:
                    logger.warning(f"Режим '{user_delivery_mode}' ({chat_id}) требует HTML, но его нет в письме. Отправка как текст.")
                # В любом случае отправляем как текст, если нет HTML

            # --- 4. ОБРАБОТКА: ОТПРАВКА КАК PDF ФАЙЛ ---
            if should_send_file and file_format_to_send == 'pdf':
                logger.info(f"Генерация PDF для письма '{email_data.get('subject', '')}' ({chat_id})")

                if WeasyHTML is None:
                    logger.error(f"Невозможно создать PDF ({chat_id}): Библиотека WeasyPrint не импортирована или недоступна.");
                    error_text = f"⚠️ Ошибка: PDF не создан (необходимая библиотека WeasyPrint не найдена на сервере)."
                    try: self._send_telegram_message_with_retry(self.bot.send_message, chat_id, error_text)
                    except Exception as fallback_err: logger.error(f"Не удалось отправить уведомление об ошибке WeasyPrint ({chat_id}): {fallback_err}")
                    return False # Не можем продолжить без WeasyPrint

                # Используем временную директорию для PDF
                with TemporaryFileManager(prefix=f"pdf_{chat_id}_") as temp_dir:
                    pdf_html_content_generator = "" # Строка для накопления HTML для PDF
                    try:
                        # --- Извлечение данных из ИСХОДНОГО HTML ---
                        logger.debug(f"Извлечение данных из HTML для PDF ({chat_id})...")
                        # Используем html.unescape для раскодирования сущностей перед парсингом
                        unescaped_raw_html = html.unescape(raw_html_body)
                        soup = BeautifulSoup(unescaped_raw_html, 'html.parser')
                        tables = soup.find_all('table')

                        if not tables:
                            logger.warning(f"Таблицы не найдены в исходном HTML для PDF ({chat_id}). Попытка отправить текст.")
                            # Можно здесь переключиться на отправку текста или HTML файла как fallback
                            # Но для простоты пока вернем ошибку генерации PDF
                            raise ValueError("Таблицы не найдены в исходном HTML")

                        # Добавляем Заголовок и Дату отчета в PDF
                        pdf_html_content_generator += "<h1>Отчет: {}</h1>\n".format(html.escape(email_data.get('subject', 'N/A')))
                        pdf_html_content_generator += "<p>Дата отчета: {}</p>\n".format(html.escape(email_data.get('date', 'N/A')))
                        pdf_html_content_generator += "<hr/>\n" # Горизонтальная линия

                        table_count = 0
                        for table in tables:
                            table_count += 1
                            tbody = table.find('tbody')
                            thead = table.find('thead')

                            # Пропускаем таблицы без тела или строк в теле
                            if not tbody or not tbody.find('tr'):
                                logger.debug(f"Пропуск пустой таблицы #{table_count} при генерации PDF ({chat_id}).")
                                continue

                            # --- НАЧАЛО ТАБЛИЦЫ В PDF ---
                            pdf_html_content_generator += "<table>\n"

                            # Обработка заголовка таблицы (thead)
                            if thead:
                                pdf_html_content_generator += "<thead>\n<tr>\n"
                                headers = thead.find_all('th')
                                for th in headers:
                                    header_text = ' '.join(th.stripped_strings) # Получаем текст из заголовка
                                    # Убрана установка ширины из Python
                                    pdf_html_content_generator += f'<th>{html.escape(header_text)}</th>\n'
                                pdf_html_content_generator += "</tr>\n</thead>\n"

                            # Обработка тела таблицы (tbody)
                            pdf_html_content_generator += "<tbody>\n"
                            rows = tbody.find_all('tr')
                            for row in rows:
                                pdf_html_content_generator += "<tr>\n"
                                cells = row.find_all(['th', 'td']) # Находим и th и td в теле
                                for cell in cells:
                                    # --- Используем decode_contents для сохранения HTML внутри ячейки ---
                                    cell_inner_html = ""
                                    try:
                                        # Получаем внутреннее HTML содержимое ячейки
                                        cell_inner_html = cell.decode_contents(formatter="html")
                                    except Exception as e_inner:
                                        # Fallback: Если decode_contents не сработал, используем get_text
                                        logger.warning(f"Не удалось получить inner HTML ячейки (таблица {table_count}, {chat_id}), используем get_text: {e_inner}")
                                        cell_text = cell.get_text(separator='\n', strip=True)
                                        cell_inner_html = html.escape(cell_text).replace('\n', '<br/>')

                                    # Определяем тег (th или td)
                                    tag_name = "th" if cell.name == 'th' else "td"
                                    # Убрана установка ширины из Python
                                    pdf_html_content_generator += f'<{tag_name}>{cell_inner_html}</{tag_name}>\n'
                                    # --- Конец обработки ячейки ---
                                pdf_html_content_generator += "</tr>\n"
                            pdf_html_content_generator += "</tbody>\n"

                            # --- КОНЕЦ ТАБЛИЦЫ В PDF ---
                            pdf_html_content_generator += "</table>\n"

                        logger.debug(f"Сгенерировано {table_count} таблиц для PDF ({chat_id}). Общая длина HTML: {len(pdf_html_content_generator)}")

                        # --- Финальный HTML для рендеринга в PDF ---
                        final_pdf_html = f'''<!DOCTYPE html>
                        <html lang="ru">
                        <head>
                            <meta charset="UTF-8">
                            <title>{html.escape(email_data.get("subject", "Отчет"))}</title>
                            <style>
                                @page {{
                                    size: A4 landscape; /* Альбомная ориентация */
                                    margin: 1.5cm; /* Поля */
                                }}
                                html {{
                                    font-size: 9.5pt; /* Базовый размер шрифта */
                                    -webkit-text-size-adjust: 100%;
                                }}
                                body {{
                                    /* Упрощенный стек шрифтов */
                                    font-family: "DejaVu Sans", sans-serif;
                                    line-height: 1.5; /* Увеличен для читаемости */
                                    color: #333;
                                }}
                                h1 {{
                                    font-size: 15pt;
                                    margin-bottom: 0.6em;
                                    color: #111;
                                    font-weight: bold;
                                }}
                                h2 {{ /* Стиль для заголовков таблиц (если бы они были) */
                                    font-size: 11pt;
                                    margin-top: 1.3em;
                                    margin-bottom: 0.6em;
                                    color: #333;
                                    border-bottom: 1px solid #eaeaea;
                                    padding-bottom: 0.2em;
                                    font-weight: bold;
                                }}
                                p {{ /* Стиль для параграфа с датой */
                                    margin: 0.5em 0;
                                    font-size: 9pt; /* Чуть меньше основного */
                                    color: #555;
                                }}
                                hr {{ /* Стиль для линии */
                                    border: none;
                                    border-top: 1px solid #ccc;
                                    margin: 1.2em 0;
                                }}
                                table {{
                                    border-collapse: collapse;
                                    width: 100%;
                                    margin-bottom: 1.5em; /* Больше отступ между таблицами */
                                    page-break-inside: auto; /* Позволить разрыв страницы внутри таблицы, если она очень большая */
                                    border: none;
                                    table-layout: auto; /* ИЗМЕНЕНО: Автоматическая ширина колонок */
                                }}
                                tr {{
                                    page-break-inside: avoid !important; /* Стараться не разрывать строку */
                                    page-break-after: auto;
                                }}
                                thead {{
                                    display: table-header-group; /* Повторять заголовок на новых страницах */
                                    background-color: #f7f7f7;
                                    font-weight: bold;
                                    font-size: 9pt; /* Заголовок чуть меньше */
                                }}
                                th, td {{
                                    border: 1px solid #e0e0e0; /* Чуть светлее рамки */
                                    padding: 6px 8px; /* Немного меньше отступы */
                                    text-align: left;
                                    vertical-align: top; /* Важно для содержимого разной высоты */
                                    word-wrap: break-word; /* Перенос длинных слов */
                                    overflow-wrap: break-word; /* Синоним для совместимости */
                                    page-break-inside: avoid !important; /* Стараться не разрывать содержимое ячейки */
                                    /* ДОБАВЛЕНО: Настройки шрифта для цифр */
                                    font-feature-settings: 'tnum' on; /* Табличные цифры (одинаковая ширина) */
                                    line-height: 1.4; /* Межстрочный интервал внутри ячейки */
                                }}
                                th {{
                                    background-color: #f2f2f2; /* Фон заголовка */
                                }}
                                /* Стили для содержимого внутри ячеек */
                                td p, th p {{ margin: 0; line-height: 1.3; }}
                                body > p {{ margin: 0.5em 0; }} /* Отступ для параграфа даты */
                                a {{ color: #0056b3; text-decoration: none; }} /* Цвет ссылок */
                                a:hover {{ text-decoration: underline; }}
                                img {{ /* На всякий случай, если в ячейках будут картинки */
                                    max-width: 100%;
                                    height: auto;
                                    display: block;
                                    margin-bottom: 0.5em;
                                    vertical-align: middle; /* Выравнивание по вертикали */
                                }}
                            </style>
                        </head>
                        <body>
                            {pdf_html_content_generator}
                        </body>
                        </html>'''

                        # --- Конвертация HTML в PDF ---
                        base_filename = re.sub(r'[^\w\-_\. ]', '_', email_data.get('subject', 'email'))[:50]
                        # Добавляем дату в имя файла для уникальности и информативности
                        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M")
                        pdf_filename = f"{base_filename}_{timestamp}.pdf"
                        temp_file_path = os.path.join(temp_dir, pdf_filename)

                        logger.debug(f"Рендеринг PDF в '{temp_file_path}' ({chat_id})...");
                        WeasyHTML(string=final_pdf_html).write_pdf(temp_file_path)
                        pdf_size_mb = os.path.getsize(temp_file_path) / (1024 * 1024)
                        logger.debug(f"PDF файл '{temp_file_path}' успешно создан ({pdf_size_mb:.2f} МБ) ({chat_id}).")

                        # --- Формирование подписи (caption) для PDF ---
                        caption_header = (
                            f"📊 Отчет: {email_data.get('subject', 'N/A')}\n"
                            f"📅 Дата: {email_data.get('date', 'N/A')}\n\n" # Двойной перенос для отделения
                        )
                        caption_reason = f"📄 PDF-файл ({pdf_size_mb:.1f} МБ)"
                        if user_delivery_mode == DELIVERY_MODE_PDF:
                            caption_reason += " (режим PDF)"

                        full_caption = caption_header + caption_reason
                        # Ограничиваем длину caption
                        if len(full_caption) > 1024:
                            full_caption = full_caption[:1020] + "..."
                            logger.warning(f"Caption для PDF обрезан до 1024 символов ({chat_id}).")

                        # --- Отправка PDF файла в Telegram ---
                        with open(temp_file_path, 'rb') as pdf_file:
                            self._send_telegram_message_with_retry(
                                self.bot.send_document,
                                chat_id,
                                pdf_file,
                                caption=full_caption,
                                visible_file_name=pdf_filename, # Используем сгенерированное имя файла
                                parse_mode=None # Caption здесь простой текст, без Markdown
                            )
                        logger.info(f"PDF файл '{pdf_filename}' успешно отправлен ({chat_id})")

                        # --- Отправка вложений (если они были в письме) ---
                        if has_attachments:
                            logger.info(f"Отправка {len(email_data['attachments'])} вложений ({chat_id}) после PDF.")
                            for attachment in email_data["attachments"]:
                                self.send_attachment_to_telegram(chat_id, attachment)
                                time.sleep(0.5) # Небольшая пауза между файлами

                        return True # Успешная отправка PDF

                    except Exception as e_pdf: # Ловим ВСЕ ошибки при генерации/отправке PDF
                        logger.error(f"Ошибка при генерации или отправке PDF ({chat_id}): {e_pdf}", exc_info=True)
                        error_text = f"⚠️ Произошла ошибка при создании PDF-версии отчета '{email_data.get('subject', '')}'. Подробности в логах сервера."
                        try:
                             # Отправляем уведомление об ошибке
                             self._send_telegram_message_with_retry(self.bot.send_message, chat_id, error_text)
                             # Если есть вложения, можно попробовать отправить хотя бы их
                             if has_attachments:
                                 self._send_telegram_message_with_retry(self.bot.send_message, chat_id, "Попытка отправить только вложения...")
                                 for attachment in email_data["attachments"]:
                                     self.send_attachment_to_telegram(chat_id, attachment)
                                     time.sleep(0.5)
                        except Exception as fallback_err:
                             logger.error(f"Не удалось отправить уведомление об ошибке PDF и/или вложения ({chat_id}): {fallback_err}")
                        return False # Ошибка при обработке PDF
                # Конец with TemporaryFileManager

            # --- 5. ОБРАБОТКА: ОТПРАВКА КАК HTML ФАЙЛ ---
            elif should_send_file and file_format_to_send == 'html':
                # --- НАЧАЛО БЛОКА HTML ---
                logger.info(f"Отправка HTML для письма '{email_data.get('subject', '')}' ({chat_id})")
                with TemporaryFileManager(prefix=f"html_{chat_id}_") as temp_dir:
                    try:
                        # --- Подготовка HTML файла ---
                        base_filename = re.sub(r'[^\w\-_\. ]', '_', email_data.get('subject', 'email'))[:50]
                        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M")
                        html_filename = f"{base_filename}_{timestamp}.html"
                        temp_file_path = os.path.join(temp_dir, html_filename)

                        # Используем исходный raw_html_body, раскодировав сущности
                        processed_html_for_html = html.unescape(raw_html_body)
                        clean_html = processed_html_for_html # По умолчанию используем как есть

                        # Опциональная очистка HTML от лишнего (скрипты, стили, Superset ссылка)
                        try:
                            soup_html = BeautifulSoup(processed_html_for_html, 'html.parser')
                            for tag in soup_html(['script', 'style', 'meta', 'link', 'head', 'title']):
                                tag.decompose()
                            # Удаление блока с 'Explore in Superset', если он есть
                            superset_link = soup_html.find('a', string='Explore in Superset')
                            if superset_link:
                                parent_to_remove = superset_link.find_parent(['div', 'p', 'td', 'th', 'tr', 'body']) # Ищем родителя для удаления
                                if parent_to_remove and parent_to_remove.name != 'body':
                                    logger.debug(f"Удаление родительского блока '{parent_to_remove.name}' ссылки 'Explore in Superset' для HTML файла.")
                                    parent_to_remove.decompose()
                                else:
                                    logger.debug("Удаление только самой ссылки 'Explore in Superset' для HTML файла.")
                                    superset_link.decompose()
                            clean_html = str(soup_html)
                        except Exception as parse_err_html:
                            logger.warning(f"Ошибка парсинга/очистки HTML для файла ({chat_id}): {parse_err_html}. Используем исходный HTML.")

                        # --- Запись HTML в файл с базовыми стилями ---
                        with open(temp_file_path, 'w', encoding='utf-8') as f:
                            f.write('<!DOCTYPE html>\n<html lang="ru">\n<head>\n    <meta charset="UTF-8">\n    <meta name="viewport" content="width=device-width, initial-scale=1.0">\n')
                            f.write(f'    <title>{html.escape(email_data.get("subject", "Отчет"))}</title>\n')
                            # Добавляем простые стили для читаемости HTML файла
                            f.write('    <style> body { font-family: sans-serif; line-height: 1.5; padding: 15px; max-width: 1200px; margin: 0 auto; } table { border-collapse: collapse; width: 100%; margin-bottom: 1em; border: 1px solid #ccc; } th, td { border: 1px solid #ddd; padding: 8px; text-align: left; vertical-align: top; } th { background-color: #f2f2f2; font-weight: bold; } img { max-width: 100%; height: auto; } a {color: #0000EE; text-decoration: underline;} </style>\n')
                            f.write('</head>\n<body>\n')
                            f.write(clean_html) # Записываем очищенный (или исходный) HTML
                            f.write('\n</body>\n</html>')
                        logger.debug(f"HTML файл '{temp_file_path}' создан ({chat_id}).")

                        # --- Формирование подписи (caption) для HTML ---
                        caption_header = (
                            f"📊 Отчет: {email_data.get('subject', 'N/A')}\n"
                            f"📅 Дата: {email_data.get('date', 'N/A')}\n\n"
                        )
                        caption_reason = "📄 HTML-файл"
                        if user_delivery_mode == DELIVERY_MODE_HTML:
                            caption_reason += " (режим HTML)"
                        elif user_delivery_mode == DELIVERY_MODE_SMART:
                            caption_reason += " (т.к. сообщение длинное)"

                        full_caption = caption_header + caption_reason
                        if len(full_caption) > 1024:
                             full_caption = full_caption[:1020] + "..."

                        # --- Отправка HTML файла ---
                        with open(temp_file_path, 'rb') as html_file:
                            self._send_telegram_message_with_retry(
                                self.bot.send_document,
                                chat_id,
                                html_file,
                                caption=full_caption,
                                visible_file_name=html_filename,
                                parse_mode=None
                            )
                        logger.info(f"HTML файл '{html_filename}' отправлен ({chat_id})")

                        # --- Отправка вложений ---
                        if has_attachments:
                            logger.info(f"Отправка {len(email_data['attachments'])} вложений ({chat_id}) после HTML.")
                            for attachment in email_data["attachments"]:
                                self.send_attachment_to_telegram(chat_id, attachment)
                                time.sleep(0.5)
                        return True # Успех HTML

                    except Exception as e_html:
                        logger.error(f"Ошибка при создании или отправке HTML файла ({chat_id}): {e_html}", exc_info=True)
                        error_text = f"⚠️ Не удалось отправить отчет '{email_data.get('subject', '')}' как HTML файл."
                        try:
                             self._send_telegram_message_with_retry(self.bot.send_message, chat_id, error_text)
                             if has_attachments:
                                 self._send_telegram_message_with_retry(self.bot.send_message, chat_id, "Попытка отправить только вложения...")
                                 for attachment in email_data["attachments"]:
                                     self.send_attachment_to_telegram(chat_id, attachment)
                                     time.sleep(0.5)
                        except Exception as fallback_err:
                             logger.error(f"Не удалось отправить уведомление об ошибке HTML и/или вложения ({chat_id}): {fallback_err}")
                        return False
                 # --- КОНЕЦ БЛОКА HTML ---

            # --- 6. ОБРАБОТКА: ОТПРАВКА КАК ТЕКСТ ---
            else:
                logger.info(
                    f"Отправка письма '{email_data.get('subject', '')}' как текст для {chat_id} (режим: {user_delivery_mode}, длина тела: {message_length})")
                part_to_log = "N/A"

                try:
                    # Формируем заголовок
                    header = (
                        f"*📊 Отчет:* {self.escape_markdown_v2(email_data.get('subject', 'N/A'))}\n"
                        f"*📅 Дата:* {self.escape_markdown_v2(email_data.get('date', 'N/A'))}\n\n"
                    )
                    # Экранируем отформатированное тело
                    escaped_body = self.escape_markdown_v2(formatted_body)

                    full_message_text_with_header = header + escaped_body
                    logical_separator = "________________"  # Наш логический разделитель
                    # --- ИЗМЕНЕНИЕ: Экранируем ВИДИМЫЙ разделитель ---
                    visible_separator_md = self.escape_markdown_v2(logical_separator)
                    # Экранируем разделитель для использования в split()
                    escaped_split_separator = self.escape_markdown_v2(logical_separator)
                    logger.debug(f"Используется экранированный разделитель для split: '{escaped_split_separator}'")
                    logger.debug(f"Видимый разделитель (экранированный): '{visible_separator_md}'")

                    # 1. Разбиваем ПОЛНЫЙ текст по ЭКРАНИРОВАННОМУ разделителю
                    logical_blocks_raw = full_message_text_with_header.split(escaped_split_separator)
                    logger.debug(
                        f"Текст разбит на {len(logical_blocks_raw)} потенциальных логических блока(ов) по сепаратору.")

                    final_message_parts = []
                    current_message_part = ""

                    # --- ИЗМЕНЕНИЕ: Логика сборки сообщений с видимым сепаратором ---
                    for i, block in enumerate(logical_blocks_raw):
                        trimmed_block = block.strip()

                        if not trimmed_block: continue  # Пропускаем пустые блоки

                        # Определяем, нужен ли разделитель ПЕРЕД этим блоком
                        # Он нужен, если это не первый блок (i > 0) и мы добавляем его к
                        # НЕ ПУСТОМУ текущему сообщению ИЛИ начинаем новое сообщение НЕ с первого блока
                        needs_separator_before = (i > 0)  # Сепаратор нужен перед всеми блоками, кроме первого
                        separator_to_add_md = f"\n\n{visible_separator_md}\n\n" if needs_separator_before else ""

                        # 2. Проверяем, не слишком ли длинный САМ блок
                        # При проверке длины блока учитываем возможный сепаратор ПЕРЕД ним,
                        # если бы он отправлялся ОТДЕЛЬНО (хотя обычно сепаратор будет присоединен к предыдущему блоку)
                        # Но безопаснее проверить блок сам по себе, без сепаратора.
                        if len(trimmed_block) > TELEGRAM_MAX_LEN:
                            logger.warning(
                                f"Логический блок #{i + 1} (начинающийся с '{trimmed_block[:50]}...') "
                                f"длиной {len(trimmed_block)} символов превышает лимит Telegram. "
                                f"Он будет разбит стандартным split_text."
                            )
                            # Завершаем предыдущее сообщение (если было)
                            if current_message_part:
                                final_message_parts.append(current_message_part)
                                logger.debug(
                                    f"Завершено сообщение перед длинным блоком (длина {len(current_message_part)}).")
                            current_message_part = ""  # Сбрасываем

                            # Разбиваем сам длинный блок
                            sub_parts = self.split_text(trimmed_block, max_length=TELEGRAM_MAX_LEN)
                            # Добавляем сепаратор ПЕРЕД первой частью этого длинного блока, если он нужен
                            if needs_separator_before and sub_parts:
                                # Проверяем, влезет ли сепаратор + первая часть
                                if len(separator_to_add_md.strip()) + len(sub_parts[0]) <= TELEGRAM_MAX_LEN:
                                    # Добавляем сепаратор в начало первой части (убирая лишние \n по краям сепаратора)
                                    final_message_parts.append(separator_to_add_md.strip() + "\n\n" + sub_parts[0])
                                    final_message_parts.extend(sub_parts[1:])  # Добавляем остальные части как есть
                                else:
                                    # Если не влезает даже с первой частью, отправляем сепаратор отдельно, потом части
                                    final_message_parts.append(separator_to_add_md.strip())
                                    final_message_parts.extend(sub_parts)
                            else:  # Сепаратор не нужен или нет частей
                                final_message_parts.extend(sub_parts)

                            continue  # Переходим к следующему логическому блоку

                        # 3. Блок помещается сам по себе. Пытаемся добавить его к текущему сообщению.
                        # Рассчитываем длину, ЕСЛИ мы добавим сепаратор и блок
                        projected_length = len(current_message_part) + len(separator_to_add_md) + len(trimmed_block)

                        if current_message_part and projected_length <= TELEGRAM_MAX_LEN:
                            # Влезает! Добавляем сепаратор и блок к текущему сообщению
                            current_message_part += separator_to_add_md + trimmed_block
                            logger.debug(
                                f"Блок #{i + 1} добавлен к текущему сообщению с сепаратором (новая длина: {len(current_message_part)})")
                        elif not current_message_part and len(trimmed_block) <= TELEGRAM_MAX_LEN:
                            # Это первый блок или начало нового сообщения, и он влезает сам по себе
                            current_message_part = trimmed_block  # Сепаратор не нужен в начале
                            logger.debug(
                                f"Начато новое сообщение с блока #{i + 1} (длина: {len(current_message_part)})")
                        else:
                            # Не влезает! Завершаем текущее сообщение и начинаем новое с этого блока.
                            if current_message_part:
                                final_message_parts.append(current_message_part)
                                logger.debug(f"Текущее сообщение (длина {len(current_message_part)}) завершено.")
                            # Начинаем новое сообщение. Добавляем сепаратор ПЕРЕД ним, если он нужен.
                            # Убираем лишние \n по краям сепаратора при добавлении к блоку.
                            message_start = separator_to_add_md.strip() + "\n\n" if needs_separator_before else ""
                            current_message_part = message_start + trimmed_block
                            # Проверяем, не превысила ли длина ИЗ-ЗА добавления сепаратора
                            if len(current_message_part) > TELEGRAM_MAX_LEN:
                                logger.warning(
                                    f"Блок #{i + 1} с сепаратором превысил лимит. Отправка сепаратора отдельно.")
                                if needs_separator_before:
                                    final_message_parts.append(separator_to_add_md.strip())
                                current_message_part = trimmed_block  # Начинаем новое сообщение только с блока

                            logger.debug(
                                f"Начато новое сообщение с блока #{i + 1} {'с сепаратором ' if needs_separator_before else ''}(длина: {len(current_message_part)})")

                    # 4. После цикла добавляем последнее накопленное сообщение (если оно не пустое)
                    if current_message_part:
                        final_message_parts.append(current_message_part)
                        logger.debug(f"Последнее накопленное сообщение (длина {len(current_message_part)}) добавлено.")

                    logger.info(
                        f"Итоговое количество сообщений для отправки (после уплотнения): {len(final_message_parts)}")

                    if not final_message_parts and not has_attachments:
                        logger.warning(f"Нет ни текста, ни вложений для отправки ({chat_id}).")
                        # Отправляем уведомление, если совсем пусто
                        self._send_telegram_message_with_retry(self.bot.send_message, chat_id,
                                                               f"ℹ️ Письмо '{email_data.get('subject', '')}' не содержит текста для отправки.")
                        return False  # Нечего отправлять

                    if not final_message_parts and has_attachments:
                        logger.info(
                            f"Нет текста, отправка только вложений ({len(email_data['attachments'])} шт.) для {chat_id}")
                        # Отправляем только вложения
                        for attachment in email_data["attachments"]:
                            self.send_attachment_to_telegram(chat_id, attachment)
                            time.sleep(0.5)
                        return True

                    # Если есть текст (final_message_parts не пуст)
                    if not has_attachments:
                        # Вложений нет, просто отправляем части текста
                        for i, part in enumerate(final_message_parts):
                            part_to_log = part  # Сохраняем для лога ошибки
                            self._send_telegram_message_with_retry(
                                self.bot.send_message,
                                chat_id,
                                part,
                                parse_mode='MarkdownV2',
                                disable_web_page_preview=True
                            )
                            if len(final_message_parts) > 1 and i < len(final_message_parts) - 1:
                                time.sleep(0.5)  # Пауза между частями
                    else:
                        # Есть вложения
                        # Проверяем, можно ли использовать caption (1 вложение, первая часть текста < 1024)
                        can_use_caption = (
                                len(final_message_parts) > 0 and
                                len(final_message_parts[0]) <= 1024 and  # Caption лимит
                                len(email_data["attachments"]) == 1  # Только одно вложение
                        )

                        if can_use_caption:
                            # Отправляем вложение с первой частью текста как caption
                            logger.debug(f"Использование caption для вложения и текста ({chat_id})")
                            self.send_attachment_with_message(
                                chat_id,
                                email_data["attachments"][0],
                                final_message_parts[0]  # Первая часть как caption (уже экранирована)
                            )
                            # Отправляем оставшиеся части текста (если есть)
                            for i, part in enumerate(final_message_parts[1:]):
                                part_to_log = part
                                self._send_telegram_message_with_retry(
                                    self.bot.send_message, chat_id, part, parse_mode='MarkdownV2',
                                    disable_web_page_preview=True
                                )
                                # Пауза между остальными частями текста
                                if len(final_message_parts) > 2 and i < len(
                                        final_message_parts) - 2:  # Проверяем i < len - 2, т.к. final_message_parts[1:]
                                    time.sleep(0.5)
                        else:
                            # Если caption нельзя использовать (много вложений или текст длинный)
                            # Сначала отправляем весь текст
                            logger.debug(
                                f"Отправка текста ({len(final_message_parts)} частей), затем вложений ({len(email_data['attachments'])} шт.) ({chat_id})")
                            for i, part in enumerate(final_message_parts):
                                part_to_log = part
                                self._send_telegram_message_with_retry(
                                    self.bot.send_message, chat_id, part, parse_mode='MarkdownV2',
                                    disable_web_page_preview=True
                                )
                                if len(final_message_parts) > 1 and i < len(final_message_parts) - 1:
                                    time.sleep(0.5)
                            # Затем отправляем все вложения по одному
                            logger.info(f"Отправка {len(email_data['attachments'])} вложений ({chat_id}) после текста.")
                            for attachment in email_data["attachments"]:
                                self.send_attachment_to_telegram(chat_id, attachment)
                                time.sleep(0.5)  # Пауза между вложениями

                    logger.info(
                        f"Сообщение текстом (возможно, из {len(final_message_parts)} частей) и вложения (если были) отправлены ({chat_id})")
                    return True  # Успех отправки текста

                except Exception as e_text:
                    # Логируем ошибку, включая часть текста, на которой споткнулись
                    failing_part_preview = part_to_log[:200] + ('...' if len(part_to_log) > 200 else '')
                    logger.error(
                        f"Ошибка отправки текста/вложений ({chat_id}, часть preview: '{failing_part_preview}'): {e_text}",
                        exc_info=True)
                    error_text = f"⚠️ Не удалось отправить часть отчета '{email_data.get('subject', '')}' (текст)."
                    try:
                        # Пытаемся отправить уведомление об ошибке
                        self._send_telegram_message_with_retry(self.bot.send_message, chat_id, error_text)
                    except Exception as fallback_err:
                        logger.error(
                            f"Не удалось отправить уведомление об ошибке отправки текста ({chat_id}): {fallback_err}")
                    return False  # Ошибка при отправке текста

        # --- 7. Общая обработка непредвиденных ошибок ---
        except Exception as e_main:
            logger.error(f"Критическая ошибка в _send_to_telegram_now ({chat_id}): {e_main}", exc_info=True)
            try:
                # Отправляем общее уведомление об ошибке
                error_text = f"⚠️ Произошла критическая ошибка при обработке отчета '{email_data.get('subject', '')}'. Обратитесь к администратору."
                self._send_telegram_message_with_retry(self.bot.send_message, chat_id, error_text)
            except Exception as fallback_err:
                logger.error(f"Не удалось отправить уведомление об общей ошибке ({chat_id}): {fallback_err}")
            return False # Критическая ошибка

    # --- НОВЫЙ МЕТОД: Обертка для отправки с retry ---
    def _send_telegram_message_with_retry(self, send_func, *args, **kwargs):
        """Отправляет сообщение через Telegram API с логикой повторных попыток."""
        last_exception = None
        for attempt in range(MAX_RETRIES):
            try:
                # Убираем parse_mode если он None, т.к. send_document его не принимает явно в некоторых версиях
                # Но если parse_mode='MarkdownV2' или другой, оставляем его.
                current_parse_mode = kwargs.get('parse_mode')
                if current_parse_mode is None and 'parse_mode' in kwargs:
                    del kwargs['parse_mode']

                return send_func(*args, **kwargs)

            except telebot.apihelper.ApiTelegramException as e:
                 last_exception = e
                 # Обрабатываем специфичные ошибки Telegram
                 # --- УЛУЧШЕННАЯ ПРОВЕРКА ОШИБКИ ПАРСИНГА ---
                 if e.error_code == 400 and "can't parse entities" in str(e).lower():
                      problem_text_preview = "N/A"
                      # Пытаемся получить текст из аргументов (обычно второй аргумент для send_message)
                      # или из caption в kwargs
                      if len(args) > 1 and isinstance(args[1], str):
                           problem_text = args[1]
                           problem_text_preview = problem_text[:200] + ('...' if len(problem_text) > 200 else '')
                      elif 'caption' in kwargs and isinstance(kwargs['caption'], str):
                           problem_text = kwargs['caption']
                           problem_text_preview = problem_text[:200] + ('...' if len(problem_text) > 200 else '')

                      logger.error(
                          f"Ошибка парсинга Markdown/HTML в Telegram для чата {args[0]} "
                          f"(parse_mode='{current_parse_mode}', text/caption preview: '{problem_text_preview}'): {e}. "
                          f"Проверьте функцию экранирования или наличие незакрытых тегов/символов."
                      )
                      # Прерываем попытки, так как повтор не поможет с неправильным форматированием
                      break
                 # --- КОНЕЦ УЛУЧШЕННОЙ ПРОВЕРКИ ---
                 elif e.error_code == 400 and 'message is too long' in str(e).lower():
                      logger.error(f"Ошибка отправки: Сообщение слишком длинное для чата {args[0]} ({e})")
                      break # Прерываем, разбиение должно было произойти раньше
                 elif e.error_code == 403: # Forbidden: bot was blocked by the user, etc.
                      logger.error(f"Ошибка отправки (403 Forbidden) для чата {args[0]}: {e}. Пользователь мог заблокировать бота.")
                      # TODO: Возможно, стоит деактивировать пользователя в БД?
                      break # Нет смысла повторять
                 elif e.error_code == 429: # Too Many Requests
                      # Пытаемся получить время ожидания из ответа API
                      retry_after = RETRY_DELAY * (2 ** attempt) # Fallback
                      try:
                          # result_json может быть недоступен или не содержать нужных данных
                          if hasattr(e, 'result_json') and isinstance(e.result_json, dict):
                              retry_after = e.result_json.get('parameters', {}).get('retry_after', retry_after)
                      except Exception: pass # Игнорируем ошибки парсинга retry_after
                      wait_time = max(1, retry_after) # Ждем минимум 1 секунду
                      logger.warning(f"Ошибка отправки (429 Too Many Requests) для чата {args[0]}: {e}. Повтор через {wait_time}с (попытка {attempt + 1}/{MAX_RETRIES})")
                      time.sleep(wait_time)
                 elif attempt < MAX_RETRIES - 1:
                      wait_time = RETRY_DELAY * (2 ** attempt)
                      logger.warning(f"Ошибка API Telegram при отправке ({send_func.__name__}) для чата {args[0]} (попытка {attempt + 1}/{MAX_RETRIES}): {e}. Повтор через {wait_time}с")
                      time.sleep(wait_time)
                 else:
                      logger.error(f"Не удалось отправить сообщение через API Telegram ({send_func.__name__}) для чата {args[0]} после {MAX_RETRIES} попыток: {e}")

            except Exception as e: # Ловим другие возможные ошибки (сетевые и т.д.)
                 last_exception = e
                 if attempt < MAX_RETRIES - 1:
                     wait_time = RETRY_DELAY * (2 ** attempt)
                     logger.warning(f"Сетевая или другая ошибка при отправке ({send_func.__name__}) для чата {args[0]} (попытка {attempt + 1}/{MAX_RETRIES}): {e}. Повтор через {wait_time}с")
                     time.sleep(wait_time)
                 else:
                     logger.error(f"Не удалось отправить сообщение ({send_func.__name__}) для чата {args[0]} после {MAX_RETRIES} попыток из-за не-API ошибки: {e}")

        # Если все попытки не удались, пробрасываем последнее исключение
        if last_exception:
            raise last_exception


    def send_attachment_with_message(self, chat_id: str, attachment: Dict[str, Any], message: str) -> None:
        """
        Отправка вложения вместе с текстом сообщения (использует TemporaryFileManager).
        Ожидает, что 'message' уже содержит заголовок и ЭКРАНИРОВАННОЕ тело.
        Использует parse_mode='MarkdownV2' для caption.
        """
        # Используем контекстный менеджер для временных файлов
        with TemporaryFileManager(prefix=f"att_msg_{chat_id}_") as temp_dir:
            safe_filename = "attachment.bin" # Default
            try:
                filename = attachment.get('filename', 'attachment.bin')
                content = attachment.get('content')
                content_type = attachment.get('content_type', 'application/octet-stream')

                if not content:
                     logger.warning(f"Пустое содержимое для вложения '{filename}', пропускаем.")
                     # Отправляем только текст (он уже экранирован и с заголовком)
                     # Используем MarkdownV2, так как 'message' содержит заголовок в этом формате
                     self._send_telegram_message_with_retry(
                         self.bot.send_message, chat_id, message,
                         parse_mode='MarkdownV2', disable_web_page_preview=True
                         )
                     return

                safe_filename = re.sub(r'[<>:"/\\|?*]', '_', filename)
                temp_file_path = os.path.join(temp_dir, safe_filename)

                with open(temp_file_path, 'wb') as temp_file:
                    temp_file.write(content)
                logger.debug(f"Создан временный файл: {temp_file_path} для вложения {filename}")

                file_size = os.path.getsize(temp_file_path)
                MAX_TG_FILE_SIZE = 50 * 1024 * 1024
                MAX_TG_CAPTION_LEN = 1024
                if file_size > MAX_TG_FILE_SIZE:
                    logger.warning(f"Вложение {filename} слишком большое ({file_size / (1024 * 1024):.2f} МБ)")
                    # Отправляем текст и предупреждение о файле
                    self._send_telegram_message_with_retry(
                        self.bot.send_message, chat_id, message,
                        parse_mode='MarkdownV2', disable_web_page_preview=True
                        )
                    # Предупреждение отправляем без parse_mode
                    self._send_telegram_message_with_retry(
                        self.bot.send_message, chat_id,
                        f"⚠️ Вложение '{safe_filename}' ({file_size / (1024 * 1024):.2f} МБ) не отправлено (слишком большое)."
                        )
                    return

                # Ограничиваем длину caption (message уже содержит экранирование)
                # Добавляем троеточие, если обрезали
                caption = message[:MAX_TG_CAPTION_LEN - 3] + "..." if len(message) > MAX_TG_CAPTION_LEN else message

                # Определяем метод отправки
                send_method = self.bot.send_document
                if content_type.startswith('image/'): send_method = self.bot.send_photo
                elif content_type.startswith('video/'): send_method = self.bot.send_video
                elif content_type.startswith('audio/'): send_method = self.bot.send_audio

                # Отправляем с retry
                with open(temp_file_path, 'rb') as file_to_send:
                     # Устанавливаем parse_mode='MarkdownV2' для caption
                     # visible_file_name нужен только для send_document
                     send_kwargs = {
                         'caption': caption,
                         'parse_mode': 'MarkdownV2' # Здесь нужен parse_mode для заголовка в caption
                     }
                     # Для send_document нужно добавить visible_file_name
                     # Для других методов (photo, video, audio) этот параметр не нужен или вызовет ошибку
                     if send_method == self.bot.send_document:
                         send_kwargs['visible_file_name'] = safe_filename

                     self._send_telegram_message_with_retry(
                          send_method,
                          chat_id,
                          file_to_send,
                          **send_kwargs
                     )
                logger.info(f"Вложение '{filename}' отправлено с сообщением для {chat_id}")

            except Exception as e:
                logger.error(f"Ошибка при отправке вложения '{safe_filename}' с сообщением для {chat_id}: {e}", exc_info=True)
                # Пытаемся отправить хотя бы текст сообщения
                try:
                    # Отправляем исходный message (уже с заголовком и экранированием) с MarkdownV2
                    self._send_telegram_message_with_retry(
                        self.bot.send_message, chat_id, message,
                        parse_mode='MarkdownV2', disable_web_page_preview=True
                        )
                    # Отправляем предупреждение об ошибке вложения (без parse_mode)
                    failed_filename = self.escape_markdown_v2(attachment.get('filename', 'N/A')) # Экранируем имя файла для безопасности
                    self._send_telegram_message_with_retry(
                        self.bot.send_message, chat_id, f"⚠️ Не удалось отправить вложение: {failed_filename}"
                        )
                except Exception as fallback_e:
                    logger.error(f"Не удалось отправить даже текст сообщения после ошибки вложения: {fallback_e}")
        # Очистка временной директории произойдет автоматически при выходе из with

    def split_text(self, text: str, max_length: int = 4096) -> List[str]:
        """ Разбивает текст на части. """
        # ... (без изменений) ...
        parts = []
        safety_margin = 20 # Запас для префиксов и непредвиденных символов
        limit = max_length - safety_margin

        if limit <= 0:
            logger.error(f"Невозможно разбить текст: max_length ({max_length}) слишком мал.")
            return [text[:max_length]] if text else [] # Обрезаем до max_length

        current_pos = 0
        text_len = len(text)

        while current_pos < text_len:
            # Конец среза
            end_pos = min(current_pos + limit, text_len)

            # Если оставшийся текст помещается в одну часть (уже с учетом полной max_length)
            # Проверяем <= max_length, т.к. последняя часть может быть длиннее limit
            if text_len - current_pos <= max_length:
                parts.append(text[current_pos:])
                break

            # Ищем последний перенос строки в срезе
            split_at = text.rfind('\n', current_pos, end_pos)

            # Если переноса нет или он в самом начале, ищем последний пробел
            if split_at == -1 or split_at == current_pos:
                split_at = text.rfind(' ', current_pos, end_pos)
                # Если и пробела нет, режем по лимиту
                if split_at == -1 or split_at == current_pos:
                     # Проверяем, не будет ли end_pos совпадать с началом следующей части
                     if end_pos == current_pos: end_pos += 1 # Сдвигаем на 1, если застряли
                     split_at = end_pos


            # Добавляем часть до точки разреза
            parts.append(text[current_pos:split_at])

            # Обновляем позицию, пропуская сам разделитель (перенос или пробел)
            current_pos = split_at + 1
            # Пропускаем пробельные символы в начале следующей части
            while current_pos < text_len and text[current_pos].isspace():
                current_pos += 1

        # Фильтруем пустые строки
        result_parts = [part for part in parts if part and not part.isspace()]

        if not result_parts and text:
            logger.warning("split_text не смог разбить текст, возвращаем обрезанный исходник.")
            return [text[:max_length]] # Возвращаем обрезанный

        return result_parts


    def send_attachment_to_telegram(self, chat_id: str, attachment: Dict[str, Any]) -> None:
        """
        Отправка вложения в Telegram (использует TemporaryFileManager).
        """
        # Используем контекстный менеджер
        with TemporaryFileManager(prefix=f"att_{chat_id}_") as temp_dir:
            try:
                filename = attachment.get('filename', 'attachment.bin')
                content = attachment.get('content')
                content_type = attachment.get('content_type', 'application/octet-stream')

                if not content:
                    logger.warning(f"Пустое содержимое для отдельного вложения '{filename}', пропускаем.")
                    return

                safe_filename = re.sub(r'[<>:"/\\|?*]', '_', filename)
                caption = safe_filename[:1020] + "..." if len(safe_filename) > 1024 else safe_filename # Ограничение caption
                temp_file_path = os.path.join(temp_dir, safe_filename)

                with open(temp_file_path, 'wb') as temp_file:
                    temp_file.write(content)
                logger.debug(f"Создан временный файл: {temp_file_path} для вложения {filename}")

                file_size = os.path.getsize(temp_file_path)
                MAX_TG_FILE_SIZE = 50 * 1024 * 1024
                if file_size > MAX_TG_FILE_SIZE:
                    logger.warning(f"Вложение {filename} слишком большое ({file_size / (1024 * 1024):.2f} МБ)")
                    self._send_telegram_message_with_retry(self.bot.send_message, chat_id, f"⚠️ Вложение '{safe_filename}' ({file_size / (1024 * 1024):.2f} МБ) не отправлено (слишком большое).")
                    return

                # Определяем метод отправки
                send_method = self.bot.send_document
                if content_type.startswith('image/'): send_method = self.bot.send_photo
                elif content_type.startswith('video/'): send_method = self.bot.send_video
                elif content_type.startswith('audio/'): send_method = self.bot.send_audio

                # Отправляем с retry
                with open(temp_file_path, 'rb') as file_to_send:
                     self._send_telegram_message_with_retry(
                          send_method,
                          chat_id,
                          file_to_send,
                          caption=caption,
                          # parse_mode не нужен для caption файла
                          visible_file_name=safe_filename # Для send_document
                     )
                logger.info(f"Отдельное вложение '{filename}' отправлено для {chat_id}")

            except Exception as e:
                logger.error(f"Ошибка при отправке отдельного вложения для {chat_id} (файл: {attachment.get('filename')}): {e}", exc_info=True)
                try:
                    self._send_telegram_message_with_retry(self.bot.send_message, chat_id, f"⚠️ Не удалось отправить вложение: {attachment.get('filename', 'N/A')}")
                except Exception: pass
        # Очистка временной директории произойдет автоматически

    def mark_as_read(self, mail: imaplib.IMAP4_SSL, msg_id: bytes) -> None:
        """ Отметить письмо как прочитанное. """
        # ... (без изменений) ...
        for attempt in range(MAX_RETRIES):
            try:
                logger.debug(f"Попытка {attempt+1} отметить письмо {msg_id.decode()} как прочитанное...")
                status, data = mail.store(msg_id, '+FLAGS', '\\Seen')
                if status == 'OK':
                    logger.debug(f"Письмо {msg_id.decode()} успешно отмечено как прочитанное.")
                    return
                else:
                    # Если сервер вернул не OK, возможно, ID невалиден или что-то еще
                    logger.warning(f"Не удалось отметить письмо {msg_id.decode()} как прочитанное (статус: {status}, данные: {data}). Прерываем попытки.")
                    return # Прекращаем попытки
            except (imaplib.IMAP4.error, imaplib.IMAP4.abort) as e:
                if attempt < MAX_RETRIES - 1:
                    wait_time = RETRY_DELAY * (2 ** attempt)
                    logger.warning(
                        f"Ошибка IMAP при отметке письма {msg_id.decode()} как прочитанного (попытка {attempt + 1}/{MAX_RETRIES}): {e}. Повтор через {wait_time}с")
                    time.sleep(wait_time)
                    # Попробуем переподключиться
                    try: self._get_mail_connection()
                    except: logger.error("Не удалось переподключиться к почте во время retry.")
                else:
                    logger.error(
                        f"Не удалось отметить письмо {msg_id.decode()} как прочитанное после {MAX_RETRIES} попыток: {e}")
            except Exception as e:
                logger.error(f"Непредвиденная ошибка при отметке письма {msg_id.decode()} как прочитанного: {e}", exc_info=True)
                return # Прерываем попытки


    def get_email_subject(self, mail: imaplib.IMAP4_SSL, msg_id: bytes) -> Optional[str]:
        """ Получить только заголовок письма. """
        # ... (без изменений) ...
        try:
            # Получаем только заголовок письма
            logger.debug(f"Извлечение заголовка для письма {msg_id.decode()}...")
            status, msg_data = mail.fetch(msg_id, "(BODY.PEEK[HEADER.FIELDS (SUBJECT FROM DATE)])") # Добавим From и Date для полноты
            if status != "OK" or not msg_data or not msg_data[0] or not isinstance(msg_data[0], tuple) or len(msg_data[0]) < 2:
                logger.warning(f"Не удалось получить заголовок письма {msg_id.decode()} (статус: {status}, данные: {msg_data})")
                return None

            # Извлекаем заголовок
            header_data = msg_data[0][1]
            if not isinstance(header_data, bytes):
                 logger.warning(f"Некорректный тип данных для header_data письма {msg_id.decode()}: {type(header_data)}")
                 return None

            # Используем email parser для надежности
            parser = email.parser.BytesHeaderParser()
            header = parser.parsebytes(header_data)

            subject = self.decode_mime_header(header.get("Subject", "Без темы"))
            subject = self.clean_subject(subject)
            logger.debug(f"Извлечена тема '{subject}' для письма {msg_id.decode()}.")

            return subject
        except (imaplib.IMAP4.error, imaplib.IMAP4.abort) as e:
             logger.error(f"Ошибка IMAP при извлечении заголовка письма {msg_id.decode()}: {e}")
             # Сбрасываем соединение
             with self._mail_lock:
                if self._mail_connection == mail:
                    try: mail.close(); mail.logout()
                    except: pass
                    self._mail_connection = None
             return None
        except Exception as e:
            logger.error(f"Непредвиденная ошибка при извлечении заголовка письма {msg_id.decode()}: {e}", exc_info=True)
            return None


    def _process_email_worker(self) -> None:
        """ Рабочий поток для обработки писем из очереди (отправка в Telegram). """
        # ... (без изменений) ...
        while not self.stop_event.is_set():
            try:
                # Получаем задачу из очереди с таймаутом
                try:
                    email_data, matching_subjects = self.email_queue.get(timeout=1)
                except queue.Empty:
                    continue # Если очередь пуста, просто проверяем stop_event и ждем дальше

                if not email_data or not matching_subjects:
                    logger.warning("Получены некорректные данные из очереди email_queue.")
                    self.email_queue.task_done()
                    continue

                processed_chat_ids = set() # Для предотвращения дублирования отправок по разным шаблонам одному юзеру
                # Обрабатываем письмо для каждого совпавшего чата
                for subject_pattern, chat_id in matching_subjects:
                     if chat_id in processed_chat_ids:
                          logger.debug(f"Пропуск дублирующей отправки для chat_id {chat_id} (шаблон '{subject_pattern}')")
                          continue

                     logger.info(f"Запуск отправки письма с темой '{email_data.get('subject', 'N/A')}' для чата {chat_id} (шаблон: '{subject_pattern}')")
                     # Вызываем send_to_telegram, который сам решает - отправить сразу или отложить
                     self.send_to_telegram(chat_id, email_data)
                     processed_chat_ids.add(chat_id)

                # Отмечаем задачу как выполненную (один раз для всего пакета совпадений)
                self.email_queue.task_done()
                logger.debug("Задача из email_queue обработана.")

            except Exception as e:
                logger.error(f"Ошибка в рабочем потоке обработки писем (_process_email_worker): {e}", exc_info=True)
                 # Небольшая пауза в случае ошибки, чтобы не загружать CPU
                time.sleep(1)


    def _start_workers(self) -> None:
        """ Запуск рабочих потоков для обработки писем из очереди. """
        # ... (без изменений) ...
        if self.workers: # Если потоки уже есть, не запускаем новые
            logger.debug("Рабочие потоки обработки email уже запущены.")
            return

        self.stop_event.clear() # Убедимся, что флаг снят
        for i in range(MAX_WORKERS):
            worker = threading.Thread(
                target=self._process_email_worker,
                name=f"EmailQueueWorker-{i}",
                daemon=True
            )
            worker.start()
            self.workers.append(worker)
        logger.info(f"Запущено {MAX_WORKERS} рабочих потоков для обработки email из очереди.")

    def _stop_workers(self) -> None:
        """ Остановка рабочих потоков обработки очереди email. """
        # ... (без изменений) ...
        # self.stop_event.set() # Не ставим здесь, т.к. он общий
        logger.info("Остановка рабочих потоков обработки email...")
        active_workers = []
        for worker in self.workers:
            if worker.is_alive():
                try:
                    worker.join(timeout=2)
                    if worker.is_alive():
                         logger.warning(f"Поток {worker.name} не завершился вовремя.")
                    else:
                         logger.debug(f"Поток {worker.name} завершен.")
                except Exception as e:
                     logger.error(f"Ошибка при ожидании потока {worker.name}: {e}")
            else:
                 logger.debug(f"Поток {worker.name} уже был неактивен.")

        self.workers = [] # Очищаем список в любом случае
        logger.info("Рабочие потоки обработки email остановлены.")


    def process_emails(self) -> None:
        """ Оптимизированная функция обработки писем. """
        logger.info("--- Начало цикла проверки почты ---")
        start_time = time.time()

        try:
            # Повторная загрузка данных о клиентах
            self.reload_client_data()

            # Если нет активных клиентов/шаблонов, пропускаем
            if not self._subject_patterns:
                logger.info("Нет активных шаблонов для проверки почты, пропускаем цикл.")
                return

            # Подключение к почтовому серверу
            try:
                mail = self._get_mail_connection()
                if not mail: # Если _get_mail_connection вернул None (маловероятно, но возможно)
                    logger.error("Не удалось получить соединение с почтовым сервером.")
                    return
            except Exception as conn_err:
                 logger.error(f"Критическая ошибка при получении соединения с почтой: {conn_err}", exc_info=True)
                 return # Прерываем цикл обработки, если не можем подключиться

            # Получение непрочитанных писем
            msg_ids = self.get_all_unseen_emails(mail)

            if not msg_ids:
                logger.info("Нет новых непрочитанных писем.")
                return

            # Запускаем рабочие потоки, если они еще не запущены
            if not self.workers:
                self._start_workers()

            emails_processed_count = 0
            notifications_potential = 0
            emails_to_mark_read = []
            emails_to_mark_unread = [] # Письма, которые точно нужно оставить непрочитанными

            # Обработка каждого письма
            for msg_id in msg_ids:
                try:
                    # Сначала получаем только тему
                    subject = self.get_email_subject(mail, msg_id)

                    if subject is None: # Если get_email_subject вернул None из-за ошибки
                        logger.warning(f"Не удалось получить тему письма {msg_id.decode()}, пропускаем")
                        continue

                    # Проверка соответствия темы
                    matching_subjects = self.check_subject_match(subject)

                    if matching_subjects:
                        # Если есть совпадение, загружаем полное содержимое
                        logger.info(f"Тема '{subject}' совпала. Извлечение полного письма {msg_id.decode()}...")
                        email_data = self.extract_email_content(mail, msg_id)

                        if email_data:
                            emails_processed_count += 1
                            notifications_potential += len(matching_subjects)
                            # Добавляем в очередь для отправки
                            self.email_queue.put((email_data, matching_subjects))
                            emails_to_mark_read.append(msg_id)
                            logger.debug(f"Письмо {msg_id.decode()} добавлено в очередь на отправку.")
                        else:
                            logger.warning(f"Не удалось извлечь содержимое письма {msg_id.decode()} после совпадения темы. Оставляем непрочитанным.")
                            # Если не удалось извлечь, лучше оставить непрочитанным
                            emails_to_mark_unread.append(msg_id)
                    else:
                        # Если тема не совпала, оставляем непрочитанным
                        # logger.info(f"Письмо {msg_id.decode()} с темой '{subject}' не соответствует шаблонам, оставляем непрочитанным.")
                        # Явно помечаем как непрочитанное на случай, если PEEK изменил статус (хотя не должен)
                        emails_to_mark_unread.append(msg_id)

                except Exception as loop_err:
                    logger.error(f"Ошибка при обработке письма {msg_id.decode()} в цикле: {loop_err}", exc_info=True)
                    # Стараемся продолжить обработку следующих писем

            # Отмечаем письма как прочитанные (те, что были обработаны)
            if emails_to_mark_read:
                 logger.info(f"Пометка {len(emails_to_mark_read)} писем как прочитанных...")
                 for msg_id in emails_to_mark_read:
                     self.mark_as_read(mail, msg_id)

            # Отмечаем письма как непрочитанные (те, что не совпали или не обработались)
            if emails_to_mark_unread:
                logger.info(f"Пометка {len(emails_to_mark_unread)} писем как непрочитанных...")
                for msg_id in emails_to_mark_unread:
                    self.mark_as_unread(mail, msg_id) # Убедимся, что они точно не прочитаны

            # Не ждем завершения email_queue здесь, worker'ы работают асинхронно

            elapsed_time = time.time() - start_time
            logger.info(
                f"Цикл проверки почты завершен за {elapsed_time:.2f} сек. "
                f"Обработано писем: {emails_processed_count}, "
                f"Потенциальных уведомлений: {notifications_potential} (в очереди: {self.email_queue.qsize()})"
            )

        except Exception as e:
            logger.error(f"Критическая ошибка в цикле проверки почты: {e}", exc_info=True)
            # Сбрасываем почтовое соединение при критической ошибке
            with self._mail_lock:
                if self._mail_connection:
                    try: self._mail_connection.close(); self._mail_connection.logout()
                    except: pass
                    self._mail_connection = None
        finally:
             logger.info("--- Конец цикла проверки почты ---")


    def test_connections(self) -> Dict[str, bool]:
        """ Тестирование подключений к серверам. """
        # ... (без изменений) ...
        results = {"mail": False, "telegram": False}
        logger.info("Тестирование соединений...")

        # Проверка почтового сервера
        try:
            logger.debug("Тестирование IMAP соединения...")
            test_mail = imaplib.IMAP4_SSL(self.email_server, timeout=CONNECTION_TIMEOUT)
            test_mail.login(self.email_account, self.password)
            test_mail.select("inbox")
            test_mail.close()
            test_mail.logout()
            logger.info("Подключение к почтовому серверу (IMAP) успешно.")
            results["mail"] = True
        except Exception as e:
            logger.error(f"Ошибка при тестировании почтового соединения (IMAP): {e}")

        # Проверка подключения к Telegram API
        try:
            logger.debug("Тестирование Telegram API...")
            test_message = self.bot.get_me()
            logger.info(f"Подключение к Telegram API успешно. Бот: {test_message.username} ({test_message.first_name})")
            results["telegram"] = True
        except Exception as e:
            logger.error(f"Ошибка при тестировании Telegram API: {e}")

        logger.info(f"Результаты тестирования: {results}")
        return results

    def start_scheduler(self, interval: int = 5) -> None:
        """ Запуск планировщика для регулярной проверки почты. """
        # ... (без изменений, но добавим запуск/остановку нового планировщика) ...

        # Настройка расписания основной проверки почты
        self.check_interval = interval
        schedule.clear() # Очищаем предыдущие задачи на всякий случай
        schedule.every(interval).minutes.do(self.process_emails)
        logger.info(f"Планировщик основной проверки почты настроен. Интервал: {interval} минут")

        # Запускаем рабочие потоки для обработки очереди email
        self._start_workers()

        # --- ЗАПУСК ПЛАНИРОВЩИКА ОТЛОЖЕННЫХ ОТПРАВОК ---
        self.delayed_sender.start()

        # Запускаем проверку сразу
        logger.info("Первый запуск проверки почты...")
        try:
             self.process_emails()
        except Exception as first_run_err:
             logger.error(f"Ошибка при первом запуске process_emails: {first_run_err}", exc_info=True)

        # Основной цикл ожидания schedule
        logger.info("Вход в основной цикл ожидания schedule...")
        while not self.stop_event.is_set():
            try:
                schedule.run_pending()
                # Используем wait с проверкой события остановки
                # Проверяем каждую секунду, чтобы быстрее реагировать на stop_event
                self.stop_event.wait(timeout=1)
            except KeyboardInterrupt:
                logger.info("Получен сигнал KeyboardInterrupt, остановка...")
                self.stop_event.set() # Устанавливаем флаг для других потоков
                break
            except Exception as e:
                logger.error(f"Ошибка в основном цикле schedule: {e}", exc_info=True)
                # Пауза перед следующей попыткой
                time.sleep(5)

        logger.info("Основной цикл schedule завершен.")
        # Остановка компонентов будет в shutdown


    def shutdown(self) -> None:
        """ Корректное завершение работы форвардера. """
        logger.info("Завершение работы форвардера EmailTelegramForwarder...")

        # 1. Устанавливаем флаг остановки (если еще не установлен)
        self.stop_event.set()

        # 2. Останавливаем основной планировщик (schedule) - он остановится сам в цикле

        # 3. Останавливаем рабочие потоки обработки очереди email
        self._stop_workers()

        # 4. Очищаем очередь email
        logger.debug("Очистка очереди email...")
        cleared_count = 0
        while not self.email_queue.empty():
            try:
                self.email_queue.get_nowait()
                self.email_queue.task_done()
                cleared_count += 1
            except queue.Empty:
                break
            except Exception as q_err:
                 logger.warning(f"Ошибка при очистке email_queue: {q_err}")
                 break
        logger.debug(f"Очищено {cleared_count} элементов из email_queue.")


        # 5. Останавливаем планировщик отложенных отправок
        if self.delayed_sender:
            self.delayed_sender.stop()

        # 6. Закрытие соединения с почтовым сервером
        logger.debug("Закрытие соединения с почтовым сервером...")
        try:
            with self._mail_lock:
                if self._mail_connection:
                    try:
                        self._mail_connection.close()
                        self._mail_connection.logout()
                        logger.debug("Соединение с почтовым сервером закрыто.")
                    except Exception as mail_close_err:
                         logger.warning(f"Ошибка при закрытии соединения с почтовым сервером: {mail_close_err}")
                    finally:
                         self._mail_connection = None
        except Exception as e:
            logger.error(f"Ошибка при доступе к блокировке почтового соединения во время shutdown: {e}")

        logger.info("Форвардер EmailTelegramForwarder успешно завершил работу.")


def main():
    """Основная функция для запуска форвардера."""
    # ... (без изменений) ...
    forwarder = None
    try:
        logger.info("Инициализация EmailTelegramForwarder...")
        forwarder = EmailTelegramForwarder()

        logger.info("Тестирование соединений перед запуском...")
        connections = forwarder.test_connections()

        if not connections.get("mail", False):
            logger.error("Не удалось подключиться к почтовому серверу. Проверьте настройки. Завершение работы.")
            return

        if not connections.get("telegram", False):
            logger.error("Не удалось подключиться к Telegram API. Проверьте токен. Завершение работы.")
            return

        logger.info("Запуск планировщика проверки писем...")
        # Интервал берется из настроек внутри forwarder'а
        forwarder.start_scheduler(interval=settings.CHECK_INTERVAL)

    except KeyboardInterrupt:
        logger.info("Программа остановлена пользователем (KeyboardInterrupt).")
    except Exception as e:
        logger.critical(f"Критическая ошибка при запуске/работе программы: {e}", exc_info=True)
    finally:
        if forwarder:
            logger.info("Начало процедуры завершения работы forwarder...")
            try:
                forwarder.shutdown()
            except Exception as e_shut:
                logger.error(f"Ошибка при завершении работы программы: {e_shut}", exc_info=True)
        logger.info("Программа завершила работу.")


if __name__ == "__main__":
    main()