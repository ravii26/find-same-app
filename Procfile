# Procfile
api: uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 1 --log-level info
worker: dramatiq app.jobs.actors --processes 1 --threads 4 --queues scans webhooks discovery