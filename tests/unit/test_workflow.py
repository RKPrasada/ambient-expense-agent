import json
import pytest
from expense_agent.agent import root_agent, parse_expense_from_event
from google.adk.apps import App
from google.adk.runners import InMemoryRunner
from google.genai import types


def test_parse_expense_plain():
    event = {
        "data": {
            "amount": 45.50,
            "submitter": "Alice",
            "category": "Meals",
            "description": "Team lunch",
            "date": "2026-06-19"
        }
    }
    expense = parse_expense_from_event(event)
    assert expense.amount == 45.50
    assert expense.submitter == "Alice"
    assert expense.category == "Meals"


def test_parse_expense_base64():
    # {"amount": 120.0, "submitter": "Bob", "category": "Travel", "description": "Flight", "date": "2026-06-19"}
    payload = "eyJhbW91bnQiOiAxMjAuMCwgInN1Ym1pdHRlciI6ICJCb2IiLCAiY2F0ZWdvcnkiOiAiVHJhdmVsIiwgImRlc2NyaXB0aW9uIjogIkZsaWdodCIsICJkYXRlIjogIjIwMjYtMDYtMTkifQ=="
    event = {
        "data": payload
    }
    expense = parse_expense_from_event(event)
    assert expense.amount == 120.0
    assert expense.submitter == "Bob"
    assert expense.description == "Flight"


@pytest.mark.asyncio
async def test_workflow_auto_approve():
    app = App(name="expense_agent", root_agent=root_agent)
    runner = InMemoryRunner(app=app)
    
    session = await runner.session_service.create_session(
        app_name="expense_agent", user_id="test_user"
    )
    
    event_data = {
        "data": {
            "amount": 50.0,
            "submitter": "Alice",
            "category": "Meals",
            "description": "Lunch",
            "date": "2026-06-19"
        }
    }
    
    new_message = types.Content(
        role="user",
        parts=[types.Part.from_text(text=json.dumps(event_data))]
    )
    
    events = []
    async for ev in runner.run_async(
        user_id="test_user",
        session_id=session.id,
        new_message=new_message
    ):
        events.append(ev)
        
    output_events = [ev for ev in events if ev.output is not None]
    assert len(output_events) > 0
    final_output = output_events[-1].output
    assert final_output["status"] == "approved"
    assert final_output["method"] == "auto-approved"


@pytest.mark.asyncio
async def test_workflow_risk_review_and_hitl():
    from unittest.mock import patch
    app = App(name="expense_agent", root_agent=root_agent)
    runner = InMemoryRunner(app=app)
    
    session = await runner.session_service.create_session(
        app_name="expense_agent", user_id="test_user"
    )
    
    event_data = {
        "data": {
            "amount": 250.0,
            "submitter": "Bob",
            "category": "Equipment",
            "description": "Gold keyboard",
            "date": "2026-06-19"
        }
    }
    
    new_message = types.Content(
        role="user",
        parts=[types.Part.from_text(text=json.dumps(event_data))]
    )
    
    from google.adk.models.llm_response import LlmResponse
    
    async def mock_generate_content_async(self, llm_request, stream=False):
        yield LlmResponse(
            content=types.Content(
                role="model",
                parts=[types.Part.from_text(text=json.dumps({
                    "risk_score": 8,
                    "risk_factors": ["High amount", "Suspicious description"],
                    "justification": "Mocked LLM justification."
                }))]
            )
        )
        
    with patch('google.adk.models.google_llm.Gemini.generate_content_async', mock_generate_content_async):
        # 1. Run the first step
        events = []
        async for ev in runner.run_async(
            user_id="test_user",
            session_id=session.id,
            new_message=new_message
        ):
            events.append(ev)
            print(f"DEBUG EVENT: author={getattr(ev, 'author', None)} output={getattr(ev, 'output', None)} interrupt_ids={getattr(ev, 'interrupt_ids', None)}")
            
        # The workflow should pause at human_gate, yielding RequestInput
        interrupts = [ev for ev in events if ev.long_running_tool_ids]
        if not interrupts:
            for ev in events:
                print(f"EVENT DETAILS: {ev.model_dump()}")
        assert len(interrupts) > 0
        assert "decision" in interrupts[-1].long_running_tool_ids
        
        invocation_id = events[-1].invocation_id
        assert invocation_id is not None
        
        # 2. Resume session by sending the human's response
        resume_message = types.Content(
            role="user",
            parts=[
                types.Part(
                    function_response=types.FunctionResponse(
                        name="decision",
                        id="decision",
                        response={"result": "approve"}
                    )
                )
            ]
        )
        
        events_resume = []
        async for ev in runner.run_async(
            user_id="test_user",
            session_id=session.id,
            invocation_id=invocation_id,
            new_message=resume_message
        ):
            events_resume.append(ev)
            
        output_events = [ev for ev in events_resume if ev.output is not None]
        assert len(output_events) > 0
        final_output = output_events[-1].output
        assert final_output["status"] == "approved"
        assert final_output["method"] == "human-reviewed"
        assert final_output["risk_analysis"]["risk_score"] == 8

