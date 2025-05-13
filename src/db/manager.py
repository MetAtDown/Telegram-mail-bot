import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Any, Union, Tuple
from contextlib import contextmanager
import queue
import traceback

from src.utils.logger import get_logger
from src.config import settings
from src.utils.cache_manager import is_cache_valid, invalidate_caches

# Настройка логирования
logger = get_logger("db_manager")

# Константы для повторных попыток соединения
MAX_RETRY_ATTEMPTS = 3
RETRY_DELAY = 0.5  # в секундах

# Константы для маски
DELIVERY_MODE_TEXT = 'text'
DELIVERY_MODE_HTML = 'html'
DELIVERY_MODE_SMART = 'smart'
DELIVERY_MODE_PDF = 'pdf'
ALLOWED_DELIVERY_MODES = {DELIVERY_MODE_TEXT, DELIVERY_MODE_HTML, DELIVERY_MODE_SMART, DELIVERY_MODE_PDF}
DEFAULT_DELIVERY_MODE = DELIVERY_MODE_SMART

class DatabaseManager:
    _instance = None
    _lock = threading.Lock()

    # Максимальный размер пула соединений
    _MAX_CONNECTIONS = 5

    def __new__(cls, *args, **kwargs):
        """Реализация паттерна Singleton для работы с одним экземпляром БД."""
        with cls._lock:
            if cls._instance is None:
                cls._instance = super(DatabaseManager, cls).__new__(cls)
        return cls._instance

    def __init__(self, db_path: Optional[Union[str, Path]] = None):
        """
        Инициализация менеджера базы данных.

        Args:
            db_path: Путь к файлу базы данных (если None, используется путь из настроек)
        """
        # Избегаем повторной инициализации для паттерна Singleton
        if hasattr(self, 'initialized'):
            return

        # Флаг для сброса пула соединений
        self._reset_pool = False

        self.db_path = str(db_path or settings.DATABASE_PATH)
        # Убеждаемся, что директория существует
        db_dir = Path(self.db_path).parent
        if not db_dir.exists():
            db_dir.mkdir(parents=True, exist_ok=True)
            logger.info(f"Создана директория для БД: {db_dir}")

        logger.info(f"Используется БД: {self.db_path}")
        logger.info(f"БД существует: {Path(self.db_path).exists()}")

        # Пул соединений
        self.connection_pool = queue.Queue(maxsize=self._MAX_CONNECTIONS)
        self.connections_in_use = 0
        self.connection_lock = threading.RLock()

        # Словарь для отслеживания времени использования соединений
        self.connection_timestamps = {}
        self.timestamps_lock = threading.RLock()

        # Инициализация пула соединений
        self._init_connection_pool()

        # Кэш для часто запрашиваемых данных
        self.cache = {}
        self.cache_lock = threading.RLock()
        self.cache_ttl = 300  # время жизни кэша в секундах

        self.initialize_database()
        self.initialized = True

        # Запуск фонового потока для проверки соединений
        self.should_run = True
        self.health_check_thread = threading.Thread(
            target=self._connection_health_check,
            daemon=True
        )
        self.health_check_thread.start()

    def _init_connection_pool(self):
        """Инициализирует пул соединений."""
        for _ in range(self._MAX_CONNECTIONS):
            try:
                conn = self._create_connection()
                self.connection_pool.put(conn)
                logger.debug(f"Добавлено соединение в пул: {id(conn)}")
            except Exception as e:
                logger.error(f"Ошибка при создании соединения для пула: {e}")

    def _create_connection(self) -> sqlite3.Connection:
        """Создает новое соединение с базой данных с оптимальными настройками."""
        # Проверяем существование файла БД и директории
        db_path = self.db_path
        db_dir = Path(db_path).parent

        # Создаем директорию, если её нет
        if not db_dir.exists():
            logger.info(f"Создаем директорию для БД: {db_dir}")
            db_dir.mkdir(parents=True, exist_ok=True)

        # Проверяем доступность файла
        if Path(db_path).exists():
            logger.debug(f"БД существует по пути: {db_path}")
        else:
            logger.info(f"БД не найдена, будет создана: {db_path}")

        try:
            # Используем IMMEDIATE для улучшения изоляции транзакций
            conn = sqlite3.connect(db_path, check_same_thread=False, timeout=30, isolation_level="IMMEDIATE")
            conn.row_factory = sqlite3.Row

            # Установка прагм для оптимизации производительности
            cursor = conn.cursor()
            cursor.execute('PRAGMA journal_mode=WAL')  # Write-Ahead Logging для улучшения конкурентного доступа
            cursor.execute('PRAGMA synchronous=NORMAL')  # Компромисс между производительностью и надежностью
            cursor.execute('PRAGMA cache_size=-20000')  # ~20MB кэша (в страницах)
            cursor.execute('PRAGMA foreign_keys=ON')  # Включаем поддержку внешних ключей
            cursor.execute('PRAGMA temp_store=MEMORY')  # Хранение временных таблиц в памяти
            cursor.execute('PRAGMA mmap_size=268435456')  # 256MB для memory-mapped I/O

            # Сохраняем метку времени в отдельном словаре
            with self.timestamps_lock:
                self.connection_timestamps[id(conn)] = time.time()

            return conn
        except Exception as e:
            logger.error(f"Ошибка создания соединения с БД: {e}, путь: {db_path}")
            raise

    @contextmanager
    def get_connection(self) -> sqlite3.Connection:
        """
        Получение соединения с базой данных из пула с поддержкой многопоточности.
        Реализовано как контекстный менеджер для автоматического возврата соединения в пул.

        Returns:
            Соединение с базой данных
        """
        conn = None
        for attempt in range(MAX_RETRY_ATTEMPTS):
            try:
                with self.connection_lock:
                    # Если есть свободное соединение в пуле, используем его
                    if not self.connection_pool.empty():
                        conn = self.connection_pool.get(block=False)
                        self.connections_in_use += 1
                        logger.debug(f"Получено соединение из пула: {id(conn)}")
                    # Если пул пуст, но мы не достигли лимита, создаем новое соединение
                    elif self.connections_in_use < self._MAX_CONNECTIONS:
                        conn = self._create_connection()
                        self.connections_in_use += 1
                        logger.debug(f"Создано новое соединение: {id(conn)}")
                    # Если все соединения используются, ждем освобождения с таймаутом
                    else:
                        logger.warning("Все соединения в пуле заняты, ожидаем освобождения...")

                if conn is None:
                    # Если не удалось получить соединение, пробуем подождать
                    try:
                        conn = self.connection_pool.get(block=True, timeout=5)
                        with self.connection_lock:
                            self.connections_in_use += 1
                            logger.debug(f"Получено соединение из пула после ожидания: {id(conn)}")
                    except queue.Empty:
                        if attempt < MAX_RETRY_ATTEMPTS - 1:
                            logger.warning(
                                f"Тайм-аут ожидания соединения, повторная попытка {attempt + 1}/{MAX_RETRY_ATTEMPTS}")
                            time.sleep(RETRY_DELAY)
                            continue
                        else:
                            raise RuntimeError("Не удалось получить соединение с базой данных")

                # Проверяем, работает ли соединение
                try:
                    cursor = conn.cursor()
                    cursor.execute("SELECT 1")
                    cursor.fetchone()

                    # Обновляем метку времени
                    with self.timestamps_lock:
                        self.connection_timestamps[id(conn)] = time.time()
                    break
                except sqlite3.Error:
                    # Если соединение неисправно, создаем новое
                    logger.warning(f"Обнаружено неработающее соединение {id(conn)}, пересоздаем...")
                    try:
                        with self.timestamps_lock:
                            self.connection_timestamps.pop(id(conn), None)
                        conn.close()
                    except:
                        pass

                    with self.connection_lock:
                        self.connections_in_use -= 1

                    conn = self._create_connection()
                    with self.connection_lock:
                        self.connections_in_use += 1
                        logger.debug(f"Пересоздано соединение: {id(conn)}")
                    break
            except Exception as e:
                logger.error(f"Ошибка при получении соединения (попытка {attempt + 1}/{MAX_RETRY_ATTEMPTS}): {e}")
                logger.debug(traceback.format_exc())
                if attempt == MAX_RETRY_ATTEMPTS - 1:
                    raise
                time.sleep(RETRY_DELAY)

        try:
            yield conn
        finally:
            # Возвращаем соединение в пул, независимо от результата операций
            try:
                if conn:
                    # Сбрасываем незавершенные транзакции перед возвратом соединения
                    try:
                        conn.rollback()
                    except:
                        pass

                    # Если установлен флаг сброса или соединение в плохом состоянии, закрываем его
                    if self._reset_pool:
                        try:
                            conn.close()
                            logger.debug(f"Соединение {id(conn)} закрыто из-за сброса пула")
                        except Exception as e:
                            logger.error(f"Ошибка при закрытии соединения: {e}")
                    else:
                        # Обновляем метку времени и возвращаем в пул
                        with self.timestamps_lock:
                            self.connection_timestamps[id(conn)] = time.time()
                        self.connection_pool.put(conn)
                        logger.debug(f"Соединение {id(conn)} возвращено в пул")

                    with self.connection_lock:
                        self.connections_in_use -= 1
                        if self._reset_pool:
                            self._reset_pool = False
                            logger.debug("Сброшен флаг очистки пула соединений")
            except Exception as e:
                logger.error(f"Ошибка при возврате соединения в пул: {e}")

    def _connection_health_check(self):
        """Фоновый поток для проверки состояния соединений в пуле."""
        check_interval = 60  # Проверка каждую минуту

        while self.should_run:
            try:
                time.sleep(check_interval)

                # Проверяем только если есть свободные соединения в пуле
                connections_to_check = []

                try:
                    # Извлекаем все соединения из пула для проверки
                    while not self.connection_pool.empty():
                        conn = self.connection_pool.get(block=False)
                        connections_to_check.append(conn)
                except queue.Empty:
                    pass

                for conn in connections_to_check:
                    try:
                        # Проверяем соединение
                        cursor = conn.cursor()
                        cursor.execute("SELECT 1")
                        cursor.fetchone()

                        # Проверяем, не устарело ли соединение (старше 30 минут)
                        current_time = time.time()
                        with self.timestamps_lock:
                            last_used = self.connection_timestamps.get(id(conn), 0)
                            if current_time - last_used > 1800:  # 30 минут в секундах
                                logger.info(f"Закрываем устаревшее соединение {id(conn)} и создаем новое")
                                self.connection_timestamps.pop(id(conn), None)
                                conn.close()
                                conn = self._create_connection()

                        # Возвращаем соединение в пул
                        self.connection_pool.put(conn)
                    except Exception as e:
                        logger.warning(f"Найдено неработающее соединение {id(conn)}, пересоздаем: {e}")
                        try:
                            with self.timestamps_lock:
                                self.connection_timestamps.pop(id(conn), None)
                            conn.close()
                        except:
                            pass

                        # Создаем новое соединение взамен неработающего
                        new_conn = self._create_connection()
                        self.connection_pool.put(new_conn)
            except Exception as e:
                logger.error(f"Ошибка в потоке проверки состояния соединений: {e}")

    def close_connection_pool(self) -> None:
        """Закрытие всех соединений в пуле."""
        logger.info("Закрытие пула соединений с базой данных")
        self.should_run = False  # Останавливаем поток проверки соединений

        if hasattr(self, 'health_check_thread') and self.health_check_thread.is_alive():
            self.health_check_thread.join(timeout=1)

        connections = []

        # Извлекаем все соединения из пула
        try:
            while not self.connection_pool.empty():
                conn = self.connection_pool.get(block=False)
                connections.append(conn)
        except queue.Empty:
            pass

        # Закрываем все соединения
        for conn in connections:
            try:
                with self.timestamps_lock:
                    self.connection_timestamps.pop(id(conn), None)
                conn.close()
                logger.debug("Соединение с базой данных закрыто")
            except Exception as e:
                logger.error(f"Ошибка при закрытии соединения: {e}")

        # Очищаем словарь времени использования
        with self.timestamps_lock:
            self.connection_timestamps.clear()

    def shutdown(self) -> None:
        """Безопасное завершение работы менеджера базы данных."""
        try:
            logger.info("Начинаем безопасное завершение работы менеджера базы данных")
            self.close_connection_pool()
            logger.info("Менеджер базы данных успешно завершил работу")
        except Exception as e:
            logger.error(f"Ошибка при завершении работы менеджера базы данных: {e}")

    def initialize_database(self) -> None:
        """
        Инициализация базы данных: создание/обновление таблиц.
        - Убран delivery_mode из users.
        - Добавлен delivery_mode в subjects (с миграцией).
        """
        logger.info("Проверка и инициализация структуры базы данных...")
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()

                # --- Таблица users (БЕЗ delivery_mode) ---
                logger.debug("Проверка таблицы 'users'...")
                cursor.execute('''
                CREATE TABLE IF NOT EXISTS users (
                    chat_id TEXT PRIMARY KEY,
                    status TEXT DEFAULT 'Enable' NOT NULL CHECK (status IN ('Enable', 'Disable')),
                    notes TEXT DEFAULT '',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                ''')
                # Удаляем старый индекс, если он был
                cursor.execute('DROP INDEX IF EXISTS idx_users_delivery_mode')
                # Проверяем и удаляем старый столбец, если он существует (на случай старой схемы)
                try:
                    cursor.execute("PRAGMA table_info(users)")
                    columns = [col['name'] for col in cursor.fetchall()]
                    if 'delivery_mode' in columns:
                        logger.warning("Обнаружен столбец 'delivery_mode' в таблице 'users'. Выполняется удаление...")
                        # SQLite не поддерживает DROP COLUMN напрямую в старых версиях
                        # Безопасный способ - пересоздать таблицу, но это сложно с данными.
                        # Попытаемся ALTER TABLE RENAME COLUMN (если возможно) или просто проигнорируем
                        # В новых версиях SQLite (3.35.0+) можно:
                        # cursor.execute('ALTER TABLE users DROP COLUMN delivery_mode')
                        # Для совместимости пока оставим как есть, но столбец использоваться не будет.
                        # Если возникнут проблемы, потребуется ручная миграция.
                        logger.info("Столбец 'delivery_mode' в 'users' будет проигнорирован.")
                except Exception as alter_err:
                    logger.error(f"Ошибка при проверке/удалении столбца 'delivery_mode' из 'users': {alter_err}")

                cursor.execute('CREATE INDEX IF NOT EXISTS idx_users_status ON users (status)')
                logger.debug("Таблица 'users' проверена/создана.")

                # --- Таблица subjects (С delivery_mode) ---
                logger.debug("Проверка таблицы 'subjects'...")
                cursor.execute(f'''
                CREATE TABLE IF NOT EXISTS subjects (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id TEXT NOT NULL,
                    subject TEXT NOT NULL,
                    delivery_mode TEXT DEFAULT '{DEFAULT_DELIVERY_MODE}' NOT NULL CHECK (delivery_mode IN ('{DELIVERY_MODE_TEXT}', '{DELIVERY_MODE_HTML}', '{DELIVERY_MODE_SMART}', '{DELIVERY_MODE_PDF}')),
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (chat_id) REFERENCES users (chat_id) ON DELETE CASCADE,
                    UNIQUE(chat_id, subject)
                )
                ''')
                logger.debug("Таблица 'subjects' проверена/создана.")

                # Проверка и добавление столбца delivery_mode, если его нет (миграция)
                logger.debug("Проверка наличия столбца 'delivery_mode' в 'subjects'...")
                cursor.execute("PRAGMA table_info(subjects)")
                columns = [col['name'] for col in cursor.fetchall()]
                if 'delivery_mode' not in columns:
                    logger.warning("Столбец 'delivery_mode' отсутствует в таблице 'subjects'. Добавление столбца...")
                    try:
                        # Добавляем столбец с DEFAULT значением
                        alter_query = f'''
                        ALTER TABLE subjects
                        ADD COLUMN delivery_mode TEXT DEFAULT '{DEFAULT_DELIVERY_MODE}' NOT NULL CHECK (delivery_mode IN ('{DELIVERY_MODE_TEXT}', '{DELIVERY_MODE_HTML}', '{DELIVERY_MODE_SMART}', '{DELIVERY_MODE_PDF}'))
                        '''
                        cursor.execute(alter_query)
                        # Важно: После ALTER TABLE ... ADD COLUMN с DEFAULT, SQLite в новых версиях
                        # автоматически заполнит DEFAULT для существующих строк.
                        # В очень старых версиях мог потребоваться UPDATE.
                        conn.commit()  # Коммит после ALTER TABLE
                        logger.info(
                            f"Столбец 'delivery_mode' успешно добавлен в 'subjects' с значением по умолчанию '{DEFAULT_DELIVERY_MODE}'.")
                    except Exception as alter_err:
                        conn.rollback()  # Откат в случае ошибки ALTER
                        logger.error(
                            f"Ошибка при добавлении столбца 'delivery_mode' в 'subjects': {alter_err}. Может потребоваться ручная миграция.")
                        raise  # Прерываем инициализацию, если миграция не удалась
                else:
                    logger.debug("Столбец 'delivery_mode' уже существует в 'subjects'.")

                # Создание индексов для ускорения поиска
                logger.debug("Проверка индексов для 'subjects'...")
                cursor.execute('CREATE INDEX IF NOT EXISTS idx_subjects_chat_id ON subjects (chat_id)')
                cursor.execute('CREATE INDEX IF NOT EXISTS idx_subjects_subject ON subjects (subject COLLATE NOCASE)')
                # Добавляем индекс для ускорения JOIN-запросов и поиска по chat_id/subject
                cursor.execute('CREATE INDEX IF NOT EXISTS idx_subjects_chat_id_subject ON subjects (chat_id, subject)')
                # Индекс для нового поля (возможно, полезен для будущих запросов)
                cursor.execute('CREATE INDEX IF NOT EXISTS idx_subjects_delivery_mode ON subjects (delivery_mode)')
                logger.debug("Индексы для 'subjects' проверены/созданы.")

                conn.commit()
                logger.info("База данных инициализирована успешно (проверена/обновлена структура таблиц)")

        except Exception as e:
            logger.error(f"Критическая ошибка при инициализации базы данных: {e}", exc_info=True)
            raise

    def _clear_cache(self, key=None):
        """Очищает кэш или конкретный ключ в кэше."""
        with self.cache_lock:
            if key:
                old_value = self.cache.pop(key, None)
                logger.debug(f"Очищен кэш для ключа {key}" +
                             (
                                 f", было {len(old_value[0]) if old_value and isinstance(old_value[0], (list, dict)) else old_value}"
                                 if old_value else ""))
            else:
                keys = list(self.cache.keys())
                self.cache.clear()
                logger.debug(f"Очищен весь кэш ({len(keys)} ключей): {keys[:5]}..." if keys else "Кэш был пуст")

    def _get_from_cache(self, key):
        """Получает данные из кэша, если они еще действительны."""
        with self.cache_lock:
            if key in self.cache:
                data, timestamp = self.cache[key]

                # Modified condition: check both TTL and global invalidation timestamp
                if time.time() - timestamp < self.cache_ttl and is_cache_valid(timestamp):
                    logger.debug(f"Данные получены из кэша: {key}")
                    return data
                else:
                    # Данные устарели или глобальный кэш был сброшен, удаляем из кэша
                    del self.cache[key]
                    logger.debug(f"Удалены устаревшие данные из кэша: {key}")
        return None

    def _set_in_cache(self, key, data):
        """Сохраняет данные в кэше с текущей временной меткой."""
        with self.cache_lock:
            self.cache[key] = (data, time.time())
            logger.debug(f"Данные сохранены в кэше: {key}" +
                         (f", размер: {len(data) if isinstance(data, (list, dict)) else 'нескалярный'}"
                          if data is not None else ", пустые данные"))

    def update_subject_delivery_mode(self, chat_id: str, subject: str, mode: str) -> bool:
        """
        Обновление режима доставки для конкретной темы пользователя.

        Args:
            chat_id: ID чата пользователя
            subject: Тема (подписка)
            mode: Новый режим доставки ('text', 'html', 'smart', 'pdf')

        Returns:
            True если режим обновлен успешно, иначе False
        """
        if mode not in ALLOWED_DELIVERY_MODES:
            logger.warning(
                f"Попытка установить неверный режим доставки '{mode}' для темы '{subject}' пользователя {chat_id}")
            return False

        logger.info(f"Попытка обновить режим доставки на '{mode}' для темы '{subject}' пользователя {chat_id}")
        try:
            with self.get_connection() as conn:
                conn.execute('BEGIN IMMEDIATE')
                cursor = conn.cursor()

                query = '''
                UPDATE subjects
                SET delivery_mode = ?
                WHERE chat_id = ? AND subject = ?
                '''
                cursor.execute(query, (mode, chat_id, subject))
                rows_affected = cursor.rowcount
                conn.commit()

                success = rows_affected > 0
                if success:
                    logger.info(
                        f"Режим доставки для темы '{subject}' пользователя {chat_id} успешно обновлен на '{mode}'")
                    # Очищаем кэши, которые зависят от данных тем
                    self._clear_cache(f"user_subjects_{chat_id}")  # Кэш конкретного пользователя
                    self._clear_cache("all_subjects")  # Общий кэш тем
                else:
                    # Проверяем, существует ли такая подписка вообще
                    cursor.execute("SELECT 1 FROM subjects WHERE chat_id = ? AND subject = ?", (chat_id, subject))
                    if cursor.fetchone():
                        logger.warning(
                            f"Не удалось обновить режим доставки для темы '{subject}' пользователя {chat_id}, но запись существует (возможно, режим уже '{mode}'?).")
                    else:
                        logger.warning(
                            f"Не удалось обновить режим доставки: тема '{subject}' не найдена для пользователя {chat_id}")

                return success
        except Exception as e:
            logger.error(f"Ошибка при обновлении режима доставки темы '{subject}' для {chat_id}: {e}", exc_info=True)
            try:
                conn.rollback()
            except Exception as rb_err:
                logger.error(f"Ошибка при откате транзакции: {rb_err}")
            return False

    def refresh_data(self):
        """Принудительное обновление всех кэшированных данных и сброс соединений."""
        logger.info("Выполняется принудительное обновление данных и сброс соединений")
        self._clear_cache()

        # Add this line to trigger cross-process cache invalidation
        invalidate_caches()

        # Force close ALL connections in the pool
        with self.connection_lock:
            self._reset_pool = True
            # Extract all connections from the pool
            connections = []
            while not self.connection_pool.empty():
                try:
                    connections.append(self.connection_pool.get(False))
                except queue.Empty:
                    break

            # Close all connections
            for conn in connections:
                try:
                    conn.close()
                except:
                    pass

            # Reset the connection pool
            self.connection_pool = queue.Queue(maxsize=self._MAX_CONNECTIONS)
            self._init_connection_pool()

        # Force SQLite to reset its internal state
        try:
            with self.get_connection() as conn:
                conn.execute("PRAGMA wal_checkpoint(FULL)")
        except:
            pass

        return True

    def release_connection(self) -> None:
        """Освобождает соединения после запроса"""
        with self.connection_lock:
            prev_value = self._reset_pool
            self._reset_pool = True
            logger.debug(f"Установлен флаг сброса пула соединений (было {prev_value})")

    def get_all_subjects(self) -> Dict[str, List[Dict[str, Any]]]:
        """
        Получение всех тем и связанных с ними данных (chat_id, статус, режим доставки).
        Структура: { 'Тема': [{'chat_id': id, 'enabled': bool, 'delivery_mode': str}, ...], ... }

        Returns:
            Словарь тем с информацией о подписчиках.
        """
        cache_key = "all_subjects"
        cached_data = self._get_from_cache(cache_key)
        if cached_data:
            return cached_data

        logger.debug("Запрос всех тем и данных подписчиков из БД...")
        subject_data = {}
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()

                # Выбираем тему, chat_id, статус пользователя И режим доставки из subjects
                query = '''
                    SELECT s.subject, s.chat_id, u.status, s.delivery_mode
                    FROM subjects s
                    JOIN users u ON s.chat_id = u.chat_id
                    ORDER BY s.subject, u.chat_id  -- Опциональная сортировка для консистентности
                '''
                cursor.execute(query)
                result = cursor.fetchall()

                total_subscriptions = len(result)
                logger.debug(f"Получено {total_subscriptions} записей подписок из БД.")

                for row in result:
                    subject = row['subject']
                    chat_id = row['chat_id']
                    status = row['status']
                    delivery_mode = row['delivery_mode']
                    is_enabled = status == 'Enable'

                    # Создаем запись подписчика
                    subscriber_info = {
                        "chat_id": chat_id,
                        "enabled": is_enabled,
                        "delivery_mode": delivery_mode
                    }

                    # Добавляем в словарь
                    if subject not in subject_data:
                        subject_data[subject] = []
                    subject_data[subject].append(subscriber_info)

                logger.info(f"Сформирован словарь для {len(subject_data)} уникальных тем.")
                # Сохраняем в кэш
                self._set_in_cache(cache_key, subject_data)
                return subject_data
        except Exception as e:
            logger.error(f"Ошибка при получении всех тем и данных подписчиков: {e}", exc_info=True)
            return {}  # Возвращаем пустой словарь в случае ошибки

    def get_user_subjects(self, chat_id: str) -> List[Tuple[str, str]]:
        """
        Получение всех тем пользователя и их режимов доставки.

        Args:
            chat_id: ID чата пользователя

        Returns:
            Список кортежей [(тема, режим_доставки), ...]
        """
        cache_key = f"user_subjects_{chat_id}"
        cached_data = self._get_from_cache(cache_key)
        if cached_data:
            return cached_data

        logger.debug(f"Запрос тем и режимов доставки для пользователя {chat_id} из БД...")
        subjects_with_modes = []
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()

                # Выбираем тему и режим доставки для указанного пользователя
                query = '''
                SELECT subject, delivery_mode
                FROM subjects
                WHERE chat_id = ?
                ORDER BY subject -- Опциональная сортировка
                '''
                cursor.execute(query, (chat_id,))
                result = cursor.fetchall()

                subjects_with_modes = [(row['subject'], row['delivery_mode']) for row in result]
                logger.info(f"Получено {len(subjects_with_modes)} тем для пользователя {chat_id}")

                # Сохраняем в кэш
                self._set_in_cache(cache_key, subjects_with_modes)
                return subjects_with_modes
        except Exception as e:
            logger.error(f"Ошибка при получении тем и режимов пользователя {chat_id}: {e}", exc_info=True)
            return []  # Возвращаем пустой список в случае ошибки

    def get_user_status(self, chat_id: str) -> bool:
        """
        Получение статуса пользователя.

        Args:
            chat_id: ID чата пользователя

        Returns:
            True если пользователь активен, иначе False
        """
        cache_key = f"user_status_{chat_id}"
        cached_data = self._get_from_cache(cache_key)
        if cached_data is not None:
            return cached_data

        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()

                query = '''
                SELECT status
                FROM users
                WHERE chat_id = ?
                '''

                cursor.execute(query, (chat_id,))
                result = cursor.fetchone()

                status = result['status'] == 'Enable' if result else False
                logger.debug(f"Статус пользователя {chat_id}: {'активен' if status else 'отключен'}")

                # Сохраняем в кэш
                self._set_in_cache(cache_key, status)
                return status
        except Exception as e:
            logger.error(f"Ошибка при получении статуса пользователя {chat_id}: {e}")
            return False

    def is_user_registered(self, chat_id: str) -> bool:
        """
        Проверка, зарегистрирован ли пользователь.

        Args:
            chat_id: ID чата пользователя

        Returns:
            True если пользователь зарегистрирован, иначе False
        """
        cache_key = f"user_registered_{chat_id}"
        cached_data = self._get_from_cache(cache_key)
        if cached_data is not None:
            return cached_data

        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()

                query = '''
                SELECT 1 FROM users 
                WHERE chat_id = ?
                '''

                cursor.execute(query, (chat_id,))
                result = cursor.fetchone()

                is_registered = result is not None
                logger.debug(f"Пользователь {chat_id} зарегистрирован: {is_registered}")

                # Сохраняем в кэш
                self._set_in_cache(cache_key, is_registered)
                return is_registered
        except Exception as e:
            logger.error(f"Ошибка при проверке регистрации пользователя {chat_id}: {e}")
            return False

    def update_user_status(self, chat_id: str, enabled: bool) -> bool:
        """
        Обновление статуса пользователя.

        Args:
            chat_id: ID чата пользователя
            enabled: Новый статус (True - активен, False - отключен)

        Returns:
            True если статус обновлен успешно, иначе False
        """
        try:
            with self.get_connection() as conn:
                # Явно начинаем транзакцию
                conn.execute('BEGIN IMMEDIATE')
                cursor = conn.cursor()

                status = 'Enable' if enabled else 'Disable'
                now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

                query = '''
                UPDATE users
                SET status = ?, updated_at = ?
                WHERE chat_id = ?
                '''

                cursor.execute(query, (status, now, chat_id))
                rows_affected = cursor.rowcount
                conn.commit()  # Явный коммит

                success = rows_affected > 0
                logger.info(
                    f"Обновление статуса пользователя {chat_id}: {status}, успех: {success}, затронуто строк: {rows_affected}")

                if success:
                    # Очищаем соответствующие кэши
                    self._clear_cache(f"user_status_{chat_id}")
                    self._clear_cache("all_subjects")
                    self._clear_cache("all_users")

                return success
        except Exception as e:
            logger.error(f"Ошибка при обновлении статуса пользователя {chat_id}: {e}")
            return False

    def get_user_notes(self, chat_id: str) -> str:
        """
        Получение заметок для пользователя.

        Args:
            chat_id: ID чата пользователя

        Returns:
            Текст заметок или пустая строка.
        """
        cache_key = f"user_notes_{chat_id}"
        cached_data = self._get_from_cache(cache_key)
        if cached_data is not None:
            return cached_data

        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                query = 'SELECT notes FROM users WHERE chat_id = ?'
                cursor.execute(query, (chat_id,))
                result = cursor.fetchone()

                notes = result['notes'] if result and result['notes'] else ''
                logger.debug(f"Заметки для пользователя {chat_id}: {len(notes)} символов")

                self._set_in_cache(cache_key, notes)
                return notes
        except Exception as e:
            logger.error(f"Ошибка при получении заметок для пользователя {chat_id}: {e}")
            return ''  # Возвращаем пустую строку при ошибке


    def update_user_notes(self, chat_id: str, notes: str) -> bool:
        """
        Обновление заметок для пользователя.

        Args:
            chat_id: ID чата пользователя
            notes: Новый текст заметок

        Returns:
            True если заметки обновлены успешно, иначе False
        """
        try:
            # Очищаем заметки перед сохранением
            cleaned_notes = notes.strip() if notes else ''

            with self.get_connection() as conn:
                conn.execute('BEGIN IMMEDIATE')
                cursor = conn.cursor()
                now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                query = 'UPDATE users SET notes = ?, updated_at = ? WHERE chat_id = ?'
                cursor.execute(query, (cleaned_notes, now, chat_id))
                rows_affected = cursor.rowcount
                conn.commit()

                success = rows_affected > 0
                logger.info(
                    f"Обновление заметок пользователя {chat_id}: успех: {success}, затронуто строк: {rows_affected}, длина заметки: {len(cleaned_notes)}")

                if success:
                    # Очищаем кэш для этого пользователя
                    self._clear_cache(f"user_notes_{chat_id}")
                    # Очищаем и другие кэши, которые могли бы зависеть от данных пользователя, если бы notes там использовались
                    # self._clear_cache("all_users") # Не требуется, если notes не кэшируются в all_users

                return success
        except Exception as e:
            logger.error(f"Ошибка при обновлении заметок пользователя {chat_id}: {e}")
            try:
                conn.rollback()
            except Exception as rb_err:
                logger.error(f"Ошибка при откате транзакции: {rb_err}")
            return False

    def add_user(self, chat_id: str, status: str = 'Enable', notes: str = '') -> bool:
        """
        Добавление нового пользователя или обновление существующего.
        (Убрана обработка delivery_mode)

        Args:
            chat_id: ID чата пользователя
            status: Статус пользователя ('Enable' или 'Disable')
            notes: Текстовые заметки (опционально)

        Returns:
            True если пользователь добавлен/обновлен успешно, иначе False
        """
        if status not in ('Enable', 'Disable'):
            logger.warning(f"Неверный статус '{status}' при добавлении пользователя {chat_id}. Используется 'Enable'.")
            status = 'Enable'

        # Очищаем заметки перед использованием
        cleaned_notes = notes.strip() if notes else ''

        logger.info(
            f"Попытка добавления/обновления пользователя: {chat_id}, статус: {status}, заметки: {len(cleaned_notes)} символов")

        try:
            with self.get_connection() as conn:
                conn.execute('BEGIN IMMEDIATE')
                cursor = conn.cursor()
                now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

                # Обновлено: УБРАНО поле delivery_mode
                query = '''
                INSERT OR REPLACE INTO users (chat_id, status, notes, created_at, updated_at)
                VALUES (?, ?, ?, COALESCE((SELECT created_at FROM users WHERE chat_id = ?), ?), ?)
                '''

                cursor.execute(query, (chat_id, status, cleaned_notes, chat_id, now, now))
                conn.commit()

                rows_affected = cursor.rowcount
                logger.info(f"Добавлен/обновлен пользователь {chat_id}: затронуто строк: {rows_affected}")

                # Очищаем соответствующие кэши (убраны кэши для delivery_mode)
                self._clear_cache(f"user_registered_{chat_id}")
                self._clear_cache(f"user_status_{chat_id}")
                self._clear_cache(f"user_notes_{chat_id}")
                self._clear_cache("all_users")
                # Эти кэши тоже сбрасываем, т.к. статус пользователя мог измениться
                self._clear_cache("all_subjects")
                # Устаревшие кэши
                # self._clear_cache("all_client_data")
                # self._clear_cache("active_users_with_subjects")

                return rows_affected > 0
        except Exception as e:
            logger.error(f"Ошибка при добавлении/обновлении пользователя {chat_id}: {e}", exc_info=True)
            try:
                conn.rollback()
            except Exception as rb_err:
                logger.error(f"Ошибка при откате транзакции: {rb_err}")
            return False

    def add_subject(self, chat_id: str, subject: str) -> bool:
        """
        Добавление новой темы для пользователя.
        Режим доставки будет установлен в DEFAULT ('smart').

        Args:
            chat_id: ID чата пользователя
            subject: Тема для подписки

        Returns:
            True если тема добавлена успешно, иначе False
        """
        logger.info(
            f"Попытка добавления темы '{subject}' для пользователя {chat_id} (режим по умолчанию: {DEFAULT_DELIVERY_MODE})")
        try:
            with self.get_connection() as conn:
                conn.execute('BEGIN IMMEDIATE')
                cursor = conn.cursor()

                try:
                    # Проверяем, существует ли пользователь
                    cursor.execute('SELECT 1 FROM users WHERE chat_id = ?', (chat_id,))
                    if not cursor.fetchone():
                        # Если пользователя нет, добавляем его
                        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                        # Используем обновленный запрос без delivery_mode
                        cursor.execute(
                            'INSERT INTO users (chat_id, status, created_at, updated_at) VALUES (?, ?, ?, ?)',
                            (chat_id, 'Enable', now, now)
                        )
                        logger.info(f"Создан новый пользователь {chat_id} при добавлении темы")

                    # Запрос INSERT OR IGNORE вставит строку с DEFAULT значением для delivery_mode
                    query = '''
                    INSERT OR IGNORE INTO subjects (chat_id, subject)
                    VALUES (?, ?)
                    '''
                    cursor.execute(query, (chat_id, subject))
                    rows_affected = cursor.rowcount
                    conn.commit()

                    success = rows_affected > 0
                    if success:
                        logger.info(
                            f"Тема '{subject}' успешно добавлена для пользователя {chat_id} с режимом по умолчанию '{DEFAULT_DELIVERY_MODE}'")
                        # Очищаем кэши, связанные с темами
                        self._clear_cache(f"user_subjects_{chat_id}")
                        self._clear_cache("all_subjects")
                        # self._clear_cache("all_client_data") # Устаревший кэш
                    else:
                        # Проверяем, может тема уже существует
                        cursor.execute("SELECT delivery_mode FROM subjects WHERE chat_id = ? AND subject = ?",
                                       (chat_id, subject))
                        existing = cursor.fetchone()
                        if existing:
                            logger.warning(
                                f"Тема '{subject}' уже существует для пользователя {chat_id} (режим: {existing['delivery_mode']}). Добавление проигнорировано.")
                        else:
                            # Эта ветка маловероятна при IGNORE, но на всякий случай
                            logger.error(
                                f"Не удалось добавить тему '{subject}' для {chat_id}, но она и не существовала. Затронуто строк: {rows_affected}")

                    return success
                except Exception as e:
                    conn.rollback()
                    logger.error(f"Ошибка в транзакции при добавлении темы '{subject}' для пользователя {chat_id}: {e}",
                                 exc_info=True)
                    return False
        except Exception as e:
            logger.error(f"Ошибка при подключении к БД для добавления темы '{subject}' для {chat_id}: {e}",
                         exc_info=True)
            return False

    def add_multiple_subjects(self, chat_id: str, subjects: List[str]) -> int:
        """
        Добавление нескольких тем для пользователя.
        Режим доставки будет установлен в DEFAULT ('smart') для новых тем.

        Args:
            chat_id: ID чата пользователя
            subjects: Список тем для подписки

        Returns:
            Количество успешно добавленных *новых* тем
        """
        if not subjects:
            return 0

        added_count = 0
        logger.info(
            f"Попытка массового добавления {len(subjects)} тем для пользователя {chat_id} (режим по умолчанию: {DEFAULT_DELIVERY_MODE})")
        try:
            with self.get_connection() as conn:
                conn.execute('BEGIN IMMEDIATE')
                cursor = conn.cursor()

                try:
                    # Проверяем, существует ли пользователь
                    cursor.execute('SELECT 1 FROM users WHERE chat_id = ?', (chat_id,))
                    if not cursor.fetchone():
                        # Если пользователя нет, добавляем его
                        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                        # Используем обновленный запрос без delivery_mode
                        cursor.execute(
                            'INSERT INTO users (chat_id, status, created_at, updated_at) VALUES (?, ?, ?, ?)',
                            (chat_id, 'Enable', now, now)
                        )
                        logger.info(f"Создан новый пользователь {chat_id} при массовом добавлении тем")

                    # Используем executemany для эффективности, если возможно,
                    # но INSERT OR IGNORE с executemany не всегда возвращает корректный rowcount.
                    # Поэтому делаем в цикле для точного подсчета.
                    for subject in subjects:
                        cursor.execute(
                            'INSERT OR IGNORE INTO subjects (chat_id, subject) VALUES (?, ?)',
                            (chat_id, subject)
                        )
                        # Увеличиваем счетчик, только если строка была действительно вставлена
                        if cursor.rowcount > 0:
                            added_count += 1

                    conn.commit()
                    logger.info(
                        f"Массовое добавление тем для пользователя {chat_id}: успешно добавлено {added_count} новых тем из {len(subjects)}")

                    if added_count > 0:
                        # Очищаем кэши, связанные с темами
                        self._clear_cache(f"user_subjects_{chat_id}")
                        self._clear_cache("all_subjects")
                        # self._clear_cache("all_client_data") # Устаревший кэш

                    return added_count
                except Exception as e:
                    conn.rollback()
                    logger.error(f"Ошибка в транзакции при массовом добавлении тем для пользователя {chat_id}: {e}",
                                 exc_info=True)
                    return 0
        except Exception as e:
            logger.error(f"Ошибка при подключении к БД для массового добавления тем для {chat_id}: {e}", exc_info=True)
            return 0

    def delete_subject(self, chat_id: str, subject: str) -> bool:
        """
        Удаление темы у пользователя.

        Args:
            chat_id: ID чата пользователя
            subject: Тема для удаления

        Returns:
            True если тема удалена успешно, иначе False
        """
        try:
            with self.get_connection() as conn:
                # Явно начинаем транзакцию
                conn.execute('BEGIN IMMEDIATE')
                cursor = conn.cursor()

                query = '''
                DELETE FROM subjects
                WHERE chat_id = ? AND subject = ?
                '''

                cursor.execute(query, (chat_id, subject))
                conn.commit()  # Явный коммит

                success = cursor.rowcount > 0
                logger.info(
                    f"Удаление темы '{subject}' у пользователя {chat_id}: успех: {success}, затронуто строк: {cursor.rowcount}")

                if success:
                    # Очищаем соответствующие кэши
                    self._clear_cache(f"user_subjects_{chat_id}")
                    self._clear_cache("all_subjects")
                    self._clear_cache("all_client_data")

                return success
        except Exception as e:
            logger.error(f"Ошибка при удалении темы '{subject}' у пользователя {chat_id}: {e}")
            return False

    def delete_user(self, chat_id: str) -> bool:
        """
        Удаление пользователя и всех его тем.

        Args:
            chat_id: ID чата пользователя

        Returns:
            True если пользователь удален успешно, иначе False
        """
        try:
            with self.get_connection() as conn:
                # Начинаем транзакцию явно
                conn.execute('BEGIN IMMEDIATE')
                cursor = conn.cursor()

                try:
                    # Сначала удаляем все темы пользователя
                    cursor.execute('DELETE FROM subjects WHERE chat_id = ?', (chat_id,))
                    subjects_deleted = cursor.rowcount

                    # Затем удаляем самого пользователя
                    cursor.execute('DELETE FROM users WHERE chat_id = ?', (chat_id,))
                    user_deleted = cursor.rowcount

                    conn.commit()  # Явный коммит
                    logger.info(
                        f"Удаление пользователя {chat_id}: успех: {user_deleted > 0}, удалено тем: {subjects_deleted}")

                    if user_deleted > 0:
                        # Очищаем соответствующие кэши
                        self._clear_cache(f"user_registered_{chat_id}")
                        self._clear_cache(f"user_status_{chat_id}")
                        self._clear_cache(f"user_delivery_mode_{chat_id}")
                        self._clear_cache(f"user_subjects_{chat_id}")
                        self._clear_cache("all_subjects")
                        self._clear_cache("all_users")
                        self._clear_cache("all_client_data")
                        self._clear_cache("active_users_with_subjects")

                        logger.info(f"Пользователь {chat_id} успешно удален вместе с {subjects_deleted} темами")
                        return True
                    else:
                        logger.warning(f"Пользователь {chat_id} не найден")
                        return False
                except Exception as e:
                    conn.rollback()
                    logger.error(f"Ошибка в транзакции при удалении пользователя {chat_id}: {e}")
                    return False
        except Exception as e:
            logger.error(f"Ошибка при подключении к БД для удаления пользователя {chat_id}: {e}")
            return False

    def delete_all_user_subjects(self, chat_id: str) -> int:
        """
        Удаление всех тем пользователя.

        Args:
            chat_id: ID чата пользователя

        Returns:
            Количество удаленных тем
        """
        try:
            with self.get_connection() as conn:
                # Явно начинаем транзакцию
                conn.execute('BEGIN IMMEDIATE')
                cursor = conn.cursor()

                query = '''
                DELETE FROM subjects
                WHERE chat_id = ?
                '''

                cursor.execute(query, (chat_id,))
                conn.commit()  # Явный коммит

                rows_deleted = cursor.rowcount
                logger.info(f"Удаление всех тем пользователя {chat_id}: удалено {rows_deleted} тем")

                if rows_deleted > 0:
                    # Очищаем соответствующие кэши
                    self._clear_cache(f"user_subjects_{chat_id}")
                    self._clear_cache("all_subjects")
                    self._clear_cache("all_client_data")

                return rows_deleted
        except Exception as e:
            logger.error(f"Ошибка при удалении всех тем пользователя {chat_id}: {e}")
            return 0

    def get_all_users(self) -> Dict[str, bool]:
        """
        Получение всех пользователей и их статусов.

        Returns:
            Словарь {chat_id: is_enabled}
        """
        cache_key = "all_users"
        cached_data = self._get_from_cache(cache_key)
        if cached_data:
            return cached_data

        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()

                query = '''
                SELECT chat_id, status
                FROM users
                '''

                cursor.execute(query)
                result = cursor.fetchall()

                user_states = {row['chat_id']: row['status'] == 'Enable' for row in result}
                logger.debug(f"Получено {len(user_states)} пользователей")

                # Сохраняем в кэш
                self._set_in_cache(cache_key, user_states)
                return user_states
        except Exception as e:
            logger.error(f"Ошибка при получении всех пользователей: {e}")
            return {}



    def execute_optimized_query(self, query: str, params: Tuple = (), fetch_all: bool = True) -> List[Dict[str, Any]]:
        """
        Выполнение оптимизированного запроса с контролем ресурсов.

        Args:
            query: SQL-запрос
            params: Параметры для запроса
            fetch_all: Получить все строки (True) или только первую (False)

        Returns:
            Результат запроса в виде списка словарей
        """
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(query, params)

                if fetch_all:
                    rows = cursor.fetchall()
                    return [{k: row[k] for k in row.keys()} for row in rows]
                else:
                    row = cursor.fetchone()
                    return [{k: row[k] for k in row.keys()}] if row else []
        except Exception as e:
            logger.error(f"Ошибка при выполнении оптимизированного запроса: {e}")
            return []