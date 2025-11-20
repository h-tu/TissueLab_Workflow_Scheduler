import asyncio
from typing import Annotated, List, Dict, Any
from fastapi import FastAPI, HTTPException, Header, Depends, BackgroundTasks, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
import logging

# Import our modules
from .models import (
    WorkflowCreate, 
    Workflow, 
    Branch, 
    Job, 
    JobStatus,
    JobType
)
from .scheduler import Scheduler
from .wsi_tiler import WSITiler

# Configure Logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="TissueLab Scheduler")

# --- Configuration ---
# Allow Frontend (running on port 3000) to talk to Backend (port 8000)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], 
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- Global Singletons ---
scheduler = Scheduler()
tiler = WSITiler()

# --- Lifecycle Events ---
@app.on_event("startup")
async def startup_event():
    """Start the Scheduler Loop when API starts."""
    asyncio.create_task(scheduler.run_loop())

# --- Dependencies ---
async def get_user_id(x_user_id: Annotated[str | None, Header()] = None):
    """Enforces Multi-Tenant Isolation via Header"""
    if not x_user_id:
        raise HTTPException(status_code=400, detail="Missing X-User-ID header")
    return x_user_id

# ==========================
# 1. SCHEDULER ENDPOINTS
# ==========================

@app.post("/workflows/", response_model=Workflow)
async def submit_workflow(
    workflow_in: WorkflowCreate, 
    background_tasks: BackgroundTasks,
    user_id: str = Depends(get_user_id)
):
    """
    Submit a new DAG.
    The Scheduler decides if it runs immediately or queues based on the 3-User Limit.
    """
    # 1. Convert API Input (Pydantic) to Internal Domain Models
    # We need to map WorkflowCreate -> Workflow, BranchCreate -> Branch, etc.
    
    internal_branches = []
    
    for b_in in workflow_in.branches:
        internal_jobs = []
        for j_in in b_in.jobs:
            # Create Job
            job = Job(
                job_type=j_in.job_type,
                params=j_in.params,
                status=JobStatus.PENDING
            )
            internal_jobs.append(job)
            
        # Create Branch
        branch = Branch(
            name=b_in.branch_name,
            jobs=internal_jobs,
            status=JobStatus.PENDING
        )
        internal_branches.append(branch)

    # Create Workflow
    new_workflow = Workflow(
        user_id=user_id,
        name=workflow_in.workflow_name,
        branches=internal_branches,
        status=JobStatus.PENDING
    )
    
    # 2. Submit to Scheduler
    # This method returns True if running, False if Queued
    is_running = await scheduler.submit_workflow(new_workflow)
    
    if is_running:
        new_workflow.status = JobStatus.RUNNING
        logger.info(f"Workflow {new_workflow.id} started immediately for user {user_id}")
    else:
        logger.info(f"Workflow {new_workflow.id} queued for user {user_id}")
    
    return new_workflow

@app.get("/workflows/")
async def list_workflows(user_id: str = Depends(get_user_id)):
    """
    Multi-Tenant List: Users ONLY see their own workflows.
    """
    user_workflows = []
    
    # 1. Check active workflows (Running)
    for w in scheduler.active_workflows.values():
        if w.user_id == user_id:
            user_workflows.append(w)
            
    # 2. Check pending workflows (Queued)
    for w in scheduler.pending_workflows:
        if w.user_id == user_id:
            user_workflows.append(w)
            
    return user_workflows

@app.get("/status")
async def get_system_status():
    """Observability: See the Queue Depth and Active Users."""
    return {
        "active_users_count": len(scheduler.active_user_ids),
        "active_users": list(scheduler.active_user_ids),
        "queue_depth": len(scheduler.pending_workflows),
        "running_jobs": scheduler.running_job_count
    }

# ==========================
# 2. WSI TILE SERVER
# ==========================

@app.get("/slides")
async def get_available_slides():
    """Returns a list of available .svs files in the data directory."""
    try:
        slides = tiler.list_slides()
        return {"slides": slides}
    except Exception as e:
        logger.error(f"Error listing slides: {e}")
        return {"slides": [], "error": str(e)}

@app.get("/slides/{filename}/info")
async def get_slide_metadata(filename: str):
    """
    Returns WSI dimensions so OpenSeadragon knows how to build the viewport.
    """
    try:
        return tiler.get_slide_info(filename)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Slide not found")
    except Exception as e:
        logger.error(f"Error getting info for {filename}: {e}")
        raise HTTPException(status_code=500, detail="Failed to read slide")

@app.get("/tiles/{filename}/{level}/{x}_{y}.jpeg")
async def get_tile(filename: str, level: int, x: int, y: int):
    """
    Serves a single 256x256 JPEG tile.
    Used by the Frontend Visualizer.
    """
    try:
        # Fetch bytes from our custom Tiler class
        tile_bytes = tiler.get_tile(filename, level, x, y)
        return Response(content=tile_bytes, media_type="image/jpeg")
    except Exception as e:
        # Log error but don't crash
        logger.error(f"Tile Error: {e}")
        raise HTTPException(status_code=404, detail="Tile not found")

# ==========================
# 3. RESULTS ENDPOINT
# ==========================

@app.get("/results/{job_id}")
async def get_job_results(job_id: str, user_id: str = Depends(get_user_id)):
    """
    Returns the JSON polygons for a completed segmentation job.
    """
    return {"status": "Not implemented for mock MVP", "job_id": job_id}

if __name__ == "__main__":
    import uvicorn
    # reload=True helps you see changes immediately during development
    uvicorn.run("backend.main:app", host="0.0.0.0", port=8000, reload=True)