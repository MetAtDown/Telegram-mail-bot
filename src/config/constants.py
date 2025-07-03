"""
Централизованное хранилище констант проекта.
"""

# --- Режимы доставки ---
DELIVERY_MODE_TEXT = 'text'
DELIVERY_MODE_HTML = 'html'
DELIVERY_MODE_SMART = 'smart'
DELIVERY_MODE_PDF = 'pdf'

# Режим доставки по умолчанию
DEFAULT_DELIVERY_MODE = DELIVERY_MODE_SMART

# Множество всех разрешенных режимов доставки для быстрой проверки
ALLOWED_DELIVERY_MODES = {
    DELIVERY_MODE_TEXT,
    DELIVERY_MODE_HTML,
    DELIVERY_MODE_SMART,
    DELIVERY_MODE_PDF
}