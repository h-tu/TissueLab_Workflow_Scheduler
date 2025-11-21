// CONFIGURATION
const API_URL = "http://localhost:8000";
const TILE_SERVER_URL = "http://localhost:8000/tiles";

let viewer = null;
let overlay = null;
let pollInterval = null;
let currentSlideFilename = null;
let completedJobIds = new Set();
let isOverlayVisible = true;

// --- 1. INITIALIZATION ---
document.addEventListener("DOMContentLoaded", () => {
    initViewer();
    startPolling();
    fetchSlideList();
    
    // Default user
    document.getElementById('userIdInput').value = "User_0";
});

// HELPER: Quick User Switch
window.setUserId = function(id) {
    document.getElementById('userIdInput').value = "User_" + id;
    fetchStatus(); 
}

// --- 2. OPENSEADRAGON VIEWER ---
function initViewer() {
    viewer = OpenSeadragon({
        id: "openseadragon1",
        prefixUrl: "https://cdnjs.cloudflare.com/ajax/libs/openseadragon/4.1.0/images/",
        animationTime: 0.5,
        blendTime: 0.1,
        constrainDuringPan: true,
        maxZoomPixelRatio: 2,
        minZoomLevel: 0, // Full zoom out
        visibilityRatio: 1,
        zoomPerScroll: 2,
    });

    overlay = viewer.svgOverlay();
}

async function fetchSlideList() {
    try {
        const res = await fetch(`${API_URL}/slides`);
        const data = await res.json();
        
        if (data.slides && data.slides.length > 0) {
            renderSlideList(data.slides);
        } else {
            document.getElementById('slideList').innerHTML = 
                '<div class="text-xs text-red-400 p-2">No .svs files found in data/inputs</div>';
            document.getElementById('currentSlideName').innerText = "No Data";
        }
    } catch (e) {
        console.error("Slide Fetch Error:", e);
        document.getElementById('slideList').innerHTML = 
            '<div class="text-xs text-red-400 p-2">Backend Connection Failed</div>';
    }
}

function renderSlideList(slides) {
    const container = document.getElementById('slideList');
    
    const html = slides.map(slide => {
        const filename = slide.name;
        const displayName = filename.length > 20 ? filename.substring(0, 18) + "..." : filename;
        const fileSize = slide.size || "Unknown";
        const dimensions = slide.dimensions || "Unknown";
        
        return `
            <div onclick="loadSlide('${filename}')" 
                 class="slide-item cursor-pointer p-3 rounded hover:bg-slate-700 text-slate-300 group border border-transparent hover:border-slate-600 transition-all mb-1">
                <div class="flex items-center gap-2 mb-1">
                    <i class="fa-solid fa-file-medical text-slate-500 group-hover:text-blue-400"></i>
                    <span class="font-medium text-xs text-white" title="${filename}">${displayName}</span>
                </div>
                <div class="flex justify-between text-[10px] text-slate-400 pl-5 opacity-80">
                    <span>${dimensions}</span>
                    <span>${fileSize}</span>
                </div>
            </div>
        `;
    }).join('');
    
    container.innerHTML = html;
}

async function loadSlide(filename) {
    const loader = document.getElementById('viewerLoader');
    const label = document.getElementById('currentSlideName');
    const prompt = document.getElementById('selectSlidePrompt');
    
    if (prompt) prompt.classList.add('hidden');

    loader.classList.remove('hidden');
    loader.classList.remove('bg-slate-900'); 
    loader.classList.add('bg-slate-900/50'); 
    loader.classList.add('pointer-events-none'); 
    
    label.innerText = "Loading: " + filename;
    currentSlideFilename = filename;
    
    if(overlay) d3_clear_overlay(); 

    try {
        const response = await fetch(`${API_URL}/slides/${filename}/info`);
        if (!response.ok) throw new Error("Slide not found");
        
        const info = await response.json();

        const tileSource = {
            height: info.height,
            width: info.width,
            tileSize: 256,
            minLevel: 0,
            maxLevel: info.level_count - 1,
            getTileUrl: function(level, x, y) {
                return `${TILE_SERVER_URL}/${filename}/${level}/${x}_${y}.jpeg`;
            }
        };

        viewer.addOnceHandler('tile-loaded', function() {
            loader.classList.add('hidden');
            loader.classList.remove('bg-slate-900/50'); 
            loader.classList.remove('pointer-events-none');
            loader.classList.add('bg-slate-900');
            label.innerText = filename;
        });

        viewer.addOnceHandler('open-failed', function() {
             loader.classList.add('hidden');
             label.innerText = "Error Opening Slide";
        });

        viewer.open(tileSource);

    } catch (error) {
        console.error(error);
        label.innerText = "Error Loading File";
        loader.classList.add('hidden');
    }
}

