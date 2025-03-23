import sqlite3
from typing import List, Dict, Any, Tuple, Optional
from pathlib import Path
import time
import threading
import functools
import uuid
from functools import lru_cache
from contextlib import contextmanager

from src.utils.logger import get_logger
from src.config import settings
from src.utils.cache_manager import invalidate_caches, is_cache_valid

# Настройка логирования
logger = get_logger("db_tools")

# Кэширование и оптимизация
_query_cache = {}
_cache_lock = threading.RLock()
_cache_ttl = 300  # время жизни кэша в секундах
_SAFE_QUERIES = ('SELECT', 'PRAGMA', 'WITH')

# Флаг, который указывает, что необходимо сбросить пул соединений
_global_reset_flag = False
_global_reset_lock = threading.RLock()

# Пул соединений
_connection_pool = []
_pool_lock = threading.RLock()
_MAX_POOL_SIZE = 3


def set_global_reset_flag(value=True):
    """Устанавливает глобальный флаг сброса для всех соединений"""
    global _global_reset_flag
    with _global_reset_lock:
        _global_reset_flag = value
        logger.debug(f"Глобальный флаг сброса соединений установлен в {value}")


def with_file_lock(func):
    """Декоратор для защиты операций с файлами"""
    file_lock = threading.RLock()

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        with file_lock:
            return func(*args, **kwargs)

    return wrapper


@contextmanager
def get_connection(db_path: str = None) -> sqlite3.Connection:
    """
    Контекстный менеджер для получения соединения с базой данных.
    """
    if db_path is None:
        db_path = settings.DATABASE_PATH

    # Проверяем глобальный флаг сброса
    global _global_reset_flag
    with _global_reset_lock:
        if _global_reset_flag:
            close_all_connections()
            _global_reset_flag = False

    # Проверяем существование файла базы данных
    if not Path(db_path).exists():
        raise FileNotFoundError(f"База данных не найдена: {db_path}")

    conn = None

    # Попытка получить соединение из пула
    with _pool_lock:
        for i, (connection, path) in enumerate(_connection_pool):
            if path == db_path:
                conn = connection
                del _connection_pool[i]
                break

    # Если нет свободного соединения, создаем новое
    if conn is None:
        conn = _create_connection(db_path)

    try:
        # Remove the problematic line that tried to set query_id
        yield conn
    finally:
        # Возвращаем соединение в пул или закрываем, если пул полон
        with _pool_lock:
            try:
                conn.rollback()
            except:
                pass

            if len(_connection_pool) < _MAX_POOL_SIZE:
                try:
                    cursor = conn.cursor()
                    cursor.execute("SELECT 1")
                    cursor.fetchone()
                    _connection_pool.append((conn, db_path))
                except sqlite3.Error:
                    try:
                        conn.close()
                    except:
                        pass
            else:
                try:
                    conn.close()
                except:
                    pass


def _create_connection(db_path: str) -> sqlite3.Connection:
    """
    Создает новое оптимизированное соединение с базой данных.

    Args:
        db_path: Путь к файлу базы данных

    Returns:
        Соединение с базой данных
    """
    conn = sqlite3.connect(db_path, check_same_thread=False, timeout=30, isolation_level="IMMEDIATE")
    conn.row_factory = sqlite3.Row

    # Установка прагм для оптимизации производительности
    cursor = conn.cursor()
    cursor.execute('PRAGMA journal_mode=WAL')
    cursor.execute('PRAGMA synchronous=NORMAL')
    cursor.execute('PRAGMA cache_size=-10000')  # ~10MB кэша
    cursor.execute('PRAGMA foreign_keys=ON')
    cursor.execute('PRAGMA temp_store=MEMORY')
    cursor.execute('PRAGMA mmap_size=134217728')  # 128MB для memory-mapped I/O

    # Добавляем дополнительную оптимизацию
    cursor.execute('PRAGMA busy_timeout=30000')  # 30 сек ожидания блокировки

    return conn


