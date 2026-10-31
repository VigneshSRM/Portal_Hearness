FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    REDMINE_AGENT_HOME=/app

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY redmine_agent ./redmine_agent
COPY agent ./agent
COPY config ./config
COPY pyproject.toml README.md ./

RUN useradd --create-home --uid 1000 harness && mkdir -p /app/data && chown -R harness /app/data
USER harness

EXPOSE 8000
CMD ["python", "-m", "redmine_agent", "dashboard", "--host", "0.0.0.0", "--port", "8000"]
