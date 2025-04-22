import re
import time
import os
import secrets
import gc
from threading import Lock
from flask import Flask, render_template, request, redirect, url_for, flash, session, jsonify, abort
from flask_wtf.csrf import CSRFProtect
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from werkzeug.middleware.proxy_fix import ProxyFix
from functools import wraps
from datetime import datetime, timedelta
import math
import subprocess
from pathlib import Path
from typing import List, Optional, Callable, Dict, Any
import concurrent.futures
import signal
import sys
import psutil

from src.config import settings
from src.utils.logger import get_logger
from src.core.bot_status import get_bot_status, start_bot, stop_bot
from src.db.tools import execute_query, get_table_list, get_table_info, get_common_queries, optimize_database, \
    close_all_connections, clear_query_cache
from src.db.manager import DatabaseManager
from src.utils.cache_manager import invalidate_caches, is_cache_valid

from src.web.auth import init_admin_users, hash_password, verify_password, log_activity

# Настройка логирования
logger = get_logger("web_admin")

# Отключаем общий кэш SQLite для предотвращения блокировок
os.environ["SQLITE_ENABLE_SHARED_CACHE"] = "0"

# Защита от брутфорса с несколькими стадиями блокировок
MAX_ATTEMPTS = 5
LOCKOUT_STAGES = [
    15 * 60,  # 15 минут в секундах
    60 * 60,  # 1 час в секундах
    24 * 60 * 60  # 24 часа в секундах
]

# Глобальная блокировка доступа
admin_login_lock = Lock()

# Подключение менеджера базы данных
db_manager = DatabaseManager()
logger.info(f"Используемая БД в директории: {settings.DATABASE_PATH}")
db_dir = Path(settings.DATABASE_PATH).parent
if not db_dir.exists():
    logger.info(f"Creating database directory: {db_dir}")
    db_dir.mkdir(parents=True, exist_ok=True)

# Проверка существования директорий для шаблонов и статических файлов
templates_dir = settings.TEMPLATES_DIR
static_dir = settings.STATIC_DIR

if not templates_dir.exists():
    templates_dir.mkdir(parents=True, exist_ok=True)
    logger.info(f"Создана директория для шаблонов: {templates_dir}")

if not static_dir.exists():
    static_dir.mkdir(parents=True, exist_ok=True)
    static_dir.joinpath('css').mkdir(exist_ok=True)
    static_dir.joinpath('img').mkdir(exist_ok=True)
    logger.info(f"Создана директория для статических файлов: {static_dir}")

# Инициализация Flask-приложения
app = Flask(__name__,
            template_folder=str(templates_dir),
            static_folder=str(static_dir))

# Для правильной работы за прокси-сервером
app.wsgi_app = ProxyFix(app.wsgi_app)

# Генерация сильного секретного ключа, если он не установлен
if not settings.SECRET_KEY:
    logger.warning("SECRET_KEY не установлен, генерируем случайный ключ")
    app.secret_key = secrets.token_hex(32)
else:
    app.secret_key = settings.SECRET_KEY

# Настройка сессий
app.config['SESSION_TYPE'] = 'filesystem'
app.config['SESSION_FILE_DIR'] = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'sessions')
app.config['SESSION_PERMANENT'] = True
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(hours=12)
app.config['SESSION_COOKIE_SECURE'] = True  # Для HTTPS
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'

# Настройка кэширования
app.config['SEND_FILE_MAX_AGE_DEFAULT'] = timedelta(hours=1)

# Настройки для SQLite
app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {'isolation_level': None}

# Настройка CSRF защиты
csrf = CSRFProtect(app)

# Настройка ограничения скорости запросов
limiter = Limiter(
    get_remote_address,
    app=app,
    default_limits=["200 per day", "50 per hour"],
    storage_uri="memory://",
    strategy="fixed-window-elastic-expiry"
)


# Закрытие соединений после каждого запроса
@app.teardown_request
def close_db_connection(exception=None):
    if db_manager:
        db_manager.release_connection()


# Константа для количества элементов на странице
ITEMS_PER_PAGE = 15

# Настройки для администратора
ADMIN_USERNAME = settings.ADMIN_USERNAME
ADMIN_PASSWORD = settings.ADMIN_PASSWORD

# Структура для отслеживания попыток входа
ip_login_attempts = {}
username_login_attempts = {}
global_lockout_until = 0
global_lockout_trigger_threshold = 10  # Количество заблокированных IP для глобальной блокировки

# Пул потоков для параллельного выполнения длительных операций
executor = concurrent.futures.ThreadPoolExecutor(max_workers=2)

# Кэш для часто используемых данных
cache = {}
cache_timestamps = {}
CACHE_TTL = 300  # 5 минут в секундах
cache_lock = Lock()


def initialize_database() -> None:
    """Инициализация базы данных и таблиц для аутентификации."""
    # Инициализировать таблицы пользователей админки
    if db_manager:
        init_admin_users(db_manager)
    else:
        logger.error("db_manager не инициализирован при вызове initialize_database")


def invalidate_all_caches():
    """
    Полное обновление всех типов кэшей для обеспечения
    согласованности данных во всем приложении.
    """
    logger.info("Выполняется полная инвалидация всех кэшей")

    # Add this line to trigger cross-process cache invalidation
    invalidate_caches()

    # Очищаем кэш приложения
    clear_cache()

    # Очищаем кэш запросов из tools.py
    clear_query_cache()

    # Сбрасываем кэш и соединения менеджера базы данных
    db_manager.refresh_data()

    # Принудительно сбрасываем внутреннее состояние SQLite
    try:
        with db_manager.get_connection() as conn:
            # Форсируем чекпойнт WAL для обеспечения видимости изменений для всех соединений
            conn.execute("PRAGMA wal_checkpoint(RESTART)")
            # Принудительная оптимизация для сброса внутреннего кэша SQLite
            conn.execute("PRAGMA optimize")
    except Exception as e:
        logger.error(f"Ошибка при инвалидации кэша SQLite: {e}")

    # Явно очищаем кэши, специфичные для SQL-консоли
    with cache_lock:
        keys_to_remove = []
        for key in cache:
            if key == 'table_list' or key.startswith('table_info_'):
                keys_to_remove.append(key)

        for key in keys_to_remove:
            cache.pop(key, None)
            cache_timestamps.pop(key, None)
            logger.debug(f"Очищен ключ кэша SQL-консоли: {key}")


def get_cached_data(key, refresh_func, ttl=CACHE_TTL):
    """
    Получает данные из кэша или обновляет их при необходимости.

    Args:
        key: Ключ кэша
        refresh_func: Функция для получения свежих данных
        ttl: Время жизни кэша в секундах

    Returns:
        Данные из кэша или от refresh_func
    """
    with cache_lock:
        current_time = time.time()
        if key in cache and (current_time - cache_timestamps.get(key, 0)) < ttl and is_cache_valid(
                cache_timestamps.get(key, 0)):
            return cache[key]

    # Если данных нет в кэше или они устарели, получаем новые
    fresh_data = refresh_func()

    with cache_lock:
        cache[key] = fresh_data
        cache_timestamps[key] = time.time()

    return fresh_data


def clear_cache(key=None):
    """
    Очищает весь кэш или конкретный ключ.

    Args:
        key: Конкретный ключ для очистки, None для очистки всего кэша
    """
    with cache_lock:
        if key:
            cache.pop(key, None)
            cache_timestamps.pop(key, None)
            logger.debug(f"Очищен кэш для ключа {key}")
        else:
            cache.clear()
            cache_timestamps.clear()
            logger.debug("Очищен весь кэш админки")


# Новые декораторы для проверки прав доступа
def login_required(f: Callable) -> Callable:
    """
    Декоратор для проверки авторизации пользователя.

    Args:
        f: Декорируемая функция

    Returns:
        Функция-обертка
    """

    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            flash('Пожалуйста, войдите для доступа к этой странице', 'danger')
            return redirect(url_for('login', next=request.url))

        # Проверка времени последней активности для автоматического выхода
        if 'last_activity' in session:
            last_activity = session.get('last_activity')
            if time.time() - last_activity > app.config['PERMANENT_SESSION_LIFETIME'].total_seconds():
                session.clear()
                flash('Сессия истекла. Пожалуйста, войдите снова.', 'warning')
                return redirect(url_for('login'))

        # Обновляем время последней активности
        session['last_activity'] = time.time()

        return f(*args, **kwargs)

    return decorated_function


