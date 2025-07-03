# tests/test_telegram_handler.py

import sys
import os
import time
import tracemalloc
import gc
import linecache
from unittest.mock import patch, MagicMock

# --- 1. Настройка путей ---
script_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(script_dir)
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from tests.test_utils import MockSummarizationManager
from tests.test_utils import MockWeasyHTML
sys.modules['weasyprint'] = MagicMock()
sys.modules['weasyprint'].HTML = MockWeasyHTML

mock_telebot = MagicMock()
# Это список, в который будут складываться наши обработчики
mock_telebot.TeleBot.return_value.message_handlers = []
# Это функция, которая будет вызываться декоратором @bot.message_handler
def mock_message_handler_decorator(*args, **kwargs):
    def decorator(func):
        # Добавляем обработчик в наш список для последующего поиска
        mock_telebot.TeleBot.return_value.message_handlers.append(
            {'function': func, 'filters': kwargs}
        )
        return func
    return decorator

# Привязываем нашу мок-функцию к моку telebot
mock_telebot.TeleBot.return_value.message_handler = mock_message_handler_decorator

# Подменяем реальный telebot на наш умный мок
sys.modules['telebot'] = mock_telebot

# --- 3. Импорт моков и тестируемого класса ---
from tests.test_utils import MockDBManager, get_process_memory
from src.core.telegram_handler import EmailBotHandler


# --- 4. Функции анализа памяти ---
def print_top_comparison(snapshot, old_snapshot, limit=15):
    # (Копируем эту функцию из предыдущего теста)
    print(f"\n--- TOP {limit} NEW MEMORY ALLOCATIONS (DIFFERENCE) ---")
    stats = snapshot.compare_to(old_snapshot, 'lineno')
    for i, stat in enumerate(stats[:limit], 1):
        frame = stat.traceback[0]
        filename = os.path.basename(frame.filename)
        line_content = linecache.getline(frame.filename, frame.lineno).strip()
        print(f"#{i}: {filename}:{frame.lineno}: {stat.size / 1024:.1f} KB in {stat.count_diff} new blocks")
    linecache.clearcache()


# --- Основная функция теста ---
def run_handler_leak_test(iterations: int):
    """
    Тестирует утечку памяти в EmailBotHandler, особенно в кэше контекста.
    """
    print("--- Starting Telegram Handler Leak Test ---")

    # Патчим только базу данных. Telebot уже замокан глобально.
    with patch('src.db.manager.DatabaseManager', return_value=MockDBManager()), \
            patch('src.core.telegram_handler.SummarizationManager', MockSummarizationManager), \
            patch('src.core.summarization.DatabaseManager', return_value=MockDBManager()):

        # --- Инициализация теста ---
        print("Initializing memory tracking...")
        tracemalloc.start(25)

        print("Creating EmailBotHandler instance...")
        handler = EmailBotHandler(db_manager=MockDBManager())
        handler._queue_message = MagicMock()

        # Модифицируем мок-бота, чтобы send_message возвращал объект с message_id
        # Каждый раз message_id будет новым
        message_id_counter = 0

        def mock_send_message(*args, **kwargs):
            nonlocal message_id_counter
            message_id_counter += 1
            mock_msg = MagicMock()
            mock_msg.message_id = message_id_counter
            return mock_msg

        handler.bot.send_message = mock_send_message

        print("Taking initial snapshot...")
        gc.collect()
        initial_memory = get_process_memory()
        initial_snapshot = tracemalloc.take_snapshot()
        print(f"Initial memory usage: {initial_memory:.2f} MB")

        # --- Цикл нагрузки: симуляция команды /reports ---
        print(f"\nRunning {iterations} simulations of /reports command...")
        start_time = time.time()

        # Находим нужный обработчик
        # ВАЖНО: handler.register_handlers() привязывает реальные функции,
        # а не моки. Мы можем вызвать их напрямую.
        reports_handler = None
        for h in handler.bot.message_handlers:
            if h['filters'].get('commands') and 'reports' in h['filters']['commands']:
                reports_handler = h['function']
                break

        if not reports_handler:
            raise RuntimeError("Could not find the /reports handler!")

        for i in range(iterations):
            # Создаем мок входящего сообщения
            mock_message = MagicMock()
            mock_message.chat.id = 123456
            mock_message.from_user.first_name = "Tester"

            # Вызываем обработчик напрямую
            reports_handler(mock_message)

        end_time = time.time()
        print(f"Loop finished in {end_time - start_time:.2f} seconds.")

        # Проверяем, что кэш действительно вырос
        print(f"Final context cache size: {len(handler.message_report_context_cache)}")

        # --- Сбор и анализ результатов ---
        print("\nTaking final snapshot and analyzing results...")
        gc.collect()

        final_memory = get_process_memory()
        final_snapshot = tracemalloc.take_snapshot()

        print("\n" + "=" * 40)
        print("--- MEMORY USAGE SUMMARY (psutil) ---")
        print(f"Initial memory: {initial_memory:.2f} MB")
        print(f"Final memory:   {final_memory:.2f} MB")
        print(f"Difference:     {final_memory - initial_memory:.2f} MB")
        if iterations > 0:
            leak_per_call = ((final_memory - initial_memory) * 1024) / iterations
            print(f"Leak per /reports call: {leak_per_call:.2f} KB")
        print("=" * 40)

        # Сравниваем снимки
        print_top_comparison(final_snapshot, initial_snapshot)

        tracemalloc.stop()


if __name__ == "__main__":
    # 1000 вызовов /reports без закрытия - хороший стресс-тест
    TEST_ITERATIONS = 1000
    run_handler_leak_test(TEST_ITERATIONS)


