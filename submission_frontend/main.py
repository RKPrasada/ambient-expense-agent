import os
import re
import asyncio
import json
import logging
from typing import Any, Dict, Optional, List
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
import vertexai
from google.adk.sessions.vertex_ai_session_service import VertexAiSessionService
from vertexai.preview.reasoning_engines import ReasoningEngine

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("submission_frontend")

# Read configurations from environment variables
project_id = os.environ.get("GOOGLE_CLOUD_PROJECT", "project-3e8149d8-09a5-4f89-ac3")
agent_runtime_id = os.environ.get("AGENT_RUNTIME_ID")

# If AGENT_RUNTIME_ID is not set in environment, attempt to discover from deployment_metadata.json
if not agent_runtime_id:
    try:
        metadata_path = "/Users/aruna/ambient-expense-agent/deployment_metadata.json"
        if os.path.exists(metadata_path):
            with open(metadata_path, "r") as f:
                meta = json.load(f)
                agent_runtime_id = meta.get("remote_agent_runtime_id")
    except Exception as e:
        logger.warning(f"Failed to load deployment_metadata.json: {e}")

if not agent_runtime_id:
    # Fallback to the known deployed runtime ID if not configured
    agent_runtime_id = "projects/70101449967/locations/us-east1/reasoningEngines/1772060900254023680"

# Parse location from the agent runtime ID
location = "us-east1"
m = re.search(r"locations/([^/]+)", agent_runtime_id)
if m:
    location = m.group(1)

# Extract short engine ID
engine_id = agent_runtime_id.split('/')[-1]

logger.info(f"Initializing Frontend Service with Project: {project_id}, Location: {location}, Engine ID: {engine_id}")

# Initialize Vertex AI
vertexai.init(project=project_id, location=location)

# Initialize Session Service
session_service = VertexAiSessionService(
    project=project_id,
    location=location,
    agent_engine_id=engine_id
)

# Initialize Reasoning Engine client
re_engine = ReasoningEngine(agent_runtime_id)

app = FastAPI(title="Ambient Expense Manager Dashboard")


class ActionRequest(BaseModel):
    interrupt_id: str
    approved: bool


def find_pending_approvals(events: List[Any]) -> Dict[str, Any]:
    """Scans event list to find unresolved adk_request_input tool calls."""
    pending = {}
    for event in events:
        content = getattr(event, "content", None)
        if not content or not getattr(content, "parts", None):
            continue
        for part in content.parts:
            # Check for function call requesting input
            fc = getattr(part, "function_call", None)
            if fc and getattr(fc, "name", None) == "adk_request_input":
                call_id = getattr(fc, "id", None) or "decision"
                pending[call_id] = fc
            # Check for function response resolving it
            fr = getattr(part, "function_response", None)
            if fr and getattr(fr, "name", None) == "adk_request_input":
                response_id = getattr(fr, "id", None) or "decision"
                if response_id in pending:
                    del pending[response_id]
    return pending


@app.get("/api/pending")
async def get_pending():
    """Queries the SessionService and returns all active sessions with unresolved approval requests."""
    try:
        list_response = await session_service.list_sessions(app_name="expense_agent")
        sessions = list_response.sessions
        pending_approvals = []

        for s in sessions:
            full_session = await session_service.get_session(
                app_name="expense_agent",
                user_id=s.user_id,
                session_id=s.id
            )
            if not full_session:
                continue

            unresolved = find_pending_approvals(full_session.events)
            if unresolved:
                for interrupt_id, func_call in unresolved.items():
                    # Parse args to get original message
                    message = ""
                    if func_call.args:
                        message = func_call.args.get("message", "")

                    pending_approvals.append({
                        "session_id": full_session.id,
                        "user_id": full_session.user_id,
                        "interrupt_id": interrupt_id,
                        "message": message,
                        "expense": full_session.state.get("expense", {}),
                        "risk_analysis": full_session.state.get("risk_analysis", {}),
                        "last_update_time": full_session.last_update_time
                    })

        return pending_approvals
    except Exception as e:
        logger.error(f"Error fetching pending approvals: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/action/{session_id}")