def admin_required(f: Callable) -> Callable:
    """
    Декоратор для проверки прав администратора.

    Args:
        f: Декорируемая функция

    Returns:
        Функция-обертка
    """

    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_role' not in session or session['user_role'] != 'admin':
            flash('У вас недостаточно прав для доступа к этой странице', 'danger')
            return redirect(url_for('index'))
        return f(*args, **kwargs)

    return decorated_function


def operator_required(f: Callable) -> Callable:
    """
    Декоратор для проверки прав оператора или админа.

    Args:
        f: Декорируемая функция

    Returns:
        Функция-обертка
    """

    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_role' not in session or session['user_role'] not in ['operator', 'admin']:
            flash('У вас недостаточно прав для выполнения этого действия', 'danger')
            return redirect(url_for('index'))
        return f(*args, **kwargs)

    return decorated_function


def is_allowed_sql(query: str, user_role: str) -> bool:
    """
    Проверяет разрешен ли SQL-запрос для данной роли.

    Args:
        query: SQL-запрос
        user_role: Роль пользователя

    Returns:
        True если запрос разрешен, иначе False
    """
    query_upper = query.strip().upper()

    # Для читателя запрещены все запросы
    if user_role == 'viewer':
        return False

    # Для оператора разрешены только SELECT, PRAGMA и EXPLAIN
    if user_role == 'operator':
        return (query_upper.startswith('SELECT') or
                query_upper.startswith('PRAGMA') or
                query_upper.startswith('EXPLAIN'))

    # Для админа разрешены все запросы
    return True


def check_duplicate_subjects(subjects: List[str], exclude_chat_id: Optional[str] = None) -> List[str]:
    """
    Проверяет наличие дубликатов тем у других пользователей.

    Args:
        subjects: Список тем для проверки
        exclude_chat_id: ID чата пользователя, которого нужно исключить из проверки

    Returns:
        Список тем, которые уже существуют у других пользователей
    """
    if not subjects:
        return []

    duplicate_subjects = []

    # Получаем данные из кэша или БД
    def get_client_data():
        return db_manager.get_all_client_data()

    client_data = get_cached_data('all_client_data', get_client_data)

    for chat_id, user_subjects in client_data.items():
        if exclude_chat_id and chat_id == exclude_chat_id:
            continue
        for subject in subjects:
            if subject in user_subjects and subject not in duplicate_subjects:
                duplicate_subjects.append(subject)

    return duplicate_subjects


# Обработчики сигналов для корректного завершения работы
def signal_handler(sig, frame):
    """Корректное завершение работы при получении сигнала."""
    logger.info(f"Получен сигнал {sig}. Завершаем работу...")

    # Освобождаем ресурсы
    executor.shutdown(wait=False)
    close_all_connections()

    # Если используется менеджер базы данных, закрываем его
    if db_manager:
        db_manager.shutdown()

    # Принудительное завершение для предотвращения зависания
    os._exit(0)


# Регистрируем обработчики сигналов
signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)


@app.before_request
def before_request():
    """Выполняется перед каждым запросом."""
    # Проверяем глобальную блокировку
    if time.time() < global_lockout_until and request.endpoint != 'static':
        abort(403, description="Доступ временно ограничен. Попробуйте позже.")


@app.after_request
def after_request(response):
    """Добавляет заголовки безопасности к каждому ответу."""
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Frame-Options'] = 'SAMEORIGIN'
    response.headers['X-XSS-Protection'] = '1; mode=block'
    response.headers['Strict-Transport-Security'] = 'max-age=31536000; includeSubDomains'
    response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    response.headers['Pragma'] = 'no-cache'
    response.headers['Expires'] = '0'
    return response


@app.route('/robots.txt')
def robots():
    """Запрещаем индексацию всех страниц."""
    return "User-agent: *\nDisallow: /"


@app.route('/sql-console', methods=['GET', 'POST'])
@login_required
@limiter.limit("100 per minute")
def sql_console():
    """Консоль для выполнения SQL-запросов."""
    # Проверка прав доступа
    user_role = session.get('user_role', 'viewer')

    if user_role == 'viewer':
        flash('У вас нет прав для доступа к SQL-консоли', 'danger')
        return redirect(url_for('index'))

    # При прямом доступе к SQL-консоли сначала инвалидируем кэши
    if request.method == 'GET' and not request.args.get('query'):
        invalidate_all_caches()

    db_path = str(Path(settings.DATABASE_PATH))

    # Получаем данные из кэша или обновляем
    def get_tables():
        return get_table_list(db_path)

    tables = get_cached_data('table_list', get_tables, ttl=3600)  # Кэшируем на час
    common_queries = get_common_queries()

    # Получаем параметры из формы и URL
    query = request.form.get('query', request.args.get('query', '').strip())
    page = request.args.get('page', 1, type=int)
    search_query = request.args.get('search', '')
    per_page = request.args.get('per_page', ITEMS_PER_PAGE, type=int)

    results = []
    headers = []
    error = None
    success = False

    if query:
        # Проверяем права на выполнение запроса
        if not is_allowed_sql(query, user_role):
            error = "У вас нет прав для выполнения этого типа запроса"
            flash(error, 'danger')
            return render_template('sql_console.html',
                                   query=query,
                                   results=[],
                                   headers=[],
                                   tables=tables,
                                   table_info={},
                                   common_queries=common_queries,
                                   success=False,
                                   error=error,
                                   page=1,
                                   total_pages=0,
                                   total_results=0,
                                   search_query='',
                                   per_page=per_page,
                                   user_role=user_role)

        try:
            # Выполнение запроса без параметров
            success, results, headers, error = execute_query(db_path, query)

            # Применяем поиск, если он указан
            if search_query and success and results:
                filtered_results = []
                for row in results:
                    # Поиск по всем полям
                    for header in headers:
                        if str(row[header]).lower().find(search_query.lower()) != -1:
                            filtered_results.append(row)
                            break
                results = filtered_results

            # Запись в лог
            if success:
                logger.info(f"SQL запрос выполнен успешно: {query[:100]}")
                # Логирование действия пользователя
                if 'user_id' in session:
                    log_activity(db_manager, session['user_id'], f"sql_query",
                                 request.remote_addr, query[:100])
            else:
                logger.warning(f"Ошибка выполнения SQL запроса: {error}")
        except Exception as e:
            success = False
            error = f"Ошибка выполнения: {str(e)}"
            logger.error(f"Ошибка при выполнении SQL запроса: {e}")

    # Получение информации о таблицах из кэша или обновляем
    table_info = {}
    for table in tables:
        def get_table_info_func(table_name=table):
            return get_table_info(db_path, table_name)

        table_info[table] = get_cached_data(f'table_info_{table}', get_table_info_func, ttl=3600)  # Кэшируем на час

    # Пагинация результатов
    total_results = len(results)
    total_pages = math.ceil(total_results / per_page) if total_results > 0 else 0

    start_idx = (page - 1) * per_page
    end_idx = start_idx + per_page
    paginated_results = results[start_idx:end_idx] if results else []

    # Проверяем, является ли запрос AJAX
    is_ajax = request.headers.get('X-Requested-With') == 'XMLHttpRequest'

    if is_ajax and request.method == 'POST':
        # Для AJAX запросов возвращаем только часть с результатами
        return render_template('sql_console.html',
                               query=query,
                               results=paginated_results,
                               headers=headers,
                               tables=tables,
                               table_info=table_info,
                               common_queries=common_queries,
                               success=success,
                               error=error,
                               page=page,
                               total_pages=total_pages,
                               total_results=total_results,
                               search_query=search_query,
                               per_page=per_page,
                               user_role=user_role)

    # Для обычных запросов возвращаем полную страницу
    return render_template('sql_console.html',
                           query=query,
                           results=paginated_results,
                           headers=headers,
                           tables=tables,
                           table_info=table_info,
                           common_queries=common_queries,
                           success=success,
                           error=error,
                           page=page,
                           total_pages=total_pages,
                           total_results=total_results,
                           search_query=search_query,
                           per_page=per_page,
                           user_role=user_role)


