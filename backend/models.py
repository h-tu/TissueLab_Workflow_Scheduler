from enum import Enum
from typing import List, Optional, Dict, Any
from pydantic import BaseModel, Field
from uuid import UUID, uuid4
from datetime import datetime

class JobStatus(str, Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"

class JobType(str, Enum):
    SEGMENTATION = "SEGMENTATION"
    TISSUE_MASK = "TISSUE_MASK"

class JobBase(BaseModel):
    job_type: JobType
    params: Dict[str, Any] = Field(default_factory=dict) 

class JobCreate(JobBase):
    pass

class Job(JobBase):
    id: UUID = Field(default_factory=uuid4)
    status: JobStatus = JobStatus.PENDING
    created_at: datetime = Field(default_factory=datetime.now)
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    result_data: Optional[Dict[str, Any]] = None
    error_msg: Optional[str] = None

class BranchCreate(BaseModel):
    branch_name: str
    jobs: List[JobCreate]

class Branch(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    name: str
    jobs: List[Job] = []
    status: JobStatus = JobStatus.PENDING

class WorkflowCreate(BaseModel):
    workflow_name: str
    branches: List[BranchCreate]

class Workflow(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    user_id: str
    name: str
    branches: List[Branch] = []
    created_at: datetime = Field(default_factory=datetime.now)
    completed_at: Optional[datetime] = None
    status: JobStatus = JobStatus.PENDING

    @property
    def progress(self) -> float:
        total_jobs = sum(len(b.jobs) for b in self.branches)
        if total_jobs == 0: return 0.0
        completed = sum(
            1 for b in self.branches for j in b.jobs 
            if j.status == JobStatus.COMPLETED
        )
        return round((completed / total_jobs) * 100, 2)