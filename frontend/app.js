// CONFIGURATION
const API_URL = "http://localhost:8000";
const TILE_SERVER_URL = "http://localhost:8000/tiles";

let viewer = null;
let pollInterval = null;

// --- 1. INITIALIZATION ---
document.addEventListener("DOMContentLoaded", () => {
    initViewer();
    startPolling();
    
    // Auto-detect and load the slide list
    fetchSlideList();
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
        minZoomLevel: 1,
        visibilityRatio: 1,
        zoomPerScroll: 2,
    });
}

async function fetchSlideList() {
    try {
        const res = await fetch(`${API_URL}/slides`);
        const data = await res.json();
        
        if (data.slides && data.slides.length > 0) {
            console.log("Found slides:", data.slides);
            
            // 1. Render the list in the sidebar
            renderSlideList(data.slides);
            
            // 2. Auto-load the first one if nothing is loaded
            loadSlide(data.slides[0]); 
        } else {
            document.getElementById('slideList').innerHTML = 
                '<div class="text-xs text-red-400 p-2">No .svs files found in data/inputs</div>';
            document.getElementById('currentSlideName').innerText = "No Data";
        }
    } catch (e) {
        console.error("Could not fetch slide list:", e);
        document.getElementById('slideList').innerHTML = 
            '<div class="text-xs text-red-400 p-2">Backend Connection Failed</div>';
        document.getElementById('currentSlideName').innerText = "Backend Disconnected";
    }
}

function renderSlideList(slides) {
    const container = document.getElementById('slideList');
    
    const html = slides.map(filename => {
        // Truncate long filenames for UI beauty
        const displayName = filename.length > 28 ? filename.substring(0, 25) + "..." : filename;
        
        return `
            <div onclick="loadSlide('${filename}')" 
                 class="slide-item cursor-pointer p-2 rounded hover:bg-slate-700 text-xs text-slate-300 flex items-center gap-2 group">
                <i class="fa-solid fa-file-medical text-slate-500 group-hover:text-blue-400"></i>
                <span title="${filename}">${displayName}</span>
            </div>
        `;
    }).join('');
    
    container.innerHTML = html;
}

async function loadSlide(filename) {
    const loader = document.getElementById('viewerLoader');
    const label = document.getElementById('currentSlideName');
    
    // Show loading state
    loader.classList.remove('hidden');
    label.innerText = "Loading: " + filename;
    
    // Highlight selected item in list (optional visual polish)
    const listItems = document.querySelectorAll('.slide-item');
    listItems.forEach(item => {
        if(item.innerText.includes(filename.substring(0, 10))) {
            item.classList.add('bg-slate-700', 'text-white');
        } else {
            item.classList.remove('bg-slate-700', 'text-white');
        }
    });

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

        viewer.open(tileSource);
        
        viewer.addHandler('open', function() {
            loader.classList.add('hidden');
            label.innerText = filename;
        });

    } catch (error) {
        console.error("Failed to load slide:", error);
        label.innerText = "Error Loading File";
        loader.classList.add('hidden');
    }
}

// --- 3. DASHBOARD LOGIC (POLLING) ---
function startPolling() {
    fetchStatus();
    pollInterval = setInterval(fetchStatus, 1000);
}

async function fetchStatus() {
    const userId = document.getElementById('userIdInput').value;
    
    try {
        // 1. Get System Stats
        const statusRes = await fetch(`${API_URL}/status`);
        const status = await statusRes.json();
        
        document.getElementById('activeUsersCount').innerText = `${status.active_users_count}/3`;
        document.getElementById('gpuWorkersCount').innerText = `${status.running_jobs}/4`;

        // 2. Get Workflows
        const wfRes = await fetch(`${API_URL}/workflows/`, {
            headers: { 'X-User-ID': userId }
        });
        const workflows = await wfRes.json();
        
        renderWorkflows(workflows);

    } catch (e) {
        // Silent error on polling to avoid console spam
    }
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
                <div class="text-xs text-slate-400 flex justify-between">
                    <span>${wf.branches.length} Branches</span>
                    <span>${wf.id.substring(0,8)}...</span>
                </div>
            </div>
        `;
    }).join('');

    container.innerHTML = html;
}

// --- 4. MOCK SUBMISSION ---
async function submitMockWorkflow() {
    const userId = document.getElementById('userIdInput').value;
    
    const payload = {
        workflow_name: "CellSeg_Run_" + Math.floor(Math.random() * 1000),
        branches: [
            {
                branch_name: "ROI_Upper_Left",
                jobs: [
                    { job_type: "SEGMENTATION", params: { region: [0,0,1024,1024] } },
                    { job_type: "TISSUE_MASK", params: { threshold: 0.5 } }
                ]
            },
            {
                branch_name: "ROI_Lower_Right",
                jobs: [
                    { job_type: "SEGMENTATION", params: { region: [1024,1024,2048,2048] } }
                ]
            }
        ]
    };

    try {
        const res = await fetch(`${API_URL}/workflows/`, {
            method: "POST",
            headers: {
                "Content-Type": "application/json",
                "X-User-ID": userId
            },
            body: JSON.stringify(payload)
        });
        
        if (res.status === 403 || res.status === 429) {
            alert("Queue Full! You are now waiting for a slot.");
        }
        fetchStatus();
    } catch (e) {
        alert("Failed to submit workflow: " + e);
    }
}