@app.route('/')
@login_required
def index():
    """Главная страница с информацией о системе."""
    try:
        # Получаем данные из кэша или обновляем
        def get_stats():
            # Получение информации о пользователях и темах
            user_states = db_manager.get_all_users()
            client_data = db_manager.get_all_client_data()

            # Подсчет статистики
            total_users = len(user_states)
            active_users = sum(1 for status in user_states.values() if status)
            total_subjects = sum(len(subjects) for subjects in client_data.values())

            return {
                'total_users': total_users,
                'active_users': active_users,
                'total_subjects': total_subjects
            }

        stats = get_cached_data('stats', get_stats, ttl=60)  # Кэшируем на минуту

        # Получаем актуальный статус бота
        bot_status = get_bot_status(bypass_cache=True)

        # Получаем роль пользователя для отображения доступных действий
        user_role = session.get('user_role', 'viewer')

        return render_template('index.html',
                               total_users=stats['total_users'],
                               active_users=stats['active_users'],
                               total_subjects=stats['total_subjects'],
                               bot_status=bot_status,
                               timestamp=datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                               user_role=user_role)
    except Exception as e:
        logger.error(f"Ошибка при загрузке главной страницы: {e}", exc_info=True)
        flash(f"Произошла ошибка: {e}", "danger")
        return render_template('index.html', error=str(e), user_role=session.get('user_role', 'viewer'))


@app.route('/users')
@login_required
def users():
    """Страница со списком пользователей."""
    try:
        # Получаем параметры пагинации и поиска
        page = request.args.get('page', 1, type=int)
        search_query = request.args.get('search', '')
        per_page = ITEMS_PER_PAGE

        # Получаем данные из кэша или обновляем
        def get_users_data():
            user_states = db_manager.get_all_users()
            client_data = db_manager.get_all_client_data()

            users_data = []
            for chat_id, is_enabled in user_states.items():
                subjects = client_data.get(chat_id, [])
                users_data.append({
                    'chat_id': chat_id,
                    'status': 'Активен' if is_enabled else 'Отключен',
                    'is_enabled': is_enabled,
                    'subject_count': len(subjects)
                })

            # Сортировка пользователей (сначала активные)
            users_data.sort(key=lambda x: (not x['is_enabled'], x['chat_id']))

            return users_data

        users_data = get_cached_data('users_data', get_users_data, ttl=60)  # Кэшируем на минуту

        # Фильтрация по поисковому запросу
        if search_query:
            filtered_users = []
            for user in users_data:
                # Поиск по chat_id или статусу
                if str(user['chat_id']).find(search_query) != -1 or user['status'].lower().find(
                        search_query.lower()) != -1:
                    filtered_users.append(user)
            users_data = filtered_users

        # Пагинация
        total_users = len(users_data)
        total_pages = math.ceil(total_users / per_page) if total_users > 0 else 1

        start_idx = (page - 1) * per_page
        end_idx = start_idx + per_page
        paginated_users = users_data[start_idx:end_idx] if users_data else []

        # Получаем роль пользователя для отображения доступных действий
        user_role = session.get('user_role', 'viewer')

        return render_template('users.html',
                               users=paginated_users,
                               page=page,
                               total_pages=total_pages,
                               total_users=total_users,
                               search_query=search_query,
                               user_role=user_role)
    except Exception as e:
        logger.error(f"Ошибка при загрузке списка пользователей: {e}", exc_info=True)
        flash(f"Произошла ошибка: {e}", "danger")
        return redirect(url_for('index'))


@app.route('/user/<chat_id>')
@login_required
def user_details(chat_id: str):
    """
    Страница с деталями пользователя и его темами.

    Args:
        chat_id: ID чата пользователя
    """
    try:
        # Получаем данные из кэша или обновляем
        def get_user_data():
            is_enabled = db_manager.get_user_status(chat_id)
            subjects = db_manager.get_user_subjects(chat_id)

            return {
                'is_enabled': is_enabled,
                'subjects': subjects
            }

        user_data_cache = get_cached_data(f'user_data_{chat_id}', get_user_data, ttl=60)

        is_enabled = user_data_cache['is_enabled']
        subjects = user_data_cache['subjects']

        # Добавить поиск по темам
        search_query = request.args.get('search', '')
        if search_query:
            subjects = [s for s in subjects if search_query.lower() in s.lower()]

        user_data = {
            'chat_id': chat_id,
            'status': 'Активен' if is_enabled else 'Отключен',
            'is_enabled': is_enabled,
            'subjects': subjects
        }

        # Получаем роль пользователя для отображения доступных действий
        user_role = session.get('user_role', 'viewer')

        return render_template('user_details.html', user=user_data, user_role=user_role)
    except Exception as e:
        logger.error(f"Ошибка при загрузке деталей пользователя {chat_id}: {e}", exc_info=True)
        flash(f"Произошла ошибка: {e}", "danger")
        return redirect(url_for('users'))


@app.route('/user/<chat_id>/toggle-status', methods=['POST'])
@login_required
@operator_required
@limiter.limit("100 per minute")
def toggle_user_status(chat_id: str):
    """
    Изменение статуса пользователя.

    Args:
        chat_id: ID чата пользователя
    """
    try:
        current_status = db_manager.get_user_status(chat_id)
        new_status = not current_status

        if db_manager.update_user_status(chat_id, new_status):
            status_text = 'включены' if new_status else 'отключены'
            flash(f"Уведомления для пользователя {chat_id} {status_text}", "success")
            logger.info(f"Изменен статус пользователя {chat_id} на {status_text}")

            # Логирование действия
            if 'user_id' in session:
                log_activity(db_manager, session['user_id'], f"toggle_user_status",
                             request.remote_addr, f"chat_id={chat_id}, new_status={status_text}")

            # Полная инвалидация всех кэшей для обеспечения согласованности данных
            invalidate_all_caches()

        else:
            flash(f"Не удалось изменить статус пользователя {chat_id}", "danger")
            logger.warning(f"Не удалось изменить статус пользователя {chat_id}")

        return redirect(url_for('user_details', chat_id=chat_id))
    except Exception as e:
        logger.error(f"Ошибка при изменении статуса пользователя {chat_id}: {e}", exc_info=True)
        flash(f"Произошла ошибка: {e}", "danger")
        return redirect(url_for('user_details', chat_id=chat_id))


@app.route('/user/<chat_id>/add-subject', methods=['POST'])
@login_required
@operator_required
@limiter.limit("100 per minute")
def add_subject(chat_id: str):
    """
    Добавление новой темы для пользователя.

    Args:
        chat_id: ID чата пользователя
    """
    try:
        subject = request.form.get('subject', '').strip()
        confirm_duplicate = request.form.get('confirm_duplicate') == 'true'

        if not subject:
            flash("Тема не может быть пустой", "warning")
            return redirect(url_for('user_details', chat_id=chat_id))

        # Проверка на дубликаты тем у других пользователей
        duplicate_subjects = check_duplicate_subjects([subject], chat_id)

        if duplicate_subjects and not confirm_duplicate:
            flash(f"Тема '{subject}' уже существует у другого пользователя. Вы хотите продолжить?", "warning")
            return render_template('confirm_duplicate_subject.html',
                                   subject=subject,
                                   chat_id=chat_id,
                                   user_role=session.get('user_role', 'viewer'))

        if db_manager.add_subject(chat_id, subject):
            flash(f"Тема '{subject}' успешно добавлена", "success")
            logger.info(f"Добавлена тема '{subject}' для пользователя {chat_id}")

            # Логирование действия
            if 'user_id' in session:
                log_activity(db_manager, session['user_id'], f"add_subject",
                             request.remote_addr, f"chat_id={chat_id}, subject={subject}")

            # Полная инвалидация всех кэшей для обеспечения согласованности данных
            invalidate_all_caches()

        else:
            flash(f"Тема '{subject}' уже существует или произошла ошибка", "warning")
            logger.warning(f"Не удалось добавить тему '{subject}' для пользователя {chat_id}")

        return redirect(url_for('user_details', chat_id=chat_id))
    except Exception as e:
        logger.error(f"Ошибка при добавлении темы для пользователя {chat_id}: {e}", exc_info=True)
        flash(f"Произошла ошибка: {e}", "danger")
        return redirect(url_for('user_details', chat_id=chat_id))


