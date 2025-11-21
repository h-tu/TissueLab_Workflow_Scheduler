#-------------------------------------------------------------------------------
# File:        run.sh
# Description: Local development script to set up the Conda environment and run the backend/frontend servers.
# Author:      Hongyu Tu
# Created:     Nov 20, 2025
#-------------------------------------------------------------------------------
#!/bin/bash

cleanup() {
    echo ""
    echo "Shutting down servers..."
    if [ -n "$BACKEND_PID" ]; then
        kill $BACKEND_PID 2>/dev/null
    fi
    if [ -n "$FRONTEND_PID" ]; then
        kill $FRONTEND_PID 2>/dev/null
    fi
    exit
}

trap cleanup SIGINT

if [[ "$CONDA_DEFAULT_ENV" != "tissuelab" ]]; then
    echo "'tissuelab' env not active. Attempting to activate..."
    CONDA_BASE=$(conda info --base)
    if [ -f "$CONDA_BASE/etc/profile.d/conda.sh" ]; then
        source "$CONDA_BASE/etc/profile.d/conda.sh"
        conda activate tissuelab
    else
        echo "Could not find conda.sh. Please run 'conda activate tissuelab' manually."
        exit 1
    fi
fi

echo "Environment: $CONDA_DEFAULT_ENV"

echo "Starting Backend (Port 8000)..."
uvicorn backend.main:app --reload --host 0.0.0.0 --port 8000 &
BACKEND_PID=$!
sleep 2

echo "Starting Frontend (Port 3000)..."
python -m http.server 3000 --directory frontend &
FRONTEND_PID=$!

echo ""
echo "=================================================="
echo "  TissueLab is running!"
echo "  Frontend: http://localhost:3000"
echo "  Backend:  http://localhost:8000"
echo "=================================================="
echo "  (Press Ctrl+C to stop both servers)"
echo ""

wait