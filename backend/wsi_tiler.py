import os
import io
from threading import Lock
from typing import Dict, Any, List
import openslide
from PIL import Image
import logging

# Import the new cache service
from .tile_cache import TileCache

logger = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.normpath(os.path.join(BASE_DIR, "../data/inputs"))

class WSITiler:
    def __init__(self):
        self._slide_cache: Dict[str, openslide.OpenSlide] = {}
        self._lock = Lock()
        
        # Initialize the LRU Cache (Store 2000 tiles, expire after 1 hour)
        self.tile_cache = TileCache(max_size=2000, max_age_seconds=3600)

    def list_slides(self) -> List[Dict[str, Any]]:
        if not os.path.exists(DATA_DIR):
            return []
        
        files = [f for f in os.listdir(DATA_DIR) if f.lower().endswith((".svs", ".tif", ".tiff", ".ndpi"))]
        results = []

        for f in files:
            file_path = os.path.join(DATA_DIR, f)
            try:
                size_bytes = os.path.getsize(file_path)
                size_mb = size_bytes / (1024 * 1024)
                if size_mb > 1000:
                    size_str = f"{size_mb/1024:.1f} GB"
                else:
                    size_str = f"{size_mb:.0f} MB"

                dims = "Unknown"
                if f in self._slide_cache:
                    w, h = self._slide_cache[f].dimensions
                    dims = f"{w} x {h}"
                else:
                    with openslide.OpenSlide(file_path) as temp_slide:
                        w, h = temp_slide.dimensions
                        dims = f"{w} x {h}"

                results.append({
                    "name": f,
                    "size": size_str,
                    "dimensions": dims
                })

            except Exception as e:
                logger.error(f"Error reading metadata for {f}: {e}")
                results.append({
                    "name": f, 
                    "size": "Error", 
                    "dimensions": "N/A"
                })
                
        return results

    def _get_slide(self, filename: str) -> openslide.OpenSlide:
        path = os.path.join(DATA_DIR, filename)
        if not os.path.exists(path):
            raise FileNotFoundError(f"Slide not found at: {path}")

        with self._lock:
            if filename not in self._slide_cache:
                slide = openslide.OpenSlide(path)
                self._slide_cache[filename] = slide
            return self._slide_cache[filename]

    def get_tile(self, filename: str, level: int, x: int, y: int, format: str = "jpeg") -> bytes:
        # 1. Check Memory Cache First
        cache_key = f"{filename}_{level}_{x}_{y}_{format}"
        cached_data = self.tile_cache.get_tile(cache_key)
        if cached_data:
            return cached_data

        # 2. Generate Tile if not in cache
        try:
            slide = self._get_slide(filename)
        except FileNotFoundError:
            return self._get_blank_tile()
            
        tile_size = 256
        
        try:
            max_osd_level = slide.level_count - 1
            expected_downsample = 2 ** (max_osd_level - level)
            
            location_x = int(x * tile_size * expected_downsample)
            location_y = int(y * tile_size * expected_downsample)
            
            best_level = 0
            for i in range(slide.level_count):
                if slide.level_downsamples[i] <= expected_downsample:
                    best_level = i
                else:
                    break
            
            actual_downsample = slide.level_downsamples[best_level]
            region_width = int((tile_size * expected_downsample) / actual_downsample)
            region_height = int((tile_size * expected_downsample) / actual_downsample)
            
            tile_img = slide.read_region(
                (location_x, location_y), 
                best_level, 
                (region_width, region_height)
            )
            
            if region_width != tile_size or region_height != tile_size:
                tile_img = tile_img.resize((tile_size, tile_size), Image.Resampling.LANCZOS)
            
            bg = Image.new("RGB", tile_img.size, (255, 255, 255))
            if tile_img.mode == 'RGBA':
                bg.paste(tile_img, mask=tile_img.split()[3])
            else:
                bg.paste(tile_img)
                
            buf = io.BytesIO()
            bg.save(buf, format=format.upper(), quality=85)
            tile_bytes = buf.getvalue()

            # 3. Save to Cache
            self.tile_cache.put_tile(cache_key, tile_bytes)
            
            return tile_bytes
            
        except Exception:
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