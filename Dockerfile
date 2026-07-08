# Container for the mYngle Lead Prioritizer Cloud Run workflow.
#
# Default command runs the Cloud Run Job worker (cloud_job_runner.py). The
# dispatcher (cloud_dispatcher.py) uses the same image with an overridden
# command/entrypoint when deployed as a Cloud Run *service* — see
# docs/cloud_run_workflow.md for both deploy commands.
FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV PYTHONUNBUFFERED=1

# Cloud Run Jobs worker (one task per row-shard). Override for the
# dispatcher service, e.g.:
#   CMD ["uvicorn", "cloud_dispatcher:app", "--host", "0.0.0.0", "--port", "8080"]
CMD ["python", "cloud_job_runner.py"]
