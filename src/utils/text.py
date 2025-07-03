import re

def escape_markdown_v2(text: str) -> str:
    """
    Экранирует специальные символы для режима parse_mode='MarkdownV2' Telegram.

    Args:
        text: Исходный текст.

    Returns:
        Текст с экранированными символами.
    """
    if not isinstance(text, str):
        text = str(text)

    # The characters that need to be escaped are: _ * [ ] ( ) ~ ` > # + - = | { } . !
    escape_chars = r'_*[]()~`>#+-=|{}.!'
    return re.sub(f'([{re.escape(escape_chars)}])', r'\\\1', text)
