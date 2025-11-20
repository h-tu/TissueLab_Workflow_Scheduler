import os
import io
from threading import Lock
from typing import Dict, Any
import openslide
from PIL import Image
import logging

logger = logging.getLogger(__name__)

# Directory where your .svs files are stored
DATA_DIR = "../data/inputs"

class WSITiler:
    def __init__(self):
        self._cache: Dict[str, openslide.OpenSlide] = {}
        self._lock = Lock()

    def _get_slide(self, filename: str) -> openslide.OpenSlide:
        """
        Retrieves an OpenSlide object from cache or opens it.
        Thread-safe because FastAPI uses thread pools for synchronous I/O.
        """
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
        """
        Fetches a tile for Deep Zoom (OSD).
        Note: OpenSeadragon requests tiles based on 'level' and grid coordinates (x, y).
        """
        slide = self._get_slide(filename)
        
        # Standard Deep Zoom logic:
        # 1. Calculate the tile size (usually 254 or 256 + overlap). 
        #    We will stick to standard 256 for simplicity.
        tile_size = 256
        
        # 2. OpenSlide's 'read_region' takes coordinates at Level 0 (highest res).
        #    We need to map the requested tile (level, x, y) to Level 0 coordinates.
        
        # Get the downsample factor for this level
        # OpenSlide levels go 0 (full), 1 (1/4), 2 (1/16)...
        # But OSD requests often go 0 (1px), ... N (full). 
        # We need to map OSD's zoom level to OpenSlide's level.
        
        # SIMPLIFICATION FOR 24H CHALLENGE:
        # We will assume 'level' passed here matches OpenSlide levels (0 is high res).
        # If your frontend sends OSD levels (where 0 is zoomed out), we'd need to invert.
        # For now, we assume standard /z/x/y.
        
        try:
            # Helper logic to handle if requested level doesn't exist (zoom out too far)
            # We clamp to the max available level in the slide
            valid_level = min(level, slide.level_count - 1)
            
            # Convert Grid (x,y) to Pixel Coordinates at that level
            tile_x = x * tile_size
            tile_y = y * tile_size
            
            # Read the region
            # Note: read_region expects (x, y) at LEVEL 0.
            # We must multiply by the downsample factor of the requested level.
            downsample = int(slide.level_downsamples[valid_level])
            location_x = tile_x * downsample
            location_y = tile_y * downsample
            
            tile_img = slide.read_region(
                location=(location_x, location_y),
                level=valid_level,
                size=(tile_size, tile_size)
            )
            
            # Convert to RGB (OpenSlide is RGBA)
            tile_img = tile_img.convert("RGB")
            
            # Return bytes
            buf = io.BytesIO()
            tile_img.save(buf, format=format.upper(), quality=85)
            return buf.getvalue()
            
        except Exception as e:
            logger.error(f"Error reading tile {filename} {level} {x} {y}: {e}")
            # Return a blank tile on error to not crash the viewer
            return self._get_blank_tile()

    def get_slide_info(self, filename: str) -> Dict[str, Any]:
        """Returns metadata needed by OpenSeadragon to initialize."""
        slide = self._get_slide(filename)
        return {
            "width": slide.dimensions[0],
            "height": slide.dimensions[1],
            "level_count": slide.level_count,
            "tile_size": 256,
            "format": "jpeg"
        }

    def _get_blank_tile(self) -> bytes:
        img = Image.new('RGB', (256, 256), color='white')
        buf = io.BytesIO()
        img.save(buf, format="JPEG")
        return buf.getvalue()