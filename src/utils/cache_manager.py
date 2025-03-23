import time
from pathlib import Path
import threading

from src.config import settings
from src.utils.logger import get_logger

# Setup logger
logger = get_logger("cache_manager")

# Lock for thread safety
_lock = threading.RLock()

# Path to the cache invalidation timestamp file
CACHE_TS_FILE = Path(settings.DATA_DIR) / '.cache_invalidation_ts'


def invalidate_caches():
    """
    Invalidate all caches by updating the global timestamp file.
    This function should be called whenever a database change is made.
    """
    with _lock:
        try:
            # Create the parent directory if it doesn't exist
            CACHE_TS_FILE.parent.mkdir(parents=True, exist_ok=True)

            # Write current timestamp to file
            with open(CACHE_TS_FILE, 'w') as f:
                timestamp = time.time()
                f.write(str(timestamp))
            logger.debug(f"Global cache invalidated at timestamp {timestamp}")
            return True
        except Exception as e:
            logger.error(f"Error invalidating cache timestamp: {e}")
            return False


def get_invalidation_timestamp():
    """
    Get the last cache invalidation timestamp.
    Returns 0 if file doesn't exist (forcing cache refresh).
    """
    with _lock:
        try:
            if not CACHE_TS_FILE.exists():
                return 0
            with open(CACHE_TS_FILE, 'r') as f:
                return float(f.read().strip())
        except Exception as e:
            logger.error(f"Error reading cache invalidation timestamp: {e}")
            return 0  # Force refresh on error


def is_cache_valid(cached_timestamp):
    """
    Check if a cache created at cached_timestamp is still valid.
    Returns True if cache is valid, False if it should be refreshed.
    """
    invalidation_ts = get_invalidation_timestamp()
    return cached_timestamp > invalidation_ts