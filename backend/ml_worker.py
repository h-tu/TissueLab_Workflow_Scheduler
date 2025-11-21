import os
import json
import logging
import glob
import numpy as np
import torch
from shapely.geometry import Polygon, box, shape
from shapely.ops import unary_union
import cv2
import openslide
import threading 

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

    def _is_background(self, tile_np, threshold=235):
        gray = cv2.cvtColor(tile_np, cv2.COLOR_RGB2GRAY)
        mean_val = np.mean(gray)
        std_dev = np.std(gray)
        if mean_val > threshold and std_dev < 15:
            return True
        return False

    def _save_json(self, job_id, filename, cells_data, user_id, job_type):
        """
        Updated to include user_id and job_type for smart lookups.
        """
        output_filename = f"results_{job_id}.json"
        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        output_path = os.path.join(base_dir, output_filename)
        
        temp_path = output_path + ".tmp"
        try:
            with open(temp_path, "w") as f:
                json.dump({
                    "job_id": str(job_id),
                    "user_id": user_id,    # <--- New
                    "job_type": job_type,  # <--- New
                    "slide": filename,
                    "cell_count": len(cells_data),
                    "cells": cells_data 
                }, f)
            os.replace(temp_path, output_path)
        except Exception as e:
            logger.error(f"Error saving intermediate result: {e}")

    def _find_latest_mask(self, slide_filename, user_id):
        """
        Scans previous results to find a TISSUE_MASK for this user/slide.
        Returns a Shapely MultiPolygon or None.
        """
        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        # Find all result json files
        files = glob.glob(os.path.join(base_dir, "results_*.json"))
        
        candidate_masks = []

        for fpath in files:
            try:
                with open(fpath, 'r') as f:
                    data = json.load(f)
                
                # Check if it matches our criteria
                if (data.get("user_id") == user_id and 
                    data.get("slide") == slide_filename and 
                    data.get("job_type") == "TISSUE_MASK" and
                    data.get("cells")):
                    
                    candidate_masks.append(data)
            except:
                continue
        
        if not candidate_masks:
            return None
            
        # Sort by job_id (timestamp rough proxy) or pick first. 
        # Assuming last one is best.
        latest_data = candidate_masks[-1]
        logger.info(f"🎭 Smart Connection: Found existing mask from Job {latest_data['job_id']}")
        
        # Convert list of polygons to Shapely shape
        try:
            polys = []
            for cell in latest_data["cells"]:
                # Handle both new structure (cell.polygon) and old (raw list)
                pts = cell.get("polygon", cell) 
                if len(pts) >= 3:
                    polys.append(Polygon(pts))
            
            if not polys: return None
            return unary_union(polys)
        except Exception as e:
            logger.error(f"Failed to parse mask: {e}")
            return None

    def process_slide(self, slide_path: str, job_id: str, job_type: str = "SEGMENTATION", 
                      cancel_event: threading.Event = None, 
                      progress_callback = None,
                      user_id: str = None) -> str:  # <--- Added user_id
        
        filename = os.path.basename(slide_path)
        logger.info(f"🚀 [Job {job_id}] Starting processing. Type: {job_type}")

        if job_type == "TISSUE_MASK":
            return self._generate_tissue_mask(slide_path, job_id, user_id)

        if not self.model:
            raise RuntimeError("Model not loaded")

        try:
            slide = openslide.OpenSlide(slide_path)
            w, h = slide.dimensions
            try:
                pixel_size = float(slide.properties.get(openslide.PROPERTY_NAME_MPP_X, 0.5))
            except:
                pixel_size = 0.5

            all_cells = []
            
            # --- SMART MASK CHECK ---
            tissue_mask_shape = None
            if job_type == "SEGMENTATION" and user_id:
                tissue_mask_shape = self._find_latest_mask(filename, user_id)
            
            rows = range(0, h, STRIDE)
            cols = range(0, w, STRIDE)
            total_tiles = len(rows) * len(cols)
            processed_tiles = 0
            skipped_tiles = 0 # Metric
            
            logger.info(f"🔄 [Job {job_id}] Starting inference. Tiles: {total_tiles}")
            
            for y in rows:
                for x in cols:
                    if cancel_event and cancel_event.is_set():
                        raise InterruptedError("Job Cancelled by User")
                    
                    # --- SMART SKIP LOGIC ---
                    if tissue_mask_shape:
                        tile_box = box(x, y, x + TILE_SIZE, y + TILE_SIZE)
                        # If tile does not touch the mask, skip it entirely
                        if not tissue_mask_shape.intersects(tile_box):
                            skipped_tiles += 1
                            processed_tiles += 1
                            continue 

                    processed_tiles += 1
                    if progress_callback and processed_tiles % 10 == 0: 
                        pct = int((processed_tiles / total_tiles) * 100)
                        progress_callback(pct)
                    
                    if processed_tiles % 50 == 0:
                        self._save_json(job_id, filename, all_cells, user_id, job_type)

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
                    except Exception:
                        continue

                    valid_box = box(x, y, min(x + STRIDE, w), min(y + STRIDE, h))
                    
                    for poly_coords in local_polys:
                        global_poly_coords = []
                        for (px, py) in poly_coords:
                            gx = px + valid_read_x
                            gy = py + valid_read_y
                            global_poly_coords.append((gx, gy))
                        
                        if len(global_poly_coords) < 3: continue
                        poly_shape = Polygon(global_poly_coords)
                        
                        if valid_box.contains(poly_shape.centroid):
                            cell_data = {
                                "polygon": global_poly_coords,
                                "area": round(poly_shape.area, 2),
                                "centroid": [round(poly_shape.centroid.x, 1), round(poly_shape.centroid.y, 1)],
                            }
                            all_cells.append(cell_data)

            if tissue_mask_shape:
                logger.info(f"⚡ Smart Optimization: Skipped {skipped_tiles} tiles based on mask.")

            if progress_callback: progress_callback(100)
            self._save_json(job_id, filename, all_cells, user_id, job_type)
            
            return f"results_{job_id}.json"
            
        except Exception as e:
            logger.error(f"❌ [Job {job_id}] Error: {e}")
            raise e

    def _generate_tissue_mask(self, slide_path: str, job_id: str, user_id: str) -> str:
        return self._original_tissue_mask_logic(slide_path, job_id, user_id)

    def _original_tissue_mask_logic(self, slide_path, job_id, user_id):
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
        
        tissue_cells = []
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
            
            tissue_cells.append({
                "polygon": poly_points,
                "area": cv2.contourArea(cnt) * scale_x * scale_y,
                "centroid": [0,0]
            })
        
        self._save_json(job_id, filename, tissue_cells, user_id, "TISSUE_MASK")
        return f"results_{job_id}.json"

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