FROM python:3.11-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY receiver.py kill_query.py helicopter_monitor.py event_monitor.py event_export.py /app/
USER 1000:1000
CMD ["python", "/app/receiver.py"]
