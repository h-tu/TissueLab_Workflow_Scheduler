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
    
    // Randomize User ID on refresh for easier testing
    document.getElementById('userIdInput').value = "User_" + Math.floor(Math.random() * 1000);
});

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

    // Initialize SVG Overlay
    overlay = viewer.svgOverlay();
}

async function fetchSlideList() {
    try {
        const res = await fetch(`${API_URL}/slides`);
        const data = await res.json();
        
        if (data.slides && data.slides.length > 0) {
            renderSlideList(data.slides);
            loadSlide(data.slides[0].name); 
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
    
    // --- FIX: TRANSPARENT LOADER ---
    // We remove the solid background (bg-slate-900) and use a semi-transparent one.
    // This lets the user see the OpenSeadragon viewer "behind" the spinner immediately.
    loader.classList.remove('hidden');
    loader.classList.remove('bg-slate-900'); // Remove solid color
    loader.classList.add('bg-slate-900/50'); // Add transparent backdrop
    loader.classList.add('pointer-events-none'); // Let clicks pass through to the viewer
    
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

        // Remove loader when the FIRST tile arrives.
        // Because the loader is transparent, the user sees tiles loading before this event too.
        viewer.addOnceHandler('tile-loaded', function() {
            loader.classList.add('hidden');
            // Reset classes for next time
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
        // Stats
        const statusRes = await fetch(`${API_URL}/status`);
        const status = await statusRes.json();
        
        document.getElementById('activeUsersCount').innerText = `${status.active_users_count}/3`;
        document.getElementById('gpuWorkersCount').innerText = `${status.running_jobs}/4`;

        // Workflows
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
                    
                    // Fetch results
                    try {
                        const res = await fetch(`${API_URL}/results/${job.id}`, { headers: { 'X-User-ID': 'sys' } });
                        if (res.ok) {
                            const resultData = await res.json();
                            if (resultData.slide === currentSlideFilename) {
                                drawPolygons(resultData.polygons);
                                showNotification(`${resultData.cell_count} Cells Detected`);
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

async function submitMockWorkflow() {
    const userId = document.getElementById('userIdInput').value;
    const payload = {
        workflow_name: "Analysis_" + Math.floor(Math.random() * 1000),
        branches: [
            {
                branch_name: "Region_1",
                jobs: [ { job_type: "SEGMENTATION", params: {} } ]
            }
        ]
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
        fetchStatus();
    } catch (e) { alert("Error: " + e); }
}