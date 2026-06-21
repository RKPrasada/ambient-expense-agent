# Makefile for ambient-expense-agent

.PHONY: install playground test lint generate-traces grade

install:
	agents-cli install

playground:
	.venv/bin/python -u expense_agent/fast_api_app.py

generate-traces:
	uv run python -u tests/eval/generate_traces.py

grade:
	uv run python -u tests/eval/run_grade.py
