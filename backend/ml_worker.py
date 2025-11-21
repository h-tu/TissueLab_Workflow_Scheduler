import os
import json
import logging
import numpy as np
import torch
from shapely.geometry import Polygon, box
import cv2
import openslide
import threading # <--- Needed for types

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

try:
    from instanseg import InstanSeg
    INSTANSEG_AVAILABLE = True
except ImportError:
    INSTANSEG_AVAILABLE = False
    logger.warning("InstanSeg not installed. Inference will be skipped or mocked.")

TILE_SIZE = 512       
OVERLAP = 64          
STRIDE = TILE_SIZE - (2 * OVERLAP) 

class MLWorker:
    def __init__(self):
        if torch.cuda.is_available():
            self.device = "cuda"
        elif torch.backends.mps.is_available():
            self.device = "mps"
        else:
            self.device = "cpu"
            
        logger.info(f"Initializing MLWorker. Target device: {self.device}")
        
        self.model = None
        if INSTANSEG_AVAILABLE:
            try:
                logger.info("Loading InstanSeg model (brightfield_nuclei)...")
                self.model = InstanSeg(model_type="brightfield_nuclei", device=self.device, verbosity=0)
                logger.info("✅ InstanSeg model loaded successfully.")
            except Exception as e:
                logger.error(f"❌ Failed to load InstanSeg: {e}")

    def _is_background(self, tile_np, threshold=220):
        gray = cv2.cvtColor(tile_np, cv2.COLOR_RGB2GRAY)
        mean_val = np.mean(gray)
        if mean_val > threshold:
            return True
        return False

    # --- UPDATED: Accepting cancel_event and progress_callback ---
    def process_slide(self, slide_path: str, job_id: str, job_type: str = "SEGMENTATION", 
                      cancel_event: threading.Event = None, 
                      progress_callback = None) -> str:
        
        filename = os.path.basename(slide_path)
        logger.info(f"🚀 [Job {job_id}] Starting processing. Type: {job_type}")

        if job_type == "TISSUE_MASK":
            return self._generate_tissue_mask(slide_path, job_id)

        if not self.model:
            raise RuntimeError("Model not loaded")

        try:
            slide = openslide.OpenSlide(slide_path)
            w, h = slide.dimensions
            
            try:
                pixel_size = float(slide.properties.get(openslide.PROPERTY_NAME_MPP_X, 0.5))
            except:
                pixel_size = 0.5

            all_polygons = []
            cell_limit = 5000 
            
            # --- PROGRESS CALCULATION SETUP ---
            rows = range(0, h, STRIDE)
            cols = range(0, w, STRIDE)
            total_tiles = len(rows) * len(cols)
            processed_tiles = 0
            
            logger.info(f"🔄 [Job {job_id}] Starting tiled inference. Total tiles: {total_tiles}")
            
            for y in rows:
                for x in cols:
                    # --- 1. CHECK CANCELLATION ---
                    if cancel_event and cancel_event.is_set():
                        logger.warning(f"🛑 [Job {job_id}] Cancellation detected. Aborting worker.")
                        raise InterruptedError("Job Cancelled by User")

                    # --- 2. UPDATE PROGRESS ---
                    processed_tiles += 1
                    if progress_callback and processed_tiles % 5 == 0: # Update every 5 tiles to reduce overhead
                        pct = int((processed_tiles / total_tiles) * 100)
                        progress_callback(pct)

                    if len(all_polygons) >= cell_limit: break

                    read_x = x - OVERLAP
                    read_y = y - OVERLAP
                    
                    valid_read_x = max(0, read_x)
                    valid_read_y = max(0, read_y)
                    
                    tile = slide.read_region((valid_read_x, valid_read_y), 0, (TILE_SIZE, TILE_SIZE)).convert("RGB")
                    tile_np = np.array(tile)

                    if self._is_background(tile_np):
                        continue
                    
                    tile_input = tile_np.transpose(2, 0, 1)

                    try:
                        labeled_output, _ = self.model.eval_small_image(tile_input, pixel_size)
                        
                        if isinstance(labeled_output, torch.Tensor):
                            labeled_output = labeled_output.detach().cpu().numpy()
                        
                        labeled_output = np.squeeze(labeled_output)
                        local_polys = self._mask_to_polygons(labeled_output)
                    
                    except Exception as e:
                        continue

                    valid_box = box(x, y, min(x + STRIDE, w), min(y + STRIDE, h))
                    
                    for poly_coords in local_polys:
                        global_poly = []
                        for (px, py) in poly_coords:
                            gx = px + valid_read_x
                            gy = py + valid_read_y
                            global_poly.append((gx, gy))
                        
                        if len(global_poly) < 3: continue
                        
                        poly_shape = Polygon(global_poly)
                        
                        if valid_box.contains(poly_shape.centroid):
                            all_polygons.append(global_poly)

            # Finalize progress
            if progress_callback: progress_callback(100)

            output_filename = f"results_{job_id}.json"
            output_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), output_filename)
            
            with open(output_path, "w") as f:
                json.dump({
                    "job_id": str(job_id),
                    "slide": filename,
                    "cell_count": len(all_polygons),
                    "polygons": all_polygons 
                }, f)
            
            return output_filename
            
        except Exception as e:
            logger.error(f"❌ [Job {job_id}] Error: {e}")
            raise e

    def _generate_tissue_mask(self, slide_path: str, job_id: str) -> str:
        return self._original_tissue_mask_logic(slide_path, job_id)

    def _original_tissue_mask_logic(self, slide_path, job_id):
        filename = os.path.basename(slide_path)
        slide = openslide.OpenSlide(slide_path)
        thumbnail = slide.get_thumbnail((2048, 2048))
        img_np = np.array(thumbnail)
        gray = cv2.cvtColor(img_np, cv2.COLOR_RGB2GRAY)
        blur = cv2.GaussianBlur(gray, (5,5), 0)
        _, mask = cv2.threshold(blur, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        scale_x = slide.dimensions[0] / thumbnail.size[0]
        scale_y = slide.dimensions[1] / thumbnail.size[1]
        tissue_polygons = []
        for cnt in contours:
            if cv2.contourArea(cnt) < 500: continue
            epsilon = 0.005 * cv2.arcLength(cnt, True)
            approx = cv2.approxPolyDP(cnt, epsilon, True)
            if len(approx) < 3: continue
            poly_points = []
            for point in approx:
                x_thumb, y_thumb = point[0]
                gx = int(x_thumb * scale_x)
                gy = int(y_thumb * scale_y)
                poly_points.append([gx, gy])
            tissue_polygons.append(poly_points)
        
        output_filename = f"results_{job_id}.json"
        output_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), output_filename)
        with open(output_path, "w") as f:
            json.dump({
                "job_id": str(job_id),
                "slide": filename,
                "cell_count": len(tissue_polygons),
                "polygons": tissue_polygons 
            }, f)
        return output_filename

    def _mask_to_polygons(self, label_mask):
        polys = []
        unique_ids = np.unique(label_mask)
        for uid in unique_ids:
            if uid == 0: continue
            binary_mask = (label_mask == uid).astype(np.uint8)
            contours, _ = cv2.findContours(binary_mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
            for cnt in contours:
                epsilon = 0.01 * cv2.arcLength(cnt, True)
                approx = cv2.approxPolyDP(cnt, epsilon, True)
                if len(approx) > 2:
                    polys.append(approx.reshape(-1, 2).tolist())
        return polys

worker = MLWorker()