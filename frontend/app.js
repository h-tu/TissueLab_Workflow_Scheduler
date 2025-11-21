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
let allCellsCache = []; // Stores {polygon:[], area:...}

// --- 1. INITIALIZATION ---
document.addEventListener("DOMContentLoaded", () => {
    initViewer();
    startPolling();
    fetchSlideList();
    
    // Default user
    document.getElementById('userIdInput').value = "User_0";
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
    if(!confirm("Are you sure you want to delete this workflow? This will stop any running jobs.")) return;
    
    try {
        await fetch(`${API_URL}/workflows/${id}`, { 
            method: "DELETE", 
            headers: { 'X-User-ID': document.getElementById('userIdInput').value } 
        });
        fetchStatus(); 
    } catch(e) { console.error(e); }
}

window.cancelJob = async function(jobId) {
    if(!confirm("Cancel this specific job?")) return;
    const userId = document.getElementById('userIdInput').value;
    try {
        await fetch(`${API_URL}/jobs/${jobId}`, { 
            method: "DELETE", 
            headers: { 'X-User-ID': userId } 
        });
        fetchStatus();
    } catch(e) { console.error(e); }
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
        if (data.slides && data.slides.length > 0) {
            renderSlideList(data.slides);
        } else {
            document.getElementById('slideList').innerHTML = 
                '<div class="text-xs text-red-400 p-2">No .svs files found</div>';
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
            if (activeWf) {
                renderDetailsContent(activeWf);
            } else {
                window.closeWorkflowDetails(); 
            }
        }
    } catch (e) { }
}

