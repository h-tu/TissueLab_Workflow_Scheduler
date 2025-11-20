from enum import Enum
from typing import List, Optional, Dict, Any
from pydantic import BaseModel, Field
from uuid import UUID, uuid4
from datetime import datetime

# --- Enums for State Management ---
class JobStatus(str, Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"

class JobType(str, Enum):
    SEGMENTATION = "SEGMENTATION"
    TISSUE_MASK = "TISSUE_MASK"

# --- The Atomic Unit: The Job ---
class JobBase(BaseModel):
    """Defines what a job looks like before it runs."""
    job_type: JobType
    # Parameters for the ML model (e.g., tile_size, model_version)
    params: Dict[str, Any] = Field(default_factory=dict) 

class JobCreate(JobBase):
    pass

class Job(JobBase):
    """The internal representation with status tracking."""
    id: UUID = Field(default_factory=uuid4)
    status: JobStatus = JobStatus.PENDING
    created_at: datetime = Field(default_factory=datetime.now)
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    result_data: Optional[Dict[str, Any]] = None  # Stores polygon output path or stats
    error_msg: Optional[str] = None

# --- The Branch: Serial Execution Container ---
class BranchCreate(BaseModel):
    branch_name: str
    jobs: List[JobCreate]

class Branch(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    name: str
    # Jobs within this list must run SERIALLY (FIFO)
    jobs: List[Job] = []
    status: JobStatus = JobStatus.PENDING

# --- The Workflow: Parallel Execution Container ---
class WorkflowCreate(BaseModel):
    workflow_name: str
    # Branches run in PARALLEL
    branches: List[BranchCreate]

class Workflow(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    user_id: str  # From X-User-ID header
    name: str
    branches: List[Branch] = []
    created_at: datetime = Field(default_factory=datetime.now)
    status: JobStatus = JobStatus.PENDING

    @property
    def progress(self) -> float:
        """Helper to calculate percentage for the UI."""
        total_jobs = sum(len(b.jobs) for b in self.branches)
        if total_jobs == 0: return 0.0
        completed = sum(
            1 for b in self.branches for j in b.jobs 
            if j.status == JobStatus.COMPLETED
        )
        return round((completed / total_jobs) * 100, 2)