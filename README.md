# TissueLab Workflow Scheduler

#### Hongyu Tu, Nov 20-21, 2025


## Key Features & Design Decisions

### 1. Hierarchical DAG Scheduling
The core scheduler implements a **Producer-Consumer** model with a nuanced understanding of workflow structures:
* **Branch-Aware Parallelism:** Jobs within a specific branch execute serially (FIFO) to ensure dependency integrity (e.g., Tissue Mask $\rightarrow$ Segmentation). However, distinct branches execute in parallel, maximizing GPU utilization.
* **Global & User Concurrency:** The system respects a global worker limit (`MAX_WORKERS=4`) to prevent OOM errors, while simultaneously enforcing a strict limit on active concurrent users (Max 3) to ensure fairness in a multi-tenant environment.

### 2. Smart Inference Optimization
To handle gigapixel-scale WSIs efficiently, the `MLWorker` implements a **Region-of-Interest (ROI) strategy**:
* **Tile-Based Inference:** Large images are processed in overlapping tiles to maintain context at boundaries.
* **Mask-Guided Acceleration:** If a "Tissue Mask" job completes first, subsequent "Segmentation" jobs automatically utilize the generated polygon data to skip processing background tiles, significantly reducing inference time.

### 3. Resilience & State Management
* **Persistence:** The scheduler maintains its state in `scheduler_state.json`. In the event of a container restart, the system rehydrates the state, marks interrupted jobs as `FAILED` (for safety), and resumes queue processing.
* **Graceful Cancellation:** Jobs support threading-event-based cancellation, allowing users to stop long-running inference tasks immediately.

### 4. Full-Stack Observability
* **Frontend Visualization:** A bespoke OpenSeadragon viewer with SVG overlays renders segmentation results (nuclei polygons) in real-time over the WSI.
* **Metrics:** Integrated Prometheus middleware exposes key metrics (`queue_depth`, `job_latency`, `active_users`) for Grafana dashboards.

---

## Setup Instructions

### Prerequisites
* **Docker & Docker Compose** (Recommended)
* **Python 3.10+** (For local execution)
* **Data:** Place your `.svs` files in the `./data/inputs` directory.

### Option 1: Docker Deployment 
This spins up the Backend, Frontend (Nginx), Prometheus, and Grafana.

1.  **Clone the repository:**
    ```bash
    git clone https://github.com/h-tu/TissueLab_Workflow_Scheduler.git
    cd TissueLab_Workflow_Scheduler
    ```

2.  **Add Test Data:**
    Download sample `.svs` files (e.g., from [CMU OpenSlide](https://openslide.cs.cmu.edu/download/openslide-testdata/Aperio/)) and place them in `data/inputs/`.

3.  **Launch Services:**
    ```bash
    docker-compose up --build
    ```

4.  **Access the Application:**
    * **Web UI:** [http://localhost:3000](http://localhost:3000)
    * **API Docs:** [http://localhost:8000/docs](http://localhost:8000/docs)
    * **Grafana:** [http://localhost:3001](http://localhost:3001) (Login: `admin` / `admin`)
    * **Prometheus:** [http://localhost:9090](http://localhost:9090)

### Option 2: Local Development
If you prefer running without Docker:

1.  **Create Environment:**
    ```bash
    conda create -n tissuelab python=3.10 openslide -c conda-forge -y
    conda activate tissuelab

    pip install -r requirements.txt
    ```

2.  **Run the Application:**
    We provide a helper script to launch both the Uvicorn backend and a simple Python HTTP frontend server.
    ```bash
    chmod +x run.sh
    ./run.sh
    ```

---

## Scaling Strategy (10x Growth)

To scale this system from handling 3 active users to 30+ users and 10x job volume, the following architectural changes would be implemented:

### 1. Decoupling the Queue (Redis/Celery)
Currently, the scheduler runs in-memory using Python `asyncio` locks and a `deque`.
* **Change:** Replace the internal `deque` with a distributed task queue like **Redis** (using Celery or RQ).
* **Benefit:** This persists the queue outside the application process, allowing the backend API to scale horizontally without losing queue state.

### 2. Horizontal Worker Scaling (Kubernetes)
Currently, the `MLWorker` runs as a thread within the API container.
* **Change:** Extract `MLWorker` into a standalone container. Deploy on **Kubernetes (K8s)** using a `Deployment` or `Job` resource.
* **Benefit:** We can autoscale the number of worker pods based on the `queue_depth` metric from Prometheus. This also isolates heavy GPU computation from the lightweight API server.

### 3. Database Migration (PostgreSQL)
Currently, state is saved to a JSON file.
* **Change:** Migrate `scheduler_state.json` to a relational database (PostgreSQL).
* **Benefit:** Provides ACID compliance, handles high-concurrency read/writes for 10x users, and enables complex querying for historical reporting.

### 4. Storage Optimization (S3/MinIO)
* **Change:** Instead of local file system paths for `.svs` files and JSON results, use object storage.
* **Benefit:** Allows any worker node to access any slide image, regardless of which physical machine it is running on.

---

## Testing & Monitoring in Production

### Testing Strategy
1.  **Unit Tests (`pytest`):**
    * Test `Scheduler` logic: Ensure user limits and branch serialization logic holds.
    * Test `TileCache`: Verify LRU eviction works correctly.
2.  **Integration Tests:**
    * Mock the `InstanSeg` model to test the full pipeline from `POST /workflows` to `results.json` generation without needing a GPU.
3.  **Load Testing (`Locust`):**
    * Simulate 50 concurrent users submitting workflows to verify the `MAX_ACTIVE_USERS` queuing logic works under pressure.

### Production Monitoring
1.  **Application Metrics (Prometheus):**
    * **`job_latency_seconds`:** Critical for SLA monitoring.
    * **`gpu_utilization`:** To ensure we aren't paying for idle GPUs.
    * **`queue_wait_time`:** To determine if we need to auto-scale more workers.
2.  **Error Tracking (Sentry):**
    * Capture and aggregate tracebacks from `ml_worker` failures (e.g., corrupt slides, OOM errors).
3.  **Distributed Tracing (OpenTelemetry):**
    * Trace a request ID from the Frontend $\rightarrow$ API $\rightarrow$ Scheduler $\rightarrow$ Worker to pinpoint bottlenecks.

