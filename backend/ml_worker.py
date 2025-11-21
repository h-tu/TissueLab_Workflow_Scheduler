#-------------------------------------------------------------------------------
# File:        ml_worker.py
# Description: Executes all long-running image processing jobs (Segmentation, Tissue Mask, Report, Visualization).
# Author:      Hongyu Tu
# Created:     Nov 20, 2025
#-------------------------------------------------------------------------------

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
import time

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
        self.model = None
        if INSTANSEG_AVAILABLE:
            try:
                self.model = InstanSeg(model_type="brightfield_nuclei", device=self.device, verbosity=0)
            except Exception as e:
                logger.error(f"❌ Failed to load InstanSeg: {e}")

    def _is_background(self, tile_np, threshold=235):
        gray = cv2.cvtColor(tile_np, cv2.COLOR_RGB2GRAY)
        mean_val = np.mean(gray)
        std_dev = np.std(gray)
        return mean_val > threshold and std_dev < 15

    def _save_json(self, job_id, filename, cells_data, user_id, job_type, extra_data=None):
        output_filename = f"results_{job_id}.json"
        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        output_path = os.path.join(base_dir, output_filename)
        temp_path = output_path + ".tmp"
        
        content = {
            "job_id": str(job_id),
            "user_id": user_id,
            "job_type": job_type,
            "slide": filename,
            "cell_count": len(cells_data) if isinstance(cells_data, list) else 0,
            "cells": cells_data
        }
        if extra_data: content.update(extra_data)

        try:
            with open(temp_path, "w") as f: json.dump(content, f)
            os.replace(temp_path, output_path)
        except Exception as e:
            logger.error(f"Error saving result: {e}")

    def _find_latest_mask(self, slide_filename, user_id):
        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        files = glob.glob(os.path.join(base_dir, "results_*.json"))
        candidate_masks = []
        for fpath in files:
            try:
                with open(fpath, 'r') as f: data = json.load(f)
                if (data.get("user_id") == user_id and 
                    data.get("slide") == slide_filename and 
                    data.get("job_type") == "TISSUE_MASK" and
                    data.get("cells")):
                    candidate_masks.append(data)
            except: continue
        
        if not candidate_masks: return None
        latest_data = candidate_masks[-1]
        
        try:
            polys = []
            for cell in latest_data["cells"]:
                pts = cell.get("polygon", cell)
                if len(pts) >= 3: polys.append(Polygon(pts))
            if not polys: return None
            return unary_union(polys)
        except: return None

    def process_slide(self, slide_path: str, job_id: str, job_type: str = "SEGMENTATION", 
                      cancel_event: threading.Event = None, 
                      progress_callback = None,
                      user_id: str = None) -> str:
        
        filename = os.path.basename(slide_path)
        logger.info(f"🚀 [Job {job_id}] Starting {job_type}")

        # --- ROUTING ---
        if job_type == "TISSUE_MASK":
            return self._generate_tissue_mask(slide_path, job_id, user_id)
        elif job_type == "VISUALIZATION":
            return self._generate_visualization(slide_path, job_id, user_id, progress_callback)
        elif job_type == "REPORT":
            return self._generate_report(slide_path, job_id, user_id, progress_callback)

        # --- SEGMENTATION LOGIC ---
        if not self.model: raise RuntimeError("Model not loaded")
        
        slide = openslide.OpenSlide(slide_path)
        w, h = slide.dimensions
        try: pixel_size = float(slide.properties.get(openslide.PROPERTY_NAME_MPP_X, 0.5))
        except: pixel_size = 0.5

        all_cells = []
        tissue_mask_shape = None
        if job_type == "SEGMENTATION" and user_id:
            tissue_mask_shape = self._find_latest_mask(filename, user_id)

        rows = range(0, h, STRIDE)
        cols = range(0, w, STRIDE)
        total_tiles = len(rows) * len(cols)
        processed = 0

        for y in rows:
            for x in cols:
                if cancel_event and cancel_event.is_set(): raise InterruptedError("Job Cancelled")
                
                if tissue_mask_shape:
                    if not tissue_mask_shape.intersects(box(x, y, x+TILE_SIZE, y+TILE_SIZE)):
                        processed += 1; continue

                processed += 1
                if progress_callback and processed % 10 == 0: 
                    progress_callback(int((processed/total_tiles)*100))
                if processed % 50 == 0:
                    self._save_json(job_id, filename, all_cells, user_id, job_type)

                read_x, read_y = x - OVERLAP, y - OVERLAP
                tile = slide.read_region((max(0,read_x), max(0,read_y)), 0, (TILE_SIZE, TILE_SIZE)).convert("RGB")
                tile_np = np.array(tile)

                if self._is_background(tile_np): continue
                
                try:
                    output, _ = self.model.eval_small_image(tile_np.transpose(2,0,1), pixel_size)
                    if isinstance(output, torch.Tensor): output = output.detach().cpu().numpy()
                    local_polys = self._mask_to_polygons(np.squeeze(output))
                except: continue

                valid_box = box(x, y, min(x+STRIDE, w), min(y+STRIDE, h))
                for pts in local_polys:
                    g_pts = [(p[0]+max(0,read_x), p[1]+max(0,read_y)) for p in pts]
                    if len(g_pts)<3: continue
                    poly = Polygon(g_pts)
                    if valid_box.contains(poly.centroid):
                        all_cells.append({
                            "polygon": g_pts,
                            "area": round(poly.area, 2),
                            "centroid": [round(poly.centroid.x, 1), round(poly.centroid.y, 1)]
                        })

        if progress_callback: progress_callback(100)
        self._save_json(job_id, filename, all_cells, user_id, job_type)
        return f"results_{job_id}.json"

    def _generate_tissue_mask(self, slide_path, job_id, user_id):
        # (Existing logic, simplified for brevity)
        filename = os.path.basename(slide_path)
        slide = openslide.OpenSlide(slide_path)
        thumb = np.array(slide.get_thumbnail((2048, 2048)))
        gray = cv2.cvtColor(thumb, cv2.COLOR_RGB2GRAY)
        _, mask = cv2.threshold(cv2.GaussianBlur(gray,(5,5),0), 0, 255, cv2.THRESH_BINARY_INV+cv2.THRESH_OTSU)
        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        sx = slide.dimensions[0] / thumb.shape[1]
        sy = slide.dimensions[1] / thumb.shape[0]
        cells = []
        for cnt in contours:
            if cv2.contourArea(cnt) < 500: continue
            approx = cv2.approxPolyDP(cnt, 0.005*cv2.arcLength(cnt,True), True)
            if len(approx) < 3: continue
            cells.append({
                "polygon": [[int(p[0][0]*sx), int(p[0][1]*sy)] for p in approx],
                "area": cv2.contourArea(cnt)*sx*sy
            })
        self._save_json(job_id, filename, cells, user_id, "TISSUE_MASK")
        return f"results_{job_id}.json"

    # --- NEW: VISUALIZATION TASK ---
    def _generate_visualization(self, slide_path, job_id, user_id, cb):
        """Generates a color histogram or heatmap representation."""
        filename = os.path.basename(slide_path)
        slide = openslide.OpenSlide(slide_path)
        
        if cb: cb(20)
        # Simulate processing time for demo
        time.sleep(2) 
        
        thumb = np.array(slide.get_thumbnail((1024, 1024)))
        if cb: cb(50)
        
        # Simple Image Processing: CLAHE (Contrast Limited Adaptive Histogram Equalization)
        lab = cv2.cvtColor(thumb, cv2.COLOR_RGB2LAB)
        l, a, b = cv2.split(lab)
        clahe = cv2.createCLAHE(clipLimit=3.0, tileGridSize=(8,8))
        cl = clahe.apply(l)
        limg = cv2.merge((cl,a,b))
        final = cv2.cvtColor(limg, cv2.COLOR_LAB2RGB)
        
        # We don't actually save the image in this strict JSON architecture, 
        # but we return "cells" as dummy regions to visualize the 'Process'
        if cb: cb(100)
        
        # Fake "result" metadata
        meta = {"info": "Histogram Equalization Complete", "resolution": "1024x1024"}
        self._save_json(job_id, filename, [], user_id, "VISUALIZATION", meta)
        return f"results_{job_id}.json"

    # --- NEW: REPORT TASK ---
    def _generate_report(self, slide_path, job_id, user_id, cb):
        """Aggregates previous results into a summary."""
        filename = os.path.basename(slide_path)
        if cb: cb(10)
        
        # Scan for previous results for this slide
        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        files = glob.glob(os.path.join(base_dir, "results_*.json"))
        
        total_nuclei = 0
        total_tissue_area = 0
        
        if cb: cb(50)
        for fpath in files:
            try:
                with open(fpath, 'r') as f: d = json.load(f)
                if d.get("slide") == filename and d.get("user_id") == user_id:
                    if d.get("job_type") == "SEGMENTATION":
                        total_nuclei += d.get("cell_count", 0)
                    elif d.get("job_type") == "TISSUE_MASK":
                         # rough area sum
                         for c in d.get("cells", []):
                             total_tissue_area += c.get("area", 0)
            except: continue
            
        time.sleep(1)
        if cb: cb(100)
        
        report_data = {
            "summary": f"Analysis Report for {filename}",
            "stats": {
                "total_nuclei_detected": total_nuclei,
                "tissue_area_pixels": total_tissue_area,
                "density": total_nuclei / max(1, total_tissue_area)
            }
        }
        self._save_json(job_id, filename, [], user_id, "REPORT", report_data)
        return f"results_{job_id}.json"

    def _mask_to_polygons(self, mask):
        # (Existing logic)
        polys = []
        for uid in np.unique(mask):
            if uid == 0: continue
            cnts, _ = cv2.findContours((mask==uid).astype(np.uint8), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
            for c in cnts:
                approx = cv2.approxPolyDP(c, 0.01*cv2.arcLength(c,True), True)
                if len(approx)>2: polys.append(approx.reshape(-1,2).tolist())
        return polys

worker = MLWorker()