@app.route('/user/<chat_id>/edit-subject', methods=['POST'])
@login_required
@operator_required
@limiter.limit("100 per minute")
def edit_subject(chat_id: str):
    """
    Редактирование темы пользователя.

    Args:
        chat_id: ID чата пользователя
    """
    try:
        old_subject = request.form.get('old_subject', '').strip()
        new_subject = request.form.get('new_subject', '').strip()
        confirm_duplicate = request.form.get('confirm_duplicate') == 'true'

        if not old_subject or not new_subject:
            flash("Тема не может быть пустой", "warning")
            return redirect(url_for('user_details', chat_id=chat_id))

        # Проверка на дубликаты тем у других пользователей
        if old_subject != new_subject:  # Проверяем только если тема действительно изменилась
            duplicate_subjects = check_duplicate_subjects([new_subject], chat_id)

            if duplicate_subjects and not confirm_duplicate:
                flash(f"Тема '{new_subject}' уже существует у другого пользователя. Вы хотите продолжить?", "warning")
                return render_template('confirm_edit_subject.html',
                                       old_subject=old_subject,
                                       new_subject=new_subject,
                                       chat_id=chat_id,
                                       user_role=session.get('user_role', 'viewer'))

        # Удаляем старую тему и добавляем новую
        if db_manager.delete_subject(chat_id, old_subject) and db_manager.add_subject(chat_id, new_subject):
            flash(f"Тема изменена с '{old_subject}' на '{new_subject}'", "success")
            logger.info(f"Изменена тема с '{old_subject}' на '{new_subject}' для пользователя {chat_id}")

            # Логирование действия
            if 'user_id' in session:
                log_activity(db_manager, session['user_id'], f"edit_subject",
                             request.remote_addr, f"chat_id={chat_id}, old={old_subject}, new={new_subject}")

            # Полная инвалидация всех кэшей для обеспечения согласованности данных
            invalidate_all_caches()

        else:
            flash(f"Не удалось изменить тему", "danger")
            logger.warning(f"Не удалось изменить тему с '{old_subject}' на '{new_subject}' для пользователя {chat_id}")

        return redirect(url_for('user_details', chat_id=chat_id))
    except Exception as e:
        logger.error(f"Ошибка при редактировании темы для пользователя {chat_id}: {e}", exc_info=True)
        flash(f"Произошла ошибка: {e}", "danger")
        return redirect(url_for('user_details', chat_id=chat_id))


@app.route('/user/<chat_id>/delete-subject', methods=['POST'])
@login_required
@operator_required
@limiter.limit("100 per minute")
def delete_subject(chat_id: str):
    """
    Удаление темы у пользователя.

    Args:
        chat_id: ID чата пользователя
    """
    try:
        subject = request.form.get('subject', '').strip()

        if not subject:
            flash("Необходимо указать тему для удаления", "warning")
            return redirect(url_for('user_details', chat_id=chat_id))

        if db_manager.delete_subject(chat_id, subject):
            flash(f"Тема '{subject}' успешно удалена", "success")
            logger.info(f"Удалена тема '{subject}' у пользователя {chat_id}")

            # Логирование действия
            if 'user_id' in session:
                log_activity(db_manager, session['user_id'], f"delete_subject",
                             request.remote_addr, f"chat_id={chat_id}, subject={subject}")

            # Полная инвалидация всех кэшей для обеспечения согласованности данных
            invalidate_all_caches()

        else:
            flash(f"Не удалось удалить тему '{subject}'", "warning")
            logger.warning(f"Не удалось удалить тему '{subject}' у пользователя {chat_id}")

        return redirect(url_for('user_details', chat_id=chat_id))
    except Exception as e:
        logger.error(f"Ошибка при удалении темы у пользователя {chat_id}: {e}", exc_info=True)
        flash(f"Произошла ошибка: {e}", "danger")
        return redirect(url_for('user_details', chat_id=chat_id))


@app.route('/user/<chat_id>/add-subjects-bulk', methods=['POST'])
@login_required
@operator_required
@limiter.limit("100 per minute")
def add_subjects_bulk(chat_id: str):
    """
    Массовое добавление тем для пользователя.

    Args:
        chat_id: ID чата пользователя
    """
    try:
        subjects_text = request.form.get('subjects_bulk', '').strip()
        confirm_duplicate_subjects = request.form.get('confirm_duplicate_subjects') == 'true'

        if not subjects_text:
            flash("Необходимо указать хотя бы одну тему", "warning")
            return redirect(url_for('user_details', chat_id=chat_id))

        # Разделяем текст на отдельные темы
        subjects = [line.strip() for line in subjects_text.split('\n') if line.strip()]

        if not subjects:
            flash("Необходимо указать хотя бы одну тему", "warning")
            return redirect(url_for('user_details', chat_id=chat_id))

        # Проверка на дубликаты тем у других пользователей
        duplicate_subjects = check_duplicate_subjects(subjects, chat_id)

        if duplicate_subjects and not confirm_duplicate_subjects:
            duplicate_subjects_text = ", ".join(duplicate_subjects)
            flash(
                f"Следующие темы уже существуют у других пользователей: {duplicate_subjects_text}. Вы хотите продолжить?",
                "warning")
            return render_template('confirm_bulk_subjects.html',
                                   subjects_text=subjects_text,
                                   chat_id=chat_id,
                                   duplicate_subjects=duplicate_subjects,
                                   user_role=session.get('user_role', 'viewer'))

        # Выполняем в отдельном потоке для большого количества тем
        def add_subjects_task():
            count = db_manager.add_multiple_subjects(chat_id, subjects)
            return count

        future = executor.submit(add_subjects_task)
        count = future.result()

        if count > 0:
            flash(f"Успешно добавлено {count} новых тем", "success")
            logger.info(f"Добавлено {count} новых тем для пользователя {chat_id}")

            # Логирование действия
            if 'user_id' in session:
                log_activity(db_manager, session['user_id'], f"add_subjects_bulk",
                             request.remote_addr, f"chat_id={chat_id}, count={count}")

            # Полная инвалидация всех кэшей для обеспечения согласованности данных
            invalidate_all_caches()

        else:
            flash("Не удалось добавить новые темы или все они уже существуют", "warning")
            logger.warning(f"Не удалось добавить новые темы для пользователя {chat_id}")

        return redirect(url_for('user_details', chat_id=chat_id))
    except Exception as e:
        logger.error(f"Ошибка при массовом добавлении тем для пользователя {chat_id}: {e}", exc_info=True)
        flash(f"Произошла ошибка: {e}", "danger")
        return redirect(url_for('user_details', chat_id=chat_id))


@app.route('/user/<chat_id>/delete', methods=['POST'])
@login_required
@operator_required
@limiter.limit("100 per minute")
def delete_user(chat_id: str):
    """
    Удаление пользователя.

    Args:
        chat_id: ID чата пользователя
    """
    try:
        if db_manager.delete_user(chat_id):
            flash(f"Пользователь {chat_id} успешно удален", "success")
            logger.info(f"Пользователь {chat_id} удален")

            # Логирование действия
            if 'user_id' in session:
                log_activity(db_manager, session['user_id'], f"delete_user",
                             request.remote_addr, f"chat_id={chat_id}")

            # Полная инвалидация всех кэшей для обеспечения согласованности данных
            invalidate_all_caches()

        else:
            flash(f"Не удалось удалить пользователя {chat_id}", "danger")
            logger.warning(f"Не удалось удалить пользователя {chat_id}")

        return redirect(url_for('users'))
    except Exception as e:
        logger.error(f"Ошибка при удалении пользователя {chat_id}: {e}", exc_info=True)
        flash(f"Произошла ошибка: {e}", "danger")
        return redirect(url_for('users'))