function checkNewResults(workflows) {
    workflows.forEach(wf => {
        wf.branches.forEach(branch => {
            branch.jobs.forEach(async (job) => {
                // 1. Handle Completed (Notification)
                if (job.status === 'COMPLETED' && !completedJobIds.has(job.id)) {
                    completedJobIds.add(job.id);
                    loadJobResult(job.id, true); 
                }
                
                // 2. Handle Running (Live Streaming)
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

// --- NEW: Function to View Results (Report/Viz) ---
window.viewJobResult = async function(jobId, type) {
    try {
        const res = await fetch(`${API_URL}/results/${jobId}`, { headers: { 'X-User-ID': 'sys' } });
        if(!res.ok) return alert("Result not found");
        const data = await res.json();
        
        let html = '';
        
        if (type === 'REPORT') {
            html = `
                <div class="bg-slate-800 p-4 rounded border border-slate-600 text-slate-200 font-mono text-xs">
                    <h3 class="font-bold text-blue-400 mb-3 text-sm border-b border-slate-700 pb-2">${data.summary || 'Analysis Report'}</h3>
                    <div class="grid grid-cols-2 gap-y-2 gap-x-4">
                        ${Object.entries(data.stats || {}).map(([k,v]) => 
                            `<div class="text-slate-400 capitalize">${k.replace(/_/g, ' ')}:</div>
                             <div class="text-right text-white font-bold">${typeof v === 'number' ? v.toLocaleString(undefined, {maximumFractionDigits:2}) : v}</div>`
                        ).join('')}
                    </div>
                </div>`;
        } else if (type === 'VISUALIZATION') {
             html = `
                <div class="bg-slate-800 p-4 rounded border border-slate-600 text-slate-200 font-mono text-xs">
                    <h3 class="font-bold text-purple-400 mb-3 text-sm border-b border-slate-700 pb-2">Visualization Output</h3>
                    <div class="mb-3 flex items-center gap-2">
                        <i class="fa-solid fa-circle-info text-slate-500"></i>
                        <span>${data.info || 'Process Complete'}</span>
                    </div>
                    <div class="grid grid-cols-2 gap-2 mb-3">
                         <div class="text-slate-400">Resolution:</div>
                         <div class="text-right text-white">${data.resolution || 'N/A'}</div>
                    </div>
                    <div class="text-[10px] italic text-slate-500 bg-slate-900/50 p-2 rounded border border-slate-700/50">
                        * Note: Histogram generation is mocked in this demo environment.
                    </div>
                </div>`;
        } else {
            // Default fallback for segmentation/tissue mask if clicked
            html = `
                <div class="bg-slate-800 p-4 rounded border border-slate-600 text-slate-200 font-mono text-xs">
                    <h3 class="font-bold text-emerald-400 mb-2 text-sm">Segmentation Stats</h3>
                    <div>Total Objects: <span class="text-white font-bold">${data.cell_count || 0}</span></div>
                    <div class="text-xs text-slate-500 mt-2">Overlay is visible on the main viewer.</div>
                </div>
            `;
        }
        
        // Create dynamic modal
        const modal = document.createElement('div');
        modal.className = "fixed inset-0 z-[70] bg-black/60 backdrop-blur-sm flex items-center justify-center animation-fade-in";
        modal.innerHTML = `
            <div class="bg-slate-900 p-6 rounded-xl shadow-2xl border border-slate-700 max-w-md w-full relative transform scale-100 transition-all">
                <button onclick="this.closest('.fixed').remove()" class="absolute top-4 right-4 text-slate-500 hover:text-white transition-colors"><i class="fa-solid fa-xmark fa-lg"></i></button>
                <h2 class="text-lg font-bold text-white mb-4 flex items-center gap-2">
                    <i class="fa-solid fa-square-poll-vertical text-blue-500"></i> Result Data
                </h2>
                ${html}
                <div class="mt-6 flex justify-end">
                    <button onclick="this.closest('.fixed').remove()" class="bg-slate-700 hover:bg-slate-600 text-white px-4 py-2 rounded text-xs font-bold transition-colors">Close</button>
                </div>
            </div>
        `;
        document.body.appendChild(modal);

    } catch(e) { console.error(e); alert("Error loading result data."); }
}

// --- 4. VISUALIZATION OPTIMIZATION ---
function drawVisiblePolygons() {
    if (!overlay || !currentSlideWidth || allCellsCache.length === 0) return;

    const svgNode = overlay.node();
    d3_clear_overlay();
    
    const bounds = viewer.viewport.getBounds();
    const fragment = document.createDocumentFragment();
    
    // Viewport Culling Logic
    const minX = bounds.x * currentSlideWidth;
    const maxX = (bounds.x + bounds.width) * currentSlideWidth;
    const minY = bounds.y * currentSlideWidth;
    const maxY = (bounds.y + bounds.height) * currentSlideWidth;

    let count = 0;
    const maxDraw = 4000; // Safety limit per frame

    for (let i = 0; i < allCellsCache.length; i++) {
        if (count >= maxDraw) break;
        
        const cell = allCellsCache[i];
        const poly = cell.polygon;
        if (!poly || poly.length === 0) continue;

        // Check if first point is in viewport
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

// --- TIME FORMATTING HELPERS ---
function formatDuration(seconds) {
    if (!seconds || seconds < 0) return "0s";
    const m = Math.floor(seconds / 60);
    const s = Math.floor(seconds % 60);
    return m > 0 ? `${m}m ${s}s` : `${s}s`;
}

function getWorkflowTiming(wf, percent) {
    if (!wf.started_at) return { elapsed: "0s", remaining: "Pending" };
    
    const startTime = new Date(wf.started_at).getTime();
    const now = new Date().getTime();
    let elapsedMs = now - startTime;
    
    if (wf.status === 'COMPLETED' || wf.status === 'FAILED' || wf.status === 'CANCELLED') {
        if (wf.completed_at) {
             elapsedMs = new Date(wf.completed_at).getTime() - startTime;
        }
        return { elapsed: formatDuration(elapsedMs / 1000), remaining: "-" };
    }

    const elapsedSec = elapsedMs / 1000;
    let remainingStr = "Calculating...";
    
    if (percent > 0) {
        const totalEstSec = elapsedSec / (percent / 100);
        const remainSec = totalEstSec - elapsedSec;
        remainingStr = formatDuration(remainSec);
    }

    return { elapsed: formatDuration(elapsedSec), remaining: remainingStr };
}

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

function calculateBranchPct(b) {
    let total=0, prog=0; 
    b.jobs.forEach(j=>{
        total++; 
        prog+=(j.status==='COMPLETED'?100:(j.status==='RUNNING'?(j.progress||0):0))
    });
    return total>0?Math.round(prog/total):0;
}

// --- SIDEBAR WORKFLOW LIST ---
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
        const timing = getWorkflowTiming(wf, percent);
        
        const deleteBtn = (wf.status === "COMPLETED" || wf.status === "FAILED" || wf.status === "CANCELLED") ? 
            `<button onclick="deleteWorkflow('${wf.id}', event)" class="text-slate-500 hover:text-red-400 p-1"><i class="fa-solid fa-trash-can"></i></button>` : '';

        const branchBars = wf.branches.map(b => {
            const bPct = calculateBranchPct(b);
            return `<div class="h-1 flex-1 rounded-full bg-slate-600 overflow-hidden" title="${b.name}"><div class="h-full bg-blue-400" style="width: ${bPct}%"></div></div>`;
        }).join('');

        return `
            <div onclick="openWorkflowDetails('${wf.id}')" 
                 class="bg-slate-700 rounded-lg p-3 border border-slate-600 shadow-sm hover:border-blue-500/50 transition-all cursor-pointer relative group">
                <div class="flex justify-between items-start mb-1">
                    <div>
                        <div class="font-bold text-sm text-slate-200">${wf.name}</div>
                        <div class="text-[10px] text-slate-400 font-mono mt-1">
                             <span class="text-blue-300"><i class="fa-regular fa-clock"></i> ${timing.elapsed}</span>
                             <span class="ml-2 text-slate-500">ETA: ${timing.remaining}</span>
                        </div>
                    </div>
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

// --- TREE VISUALIZATION RENDERER (DETAILS MODAL) ---
function renderDetailsContent(wf) {
    const percent = calculateWorkflowStats(wf);
    const timing = getWorkflowTiming(wf, percent);

    document.getElementById('detailWfName').innerHTML = `
        <div class="flex justify-between items-center w-full">
            <div>
                <div>${wf.name}</div>
                <div class="text-xs font-normal text-slate-400 mt-1 font-mono">
                    Elapsed: <span class="text-white">${timing.elapsed}</span> &bull; Remaining: <span class="text-white">${timing.remaining}</span>
                </div>
            </div>
            <span class="text-sm font-mono text-blue-400 bg-blue-900/30 px-2 py-1 rounded">${percent}%</span>
        </div>`;
    document.getElementById('detailWfId').innerText = "ID: " + wf.id;

    // TREE CONTAINER - Updated gap-8 to gap-16 here!
    let treeHtml = `
    <div class="flex flex-col items-center pt-4 pb-10 w-full overflow-x-auto">
        <!-- Root Node -->
        <div class="flex flex-col items-center mb-0 relative z-10">
            <div class="bg-slate-900 border border-blue-500/50 text-blue-100 px-4 py-2 rounded-lg shadow-lg font-bold text-sm flex items-center gap-2">
                <i class="fa-solid fa-play-circle text-blue-400"></i> Start
            </div>
            <div class="h-4 w-0.5 bg-slate-600"></div>
        </div>
        
        <!-- Branches Container -->
        <div class="flex justify-center gap-16 items-start relative">
             ${wf.branches.length > 1 ? `
                <div class="absolute h-0.5 bg-slate-600" 
                     style="top: 0; left: calc(${100/(2*wf.branches.length)}%); right: calc(${100/(2*wf.branches.length)}%);">
                </div>
             ` : ''}
             
             ${wf.branches.map(renderBranchNode).join('')}
        </div>
    </div>`;
    
    document.getElementById('detailContent').innerHTML = treeHtml;
}

function renderBranchNode(branch) {
    return `
    <div class="flex flex-col items-center relative">
        <!-- Branch Connector Line (Up to bar) -->
        <div class="h-4 w-0.5 bg-slate-600"></div>
        
        <!-- Branch Header -->
        <div class="bg-slate-800 border border-slate-600 text-slate-200 px-3 py-1.5 rounded-md text-xs font-bold shadow mb-4 z-10 min-w-[120px] text-center flex flex-col items-center gap-1">
             <span>${branch.name}</span>
             <span class="text-[9px] font-mono text-slate-400">${branch.status}</span>
        </div>

        <!-- Jobs Stack -->
        <div class="flex flex-col items-center gap-0">
            ${branch.jobs.map((job, idx) => renderJobNode(job, idx, branch.jobs.length)).join('')}
        </div>
    </div>`;
}

function renderJobNode(job, idx, total) {
    let borderClass = "border-slate-600";
    let bgClass = "bg-slate-800";
    let icon = '<i class="fa-solid fa-circle text-slate-600"></i>';
    let statusText = "PENDING";

    if (job.status === 'RUNNING') {
        borderClass = "border-blue-500 animate-pulse";
        bgClass = "bg-slate-800/80";
        icon = '<i class="fa-solid fa-spinner fa-spin text-blue-400"></i>';
        statusText = `${job.progress}%`;
    } else if (job.status === 'COMPLETED') {
        borderClass = "border-emerald-500";
        bgClass = "bg-emerald-900/10";
        icon = '<i class="fa-solid fa-check text-emerald-400"></i>';
        statusText = "DONE";
    } else if (job.status === 'FAILED') {
        borderClass = "border-red-500";
        icon = '<i class="fa-solid fa-xmark text-red-400"></i>';
        statusText = "FAIL";
    } else if (job.status === 'CANCELLED') {
        borderClass = "border-slate-500";
        icon = '<i class="fa-solid fa-ban text-slate-400"></i>';
        statusText = "STOP";
    }

    // Action Button logic: Cancel if running, View Result if completed
    let actionBtn = '';
    if(job.status === 'PENDING' || job.status === 'RUNNING') {
        actionBtn = `<button onclick="cancelJob('${job.id}')" class="absolute -right-8 top-2 text-red-400 hover:text-white text-[10px] bg-slate-900 rounded p-1 border border-slate-700 transition-colors" title="Cancel Job"><i class="fa-solid fa-ban"></i></button>`;
    } else if (job.status === 'COMPLETED') {
        // Show "View" button for all completed jobs (Report, Viz, even Seg/Mask to show counts)
        actionBtn = `<button onclick="viewJobResult('${job.id}', '${job.job_type}')" class="absolute -right-8 top-2 text-blue-400 hover:text-white text-[10px] bg-slate-900 rounded p-1 border border-slate-700 transition-colors" title="View Result"><i class="fa-solid fa-eye"></i></button>`;
    }

    return `
    ${idx > 0 ? '<div class="job-arrow"><i class="fa-solid fa-arrow-down"></i></div>' : ''}
    <div class="tree-node relative group w-40">
        <div class="${bgClass} border ${borderClass} rounded p-2 shadow-sm flex items-center gap-2 relative z-10 transition-all hover:scale-105">
            <div class="text-xs text-slate-300 flex-1 font-medium truncate" title="${job.job_type}">${job.job_type}</div>
            <div class="text-[10px] font-mono text-slate-400 flex items-center gap-1">
                ${statusText} ${icon}
            </div>
        </div>
        ${actionBtn}
    </div>`;
}

// --- TREE-STYLE WORKFLOW BUILDER ---
window.openBuilder = function() {
    if (!currentSlideFilename) return alert("Select a slide first.");
    document.getElementById('builderModal').classList.remove('hidden');
    const container = document.getElementById('builderBranches');
    container.innerHTML = ''; 
    
    // Set default name
    document.getElementById('buildWfName').value = "Analysis_" + currentSlideFilename.split('.')[0];
    
    // Render the "Add Branch" button FIRST so logic works correctly
    renderAddBranchBtn();

    // Start with 1 branch by default (this will insert BEFORE the Add button)
    addBuilderBranch();
    
    setTimeout(updateBuilderIndicators, 100);
}

window.closeBuilder = function() { document.getElementById('builderModal').classList.add('hidden'); }

function renderAddBranchBtn() {
    const container = document.getElementById('builderBranches');
    // Ensure no duplicates
    if (document.getElementById('btn-add-branch-wrapper')) return;

    const wrapper = document.createElement('div');
    wrapper.id = 'btn-add-branch-wrapper';
    wrapper.className = "flex flex-col items-center justify-start min-w-[120px] h-full pt-8 opacity-50 hover:opacity-100 transition-all group";
    
    wrapper.innerHTML = `
        <!-- Dashed Line -->
        <div class="h-0.5 w-8 bg-slate-600 mb-2 hidden"></div>
        
        <button onclick="addBuilderBranch()" class="border-2 border-dashed border-slate-600 group-hover:border-blue-500/50 bg-slate-800/30 rounded-lg p-6 flex flex-col items-center gap-2 text-slate-500 group-hover:text-blue-400 transition-all shadow-inner">
            <div class="h-10 w-10 rounded-full bg-slate-700 flex items-center justify-center mb-1">
                <i class="fa-solid fa-plus text-lg"></i>
            </div>
            <span class="font-bold text-[10px] uppercase tracking-wide">Add Branch</span>
        </button>
    `;
    container.appendChild(wrapper);
}

window.addBuilderBranch = function() {
    const container = document.getElementById('builderBranches');
    const addBtnWrapper = document.getElementById('btn-add-branch-wrapper');
    
    const branchId = 'branch_' + Date.now() + Math.random();
    
    // Create Branch Visual Container (Column)
    const div = document.createElement('div');
    div.className = "flex flex-col items-center min-w-[200px] builder-branch relative";
    div.dataset.id = branchId;
    
    div.innerHTML = `
        <!-- Branch Head -->
        <div class="relative w-full flex flex-col items-center">
            <!-- Top connector lines (for tree effect) -->
            <div class="h-4 w-0.5 bg-slate-600 mb-0 branch-line-top hidden"></div> 
            
            <div class="bg-slate-800 border border-slate-600 p-2 rounded shadow-lg z-10 w-full flex flex-col gap-2">
                <div class="flex justify-between items-center">
                    <span class="text-[10px] font-bold text-slate-400 uppercase">Branch</span>
                    <button onclick="removeBuilderBranch(this)" class="text-red-400 hover:text-white text-[10px]"><i class="fa-solid fa-trash"></i></button>
                </div>
                <input type="text" value="Branch ${document.querySelectorAll('.builder-branch').length + 1}" 
                       class="branch-name-input bg-slate-900 border border-slate-700 rounded text-xs px-2 py-1 text-white w-full text-center focus:border-blue-500 outline-none transition-colors">
            </div>
            <!-- Jobs Container (Vertical Stack) -->
            <div class="flex flex-col items-center w-full pt-2 jobs-container gap-2"></div>
            
            <!-- Add Job Button -->
            <button onclick="addBuilderJob(this)" class="mt-2 text-blue-400 hover:text-blue-300 text-xs border border-dashed border-blue-500/30 rounded px-3 py-1 w-full hover:bg-blue-500/10 transition-colors">
                <i class="fa-solid fa-plus"></i> Add Job
            </button>
        </div>
    `;
    
    // Insert BEFORE the "Add Branch" button
    if (addBtnWrapper) {
        container.insertBefore(div, addBtnWrapper);
    } else {
        container.appendChild(div);
    }
    
    // Add initial job
    const btn = div.querySelector('button[onclick^="addBuilderJob"]');
    addBuilderJob(btn);
    
    updateBranchLines();
}

window.updateBranchLines = function() {
    const branches = document.querySelectorAll('.builder-branch');
    // Logic for tree connectors could go here (e.g. showing horizontal bar)
    // For now, just ensure vertical stems are visible
    branches.forEach(b => {
        b.querySelector('.branch-line-top').classList.remove('hidden');
    });
}

window.removeBuilderBranch = function(btn) {
    btn.closest('.builder-branch').remove();
    updateBranchLines();
    updateBuilderIndicators();
}

window.addBuilderJob = function(btn) {
    const branchDiv = btn.closest('.builder-branch').querySelector('.jobs-container');
    const div = document.createElement('div');
    div.className = "builder-job w-full relative";
    
    // If not first job, show arrow
    const arrow = branchDiv.children.length > 0 ? '<div class="text-center text-slate-600 text-[10px] py-0.5"><i class="fa-solid fa-arrow-down"></i></div>' : '';
    
    div.innerHTML = `
        ${arrow}
        <div class="bg-slate-700/50 border border-slate-600 rounded p-2 flex items-center gap-2 group hover:border-blue-500/50 transition-colors">
            <div class="h-6 w-6 rounded bg-slate-800 flex items-center justify-center text-[10px] text-slate-400 font-mono shadow-inner">J</div>
            <div class="flex-1 flex flex-col">
                <select class="job-type-select bg-transparent text-white text-xs outline-none font-medium" onchange="updateBuilderIndicators()">
                    <option value="SEGMENTATION">Nuclei Segmentation</option>
                    <option value="TISSUE_MASK">Tissue Mask</option>
                    <option value="VISUALIZATION">Histogram Viz</option>
                    <option value="REPORT">Generate Report</option>
                </select>
                <span class="smart-mask-indicator text-[9px] text-amber-400 hidden"><i class="fa-solid fa-bolt"></i> Smart Optimized</span>
            </div>
            <button onclick="removeBuilderJob(this)" class="text-slate-500 hover:text-red-400 opacity-0 group-hover:opacity-100 transition-opacity"><i class="fa-solid fa-xmark"></i></button>
        </div>`;
        
    branchDiv.appendChild(div);
    updateBuilderIndicators();
}

window.removeBuilderJob = function(btn) {
    btn.closest('.builder-job').remove();
    updateBuilderIndicators();
}

// --- UPDATED: Smart Optimization Logic (Per Branch) ---
function updateBuilderIndicators() {
    // 1. Check History for this slide (Server-side completed masks)
    const hasHistoryMask = currentWorkflowsCache.some(wf => 
        wf.slide_name === currentSlideFilename && 
        wf.branches.some(b => b.jobs.some(j => j.job_type === 'TISSUE_MASK' && j.status === 'COMPLETED'))
    );

    // 2. Check Local Dependencies (Iterate PER BRANCH)
    document.querySelectorAll('.builder-branch').forEach(branchDiv => {
        let hasLocalMask = false;

        // Get all jobs in this branch in sequential order
        branchDiv.querySelectorAll('.builder-job').forEach(jobDiv => {
            const select = jobDiv.querySelector('.job-type-select');
            const indicator = jobDiv.querySelector('.smart-mask-indicator');
            
            if(!select || !indicator) return;

            // Determine visibility for THIS job
            if (select.value === 'SEGMENTATION') {
                // Smart optimized if: Global history exists OR a mask is upstream in this branch
                if (hasHistoryMask || hasLocalMask) {
                    indicator.classList.remove('hidden');
                } else {
                    indicator.classList.add('hidden');
                }
            } else {
                indicator.classList.add('hidden');
            }

            // Update state for the NEXT job in this branch
            // (If this job IS a mask, set flag to true for subsequent jobs)
            if (select.value === 'TISSUE_MASK') {
                hasLocalMask = true;
            }
        });
    });
}

window.submitBuilder = async function() {
    const userId = document.getElementById('userIdInput').value;
    const branches = [];
    
    document.querySelectorAll('.builder-branch').forEach(bDiv => {
        const branchName = bDiv.querySelector('.branch-name-input').value;
        const jobs = [];
        
        bDiv.querySelectorAll('.job-type-select').forEach(sel => {
            jobs.push({ job_type: sel.value, params: {} });
        });
        
        if (jobs.length > 0) {
            branches.push({ branch_name: branchName, jobs: jobs });
        }
    });

    if (branches.length === 0) return alert("Please add at least one branch with jobs.");

    try {
        const res = await fetch(`${API_URL}/workflows/`, {
            method: "POST",
            headers: { "Content-Type": "application/json", "X-User-ID": userId },
            body: JSON.stringify({ 
                workflow_name: document.getElementById('buildWfName').value, 
                slide_name: currentSlideFilename, 
                branches: branches 
            })
        });
        
        if (!res.ok) {
            const err = await res.json();
            return alert(err.detail || "Error submitting workflow");
        }
        
        closeBuilder(); 
        fetchStatus();
    } catch (e) { alert("Error: " + e); }
}