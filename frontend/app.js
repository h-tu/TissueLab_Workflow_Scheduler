// CONFIGURATION
const API_URL = "http://localhost:8000";
const TILE_SERVER_URL = "http://localhost:8000/tiles";

let viewer = null;
let overlay = null;
let pollInterval = null;
let currentSlideFilename = null;
let currentSlideWidth = 0; 
let completedJobIds = new Set();
let isOverlayVisible = true;
let currentWorkflowsCache = []; 
let currentDetailWfId = null;

// --- NEW: Rendering Performance Cache ---
let allCellsCache = []; // Stores {polygon:[], area:...}

document.addEventListener("DOMContentLoaded", () => {
    initViewer();
    startPolling();
    fetchSlideList();
    document.getElementById('userIdInput').value = "User_0";
    
    const modal = document.getElementById('detailsModal');
    if (modal) {
        const closeBtn = modal.querySelector('button');
        if(closeBtn) closeBtn.onclick = () => closeWorkflowDetails();
    }
});

// Ensure these are globally available for HTML onclick attributes
window.setUserId = function(id) {
    document.getElementById('userIdInput').value = "User_" + id;
    fetchStatus(); 
}

window.openWorkflowDetails = function(wfId) {
    const wf = currentWorkflowsCache.find(w => w.id === wfId);
    if(!wf) return;
    currentDetailWfId = wfId;
    renderDetailsContent(wf);
    document.getElementById('detailsModal').classList.remove('hidden');
}

window.closeWorkflowDetails = function() {
    currentDetailWfId = null;
    document.getElementById('detailsModal').classList.add('hidden');
}

window.deleteWorkflow = async function(id, event) {
    if(event) event.stopPropagation(); 
    if(!confirm("Delete workflow?")) return;
    await fetch(`${API_URL}/workflows/${id}`, { method: "DELETE", headers: { 'X-User-ID': document.getElementById('userIdInput').value } });
    fetchStatus(); 
}

window.cancelJob = async function(jobId) {
    if(!confirm("Cancel this specific job?")) return;
    const userId = document.getElementById('userIdInput').value;
    await fetch(`${API_URL}/jobs/${jobId}`, { method: "DELETE", headers: { 'X-User-ID': userId } });
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
        minZoomLevel: 0, 
        visibilityRatio: 1,
        zoomPerScroll: 2,
    });

    overlay = viewer.svgOverlay();

    // Optimization: Redraw only on view change with debounce
    viewer.addHandler('viewport-change', () => {
        if(window.redrawTimeout) clearTimeout(window.redrawTimeout);
        window.redrawTimeout = setTimeout(drawVisiblePolygons, 50); 
    });
    
    viewer.addHandler('animation-finish', drawVisiblePolygons);
}

async function fetchSlideList() {
    try {
        const res = await fetch(`${API_URL}/slides`);
        const data = await res.json();
        if (data.slides && data.slides.length > 0) renderSlideList(data.slides);
        else {
            document.getElementById('slideList').innerHTML = '<div class="text-xs text-red-400 p-2">No .svs files found</div>';
            document.getElementById('currentSlideName').innerText = "No Data";
        }
    } catch (e) { console.error(e); }
}

function renderSlideList(slides) {
    const container = document.getElementById('slideList');
    const html = slides.map(slide => {
        const filename = slide.name;
        const displayName = filename.length > 20 ? filename.substring(0, 18) + "..." : filename;
        return `
            <div onclick="loadSlide('${filename}')" 
                 class="slide-item cursor-pointer p-3 rounded hover:bg-slate-700 text-slate-300 group border border-transparent hover:border-slate-600 transition-all mb-1">
                <div class="flex items-center gap-2 mb-1">
                    <i class="fa-solid fa-file-medical text-slate-500 group-hover:text-blue-400"></i>
                    <span class="font-medium text-xs text-white" title="${filename}">${displayName}</span>
                </div>
                <div class="flex justify-between text-[10px] text-slate-400 pl-5 opacity-80">
                    <span>${slide.dimensions || "Unknown"}</span>
                    <span>${slide.size || "Unknown"}</span>
                </div>
            </div>
        `;
    }).join('');
    container.innerHTML = html;
}

