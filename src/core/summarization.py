from typing import Optional, Dict, Any
import threading
from src.utils.logger import get_logger
from src.core.llm_engine import YandexAIEngine
from src.db.manager import DatabaseManager

logger = get_logger("summarization")

class SummarizationManager:
    """
    Manages text summarization functionality for the application.
    """
    _instance = None
    _lock = threading.Lock()

    def __new__(cls):
        """Implementation of Singleton pattern."""
        with cls._lock:
            if cls._instance is None:
                cls._instance = super(SummarizationManager, cls).__new__(cls)
        return cls._instance

    def __init__(self):
        """Initialize the summarization manager."""
        if hasattr(self, 'initialized'):
            return
            
        self.db_manager = DatabaseManager()
        self.llm_engine = YandexAIEngine()
        self.initialized = True
        logger.info("Summarization manager initialized")

    def _get_ai_client_instance(self):
        """
        Gets the instance of the AI client for summarization.

        Returns:
            The YandexAIEngine instance that can perform summarization
        """
        return self.llm_engine

    def can_summarize(self, chat_id: str, text_length: int) -> bool:
        """
        Determines if a text should be summarized based on user settings and text length.
        
        Args:
            chat_id: The Telegram chat ID of the user
            text_length: The length of the text to be summarized
            
        Returns:
            bool: True if summarization is allowed and appropriate, False otherwise
        """
        # Check minimum text length requirement (at least 200 characters)
        if text_length < 200:
            logger.debug(f"Text too short for summarization: {text_length} characters")
            return False
            
        # Get user summarization settings
        user_settings = self.db_manager.get_user_summarization_settings(chat_id)
        
        # If global summarization is disabled for this user, don't summarize
        if not user_settings.get('allow_summarization', False):
            logger.debug(f"Summarization disabled for user {chat_id}")
            return False
            
        return True

    def summarize_text(self, chat_id: str, subject: str, text: str) -> Optional[Dict[str, Any]]:
        """
        Summarizes a text using an AI model.

        Args:
            chat_id: The Telegram chat ID of the user
            subject: The subject of the email for which to get subject-specific settings
            text: The text to summarize

        Returns:
            Optional[Dict[str, Any]]: Dictionary with summarization results or None if failed
        """
        try:
            from src.config import settings

            # Get subject-specific summarization settings
            subject_settings = self.db_manager.get_subject_summarization_settings(chat_id, subject)

            # Get the prompt to use
            prompt_id = subject_settings.get('prompt_id')
            if prompt_id:
                prompt_obj = self.db_manager.get_summarization_prompt_by_id(prompt_id)
                prompt_text = prompt_obj['prompt_text'] if prompt_obj else None
            else:
                # Use default prompt if no specific prompt is set
                default_prompt = self.db_manager.get_default_summarization_prompt()
                prompt_text = default_prompt['prompt_text'] if default_prompt else None

            if not prompt_text:
                prompt_text = getattr(settings, 'DEFAULT_SUMMARIZATION_PROMPT',
                                      'Summarize the following email report concisely:')

            # Исправляем получение AI клиента
            ai_client = self._get_ai_client_instance()
            summary = ai_client.summarize(text, prompt_text)

            if not summary:
                logger.warning(f"Empty summary returned for user {chat_id}")
                return None

            # Return the results
            result = {
                'summary': summary,
                'original_text': text,
                'send_original': subject_settings.get('send_original', True)
            }

            logger.info(f"Successfully summarized text for user {chat_id}")
            return result

        except Exception as e:
            logger.error(f"Error summarizing text for user {chat_id}: {e}", exc_info=True)
            return None

    def toggle_user_summarization(self, chat_id: str, enable: bool) -> bool:
        """
        Enables or disables summarization for a user.

        Args:
            chat_id: The Telegram chat ID of the user
            enable: True to enable summarization, False to disable

        Returns:
            bool: True if the operation was successful, False otherwise
        """
        try:
            return self.db_manager.update_user_summarization_settings(chat_id, enable)
        except Exception as e:
            logger.error(f"Error toggling summarization for user {chat_id}: {e}", exc_info=True)
            return False

    def toggle_report_summarization(self, chat_id: str, subject: str, enable: bool) -> bool:
        """
        Enables or disables summarization for a specific email report subject.
        
        Args:
            chat_id: The Telegram chat ID of the user
            subject: The subject of the email report
            enable: True to enable summarization, False to disable
            
        Returns:
            bool: True if the operation was successful, False otherwise
        """
        try:
            return self.db_manager.update_subject_summarization(chat_id, subject, enable)
        except Exception as e:
            logger.error(f"Error toggling report summarization for user {chat_id}, subject {subject}: {e}", exc_info=True)
            return False
            
    def get_report_summarization_status(self, chat_id: str, subject: str) -> bool:
        """
        Gets the summarization status for a specific email report subject.
        
        Args:
            chat_id: The Telegram chat ID of the user
            subject: The subject of the email report
            
        Returns:
            bool: True if summarization is enabled for this report, False otherwise
        """
        try:
            return self.db_manager.get_subject_summarization_status(chat_id, subject)
        except Exception as e:
            logger.error(f"Error getting report summarization status for user {chat_id}, subject {subject}: {e}", exc_info=True)
            return False