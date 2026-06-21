# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
import os
import json
import logging
import sys
from typing import Any, Dict

import google.auth
from fastapi import FastAPI, Request
from google.adk.cli.fast_api import get_fast_api_app
from google.genai import types

from expense_agent.app_utils.telemetry import setup_telemetry
from expense_agent.app_utils.typing import Feedback

setup_telemetry()

# Setup standard Python logging for console logs as per Developer Checklist
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("ambient_expense_agent")

allow_origins = (
    os.getenv("ALLOW_ORIGINS", "").split(",") if os.getenv("ALLOW_ORIGINS") else None
)

# Artifact bucket for ADK (created by Terraform, passed via env var)
logs_bucket_name = os.environ.get("LOGS_BUCKET_NAME")

AGENT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# In-memory session configuration - no persistent storage
session_service_uri = None

artifact_service_uri = f"gs://{logs_bucket_name}" if logs_bucket_name else None

app: FastAPI = get_fast_api_app(
    agents_dir=AGENT_DIR,
    web=True,
    artifact_service_uri=artifact_service_uri,
    allow_origins=allow_origins,
    session_service_uri=session_service_uri,
    otel_to_cloud=False,  # Set to False as per Developer Checklist
)
app.title = "ambient-expense-agent"
app.description = "API for interacting with the Agent ambient-expense-agent"

# Dynamically extract ADK server instance to reuse same sessions/runners
adk_server = None
for route in app.routes:
    if route.path == "/run" and hasattr(route.endpoint, "__closure__") and route.endpoint.__closure__:
        for cell in route.endpoint.__closure__:
            val = cell.cell_contents
            if val.__class__.__name__ in ("ApiServer", "DevServer"):
                adk_server = val
                break

if not adk_server:
    logger.error("Failed to extract ADK ApiServer/DevServer instance from app routes.")


@app.post("/feedback")
def collect_feedback(feedback: Feedback) -> dict[str, str]:
    """Collect and log feedback."""
    logger.info(f"Feedback received: {feedback.model_dump()}")
    return {"status": "success"}


@app.post("/")
@app.post("/pubsub")
async def handle_pubsub_trigger(request: Request) -> dict[str, Any]:
    """Accepts Pub/Sub trigger messages, normalizes subscription paths, and feeds each message into the workflow."""
    if not adk_server:
        return {"status": "error", "message": "ADK server not initialized"}

    payload = await request.json()
    
    # Handle the subscription normalization gotcha
    subscription_path = payload.get("subscription", "")
    if subscription_path:
        # E.g. "projects/project-3e8149d8-09a5-4f89-ac3/subscriptions/expense-sub" -> "expense-sub"
        user_id = subscription_path.split("/")[-1]
    else:
        user_id = "local-trigger"

    app_name = "expense_agent"
    runner = await adk_server.get_runner_async(app_name)
    
    # Programmatically create a new session
    session = await adk_server.session_service.create_session(
        app_name=app_name, user_id=user_id
    )
    
    # Wrap payload as user message content for ADK workflow
    new_message = types.Content(
        role="user",
        parts=[types.Part.from_text(text=json.dumps(payload))]
    )
    
    events = []
    logger.info(f"Triggering workflow session {session.id} for user {user_id}")
    async for event in runner.run_async(
        user_id=user_id,
        session_id=session.id,
        new_message=new_message
    ):
        events.append(event)

    # Analyze the events to determine execution outcome
    is_paused = False
    interrupt_message = None
    final_output = None
    
    for ev in events:
        if ev.long_running_tool_ids and "decision" in ev.long_running_tool_ids:
            is_paused = True
            if ev.content and ev.content.parts:
                interrupt_message = ev.content.parts[0].text
            else:
                interrupt_message = "Waiting for human approval"
        if ev.output is not None:
            final_output = ev.output

    if is_paused:
        logger.info(f"Session {session.id} paused at human gate for approval.")
        return {
            "status": "paused_for_review",
            "session_id": session.id,
            "user_id": user_id,
            "message": interrupt_message
        }
    elif final_output:
        logger.info(f"Session {session.id} completed successfully.")
        return {
            "status": "completed",
            "session_id": session.id,
            "user_id": user_id,
            "output": final_output
        }
    else:
        logger.info(f"Session {session.id} processed with no explicit final output.")
        return {
            "status": "processed",
            "session_id": session.id,
            "user_id": user_id,
            "events_count": len(events)
        }


# Main execution
if __name__ == "__main__":
    import uvicorn

    # Serve on port 8080 as requested
    uvicorn.run(app, host="0.0.0.0", port=8080)