window.loadSlide = async function(filename) {
    const loader = document.getElementById('viewerLoader');
    const label = document.getElementById('currentSlideName');
    const prompt = document.getElementById('selectSlidePrompt');
    
    if (prompt) prompt.classList.add('hidden');
    loader.classList.remove('hidden');
    loader.classList.add('bg-slate-900/50'); 
    
    label.innerText = "Loading: " + filename;
    currentSlideFilename = filename; 
    if(overlay) d3_clear_overlay(); 
    allCellsCache = []; 

    try {
        const response = await fetch(`${API_URL}/slides/${filename}/info`);
        if (!response.ok) throw new Error("Slide not found");
        
        const info = await response.json();
        currentSlideWidth = info.width; 

        viewer.open({
            height: info.height,
            width: info.width,
            tileSize: 256,
            minLevel: 0,
            maxLevel: info.level_count - 1,
            getTileUrl: (level, x, y) => `${TILE_SERVER_URL}/${filename}/${level}/${x}_${y}.jpeg`
        });

        viewer.addOnceHandler('tile-loaded', function() {
            loader.classList.add('hidden');
            label.innerText = filename;
        });

        checkForExistingResults();

    } catch (error) {
        console.error(error);
        label.innerText = "Error Loading File";
        loader.classList.add('hidden');
    }
}

// --- 3. POLLING & DASHBOARD ---
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
        document.getElementById('pendingJobsCount').innerText = status.pending_jobs_count || 0;
        document.getElementById('avgLatency').innerText = (status.avg_job_latency || 0) + "s";

        const wfRes = await fetch(`${API_URL}/workflows/`, { headers: { 'X-User-ID': userId } });
        const workflows = await wfRes.json();
        
        currentWorkflowsCache = workflows; 
        renderWorkflows(workflows);
        checkNewResults(workflows); 

        if (currentDetailWfId) {
            const activeWf = workflows.find(w => w.id === currentDetailWfId);
            if (activeWf) renderDetailsContent(activeWf);
            else window.closeWorkflowDetails(); 
        }
    } catch (e) { }
}

function checkNewResults(workflows) {
    workflows.forEach(wf => {
        wf.branches.forEach(branch => {
            branch.jobs.forEach(async (job) => {
                // 1. Handle Completed
                if (job.status === 'COMPLETED' && !completedJobIds.has(job.id)) {
                    completedJobIds.add(job.id);
                    loadJobResult(job.id, true); 
                }
                
                // 2. Handle Running (Live Streaming)
                // FIX: Check wf.slide_name instead of job.slide_name (which is undefined)
                if (job.status === 'RUNNING' && wf.slide_name === currentSlideFilename) {
                    loadJobResult(job.id, false); 
                }
            });
        });
    });
}

function checkForExistingResults() {
    if(!currentWorkflowsCache) return;
    currentWorkflowsCache.forEach(wf => {
        if(wf.slide_name !== currentSlideFilename) return;
        wf.branches.forEach(branch => {
            branch.jobs.forEach(job => {
                if(job.status === 'COMPLETED' || job.status === 'RUNNING') loadJobResult(job.id, false);
            });
        });
    });
}

async function loadJobResult(jobId, showNotify) {
    try {
        const res = await fetch(`${API_URL}/results/${jobId}`, { headers: { 'X-User-ID': 'sys' } });
        if (res.ok) {
            const resultData = await res.json();
            if (resultData.slide === currentSlideFilename) {
                // Handle new data structure (cells) or old (polygons)
                if (resultData.cells) {
                    allCellsCache = resultData.cells;
                } else if (resultData.polygons) {
                    allCellsCache = resultData.polygons.map(p => ({ polygon: p }));
                }

                drawVisiblePolygons(); // Trigger optimized draw
                if(showNotify) showNotification(`${resultData.cell_count} Objects`);
            }
        }
    } catch (err) {}
}