@app.route('/login', methods=['GET', 'POST'])
@limiter.limit("20 per hour")
def login():
    """Страница авторизации с усиленной защитой от брутфорса."""
    # Проверяем глобальную блокировку
    global global_lockout_until
    if time.time() < global_lockout_until:
        wait_time = int(global_lockout_until - time.time())
        wait_minutes = wait_time // 60
        time_text = f"{wait_minutes // 60} ч. {wait_minutes % 60} мин." if wait_minutes > 60 else f"{wait_minutes} мин."
        flash(f'Система временно заблокирована. Попробуйте снова через {time_text}', 'danger')
        return render_template('login.html')

    if request.method == 'POST':
        client_ip = request.remote_addr
        username = request.form.get('username', '')
        current_time = time.time()

        # Проверяем, заблокирован ли IP
        if client_ip in ip_login_attempts and ip_login_attempts[client_ip].get('locked_until', 0) > current_time:
            wait_time = int(ip_login_attempts[client_ip]['locked_until'] - current_time)
            wait_minutes = wait_time // 60

            if wait_minutes > 60:
                time_text = f"{wait_minutes // 60} ч. {wait_minutes % 60} мин."
            else:
                time_text = f"{wait_minutes} мин."

            flash(f'Слишком много неудачных попыток. Попробуйте снова через {time_text}', 'danger')
            logger.warning(f"Попытка входа с заблокированного IP {client_ip}")
            return render_template('login.html')

        # Проверяем, заблокирован ли пользователь
        if username in username_login_attempts and username_login_attempts[username].get('locked_until',
                                                                                         0) > current_time:
            wait_time = int(username_login_attempts[username]['locked_until'] - current_time)
            wait_minutes = wait_time // 60

            if wait_minutes > 60:
                time_text = f"{wait_minutes // 60} ч. {wait_minutes % 60} мин."
            else:
                time_text = f"{wait_minutes} мин."

            flash(f'Слишком много неудачных попыток для этого пользователя. Попробуйте снова через {time_text}',
                  'danger')
            logger.warning(f"Попытка входа для заблокированного пользователя {username} с IP {client_ip}")
            return render_template('login.html')

        # Инициализация структур для отслеживания попыток
        if client_ip not in ip_login_attempts:
            ip_login_attempts[client_ip] = {'attempts': 0, 'locked_until': 0, 'lockout_count': 0}

        if username not in username_login_attempts:
            username_login_attempts[username] = {'attempts': 0, 'locked_until': 0, 'lockout_count': 0}

        # Сбрасываем блокировку, если время истекло
        if ip_login_attempts[client_ip]['locked_until'] <= current_time:
            ip_login_attempts[client_ip]['locked_until'] = 0

        if username_login_attempts[username]['locked_until'] <= current_time:
            username_login_attempts[username]['locked_until'] = 0

        password = request.form.get('password', '')

        # Используем блокировку для предотвращения race condition
        with admin_login_lock:
            try:
                with db_manager.get_connection() as conn:
                    cursor = conn.cursor()

                    # Проверяем существование таблицы admin_users
                    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='admin_users'")
                    if not cursor.fetchone():
                        # Если таблицы нет, используем стандартную авторизацию
                        if username == ADMIN_USERNAME and password == ADMIN_PASSWORD:
                            # Сбрасываем счетчики при успешном входе
                            ip_login_attempts[client_ip] = {'attempts': 0, 'locked_until': 0, 'lockout_count': 0}
                            username_login_attempts[username] = {'attempts': 0, 'locked_until': 0, 'lockout_count': 0}

                            session.clear()
                            session['user_id'] = 1
                            session['username'] = username
                            session['user_role'] = 'admin'
                            session['last_activity'] = time.time()

                            flash('Вы успешно вошли в систему', 'success')
                            logger.info(f"Пользователь {username} успешно вошел в систему с IP {client_ip}")

                            # Инициализация БД для создания таблиц admin_users
                            initialize_database()

                            next_page = request.args.get('next')
                            return redirect(next_page or url_for('index'))
                    else:
                        # Используем новую систему авторизации
                        cursor.execute(
                            "SELECT id, username, password_hash, role, is_active FROM admin_users WHERE username = ?",
                            (username,)
                        )
                        user = cursor.fetchone()

                        if user and user['is_active'] and verify_password(user['password_hash'], password):
                            # Обновляем время последнего входа
                            cursor.execute(
                                "UPDATE admin_users SET last_login = ? WHERE id = ?",
                                (datetime.now().strftime('%Y-%m-%d %H:%M:%S'), user['id'])
                            )
                            conn.commit()

                            # Сбрасываем счетчики при успешном входе
                            ip_login_attempts[client_ip] = {'attempts': 0, 'locked_until': 0, 'lockout_count': 0}
                            username_login_attempts[username] = {'attempts': 0, 'locked_until': 0, 'lockout_count': 0}

                            session.clear()
                            session['user_id'] = user['id']
                            session['username'] = user['username']
                            session['user_role'] = user['role']
                            session['last_activity'] = time.time()

                            # Логируем вход
                            log_activity(db_manager, user['id'], "login", client_ip)

                            flash('Вы успешно вошли в систему', 'success')
                            logger.info(f"Пользователь {username} успешно вошел в систему с IP {client_ip}")

                            next_page = request.args.get('next')
                            return redirect(next_page or url_for('index'))

                    # Если дошли сюда, значит авторизация не удалась
                    # Увеличиваем счетчик неудачных попыток для IP и пользователя
                    ip_login_attempts[client_ip]['attempts'] += 1
                    username_login_attempts[username]['attempts'] += 1

                    # Проверяем необходимость блокировки IP
                    if ip_login_attempts[client_ip]['attempts'] >= MAX_ATTEMPTS:
                        lockout_stage = min(ip_login_attempts[client_ip]['lockout_count'], len(LOCKOUT_STAGES) - 1)
                        lockout_time = LOCKOUT_STAGES[lockout_stage]

                        ip_login_attempts[client_ip]['locked_until'] = current_time + lockout_time
                        ip_login_attempts[client_ip]['lockout_count'] += 1
                        ip_login_attempts[client_ip]['attempts'] = 0

                        # Глобальная блокировка при большом количестве заблокированных IP
                        blocked_ips = sum(
                            1 for ip, data in ip_login_attempts.items() if data['locked_until'] > current_time)
                        if blocked_ips >= global_lockout_trigger_threshold:
                            global_lockout_until = current_time + LOCKOUT_STAGES[0]  # 15 минут по умолчанию
                            logger.warning(f"Активирована глобальная блокировка: {blocked_ips} заблокированных IP")

                    # Проверяем необходимость блокировки пользователя
                    if username_login_attempts[username]['attempts'] >= MAX_ATTEMPTS:
                        lockout_stage = min(username_login_attempts[username]['lockout_count'], len(LOCKOUT_STAGES) - 1)
                        lockout_time = LOCKOUT_STAGES[lockout_stage]

                        username_login_attempts[username]['locked_until'] = current_time + lockout_time
                        username_login_attempts[username]['lockout_count'] += 1
                        username_login_attempts[username]['attempts'] = 0

                    # Выводим сообщение о неудачной попытке
                    remaining_ip = MAX_ATTEMPTS - ip_login_attempts[client_ip]['attempts']
                    flash(f'Неправильное имя пользователя или пароль. Осталось попыток: {remaining_ip}', 'danger')
                    logger.warning(f"Неудачная попытка входа с IP {client_ip} для пользователя: {username}")

            except Exception as e:
                logger.error(f"Ошибка при проверке авторизации: {e}")
                flash("Ошибка при входе в систему", "danger")

    return render_template('login.html')


@app.route('/logout')
def logout():
    """Выход из системы."""
    username = session.get('username', 'Неизвестный')
    user_id = session.get('user_id')

    # Логирование выхода
    if user_id:
        log_activity(db_manager, user_id, "logout", request.remote_addr)

    session.clear()
    flash('Вы вышли из системы', 'info')
    logger.info(f"Пользователь {username} вышел из системы")

    return redirect(url_for('login'))


