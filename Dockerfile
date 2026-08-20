FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8080

# --limit-concurrency is a SAFETY VALVE, not a throttle. It must sit well above
# normal bursts: excess requests get a hard 503, so setting it near the pool
# size turns ordinary load into user-visible failures (32 was measured rejecting
# 87 of 120 requests). Requests are meant to queue briefly on the connection
# pool instead — see database.py. This only trips in genuine overload, where a
# fast 503 beats every request hanging.
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080", "--limit-concurrency", "64"]