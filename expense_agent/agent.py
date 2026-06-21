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

import base64
import json
import os
from typing import Any, Dict, Optional

import google.auth
from google.adk.agents import LlmAgent
from google.adk.agents.context import Context
from google.adk.apps import App
from google.adk.events.event import Event
from google.adk.events.request_input import RequestInput
from google.adk.models import Gemini
from google.adk.workflow import Workflow, node
from google.genai import types
from pydantic import BaseModel, Field

# Import configurations
from expense_agent.config import MODEL_NAME, THRESHOLD

# Initialize GCP environment variables for local/Vertex execution
try:
    _, project_id = google.auth.default()
    if project_id:
        os.environ.setdefault("GOOGLE_CLOUD_PROJECT", project_id)
except Exception:
    pass

os.environ.setdefault("GOOGLE_CLOUD_LOCATION", "us-central1")
os.environ.setdefault("GOOGLE_GENAI_USE_VERTEXAI", "True")

# Resolve model name with fallback for Vertex AI if gemini-3.1-flash-lite is not available
actual_model = MODEL_NAME
if os.environ.get("GOOGLE_GENAI_USE_VERTEXAI", "True").strip().lower() == "true":
    if MODEL_NAME == "gemini-3.1-flash-lite":
        actual_model = "gemini-2.5-flash"



# --- Schemas ---

class Expense(BaseModel):
    amount: float
    submitter: str
    category: str
    description: str
    date: str


class RiskAnalysis(BaseModel):
    risk_score: int = Field(description="Risk score from 1 to 10")
    risk_factors: list[str] = Field(description="List of identified risk factors or anomalies")
    justification: str = Field(description="Brief justification for the risk score")


# --- Helper Parsing Function ---

def parse_expense_from_event(event_dict: Dict[str, Any]) -> Expense:
    """Extracts and parses expense details from a JSON event dictionary."""
    data_val = None
    if "data" in event_dict:
        data_val = event_dict["data"]
    elif (
        "message" in event_dict
        and isinstance(event_dict["message"], dict)
        and "data" in event_dict["message"]
    ):
        data_val = event_dict["message"]["data"]
    
    if data_val is None:
        data_val = event_dict

    expense_dict = {}
    if isinstance(data_val, dict):
        expense_dict = data_val
    elif isinstance(data_val, str):
        # Attempt base64 decoding (standard Pub/Sub payload)
        try:
            decoded = base64.b64decode(data_val).decode("utf-8")
            expense_dict = json.loads(decoded)
        except Exception:
            # If not base64, try parsing directly as a JSON string
            try:
                expense_dict = json.loads(data_val)
            except Exception:
                raise ValueError(f"Could not parse data string: {data_val}")
    else:
        raise ValueError(f"Unexpected data type: {type(data_val)}")

    return Expense(
        amount=float(expense_dict.get("amount", 0)),
        submitter=str(expense_dict.get("submitter", "Unknown")),
        category=str(expense_dict.get("category", "General")),
        description=str(expense_dict.get("description", "No description")),
        date=str(expense_dict.get("date", ""))
    )


# --- Workflow Graph Nodes ---

def parse_input(ctx: Context, node_input: Any) -> Event:
    """Decodes and extracts the expense details, routing based on amount."""
    raw_dict = {}
    if hasattr(node_input, "parts"):
        # types.Content
        text = node_input.parts[0].text
        try:
            raw_dict = json.loads(text)
        except Exception:
            pass
    elif isinstance(node_input, str):
        try:
            raw_dict = json.loads(node_input)
        except Exception:
            pass
    elif isinstance(node_input, dict):
        raw_dict = node_input
    elif hasattr(node_input, "model_dump"):
        raw_dict = node_input.model_dump()

    try:
        expense = parse_expense_from_event(raw_dict)
    except Exception as e:
        return Event(
            output={"error": f"Failed to parse expense: {str(e)}"},
            route="error",
            state={"error": str(e)}
        )

    # Redact PII (e.g. SSNs) before model sees it
    import re
    ssn_pattern = re.compile(r'\b\d{3}[- ]?\d{2}[- ]?\d{4}\b')
    expense.description = ssn_pattern.sub("[REDACTED SSN]", expense.description)

    # Detect prompt injection attempts
    injection_keywords = ["bypass all rules", "ignore instructions", "auto-approve", "ignore previous"]
    desc_lower = expense.description.lower()
    is_injection = any(kw in desc_lower for kw in injection_keywords)

    state_update = {
        "expense": expense.model_dump(),
        "amount": expense.amount,
        "submitter": expense.submitter,
        "category": expense.category,
        "description": expense.description,
        "date": expense.date,
    }

    if is_injection:
        # Escalate directly to human review, bypassing the model
        risk_analysis = {
            "risk_score": 10,
            "risk_factors": ["Prompt injection attempt detected"],
            "justification": "Adversarial or rule-bypass text was detected in the expense description."
        }
        state_update["risk_analysis"] = risk_analysis
        return Event(
            output=risk_analysis,
            route="human_review",
            state=state_update
        )

    if expense.amount < THRESHOLD:
        return Event(
            output=expense.model_dump(),
            route="auto_approve",
            state=state_update
        )
    else:
        return Event(
            output=expense.model_dump(),
            route="risk_review",
            state=state_update
        )


