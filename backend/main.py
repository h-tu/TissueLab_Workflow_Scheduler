#-------------------------------------------------------------------------------
# File:        main.py
# Description: FastAPI application entry point, API routing, and system status endpoints.
# Author:      Hongyu Tu
# Created:     Nov 20, 2025
#-------------------------------------------------------------------------------

import asyncio
import os
import json
from typing import Annotated, List, Dict, Any
from uuid import UUID
from fastapi import FastAPI, HTTPException, Header, Depends, BackgroundTasks, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
import logging
from prometheus_fastapi_instrumentator import Instrumentator

from .models import WorkflowCreate, Workflow, Branch, Job, JobStatus, JobType
from .scheduler import Scheduler
from .wsi_tiler import WSITiler

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="TissueLab Scheduler")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], 
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- NEW: PROMETHEUS INSTRUMENTATION ---
Instrumentator().instrument(app).expose(app)

scheduler = Scheduler()
tiler = WSITiler()

@app.on_event("startup")
async def startup_event():
    asyncio.create_task(scheduler.run_loop())

async def get_user_id(x_user_id: Annotated[str | None, Header()] = None):
    if not x_user_id:
        raise HTTPException(status_code=400, detail="Missing X-User-ID header")
    return x_user_id

@app.post("/workflows/", response_model=Workflow)
async def submit_workflow(
    workflow_in: WorkflowCreate, 
    user_id: str = Depends(get_user_id)
):
    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    slide_path = os.path.join(base_dir, "data/inputs", workflow_in.slide_name)
    
    if not os.path.exists(slide_path):
        raise HTTPException(status_code=404, detail=f"Slide '{workflow_in.slide_name}' not found on server.")

    internal_branches = []
    for b_in in workflow_in.branches:
        internal_jobs = []
        for j_in in b_in.jobs:
            job_params = j_in.params.copy()
            job_params["slide_name"] = workflow_in.slide_name
            job = Job(job_type=j_in.job_type, params=job_params, status=JobStatus.PENDING)
            internal_jobs.append(job)
        branch = Branch(name=b_in.branch_name, jobs=internal_jobs, status=JobStatus.PENDING)
        internal_branches.append(branch)

    new_workflow = Workflow(
        user_id=user_id,
        name=workflow_in.workflow_name,
        slide_name=workflow_in.slide_name,
        branches=internal_branches,
        status=JobStatus.PENDING
    )
    
    is_running = await scheduler.submit_workflow(new_workflow)
    if is_running: new_workflow.status = JobStatus.RUNNING
    return new_workflow

@app.get("/workflows/")
async def list_workflows(user_id: str = Depends(get_user_id)):
    user_workflows = []
    for w in scheduler.active_workflows.values():
        if w.user_id == user_id: user_workflows.append(w)
    for w in scheduler.pending_workflows:
        if w.user_id == user_id: user_workflows.append(w)
    return user_workflows

@app.delete("/workflows/{workflow_id}")
async def delete_workflow(workflow_id: str, user_id: str = Depends(get_user_id)):
    try:
        w_uuid = UUID(workflow_id)
        success = await scheduler.delete_workflow(w_uuid)
        if not success: raise HTTPException(status_code=404, detail="Workflow not found")
        return {"status": "deleted", "id": str(w_uuid)}
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid UUID")

# --- NEW: Cancel Job Endpoint ---
@app.delete("/jobs/{job_id}")
async def cancel_job(job_id: str, user_id: str = Depends(get_user_id)):
    try:
        j_uuid = UUID(job_id)
        success = await scheduler.cancel_job(j_uuid)
        if not success: raise HTTPException(status_code=404, detail="Job not found or cannot be cancelled")
        return {"status": "cancelled", "id": str(j_uuid)}
    except ValueError:
         raise HTTPException(status_code=400, detail="Invalid UUID")

@app.get("/status")
async def get_system_status():
    metrics = scheduler.get_metrics()
    return {
        "active_users_count": len(scheduler.active_user_ids),
        "active_users": list(scheduler.active_user_ids),
        "queue_depth": len(scheduler.pending_workflows),
        "running_jobs": scheduler.running_job_count,
        "pending_jobs_count": metrics["pending_jobs_count"],
        "avg_job_latency": metrics["avg_job_latency"]
    }

@app.get("/slides")
async def get_available_slides():
    try:
        return {"slides": tiler.list_slides()}
    except Exception as e:
        return {"slides": [], "error": str(e)}

@app.get("/slides/{filename}/info")
async def get_slide_metadata(filename: str):
    try:
        return tiler.get_slide_info(filename)
    except Exception:
        raise HTTPException(status_code=404, detail="Slide not found")

@app.get("/tiles/{filename}/{level}/{x}_{y}.jpeg")
def get_tile(filename: str, level: int, x: int, y: int):
    try:
        tile_bytes = tiler.get_tile(filename, level, x, y)
        return Response(content=tile_bytes, media_type="image/jpeg")
    except Exception:
        raise HTTPException(status_code=404, detail="Tile not found")

@app.get("/results/{job_id}")
async def get_job_results(job_id: str, user_id: str = Depends(get_user_id)):
    try:
        safe_id = os.path.basename(job_id) 
        filename = f"results_{safe_id}.json"
        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        filepath = os.path.join(base_dir, filename)
        
        if not os.path.exists(filepath):
             raise HTTPException(status_code=404, detail="Result file not found")
             
        with open(filepath, "r") as f:
            data = json.load(f)
        return data
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("backend.main:app", host="0.0.0.0", port=8000, reload=True)