@app.route('/add-user', methods=['GET', 'POST'])
@login_required
@operator_required
@limiter.limit("100 per minute")
def add_user():
    """Страница добавления нового пользователя."""
    if request.method == 'POST':
        try:
            chat_id = request.form.get('chat_id', '').strip()
            status = 'Enable' if request.form.get('status') == 'enable' else 'Disable'
            subjects_text = request.form.get('subjects', '').strip()
            confirm_overwrite = request.form.get('confirm_overwrite') == 'true'
            confirm_duplicate_subjects = request.form.get('confirm_duplicate_subjects') == 'true'

            # Проверка chat_id
            if not chat_id:
                flash("Chat ID не может быть пустым", "warning")
                return render_template('add_user.html', user_role=session.get('user_role', 'viewer'))

            # Проверка, что chat_id является целым числом (положительным или отрицательным)
            if not re.match(r'^-?\d+$', chat_id):
                flash("Chat ID должен быть целым числом (может начинаться с - для групп)", "warning")
                return render_template('add_user.html', user_role=session.get('user_role', 'viewer'))

            # Проверка существования пользователя
            user_exists = db_manager.is_user_registered(chat_id)

            # Разбираем темы, если они есть
            subjects = []
            if subjects_text:
                subjects = [line.strip() for line in subjects_text.split('\n') if line.strip()]

            # Проверка на дубликаты тем
            duplicate_subjects = []
            if subjects:
                duplicate_subjects = check_duplicate_subjects(subjects, chat_id if user_exists else None)

            # Если пользователь существует и нет подтверждения, возвращаем форму подтверждения
            if user_exists and not confirm_overwrite:
                return render_template('add_user.html',
                                       user_exists=True,
                                       chat_id=chat_id,
                                       status=status,
                                       subjects=subjects_text,
                                       user_role=session.get('user_role', 'viewer'))

            # Если есть дубликаты тем и нет подтверждения, возвращаем форму подтверждения
            if duplicate_subjects and not confirm_duplicate_subjects:
                subjects_info = ", ".join(duplicate_subjects)
                flash(f"Следующие темы уже существуют у других пользователей: {subjects_info}", "warning")
                return render_template('add_user.html',
                                       chat_id=chat_id,
                                       status=status,
                                       subjects=subjects_text,
                                       duplicate_subjects=True,
                                       user_role=session.get('user_role', 'viewer'))

            # Добавление/обновление пользователя
            logger.info(f"Отправка запроса на добавление пользователя {chat_id} в БД")
            result = db_manager.add_user(chat_id, status) # Режим доставки будет по умолчанию

            if result:
                action = "обновлен" if user_exists else "добавлен"
                flash(f"Пользователь {chat_id} успешно {action}", "success")
                logger.info(f"Пользователь {chat_id} {action}")

                # Логирование действия
                if 'user_id' in session:
                    log_activity(db_manager, session['user_id'], f"add_user",
                                 request.remote_addr, f"chat_id={chat_id}, status={status}")

                # Если есть темы, добавляем их
                if subjects:
                    def add_subjects_task():
                        return db_manager.add_multiple_subjects(chat_id, subjects)

                    future = executor.submit(add_subjects_task)
                    count = future.result()

                    if count > 0:
                        flash(f"Успешно добавлено {count} тем для пользователя", "success")
                        logger.info(f"Добавлено {count} тем для пользователя {chat_id}")
                        if 'user_id' in session:
                            log_activity(db_manager, session['user_id'], f"add_subjects",
                                         request.remote_addr, f"chat_id={chat_id}, count={count}")

                # Полная инвалидация всех кэшей
                invalidate_all_caches()

                return redirect(url_for('user_details', chat_id=chat_id))
            else:
                logger.error(f"Не удалось добавить пользователя {chat_id} в БД")
                flash(f"Не удалось добавить пользователя {chat_id}. Проверьте логи.", "danger")
                return render_template('add_user.html', user_role=session.get('user_role', 'viewer'))

        except Exception as e:
            logger.error(f"Ошибка при добавлении нового пользователя: {e}", exc_info=True)
            flash(f"Произошла ошибка: {e}", "danger")
            return render_template('add_user.html', user_role=session.get('user_role', 'viewer'))

    # Для GET запроса
    return render_template('add_user.html', user_role=session.get('user_role', 'viewer'))


@app.route('/bot-status')
@login_required
def bot_status():
    """Страница со статусом бота и его управлением."""
    try:
        # Всегда получаем актуальный статус, игнорируя кэш
        status = get_bot_status(bypass_cache=True)
        user_role = session.get('user_role', 'viewer')
        return render_template('bot_status.html', status=status, user_role=user_role)
    except Exception as e:
        logger.error(f"Ошибка при получении статуса бота: {e}", exc_info=True)
        flash(f"Произошла ошибка: {e}", "danger")
        return redirect(url_for('index'))


@app.route('/bot-status/start', methods=['POST'])
@login_required
@operator_required
@limiter.limit("100 per minute")
def start_bot_handler():
    """Запуск бота."""
    try:
        logger.info("Отправка команды на запуск бота")
        result = start_bot()

        # Создаем флаг ручной остановки
        if result:
            manual_stop_path = "/app/data/.manual_stop"
            if os.path.exists(manual_stop_path):
                os.remove(manual_stop_path)
                logger.info(f"Удален флаг ручной остановки: {manual_stop_path}")

        # Полная инвалидация всех кэшей для обеспечения согласованности данных
        invalidate_all_caches()

        if result:
            flash("Бот успешно запущен", "success")
            logger.info("Пользователь запустил бота через веб-интерфейс")

            # Логирование действия
            if 'user_id' in session:
                log_activity(db_manager, session['user_id'], "start_bot", request.remote_addr)
        else:
            flash("Не удалось запустить бота", "warning")
            logger.warning("Не удалось запустить бота через веб-интерфейс")
    except Exception as e:
        flash(f"Ошибка при запуске бота: {e}", "danger")
        logger.error(f"Ошибка при запуске бота: {e}", exc_info=True)

    return redirect(url_for('bot_status'))


@app.route('/bot-status/stop', methods=['POST'])
@login_required
@operator_required
@limiter.limit("100 per minute")
def stop_bot_handler():
    """Остановка бота."""
    try:
        logger.info("Отправка команды на остановку бота")
        result = stop_bot()

        # Создаем флаг ручной остановки
        if result:
            manual_stop_path = "/app/data/.manual_stop"
            with open(manual_stop_path, 'w') as f:
                f.write('1')
            logger.info(f"Создан флаг ручной остановки: {manual_stop_path}")

        # Полная инвалидация всех кэшей для обеспечения согласованности данных
        invalidate_all_caches()

        if result:
            flash("Бот успешно остановлен", "success")
            logger.info("Пользователь остановил бота через веб-интерфейс")

            # Логирование действия
            if 'user_id' in session:
                log_activity(db_manager, session['user_id'], "stop_bot", request.remote_addr)
        else:
            flash("Не удалось остановить бота", "warning")
            logger.warning("Не удалось остановить бота через веб-интерфейс")
    except Exception as e:
        flash(f"Ошибка при остановке бота: {e}", "danger")
        logger.error(f"Ошибка при остановке бота: {e}", exc_info=True)

    return redirect(url_for('bot_status'))


@app.route('/api/bot-status')
@login_required
@limiter.limit("20 per minute")
def bot_status_api():
    """API для получения статуса бота в формате JSON."""
    try:
        # Всегда получаем актуальный статус, игнорируя кэш
        status = get_bot_status(bypass_cache=True)

        # Преобразуем datetime в строки для JSON
        if isinstance(status.get('last_check'), datetime):
            status['last_check'] = status['last_check'].isoformat()

        return jsonify(status)
    except Exception as e:
        logger.error(f"Ошибка при получении статуса бота API: {e}", exc_info=True)
        return jsonify({
            'running': False,
            'forwarder_active': False,
            'bot_active': False,
            'last_check': datetime.now().isoformat(),
            'uptime': 'Неизвестно',
            'uptime_seconds': 0,
            'error': str(e)
        })


@app.route('/optimize-db', methods=['POST'])
@login_required
@admin_required
@limiter.limit("2 per hour")
def optimize_db():
    """Запуск оптимизации базы данных"""
    try:
        # Прямая оптимизация без сложного скрипта
        script = """
import os, sys, time, sqlite3, subprocess

# Остановка бота
subprocess.run(["supervisorctl", "stop", "bot"])
time.sleep(5)

# Оптимизация
try:
    db_path = "/app/data/email_bot.db"
    conn = sqlite3.connect(db_path, isolation_level="EXCLUSIVE", timeout=60)
    cursor = conn.cursor()
    cursor.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    cursor.execute("VACUUM")
    cursor.execute("ANALYZE")
    cursor.execute("PRAGMA optimize")
    conn.close()

    # Создаем файл об успешном завершении
    with open("/app/data/.optimize_success", "w") as f:
        f.write("1")
except Exception as e:
    with open("/app/data/.optimize_error", "w") as f:
        f.write(str(e))

# Запуск бота
subprocess.run(["supervisorctl", "start", "bot"])

# Удаление флага блокировки
if os.path.exists("/app/data/email_bot.db.optimize.lock"):
    os.unlink("/app/data/email_bot.db.optimize.lock")
"""
        script_path = os.path.join(settings.DATA_DIR, "optimize_db.py")
        with open(script_path, "w") as f:
            f.write(script)

        # Создаем файл-флаг блокировки
        lock_file = settings.DATABASE_PATH + ".optimize.lock"
        with open(lock_file, "w") as f:
            f.write(str(int(time.time())))

        # Запускаем процесс
        subprocess.Popen(["python", script_path],
                         stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL,
                         close_fds=True)

        # Логируем действие
        if 'user_id' in session and db_manager:
            log_activity(db_manager, session['user_id'], "optimize_db", request.remote_addr)

        flash("Оптимизация запущена. Страница обновится автоматически.", "info")

        processes = []
        # Получение списка процессов
        for proc in psutil.process_iter(['pid', 'name', 'cmdline', 'create_time']):
            try:
                pinfo = proc.as_dict(['pid', 'name', 'cmdline', 'create_time'])
                if 'python' in pinfo.get('name', '').lower():
                    pinfo['cmdline_str'] = ' '.join([str(cmd) for cmd in pinfo.get('cmdline', []) if cmd])
                    processes.append(pinfo)
            except:
                pass

        # Проверка файлов БД
        db_files = {}
        db_path = Path(settings.DATABASE_PATH)
        if db_path.exists():
            db_files['main'] = {
                'size': db_path.stat().st_size // 1024,
                'mtime': datetime.fromtimestamp(db_path.stat().st_mtime).strftime('%Y-%m-%d %H:%M:%S')
            }

        # Статус бота
        bot_status_info = get_bot_status(bypass_cache=True)

        return render_template('diagnostics.html',
                               optimization_started=True,
                               processes=processes,
                               db_files=db_files,
                               bot_status=bot_status_info)

    except Exception as e:
        logger.error(f"Ошибка при запуске оптимизации: {e}", exc_info=True)
        flash(f"Ошибка: {e}", "danger")
        return redirect(url_for('diagnostics'))


