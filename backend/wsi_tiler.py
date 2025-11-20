import os
import io
from threading import Lock
from typing import Dict, Any, List
import openslide
from PIL import Image
import logging

logger = logging.getLogger(__name__)

# Directory where your .svs files are stored
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "../data/inputs")

class WSITiler:
    def __init__(self):
        self._cache: Dict[str, openslide.OpenSlide] = {}
        self._lock = Lock()

    def list_slides(self) -> List[str]:
        """Scans the data directory for .svs files."""
        if not os.path.exists(DATA_DIR):
            return []
        return [f for f in os.listdir(DATA_DIR) if f.endswith(".svs") or f.endswith(".tif")]

    def _get_slide(self, filename: str) -> openslide.OpenSlide:
        path = os.path.join(DATA_DIR, filename)
        if not os.path.exists(path):
            raise FileNotFoundError(f"Slide not found: {path}")

        with self._lock:
            if filename not in self._cache:
                try:
                    slide = openslide.OpenSlide(path)
                    self._cache[filename] = slide
                    logger.info(f"Opened slide: {filename}")
                except Exception as e:
                    logger.error(f"Failed to open slide {filename}: {e}")
                    raise
            return self._cache[filename]

    def get_tile(self, filename: str, level: int, x: int, y: int, format: str = "jpeg") -> bytes:
        slide = self._get_slide(filename)
        tile_size = 256
        
        try:
            # Clamp level
            valid_level = min(level, slide.level_count - 1)
            
            # Calculate coordinates
            # Note: This logic assumes the frontend requests deep zoom levels 
            # where 0 is the zoomed-out view. OpenSlide is opposite (0 is high res).
            # We use a simplified mapping here for the 24h challenge.
            
            # OpenSlide Level 0 = High Res
            # If we want to support deep zoom properly, we usually just pass the 
            # requested level directly to openslide if the viewer is configured to match.
            
            downsample = int(slide.level_downsamples[valid_level])
            location_x = x * tile_size * downsample
            location_y = y * tile_size * downsample
            
            tile_img = slide.read_region(
                location=(location_x, location_y),
                level=valid_level,
                size=(tile_size, tile_size)
            )
            
            tile_img = tile_img.convert("RGB")
            buf = io.BytesIO()
            tile_img.save(buf, format=format.upper(), quality=85)
            return buf.getvalue()
            
        except Exception as e:
            logger.error(f"Error reading tile {filename} {level} {x} {y}: {e}")
            return self._get_blank_tile()

    def get_slide_info(self, filename: str) -> Dict[str, Any]:
        slide = self._get_slide(filename)
        return {
            "width": slide.dimensions[0],
            "height": slide.dimensions[1],
            "level_count": slide.level_count,
            "tile_size": 256,
            "format": "jpeg"
        }

    def _get_blank_tile(self) -> bytes:
        img = Image.new('RGB', (256, 256), color='gray')
        buf = io.BytesIO()
        img.save(buf, format="JPEG")
        return buf.getvalue()