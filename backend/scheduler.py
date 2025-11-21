import asyncio
import logging
import os
import json
import threading # <--- Import Threading
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
STATE_FILE = "scheduler_state.json"

class Scheduler:
    def __init__(self):
        self.pending_workflows: deque[Workflow] = deque()
        self.active_workflows: Dict[UUID, Workflow] = {}
        self.active_user_ids: Set[str] = set()
        self.running_job_count = 0
        self.lock = asyncio.Lock()
        
        # --- ADDED: Map Job IDs to Cancellation Events ---
        self.job_cancellation_events: Dict[UUID, threading.Event] = {}
        
        self._load_state()

    def _save_state(self):
        try:
            state = {
                "active_workflows": [w.model_dump(mode='json') for w in self.active_workflows.values()],
                "pending_workflows": [w.model_dump(mode='json') for w in self.pending_workflows],
                "active_user_ids": list(self.active_user_ids)
            }
            
            base_dir = os.path.dirname(os.path.abspath(__file__))
            file_path = os.path.join(base_dir, STATE_FILE)
            
            with open(file_path, "w") as f:
                json.dump(state, f, indent=2)
        except Exception as e:
            logger.error(f"Failed to save state: {e}")

    def _load_state(self):
        base_dir = os.path.dirname(os.path.abspath(__file__))
        file_path = os.path.join(base_dir, STATE_FILE)
        
        if not os.path.exists(file_path):
            return

        try:
            with open(file_path, "r") as f:
                data = json.load(f)
            
            self.active_workflows = {}
            for w_data in data.get("active_workflows", []):
                workflow = Workflow(**w_data)
                
                has_running = False
                for branch in workflow.branches:
                    for job in branch.jobs:
                        if job.status == JobStatus.RUNNING:
                            job.status = JobStatus.FAILED
                            job.error_msg = "System restart detected during execution."
                            has_running = True
                
                if has_running and workflow.status == JobStatus.RUNNING:
                     workflow.status = JobStatus.FAILED

                self.active_workflows[workflow.id] = workflow

            self.pending_workflows = deque()
            for w_data in data.get("pending_workflows", []):
                self.pending_workflows.append(Workflow(**w_data))

            self.active_user_ids = set(data.get("active_user_ids", []))
            
            logger.info(f"✅ Restored {len(self.active_workflows)} active workflows from disk.")
            
        except Exception as e:
            logger.error(f"Failed to load state: {e}")

    async def submit_workflow(self, workflow: Workflow) -> bool:
        async with self.lock:
            user_id = workflow.user_id
            
            if user_id in self.active_user_ids:
                self.active_workflows[workflow.id] = workflow
                self._save_state() 
                return True

            if len(self.active_user_ids) < MAX_ACTIVE_USERS:
                self.active_user_ids.add(user_id)
                self.active_workflows[workflow.id] = workflow
                self._save_state() 
                return True

            workflow.status = JobStatus.PENDING
            self.pending_workflows.append(workflow)
            self._save_state()
            return False

    async def delete_workflow(self, workflow_id: UUID) -> bool:
        async with self.lock:
            deleted = False
            
            if workflow_id in self.active_workflows:
                workflow = self.active_workflows[workflow_id]
                
                # --- ADDED: CANCEL ZOMBIE JOBS ---
                # If workflow is deleted, check if it has running jobs and signal them to stop.
                for branch in workflow.branches:
                    for job in branch.jobs:
                        if job.id in self.job_cancellation_events:
                            logger.info(f"🛑 Signaling kill for Job {job.id}")
                            self.job_cancellation_events[job.id].set()
                
                del self.active_workflows[workflow_id]
                deleted = True
            
            else:
                original_len = len(self.pending_workflows)
                self.pending_workflows = deque([w for w in self.pending_workflows if w.id != workflow_id])
                if len(self.pending_workflows) < original_len:
                    deleted = True
            
            if deleted:
                self._save_state()
                return True
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
            state_changed = False
            
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
                            state_changed = True
                    
                    if any(j.status == JobStatus.RUNNING for j in branch.jobs):
                        all_branches_complete = False

                if all_branches_complete:
                    any_failed = any(
                        j.status == JobStatus.FAILED 
                        for b in workflow.branches 
                        for j in b.jobs
                    )
                    workflow.status = JobStatus.FAILED if any_failed else JobStatus.COMPLETED
                    workflow.completed_at = datetime.now()
                    state_changed = True

            if state_changed:
                self._save_state()

    def _get_next_pending_job(self, branch: Branch) -> Optional[Job]:
        for job in branch.jobs:
            if job.status == JobStatus.RUNNING: return None
            if job.status == JobStatus.FAILED: return None
            if job.status == JobStatus.PENDING: return job
        return None

    async def _start_job(self, user_id: str, job: Job):
        job.status = JobStatus.RUNNING
        job.progress = 0 # Reset progress
        job.started_at = datetime.now()
        self.running_job_count += 1
        self._save_state() 
        
        asyncio.create_task(self._run_job_wrapper(job))

    async def _run_job_wrapper(self, job: Job):
        # --- ADDED: Setup Cancellation Event & Progress Callback ---
        cancel_event = threading.Event()
        self.job_cancellation_events[job.id] = cancel_event

        def update_progress(p: int):
            job.progress = p
            # Note: We do not save to disk on every progress update to save IO.
            # Persistence is strictly for status changes (reliability vs speed).

        try:
            base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            data_dir = os.path.join(base_dir, "data/inputs")
            
            slide_name = job.params.get("slide_name")
            
            if slide_name:
                slide_path = os.path.join(data_dir, slide_name)
                if not os.path.exists(slide_path):
                    raise FileNotFoundError(f"Slide {slide_name} not found on disk.")
            else:
                files = [f for f in os.listdir(data_dir) if f.endswith(".svs")]
                if not files: raise FileNotFoundError("No .svs file found in data/inputs")
                slide_path = os.path.join(data_dir, files[0])

            # Pass event and callback to worker
            result_file = await asyncio.to_thread(
                ml_worker.process_slide, 
                slide_path, 
                str(job.id),
                job.job_type,
                cancel_event,   # <--- PASS
                update_progress # <--- PASS
            )
            
            job.status = JobStatus.COMPLETED
            job.progress = 100
            job.completed_at = datetime.now()
            job.result_data = {"file": result_file}

        except InterruptedError:
            logger.warning(f"Job {job.id} was cancelled cleanly.")
            job.status = JobStatus.CANCELLED
            job.error_msg = "Cancelled by user"

        except Exception as e:
            logger.error(f"Job {job.id} FAILED: {e}")
            job.status = JobStatus.FAILED
            job.error_msg = str(e)
        
        finally:
            self.running_job_count -= 1
            # Remove event from tracker
            if job.id in self.job_cancellation_events:
                del self.job_cancellation_events[job.id]
            
            self._save_state() 

    async def _manage_user_queue(self):
        async with self.lock:
            users_to_remove = []
            for user_id in list(self.active_user_ids):
                user_workflows = [w for w in self.active_workflows.values() if w.user_id == user_id]
                
                if user_workflows and all(w.status in [JobStatus.COMPLETED, JobStatus.FAILED] for w in user_workflows):
                    users_to_remove.append(user_id)

            if users_to_remove:
                for uid in users_to_remove:
                    self.active_user_ids.remove(uid)
                self._save_state() 

            while self.pending_workflows and len(self.active_user_ids) < MAX_ACTIVE_USERS:
                next_workflow = self.pending_workflows.popleft()
                self.active_user_ids.add(next_workflow.user_id)
                self.active_workflows[next_workflow.id] = next_workflow
                self._save_state()