@app.route('/optimization-status')
@login_required
def optimization_status():
    """Проверка статуса оптимизации"""
    success_file = os.path.join(settings.DATA_DIR, ".optimize_success")
    error_file = os.path.join(settings.DATA_DIR, ".optimize_error")
    lock_file = settings.DATABASE_PATH + ".optimize.lock"

    if os.path.exists(success_file):
        try:
            os.unlink(success_file)
        except:
            pass
        return jsonify({"status": "completed"})
    elif os.path.exists(error_file):
        try:
            with open(error_file) as f:
                error = f.read()
            os.unlink(error_file)
        except:
            error = "Неизвестная ошибка"
        return jsonify({"status": "error", "message": error})
    elif os.path.exists(lock_file):
        # Проверка устаревшего lock-файла (старше 5 минут)
        file_time = os.path.getmtime(lock_file)
        if time.time() - file_time > 300:  # 5 минут
            try:
                os.unlink(lock_file)
            except:
                pass
            return jsonify({"status": "not_running"})
        return jsonify({"status": "running"})
    else:
        return jsonify({"status": "not_running"})

@app.route('/help')
@login_required
def help():
    """Страница со справочной информацией."""
    try:
        user_role = session.get('user_role', 'viewer')
        return render_template('help.html', user_role=user_role)
    except Exception as e:
        logger.error(f"Ошибка при загрузке страницы справки: {e}", exc_info=True)
        flash(f"Произошла ошибка: {e}", "danger")
        return redirect(url_for('index'))


@app.route('/diagnostics')
@login_required
@admin_required
def diagnostics():
    """Страница диагностики системы."""
    try:
        processes = []
        for proc in psutil.process_iter(['pid', 'name', 'cmdline', 'create_time']):
            try:
                pinfo = proc.as_dict(['pid', 'name', 'cmdline', 'create_time'])
                if 'python' in pinfo.get('name', '').lower():
                    # Преобразуем командную строку в строку
                    cmdline = ' '.join([str(cmd) for cmd in pinfo.get('cmdline', []) if cmd])
                    pinfo['cmdline_str'] = cmdline
                    processes.append(pinfo)
            except Exception:
                pass

        # Проверка файлов БД
        db_files = {}
        db_path = Path(settings.DATABASE_PATH)
        if db_path.exists():
            db_files['main'] = {
                'size': db_path.stat().st_size // 1024,  # size in KB
                'mtime': datetime.fromtimestamp(db_path.stat().st_mtime).strftime('%Y-%m-%d %H:%M:%S')
            }

        # Проверка WAL файлов
        wal_path = Path(f"{settings.DATABASE_PATH}-wal")
        if wal_path.exists():
            db_files['wal'] = {
                'size': wal_path.stat().st_size // 1024,  # size in KB
                'mtime': datetime.fromtimestamp(wal_path.stat().st_mtime).strftime('%Y-%m-%d %H:%M:%S')
            }

        shm_path = Path(f"{settings.DATABASE_PATH}-shm")
        if shm_path.exists():
            db_files['shm'] = {
                'size': shm_path.stat().st_size // 1024,  # size in KB
                'mtime': datetime.fromtimestamp(shm_path.stat().st_mtime).strftime('%Y-%m-%d %H:%M:%S')
            }

        # Статус бота
        bot_status_info = get_bot_status(bypass_cache=True)

        return render_template('diagnostics.html',
                               processes=processes,
                               db_files=db_files,
                               bot_status=bot_status_info)
    except Exception as e:
        logger.error(f"Ошибка при загрузке страницы диагностики: {e}", exc_info=True)
        flash(f"Произошла ошибка: {e}", "danger")
        return redirect(url_for('index'))


