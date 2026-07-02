# Use official Python 3.12 slim image
FROM python:3.12-slim

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    git \
    && rm -rf /var/lib/apt/lists/*

# Install uv package manager
RUN curl -LsSf https://astral.sh/uv/install.sh | sh
ENV PATH="/root/.local/bin:${PATH}"

# Set working directory
WORKDIR /app

# Copy dependency files
COPY pyproject.toml README.md ./

# Install project dependencies
RUN uv sync --no-dev

# Copy project source code
COPY app/ ./app/
COPY mcp_server/ ./mcp_server/
COPY pyproject.toml ./

# Expose FastAPI port
EXPOSE 8000

# Set environment variable defaults
ENV USE_MCP_DEALS=true
ENV PYTHONUNBUFFERED=1

# Command to run FastAPI server
CMD ["uv", "run", "uvicorn", "app.fast_api_app:app", "--host", "0.0.0.0", "--port", "8000"]