// --- 4. VISUALIZATION OPTIMIZATION ---
function drawVisiblePolygons() {
    if (!overlay || !currentSlideWidth || allCellsCache.length === 0) return;

    // Removed aggressive zoom check so results are visible even when zoomed out
    const svgNode = overlay.node();
    d3_clear_overlay();
    
    const bounds = viewer.viewport.getBounds();
    const fragment = document.createDocumentFragment();
    
    // Viewport Culling
    const minX = bounds.x * currentSlideWidth;
    const maxX = (bounds.x + bounds.width) * currentSlideWidth;
    const minY = bounds.y * currentSlideWidth;
    const maxY = (bounds.y + bounds.height) * currentSlideWidth;

    let count = 0;
    const maxDraw = 4000; // Limit DOM nodes per frame

    for (let i = 0; i < allCellsCache.length; i++) {
        if (count >= maxDraw) break;
        
        const cell = allCellsCache[i];
        const poly = cell.polygon;
        if (!poly || poly.length === 0) continue;

        // Check first point
        const px = poly[0][0];
        const py = poly[0][1];

        if (px > minX && px < maxX && py > minY && py < maxY) {
            const path = document.createElementNS("http://www.w3.org/2000/svg", "polygon");
            const pointsStr = poly.map(p => `${p[0]/currentSlideWidth},${p[1]/currentSlideWidth}`).join(" ");
            path.setAttribute("points", pointsStr);
            path.setAttribute("class", "cell-polygon");
            if(cell.area) path.innerHTML = `<title>Area: ${cell.area}</title>`;
            fragment.appendChild(path);
            count++;
        }
    }
    
    svgNode.appendChild(fragment);
    overlay.resize(); 
    
    const btn = document.getElementById('btnToggleOverlay');
    if(btn) {
        btn.classList.add('text-blue-400');
        btn.classList.remove('text-slate-400');
    }
}

function d3_clear_overlay() {
    if (overlay) {
        const svgNode = overlay.node();
        while (svgNode.firstChild) svgNode.removeChild(svgNode.firstChild);
    }
}

window.toggleOverlay = function() {
    if (!overlay) return;
    const svgNode = overlay.node();
    isOverlayVisible = !isOverlayVisible;
    svgNode.style.display = isOverlayVisible ? 'block' : 'none';
    
    const btn = document.getElementById('btnToggleOverlay');
    if (isOverlayVisible) {
        btn.classList.add('text-blue-400');
        btn.classList.remove('text-slate-400');
        drawVisiblePolygons();
    } else {
        btn.classList.add('text-slate-400');
        btn.classList.remove('text-blue-400');
    }
}

function showNotification(msg) {
    const toast = document.getElementById('notificationToast');
    if(!toast) return;
    document.getElementById('notificationText').innerText = msg;
    toast.classList.remove('-translate-y-32');
    setTimeout(() => toast.classList.add('-translate-y-32'), 4000);
}

// --- WORKFLOW RENDERING ---

function calculateWorkflowStats(wf) {
    let totalJobs = 0;
    let totalWeightedProgress = 0;
    wf.branches.forEach(b => {
        b.jobs.forEach(j => {
            totalJobs++;
            if (j.status === 'COMPLETED') totalWeightedProgress += 100;
            else if (j.status === 'RUNNING') totalWeightedProgress += (j.progress || 0);
        });
    });
    return totalJobs > 0 ? Math.round(totalWeightedProgress / totalJobs) : 0;
}

