FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg ca-certificates && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir ".[asr]"
RUN mkdir -p /data /tmp/link-reader
ENV PYTHONUNBUFFERED=1 DATABASE_PATH=/data/link_reader.db
CMD ["link-reader-bot"]