def _get_cache_key(query: str, params: tuple) -> str:
    """
    Генерирует ключ кэша для запроса и параметров.

    Args:
        query: SQL-запрос
        params: Параметры для безопасной подстановки

    Returns:
        Строковый ключ кэша
    """
    # Убираем комментарии с временными метками из запроса при генерации ключа
    clean_query = query
    if "/*" in query and "*/" in query:
        clean_query = query[query.find("*/") + 2:].strip()

    return f"{clean_query}_{hash(params if params else tuple())}"


def _is_safe_for_cache(query: str) -> bool:
    """
    Проверяет, является ли запрос безопасным для кэширования.

    Args:
        query: SQL-запрос

    Returns:
        True, если запрос безопасен для кэширования, иначе False
    """
    clean_query = query
    if "/*" in query and "*/" in query:
        clean_query = query[query.find("*/") + 2:].strip()

    query_upper = clean_query.strip().upper()
    return any(query_upper.startswith(prefix) for prefix in _SAFE_QUERIES)


def execute_query(
        db_path: Optional[str] = None,
        query: Optional[str] = None,
        params: Optional[tuple] = None
) -> Tuple[bool, List[Dict[str, Any]], List[str], str]:
    """
    Выполнение SQL-запроса с проверкой безопасности и сбросом кэша.

    Args:
        db_path: Путь к файлу базы данных
        query: SQL-запрос
        params: Параметры для безопасной подстановки

    Returns:
        Кортеж (успех, результаты, заголовки, сообщение об ошибке)
    """
    if db_path is None:
        db_path = settings.DATABASE_PATH

    if not Path(db_path).exists():
        return False, [], [], "База данных не найдена"

    if query is None:
        return False, [], [], "Запрос не может быть пустым"

    # Проверка на потенциально опасные запросы
    if any(op.upper() in query.upper() for op in
           ["DROP", "TRUNCATE", "DELETE FROM users", "DELETE FROM subjects"]):
        return False, [], [], "Опасные операции запрещены. Пожалуйста, используйте интерфейс для управления пользователями и темами."

    # Добавляем в запрос временную метку для сброса кэша SQLite
    query_id = str(uuid.uuid4())[:6]
    query_with_comment = f"/* {time.time()} {query_id} */ {query}"

    # Определяем, является ли запрос модифицирующим (не SELECT/PRAGMA)
    clean_query = query.strip().upper()
    is_modifying = not any(clean_query.startswith(prefix) for prefix in _SAFE_QUERIES)

    # Проверяем кэш только для запросов на чтение
    is_cacheable = _is_safe_for_cache(query)
    cache_key = None

    if is_cacheable and not is_modifying:
        cache_key = _get_cache_key(query, params)
        with _cache_lock:
            if cache_key in _query_cache:
                cache_time, results, headers = _query_cache[cache_key]
                # Modified condition: check both TTL and global invalidation timestamp
                if time.time() - cache_time < _cache_ttl and is_cache_valid(cache_time):
                    return True, results, headers, ""

    try:
        with get_connection(db_path) as conn:
            # Форсируем чекпойнт WAL для согласованности данных
            if is_modifying:
                try:
                    conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
                except:
                    pass  # Игнорируем ошибки чекпойнта

            cursor = conn.cursor()

            # Выполнение запроса
            cursor.execute(query_with_comment, params or ())

            # Для запросов на чтение (SELECT)
            if is_cacheable:
                rows = cursor.fetchall()

                if rows:
                    # Преобразуем в список словарей
                    results = []
                    for row in rows:
                        results.append({key: row[key] for key in row.keys()})

                    # Получаем заголовки
                    headers = list(results[0].keys()) if results else []

                    # Сохраняем в кэш, только если запрос не модифицирующий
                    if cache_key and not is_modifying:
                        with _cache_lock:
                            _query_cache[cache_key] = (time.time(), results, headers)

                    return True, results, headers, ""
                else:
                    # Запрос выполнен успешно, но нет данных
                    if cache_key and not is_modifying:
                        with _cache_lock:
                            _query_cache[cache_key] = (time.time(), [], [])
                    return True, [], [], "Запрос не вернул данных"
            else:
                # Для не-SELECT запросов, выполняем коммит и возвращаем количество затронутых строк
                conn.commit()
                affected = cursor.rowcount

                # Для модифицирующих запросов очищаем весь кэш и выполняем дополнительную синхронизацию
                if is_modifying:
                    with _cache_lock:
                        _query_cache.clear()
                    try:
                        # Выполняем дополнительный сброс кэша SQLite
                        conn.execute("PRAGMA wal_checkpoint(RESTART)")
                    except:
                        pass

                    # Устанавливаем флаг сброса глобально
                    set_global_reset_flag(True)

                    # Add this line to trigger cross-process cache invalidation
                    invalidate_caches()

                # Сообщение о результате операции
                message = "Запрос выполнен успешно."
                if is_modifying and affected >= 0:  # Показываем только для модифицирующих запросов с валидным счетчиком
                    message += f" Затронуто строк: {affected}"

                return True, [], [], message

    except sqlite3.Error as e:
        logger.error(f"Ошибка SQL: {e}, запрос: {query_with_comment[:100]}")
        return False, [], [], f"Ошибка SQL: {e}"


