FROM python:3.12-slim
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg \
 && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir silk-python zstandard
COPY bot /opt/chatwarden/bot
COPY gateway /opt/chatwarden/gateway
COPY tools /opt/chatwarden/tools
COPY examples /opt/chatwarden/examples
COPY tests /opt/chatwarden/tests
WORKDIR /opt/chatwarden
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
CMD ["python3", "bot/bot.py"]
