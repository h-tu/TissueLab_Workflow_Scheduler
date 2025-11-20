import asyncio
import logging
from typing import Dict, List, Set, Optional
from uuid import UUID
from datetime import datetime
from collections import deque

# Import your models
from .models import Workflow, Branch, Job, JobStatus

# Configure Logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Configuration
MAX_GLOBAL_WORKERS = 4  # Global limit on parallel GPU tasks
MAX_ACTIVE_USERS = 3    # The "Gatekeeper" limit

class Scheduler:
    def __init__(self):
        # --- State Management ---
        # Queue for incoming workflows that are waiting for a User Slot
        self.pending_workflows: deque[Workflow] = deque()
        
        # Currently executing workflows (User Slot acquired)
        self.active_workflows: Dict[UUID, Workflow] = {}
        
        # Track which users currently hold a "Slot"
        self.active_user_ids: Set[str] = set()
        
        # Track running jobs to enforce MAX_GLOBAL_WORKERS
        self.running_job_count = 0
        
        # Asyncio Lock to ensure thread safety when modifying state
        self.lock = asyncio.Lock()

    async def submit_workflow(self, workflow: Workflow) -> bool:
        """
        Entry point. Decides if workflow starts immediately or queues.
        Returns True if started, False if queued.
        """
        async with self.lock:
            user_id = workflow.user_id
            
            # Case 1: User is already active -> They can run more workflows
            if user_id in self.active_user_ids:
                logger.info(f"User {user_id} is already active. Adding workflow {workflow.id}.")
                self.active_workflows[workflow.id] = workflow
                return True

            # Case 2: User is new, but we have space ( < 3 users)
            if len(self.active_user_ids) < MAX_ACTIVE_USERS:
                logger.info(f"User {user_id} is new. Granting slot. (Active Users: {len(self.active_user_ids) + 1}/{MAX_ACTIVE_USERS})")
                self.active_user_ids.add(user_id)
                self.active_workflows[workflow.id] = workflow
                return True

            # Case 3: User Limit Reached -> Queue
            logger.info(f"User {user_id} queued. (Active Users Full: {len(self.active_user_ids)})")
            workflow.status = JobStatus.PENDING
            self.pending_workflows.append(workflow)
            return False

    async def run_loop(self):
        """
        The Main Event Loop. Run this as a background task in FastAPI.
        It constantly checks for:
        1. Completed jobs.
        2. New jobs to schedule (Branch-Aware).
        3. Queued users waiting for a slot.
        """
        logger.info("Scheduler loop started.")
        while True:
            try:
                await self._check_completions()
                await self._schedule_next_jobs()
                await self._manage_user_queue()
                await asyncio.sleep(1) # Heartbeat
            except Exception as e:
                logger.error(f"Scheduler crashed: {e}")
                await asyncio.sleep(1)

    async def _schedule_next_jobs(self):
        """
        The 'Branch-Aware' Logic.
        Iterates through ALL active workflows and ALL their branches.
        """
        if self.running_job_count >= MAX_GLOBAL_WORKERS:
            return

        async with self.lock:
            for w_id, workflow in self.active_workflows.items():
                if workflow.status == JobStatus.COMPLETED:
                    continue

                all_branches_complete = True
                
                # --- BRANCH LOGIC: Parallel across branches, Serial within ---
                for branch in workflow.branches:
                    # Find the first non-completed job in this branch (FIFO)
                    next_job = self._get_next_pending_job(branch)
                    
                    if next_job:
                        all_branches_complete = False
                        # We found a job to run!
                        if self.running_job_count < MAX_GLOBAL_WORKERS:
                            await self._start_job(workflow.user_id, next_job)
                    
                    # Check if branch is still running
                    if any(j.status == JobStatus.RUNNING for j in branch.jobs):
                        all_branches_complete = False

                if all_branches_complete:
                    workflow.status = JobStatus.COMPLETED
                    workflow.completed_at = datetime.now()
                    # Note: We don't remove it yet, we wait for _manage_user_queue to clean up

    def _get_next_pending_job(self, branch: Branch) -> Optional[Job]:
        """Helper: Returns the next job in the branch IF it's ready to run."""
        for job in branch.jobs:
            if job.status == JobStatus.RUNNING:
                return None  # Blocked: Branch is busy running previous job
            if job.status == JobStatus.FAILED:
                return None  # Blocked: Branch failed
            if job.status == JobStatus.PENDING:
                return job   # Ready!
        return None # Branch finished

    async def _start_job(self, user_id: str, job: Job):
        """fires the job off to the ML Worker."""
        logger.info(f"Starting Job {job.id} (User: {user_id})")
        job.status = JobStatus.RUNNING
        job.started_at = datetime.now()
        self.running_job_count += 1
        
        # --- HERE IS WHERE YOU CALL THE ML WORKER ---
        # In a real app, this puts a message on Redis.
        # For 24h MVP, we run it in a ThreadPool to not block the loop.
        asyncio.create_task(self._mock_ml_worker(job))

    async def _mock_ml_worker(self, job: Job):
        """
        Temporary stub to simulate work. 
        Replace this with actual InstanSeg code later.
        """
        await asyncio.sleep(5)  # Simulate GPU work
        job.status = JobStatus.COMPLETED
        job.completed_at = datetime.now()
        self.running_job_count -= 1
        logger.info(f"Job {job.id} COMPLETED.")

    async def _manage_user_queue(self):
        """
        Checks if active users are done. If so, boots them and lets a queued user in.
        """
        async with self.lock:
            # 1. Find users who are "Done" (No pending/running workflows)
            users_to_remove = []
            for user_id in list(self.active_user_ids):
                user_workflows = [w for w in self.active_workflows.values() if w.user_id == user_id]
                
                if not user_workflows:
                    continue # Should not happen, but safety check

                # A user is "Done" only if ALL their workflows are COMPLETED or FAILED
                all_done = all(w.status in [JobStatus.COMPLETED, JobStatus.FAILED] for w in user_workflows)
                
                if all_done:
                    logger.info(f"User {user_id} has finished all workflows. Releasing slot.")
                    users_to_remove.append(user_id)
                    # Cleanup finished workflows to free memory (optional)
                    # self.active_workflows = {k:v for k,v in self.active_workflows.items() if v.user_id != user_id}

            # 2. Remove them
            for uid in users_to_remove:
                self.active_user_ids.remove(uid)

            # 3. Promote queued users
            while self.pending_workflows and len(self.active_user_ids) < MAX_ACTIVE_USERS:
                next_workflow = self.pending_workflows.popleft()
                
                # Add user
                self.active_user_ids.add(next_workflow.user_id)
                # Add workflow
                self.active_workflows[next_workflow.id] = next_workflow
                
                logger.info(f"Promoted User {next_workflow.user_id} from Queue.")

    async def _check_completions(self):
        pass # Handled by callbacks in _start_job for this MVP