async def take_action(session_id: str, req: ActionRequest):
    """Resumes the paused Agent Runtime session with approval/rejection decision."""
    try:
        from google.cloud.aiplatform_v1beta1.types import StreamQueryReasoningEngineRequest
        from vertexai.reasoning_engines import _utils

        # Construct resume payload. We provide both result and approved to satisfy
        # the substring check in the expense agent logic and the requested spec.
        resume_message = {
            "role": "user",
            "parts": [
                {
                    "function_response": {
                        "name": "adk_request_input",
                        "id": req.interrupt_id,
                        "response": {
                            "approved": req.approved,
                            "result": "approve" if req.approved else "reject"
                        }
                    }
                }
            ]
        }

        input_payload = {
            "message": resume_message,
            "user_id": "default-user",  # strictly default-user
            "session_id": session_id
        }

        # Run block in threadpool to prevent blocking the async event loop
        loop = asyncio.get_event_loop()
        def _run_query():
            response = re_engine.execution_api_client.stream_query_reasoning_engine(
                request=StreamQueryReasoningEngineRequest(
                    name=re_engine.resource_name,
                    input=input_payload,
                    class_method="stream_query",
                ),
            )
            events = []
            for chunk in response:
                for parsed_json in _utils.yield_parsed_json(chunk):
                    if parsed_json is not None:
                        events.append(parsed_json)
            return events

        events = await loop.run_in_executor(None, _run_query)

        # Parse outcome
        final_message = "No response from agent."
        status = "unknown"
        for event in events:
            content = event.get("content")
            if content and content.get("parts"):
                for part in content["parts"]:
                    if part.get("text"):
                        final_message = part["text"]
            output = event.get("output")
            if output and isinstance(output, dict) and "status" in output:
                status = output["status"]

        return {
            "status": status,
            "message": final_message,
            "events": events
        }
    except Exception as e:
        logger.error(f"Error taking action on session {session_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/", response_class=HTMLResponse)