def auto_approve(ctx: Context, node_input: Any):
    """Instant auto-approval for expenses below the threshold."""
    expense = ctx.state.get("expense")
    result = {
        "status": "approved",
        "amount": expense.get("amount"),
        "submitter": expense.get("submitter"),
        "category": expense.get("category"),
        "description": expense.get("description"),
        "date": expense.get("date"),
        "method": "auto-approved",
        "risk_analysis": {
            "risk_score": 1,
            "risk_factors": ["Expense is under threshold"],
            "justification": f"Expense amount ${expense.get('amount')} is below the review threshold of ${THRESHOLD}."
        }
    }
    message_text = (
        f"✅ EXPENSE AUTO-APPROVED (Instant Approval)\n"
        f"Submitter: {result['submitter']}\n"
        f"Amount: ${result['amount']:.2f}\n"
        f"Category: {result['category']}\n"
        f"Description: {result['description']}\n"
        f"Method: Auto-approved"
    )
    yield Event(content=types.Content(role='model', parts=[types.Part.from_text(text=message_text)]))
    yield Event(output=result)


# LLM Risk Reviewer Agent Node
risk_reviewer = LlmAgent(
    name="risk_reviewer",
    model=Gemini(
        model=actual_model,
        retry_options=types.HttpRetryOptions(attempts=3),
    ),
    instruction=(
        "You are an expense auditor. Analyze the following expense details for risk factors "
        "(e.g., unusual amounts for the category, formatting anomalies, policy abuse):\n"
        "Submitter: {submitter}\n"
        "Amount: ${amount}\n"
        "Category: {category}\n"
        "Description: {description}\n"
        "Date: {date}\n\n"
        "Provide a risk score from 1 to 10, list any specific risk factors, and justify your rating."
    ),
    output_schema=RiskAnalysis,
    output_key="risk_analysis",
)


@node(rerun_on_resume=True)
async def review_agent(ctx: Context, node_input: RiskAnalysis):
    """Pauses the workflow for human approval if human decision is not yet provided."""
    amount = ctx.state.get("amount")
    submitter = ctx.state.get("submitter")
    
    if not ctx.resume_inputs or "decision" not in ctx.resume_inputs:
        alert_message = (
            f"⚠️ ALERT: High-value expense requires human approval!\n"
            f"Submitter: {submitter}\n"
            f"Amount: ${amount:.2f}\n"
            f"Risk Score: {node_input.risk_score}/10\n"
            f"Risk Factors: {', '.join(node_input.risk_factors)}\n"
            f"Justification: {node_input.justification}"
        )
        yield Event(content=types.Content(role='model', parts=[types.Part.from_text(text=alert_message)]))
        yield RequestInput(
            interrupt_id="decision",
            message=alert_message + "\n\nDo you approve or reject this expense? (approve/reject)"
        )
        return

    decision_val = ctx.resume_inputs["decision"]
    if isinstance(decision_val, dict):
        decision = decision_val.get("result", decision_val.get("output", decision_val.get("decision", str(decision_val))))
    else:
        decision = str(decision_val)
    yield Event(
        output={
            "decision": decision,
            "risk_analysis": node_input.model_dump()
        }
    )


def record_outcome(ctx: Context, node_input: dict):
    """Finalizes and records the expense approval/rejection outcome."""
    expense = ctx.state.get("expense")
    decision = node_input.get("decision", "rejected")
    risk_analysis = node_input.get("risk_analysis")

    decision_clean = decision.strip().lower()
    if "approve" in decision_clean:
        status = "approved"
        emoji = "✅"
    elif "reject" in decision_clean:
        status = "rejected"
        emoji = "❌"
    else:
        status = f"unknown ({decision})"
        emoji = "❓"

    result = {
        "status": status,
        "amount": expense.get("amount"),
        "submitter": expense.get("submitter"),
        "category": expense.get("category"),
        "description": expense.get("description"),
        "date": expense.get("date"),
        "method": "human-reviewed",
        "risk_analysis": risk_analysis
    }
    message_text = (
        f"{emoji} EXPENSE {status.upper()} (Human Reviewed)\n"
        f"Submitter: {result['submitter']}\n"
        f"Amount: ${result['amount']:.2f}\n"
        f"Category: {result['category']}\n"
        f"Description: {result['description']}\n"
        f"Risk Score: {risk_analysis.get('risk_score', 'N/A')}/10\n"
        f"Method: Human-reviewed"
    )
    yield Event(content=types.Content(role='model', parts=[types.Part.from_text(text=message_text)]))
    yield Event(output=result)


def error_handler(ctx: Context, node_input: Any):
    """Graceful handler for parsing and validation errors."""
    error_msg = ctx.state.get("error", "Unknown error")
    result = {
        "status": "error",
        "message": error_msg
    }
    message_text = f"❌ ERROR: {error_msg}"
    yield Event(content=types.Content(role='model', parts=[types.Part.from_text(text=message_text)]))
    yield Event(output=result)


# --- Workflow Topology ---

root_agent = Workflow(
    name="expense_approval_workflow",
    edges=[
        ("START", parse_input),
        (parse_input, {
            "auto_approve": auto_approve,
            "risk_review": risk_reviewer,
            "human_review": review_agent,
            "error": error_handler,
        }),
        (risk_reviewer, review_agent),
        (review_agent, record_outcome),
    ]
)

app = App(
    root_agent=root_agent,
    name="expense_agent",
)
