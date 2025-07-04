import email
import email.utils
import html
import re
import uuid
from email.header import decode_header
from functools import lru_cache
from typing import Any, Dict, List, Optional, Tuple

from bs4 import BeautifulSoup, NavigableString, Tag

from src.utils.logger import get_logger

# Настройка логирования для этого модуля
logger = get_logger("email_parser")

# Константа с префиксами для очистки темы письма
SUBJECT_PREFIXES = ["[deeray.com] ", "Re: ", "Fwd: ", "Fw: "]


@lru_cache(maxsize=128)
def decode_mime_header(header: str) -> str:
    """Декодирование MIME-заголовков с кэшированием."""
    try:
        decoded_parts = decode_header(header)
        decoded_str = ""

        for part, encoding in decoded_parts:
            if isinstance(part, bytes):
                # Проверяем наличие кодировки и используем utf-8 как fallback
                charset = encoding if encoding else 'utf-8'
                try:
                    decoded_str += part.decode(charset, errors='replace')
                except LookupError:  # Если кодировка неизвестна
                    logger.warning(f"Неизвестная кодировка '{charset}', используем 'utf-8' с заменой.")
                    decoded_str += part.decode('utf-8', errors='replace')
            else:
                decoded_str += str(part)

        return decoded_str
    except Exception as e:
        logger.error(f"Ошибка при декодировании заголовка: {e}")
        # Возвращаем исходный заголовок в случае ошибки декодирования
        return header if isinstance(header, str) else str(header)


def extract_email_body(email_message: email.message.Message) -> Tuple[str, str, Optional[str]]:
    """Извлечение тела письма с сохранением raw HTML."""
    body = None
    content_type = "text/plain"
    html_body = None
    plain_body = None
    raw_html_body = None

    try:
        if email_message.is_multipart():
            for part in email_message.walk():
                if part.get_content_maintype() == 'multipart' or part.get('Content-Disposition', '').startswith(
                        'attachment'):
                    continue

                current_content_type = part.get_content_type()
                charset = part.get_content_charset() or "utf-8"
                payload = part.get_payload(decode=True)

                if payload is None: continue  # Пропускаем части без содержимого

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
                        raw_html_body = html_body  # Сохраняем сырой HTML
                    except LookupError:
                        logger.warning(f"Неизвестная кодировка '{charset}' для text/html, используем utf-8.")
                        html_body = payload.decode('utf-8', errors="replace")
                        raw_html_body = html_body
                    except Exception as e_dec:
                        logger.error(f"Ошибка декодирования text/html: {e_dec}")

        else:  # Если письмо не multipart
            charset = email_message.get_content_charset() or "utf-8"
            payload = email_message.get_payload(decode=True)
            if payload:
                try:
                    body = payload.decode(charset, errors="replace")
                    content_type = email_message.get_content_type()
                    if content_type == "text/html":
                        raw_html_body = body
                        html_body = body  # Для логики выбора ниже
                    elif content_type == "text/plain":
                        plain_body = body  # Для логики выбора ниже
                except LookupError:
                    logger.warning(f"Неизвестная кодировка '{charset}' для non-multipart, используем utf-8.")
                    body = payload.decode('utf-8', errors="replace")
                    # Пытаемся определить тип еще раз
                    content_type = email_message.get_content_type()
                    if content_type == "text/html":
                        raw_html_body = body
                        html_body = body
                    elif content_type == "text/plain":
                        plain_body = body
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


def extract_attachments(email_message: email.message.Message) -> List[Dict[str, Any]]:
    """Извлечение вложений из письма."""
    attachments = []
    processed_parts = set()  # Для предотвращения дублирования из-за walk()

    if not email_message.is_multipart():
        return attachments

    try:
        for part in email_message.walk():
            part_id = id(part)
            if part_id in processed_parts: continue
            processed_parts.add(part_id)

            if part.is_multipart(): continue

            filename = part.get_filename()
            content_disposition = part.get('Content-Disposition', '')
            is_attachment = bool(filename) or ('attachment' in content_disposition)

            if not is_attachment and not ('inline' in content_disposition):
                continue

            if not filename and ('attachment' in content_disposition or 'inline' in content_disposition):
                filename_match = re.search(r'filename\*?=(?:(["\'])(.*?)\1|([^;\s]+))', content_disposition,
                                           re.IGNORECASE)
                if filename_match:
                    encoded_name = filename_match.group(2) or filename_match.group(3)
                    if encoded_name:
                        if encoded_name.lower().startswith("utf-8''"):
                            try:
                                # Здесь используется email.utils
                                filename = email.utils.unquote(encoded_name.split("''", 1)[1])
                            except:
                                filename = encoded_name
                        else:
                            filename = encoded_name
                    else:
                        filename = f"attachment_{uuid.uuid4().hex[:8]}.bin"
                else:
                    filename = f"attachment_{uuid.uuid4().hex[:8]}.bin"

            if not filename:
                filename = f"attachment_{uuid.uuid4().hex[:8]}.bin"

            filename = decode_mime_header(filename)

            try:
                content = part.get_payload(decode=True)
            except Exception as payload_err:
                logger.error(f"Ошибка при получении payload для '{filename}': {payload_err}")
                continue

            if content is None or len(content) == 0:
                logger.warning(f"Вложение '{filename}' не имеет содержимого или оно пустое, пропускаем")
                continue

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
        logger.error(f"Ошибка при извлечении вложений: {e}", exc_info=True)
        return []