// --- 3. POLLING & RESULT FETCHING ---
function startPolling() {
    fetchStatus();
    pollInterval = setInterval(fetchStatus, 1000);
}

async function fetchStatus() {
    const userId = document.getElementById('userIdInput').value;
    try {
        const statusRes = await fetch(`${API_URL}/status`);
        const status = await statusRes.json();
        
        document.getElementById('activeUsersCount').innerText = `${status.active_users_count}/3`;
        document.getElementById('gpuWorkersCount').innerText = `${status.running_jobs}/4`;

        const wfRes = await fetch(`${API_URL}/workflows/`, {
            headers: { 'X-User-ID': userId }
        });
        const workflows = await wfRes.json();
        
        renderWorkflows(workflows);
        checkAndLoadResults(workflows);

    } catch (e) { /* Silent fail */ }
}

function checkAndLoadResults(workflows) {
    workflows.forEach(wf => {
        wf.branches.forEach(branch => {
            branch.jobs.forEach(async (job) => {
                if (job.status === 'COMPLETED' && !completedJobIds.has(job.id)) {
                    completedJobIds.add(job.id);
                    
                    try {
                        const res = await fetch(`${API_URL}/results/${job.id}`, { headers: { 'X-User-ID': 'sys' } });
                        if (res.ok) {
                            const resultData = await res.json();
                            if (resultData.slide === currentSlideFilename) {
                                drawPolygons(resultData.polygons);
                                showNotification(`${resultData.cell_count} Objects Detected`);
                            }
                        }
                    } catch (err) {
                        console.error("Failed to load results", err);
                    }
                }
            });
        });
    });
}

function drawPolygons(polygons) {
    if (!overlay) return;
    
    const svgNode = overlay.node();
    const fragment = document.createDocumentFragment();
    
    polygons.forEach(poly => {
        const path = document.createElementNS("http://www.w3.org/2000/svg", "polygon");
        const pointsStr = poly.map(p => `${p[0]},${p[1]}`).join(" ");
        path.setAttribute("points", pointsStr);
        path.setAttribute("class", "cell-polygon");
        fragment.appendChild(path);
    });
    
    svgNode.appendChild(fragment);
    overlay.resize();
    
    document.getElementById('btnToggleOverlay').classList.remove('text-slate-400');
    document.getElementById('btnToggleOverlay').classList.add('text-blue-400');
}

function d3_clear_overlay() {
    if (overlay) {
        const svgNode = overlay.node();
        while (svgNode.firstChild) {
            svgNode.removeChild(svgNode.firstChild);
        }
    }
}

function toggleOverlay() {
    if (!overlay) return;
    const svgNode = overlay.node();
    isOverlayVisible = !isOverlayVisible;
    
    svgNode.style.display = isOverlayVisible ? 'block' : 'none';
    
    const btn = document.getElementById('btnToggleOverlay');
    if (isOverlayVisible) {
        btn.classList.add('text-blue-400');
        btn.classList.remove('text-slate-400');
    } else {
        btn.classList.add('text-slate-400');
        btn.classList.remove('text-blue-400');
    }
}

function showNotification(msg) {
    const toast = document.getElementById('notificationToast');
    document.getElementById('notificationText').innerText = msg;
    
    toast.classList.remove('-translate-y-32');
    setTimeout(() => {
        toast.classList.add('-translate-y-32');
    }, 4000);
}

function renderWorkflows(workflows) {
    const container = document.getElementById('workflowList');
    if (workflows.length === 0) {
        container.innerHTML = `<div class="text-center text-slate-500 text-sm mt-10">No active workflows</div>`;
        return;
    }

    const html = workflows.map(wf => {
        let statusColor = "bg-slate-600";
        if (wf.status === "RUNNING") statusColor = "bg-blue-500 animate-pulse";
        if (wf.status === "COMPLETED") statusColor = "bg-emerald-500";
        if (wf.status === "PENDING") statusColor = "bg-amber-500";

        const progress = wf.status === "COMPLETED" ? 100 : (wf.status === "RUNNING" ? 50 : 0);

        return `
            <div class="bg-slate-700 rounded-lg p-3 border border-slate-600 shadow-sm hover:border-slate-500 transition-colors">
                <div class="flex justify-between items-center mb-2">
                    <span class="font-bold text-sm text-slate-200">${wf.name}</span>
                    <span class="text-[10px] font-mono px-2 py-1 rounded-full text-white ${statusColor}">
                        ${wf.status}
                    </span>
                </div>
                <div class="flex gap-1 mb-2">
                     ${wf.branches.map(b => `
                        <div class="h-1 flex-1 rounded-full bg-slate-600 overflow-hidden">
                            <div class="h-full bg-blue-400" style="width: ${progress}%"></div>
                        </div>
                     `).join('')}
                </div>
            </div>
        `;
    }).join('');
    container.innerHTML = html;
}

