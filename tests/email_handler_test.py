# tests/email_handler_test.py

import sys
import os
import time
import tracemalloc
import gc
import linecache
from unittest.mock import patch, MagicMock

# --- 1. Настройка путей для импортов ---
# Добавляем корневую папку проекта в sys.path, чтобы можно было импортировать 'src'
script_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(script_dir)
if project_root not in sys.path:
    sys.path.insert(0, project_root)

# --- 2. Импорт моков и утилит из нашего нового файла ---
from tests.test_utils import (
    MockSettings, MockBot, MockImapSsl, MockDBManager, MockWeasyHTML,
    get_process_memory, MockSummarizationManager
)

# --- 3. Мокирование зависимостей ДО импорта основного класса ---
# Это нужно сделать до того, как Python попытается загрузить 'src.core.email_handler'
sys.modules['weasyprint'] = MagicMock()
sys.modules['weasyprint'].HTML = MockWeasyHTML

# --- 4. Импорт тестируемого класса ---
from src.core.email_handler import EmailTelegramForwarder


# --- Функции для анализа памяти ---
def print_top_comparison(snapshot, old_snapshot, limit=20):
    """Более информативный вывод сравнения снимков tracemalloc."""
    print(f"\n--- TOP {limit} NEW MEMORY ALLOCATIONS (DIFFERENCE) ---")
    stats = snapshot.compare_to(old_snapshot, 'lineno')

    for i, stat in enumerate(stats[:limit], 1):
        frame = stat.traceback[0]
        filename = os.path.basename(frame.filename)
        # Получаем строку кода для контекста
        line_content = linecache.getline(frame.filename, frame.lineno).strip()

        print(f"#{i}: {filename}:{frame.lineno}")
        print(f"   Line: `{line_content}`")
        print(f"   New memory: {stat.size_diff / 1024:.1f} KB in {stat.count_diff} new blocks")
        print(f"   Total size: {stat.size / 1024:.1f} KB ({stat.count} blocks total)")
        print("-" * 20)
    linecache.clearcache()  # Очищаем кэш строк файла


def run_focused_memory_test(iterations: int):
    """
    Запускает сфокусированный тест на утечку памяти,
    многократно вызывая основной рабочий метод.
    """
    with patch.object(EmailTelegramForwarder, '_check_rate_limit', return_value=True), \
         patch('src.core.email_handler.settings', MockSettings), \
         patch('src.core.email_handler.telebot.TeleBot', return_value=MockBot()), \
         patch('src.core.email_handler.SummarizationManager', return_value=MockSummarizationManager()), \
         patch('imaplib.IMAP4_SSL', MockImapSsl):

        print("Starting focused memory leak test...")
        print(f"Will run {iterations} iterations of the main processing loop.")

        # --- Инициализация ---
        tracemalloc.start(25)

        forwarder = EmailTelegramForwarder(db_manager=MockDBManager())
        forwarder.IMAP_CLASS = MockImapSsl

        print("Warming up and taking initial snapshot...")
        forwarder.process_emails()  # "Прогревочный" вызов
        gc.collect()

        initial_memory = get_process_memory()
        initial_snapshot = tracemalloc.take_snapshot()

        print(f"Initial memory usage: {initial_memory:.2f} MB")

        # --- Основной цикл нагрузки ---
        print("\nRunning main processing loop...")
        start_time = time.time()
        for i in range(iterations):
            forwarder.process_emails()

            if (i + 1) % (iterations // 50 or 1) == 0:
                progress = (i + 1) / iterations
                print(f"  Progress: [{'#' * int(progress * 20):<20}] {i + 1}/{iterations}", end='\r')

        end_time = time.time()
        print(f"\nLoop finished in {end_time - start_time:.2f} seconds.")

        # --- Сбор и анализ результатов ---
        print("Taking final snapshot and analyzing results...")
        gc.collect()

        final_memory = get_process_memory()
        final_snapshot = tracemalloc.take_snapshot()

        print("\n" + "=" * 40)
        print("--- MEMORY USAGE SUMMARY (psutil) ---")
        print(f"Initial memory: {initial_memory:.2f} MB")
        print(f"Final memory:   {final_memory:.2f} MB")
        print(f"Difference:     {final_memory - initial_memory:.2f} MB")
        if iterations > 0:
            leak_per_iter_kb = ((final_memory - initial_memory) * 1024) / iterations
            print(f"Leak per iteration: {leak_per_iter_kb:.2f} KB")
        print("=" * 40)

        # Сравниваем снимки, чтобы найти именно то, что утекло
        print_top_comparison(final_snapshot, initial_snapshot)

        tracemalloc.stop()

        print("\nShutting down forwarder...")
        forwarder.shutdown()
        print("Memory leak test completed.")


# --- Точка входа в скрипт ---
if __name__ == "__main__":
    # Начните с 5000. Если утечка мала, увеличьте до 20000 или 50000.
    TEST_ITERATIONS = 5000
    run_focused_memory_test(TEST_ITERATIONS)