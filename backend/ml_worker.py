import os
import json
import logging
import numpy as np
import torch
from shapely.geometry import Polygon, box
import cv2
import openslide

try:
    from instanseg import InstanSeg
    INSTANSEG_AVAILABLE = True
except ImportError:
    INSTANSEG_AVAILABLE = False

logger = logging.getLogger(__name__)

TILE_SIZE = 512       
OVERLAP = 64          
STRIDE = TILE_SIZE - (2 * OVERLAP) 

class MLWorker:
    def __init__(self):
        self.device = "cuda" if torch.cuda.is_available() else "cpu"
        self.model = None
        
        if INSTANSEG_AVAILABLE:
            try:
                self.model = InstanSeg(model_type="brightfield_nuclei", device=self.device, verbosity=0)
            except Exception as e:
                logger.error(f"Failed to load InstanSeg: {e}")

    def process_slide(self, slide_path: str, job_id: str) -> str:
        # if not self.model:
        #     return self._mock_inference(job_id)

        slide = openslide.OpenSlide(slide_path)
        w, h = slide.dimensions
        
        try:
            pixel_size = float(slide.properties.get(openslide.PROPERTY_NAME_MPP_X, 0.5))
        except:
            pixel_size = 0.5

        all_polygons = []
        
        cell_limit = 5000 
        
        for y in range(0, h, STRIDE):
            if len(all_polygons) >= cell_limit: break
            
            for x in range(0, w, STRIDE):
                if len(all_polygons) >= cell_limit: break

                read_x = x - OVERLAP
                read_y = y - OVERLAP
                
                valid_read_x = max(0, read_x)
                valid_read_y = max(0, read_y)
                
                tile = slide.read_region((valid_read_x, valid_read_y), 0, (TILE_SIZE, TILE_SIZE)).convert("RGB")
                tile_np = np.array(tile)
                
                tile_input = tile_np.transpose(2, 0, 1)

                try:
                    labeled_output, _ = self.model.eval_small_image(tile_input, pixel_size)
                    local_polys = self._mask_to_polygons(labeled_output)
                except Exception:
                    continue

                valid_box = box(
                    x, y, 
                    min(x + STRIDE, w), min(y + STRIDE, h)
                )
                
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

        output_filename = f"results_{job_id}.json"
        output_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), output_filename)
        
        with open(output_path, "w") as f:
            json.dump({
                "job_id": str(job_id),
                "slide": os.path.basename(slide_path),
                "cell_count": len(all_polygons),
                "polygons": all_polygons 
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

    def _mock_inference(self, job_id):
        import time, random
        time.sleep(3)
        mock_polys = []
        for _ in range(100):
            cx, cy = random.randint(0, 2000), random.randint(0, 2000)
            mock_polys.append([[cx, cy], [cx+20, cy+50], [cx-20, cy+50]])
            
        output_filename = f"results_{job_id}.json"
        output_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), output_filename)
        
        with open(output_path, "w") as f:
            json.dump({"job_id": job_id, "polygons": mock_polys}, f)
        return output_filename

worker = MLWorker()