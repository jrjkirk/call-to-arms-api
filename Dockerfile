FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8080

# --limit-concurrency bounds in-flight requests. A connection is held for a
# request's whole lifetime (acquired in require_user's dependency resolution),
# so unbounded concurrency means unbounded connection demand — which is exactly
# how the admin page's ~60-request burst exhausted the pool. Excess requests get
# a fast 503 the browser can retry, instead of every request in the burst
# blocking 30s on the pool and then 500ing. Keep this BELOW database.py's pool
# capacity; they are sized against each other.
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080", "--limit-concurrency", "32"]