async def serve_dashboard():
    """Serves the dashboard HTML page."""
    html_content = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Ambient Expense Manager Dashboard</title>
    <!-- Outfit Google Font -->
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
    <link href="https://fonts.googleapis.com/css2?family=Outfit:wght@300;400;500;600;700;800&family=Inter:wght@300;400;500;600;700&display=swap" rel="stylesheet">
    <style>
        * {
            box-sizing: border-box;
            margin: 0;
            padding: 0;
        }
        body {
            background-color: #0A0D16;
            background-image: 
                radial-gradient(circle at 10% 20%, rgba(49, 46, 129, 0.2) 0%, transparent 40%),
                radial-gradient(circle at 90% 80%, rgba(99, 102, 241, 0.12) 0%, transparent 45%);
            color: #F3F4F6;
            font-family: 'Outfit', 'Inter', sans-serif;
            min-height: 100vh;
            overflow-x: hidden;
        }
        header {
            padding: 30px 5%;
            display: flex;
            justify-content: space-between;
            align-items: center;
            border-bottom: 1px solid rgba(255, 255, 255, 0.05);
            background: rgba(10, 13, 22, 0.6);
            backdrop-filter: blur(10px);
            position: sticky;
            top: 0;
            z-index: 100;
        }
        .logo-section h1 {
            font-size: 1.5rem;
            font-weight: 700;
            letter-spacing: -0.02em;
            background: linear-gradient(to right, #FFFFFF, #94A3B8);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
        }
        .logo-section p {
            font-size: 0.85rem;
            color: #64748B;
            margin-top: 2px;
        }
        .status-badge {
            background: rgba(16, 185, 129, 0.1);
            color: #34D399;
            border: 1px solid rgba(16, 185, 129, 0.2);
            padding: 6px 16px;
            border-radius: 99px;
            font-size: 0.85rem;
            font-weight: 500;
            display: flex;
            align-items: center;
            gap: 8px;
        }
        .status-dot {
            width: 8px;
            height: 8px;
            background: #10B981;
            border-radius: 50%;
            box-shadow: 0 0 10px #10B981;
            animation: pulse 2s infinite;
        }
        @keyframes pulse {
            0% { transform: scale(0.9); opacity: 0.6; }
            50% { transform: scale(1.15); opacity: 1; }
            100% { transform: scale(0.9); opacity: 0.6; }
        }
        main {
            padding: 40px 5%;
            max-width: 1400px;
            margin: 0 auto;
        }
        .header-actions {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 30px;
        }
        .refresh-btn {
            background: rgba(255, 255, 255, 0.05);
            border: 1px solid rgba(255, 255, 255, 0.1);
            color: #E2E8F0;
            padding: 10px 20px;
            border-radius: 12px;
            font-weight: 500;
            cursor: pointer;
            transition: all 0.2s ease;
            display: flex;
            align-items: center;
            gap: 8px;
        }
        .refresh-btn:hover {
            background: rgba(255, 255, 255, 0.1);
            border-color: rgba(255, 255, 255, 0.2);
        }
        .grid {
            display: grid;
            grid-template-columns: repeat(auto-fill, minmax(380px, 1fr));
            gap: 28px;
        }
        .card {
            background: rgba(255, 255, 255, 0.02);
            backdrop-filter: blur(16px);
            -webkit-backdrop-filter: blur(16px);
            border: 1px solid rgba(255, 255, 255, 0.06);
            border-radius: 24px;
            padding: 28px;
            transition: all 0.3s cubic-bezier(0.4, 0, 0.2, 1);
            position: relative;
            display: flex;
            flex-direction: column;
            justify-content: space-between;
            min-height: 380px;
        }
        .card:hover {
            transform: translateY(-5px);
            border-color: rgba(99, 102, 241, 0.25);
            box-shadow: 0 15px 35px rgba(0, 0, 0, 0.35);
        }
        .card-header {
            display: flex;
            justify-content: space-between;
            align-items: flex-start;
            margin-bottom: 20px;
        }
        .card-title h3 {
            font-size: 1.15rem;
            font-weight: 600;
            color: #F8FAFC;
        }
        .card-title p {
            font-size: 0.85rem;
            color: #64748B;
            margin-top: 3px;
        }
        .amount-badge {
            font-size: 1.4rem;
            font-weight: 700;
            color: #F8FAFC;
            background: rgba(255, 255, 255, 0.05);
            padding: 6px 14px;
            border-radius: 12px;
            border: 1px solid rgba(255, 255, 255, 0.05);
        }
        .card-body {
            flex-grow: 1;
            margin-bottom: 24px;
        }
        .card-row {
            display: flex;
            justify-content: space-between;
            margin-bottom: 12px;
            font-size: 0.9rem;
        }
        .row-label {
            color: #64748B;
        }
        .row-value {
            color: #E2E8F0;
            font-weight: 500;
        }
        .risk-section {
            background: rgba(255, 255, 255, 0.015);
            border: 1px solid rgba(255, 255, 255, 0.04);
            border-radius: 16px;
            padding: 16px;
            margin-top: 16px;
        }
        .risk-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 10px;
        }
        .risk-badge {
            padding: 4px 10px;
            border-radius: 99px;
            font-size: 0.75rem;
            font-weight: 600;
            text-transform: uppercase;
            letter-spacing: 0.05em;
        }
        .risk-badge.high {
            background: rgba(244, 63, 94, 0.12);
            color: #FB7185;
            border: 1px solid rgba(244, 63, 94, 0.2);
        }
        .risk-badge.medium {
            background: rgba(245, 158, 11, 0.12);
            color: #FBBF24;
            border: 1px solid rgba(245, 158, 11, 0.2);
        }
        .risk-badge.low {
            background: rgba(16, 185, 129, 0.12);
            color: #34D399;
            border: 1px solid rgba(16, 185, 129, 0.2);
        }
        .risk-justification {
            font-size: 0.8rem;
            color: #94A3B8;
            line-height: 1.4;
            margin-top: 8px;
        }
        .card-actions {
            display: flex;
            gap: 16px;
            margin-top: auto;
        }
        .btn {
            flex: 1;
            padding: 12px;
            border-radius: 12px;
            font-weight: 600;
            font-size: 0.95rem;
            cursor: pointer;
            transition: all 0.2s ease;
            display: flex;
            justify-content: center;
            align-items: center;
            gap: 8px;
        }
        .btn-approve {
            background: #10B981;
            color: #FFFFFF;
            border: none;
            box-shadow: 0 4px 12px rgba(16, 185, 129, 0.25);
        }
        .btn-approve:hover {
            background: #059669;
            box-shadow: 0 6px 18px rgba(16, 185, 129, 0.4);
        }
        .btn-reject {
            background: rgba(244, 63, 94, 0.1);
            color: #FB7185;
            border: 1px solid rgba(244, 63, 94, 0.25);
        }
        .btn-reject:hover {
            background: rgba(244, 63, 94, 0.2);
            border-color: rgba(244, 63, 94, 0.4);
        }
        .btn:disabled {
            opacity: 0.5;
            cursor: not-allowed;
        }
        /* Slide-out drawer */
        .drawer-overlay {
            position: fixed;
            top: 0;
            left: 0;
            width: 100%;
            height: 100%;
            background: rgba(0, 0, 0, 0.6);
            backdrop-filter: blur(4px);
            opacity: 0;
            visibility: hidden;
            transition: all 0.3s ease;
            z-index: 999;
        }
        .drawer-overlay.open {
            opacity: 1;
            visibility: visible;
        }
        .drawer {
            position: fixed;
            top: 0;
            right: -460px;
            width: 440px;
            height: 100%;
            background: rgba(15, 23, 42, 0.96);
            backdrop-filter: blur(24px);
            border-left: 1px solid rgba(255, 255, 255, 0.08);
            box-shadow: -15px 0 35px rgba(0, 0, 0, 0.5);
            transition: right 0.4s cubic-bezier(0.4, 0, 0.2, 1);
            z-index: 1000;
            padding: 40px 30px;
            display: flex;
            flex-direction: column;
        }
        .drawer.open {
            right: 0;
        }
        .drawer-close {
            position: absolute;
            top: 25px;
            right: 25px;
            background: none;
            border: none;
            color: #94A3B8;
            font-size: 1.5rem;
            cursor: pointer;
        }
        .drawer-content {
            margin-top: 30px;
            flex-grow: 1;
            overflow-y: auto;
        }
        .drawer-title {
            font-size: 1.4rem;
            font-weight: 700;
            color: #F8FAFC;
            margin-bottom: 12px;
        }
        .review-status-card {
            border-radius: 16px;
            padding: 20px;
            margin-bottom: 24px;
            border: 1px solid rgba(255, 255, 255, 0.06);
        }
        .review-status-card.approved {
            background: rgba(16, 185, 129, 0.08);
            border-color: rgba(16, 185, 129, 0.2);
            color: #34D399;
        }
        .review-status-card.rejected {
            background: rgba(244, 63, 94, 0.08);
            border-color: rgba(244, 63, 94, 0.2);
            color: #FB7185;
        }
        .review-markdown {
            background: rgba(255, 255, 255, 0.02);
            border: 1px solid rgba(255, 255, 255, 0.05);
            border-radius: 16px;
            padding: 20px;
            font-size: 0.95rem;
            line-height: 1.6;
            color: #E2E8F0;
            white-space: pre-wrap;
        }
        .no-data {
            text-align: center;
            padding: 100px 20px;
            color: #64748B;
            grid-column: 1 / -1;
        }
        .spinner {
            border: 2px solid rgba(255, 255, 255, 0.1);
            border-top: 2px solid currentColor;
            border-radius: 50%;
            width: 18px;
            height: 18px;
            animation: spin 0.8s linear infinite;
            display: inline-block;
        }
        @keyframes spin {
            0% { transform: rotate(0deg); }
            100% { transform: rotate(360deg); }
        }
    </style>
