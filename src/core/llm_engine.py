import requests
import json
import threading
from datetime import datetime, timezone, timedelta
from src.config import settings
from src.utils.logger import get_logger

logger = get_logger("llm_engine")

class YandexAIEngine:
    """
    A client for interacting with the Yandex AI API for text summarization.
    """
    def __init__(self):
        self.folder_id = settings.YANDEX_FOLDER_ID
        self.oauth_token = settings.YANDEX_OAUTH_TOKEN
        self.base_url = settings.YANDEX_AI_BASE_URL
        self.model_name = settings.YANDEX_AI_MODEL
        self.default_prompt = settings.DEFAULT_SUMMARIZATION_PROMPT
        
        # IAM token management
        self.iam_token = None
        self.token_expires_at = datetime.now(timezone.utc)
        self.token_lock = threading.Lock()
        
        # Complete API endpoint for completion
        self.completion_endpoint = f"{self.base_url}/completion"
        
        # Model URI
        self.model_uri = f"gpt://{self.folder_id}/{self.model_name}"

        if not all([self.folder_id, self.oauth_token, self.base_url]):
            logger.error("Yandex AI API: Folder ID, OAuth Token, or API Base URL is not configured.")
            raise ValueError("Yandex AI credentials or endpoint not fully configured in settings.")
        
        # Initialize IAM token
        self.refresh_iam_token()

    def refresh_iam_token(self):
        """Exchange OAuth token for an IAM token"""
        logger.debug("Attempting to refresh the IAM token...")
        refresh_url = "https://iam.api.cloud.yandex.net/iam/v1/tokens"
        payload = {"yandexPassportOauthToken": self.oauth_token}

        try:
            response = requests.post(refresh_url, json=payload)
            response.raise_for_status()
            response_json = response.json()
            self.iam_token = response_json.get("iamToken")
            expires_at = response_json.get("expiresAt")

            if expires_at:
                formatted_expires_at = expires_at.replace('Z', '+00:00')
                self.token_expires_at = datetime.fromisoformat(formatted_expires_at)
            else:
                self.token_expires_at = datetime.now(timezone.utc)

            logger.debug("IAM token refreshed successfully")
            return True
        except Exception as e:
            logger.error(f"Error refreshing IAM token: {e}", exc_info=True)
            return False
    
    def ensure_valid_token(self):
        """Ensure we have a valid IAM token, refresh if needed"""
        with self.token_lock:
            # Check if token is expired or will expire in the next minute
            if not self.iam_token or datetime.now(timezone.utc) + timedelta(minutes=1) >= self.token_expires_at:
                if not self.refresh_iam_token():
                    raise ValueError("Failed to obtain a valid IAM token for Yandex AI API")

    def summarize(self, text: str, prompt: str = None) -> str:
        """
        Summarizes the given text using the Yandex AI API.

        Args:
            text: The text to summarize.
            prompt: An optional custom prompt to use for summarization.
                    If None, the default prompt from settings will be used.

        Returns:
            The summarized text.

        Raises:
            requests.exceptions.RequestException: If the API request fails.
            ValueError: If the API response is invalid or indicates an error.
        """
        current_prompt = prompt if prompt else self.default_prompt
        if not text:
            logger.warning("Summarization called with empty text.")
            return ""

        # Ensure we have a valid token
        self.ensure_valid_token()

        headers = {
            "Authorization": f"Bearer {self.iam_token}",
            "Content-Type": "application/json"
        }
        
        payload = {
            "modelUri": self.model_uri,
            "completionOptions": {
                "stream": False,
                "temperature": 0.3,  # Adjust for more/less creative summaries
                "maxTokens": "1000"   # Max tokens for the summary
            },
            "messages": [
                {
                    "role": "system",
                    "text": current_prompt
                },
                {
                    "role": "user",
                    "text": text
                }
            ]
        }

        logger.debug(f"Sending summarization request to Yandex AI. Endpoint: {self.completion_endpoint}, Model: {self.model_uri}")
        try:
            response = requests.post(self.completion_endpoint, headers=headers, json=payload, timeout=60)  # 60s timeout
            response.raise_for_status()  # Raises an HTTPError for bad responses (4XX or 5XX)
        except requests.exceptions.Timeout:
            logger.error("Yandex AI API request timed out.")
            raise
        except requests.exceptions.RequestException as e:
            logger.error(f"Yandex AI API request failed: {e}")
            raise

        try:
            result = response.json()
            logger.debug(f"Yandex AI API raw response: {result}")
        except json.JSONDecodeError:
            logger.error(f"Failed to decode JSON response from Yandex AI: {response.text}")
            raise ValueError("Invalid JSON response from Yandex AI API")

        if 'result' in result and 'alternatives' in result['result'] and \
           len(result['result']['alternatives']) > 0 and 'message' in result['result']['alternatives'][0] and \
           'text' in result['result']['alternatives'][0]['message']:
            summary = result['result']['alternatives'][0]['message']['text']
            logger.info(f"Successfully received summary from Yandex AI. Length: {len(summary)}")
            return summary.strip()
        elif 'error' in result:
            error_details = result['error']
            logger.error(f"Yandex AI API returned an error: {error_details}")
            raise ValueError(f"Yandex AI API error: {error_details.get('message', 'Unknown error')}")
        else:
            logger.error(f"Unexpected Yandex AI API response structure: {result}")
            raise ValueError("Could not extract summary from Yandex AI API response")


if __name__ == '__main__':
    # This is for basic testing if you run this file directly
    class MockSettings:
        YANDEX_FOLDER_ID = "YOUR_FOLDER_ID"  # Replace with your folder ID for testing
        YANDEX_OAUTH_TOKEN = "YOUR_OAUTH_TOKEN"  # Replace with your OAuth token for testing
        YANDEX_AI_BASE_URL = "https://llm.api.cloud.yandex.net/foundationModels/v1"
        YANDEX_AI_MODEL = "yandexgpt-lite"
        DEFAULT_SUMMARIZATION_PROMPT = "Summarize the following text concisely and highlight key points:"
        LOG_LEVEL = "DEBUG"
        LOG_PATH = "logs"  # Ensure this directory exists or adjust

    settings = MockSettings()  # Override settings for local test

    if not settings.YANDEX_OAUTH_TOKEN.startswith("YOUR_"):
        try:
            engine = YandexAIEngine()
            sample_text = (
                "This is a long email report about the quarterly financial performance. "
                "Our revenue increased by 15% compared to the previous quarter. "
                "The main contributors were our new product line and expanded market reach. "
                "However, we faced some challenges with supply chain disruptions that impacted our margins. "
                "We need to address these issues in the next quarter by finding alternative suppliers. "
                "The R&D team has made significant progress on our next-generation product, with beta testing scheduled for next month. "
                "Customer satisfaction metrics remained high at 92%, though we saw a slight decrease in repeat purchases. "
                "Please review the attached detailed reports and provide feedback by next Friday."
            )
            
            summary = engine.summarize(sample_text)
            print("\nGENERATED SUMMARY:")
            print("-----------------")
            print(summary)
            print("-----------------")
        except Exception as e:
            print(f"Error testing Yandex AI engine: {e}")
    else:
        print("Please replace YOUR_FOLDER_ID and YOUR_OAUTH_TOKEN in the MockSettings class to test llm_engine.py directly.")