# Используем декоратор lru_cache для кэширования результатов запросов
@lru_cache(maxsize=128)
def get_table_list(db_path: str) -> List[str]:
    """
    Получение списка таблиц в базе данных.

    Args:
        db_path: Путь к файлу базы данных

    Returns:
        Список имен таблиц
    """
    if not Path(db_path).exists():
        return []

    try:
        with get_connection(db_path) as conn:
            cursor = conn.cursor()

            # Запрос для получения списка таблиц
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
            tables = [row[0] for row in cursor.fetchall() if row[0] != 'sqlite_sequence']

            return tables

    except sqlite3.Error as e:
        logger.error(f"Ошибка при получении списка таблиц: {e}")
        return []


# Используем декоратор lru_cache для кэширования результатов запросов
@lru_cache(maxsize=128)
def get_table_info(db_path: str, table_name: str) -> List[Dict[str, Any]]:
    """
    Получение информации о структуре таблицы.

    Args:
        db_path: Путь к файлу базы данных
        table_name: Имя таблицы

    Returns:
        Список столбцов с их параметрами
    """
    if not Path(db_path).exists():
        return []

    try:
        with get_connection(db_path) as conn:
            cursor = conn.cursor()

            # Запрос для получения информации о таблице
            cursor.execute(f"PRAGMA table_info({table_name})")
            columns = cursor.fetchall()

            # Преобразуем в более понятный формат
            result = []
            for column in columns:
                col_info = {
                    'cid': column[0],
                    'name': column[1],
                    'type': column[2],
                    'notnull': column[3],
                    'dflt_value': column[4],
                    'pk': column[5]
                }
                result.append(col_info)

            return result

    except sqlite3.Error as e:
        logger.error(f"Ошибка при получении информации о таблице {table_name}: {e}")
        return []


