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

# Настройка логирования
logger = get_logger("db_manager")

# Константы для повторных попыток соединения
MAX_RETRY_ATTEMPTS = 3
RETRY_DELAY = 0.5  # в секундах

# Константы для маски
DELIVERY_MODE_TEXT = 'text'
DELIVERY_MODE_HTML = 'html'
DELIVERY_MODE_SMART = 'smart'
ALLOWED_DELIVERY_MODES = {DELIVERY_MODE_TEXT, DELIVERY_MODE_HTML, DELIVERY_MODE_SMART}
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
        """Инициализация базы данных и создание таблиц, если они не существуют."""
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()

                cursor.execute(f'''
                CREATE TABLE IF NOT EXISTS users (
                    chat_id TEXT PRIMARY KEY,
                    status TEXT DEFAULT 'Enable' NOT NULL CHECK (status IN ('Enable', 'Disable')),
                    delivery_mode TEXT DEFAULT '{DEFAULT_DELIVERY_MODE}' NOT NULL CHECK (delivery_mode IN ('{DELIVERY_MODE_TEXT}', '{DELIVERY_MODE_HTML}', '{DELIVERY_MODE_SMART}')),
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                ''')
                cursor.execute('CREATE INDEX IF NOT EXISTS idx_users_status ON users (status)')
                cursor.execute(
                    'CREATE INDEX IF NOT EXISTS idx_users_delivery_mode ON users (delivery_mode)')

                # Создание таблицы тем писем с композитным индексом для ускорения поиска
                cursor.execute('''
                CREATE TABLE IF NOT EXISTS subjects (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id TEXT NOT NULL,
                    subject TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (chat_id) REFERENCES users (chat_id) ON DELETE CASCADE,
                    UNIQUE(chat_id, subject)
                )
                ''')

                # Создание индексов для ускорения поиска
                cursor.execute('CREATE INDEX IF NOT EXISTS idx_subjects_chat_id ON subjects (chat_id)')
                cursor.execute('CREATE INDEX IF NOT EXISTS idx_subjects_subject ON subjects (subject COLLATE NOCASE)')

                # Добавляем индекс для ускорения JOIN-запросов
                cursor.execute('CREATE INDEX IF NOT EXISTS idx_subjects_chat_id_subject ON subjects (chat_id, subject)')

                conn.commit()
                logger.info("База данных инициализирована успешно (проверена структура таблиц)")


                cursor.execute("PRAGMA table_info(users)")
                columns = [col['name'] for col in cursor.fetchall()]
                if 'delivery_mode' not in columns:
                    logger.warning("Столбец 'delivery_mode' отсутствует в таблице users. Добавляем...")
                    try:
                        conn.execute('BEGIN IMMEDIATE')
                        cursor.execute(f'''
                            ALTER TABLE users
                            ADD COLUMN delivery_mode TEXT DEFAULT '{DEFAULT_DELIVERY_MODE}' NOT NULL CHECK (delivery_mode IN ('{DELIVERY_MODE_TEXT}', '{DELIVERY_MODE_HTML}', '{DELIVERY_MODE_SMART}'))
                        ''')
                        conn.commit()
                        logger.info("Столбец 'delivery_mode' успешно добавлен в таблицу users.")
                        self._clear_cache()
                        self.release_connection()
                    except Exception as alter_err:
                        conn.rollback()
                        logger.error(
                            f"Не удалось добавить столбец 'delivery_mode': {alter_err}. Возможно, потребуется ручное вмешательство.")
                        raise RuntimeError(f"Не удалось обновить схему БД: {alter_err}") from alter_err

        except Exception as e:
            logger.error(f"Ошибка при инициализации базы данных: {e}")
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
        # Import at the top of the file
        from src.utils.cache_manager import is_cache_valid

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

    def get_user_delivery_mode(self, chat_id: str) -> str:
        """
        Получение режима доставки длинных сообщений для пользователя.

        Args:
            chat_id: ID чата пользователя

        Returns:
            Режим доставки ('text', 'html', 'smart') или значение по умолчанию.
        """
        cache_key = f"user_delivery_mode_{chat_id}"
        cached_data = self._get_from_cache(cache_key)
        if cached_data is not None:
            return cached_data

        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                query = '''
                SELECT delivery_mode
                FROM users
                WHERE chat_id = ?
                '''
                cursor.execute(query, (chat_id,))
                result = cursor.fetchone()

                mode = result['delivery_mode'] if result and result['delivery_mode'] else DEFAULT_DELIVERY_MODE
                logger.debug(f"Режим доставки для пользователя {chat_id}: {mode}")

                # Сохраняем в кэш
                self._set_in_cache(cache_key, mode)
                return mode
        except Exception as e:
            logger.error(f"Ошибка при получении режима доставки для пользователя {chat_id}: {e}")
            return DEFAULT_DELIVERY_MODE  # Возвращаем значение по умолчанию при ошибке

    def update_user_delivery_mode(self, chat_id: str, mode: str) -> bool:
        """
        Обновление режима доставки для пользователя.

        Args:
            chat_id: ID чата пользователя
            mode: Новый режим доставки ('text', 'html', 'smart')

        Returns:
            True если режим обновлен успешно, иначе False
        """
        if mode not in ALLOWED_DELIVERY_MODES:
            logger.warning(f"Попытка установить неверный режим доставки '{mode}' для пользователя {chat_id}")
            return False

        try:
            with self.get_connection() as conn:
                # Явно начинаем транзакцию
                conn.execute('BEGIN IMMEDIATE')
                cursor = conn.cursor()

                now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                query = '''
                UPDATE users
                SET delivery_mode = ?, updated_at = ?
                WHERE chat_id = ?
                '''
                cursor.execute(query, (mode, now, chat_id))
                rows_affected = cursor.rowcount
                conn.commit()  # Явный коммит

                success = rows_affected > 0
                logger.info(
                    f"Обновление режима доставки пользователя {chat_id}: {mode}, успех: {success}, затронуто строк: {rows_affected}"
                )

                if success:
                    # Очищаем кэш для этого пользователя
                    self._clear_cache(f"user_delivery_mode_{chat_id}")
                    # Можно также очистить кэш all_users, если он содержит delivery_mode
                    self._clear_cache("all_users")  # На всякий случай

                return success
        except Exception as e:
            logger.error(f"Ошибка при обновлении режима доставки пользователя {chat_id}: {e}")
            try:
                # Попытка отката транзакции в случае ошибки
                conn.rollback()
            except Exception as rb_err:
                logger.error(f"Ошибка при откате транзакции: {rb_err}")
            return False

    def refresh_data(self):
        """Принудительное обновление всех кэшированных данных и сброс соединений."""
        # Import at the top of the file
        from src.utils.cache_manager import invalidate_caches

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

    def get_all_subjects(self) -> Dict[str, Dict[str, Any]]:
        """
        Получение всех тем и связанных с ними chat_id и статусов.

        Returns:
            Словарь тем с информацией о связанных chat_id и статусах
        """
        cache_key = "all_subjects"
        cached_data = self._get_from_cache(cache_key)
        if cached_data:
            return cached_data

        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()

                query = '''
                    SELECT s.subject, s.chat_id, u.status
                    FROM subjects s
                    JOIN users u ON s.chat_id = u.chat_id
                    '''

                cursor.execute(query)
                result = cursor.fetchall()

                subject_data = {}
                for row in result:
                    subject = row['subject']
                    chat_id = row['chat_id']
                    status = row['status']
                    is_enabled = status == 'Enable'

                    # Если тема еще не в словаре, создаем пустой список
                    if subject not in subject_data:
                        subject_data[subject] = []

                    # Добавляем данные в список для данной темы
                    subject_data[subject].append({"chat_id": chat_id, "enabled": is_enabled})

                # Сохраняем в кэш
                self._set_in_cache(cache_key, subject_data)
                return subject_data
        except Exception as e:
            logger.error(f"Ошибка при получении всех тем: {e}")
            return {}

    def get_user_subjects(self, chat_id: str) -> List[str]:
        """
        Получение всех тем, на которые подписан пользователь.

        Args:
            chat_id: ID чата пользователя

        Returns:
            Список тем пользователя
        """
        cache_key = f"user_subjects_{chat_id}"
        cached_data = self._get_from_cache(cache_key)
        if cached_data:
            return cached_data

        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()

                query = '''
                SELECT s.subject
                FROM subjects s
                WHERE s.chat_id = ?
                '''

                cursor.execute(query, (chat_id,))
                result = cursor.fetchall()

                subjects = [row['subject'] for row in result]
                logger.debug(f"Получено {len(subjects)} тем для пользователя {chat_id}")

                # Сохраняем в кэш
                self._set_in_cache(cache_key, subjects)
                return subjects
        except Exception as e:
            logger.error(f"Ошибка при получении тем пользователя {chat_id}: {e}")
            return []

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

    def add_user(self, chat_id: str, status: str = 'Enable', delivery_mode: str = DEFAULT_DELIVERY_MODE) -> bool:
        """
        Добавление нового пользователя или обновление существующего.

        Args:
            chat_id: ID чата пользователя
            status: Статус пользователя ('Enable' или 'Disable')
            delivery_mode: Режим доставки ('text', 'html', 'smart')

        Returns:
            True если пользователь добавлен/обновлен успешно, иначе False
        """
        if status not in ('Enable', 'Disable'):
            logger.warning(f"Неверный статус '{status}' при добавлении пользователя {chat_id}. Используется 'Enable'.")
            status = 'Enable'
        if delivery_mode not in ALLOWED_DELIVERY_MODES:
            logger.warning(
                f"Неверный режим доставки '{delivery_mode}' при добавлении пользователя {chat_id}. Используется '{DEFAULT_DELIVERY_MODE}'.")
            delivery_mode = DEFAULT_DELIVERY_MODE

        logger.info(f"Попытка добавления/обновления пользователя: {chat_id}, статус: {status}, режим: {delivery_mode}")

        try:
            with self.get_connection() as conn:
                # Явно начинаем транзакцию
                conn.execute('BEGIN IMMEDIATE')
                cursor = conn.cursor()

                now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

                # Используем INSERT OR REPLACE для добавления или обновления
                query = '''
                INSERT OR REPLACE INTO users (chat_id, status, delivery_mode, created_at, updated_at)
                VALUES (?, ?, ?, COALESCE((SELECT created_at FROM users WHERE chat_id = ?), ?), ?)
                '''

                cursor.execute(query, (chat_id, status, delivery_mode, chat_id, now, now))
                conn.commit()  # Явный коммит

                rows_affected = cursor.rowcount
                logger.info(f"Добавлен/обновлен пользователь {chat_id}: затронуто строк: {rows_affected}")

                # Очищаем соответствующие кэши
                self._clear_cache(f"user_registered_{chat_id}")
                self._clear_cache(f"user_status_{chat_id}")
                self._clear_cache(f"user_delivery_mode_{chat_id}")
                self._clear_cache("all_users")
                # Очищаем и другие кэши, которые могли зависеть от данных пользователя
                self._clear_cache("all_subjects")
                self._clear_cache("all_client_data")
                self._clear_cache("active_users_with_subjects")

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

        Args:
            chat_id: ID чата пользователя
            subject: Тема для подписки

        Returns:
            True если тема добавлена успешно, иначе False
        """
        try:
            with self.get_connection() as conn:
                # Начинаем транзакцию явно
                conn.execute('BEGIN IMMEDIATE')
                cursor = conn.cursor()

                try:
                    # Проверяем, существует ли пользователь
                    cursor.execute('SELECT 1 FROM users WHERE chat_id = ?', (chat_id,))
                    if not cursor.fetchone():
                        # Если пользователя нет, добавляем его
                        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                        cursor.execute(
                            'INSERT INTO users (chat_id, status, created_at, updated_at) VALUES (?, ?, ?, ?)',
                            (chat_id, 'Enable', now, now)
                        )
                        logger.info(f"Создан новый пользователь {chat_id} при добавлении темы")

                    query = '''
                    INSERT OR IGNORE INTO subjects (chat_id, subject)
                    VALUES (?, ?)
                    '''

                    cursor.execute(query, (chat_id, subject))
                    conn.commit()  # Явный коммит

                    success = cursor.rowcount > 0
                    logger.info(
                        f"Добавление темы '{subject}' для пользователя {chat_id}: успех: {success}, затронуто строк: {cursor.rowcount}")

                    if success:
                        # Очищаем соответствующие кэши
                        self._clear_cache(f"user_subjects_{chat_id}")
                        self._clear_cache("all_subjects")
                        self._clear_cache("all_client_data")

                    return success
                except Exception as e:
                    conn.rollback()
                    logger.error(f"Ошибка в транзакции при добавлении темы '{subject}' для пользователя {chat_id}: {e}")
                    return False
        except Exception as e:
            logger.error(f"Ошибка при добавлении темы '{subject}' для пользователя {chat_id}: {e}")
            return False

    def add_multiple_subjects(self, chat_id: str, subjects: List[str]) -> int:
        """
        Добавление нескольких тем для пользователя.

        Args:
            chat_id: ID чата пользователя
            subjects: Список тем для подписки

        Returns:
            Количество успешно добавленных тем
        """
        if not subjects:
            return 0

        try:
            with self.get_connection() as conn:
                # Начинаем транзакцию явно
                conn.execute('BEGIN IMMEDIATE')
                cursor = conn.cursor()

                try:
                    # Проверяем, существует ли пользователь
                    cursor.execute('SELECT 1 FROM users WHERE chat_id = ?', (chat_id,))
                    if not cursor.fetchone():
                        # Если пользователя нет, добавляем его
                        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                        cursor.execute(
                            'INSERT INTO users (chat_id, status, created_at, updated_at) VALUES (?, ?, ?, ?)',
                            (chat_id, 'Enable', now, now)
                        )
                        logger.info(f"Создан новый пользователь {chat_id} при массовом добавлении тем")

                    # Подготавливаем пакетную вставку
                    count = 0
                    for subject in subjects:
                        cursor.execute(
                            'INSERT OR IGNORE INTO subjects (chat_id, subject) VALUES (?, ?)',
                            (chat_id, subject)
                        )
                        count += cursor.rowcount

                    conn.commit()  # Явный коммит
                    logger.info(
                        f"Массовое добавление тем для пользователя {chat_id}: добавлено {count} из {len(subjects)}")

                    if count > 0:
                        # Очищаем соответствующие кэши
                        self._clear_cache(f"user_subjects_{chat_id}")
                        self._clear_cache("all_subjects")
                        self._clear_cache("all_client_data")

                    return count
                except Exception as e:
                    conn.rollback()
                    logger.error(f"Ошибка в транзакции при массовом добавлении тем для пользователя {chat_id}: {e}")
                    return 0
        except Exception as e:
            logger.error(f"Ошибка при массовом добавлении тем для пользователя {chat_id}: {e}")
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

    def get_all_client_data(self) -> Dict[str, List[str]]:
        """
        Получение всех данных о клиентах.

        Returns:
            Словарь {chat_id: [список тем]}
        """
        cache_key = "all_client_data"
        cached_data = self._get_from_cache(cache_key)
        if cached_data:
            return cached_data

        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()

                query = '''
                SELECT u.chat_id, s.subject
                FROM users u
                JOIN subjects s ON u.chat_id = s.chat_id
                '''

                cursor.execute(query)
                result = cursor.fetchall()

                client_data = {}
                for row in result:
                    chat_id = row['chat_id']
                    subject = row['subject']

                    if chat_id not in client_data:
                        client_data[chat_id] = []

                    client_data[chat_id].append(subject)

                # Сохраняем в кэш
                self._set_in_cache(cache_key, client_data)
                logger.debug(
                    f"Получены данные для {len(client_data)} клиентов, всего {sum(len(subjects) for subjects in client_data.values())} тем")
                return client_data
        except Exception as e:
            logger.error(f"Ошибка при получении всех данных о клиентах: {e}")
            return {}

    def get_active_users_with_subjects(self) -> Dict[str, List[str]]:
        """
        Получение только активных пользователей и их тем.
        Оптимизированный метод для обработки писем.

        Returns:
            Словарь {chat_id: [список тем]} только для активных пользователей
        """
        cache_key = "active_users_with_subjects"
        cached_data = self._get_from_cache(cache_key)
        if cached_data:
            return cached_data

        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()

                query = '''
                SELECT u.chat_id, s.subject
                FROM users u
                JOIN subjects s ON u.chat_id = s.chat_id
                WHERE u.status = 'Enable'
                '''

                cursor.execute(query)
                result = cursor.fetchall()

                active_users = {}
                for row in result:
                    chat_id = row['chat_id']
                    subject = row['subject']

                    if chat_id not in active_users:
                        active_users[chat_id] = []

                    active_users[chat_id].append(subject)

                # Сохраняем в кэш с меньшим временем жизни
                self._set_in_cache(cache_key, active_users)
                logger.debug(f"Получены данные для {len(active_users)} активных пользователей")

                return active_users
        except Exception as e:
            logger.error(f"Ошибка при получении активных пользователей с темами: {e}")
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