function renderWorkflows(workflows) {
    const container = document.getElementById('workflowList');
    if (!workflows || workflows.length === 0) {
        container.innerHTML = `<div class="text-center text-slate-500 text-sm mt-10">No workflows found.</div>`;
        return;
    }

    const html = workflows.map(wf => {
        let statusColor = "bg-slate-600";
        if (wf.status === "RUNNING") statusColor = "bg-blue-500 animate-pulse";
        if (wf.status === "COMPLETED") statusColor = "bg-emerald-500";
        if (wf.status === "FAILED") statusColor = "bg-red-500";
        if (wf.status === "PENDING") statusColor = "bg-amber-500";

        const percent = calculateWorkflowStats(wf);
        const deleteBtn = (wf.status === "COMPLETED" || wf.status === "FAILED" || wf.status === "CANCELLED") ? 
            `<button onclick="deleteWorkflow('${wf.id}', event)" class="text-slate-500 hover:text-red-400 p-1"><i class="fa-solid fa-trash-can"></i></button>` : '';

        const branchBars = wf.branches.map(b => {
            let bJobs = 0, bProg = 0;
            b.jobs.forEach(j => {
                bJobs++;
                if(j.status === 'COMPLETED') bProg += 100;
                else if(j.status === 'RUNNING') bProg += (j.progress || 0);
            });
            const bPct = bJobs > 0 ? Math.round(bProg / bJobs) : 0;
            return `<div class="h-1 flex-1 rounded-full bg-slate-600 overflow-hidden" title="${b.name}"><div class="h-full bg-blue-400" style="width: ${bPct}%"></div></div>`;
        }).join('');

        return `
            <div onclick="openWorkflowDetails('${wf.id}')" 
                 class="bg-slate-700 rounded-lg p-3 border border-slate-600 shadow-sm hover:border-blue-500/50 transition-all cursor-pointer relative group">
                <div class="flex justify-between items-start mb-1">
                    <div><div class="font-bold text-sm text-slate-200">${wf.name}</div></div>
                    <div class="flex flex-col items-end gap-1">
                        <div class="flex items-center gap-2">
                             <span class="text-xs font-mono font-bold text-blue-300">${percent}%</span>
                             <span class="text-[10px] font-mono px-2 py-0.5 rounded-full text-white ${statusColor}">${wf.status}</span>
                        </div>
                        ${deleteBtn}
                    </div>
                </div>
                <div class="flex gap-1 mt-2">${branchBars}</div>
            </div>
        `;
    }).join('');
    container.innerHTML = html;
}

function renderDetailsContent(wf) {
    const percent = calculateWorkflowStats(wf);
    document.getElementById('detailWfName').innerHTML = `
        <div class="flex justify-between items-center w-full">
            <span>${wf.name}</span>
            <span class="text-sm font-mono text-blue-400 bg-blue-900/30 px-2 py-1 rounded">${percent}%</span>
        </div>`;
    document.getElementById('detailWfId').innerText = "ID: " + wf.id;
    
    let contentHtml = `<div class="space-y-0 divide-y divide-slate-700">`;
    wf.branches.forEach(branch => {
        contentHtml += `<div class="p-4 bg-slate-800"><div class="flex items-center gap-2 mb-3"><i class="fa-solid fa-code-branch text-slate-500 text-xs"></i><span class="text-sm font-bold text-slate-300">${branch.name}</span></div><div class="space-y-2 pl-4 border-l border-slate-700">`;
        
        branch.jobs.forEach(job => {
            let icon = '<i class="fa-regular fa-circle text-slate-600"></i>', textColor = 'text-slate-400', extra = '';
            let actionBtn = '';

            if(job.status === 'PENDING' || job.status === 'RUNNING') {
                actionBtn = `<button onclick="cancelJob('${job.id}')" class="text-[10px] text-red-400 hover:text-red-200 border border-red-900 px-1 rounded ml-2">Cancel</button>`;
            }

            if(job.status === 'RUNNING') {
                icon = '<i class="fa-solid fa-circle-notch fa-spin text-blue-400"></i>'; textColor = 'text-blue-300';
                extra = `<span class="text-[10px] font-mono ml-2 bg-blue-900/50 text-blue-200 px-1 rounded">${job.progress || 0}%</span>`;
            } else if(job.status === 'COMPLETED') {
                icon = '<i class="fa-solid fa-circle-check text-emerald-400"></i>'; textColor = 'text-emerald-300';
            } else if(job.status === 'FAILED') {
                icon = '<i class="fa-solid fa-circle-xmark text-red-400"></i>'; textColor = 'text-red-300';
            } else if(job.status === 'CANCELLED') {
                icon = '<i class="fa-solid fa-ban text-slate-400"></i>'; textColor = 'text-slate-400';
            }

            contentHtml += `
                <div class="flex justify-between items-center text-sm">
                    <div class="flex items-center"><span class="${textColor}">${job.job_type}</span>${extra}</div>
                    <div class="flex items-center gap-2">${actionBtn}${icon}<span class="text-xs font-mono text-slate-500">${job.status}</span></div>
                </div>`;
        });
        contentHtml += `</div></div>`;
    });
    contentHtml += `</div>`;
    document.getElementById('detailContent').innerHTML = contentHtml;
}

