import asyncio
import logging
import os
from typing import Dict, List, Set, Optional
from uuid import UUID
from datetime import datetime
from collections import deque

from .models import Workflow, Branch, Job, JobStatus
from .ml_worker import worker as ml_worker

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

MAX_GLOBAL_WORKERS = 4  
MAX_ACTIVE_USERS = 3    

class Scheduler:
    def __init__(self):
        self.pending_workflows: deque[Workflow] = deque()
        self.active_workflows: Dict[UUID, Workflow] = {}
        self.active_user_ids: Set[str] = set()
        self.running_job_count = 0
        self.lock = asyncio.Lock()

    async def submit_workflow(self, workflow: Workflow) -> bool:
        async with self.lock:
            user_id = workflow.user_id
            
            if user_id in self.active_user_ids:
                self.active_workflows[workflow.id] = workflow
                return True

            if len(self.active_user_ids) < MAX_ACTIVE_USERS:
                self.active_user_ids.add(user_id)
                self.active_workflows[workflow.id] = workflow
                return True

            workflow.status = JobStatus.PENDING
            self.pending_workflows.append(workflow)
            return False

    async def run_loop(self):
        while True:
            try:
                await self._schedule_next_jobs()
                await self._manage_user_queue()
                await asyncio.sleep(1)
            except Exception as e:
                logger.error(f"Scheduler crashed: {e}")
                await asyncio.sleep(1)

    async def _schedule_next_jobs(self):
        if self.running_job_count >= MAX_GLOBAL_WORKERS:
            return

        async with self.lock:
            for w_id, workflow in self.active_workflows.items():
                if workflow.status == JobStatus.COMPLETED:
                    continue

                all_branches_complete = True
                
                for branch in workflow.branches:
                    next_job = self._get_next_pending_job(branch)
                    
                    if next_job:
                        all_branches_complete = False
                        if self.running_job_count < MAX_GLOBAL_WORKERS:
                            await self._start_job(workflow.user_id, next_job)
                    
                    if any(j.status == JobStatus.RUNNING for j in branch.jobs):
                        all_branches_complete = False

                if all_branches_complete:
                    workflow.status = JobStatus.COMPLETED
                    workflow.completed_at = datetime.now()

    def _get_next_pending_job(self, branch: Branch) -> Optional[Job]:
        for job in branch.jobs:
            if job.status == JobStatus.RUNNING: return None
            if job.status == JobStatus.FAILED: return None
            if job.status == JobStatus.PENDING: return job
        return None

    async def _start_job(self, user_id: str, job: Job):
        job.status = JobStatus.RUNNING
        job.started_at = datetime.now()
        self.running_job_count += 1
        asyncio.create_task(self._run_job_wrapper(job))

    async def _run_job_wrapper(self, job: Job):
        try:
            base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            data_dir = os.path.join(base_dir, "data/inputs")
            files = [f for f in os.listdir(data_dir) if f.endswith(".svs")]
            
            if not files:
                raise FileNotFoundError("No .svs file found in data/inputs")
            
            slide_path = os.path.join(data_dir, files[0])

            # --- FIX: Pass job_type to the worker ---
            result_file = await asyncio.to_thread(
                ml_worker.process_slide, 
                slide_path, 
                str(job.id),
                job.job_type
            )
            
            job.status = JobStatus.COMPLETED
            job.completed_at = datetime.now()
            job.result_data = {"file": result_file}

        except Exception as e:
            logger.error(f"Job {job.id} FAILED: {e}")
            job.status = JobStatus.FAILED
            job.error_msg = str(e)
        
        finally:
            self.running_job_count -= 1

    async def _manage_user_queue(self):
        async with self.lock:
            users_to_remove = []
            for user_id in list(self.active_user_ids):
                user_workflows = [w for w in self.active_workflows.values() if w.user_id == user_id]
                if user_workflows and all(w.status in [JobStatus.COMPLETED, JobStatus.FAILED] for w in user_workflows):
                    users_to_remove.append(user_id)

            for uid in users_to_remove:
                self.active_user_ids.remove(uid)

            while self.pending_workflows and len(self.active_user_ids) < MAX_ACTIVE_USERS:
                next_workflow = self.pending_workflows.popleft()
                self.active_user_ids.add(next_workflow.user_id)
                self.active_workflows[next_workflow.id] = next_workflow

    async def _check_completions(self):
        pass