import os
import json
import logging
import numpy as np
import torch
from shapely.geometry import Polygon, box
import cv2
import openslide

# Configure logging to ensure it shows up in the console
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

try:
    from instanseg import InstanSeg
    INSTANSEG_AVAILABLE = True
except ImportError:
    INSTANSEG_AVAILABLE = False
    logger.warning("InstanSeg not installed. Inference will be skipped or mocked if not handled.")

TILE_SIZE = 512       
OVERLAP = 64          
STRIDE = TILE_SIZE - (2 * OVERLAP) 

class MLWorker:
    def __init__(self):
        self.device = "cuda" if torch.cuda.is_available() else "cpu"
        logger.info(f"Initializing MLWorker. Target device: {self.device}")
        
        self.model = None
        
        if INSTANSEG_AVAILABLE:
            try:
                logger.info("Loading InstanSeg model (brightfield_nuclei)...")
                self.model = InstanSeg(model_type="brightfield_nuclei", device=self.device, verbosity=0)
                logger.info("✅ InstanSeg model loaded successfully.")
            except Exception as e:
                logger.error(f"❌ Failed to load InstanSeg: {e}")
        else:
            logger.warning("⚠️ InstanSeg is not available.")

    def process_slide(self, slide_path: str, job_id: str, job_type: str = "SEGMENTATION") -> str:
        filename = os.path.basename(slide_path)
        logger.info(f"🚀 [Job {job_id}] Starting processing. Type: {job_type}, File: {filename}")

        if job_type == "TISSUE_MASK":
            return self._generate_tissue_mask(slide_path, job_id)

        # Default: SEGMENTATION
        if not self.model:
            logger.error(f"🛑 [Job {job_id}] Model not loaded. Cannot perform segmentation.")
            # Optional: Fallback to mock if you want to test without model
            # return self._mock_inference(job_id)
            raise RuntimeError("Model not loaded")

        try:
            slide = openslide.OpenSlide(slide_path)
            w, h = slide.dimensions
            logger.info(f"📄 [Job {job_id}] Slide opened. Dimensions: {w}x{h}")
            
            try:
                pixel_size = float(slide.properties.get(openslide.PROPERTY_NAME_MPP_X, 0.5))
            except:
                pixel_size = 0.5
            logger.info(f"📏 [Job {job_id}] Using pixel size (MPP): {pixel_size}")

            all_polygons = []
            cell_limit = 5000 
            
            logger.info(f"🔄 [Job {job_id}] Starting tiled inference (Stride: {STRIDE}, Overlap: {OVERLAP})...")
            
            # Log progress every few rows to avoid spamming, but verify activity
            row_count = 0
            total_rows = (h // STRIDE) + 1

            for y in range(0, h, STRIDE):
                row_count += 1
                if row_count % 5 == 0:
                    logger.info(f"⏳ [Job {job_id}] Processing row {row_count}/{total_rows} (Cells found so far: {len(all_polygons)})")

                if len(all_polygons) >= cell_limit: 
                    logger.info(f"🛑 [Job {job_id}] Cell limit ({cell_limit}) reached. Stopping early.")
                    break
                
                for x in range(0, w, STRIDE):
                    if len(all_polygons) >= cell_limit: break

                    read_x = x - OVERLAP
                    read_y = y - OVERLAP
                    
                    valid_read_x = max(0, read_x)
                    valid_read_y = max(0, read_y)
                    
                    # Read region
                    tile = slide.read_region((valid_read_x, valid_read_y), 0, (TILE_SIZE, TILE_SIZE)).convert("RGB")
                    tile_np = np.array(tile)
                    tile_input = tile_np.transpose(2, 0, 1)

                    try:
                        labeled_output, _ = self.model.eval_small_image(tile_input, pixel_size)
                        local_polys = self._mask_to_polygons(labeled_output)
                    except Exception as e:
                        logger.warning(f"⚠️ [Job {job_id}] Inference error at x={x}, y={y}: {e}")
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

            logger.info(f"✅ [Job {job_id}] Inference complete. Total cells detected: {len(all_polygons)}")

            output_filename = f"results_{job_id}.json"
            output_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), output_filename)
            
            with open(output_path, "w") as f:
                json.dump({
                    "job_id": str(job_id),
                    "slide": filename,
                    "cell_count": len(all_polygons),
                    "polygons": all_polygons 
                }, f)
            
            logger.info(f"💾 [Job {job_id}] Results saved to {output_filename}")
            return output_filename
            
        except Exception as e:
            logger.error(f"❌ [Job {job_id}] Critical error in process_slide: {e}")
            raise e

    def _generate_tissue_mask(self, slide_path: str, job_id: str) -> str:
        filename = os.path.basename(slide_path)
        logger.info(f"🧬 [Job {job_id}] Generating Tissue Mask for {filename}...")
        
        try:
            slide = openslide.OpenSlide(slide_path)
            
            # 1. Thumbnail
            logger.info(f"🖼️ [Job {job_id}] Generating low-res thumbnail...")
            thumbnail = slide.get_thumbnail((2048, 2048))
            thumb_w, thumb_h = thumbnail.size
            
            # 2. Processing
            logger.info(f"⚙️ [Job {job_id}] Thresholding and finding contours...")
            img_np = np.array(thumbnail)
            gray = cv2.cvtColor(img_np, cv2.COLOR_RGB2GRAY)
            blur = cv2.GaussianBlur(gray, (5,5), 0)
            _, mask = cv2.threshold(blur, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
            
            contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
            
            # 3. Scaling
            scale_x = slide.dimensions[0] / thumb_w
            scale_y = slide.dimensions[1] / thumb_h
            
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

            logger.info(f"✅ [Job {job_id}] Tissue mask complete. Found {len(tissue_polygons)} regions.")

            # 4. Save
            output_filename = f"results_{job_id}.json"
            output_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), output_filename)
            
            with open(output_path, "w") as f:
                json.dump({
                    "job_id": str(job_id),
                    "slide": filename,
                    "cell_count": len(tissue_polygons),
                    "polygons": tissue_polygons 
                }, f)
                
            logger.info(f"💾 [Job {job_id}] Mask results saved to {output_filename}")
            return output_filename

        except Exception as e:
            logger.error(f"❌ [Job {job_id}] Error generating tissue mask: {e}")
            raise e

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