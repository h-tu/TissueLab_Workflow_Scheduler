import asyncio
from typing import Annotated, List
from fastapi import FastAPI, HTTPException, Header, Depends, BackgroundTasks, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

# Import our modules
from .models import WorkflowCreate, Workflow, JobStatus
from .scheduler import Scheduler
from .wsi_tiler import WSITiler

app = FastAPI(title="TissueLab Scheduler")

# --- Configuration ---
# Allow Frontend to talk to Backend
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # For dev only. In prod, specify domain.
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
    # Create the internal Workflow object
    new_workflow = Workflow(
        user_id=user_id,
        name=workflow_in.workflow_name,
        branches=[]
    )
    
    # Convert Pydantic models to our internal structure
    # (Assuming you have a helper or constructor in models.py, 
    #  but for brevity we assume the Scheduler handles the object creation/mapping)
    # ... [Mapping logic would go here] ...
    
    # Submit to Scheduler
    is_running = await scheduler.submit_workflow(new_workflow)
    
    return new_workflow

@app.get("/workflows/")
async def list_workflows(user_id: str = Depends(get_user_id)):
    """
    Multi-Tenant List: Users ONLY see their own workflows.
    """
    # Combine active and pending lists from scheduler
    # Note: In a real app, you'd query a database. 
    # Here we iterate the scheduler's memory for the MVP.
    
    user_workflows = []
    
    # Check active
    for w in scheduler.active_workflows.values():
        if w.user_id == user_id:
            user_workflows.append(w)
            
    # Check pending
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
# 2. WSI TILE SERVER (New)
# ==========================

@app.get("/slides/{filename}/info")
async def get_slide_metadata(filename: str):
    """
    Returns WSI dimensions so OpenSeadragon knows how to build the viewport.
    """
    try:
        return tiler.get_slide_info(filename)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Slide not found")

@app.get("/tiles/{filename}/{level}/{x}_{y}.jpeg")
async def get_tile(filename: str, level: int, x: int, y: int):
    """
    Serves a single 256x256 JPEG tile.
    Used by the Frontend Visualizer.
    """
    try:
        # Fetch bytes
        tile_bytes = tiler.get_tile(filename, level, x, y)
        return Response(content=tile_bytes, media_type="image/jpeg")
    except Exception as e:
        # If something goes wrong, 404 ensures the viewer doesn't hang
        raise HTTPException(status_code=404, detail=str(e))

# ==========================
# 3. RESULTS ENDPOINT
# ==========================

@app.get("/results/{job_id}")
async def get_job_results(job_id: str, user_id: str = Depends(get_user_id)):
    """
    Returns the JSON polygons for a completed segmentation job.
    """
    # Logic to look up result file based on job_id
    # ...
    return {"status": "Not implemented yet for MVP"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)