// --- WORKFLOW BUILDER ---
window.openBuilder = function() {
    if (!currentSlideFilename) return alert("Select a slide first.");
    document.getElementById('builderModal').classList.remove('hidden');
    document.getElementById('builderBranches').innerHTML = ''; 
    addBuilderBranch(); 
    document.getElementById('buildWfName').value = "Analysis_" + currentSlideFilename.split('.')[0];
}
window.closeBuilder = function() { document.getElementById('builderModal').classList.add('hidden'); }

window.addBuilderBranch = function() {
    const container = document.getElementById('builderBranches');
    const branchId = 'branch_' + Date.now();
    const div = document.createElement('div');
    div.className = "bg-slate-900 p-3 rounded border border-slate-700 builder-branch";
    div.dataset.id = branchId;
    div.innerHTML = `
        <div class="flex justify-between items-center mb-2">
            <div class="flex gap-2 items-center w-full"><span class="text-xs font-bold text-slate-400">BRANCH</span><input type="text" value="Region 1" class="branch-name-input bg-slate-800 border border-slate-600 rounded text-xs px-2 py-1 text-white w-1/2"></div>
            <button onclick="this.closest('.builder-branch').remove()" class="text-red-400 hover:text-red-300 text-xs"><i class="fa-solid fa-trash"></i></button>
        </div>
        <div class="space-y-2 pl-2 border-l-2 border-slate-700 jobs-container"></div>
        <button onclick="addBuilderJob('${branchId}')" class="mt-2 text-[10px] text-blue-400 hover:text-blue-300 font-bold uppercase"><i class="fa-solid fa-plus"></i> Add Job</button>`;
    container.appendChild(div);
    addBuilderJob(branchId);
}

window.addBuilderJob = function(branchId) {
    const branchDiv = document.querySelector(`.builder-branch[data-id="${branchId}"] .jobs-container`);
    const div = document.createElement('div');
    div.className = "flex gap-2 items-center builder-job";
    div.innerHTML = `<div class="h-6 w-6 rounded bg-slate-700 flex items-center justify-center text-[10px] text-slate-400 font-mono">J</div><select class="job-type-select bg-slate-800 border border-slate-600 text-white text-xs rounded px-2 py-1 flex-1"><option value="SEGMENTATION">Nuclei Segmentation (InstanSeg)</option><option value="TISSUE_MASK">Tissue Mask Generation</option></select><button onclick="this.closest('.builder-job').remove()" class="text-slate-500 hover:text-red-400"><i class="fa-solid fa-minus"></i></button>`;
    branchDiv.appendChild(div);
}

window.submitBuilder = async function() {
    const userId = document.getElementById('userIdInput').value;
    const branches = [];
    document.querySelectorAll('.builder-branch').forEach(bDiv => {
        const jobs = [];
        bDiv.querySelectorAll('.builder-job').forEach(jDiv => jobs.push({ job_type: jDiv.querySelector('.job-type-select').value, params: {} }));
        if (jobs.length > 0) branches.push({ branch_name: bDiv.querySelector('.branch-name-input').value, jobs: jobs });
    });
    if (branches.length === 0) return alert("Add at least one job.");

    try {
        const res = await fetch(`${API_URL}/workflows/`, {
            method: "POST",
            headers: { "Content-Type": "application/json", "X-User-ID": userId },
            body: JSON.stringify({ workflow_name: document.getElementById('buildWfName').value, slide_name: currentSlideFilename, branches: branches })
        });
        if (!res.ok) return alert((await res.json()).detail || "Error");
        closeBuilder(); fetchStatus();
    } catch (e) { alert("Error: " + e); }
}