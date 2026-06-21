import asyncio
import json
import os
from google.adk.runners import InMemoryRunner
from google.adk.apps import App
from google.genai import types
from expense_agent.agent import root_agent

async def main():
    # Load env variables for Vertex AI access
    from dotenv import load_dotenv
    load_dotenv(dotenv_path="/Users/aruna/ambient-expense-agent/.env")
    
    dataset_path = "tests/eval/datasets/basic-dataset.json"
    output_path = "artifacts/traces/generated_traces.json"
    
    # Ensure directories exist
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    
    # Load dataset cases
    with open(dataset_path, "r") as f:
        dataset = json.load(f)
        
    app = App(name="expense_agent", root_agent=root_agent)
    runner = InMemoryRunner(app=app)
    
    eval_cases = []
    
    for case in dataset.get("eval_cases", []):
        case_id = case["eval_case_id"]
        print(f"Running eval case: {case_id}")
        
        # Create a fresh session
        session = await runner.session_service.create_session(
            app_name="expense_agent", user_id="eval_user"
        )
        
        prompt_text = case["prompt"]["parts"][0]["text"]
        prompt_data = json.loads(prompt_text)
        
        new_message = types.Content(
            role="user",
            parts=[types.Part.from_text(text=json.dumps(prompt_data))]
        )
        
        events = []
        async for ev in runner.run_async(
            user_id="eval_user",
            session_id=session.id,
            new_message=new_message
        ):
            events.append(ev)
            
        # Check if the workflow is paused at the human gate
        is_paused = False
        invocation_id = None
        for ev in events:
            if ev.long_running_tool_ids and "decision" in ev.long_running_tool_ids:
                is_paused = True
                invocation_id = ev.invocation_id
                break
                
        if is_paused:
            print(f"  Case {case_id} paused at human gate. Simulating response...")
            # Automate decision: reject prompt injection attempts, approve clean ones
            decision = "reject" if "injection" in case_id else "approve"
            
            resume_message = types.Content(
                role="user",
                parts=[
                    types.Part(
                        function_response=types.FunctionResponse(
                            name="decision",
                            id="decision",
                            response={"result": decision}
                        )
                    )
                ]
            )
            
            async for ev in runner.run_async(
                user_id="eval_user",
                session_id=session.id,
                invocation_id=invocation_id,
                new_message=resume_message
            ):
                events.append(ev)
        else:
            print(f"  Case {case_id} completed auto-approval flow.")
            
        # Retrieve the updated session history
        session_state = await runner.session_service.get_session(
            app_name="expense_agent", user_id="eval_user", session_id=session.id
        )
        
        # Convert session events to turn-based trace format for platform grading
        turns = []
        current_turn = None
        for ev in session_state.events:
            # A new turn starts when the user provides input
            if ev.author == "user" or current_turn is None:
                if current_turn is not None:
                    turns.append(current_turn)
                current_turn = {
                    "turn_index": len(turns),
                    "events": []
                }
            
            event_dict = {
                "author": ev.author,
            }
            if ev.content:
                parts_list = []
                for p in ev.content.parts:
                    part_dict = {}
                    if p.text:
                        part_dict["text"] = p.text
                    elif p.function_call:
                        part_dict["function_call"] = {
                            "name": p.function_call.name,
                            "args": p.function_call.args
                        }
                    elif p.function_response:
                        part_dict["function_response"] = {
                            "name": p.function_response.name,
                            "response": p.function_response.response
                        }
                    parts_list.append(part_dict)
                    
                event_dict["content"] = {
                    "role": ev.content.role,
                    "parts": parts_list
                }
            current_turn["events"].append(event_dict)
            
        if current_turn:
            turns.append(current_turn)
            
        # Find the final text response from the model
        final_text = ""
        found = False
        for turn in reversed(turns):
            for event in reversed(turn.get("events", [])):
                if event.get("content") and event["content"].get("role") == "model":
                    for p in event["content"].get("parts", []):
                        if isinstance(p, dict) and p.get("text"):
                            final_text = p["text"]
                            found = True
                            break
                if found:
                    break
            if found:
                break

        eval_cases.append({
            "eval_case_id": case_id,
            "prompt": case["prompt"],
            "responses": [
                {
                    "response": {
                        "role": "model",
                        "parts": [{"text": final_text}]
                    }
                }
            ],
            "agent_data": {
                "agents": {
                    "expense_approval_workflow": {
                        "agent_id": "expense_approval_workflow",
                        "instruction": "Orchestrates ambient expense approval process."
                    },
                    "risk_reviewer": {
                        "agent_id": "risk_reviewer",
                        "instruction": "Analyze the following expense details for risk factors."
                    }
                },
                "turns": turns
            }
        })
        
    output_dataset = {
        "eval_cases": eval_cases
    }
    with open(output_path, "w") as f:
        json.dump(output_dataset, f, indent=2)
    print(f"Traces written successfully to {output_path}")

if __name__ == "__main__":
    asyncio.run(main())