</head>
<body>
    <header>
        <div class="logo-section">
            <h1>Ambient Expense Dashboard</h1>
            <p>Interactive compliance processing & HITL review</p>
        </div>
        <div class="status-badge">
            <div class="status-dot"></div>
            Connected to Agent Runtime
        </div>
    </header>

    <main>
        <div class="header-actions">
            <h2>Pending Reviews</h2>
            <button class="refresh-btn" onclick="fetchPending()">
                Refresh
            </button>
        </div>

        <div id="pending-grid" class="grid">
            <!-- Cards will be populated here -->
            <div class="no-data">Loading pending reviews...</div>
        </div>
    </main>

    <!-- Side Drawer -->
    <div id="drawer-overlay" class="drawer-overlay" onclick="closeDrawer()"></div>
    <div id="drawer" class="drawer">
        <button class="drawer-close" onclick="closeDrawer()">&times;</button>
        <div class="drawer-content">
            <h2 class="drawer-title">Compliance Review</h2>
            <div id="review-status" class="review-status-card">
                Status
            </div>
            <div id="review-text" class="review-markdown">
                Review details...
            </div>
        </div>
    </div>

    <script>
        async function fetchPending() {
            const grid = document.getElementById('pending-grid');
            try {
                const response = await fetch('/api/pending');
                const data = await response.json();
                
                if (data.length === 0) {
                    grid.innerHTML = '<div class="no-data">No pending reviews found. Everything is processed!</div>';
                    return;
                }
                
                grid.innerHTML = data.map(item => {
                    const expense = item.expense || {};
                    const amount = expense.amount || 0;
                    const submitter = expense.submitter || "Unknown";
                    const category = expense.category || "General";
                    const date = expense.date || "N/A";
                    const description = expense.description || item.message || "";
                    
                    const risk = item.risk_analysis || {};
                    const riskScore = risk.risk_score || 1;
                    const riskClass = riskScore >= 7 ? 'high' : riskScore >= 4 ? 'medium' : 'low';
                    const riskLabel = riskScore >= 7 ? 'High Risk' : riskScore >= 4 ? 'Medium Risk' : 'Low Risk';
                    const justification = risk.justification || "No risk factors identified.";

                    return `
                    <div class="card" id="card-${item.session_id}">
                        <div>
                            <div class="card-header">
                                <div class="card-title">
                                    <h3>${submitter}</h3>
                                    <p>${category}</p>
                                </div>
                                <div class="amount-badge">$${amount.toFixed(2)}</div>
                            </div>
                            <div class="card-body">
                                <div class="card-row">
                                    <span class="row-label">Date</span>
                                    <span class="row-value">${date}</span>
                                </div>
                                <div class="card-row">
                                    <span class="row-label">Description</span>
                                    <span class="row-value">${description}</span>
                                </div>
                                <div class="risk-section">
                                    <div class="risk-header">
                                        <span class="row-label">Risk Rating</span>
                                        <span class="risk-badge ${riskClass}">${riskLabel} (${riskScore}/10)</span>
                                    </div>
                                    <p class="risk-justification">${justification}</p>
                                </div>
                            </div>
                        </div>
                        <div class="card-actions">
                            <button class="btn btn-reject" onclick="takeAction('${item.session_id}', '${item.interrupt_id}', false, this)">
                                Reject
                            </button>
                            <button class="btn btn-approve" onclick="takeAction('${item.session_id}', '${item.interrupt_id}', true, this)">
                                Approve
                            </button>
                        </div>
                    </div>
                    `;
                }).join('');
            } catch (err) {
                grid.innerHTML = '<div class="no-data">Failed to load reviews. Please try again.</div>';
                console.error(err);
            }
        }

        async function takeAction(sessionId, interruptId, approved, button) {
            // Disable actions
            const card = document.getElementById(`card-${sessionId}`);
            const buttons = card.querySelectorAll('.btn');
            buttons.forEach(btn => btn.disabled = true);
            
            const originalText = button.innerText;
            button.innerHTML = '<div class="spinner"></div>';
            
            try {
                const response = await fetch(`/api/action/${sessionId}`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ interrupt_id: interruptId, approved: approved })
                });
                
                const result = await response.json();
                
                // Show final result drawer
                showDrawer(result.status, result.message);
                
                // Refresh list
                fetchPending();
            } catch (err) {
                alert("Action failed: " + err.message);
                buttons.forEach(btn => btn.disabled = false);
                button.innerText = originalText;
            }
        }

        function showDrawer(status, message) {
            const drawer = document.getElementById('drawer');
            const overlay = document.getElementById('drawer-overlay');
            const statusCard = document.getElementById('review-status');
            const textCard = document.getElementById('review-text');
            
            statusCard.className = `review-status-card ${status === 'approved' ? 'approved' : 'rejected'}`;
            statusCard.innerText = `Agent Outcome: ${status.toUpperCase()}`;
            textCard.innerText = message;
            
            drawer.classList.add('open');
            overlay.classList.add('open');
        }

        function closeDrawer() {
            document.getElementById('drawer').classList.remove('open');
            document.getElementById('drawer-overlay').classList.remove('open');
        }

        // Initial load
        fetchPending();
    </script>
</body>
</html>
"""
    return html_content


if __name__ == "__main__":
    import uvicorn
    # Dashboard serves on port 8000
    uvicorn.run(app, host="0.0.0.0", port=8000)
