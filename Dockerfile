FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# SQLite DB file lives here - mount a volume to this path in production so
# data survives container restarts/redeploys.
VOLUME ["/app/data"]
ENV DATABASE_URL=sqlite+aiosqlite:///./data/wa_assistant.db

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
