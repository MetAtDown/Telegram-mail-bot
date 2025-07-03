# tests/test_system_restarts.py

import sys
import os
import time
import tracemalloc
import gc
import linecache
from unittest.mock import patch, MagicMock

# --- 1. Настройка путей для импортов ---
script_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(script_dir)
if project_root not in sys.path:
    sys.path.insert(0, project_root)

# --- 2. Мокирование зависимостей ДО импортов ---
from tests.test_utils import MockWeasyHTML
sys.modules['weasyprint'] = MagicMock()
sys.modules['weasyprint'].HTML = MockWeasyHTML
sys.modules['telebot'] = MagicMock()

# --- 3. Импорт моков и утилит ---
from tests.test_utils import (
    MockSettings, MockImapSsl, MockDBManager, MockEmailBotHandler,
    get_process_memory
)

# --- 4. Импорт основных классов с ПРАВИЛЬНЫМИ путями ---
from src.core.system import EmailTelegramSystem
from src.core.email_handler import EmailTelegramForwarder

# --- Функции для анализа памяти ---
def print_top_comparison(snapshot, old_snapshot, limit=15):
    """Более информативный вывод сравнения снимков tracemalloc."""
    print(f"\n--- TOP {limit} NEW MEMORY ALLOCATIONS (DIFFERENCE) ---")
    stats = snapshot.compare_to(old_snapshot, 'lineno')

    for i, stat in enumerate(stats[:limit], 1):
        frame = stat.traceback[0]
        filename = os.path.basename(frame.filename)
        line_content = linecache.getline(frame.filename, frame.lineno).strip()

        print(f"#{i}: {filename}:{frame.lineno}")
        print(f"   Line: `{line_content}`")
        print(f"   New memory: {stat.size_diff / 1024:.1f} KB in {stat.count_diff} new blocks")
        print(f"   Total size: {stat.size / 1024:.1f} KB ({stat.count} blocks total)")
        print("-" * 20)
    linecache.clearcache()

# --- Основная функция теста ---
def run_restart_leak_test(iterations: int):
    """
    Тестирует утечку памяти при многократном перезапуске компонентов системы.
    """
    print("--- Starting System Restart Leak Test ---")

    # Патчим все внешние и "тяжелые" зависимости, указывая ПОЛНЫЙ путь
    # от корня проекта (src), где Python ищет модули.
    with patch('src.db.manager.DatabaseManager', return_value=MockDBManager()), \
         patch('src.core.telegram_handler.EmailBotHandler', MockEmailBotHandler), \
         patch('imaplib.IMAP4_SSL', MockImapSsl), \
         patch('src.core.system.settings', MockSettings), \
         patch('src.core.email_handler.settings', MockSettings):

        # Также нам нужно замокать start_scheduler у EmailTelegramForwarder,
        # чтобы он не запускал бесконечный цикл schedule.run_pending()
        with patch.object(EmailTelegramForwarder, 'start_scheduler') as mock_start_scheduler:

            # --- Инициализация теста ---
            print("Initializing memory tracking...")
            tracemalloc.start(25)

            print("Creating and starting system instance...")
            system = EmailTelegramSystem()
            system.start()

            # Даем время на запуск всех стартовых потоков (monitor, status, command)
            time.sleep(1)

            print("Taking initial snapshot...")
            gc.collect()
            initial_memory = get_process_memory()
            initial_snapshot = tracemalloc.take_snapshot()
            print(f"Initial memory usage: {initial_memory:.2f} MB")

            # --- Цикл нагрузки: многократный перезапуск ---
            print(f"\nRunning {iterations} restarts of the bot component...")
            start_time = time.time()
            for i in range(iterations):
                # Вызываем внутренний метод перезапуска, который мы хотим протестировать
                system._restart_bot()
                print(f"  Restart #{i + 1}/{iterations} initiated. Waiting for completion...")
                time.sleep(11)  # Ждем 11 секунд


            end_time = time.time()
            print(f"Loop finished in {end_time - start_time:.2f} seconds.")

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
                leak_per_restart = ((final_memory - initial_memory) * 1024) / iterations
                print(f"Leak per restart: {leak_per_restart:.2f} KB")
            print("=" * 40)

            # Сравниваем снимки, чтобы найти, что именно утекло
            print_top_comparison(final_snapshot, initial_snapshot)

            tracemalloc.stop()

            # --- Корректное завершение работы ---
            print("\nShutting down system...")
            system.stop()
            print("System restart leak test completed.")


if __name__ == "__main__":
    TEST_ITERATIONS = 20
    run_restart_leak_test(TEST_ITERATIONS)