// --- 4. WORKFLOW BUILDER LOGIC ---

function openBuilder() {
    document.getElementById('builderModal').classList.remove('hidden');
    const container = document.getElementById('builderBranches');
    container.innerHTML = ''; // Clear previous
    addBuilderBranch(); // Start with one branch
    
    // Generate random default name
    document.getElementById('buildWfName').value = "Job_" + Math.floor(Math.random() * 1000);
}

function closeBuilder() {
    document.getElementById('builderModal').classList.add('hidden');
}

function addBuilderBranch() {
    const container = document.getElementById('builderBranches');
    const branchId = 'branch_' + Date.now() + '_' + Math.random();
    
    const div = document.createElement('div');
    div.className = "bg-slate-900 p-3 rounded border border-slate-700 builder-branch";
    div.dataset.id = branchId;
    
    div.innerHTML = `
        <div class="flex justify-between items-center mb-2">
            <div class="flex gap-2 items-center w-full">
                <span class="text-xs font-bold text-slate-400">BRANCH</span>
                <input type="text" value="Region 1" class="branch-name-input bg-slate-800 border border-slate-600 rounded text-xs px-2 py-1 text-white w-1/2">
            </div>
            <button onclick="this.closest('.builder-branch').remove()" class="text-red-400 hover:text-red-300 text-xs"><i class="fa-solid fa-trash"></i></button>
        </div>
        <div class="space-y-2 pl-2 border-l-2 border-slate-700 jobs-container">
            </div>
        <button onclick="addBuilderJob('${branchId}')" class="mt-2 text-[10px] text-blue-400 hover:text-blue-300 font-bold uppercase">
            <i class="fa-solid fa-plus"></i> Add Job
        </button>
    `;
    
    container.appendChild(div);
    // Add default job
    addBuilderJob(branchId);
}

function addBuilderJob(branchId) {
    // Find the specific branch container by data-id
    const branchDiv = document.querySelector(`.builder-branch[data-id="${branchId}"] .jobs-container`);
    
    const div = document.createElement('div');
    div.className = "flex gap-2 items-center builder-job";
    
    div.innerHTML = `
        <div class="h-6 w-6 rounded bg-slate-700 flex items-center justify-center text-[10px] text-slate-400 font-mono">J</div>
        <select class="job-type-select bg-slate-800 border border-slate-600 text-white text-xs rounded px-2 py-1 flex-1">
            <option value="SEGMENTATION">Nuclei Segmentation (InstanSeg)</option>
            <option value="TISSUE_MASK">Tissue Mask Generation</option>
        </select>
        <button onclick="this.closest('.builder-job').remove()" class="text-slate-500 hover:text-red-400"><i class="fa-solid fa-minus"></i></button>
    `;
    
    branchDiv.appendChild(div);
}

async function submitBuilder() {
    const userId = document.getElementById('userIdInput').value;
    const wfName = document.getElementById('buildWfName').value;
    
    const branches = [];
    
    // Traverse DOM to build payload
    document.querySelectorAll('.builder-branch').forEach(bDiv => {
        const branchName = bDiv.querySelector('.branch-name-input').value;
        const jobs = [];
        
        bDiv.querySelectorAll('.builder-job').forEach(jDiv => {
            const type = jDiv.querySelector('.job-type-select').value;
            jobs.push({
                job_type: type,
                params: {}
            });
        });
        
        if (jobs.length > 0) {
            branches.push({
                branch_name: branchName,
                jobs: jobs
            });
        }
    });
    
    if (branches.length === 0) {
        alert("Please add at least one branch with one job.");
        return;
    }

    const payload = {
        workflow_name: wfName,
        branches: branches
    };

    try {
        const res = await fetch(`${API_URL}/workflows/`, {
            method: "POST",
            headers: { "Content-Type": "application/json", "X-User-ID": userId },
            body: JSON.stringify(payload)
        });
        
        if (res.status === 403 || res.status === 429) {
            alert("Queue Full! You are now waiting for a slot.");
        }
        
        closeBuilder();
        fetchStatus();
        
    } catch (e) {
        alert("Error submitting workflow: " + e);
    }
}