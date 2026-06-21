import subprocess
import os
import sys

def main():
    # Load env variables for Vertex AI access
    from dotenv import load_dotenv
    load_dotenv(dotenv_path="/Users/aruna/ambient-expense-agent/.env")
    
    cmd = [
        "agents-cli", "eval", "grade",
        "--traces", "artifacts/traces/generated_traces.json",
        "--config", "tests/eval/eval_config.yaml"
    ]
    print(f"Executing: {' '.join(cmd)}")
    
    # Flush output before subprocess runs
    sys.stdout.flush()
    
    # Execute the evaluation grading
    result = subprocess.run(cmd)
    sys.exit(result.returncode)

if __name__ == "__main__":
    main()
