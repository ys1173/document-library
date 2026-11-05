# One image for scanner, worker, vlm-worker, api and extract-server; the command selects the process.
FROM python:3.12-slim-bookworm
ARG RCLONE_VERSION=1.73.2
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl unzip tesseract-ocr tesseract-ocr-eng tesseract-ocr-jpn \
 && arch="$(dpkg --print-architecture)" \
 && curl -fsSL -o /tmp/rclone.zip "https://downloads.rclone.org/v${RCLONE_VERSION}/rclone-v${RCLONE_VERSION}-linux-${arch}.zip" \
 && unzip -j /tmp/rclone.zip '*/rclone' -d /usr/local/bin && chmod 755 /usr/local/bin/rclone && rm /tmp/rclone.zip \
 && apt-get purge -y curl unzip && apt-get autoremove -y && rm -rf /var/lib/apt/lists/*
COPY pyproject.toml README.md /src/
COPY src /src/src
RUN pip install --no-cache-dir /src && rm -rf /src
RUN useradd --create-home --uid 10001 doclib
USER doclib
WORKDIR /home/doclib
# Air-gap safety: no runtime model downloads or vendor telemetry.
ENV HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 HF_HUB_DISABLE_TELEMETRY=1 PYTHONUNBUFFERED=1
ENTRYPOINT ["doclib"]
