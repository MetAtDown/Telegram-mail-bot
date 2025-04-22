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
from functools import lru_cache
from typing import Dict, List, Tuple, Any, Optional, Set
from email.header import decode_header
from bs4 import BeautifulSoup, NavigableString
import html
from collections import defaultdict

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
DEFAULT_DELIVERY_MODE = DELIVERY_MODE_SMART

class EmailTelegramForwarder:
    def __init__(self, db_manager=None):
        """
        Инициализация форвардера писем в Telegram.

        Args:
            db_manager: Экземпляр менеджера базы данных
        """
        # Загрузка настроек
        self.email_account = settings.EMAIL_ACCOUNT
        self.password = settings.EMAIL_PASSWORD
        self.telegram_token = settings.TELEGRAM_TOKEN
        self.email_server = settings.EMAIL_SERVER  # Использование из настроек
        self.check_interval = settings.CHECK_INTERVAL

        if not all([self.email_account, self.password, self.telegram_token]):
            logger.error("Не все обязательные параметры найдены в настройках")
            raise ValueError("Отсутствуют обязательные параметры в настройках")

        # Устанавливаем менеджер базы данных
        if db_manager is None:
            from src.db.manager import DatabaseManager
            self.db_manager = DatabaseManager()
        else:
            self.db_manager = db_manager

        # Инициализация Telegram бота
        self.bot = telebot.TeleBot(self.telegram_token, threaded=True)

        # Словарь для кэширования данных о клиентах
        self.client_data = {}
        self.user_states = {}

        # Многопоточная обработка писем
        self.email_queue = queue.Queue()
        self.workers = []
        self.stop_event = threading.Event()

        # Кэш для соединений с почтовым сервером
        self._mail_connection = None
        self._mail_lock = threading.RLock()
        self._last_connection_time = 0
        self._connection_idle_timeout = 300  # 5 минут

        # Cached patterns for faster subject matching
        self._subject_patterns = {}

        # Rate limiting
        self._message_timestamps = {}
        self._rate_limit_lock = threading.RLock()
        self._max_messages_per_minute = 20  # Maximum messages per minute per chat

        # Загрузка данных о клиентах
        self.reload_client_data()

        # Префиксы для очистки тем писем (кэшируем для быстрого доступа)
        self.subject_prefixes = ["[deeray.com] ", "Re: ", "Fwd: ", "Fw: "]

    def reload_client_data(self) -> None:
        """Загрузка данных о клиентах из базы данных с оптимизацией кэширования."""
        try:
            # Получаем все темы и связанные с ними chat_id и статусы
            self.client_data = self.db_manager.get_all_subjects()

            # Предварительно обрабатываем шаблоны для быстрого сопоставления
            self._subject_patterns = {}
            for subject_pattern, clients in self.client_data.items():
                subject_lower = subject_pattern.lower()
                for client in clients:
                    if client["enabled"]:
                        if subject_lower not in self._subject_patterns:
                            self._subject_patterns[subject_lower] = []
                        self._subject_patterns[subject_lower].append((subject_pattern, client["chat_id"]))

            unique_subjects = len(self.client_data)
            total_records = sum(len(clients) for clients in self.client_data.values())

            # Получаем состояния всех пользователей
            self.user_states = self.db_manager.get_all_users()

            logger.info(f"Загружено {unique_subjects} уникальных тем и {total_records} записей из базы данных")
        except Exception as e:
            logger.error(f"Ошибка при загрузке данных о клиентах: {e}")
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
                    self._mail_connection.close()
                    self._mail_connection.logout()
                except Exception:
                    pass
                self._mail_connection = None
                logger.debug("Закрыто неактивное соединение с почтовым сервером")

            # Создаем новое соединение, если необходимо
            if self._mail_connection is None:
                for attempt in range(MAX_RETRIES):
                    try:
                        mail = imaplib.IMAP4_SSL(self.email_server, timeout=CONNECTION_TIMEOUT)
                        mail.login(self.email_account, self.password)
                        mail.select("inbox")
                        self._mail_connection = mail
                        self._last_connection_time = current_time
                        logger.debug("Успешное подключение к почтовому серверу")
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
                    status, _ = self._mail_connection.noop()
                    if status != 'OK':
                        raise Exception("Соединение неактивно")
                except Exception as e:
                    logger.warning(f"Соединение с почтовым сервером прервано: {e}. Пересоздание...")
                    try:
                        self._mail_connection.close()
                        self._mail_connection.logout()
                    except Exception:
                        pass
                    self._mail_connection = None
                    return self._get_mail_connection()  # Рекурсивный вызов для создания нового соединения

            return self._mail_connection

    def connect_to_mail(self) -> imaplib.IMAP4_SSL:
        """
        Подключение к почтовому серверу (обертка для обратной совместимости).

        Returns:
            Объект соединения с почтовым сервером
        """
        return self._get_mail_connection()

    def get_all_unseen_emails(self, mail: imaplib.IMAP4_SSL) -> List[bytes]:
        """
        Получение всех непрочитанных писем с ограничением количества за одну обработку.

        Args:
            mail: Соединение с почтовым сервером

        Returns:
            Список ID непрочитанных писем
        """
        try:
            status, messages = mail.search(None, 'UNSEEN')
            if status != "OK":
                logger.warning("Проблема при поиске непрочитанных писем")
                return []

            msg_ids = messages[0].split()
            total_msgs = len(msg_ids)

            # Ограничиваем количество писем для обработки за один раз
            if total_msgs > MAX_BATCH_SIZE:
                logger.info(
                    f"Найдено {total_msgs} непрочитанных писем, ограничиваем до {MAX_BATCH_SIZE} для текущей обработки")
                msg_ids = msg_ids[:MAX_BATCH_SIZE]
            else:
                logger.info(f"Найдено {len(msg_ids)} непрочитанных писем")

            return msg_ids
        except Exception as e:
            logger.error(f"Ошибка при получении непрочитанных писем: {e}")
            return []

    @lru_cache(maxsize=128)
    def decode_mime_header(self, header: str) -> str:
        """
        Декодирование MIME-заголовков с кэшированием результатов.

        Args:
            header: Заголовок для декодирования

        Returns:
            Декодированный заголовок
        """
        try:
            decoded_parts = decode_header(header)
            decoded_str = ""

            for part, encoding in decoded_parts:
                if isinstance(part, bytes):
                    decoded_str += part.decode(encoding or 'utf-8', errors='replace')
                else:
                    decoded_str += str(part)

            return decoded_str
        except Exception as e:
            logger.error(f"Ошибка при декодировании заголовка: {e}")
            return header

    def extract_email_content(self, mail: imaplib.IMAP4_SSL, msg_id: bytes) -> Optional[Dict[str, Any]]:
        """
        Извлечение содержимого письма по его ID с сохранением raw HTML.

        Args:
            mail: Соединение с почтовым сервером
            msg_id: ID письма

        Returns:
            Словарь с данными письма или None в случае ошибки
        """
        try:
            # Получаем письмо целиком
            status, msg_data = mail.fetch(msg_id, "(BODY.PEEK[])")
            if status != "OK" or not msg_data or not msg_data[0]:
                logger.warning(f"Не удалось получить письмо {msg_id}")
                return None

            # Парсим письмо
            raw_email = msg_data[0][1]
            email_message = email.message_from_bytes(raw_email)

            # Извлекаем тему
            subject = self.decode_mime_header(email_message.get("Subject", "Без темы"))
            subject = self.clean_subject(subject)

            # Извлекаем отправителя
            from_header = self.decode_mime_header(email_message.get("From", "Неизвестный отправитель"))

            # Извлекаем дату
            date_header = self.decode_mime_header(email_message.get("Date", ""))

            # Проверяем совпадение по теме
            matching_subjects = self.check_subject_match(subject)

            if not matching_subjects:
                return {
                    "subject": subject,
                    "from": from_header,
                    "date": date_header,
                    "id": msg_id,
                    "has_match": False
                }

            # Если есть совпадение, извлекаем тело и HTML
            body, content_type, raw_html_body = self.extract_email_body(email_message)
            attachments = self.extract_attachments(email_message)

            return {
                "subject": subject,
                "from": from_header,
                "date": date_header,
                "body": body,
                "content_type": content_type,
                "raw_html_body": raw_html_body,  # Сырой HTML
                "id": msg_id,
                "attachments": attachments,
                "has_match": True,
                "matching_subjects": matching_subjects
            }
        except Exception as e:
            logger.error(f"Ошибка при извлечении содержимого письма {msg_id}: {e}")
            return None

    def mark_as_unread(self, mail: imaplib.IMAP4_SSL, msg_id: bytes) -> None:
        """
        Отметить письмо как непрочитанное с повторными попытками.

        Args:
            mail: Соединение с почтовым сервером
            msg_id: ID письма
        """
        for attempt in range(MAX_RETRIES):
            try:
                mail.store(msg_id, '-FLAGS', '\\Seen')
                logger.debug(f"Письмо {msg_id} отмечено как непрочитанное")
                return
            except Exception as e:
                if attempt < MAX_RETRIES - 1:
                    wait_time = RETRY_DELAY * (2 ** attempt)
                    logger.warning(
                        f"Ошибка при отметке письма {msg_id} как непрочитанного (попытка {attempt + 1}/{MAX_RETRIES}): {e}. Повтор через {wait_time}с")
                    time.sleep(wait_time)
                else:
                    logger.error(
                        f"Не удалось отметить письмо {msg_id} как непрочитанное после {MAX_RETRIES} попыток: {e}")

    def extract_email_body(self, email_message: email.message.Message) -> Tuple[str, str, Optional[str]]:
        """
        Извлечение тела письма из объекта email с сохранением raw HTML.

        Args:
            email_message: Объект сообщения

        Returns:
            Кортеж (тело письма, тип содержимого, сырой HTML или None)
        """
        body = None
        content_type = "text/plain"
        html_body = None
        plain_body = None
        raw_html_body = None  # Для хранения сырого HTML

        try:
            # Если письмо состоит из нескольких частей
            if email_message.is_multipart():
                for part in email_message.walk():
                    # Пропускаем составные части
                    if part.get_content_maintype() == "multipart":
                        continue

                    # Пропускаем вложения
                    if part.get('Content-Disposition') and 'attachment' in part.get('Content-Disposition'):
                        continue

                    # Проверяем тип содержимого
                    current_content_type = part.get_content_type()
                    if current_content_type == "text/plain" and plain_body is None:
                        charset = part.get_content_charset() or "utf-8"
                        try:
                            part_body = part.get_payload(decode=True)
                            if part_body:
                                plain_body = part_body.decode(charset, errors="replace")
                        except Exception as e:
                            logger.error(f"Ошибка декодирования текстовой части письма: {e}")
                    elif current_content_type == "text/html" and html_body is None:
                        charset = part.get_content_charset() or "utf-8"
                        try:
                            part_body = part.get_payload(decode=True)
                            if part_body:
                                html_body = part_body.decode(charset, errors="replace")
                                raw_html_body = html_body  # Сохраняем сырой HTML
                        except Exception as e:
                            logger.error(f"Ошибка декодирования HTML части письма: {e}")
            else:
                # Если письмо состоит из одной части
                charset = email_message.get_content_charset() or "utf-8"
                try:
                    body_bytes = email_message.get_payload(decode=True)
                    if body_bytes:
                        body = body_bytes.decode(charset, errors="replace")
                        content_type = email_message.get_content_type()
                        if content_type == "text/html":
                            raw_html_body = body  # Сохраняем сырой HTML если это HTML
                except Exception as e:
                    logger.error(f"Ошибка декодирования тела письма: {e}")

            # Выбираем тело письма в порядке приоритета
            if plain_body:
                body = plain_body
                content_type = "text/plain"
            elif html_body:
                body = html_body
                content_type = "text/html"
            elif body is None:
                body = "⚠ Не удалось получить содержимое письма"
                content_type = "text/plain"

            return body, content_type, raw_html_body
        except Exception as e:
            logger.error(f"Ошибка при извлечении тела письма: {e}")
            return "⚠ Ошибка обработки содержимого письма", "text/plain", None

    def extract_attachments(self, email_message: email.message.Message) -> List[Dict[str, Any]]:
        """
        Извлечение вложений из письма с оптимизацией памяти.

        Args:
            email_message: Объект сообщения

        Returns:
            Список вложений в виде словарей
        """
        attachments = []

        if not email_message.is_multipart():
            return attachments

        try:
            for part in email_message.walk():
                # Пропускаем составные части и сообщения
                if part.get_content_maintype() in ('multipart', 'message'):
                    continue

                # Проверяем наличие имени файла и Content-Disposition
                filename = part.get_filename()
                content_disposition = part.get('Content-Disposition', '')

                # Более гибкая проверка на вложения
                is_attachment = filename or ('attachment' in content_disposition)

                if not is_attachment:
                    continue

                # Если имя файла не определено, но есть disposition, попробуем извлечь имя из disposition
                if not filename and 'attachment' in content_disposition:
                    # Пытаемся извлечь имя из Content-Disposition
                    filename_match = re.search(r'filename="?([^";]+)"?', content_disposition)
                    if filename_match:
                        filename = filename_match.group(1)
                    else:
                        # Если имя всё равно не найдено, создаем случайное имя
                        filename = f"attachment_{uuid.uuid4().hex}.bin"

                # Если все проверки пройдены, но имя файла всё равно не определено
                if not filename:
                    filename = f"attachment_{uuid.uuid4().hex}.bin"

                # Декодируем имя файла
                filename = self.decode_mime_header(filename)

                # Получаем содержимое вложения
                content = part.get_payload(decode=True)

                # Если содержимое равно None, пропускаем
                if content is None:
                    logger.warning(f"Вложение {filename} не имеет содержимого, пропускаем")
                    continue

                # Получаем тип содержимого
                content_type = part.get_content_type()

                logger.info(f"Найдено вложение: {filename}, тип: {content_type}, размер: {len(content)} байт")

                attachments.append({
                    'filename': filename,
                    'content': content,
                    'content_type': content_type
                })

            logger.info(f"Всего найдено вложений: {len(attachments)}")
            return attachments
        except Exception as e:
            logger.error(f"Ошибка при извлечении вложений: {e}")
            return []

    def clean_subject(self, subject: str) -> str:
        """
        Очистка темы от префиксов с оптимизацией.

        Args:
            subject: Исходная тема

        Returns:
            Очищенная тема
        """
        try:
            original_subject = subject
            subject = subject.strip()

            for prefix in self.subject_prefixes:
                if subject.startswith(prefix):
                    subject = subject[len(prefix):]
                    subject = subject.strip()
                    # Рекурсивно удаляем вложенные префиксы (например, "Re: Fwd: Тема")
                    return self.clean_subject(subject)

            return subject
        except Exception as e:
            logger.error(f"Ошибка при очистке темы письма: {e}")
            return original_subject

    def format_email_body(self, body: str, content_type: str) -> str:
        """
        Форматирует тело письма, удаляя "Explore in Superset" и URL.
        Удаляет <th>, обрабатывает <br> и <a>. НЕ обрезает текст.
        """
        logger.debug(
            f"Форматирование тела. Content-Type: {content_type}. Исходная длина: {len(body)}")
        clean_text = ""
        try:
            # Если содержимое в HTML
            if content_type == "text/html":
                # ... (существующий код парсинга HTML до получения clean_text) ...
                try:
                    unescaped_body = html.unescape(body)
                except Exception as ue:
                    logger.warning(f"Ошибка при html.unescape: {ue}. Используем исходный body.")
                    unescaped_body = body

                try:
                    soup = BeautifulSoup(unescaped_body, 'html.parser')
                except Exception as parse_err:
                    logger.error(f"Ошибка парсинга HTML BeautifulSoup: {parse_err}. Попытка вернуть исходный текст.")
                    return body.strip()

                # Удаляем ненужные теги, ВКЛЮЧАЯ <th>
                for tag in soup(['script', 'style', 'meta', 'link', 'head', 'title', 'th']):
                    tag.decompose()

                # Обработка ссылок <a>
                for link_tag in soup.find_all('a', href=True):
                    href = link_tag.get('href', '').strip()
                    link_text = link_tag.get_text(separator=' ', strip=True)
                    if href:
                        if not link_text or link_text == href:
                            replacement_node = NavigableString(f"{href}\n")
                        else:
                            replacement_node = NavigableString(f"{link_text}\n{href}\n")
                        link_tag.replace_with(replacement_node)
                    elif link_text:
                        link_tag.replace_with(NavigableString(link_text))
                    else:
                        link_tag.decompose()

                # Обработка <br> -> \n
                for br in soup.find_all('br'):
                    br.replace_with(NavigableString('\n'))

                # Получаем текст
                clean_text = soup.get_text(separator='\n').strip()


            # Если содержимое в plain text
            elif content_type == "text/plain":
                clean_text = body.strip()
            else:
                logger.warning(f"Обработка неизвестного content_type: {content_type}. Используем исходный текст.")
                clean_text = body.strip()

            lines = clean_text.splitlines()
            filtered_lines = []
            skip_next_line = False
            explore_removed = False # Флаг для логирования

            for line in lines:
                if skip_next_line:
                    skip_next_line = False
                    continue # Пропускаем строку после "Explore in Superset" (URL)

                # Используем strip() для удаления возможных пробелов по краям
                if line.strip() == "Explore in Superset":
                    skip_next_line = True # Устанавливаем флаг пропуска следующей строки
                    explore_removed = True
                    continue # Пропускаем саму строку "Explore in Superset"

                filtered_lines.append(line) # Добавляем строки, которые не нужно удалять

            if explore_removed:
                logger.debug("Удалена строка 'Explore in Superset' и следующая за ней.")

            clean_text = "\n".join(filtered_lines).strip()

            # Заменяем 3 и более переносов на 2 *после* фильтрации
            clean_text = re.sub(r'\n{3,}', '\n\n', clean_text)
            # Убираем пробелы/табы перед переносом строки
            clean_text = re.sub(r'[ \t]+\n', '\n', clean_text)


            logger.debug(f"Тело отформатировано (без Superset). Итоговая длина: {len(clean_text)}")
            return clean_text
        except Exception as e:
            logger.error(f"Критическая ошибка в format_email_body: {e}", exc_info=True)
            truncated_body = body[:1000] + "..." if len(body) > 1000 else body
            return f"⚠️ Ошибка обработки содержимого письма (см. логи).\n\n{truncated_body}"

    def check_subject_match(self, email_subject: str) -> List[Tuple[str, str]]:
        """
        Проверка соответствия темы письма шаблонам клиентов с оптимизированным алгоритмом.

        Args:
            email_subject: Тема письма для проверки

        Returns:
            Список кортежей (шаблон, chat_id) для совпадающих тем
        """
        matching_subjects = []
        email_subject_lower = email_subject.lower()

        # Сначала проверяем точные совпадения (быстрее)
        if email_subject_lower in self._subject_patterns:
            matching_subjects.extend(self._subject_patterns[email_subject_lower])

        # Затем проверяем вхождения подстрок (медленнее)
        for pattern_lower, patterns_data in self._subject_patterns.items():
            # Пропускаем шаблоны, которые уже проверены на точное совпадение
            if pattern_lower == email_subject_lower:
                continue

            # Проверяем, является ли шаблон подстрокой темы письма
            if pattern_lower in email_subject_lower:
                matching_subjects.extend(patterns_data)

        return matching_subjects

    def _check_rate_limit(self, chat_id: str) -> bool:
        """
        Проверка ограничения частоты сообщений для конкретного чата.

        Args:
            chat_id: ID чата

        Returns:
            True если отправка разрешена, False если достигнут лимит
        """
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
                logger.warning(
                    f"Достигнут лимит сообщений для чата {chat_id}: {self._max_messages_per_minute} сообщений в минуту")
                return False

            # Добавляем новую метку времени
            if chat_id not in self._message_timestamps:
                self._message_timestamps[chat_id] = []
            self._message_timestamps[chat_id].append(current_time)

            return True

    def send_to_telegram(self, chat_id: str, email_data: Dict[str, Any]) -> bool:
        """
        Отправка данных письма в Telegram с учетом настроек пользователя по режиму доставки.
        """
        # Проверяем ограничение частоты
        if not self._check_rate_limit(chat_id):
            # Откладываем отправку, если лимит превышен
            logger.warning(f"Rate limit reached for chat {chat_id}. Rescheduling.")
            # Используем threading.Timer для отложенного вызова
            threading.Timer(60.0, self.send_to_telegram, args=[chat_id, email_data]).start()
            return False  # Возвращаем False, так как отправка не произошла сейчас

        try:
            # ---> НАЧАЛО ИЗМЕНЕНИЙ: ПОЛУЧЕНИЕ РЕЖИМА ДОСТАВКИ <---
            user_delivery_mode = DEFAULT_DELIVERY_MODE  # Значение по умолчанию
            try:
                # Пытаемся получить режим из DatabaseManager
                # Убедитесь, что self.db_manager инициализирован и имеет метод get_user_delivery_mode
                if hasattr(self, 'db_manager') and self.db_manager:
                    retrieved_mode = self.db_manager.get_user_delivery_mode(chat_id)
                    if retrieved_mode:  # Проверяем, что метод вернул непустое значение
                        user_delivery_mode = retrieved_mode
                    else:
                        logger.warning(
                            f"Метод get_user_delivery_mode для {chat_id} вернул пустое значение, используем default: {DEFAULT_DELIVERY_MODE}")
                else:
                    logger.error(
                        "Экземпляр db_manager отсутствует в EmailTelegramForwarder. Невозможно получить режим доставки.")

            except AttributeError:
                logger.error("Метод get_user_delivery_mode отсутствует в db_manager. Используем default.")
            except Exception as db_err:
                logger.error(
                    f"Ошибка получения режима доставки для {chat_id}: {db_err}. Используем default: {DEFAULT_DELIVERY_MODE}")
            # ---> КОНЕЦ ИЗМЕНЕНИЙ: ПОЛУЧЕНИЕ РЕЖИМА ДОСТАВКИ <---

            # Форматируем тело письма
            body = email_data.get("body", "")  # Используем .get для безопасности
            content_type = email_data.get("content_type", "text/plain")
            raw_html_body = email_data.get("raw_html_body")  # Может быть None

            # Используем вашу функцию форматирования
            formatted_body = self.format_email_body(body, content_type)

            # Создаем заголовок
            combined_message = formatted_body
            has_attachments = bool(email_data.get("attachments"))
            TELEGRAM_MAX_LEN = 4096  # Максимальная длина сообщения Telegram
            message_length = len(combined_message)

            # ---> НАЧАЛО ИЗМЕНЕНИЙ: ЛОГИКА ВЫБОРА СПОСОБА ОТПРАВКИ <---
            send_as_html = False
            # HTML можно отправить только если есть raw_html_body
            if raw_html_body:
                if user_delivery_mode == DELIVERY_MODE_HTML:
                    send_as_html = True
                    logger.debug(f"Режим для {chat_id}: HTML. Отправляем как HTML.")
                elif user_delivery_mode == DELIVERY_MODE_SMART and message_length >= TELEGRAM_MAX_LEN:
                    send_as_html = True
                    logger.debug(
                        f"Режим для {chat_id}: Smart, сообщение длинное ({message_length}). Отправляем как HTML.")
                # Если режим 'text' или 'smart' и сообщение короткое, send_as_html остается False
                elif user_delivery_mode == DELIVERY_MODE_TEXT:
                    logger.debug(f"Режим для {chat_id}: Text. Отправляем как текст.")
                else:  # Smart и короткое
                    logger.debug(
                        f"Режим для {chat_id}: Smart, сообщение короткое ({message_length}). Отправляем как текст.")
            else:
                # Если нет raw_html_body, всегда отправляем как текст, независимо от настроек
                send_as_html = False
                logger.debug(
                    f"Нет raw_html_body для письма '{email_data.get('subject', '')}'. Отправляем как текст для {chat_id}.")
            # ---> КОНЕЦ ИЗМЕНЕНИЙ: ЛОГИКА ВЫБОРА СПОСОБА ОТПРАВКИ <---

            # --- Отправка как HTML файл ---
            if send_as_html:
                logger.info(
                    f"Отправка письма '{email_data.get('subject', '')}' как HTML файл для {chat_id} (режим: {user_delivery_mode}, длина: {message_length})")
                temp_dir = None
                temp_file_path = None
                try:
                    # Создаем временный файл (ваш существующий код)
                    temp_dir = tempfile.mkdtemp()
                    # Очищаем имя файла темы для использования в имени файла
                    base_filename = re.sub(r'[^\w\-_\. ]', '_', email_data.get('subject', 'email'))[:50]
                    # Добавляем уникальный идентификатор, чтобы избежать коллизий
                    html_filename = f"{base_filename}_{uuid.uuid4().hex[:6]}.html"
                    temp_file_path = os.path.join(temp_dir, html_filename)

                    # Обработка и запись HTML в файл (ваш существующий код)
                    # Убедитесь, что raw_html_body обрабатывается перед записью
                    processed_html = html.unescape(raw_html_body)  # Раскодируем сущности
                    processed_html = re.sub(r'<\?p>', '<p>', processed_html)  # Исправляем <?p> если есть
                    processed_html = re.sub(r'<\?>', '', processed_html)  # Исправляем <?> если есть
                    try:
                        # Используем html.parser для большей совместимости
                        soup = BeautifulSoup(processed_html, 'html.parser')
                        # Удаляем ненужные теги, если нужно (опционально)
                        # for tag in soup(['script', 'style']): tag.decompose()
                        clean_html = str(soup)
                    except Exception as parse_err:
                        logger.warning(f"Ошибка парсинга HTML для файла: {parse_err}. Используем необработанный HTML.")
                        clean_html = processed_html

                    with open(temp_file_path, 'w', encoding='utf-8') as f:
                        # Запись шапки и стилей (ваш существующий код)
                        f.write('<!DOCTYPE html>\n<html lang="ru">\n<head>\n')
                        f.write('    <meta charset="UTF-8">\n')
                        f.write('    <meta name="viewport" content="width=device-width, initial-scale=1.0">\n')
                        f.write(f'    <title>{html.escape(email_data.get("subject", "Письмо"))}</title>\n')
                        # Добавьте ваши стили сюда
                        f.write(
                            '    <style> body { font-family: sans-serif; line-height: 1.5; padding: 15px; } table { border-collapse: collapse; width: 100%; margin-bottom: 1em; } th, td { border: 1px solid #ddd; padding: 8px; text-align: left; } th { background-color: #f2f2f2; } img { max-width: 100%; height: auto; } </style>\n')
                        f.write('</head>\n<body>\n')
                        f.write(clean_html)  # Записываем очищенный HTML
                        f.write('\n</body>\n</html>')

                    # Отправляем HTML файл
                    with open(temp_file_path, 'rb') as html_file:
                        # Формируем caption для HTML файла
                        caption_header = (
                            f"📧 Новое письмо (HTML версия)\n\n"
                            f"От: {email_data.get('from', 'Неизвестно')}\n"
                            f"Тема: {email_data.get('subject', 'Без темы')}\n"
                            f"Дата: {email_data.get('date', 'Неизвестно')}\n\n"
                        )
                        # Добавляем информацию о причине отправки файла
                        if user_delivery_mode == DELIVERY_MODE_HTML:
                            caption_reason = "Выбран режим 'Только HTML файл'."
                        else:  # Smart mode
                            caption_reason = "Сообщение слишком длинное для Telegram."

                        full_caption = caption_header + caption_reason
                        # Обрезаем caption, если он превышает лимит Telegram (1024)
                        if len(full_caption) > 1024:
                            full_caption = full_caption[:1020] + "... " + caption_reason  # Сохраняем причину

                        # Отправляем документ с retry логикой (можно обернуть в декоратор @with_retry)
                        # self.bot.send_document(chat_id, html_file, caption=full_caption) # Замените на ваш метод с retry, если есть
                        # Пример с простой логикой retry здесь:
                        for attempt in range(MAX_RETRIES):
                            try:
                                self.bot.send_document(chat_id, html_file, caption=full_caption)
                                logger.info(f"HTML файл '{html_filename}' успешно отправлен для {chat_id}")
                                break  # Успешно отправлено
                            except Exception as send_err:
                                if attempt < MAX_RETRIES - 1:
                                    wait_time = RETRY_DELAY * (2 ** attempt)
                                    logger.warning(
                                        f"Ошибка отправки HTML файла для {chat_id} (попытка {attempt + 1}): {send_err}. Повтор через {wait_time}с.")
                                    time.sleep(wait_time)
                                else:
                                    logger.error(
                                        f"Не удалось отправить HTML файл для {chat_id} после {MAX_RETRIES} попыток: {send_err}")
                                    raise send_err  # Передаем ошибку дальше

                    # Отправляем вложения отдельно, если они есть
                    if has_attachments:
                        logger.info(
                            f"Отправка {len(email_data['attachments'])} вложений для {chat_id} после HTML файла.")
                        for attachment in email_data["attachments"]:
                            self.send_attachment_to_telegram(chat_id, attachment)
                            time.sleep(0.5)  # Небольшая пауза между файлами

                    return True  # Успешная отправка HTML + вложения

                except Exception as e:
                    logger.error(f"Ошибка при создании/отправке HTML файла для {chat_id}: {e}", exc_info=True)
                    # Пытаемся отправить уведомление об ошибке
                    try:
                        error_text = f"⚠️ Не удалось отправить письмо '{email_data.get('subject', '')}' как HTML файл из-за внутренней ошибки."
                        self.bot.send_message(chat_id, error_text)
                    except Exception as notify_err:
                        logger.error(f"Не удалось даже отправить уведомление об ошибке для {chat_id}: {notify_err}")
                    return False  # Ошибка отправки HTML
                finally:
                    # Очистка временных файлов (ваш существующий код)
                    if temp_file_path and os.path.exists(temp_file_path):
                        try:
                            os.remove(temp_file_path)
                        except Exception as e:
                            logger.warning(f"Не удалось удалить файл {temp_file_path}: {e}")
                    if temp_dir and os.path.exists(temp_dir):
                        try:
                            os.rmdir(temp_dir)
                        except Exception as e:
                            logger.warning(f"Не удалось удалить директорию {temp_dir}: {e}")

            # --- Отправка как текст ---
            else:
                logger.info(
                    f"Отправка письма '{email_data.get('subject', '')}' как текст для {chat_id} (режим: {user_delivery_mode}, длина: {message_length})")

                # Разбиваем текст на части (ваш существующий код)
                message_parts = self.split_text(combined_message, max_length=TELEGRAM_MAX_LEN)

                try:
                    # Логика отправки текста и вложений (ваш существующий код)
                    if not has_attachments:
                        # Отправляем все части текста
                        for i, part in enumerate(message_parts):
                            # Добавляем пометку для частей > 1
                            prefix = f"[{i + 1}/{len(message_parts)}] " if len(message_parts) > 1 else ""
                            # self.bot.send_message(chat_id, prefix + part) # Замените на ваш метод с retry
                            # Пример с простой логикой retry здесь:
                            for attempt in range(MAX_RETRIES):
                                try:
                                    self.bot.send_message(chat_id, prefix + part)
                                    break  # Успех
                                except Exception as send_err:
                                    if attempt < MAX_RETRIES - 1:
                                        wait_time = RETRY_DELAY * (2 ** attempt)
                                        logger.warning(
                                            f"Ошибка отправки текстовой части {i + 1} для {chat_id} (попытка {attempt + 1}): {send_err}. Повтор через {wait_time}с.")
                                        time.sleep(wait_time)
                                    else:
                                        logger.error(
                                            f"Не удалось отправить текстовую часть {i + 1} для {chat_id}: {send_err}")
                                        raise send_err  # Передаем ошибку
                            time.sleep(0.5)  # Пауза между частями
                    else:
                        # Есть вложения
                        # Если текст помещается в caption (1024 символа) и есть только одно вложение
                        if message_length <= 1024 and len(email_data["attachments"]) == 1:
                            first_attachment = email_data["attachments"][0]
                            self.send_attachment_with_message(chat_id, first_attachment, combined_message)
                        else:
                            # Отправляем текст частями
                            for i, part in enumerate(message_parts):
                                prefix = f"[{i + 1}/{len(message_parts)}] " if len(message_parts) > 1 else ""
                                # self.bot.send_message(chat_id, prefix + part) # Замените на ваш метод с retry
                                # Пример с простой логикой retry здесь:
                                for attempt in range(MAX_RETRIES):
                                    try:
                                        self.bot.send_message(chat_id, prefix + part)
                                        break  # Успех
                                    except Exception as send_err:
                                        if attempt < MAX_RETRIES - 1:
                                            wait_time = RETRY_DELAY * (2 ** attempt)
                                            logger.warning(
                                                f"Ошибка отправки текстовой части {i + 1} (с вложениями) для {chat_id} (попытка {attempt + 1}): {send_err}. Повтор через {wait_time}с.")
                                            time.sleep(wait_time)
                                        else:
                                            logger.error(
                                                f"Не удалось отправить текстовую часть {i + 1} (с вложениями) для {chat_id}: {send_err}")
                                            raise send_err  # Передаем ошибку
                                time.sleep(0.5)  # Пауза

                            # Затем отправляем вложения
                            logger.info(
                                f"Отправка {len(email_data['attachments'])} вложений для {chat_id} после текста.")
                            for attachment in email_data["attachments"]:
                                self.send_attachment_to_telegram(chat_id, attachment)
                                time.sleep(0.5)  # Пауза

                    logger.info(f"Сообщение успешно отправлено текстом в чат {chat_id}")
                    return True  # Успешная отправка текста + вложения

                except Exception as e:
                    logger.error(f"Ошибка при отправке текстового сообщения или вложений для {chat_id}: {e}",
                                 exc_info=True)
                    # Пытаемся отправить уведомление об ошибке
                    try:
                        error_text = f"⚠️ Не удалось отправить письмо '{email_data.get('subject', '')}' (текстовая версия) из-за внутренней ошибки."
                        self.bot.send_message(chat_id, error_text)
                    except Exception as notify_err:
                        logger.error(f"Не удалось даже отправить уведомление об ошибке для {chat_id}: {notify_err}")
                    return False  # Ошибка отправки текста

        except Exception as e:
            # Ловим общие ошибки на верхнем уровне функции
            logger.error(f"Критическая ошибка в send_to_telegram для {chat_id}: {e}", exc_info=True)
            # Пытаемся отправить уведомление об ошибке
            try:
                error_text = f"⚠️ Произошла критическая ошибка при обработке письма '{email_data.get('subject', '')}'. Администратор уведомлен."
                self.bot.send_message(chat_id, error_text)
            except Exception as notify_err:
                logger.error(f"Не удалось даже отправить уведомление о критической ошибке для {chat_id}: {notify_err}")
            return False  # Общая ошибка

    def send_attachment_with_message(self, chat_id: str, attachment: Dict[str, Any], message: str) -> None:
        """
        Отправка вложения вместе с текстом сообщения в одном сообщении Telegram.
        """
        temp_dir = None
        temp_file_path = None

        try:
            filename = attachment['filename']
            content = attachment['content']
            content_type = attachment['content_type']

            # Очищаем имя файла от недопустимых символов
            safe_filename = re.sub(r'[<>:"/\\|?*]', '_', filename)

            # Создаем временную директорию
            temp_dir = tempfile.mkdtemp()

            # Создаем файл с оригинальным именем во временной директории
            temp_file_path = os.path.join(temp_dir, safe_filename)

            with open(temp_file_path, 'wb') as temp_file:
                temp_file.write(content)

            logger.debug(f"Создан временный файл: {temp_file_path} для вложения {filename}")

            # Проверяем размер файла
            file_size = os.path.getsize(temp_file_path)
            if file_size > 50 * 1024 * 1024:
                logger.warning(f"Вложение {filename} слишком большое ({file_size / (1024 * 1024):.2f} МБ)")
                self.bot.send_message(chat_id, message)  # Убрали parse_mode
                self.bot.send_message(chat_id, f"⚠️ Вложение {safe_filename} слишком большое для отправки в Telegram")
                return

            # Ограничиваем длину caption до 1024 символов
            if len(message) > 1024:
                truncated_message = message[:1020] + "..."
            else:
                truncated_message = message

            for attempt in range(MAX_RETRIES):
                try:
                    if content_type.startswith('image/'):
                        with open(temp_file_path, 'rb') as photo:
                            self.bot.send_photo(chat_id, photo, caption=truncated_message)  # Убрали parse_mode
                    else:
                        with open(temp_file_path, 'rb') as document:
                            self.bot.send_document(chat_id, document, caption=truncated_message)  # Убрали parse_mode
                    break
                except Exception as e:
                    if attempt < MAX_RETRIES - 1:
                        wait_time = RETRY_DELAY * (2 ** attempt)
                        logger.warning(
                            f"Ошибка при отправке вложения с сообщением (попытка {attempt + 1}/{MAX_RETRIES}): {e}. Повтор через {wait_time}с")
                        time.sleep(wait_time)
                    else:
                        logger.error(f"Не удалось отправить вложение с сообщением после {MAX_RETRIES} попыток: {e}")
                        try:
                            self.bot.send_message(chat_id, message)  # Убрали parse_mode
                            # Резервная отправка вложения
                            if content_type.startswith('image/'):
                                with open(temp_file_path, 'rb') as photo:
                                    self.bot.send_photo(chat_id, photo, caption=safe_filename)
                            else:
                                with open(temp_file_path, 'rb') as document:
                                    self.bot.send_document(chat_id, document, caption=safe_filename)
                        except Exception as e2:
                            logger.error(f"Критическая ошибка отправки: {e2}")

        except Exception as e:
            logger.error(f"Ошибка при отправке вложения с сообщением: {e}")
            try:
                self.bot.send_message(chat_id, message)  # Убрали parse_mode
            except Exception as e3:
                logger.error(f"Не удалось отправить даже текст сообщения: {e3}")
        finally:
            # Очистка временных файлов
            if temp_file_path and os.path.exists(temp_file_path):
                try:
                    os.remove(temp_file_path)
                except Exception as e:
                    logger.warning(f"Не удалось удалить временный файл {temp_file_path}: {e}")
            if temp_dir and os.path.exists(temp_dir):
                try:
                    os.rmdir(temp_dir)
                except Exception as e:
                    logger.warning(f"Не удалось удалить временную директорию {temp_dir}: {e}")

    def split_text(self, text: str, max_length: int = 4096) -> List[str]:
        """
        Разбивает текст на части по заданной максимальной длине,
        гарантированно оставляя запас для префиксов типа "[N/M] ".
        """
        parts = []
        # Запас символов для префикса (например, "[99/99] " ~ 8 символов) + небольшой доп. запас
        safety_margin = 20
        # Максимальная длина контента для каждой части (кроме последней)
        limit = max_length - safety_margin

        if limit <= 0:
            logger.error(
                f"Невозможно разбить текст: max_length ({max_length}) слишком мал для safety_margin ({safety_margin}).")
            # Возвращаем текст как есть, пусть API ругается
            return [text]

        current_pos = 0
        text_len = len(text)

        while current_pos < text_len:
            # Определяем конец текущего среза (не больше длины текста)
            end_pos = min(current_pos + limit, text_len)

            # Если оставшийся текст помещается в одну часть (с учетом полной max_length)
            if text_len - current_pos <= max_length:
                parts.append(text[current_pos:])
                break  # Завершаем цикл

            # Ищем последний перенос строки в пределах среза [current_pos, end_pos)
            split_at = text.rfind('\n', current_pos, end_pos)

            # Если перенос не найден или он слишком близко к началу,
            # ищем первый пробел с конца для более естественного разрыва
            if split_at == -1 or split_at == current_pos:
                # Ищем последний пробел перед end_pos
                split_at = text.rfind(' ', current_pos, end_pos)
                # Если пробела нет, придется резать "по живому" на end_pos
                if split_at == -1 or split_at == current_pos:
                    split_at = end_pos

            # Если перенос строки найден, он является предпочтительной точкой разреза
            # Если split_at указывает на \n, то срез [current_pos:split_at] не включает \n

            # Добавляем часть до точки разреза
            parts.append(text[current_pos:split_at])
            # Обновляем текущую позицию для следующей части
            # Перемещаемся за точку разреза и пропускаем пробельные символы (\n, ' ')
            current_pos = split_at + 1
            # Пропускаем пробелы/переносы в начале следующей части (если есть)
            while current_pos < text_len and text[current_pos].isspace():
                current_pos += 1

        # Фильтруем пустые строки, которые могли образоваться
        result_parts = [part for part in parts if part and not part.isspace()]

        # Если после всех манипуляций ничего не осталось (очень странно),
        # возвращаем исходный текст, обрезанный до лимита
        if not result_parts and text:
            logger.warning("split_text не смог разбить текст, возвращаем обрезанный исходник.")
            return [text[:limit]]

        return result_parts

    def send_attachment_to_telegram(self, chat_id: str, attachment: Dict[str, Any]) -> None:
        """
        Отправка вложения в Telegram с сохранением имени файла и учетом лимита caption.
        """
        temp_dir = None
        temp_file_path = None

        try:
            filename = attachment['filename']
            content = attachment['content']
            content_type = attachment['content_type']

            # Очищаем имя файла от недопустимых символов
            safe_filename = re.sub(r'[<>:"/\\|?*]', '_', filename)
            caption = safe_filename[:1024]  # Ограничиваем caption до 1024 символов

            # Создаем временную директорию
            temp_dir = tempfile.mkdtemp()
            temp_file_path = os.path.join(temp_dir, safe_filename)

            with open(temp_file_path, 'wb') as temp_file:
                temp_file.write(content)

            logger.debug(f"Создан временный файл: {temp_file_path} для вложения {filename}")

            # Проверяем размер файла
            file_size = os.path.getsize(temp_file_path)
            if file_size > 50 * 1024 * 1024:
                logger.warning(f"Вложение {filename} слишком большое ({file_size / (1024 * 1024):.2f} МБ), пропускаем")
                self.bot.send_message(chat_id, f"⚠️ Вложение {safe_filename} слишком большое для отправки")
                return

            for attempt in range(MAX_RETRIES):
                try:
                    if content_type.startswith('image/'):
                        with open(temp_file_path, 'rb') as photo:
                            self.bot.send_photo(chat_id, photo, caption=caption)
                        logger.info(f"Изображение {filename} отправлено в чат {chat_id}")
                    else:
                        with open(temp_file_path, 'rb') as document:
                            self.bot.send_document(chat_id, document, caption=caption)
                        logger.info(f"Документ {filename} отправлен в чат {chat_id}")
                    break
                except Exception as e:
                    if attempt < MAX_RETRIES - 1:
                        wait_time = RETRY_DELAY * (2 ** attempt)
                        logger.warning(
                            f"Ошибка при отправке вложения {filename} (попытка {attempt + 1}/{MAX_RETRIES}): {e}. Повтор через {wait_time}с")
                        time.sleep(wait_time)
                    else:
                        logger.error(f"Не удалось отправить вложение {filename} после {MAX_RETRIES} попыток: {e}")
                        self.bot.send_message(chat_id, f"⚠️ Не удалось отправить вложение: {safe_filename}")

        except Exception as e:
            logger.error(f"Ошибка при отправке вложения {attachment.get('filename', 'неизвестно')}: {e}")
        finally:
            # Очистка временных файлов
            if temp_file_path and os.path.exists(temp_file_path):
                try:
                    os.remove(temp_file_path)
                except Exception as e:
                    logger.warning(f"Не удалось удалить временный файл {temp_file_path}: {e}")
            if temp_dir and os.path.exists(temp_dir):
                try:
                    os.rmdir(temp_dir)
                except Exception as e:
                    logger.warning(f"Не удалось удалить временную директорию {temp_dir}: {e}")

    def mark_as_read(self, mail: imaplib.IMAP4_SSL, msg_id: bytes) -> None:
        """
        Отметить письмо как прочитанное с повторными попытками.

        Args:
            mail: Соединение с почтовым сервером
            msg_id: ID письма
        """
        for attempt in range(MAX_RETRIES):
            try:
                mail.store(msg_id, '+FLAGS', '\\Seen')
                logger.debug(f"Письмо {msg_id} отмечено как прочитанное")
                return
            except Exception as e:
                if attempt < MAX_RETRIES - 1:
                    wait_time = RETRY_DELAY * (2 ** attempt)
                    logger.warning(
                        f"Ошибка при отметке письма {msg_id} как прочитанного (попытка {attempt + 1}/{MAX_RETRIES}): {e}. Повтор через {wait_time}с")
                    time.sleep(wait_time)
                else:
                    logger.error(
                        f"Не удалось отметить письмо {msg_id} как прочитанное после {MAX_RETRIES} попыток: {e}")

    def get_email_subject(self, mail: imaplib.IMAP4_SSL, msg_id: bytes) -> Optional[str]:
        """
        Получить только заголовок письма без загрузки всего содержимого.

        Args:
            mail: Соединение с почтовым сервером
            msg_id: ID письма

        Returns:
            Тема письма или None в случае ошибки
        """
        try:
            # Получаем только заголовок письма
            status, msg_data = mail.fetch(msg_id, "(BODY.PEEK[HEADER.FIELDS (SUBJECT)])")
            if status != "OK" or not msg_data or not msg_data[0]:
                logger.warning(f"Не удалось получить заголовок письма {msg_id}")
                return None

            # Извлекаем заголовок
            header_data = msg_data[0][1]
            header = email.message_from_bytes(header_data)
            subject = self.decode_mime_header(header.get("Subject", "Без темы"))
            subject = self.clean_subject(subject)

            return subject
        except Exception as e:
            logger.error(f"Ошибка при извлечении заголовка письма {msg_id}: {e}")
            return None

    def _process_email_worker(self) -> None:
        """
        Рабочий поток для обработки писем из очереди.
        """
        while not self.stop_event.is_set():
            try:
                # Получаем задачу из очереди с таймаутом
                try:
                    email_data, matching_subjects = self.email_queue.get(timeout=1)
                except queue.Empty:
                    continue

                # Обрабатываем письмо
                for subject_pattern, chat_id in matching_subjects:
                    logger.info(f"Обработка письма с темой '{email_data['subject']}' для чата {chat_id}")
                    self.send_to_telegram(chat_id, email_data)

                # Отмечаем задачу как выполненную
                self.email_queue.task_done()
            except Exception as e:
                logger.error(f"Ошибка в рабочем потоке обработки писем: {e}")

    def _start_workers(self) -> None:
        """
        Запуск рабочих потоков для обработки писем.
        """
        self.stop_event.clear()
        for i in range(MAX_WORKERS):
            worker = threading.Thread(
                target=self._process_email_worker,
                name=f"EmailWorker-{i}",
                daemon=True
            )
            worker.start()
            self.workers.append(worker)
        logger.info(f"Запущено {MAX_WORKERS} рабочих потоков для обработки писем")

    def _stop_workers(self) -> None:
        """
        Остановка рабочих потоков.
        """
        self.stop_event.set()
        for worker in self.workers:
            if worker.is_alive():
                worker.join(timeout=2)
        self.workers = []
        logger.info("Рабочие потоки остановлены")

    def process_emails(self) -> None:
        """Оптимизированная функция обработки писем с многопоточной обработкой."""
        logger.info("Начинаем проверку почты...")

        try:
            # Повторная загрузка данных о клиентах перед каждой проверкой
            self.reload_client_data()

            # Если нет активных клиентов, пропускаем проверку
            if not any(any(client["enabled"] for client in data) for data in self.client_data.values()):
                logger.info("Нет активных клиентов, пропускаем проверку почты")
                return

            # Подключение к почтовому серверу
            mail = self._get_mail_connection()

            # Получение непрочитанных писем
            msg_ids = self.get_all_unseen_emails(mail)

            # Если нет непрочитанных писем, пропускаем обработку
            if not msg_ids:
                logger.info("Нет новых писем, пропускаем обработку")
                return

            # Запускаем рабочие потоки, если они еще не запущены
            if not self.workers:
                self._start_workers()

            # Инициализация счетчиков
            emails_processed = 0  # Подсчет обработанных писем
            notifications_sent = 0  # Подсчет отправленных уведомлений
            emails_to_mark_read = []  # Список ID писем для пометки прочитанными

            # Проверка каждого письма
            for msg_id in msg_ids:
                # Сначала получаем только тему письма (оптимизация)
                subject = self.get_email_subject(mail, msg_id)

                if not subject:
                    logger.warning(f"Не удалось получить тему письма {msg_id}, пропускаем")
                    continue

                logger.info(f"Обработка письма с темой: {subject}")

                # Проверка соответствия темы и шаблонов клиентов
                matching_subjects = self.check_subject_match(subject)

                if matching_subjects:
                    # Только если есть совпадение, загружаем полное содержимое письма
                    email_data = self.extract_email_content(mail, msg_id)

                    if not email_data:
                        logger.warning(f"Не удалось извлечь содержимое письма {msg_id}")
                        continue

                    # Увеличиваем счетчик обработанных писем
                    emails_processed += 1

                    # Добавляем письмо в очередь для обработки в отдельном потоке
                    self.email_queue.put((email_data, matching_subjects))
                    notifications_sent += len(matching_subjects)

                    # Добавляем ID письма в список для последующей пометки прочитанным
                    emails_to_mark_read.append(msg_id)
                else:
                    logger.info(
                        f"Письмо с темой '{subject}' не соответствует ни одному шаблону, оставляем непрочитанным")
                    # Гарантия, что письмо останется непрочитанным
                    self.mark_as_unread(mail, msg_id)

            # Отмечаем письма как прочитанные пакетом
            for msg_id in emails_to_mark_read:
                self.mark_as_read(mail, msg_id)

            # Ожидаем завершения обработки всех писем перед возвратом
            # (не блокируем поток, так как обработка происходит асинхронно)

            # Логируем итоговую статистику
            logger.info(
                f"Проверка почты завершена. Обработано писем: {emails_processed}, отправлено уведомлений: {notifications_sent}")

        except Exception as e:
            logger.error(f"Критическая ошибка при проверке почты: {e}")
            logger.exception(e)  # Добавляем полный стек-трейс

            # Пробуем переподключиться к почтовому серверу при следующей проверке
            try:
                with self._mail_lock:
                    if self._mail_connection:
                        try:
                            self._mail_connection.close()
                            self._mail_connection.logout()
                        except Exception:
                            pass
                        self._mail_connection = None
            except Exception:
                pass

    def test_connections(self) -> Dict[str, bool]:
        """
        Тестирование подключений к серверам с повторными попытками.

        Returns:
            Словарь с результатами проверки {"mail": bool, "telegram": bool}
        """
        # Проверка почтового сервера
        mail_status = False
        for attempt in range(MAX_RETRIES):
            try:
                test_mail = imaplib.IMAP4_SSL(self.email_server, timeout=CONNECTION_TIMEOUT)
                test_mail.login(self.email_account, self.password)
                test_mail.select("inbox")
                test_mail.close()
                test_mail.logout()
                logger.info("Подключение к почтовому серверу прошло успешно")
                mail_status = True
                break
            except Exception as e:
                if attempt < MAX_RETRIES - 1:
                    wait_time = RETRY_DELAY * (2 ** attempt)
                    logger.warning(
                        f"Ошибка при тестировании почтового соединения (попытка {attempt + 1}/{MAX_RETRIES}): {e}. Повтор через {wait_time}с")
                    time.sleep(wait_time)
                else:
                    logger.error(f"Не удалось подключиться к почтовому серверу после {MAX_RETRIES} попыток: {e}")

        # Проверка подключения к Telegram API
        telegram_status = False
        for attempt in range(MAX_RETRIES):
            try:
                test_message = self.bot.get_me()
                logger.info(f"Подключение к Telegram API прошло успешно. Имя бота: {test_message.first_name}")
                telegram_status = True
                break
            except Exception as e:
                if attempt < MAX_RETRIES - 1:
                    wait_time = RETRY_DELAY * (2 ** attempt)
                    logger.warning(
                        f"Ошибка при тестировании Telegram API (попытка {attempt + 1}/{MAX_RETRIES}): {e}. Повтор через {wait_time}с")
                    time.sleep(wait_time)
                else:
                    logger.error(f"Не удалось подключиться к Telegram API после {MAX_RETRIES} попыток: {e}")

        return {
            "mail": mail_status,
            "telegram": telegram_status
        }

    def start_scheduler(self, interval: int = 5) -> None:
        """
        Запуск планировщика для регулярной проверки почты с функцией самовосстановления.

        Args:
            interval: Интервал проверки в минутах
        """
        # Настройка расписания проверки почты (каждые X минут)
        self.check_interval = interval
        schedule.every(interval).minutes.do(self.process_emails)

        logger.info(f"Планировщик запущен. Интервал проверки: {interval} минут")

        # Запускаем рабочие потоки для обработки писем
        self._start_workers()

        # Запускаем проверку сразу, не дожидаясь первого запланированного запуска
        self.process_emails()

        # Переменная для отслеживания последнего успешного запуска
        last_successful_check = time.time()
        check_timeout = interval * 60 * 2  # Удвоенный интервал в секундах

        # Основной цикл
        while True:
            try:
                schedule.run_pending()

                # Проверяем, не пропущены ли запланированные проверки
                current_time = time.time()
                if current_time - last_successful_check > check_timeout:
                    logger.warning(f"Обнаружен пропуск запланированной проверки. Выполняем проверку вручную.")
                    self.process_emails()
                    last_successful_check = current_time

                # Обновляем время последней успешной проверки
                for job in schedule.jobs:
                    if job.last_run is not None:
                        last_successful_check = time.time()

                time.sleep(1)
            except KeyboardInterrupt:
                logger.info("Работа программы прервана пользователем")
                self._stop_workers()
                break
            except Exception as e:
                logger.error(f"Неожиданная ошибка в основном цикле: {e}")
                logger.exception(e)

                # Пытаемся восстановить работу
                try:
                    # Перезапускаем рабочие потоки
                    self._stop_workers()
                    self._start_workers()

                    # Если планировщик пуст, пересоздаем задачи
                    if not schedule.jobs:
                        schedule.every(interval).minutes.do(self.process_emails)
                        logger.info(f"Планировщик перезапущен. Интервал проверки: {interval} минут")
                except Exception as e2:
                    logger.error(f"Не удалось восстановить работу: {e2}")

                # Делаем паузу перед следующей итерацией
                time.sleep(60)

    def shutdown(self) -> None:
        """
        Корректное завершение работы форвардера.
        """
        logger.info("Завершение работы форвардера...")

        # Остановка рабочих потоков
        self._stop_workers()

        # Очистка очереди
        while not self.email_queue.empty():
            try:
                self.email_queue.get_nowait()
                self.email_queue.task_done()
            except queue.Empty:
                break

        # Закрытие соединения с почтовым сервером
        try:
            with self._mail_lock:
                if self._mail_connection:
                    try:
                        self._mail_connection.close()
                        self._mail_connection.logout()
                    except Exception:
                        pass
                    self._mail_connection = None
        except Exception as e:
            logger.error(f"Ошибка при закрытии соединения с почтовым сервером: {e}")

        logger.info("Форвардер успешно завершил работу")


def main():
    """Основная функция для запуска форвардера с обработкой исключений."""
    forwarder = None
    try:
        # Создание экземпляра форвардера
        forwarder = EmailTelegramForwarder()

        # Проверка соединений перед запуском
        connections = forwarder.test_connections()

        if not connections["mail"]:
            logger.error("Не удалось подключиться к почтовому серверу. Проверьте настройки.")
            return

        if not connections["telegram"]:
            logger.error("Не удалось подключиться к Telegram API. Проверьте токен.")
            return

        # Запуск планировщика проверки писем с интервалом из настроек
        forwarder.start_scheduler(interval=settings.CHECK_INTERVAL)
    except KeyboardInterrupt:
        logger.info("Программа остановлена пользователем")
    except Exception as e:
        logger.critical(f"Критическая ошибка при запуске программы: {e}")
        logger.exception(e)
    finally:
        # Корректное завершение работы
        if forwarder:
            try:
                forwarder.shutdown()
            except Exception as e:
                logger.error(f"Ошибка при завершении работы программы: {e}")


if __name__ == "__main__":
    main()