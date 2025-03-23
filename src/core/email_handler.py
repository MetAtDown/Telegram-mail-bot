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
from bs4 import BeautifulSoup
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
        Извлечение содержимого письма по его ID с оптимизацией для уменьшения потребления памяти.

        Args:
            mail: Соединение с почтовым сервером
            msg_id: ID письма

        Returns:
            Словарь с данными письма или None в случае ошибки
        """
        try:
            # Получаем письмо целиком, используя PEEK чтобы не менять флаг прочтения
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

            # Извлекаем тело письма и вложения только если необходимо
            # (для повышения производительности)
            matching_subjects = self.check_subject_match(subject)

            if not matching_subjects:
                # Если нет совпадений по теме, возвращаем только базовую информацию
                return {
                    "subject": subject,
                    "from": from_header,
                    "date": date_header,
                    "id": msg_id,
                    "has_match": False
                }

            # Если есть совпадение, извлекаем тело и вложения
            body, content_type = self.extract_email_body(email_message)
            attachments = self.extract_attachments(email_message)

            return {
                "subject": subject,
                "from": from_header,
                "date": date_header,
                "body": body,
                "content_type": content_type,
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

    def extract_email_body(self, email_message: email.message.Message) -> Tuple[str, str]:
        """
        Извлечение тела письма из объекта email с оптимизацией памяти.

        Args:
            email_message: Объект сообщения

        Returns:
            Кортеж (текст письма, тип содержимого)
        """
        body = None
        content_type = "text/plain"
        html_body = None
        plain_body = None

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
                except Exception as e:
                    logger.error(f"Ошибка декодирования тела письма: {e}")

            # Выбираем тело письма в порядке приоритета: plain text, html, или что есть
            if plain_body:
                body = plain_body
                content_type = "text/plain"
            elif html_body:
                body = html_body
                content_type = "text/html"
            elif body is None:
                body = "⚠ Не удалось получить содержимое письма"
                content_type = "text/plain"

            return body, content_type
        except Exception as e:
            logger.error(f"Ошибка при извлечении тела письма: {e}")
            return "⚠ Ошибка обработки содержимого письма", "text/plain"

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
        Форматирование тела письма для отправки в Telegram с оптимизацией.

        Args:
            body: Тело письма
            content_type: MIME-тип содержимого

        Returns:
            Отформатированное тело письма
        """
        try:
            # Если содержимое в HTML, извлекаем текст и ссылки
            if content_type == "text/html":
                # Используем lxml парсер для быстрой работы
                soup = BeautifulSoup(body, 'lxml')

                # Удаляем ненужные элементы, которые могут мешать чтению
                for tag in soup(['style', 'script', 'meta', 'link']):
                    tag.decompose()

                # Сохраняем все ссылки
                links = {}
                for a_tag in soup.find_all('a', href=True):
                    href = a_tag['href']
                    text = a_tag.get_text().strip()
                    # Пропускаем пустые ссылки и ссылки без текста
                    if href and text:
                        links[text] = href

                # Заменяем некоторые теги для лучшего форматирования в Markdown
                for tag_name, replacement in [
                    ('h1', '# '), ('h2', '## '), ('h3', '### '),
                    ('br', '\n'), ('p', '\n\n'), ('div', '\n')
                ]:
                    for tag in soup.find_all(tag_name):
                        if tag.string:  # Только если тег содержит текст
                            tag.insert_before(replacement)

                # Получаем текст из HTML
                body = soup.get_text('\n', strip=True)

                # Удаляем множественные переносы строк
                body = re.sub(r'\n{3,}', '\n\n', body)

                # Заменяем текст ссылками в формате Markdown для Telegram
                # Сортируем ссылки по длине текста (от длинных к коротким)
                # чтобы избежать проблем с подстроками
                for text, href in sorted(links.items(), key=lambda x: len(x[0]), reverse=True):
                    # Проверяем, что текст ссылки присутствует в теле письма
                    if text in body:
                        body = body.replace(text, f"[{text}]({href})", 1)

            # Обрезаем текст до допустимой длины
            max_length = 3900  # Оставляем запас для остальной части сообщения
            if len(body) > max_length:
                body = body[:max_length] + "...(сообщение обрезано)"

            return body
        except Exception as e:
            logger.error(f"Ошибка форматирования тела письма: {e}")
            return "⚠ Ошибка обработки содержимого письма"

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
        Отправка данных письма в Telegram с повторными попытками и ограничением частоты.
        """
        # Проверяем ограничение частоты
        if not self._check_rate_limit(chat_id):
            threading.Timer(60.0, self.send_to_telegram, args=[chat_id, email_data]).start()
            return False

        for attempt in range(MAX_RETRIES):
            try:
                # Форматируем тело письма
                body = self.format_email_body(email_data["body"], email_data["content_type"])

                # Создаем сообщение
                message = (
                    f"📧 Новое письмо\n\n"
                    f"От: {email_data['from']}\n"
                    f"Тема: {email_data['subject']}\n"
                    f"Дата: {email_data['date']}\n\n"
                    f"{body}"
                )

                # Проверяем наличие вложений
                has_attachments = "attachments" in email_data and email_data["attachments"]

                if not has_attachments:
                    # Отправляем обычное сообщение без вложений
                    self.bot.send_message(chat_id, message, parse_mode="Markdown")
                    logger.info(f"Сообщение успешно отправлено в чат {chat_id}")
                else:
                    # Если есть ровно одно вложение, отправляем его с текстом сообщения как caption
                    if len(email_data["attachments"]) == 1:
                        self.send_attachment_with_message(chat_id, email_data["attachments"][0], message)
                    else:
                        # Отправляем первое вложение с сообщением как caption
                        self.send_attachment_with_message(chat_id, email_data["attachments"][0], message)

                        # Отправляем остальные вложения без текста
                        for attachment in email_data["attachments"][1:]:
                            self.send_attachment_to_telegram(chat_id, attachment)

                return True

            except Exception as e:
                if attempt < MAX_RETRIES - 1:
                    wait_time = RETRY_DELAY * (2 ** attempt)
                    logger.warning(
                        f"Попытка {attempt + 1} отправки сообщения не удалась: {e}. Повторяем через {wait_time}с...")
                    time.sleep(wait_time)
                else:
                    logger.error(f"Ошибка отправки сообщения в Telegram после {MAX_RETRIES} попыток: {e}")
                    try:
                        simple_message = f"📧 Новое письмо\n\nТема: {email_data['subject']}"
                        self.bot.send_message(chat_id, simple_message)
                        logger.info(f"Отправлено упрощенное сообщение в чат {chat_id}")
                        return True
                    except Exception as e2:
                        logger.error(f"Критическая ошибка отправки: {e2}")
                        return False

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
                self.bot.send_message(chat_id, message, parse_mode="Markdown")
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
                            self.bot.send_photo(chat_id, photo, caption=truncated_message, parse_mode="Markdown")
                    else:
                        with open(temp_file_path, 'rb') as document:
                            self.bot.send_document(chat_id, document, caption=truncated_message, parse_mode="Markdown")
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
                            self.bot.send_message(chat_id, message, parse_mode="Markdown")
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
                self.bot.send_message(chat_id, message, parse_mode="Markdown")
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

    def send_attachment_to_telegram(self, chat_id: str, attachment: Dict[str, Any]) -> None:
        """
        Отправка вложения в Telegram с сохранением имени файла и расширения.
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
                logger.warning(f"Вложение {filename} слишком большое ({file_size / (1024 * 1024):.2f} МБ), пропускаем")
                self.bot.send_message(chat_id, f"⚠️ Вложение {safe_filename} слишком большое для отправки в Telegram")
                return

            for attempt in range(MAX_RETRIES):
                try:
                    if content_type.startswith('image/'):
                        with open(temp_file_path, 'rb') as photo:
                            self.bot.send_photo(chat_id, photo, caption=safe_filename)
                        logger.info(f"Изображение {filename} отправлено в чат {chat_id}")
                    else:
                        with open(temp_file_path, 'rb') as document:
                            self.bot.send_document(chat_id, document, caption=safe_filename)
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
                        try:
                            self.bot.send_message(chat_id, f"⚠️ Не удалось отправить вложение: {safe_filename}")
                        except Exception:
                            pass

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