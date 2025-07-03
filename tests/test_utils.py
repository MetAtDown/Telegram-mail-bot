# tests/test_utils.py
import os
import psutil
from unittest.mock import MagicMock

# --- MOCK EXTERNAL DEPENDENCIES ---
# Mock WeasyPrint to avoid actually generating PDFs
class MockWeasyHTML:
    def __init__(self, string=None, url=None):
        self.source = string or url

    def write_pdf(self, target):
        # Just create an empty file with mock content
        with open(target, 'wb') as f:
            f.write(b'PDF_MOCK_CONTENT')

# --- MOCK CLASSES ---
class MockSettings:
    EMAIL_ACCOUNT = "test@example.com"
    EMAIL_PASSWORD = "password"
    EMAIL_SERVER = "imap.example.com"
    TELEGRAM_TOKEN = "token123"
    CHECK_INTERVAL = 5
    DATABASE_PATH = "test_db.sqlite"

class MockImapResponse:
    def __init__(self, status="OK", data=None):
        self.status = status
        self.data = data or []

class MockImapConnection:
    def __init__(self):
        self.selected = False
        self.logged_in = False
        self.mailbox = "inbox"

    def login(self, username, password):
        self.logged_in = True
        return "OK", []

    def select(self, mailbox):
        self.selected = True
        self.mailbox = mailbox
        return "OK", []

    def search(self, *args):
        # Return 10 message IDs to process in a loop
        return "OK", [b"1 2 3 4 5 6 7 8 9 10"]

    def fetch(self, msg_id, params):
        raw_email = b"""From: sender@example.com
Subject: Test Subject
Date: Mon, 1 Jan 2023 12:00:00 +0000
Content-Type: text/html; charset=UTF-8

<html><body><p>Test content for leak analysis</p></body></html>"""
        return "OK", [(b'1 (FLAGS (\\Seen))', raw_email)]

    def store(self, msg_id, cmd, flags):
        return "OK", []

    def noop(self):
        return "OK", [b'NOOP successful.']

    def close(self):
        self.selected = False
        return "OK", []

    def logout(self):
        self.logged_in = False
        return "OK", []

class MockImapSsl:
    def __init__(self, host, timeout=None):
        self.host = host
        self.timeout = timeout
        self.connection = MockImapConnection()

    def __getattr__(self, name):
        # Delegate calls to the mock connection object
        return getattr(self.connection, name)

class MockBot:
    def send_message(self, chat_id, text, **kwargs):
        return MagicMock()

    def send_document(self, chat_id, document, **kwargs):
        # To avoid issues with file handles, ensure the mock consumes the "file"
        if hasattr(document, 'read'):
            document.read()
        return MagicMock()

    def send_photo(self, chat_id, photo, **kwargs):
        if hasattr(photo, 'read'):
            photo.read()
        return MagicMock()

    def get_me(self):
        mock = MagicMock()
        mock.username = "test_bot"
        mock.first_name = "Test Bot"
        return mock

class MockDBManager:
    def get_all_subjects(self):
        return {
            "Test Subject": [
                {"chat_id": "123456", "enabled": True, "delivery_mode": "html"},
                {"chat_id": "654321", "enabled": True, "delivery_mode": "pdf"},
            ]
        }

    def get_user_subjects(self, chat_id):
        return [
            ("Test Subject 1", "smart"),
            ("Another Important Report", "pdf")
        ]

    def get_user_summarization_settings(self, chat_id):
        return {'allow_summarization': True}

    def get_subject_summarization_status(self, chat_id, pattern):
        return False

    def get_user_delivery_settings(self, chat_id):
        return {'allow_delivery_mode_selection': True}

    def get_subject_summarization_settings(self, chat_id, subject):
        return {'send_original': True}

    def get_all_users(self):
        return {}

    def shutdown(self):
        pass  # Просто пустая заглушка

    def get_subject_summarization_status(self, chat_id, pattern):
        return False  # No summarization for tests

class MockSummarizationManager:
    def summarize_text(self, chat_id, subject, text):
        # В тесте нам не нужна реальная суммаризация.
        # Просто возвращаем None, чтобы не блокировать поток.
        return None

    def get_report_summarization_status(self, chat_id, subject):
        return False

class MockEmailBotHandler:
    """
    Легкий мок для EmailBotHandler.
    Самое главное - его метод start() не должен блокировать поток.
    """
    def __init__(self, db_manager):
        self.db_manager = db_manager
        # Мы не создаем реальный telebot, чтобы избежать лишних объектов
        self.bot = MockBot() # Используем наш уже существующий легкий мок

    def start(self):
        # В реальном коде здесь blocking-вызов вроде bot.polling()
        # В тесте мы просто имитируем работу и выходим.
        print(f"[{__class__.__name__}.start() called] -> Exiting immediately for test.")
        pass

    def stop(self):
        # Имитируем остановку
        print(f"[{__class__.__name__}.stop() called]")
        pass

# --- HELPER FUNCTIONS ---
def get_process_memory():
    """Return the memory usage of the current process in MB"""
    process = psutil.Process(os.getpid())
    return process.memory_info().rss / (1024 * 1024)