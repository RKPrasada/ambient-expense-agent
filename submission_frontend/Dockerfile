FROM python:3.11-slim

WORKDIR /app

# Copy the source code and configuration files
COPY . .

# Install uv and dependencies globally within the container
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/
RUN uv pip install --system .

# Expose port 8080 (Cloud Run's default port)
EXPOSE 8080

# Run uvicorn on port 8080
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]