def clean_subject(subject: str) -> str:
    """Очистка темы от префиксов."""
    original_subject = subject
    try:
        if not isinstance(subject, str):
            subject = str(subject)

        subject = subject.strip()

        while True:
            found_prefix = False
            for prefix in SUBJECT_PREFIXES:
                if subject.lower().startswith(prefix.lower()):
                    subject = subject[len(prefix):].strip()
                    found_prefix = True
                    break
            if not found_prefix:
                break

        return subject
    except Exception as e:
        logger.error(f"Ошибка при очистке темы письма ('{original_subject}'): {e}")
        return original_subject


def format_email_body(body: str, content_type: str) -> str:
    """
    Форматирует тело письма (HTML -> Текст), корректно обрабатывая <br> и <p>
    на основе известной структуры генерации HTML.
    """
    final_text = ""
    try:
        if content_type == "text/html":
            try:
                unescaped_body = html.unescape(body)
                soup = BeautifulSoup(unescaped_body, 'html.parser')
                content_cells = soup.find_all('td')
                processed_parts = []

                if not content_cells:
                    logger.warning("Не найдены теги <td>, попытка обработки всего body.")
                    content_cells = [soup]

                for cell in content_cells:
                    current_cell_parts = []
                    processed_text_nodes = set()
                    for element in cell.descendants:
                        if isinstance(element, NavigableString):
                            if id(element) not in processed_text_nodes:
                                text = str(element).strip()
                                if text:
                                    current_cell_parts.append(text)
                        elif isinstance(element, Tag):
                            if element.name == 'br':
                                current_cell_parts.append('\n')
                            elif element.name == 'p':
                                p_text = element.get_text(strip=True)
                                if not p_text:
                                    while current_cell_parts and current_cell_parts[-1] == '\n':
                                        current_cell_parts.pop()
                                    if current_cell_parts:
                                        current_cell_parts.append('\n\n')
                                else:
                                    if current_cell_parts and current_cell_parts[-1] != '\n':
                                        current_cell_parts.append('\n')
                            elif element.name == 'a':
                                href = element.get('href', '').strip()
                                link_text = ' '.join(element.stripped_strings)
                                for text_node in element.find_all(text=True):
                                    processed_text_nodes.add(id(text_node))
                                if href:
                                    if not link_text or link_text == href:
                                        current_cell_parts.append(href)
                                    else:
                                        current_cell_parts.append(f"{link_text} ({href})")
                                    current_cell_parts.append('\n')
                                elif link_text:
                                    current_cell_parts.append(link_text)

                    cell_text = "".join(current_cell_parts)
                    processed_parts.append(cell_text)

                final_text = "\n\n".join(part.strip() for part in processed_parts if part.strip())

            except Exception as parse_err:
                logger.error(f"Ошибка парсинга HTML BeautifulSoup: {parse_err}.", exc_info=True)
                final_text = body.strip()

        elif content_type == "text/plain":
            final_text = body.strip()
        else:
            logger.warning(f"Обработка неизвестного content_type: {content_type}. Используем исходный текст.")
            final_text = body.strip()

        lines = final_text.splitlines()
        filtered_lines = []
        skip_next_line = False
        for line in lines:
            if skip_next_line:
                skip_next_line = False
                continue
            if line.strip() == "Explore in Superset":
                skip_next_line = True
                continue
            filtered_lines.append(line)

        final_text = "\n".join(filtered_lines)
        final_text = re.sub(r'\n{3,}', '\n\n', final_text)
        final_text = "\n".join([line.rstrip() for line in final_text.splitlines()])
        final_text = final_text.strip()

        return final_text

    except Exception as e:
        logger.error(f"Критическая ошибка в format_email_body: {e}", exc_info=True)
        truncated_body = body[:1000] + "..." if body and len(body) > 1000 else body if body else ""
        return f"⚠️ Ошибка обработки содержимого письма (см. логи).\n\n{truncated_body}"