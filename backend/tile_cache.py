#-------------------------------------------------------------------------------
# File:        tile_cache.py
# Description: In-memory LRU cache service for storing generated WSI tiles.
# Author:      Hongyu Tu
# Created:     Nov 20, 2025
#-------------------------------------------------------------------------------

import time
import threading
import logging
from collections import OrderedDict
from typing import Optional

logger = logging.getLogger(__name__)

class TileCache:
    """
    In-memory LRU cache for storing generated JPEG tiles.
    Based on TissueLab's TileCacheService.
    """
    def __init__(self, max_size: int = 1000, max_age_seconds: int = 3600):
        self.max_size = max_size
        self.max_age_seconds = max_age_seconds
        self._lock = threading.RLock()
        self._cache = OrderedDict()
        self._timestamps = {}
        
        # Background thread to clean up expired tiles
        self._cleanup_thread = threading.Thread(target=self._periodic_cleanup, daemon=True)
        self._cleanup_thread.start()

    def get_tile(self, key: str) -> Optional[bytes]:
        with self._lock:
            if key not in self._cache:
                return None
            
            # Check if expired
            if time.time() - self._timestamps[key] > self.max_age_seconds:
                del self._cache[key]
                del self._timestamps[key]
                return None

            # Mark as recently used (move to end of OrderedDict)
            self._cache.move_to_end(key)
            return self._cache[key]

    def put_tile(self, key: str, data: bytes):
        with self._lock:
            # Evict oldest if cache is full
            while len(self._cache) >= self.max_size:
                oldest = next(iter(self._cache))
                del self._cache[oldest]
                del self._timestamps[oldest]
            
            self._cache[key] = data
            self._timestamps[key] = time.time()
            self._cache.move_to_end(key)

    def _periodic_cleanup(self):
        """Runs every 5 minutes to remove expired entries"""
        while True:
            time.sleep(300)
            with self._lock:
                now = time.time()
                expired = [k for k, ts in self._timestamps.items() if now - ts > self.max_age_seconds]
                for k in expired:
                    if k in self._cache: del self._cache[k]
                    if k in self._timestamps: del self._timestamps[k]