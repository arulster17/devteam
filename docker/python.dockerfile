FROM python:3.12-slim

WORKDIR /workspace

# Deps layer — cached separately from source so rebuilds after code changes are fast
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt pytest pytest-json-report

COPY . .

CMD sh -c 'if [ -n "$RESULTS_PATH" ]; then python -m pytest -v --json-report --json-report-file="$RESULTS_PATH"; else python -m pytest -v; fi'