def get_table_row_count(db_path: str, table_name: str) -> int:
    """
    Получение количества строк в таблице.

    Args:
        db_path: Путь к файлу базы данных
        table_name: Имя таблицы

    Returns:
        Количество строк
    """
    if not Path(db_path).exists():
        return 0

    try:
        with get_connection(db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(f"SELECT COUNT(*) FROM {table_name}")
            result = cursor.fetchone()
            return result[0] if result else 0
    except sqlite3.Error as e:
        logger.error(f"Ошибка при получении количества строк в таблице {table_name}: {e}")
        return 0


def get_common_queries() -> Dict[str, str]:
    """
    Возвращает словарь с готовыми SQL-запросами.

    Returns:
        Словарь с именами запросов и их текстом
    """
    return {
        "Все пользователи": "SELECT * FROM users;",
        "Все темы": "SELECT * FROM subjects;",
        "Пользователи с темами": """
            SELECT u.chat_id, u.status, COUNT(s.id) as subject_count 
            FROM users u 
            LEFT JOIN subjects s ON u.chat_id = s.chat_id 
            GROUP BY u.chat_id;
        """,
        "Активные пользователи": "SELECT * FROM users WHERE status = 'Enable';",
        "Неактивные пользователи": "SELECT * FROM users WHERE status = 'Disable';",
        "Статистика": """
            SELECT 
                (SELECT COUNT(*) FROM users) as total_users,
                (SELECT COUNT(*) FROM users WHERE status = 'Enable') as active_users,
                (SELECT COUNT(*) FROM subjects) as total_subjects,
                (SELECT COUNT(DISTINCT subject) FROM subjects) as unique_subjects;
        """,
        "Популярные темы": """
            SELECT subject, COUNT(chat_id) as user_count
            FROM subjects
            GROUP BY subject
            ORDER BY user_count DESC
            LIMIT 20;
        """,
        "Пользователи без тем": """
            SELECT u.chat_id, u.status
            FROM users u
            LEFT JOIN subjects s ON u.chat_id = s.chat_id
            WHERE s.id IS NULL;
        """
    }


def clear_query_cache():
    """Очищает кэш запросов."""
    from src.utils.cache_manager import invalidate_caches

    with _cache_lock:
        cache_size = len(_query_cache)
        _query_cache.clear()
    get_table_list.cache_clear()
    get_table_info.cache_clear()
    logger.info(f"Кэш запросов очищен, удалено {cache_size} записей")

    # Add this line to trigger cross-process cache invalidation
    invalidate_caches()

    # Устанавливаем флаг сброса соединений
    set_global_reset_flag(True)


def close_all_connections():
    """Закрывает все соединения в пуле."""
    with _pool_lock:
        conn_count = len(_connection_pool)
        for conn, _ in _connection_pool:
            try:
                conn.close()
            except Exception as e:
                logger.debug(f"Ошибка при закрытии соединения: {e}")
        _connection_pool.clear()
    logger.info(f"Закрыто {conn_count} соединений в пуле")


def optimize_database(db_path: str = None) -> bool:
    """
    Выполняет оптимизацию базы данных.

    Args:
        db_path: Путь к файлу базы данных

    Returns:
        True при успешном выполнении, False при ошибке
    """
    if db_path is None:
        db_path = settings.DATABASE_PATH

    if not Path(db_path).exists():
        logger.error(f"База данных не найдена: {db_path}")
        return False

    try:
        # Сначала закрываем все соединения
        close_all_connections()

        # Создаем новое изолированное соединение
        conn = sqlite3.connect(db_path, isolation_level="EXCLUSIVE", timeout=60)
        cursor = conn.cursor()

        # Проверка целостности базы данных
        logger.info("Проверка целостности базы данных...")
        cursor.execute("PRAGMA integrity_check")
        integrity_result = cursor.fetchone()[0]
        if integrity_result != "ok":
            logger.warning(f"Проверка целостности БД: {integrity_result}")

        # Очистка WAL файла
        logger.info("Очистка WAL файла...")
        cursor.execute("PRAGMA wal_checkpoint(TRUNCATE)")

        # VACUUM для уменьшения размера файла базы данных
        logger.info("Выполнение VACUUM...")
        cursor.execute("VACUUM")

        # Анализ для оптимизации запросов
        logger.info("Выполнение ANALYZE...")
        cursor.execute("ANALYZE")

        # Оптимизация индексов
        logger.info("Оптимизация индексов...")
        cursor.execute("PRAGMA optimize")

        # Сброс на ROLLBACK-журнал и обратно на WAL для очистки файлов
        logger.info("Переключение режимов журнала...")
        cursor.execute("PRAGMA journal_mode=DELETE")
        cursor.execute("PRAGMA journal_mode=WAL")

        conn.close()

        # Устанавливаем флаг сброса пула соединений
        set_global_reset_flag(True)

        logger.info(f"База данных {db_path} успешно оптимизирована")
        return True
    except sqlite3.Error as e:
        logger.error(f"Ошибка при оптимизации базы данных: {e}")
        return False