# Новые маршруты для управления пользователями админки
@app.route('/admin/users')
@login_required
@admin_required
def admin_users():
    """Управление административными пользователями."""
    try:
        with db_manager.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT id, username, role, created_at, last_login, is_active 
                FROM admin_users ORDER BY username
            """)
            users = cursor.fetchall()
            return render_template('admin_users.html', users=users, user_role='admin')
    except Exception as e:
        logger.error(f"Ошибка при получении списка пользователей админки: {e}")
        flash(f"Произошла ошибка: {e}", "danger")
        return redirect(url_for('index'))


@app.route('/admin/users/add', methods=['GET', 'POST'])
@login_required
@admin_required
def add_admin_user():
    """Добавление нового административного пользователя."""
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        role = request.form.get('role', 'viewer')

        if not username or not password:
            flash("Имя пользователя и пароль обязательны", "warning")
            return render_template('add_admin_user.html', user_role='admin')

        try:
            with db_manager.get_connection() as conn:
                cursor = conn.cursor()
                # Проверяем, существует ли пользователь
                cursor.execute("SELECT 1 FROM admin_users WHERE username = ?", (username,))
                if cursor.fetchone():
                    flash(f"Пользователь с именем {username} уже существует", "warning")
                    return render_template('add_admin_user.html', user_role='admin')

                # Хешируем пароль и добавляем пользователя
                password_hash = hash_password(password)
                cursor.execute(
                    "INSERT INTO admin_users (username, password_hash, role) VALUES (?, ?, ?)",
                    (username, password_hash, role)
                )
                conn.commit()

                # Получаем ID нового пользователя
                cursor.execute("SELECT id FROM admin_users WHERE username = ?", (username,))
                user_id = cursor.fetchone()['id']

                # Логируем действие
                log_activity(db_manager, session['user_id'], f"created_user",
                             request.remote_addr, f"username={username}, role={role}")

                flash(f"Пользователь {username} успешно добавлен", "success")
                return redirect(url_for('admin_users'))
        except Exception as e:
            logger.error(f"Ошибка при добавлении пользователя админки: {e}")
            flash(f"Произошла ошибка: {e}", "danger")
            return render_template('add_admin_user.html', user_role='admin')

    return render_template('add_admin_user.html', user_role='admin')


@app.route('/admin/users/edit/<int:user_id>', methods=['GET', 'POST'])
@login_required
@admin_required
def edit_admin_user(user_id):
    """Редактирование административного пользователя."""
    try:
        with db_manager.get_connection() as conn:
            cursor = conn.cursor()

            if request.method == 'POST':
                username = request.form.get('username', '').strip()
                password = request.form.get('password', '')
                role = request.form.get('role', 'viewer')
                is_active = request.form.get('is_active') == 'true'

                if not username:
                    flash("Имя пользователя не может быть пустым", "warning")
                    return redirect(url_for('edit_admin_user', user_id=user_id))

                # Проверяем, что имя не занято другим пользователем
                cursor.execute("SELECT id FROM admin_users WHERE username = ? AND id != ?", (username, user_id))
                if cursor.fetchone():
                    flash(f"Пользователь с именем {username} уже существует", "warning")
                    return redirect(url_for('edit_admin_user', user_id=user_id))

                # Обновляем данные пользователя
                if password:
                    # Если указан новый пароль
                    password_hash = hash_password(password)
                    cursor.execute(
                        "UPDATE admin_users SET username = ?, password_hash = ?, role = ?, is_active = ? WHERE id = ?",
                        (username, password_hash, role, is_active, user_id)
                    )
                else:
                    # Если пароль не менялся
                    cursor.execute(
                        "UPDATE admin_users SET username = ?, role = ?, is_active = ? WHERE id = ?",
                        (username, role, is_active, user_id)
                    )

                conn.commit()

                # Логируем действие
                log_activity(db_manager, session['user_id'], f"edit_user",
                             request.remote_addr, f"user_id={user_id}, role={role}")

                flash(f"Пользователь {username} успешно обновлен", "success")
                return redirect(url_for('admin_users'))

            # GET запрос - загружаем данные пользователя
            cursor.execute("SELECT * FROM admin_users WHERE id = ?", (user_id,))
            user = cursor.fetchone()

            if not user:
                flash("Пользователь не найден", "danger")
                return redirect(url_for('admin_users'))

            return render_template('edit_admin_user.html', user=user, user_role='admin')

    except Exception as e:
        logger.error(f"Ошибка при редактировании пользователя админки: {e}")
        flash(f"Произошла ошибка: {e}", "danger")
        return redirect(url_for('admin_users'))


@app.route('/admin/users/delete/<int:user_id>', methods=['POST'])
@login_required
@admin_required
def delete_admin_user(user_id):
    """Удаление административного пользователя."""
    try:
        # Нельзя удалить самого себя
        if user_id == session.get('user_id'):
            flash("Невозможно удалить собственную учетную запись", "danger")
            return redirect(url_for('admin_users'))

        with db_manager.get_connection() as conn:
            cursor = conn.cursor()

            # Получаем имя пользователя для логирования
            cursor.execute("SELECT username FROM admin_users WHERE id = ?", (user_id,))
            user = cursor.fetchone()

            if not user:
                flash("Пользователь не найден", "danger")
                return redirect(url_for('admin_users'))

            # Удаляем пользователя
            cursor.execute("DELETE FROM admin_users WHERE id = ?", (user_id,))
            conn.commit()

            # Логируем действие
            log_activity(db_manager, session['user_id'], f"delete_user",
                         request.remote_addr, f"username={user['username']}")

            flash(f"Пользователь {user['username']} успешно удален", "success")
            return redirect(url_for('admin_users'))

    except Exception as e:
        logger.error(f"Ошибка при удалении пользователя админки: {e}")
        flash(f"Произошла ошибка: {e}", "danger")
        return redirect(url_for('admin_users'))


@app.route('/activity-log')
@login_required
@admin_required
def activity_log():
    """Просмотр журнала активности пользователей."""
    try:
        page = request.args.get('page', 1, type=int)
        per_page = 50  # Показываем больше записей на странице

        with db_manager.get_connection() as conn:
            cursor = conn.cursor()

            # Получаем общее количество записей
            cursor.execute("SELECT COUNT(*) FROM activity_log")
            total_entries = cursor.fetchone()[0]

            # Пагинация
            total_pages = math.ceil(total_entries / per_page)
            offset = (page - 1) * per_page

            # Получаем записи журнала с информацией о пользователях
            cursor.execute("""
                SELECT l.id, l.user_id, u.username, l.action, l.timestamp, l.ip_address, l.resource
                FROM activity_log l
                LEFT JOIN admin_users u ON l.user_id = u.id
                ORDER BY l.timestamp DESC
                LIMIT ? OFFSET ?
            """, (per_page, offset))

            logs = cursor.fetchall()

            return render_template('activity_log.html',
                                   logs=logs,
                                   page=page,
                                   total_pages=total_pages,
                                   user_role='admin')
    except Exception as e:
        logger.error(f"Ошибка при загрузке журнала активности: {e}")
        flash(f"Произошла ошибка: {e}", "danger")
        return redirect(url_for('index'))


@app.errorhandler(404)
def page_not_found(e):
    """Обработка 404 ошибки."""
    logger.warning(f"Страница не найдена: {request.path}")
    return render_template('404.html', user_role=session.get('user_role', 'viewer')), 404


@app.errorhandler(500)
def internal_server_error(e):
    """Обработка 500 ошибки."""
    logger.error(f"Внутренняя ошибка сервера: {e}", exc_info=True)
    return render_template('500.html', user_role=session.get('user_role', 'viewer')), 500


@app.errorhandler(429)
def ratelimit_handler(e):
    """Обработка превышения лимита запросов."""
    logger.warning(f"Превышение лимита запросов: {request.path} с IP {request.remote_addr}")
    return render_template('429.html', error=str(e), user_role=session.get('user_role', 'viewer')), 429


@app.errorhandler(403)
def forbidden_handler(e):
    """Обработка ошибки доступа."""
    logger.warning(f"Запрещенный доступ: {request.path} с IP {request.remote_addr}")
    return render_template('403.html', error=str(e), user_role=session.get('user_role', 'viewer')), 403


def parse_args():
    """Разбор аргументов командной строки."""
    import argparse
    parser = argparse.ArgumentParser(description='Веб-интерфейс администратора Email-Telegram бота')
    parser.add_argument('--host', type=str, default='127.0.0.1',
                        help='IP адрес для прослушивания (по умолчанию 127.0.0.1 для локального доступа)')
    parser.add_argument('--port', type=int, default=5000, help='Порт для прослушивания')
    parser.add_argument('--debug', action='store_true', help='Запустить в режиме отладки')
    parser.add_argument('--workers', type=int, default=2,
                        help='Количество рабочих процессов (только для gunicorn)')
    parser.add_argument('--timeout', type=int, default=30,
                        help='Тайм-аут для запросов в секундах (только для gunicorn)')
    parser.add_argument('--access-log', type=str, default='-',
                        help='Путь к файлу журнала доступа (только для gunicorn)')
    return parser.parse_args()


# Настройки для Gunicorn
def get_gunicorn_config():
    """Получение настроек для Gunicorn из переменных окружения."""
    import os

    config = {
        'bind': os.environ.get('GUNICORN_BIND', '0.0.0.0:5000'),
        'workers': int(os.environ.get('GUNICORN_WORKERS', '2')),
        'timeout': int(os.environ.get('GUNICORN_TIMEOUT', '60')),
        'worker_class': os.environ.get('GUNICORN_WORKER_CLASS', 'sync'),
        'max_requests': int(os.environ.get('GUNICORN_MAX_REQUESTS', '1000')),
        'max_requests_jitter': int(os.environ.get('GUNICORN_MAX_REQUESTS_JITTER', '50')),
        'access_log_format': '%(h)s %(l)s %(u)s %(t)s "%(r)s" %(s)s %(b)s "%(f)s" "%(a)s" %(D)s ms',
    }

    return config


def main():
    """Основная функция для запуска веб-интерфейса."""
    # Разбор аргументов командной строки
    args = parse_args()

    # Проверяем наличие пароля администратора
    if not ADMIN_PASSWORD:
        logger.critical("Отсутствует пароль администратора в переменных окружения (ADMIN_PASSWORD)!")
        logger.critical("Добавьте ADMIN_PASSWORD в файл .env и перезапустите приложение.")
        print("ОШИБКА: Отсутствует пароль администратора (ADMIN_PASSWORD) в переменных окружения!")
        print("Добавьте ADMIN_PASSWORD в файл .env и перезапустите приложение.")
        sys.exit(1)

    # Инициализация базы данных для пользователей админки
    initialize_database()

    # Запускаем приложение
    logger.info(f"Запуск веб-интерфейса администратора на {args.host}:{args.port}")

    # Проверяем, доступен ли gunicorn для продакшен-запуска
    try:
        import gunicorn
        logger.info("Обнаружен gunicorn, рекомендуется использовать его для продакшен-запуска")
        print("Для запуска с gunicorn используйте:")
        print(
            f"gunicorn --bind {args.host}:{args.port} --workers {args.workers} --timeout {args.timeout} --access-logfile {args.access_log} 'src.admin:app'")
    except ImportError:
        logger.warning("Gunicorn не найден. Для продакшен-запуска рекомендуется установить gunicorn")

    # Стандартный запуск Flask для разработки
    app.run(debug=args.debug, host=args.host, port=args.port, threaded=True)


if __name__ == "__main__":
    main()