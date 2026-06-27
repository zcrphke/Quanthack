FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# 3-strategy system files
COPY models.py .
COPY multi_pairs_server.py .
COPY claude_coordinator.py .
COPY strategy_engine.py .
COPY pairs_config.py .
COPY risk_supervisor.py .
COPY config.py .
COPY validate.py .
# State persists in /data (Northflank persistent volume)
ENV STATE_FILE=/data/mpairs_state.json
ENV PORT=8003
EXPOSE 8003
CMD ["gunicorn","--bind","0.0.0.0:8003","--workers","1","--timeout","120","--access-logfile","-","multi_pairs_server:app"]
