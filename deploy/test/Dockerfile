# Test image: the product image's system packages plus dev dependencies. Source is mounted at run time.
FROM python:3.12-slim-bookworm
ARG RCLONE_VERSION=1.73.2
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl unzip fonts-dejavu-core postgresql-client tesseract-ocr tesseract-ocr-eng tesseract-ocr-jpn \
 && arch="$(dpkg --print-architecture)" \
 && curl -fsSL -o /tmp/rclone.zip "https://downloads.rclone.org/v${RCLONE_VERSION}/rclone-v${RCLONE_VERSION}-linux-${arch}.zip" \
 && unzip -j /tmp/rclone.zip '*/rclone' -d /usr/local/bin && chmod 755 /usr/local/bin/rclone && rm /tmp/rclone.zip
COPY pyproject.toml README.md /src/
COPY src /src/src
RUN pip install --no-cache-dir -e '/src[dev]'
WORKDIR /src
