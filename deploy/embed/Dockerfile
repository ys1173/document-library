# For testing and small sites. Production can point doclib at any OpenAI-compatible embedding service instead.
#   CPU: docker build -t doclib-embed:cpu deploy/embed
#   GPU: docker build -t doclib-embed:gpu --build-arg TORCH_INDEX=https://download.pytorch.org/whl/cu124 deploy/embed
# Weights are mounted at /models, or baked in for air-gapped sites with --build-arg BAKE_MODEL=BAAI/bge-m3.
FROM python:3.12-slim-bookworm
ARG TORCH_INDEX=https://download.pytorch.org/whl/cpu
ARG BAKE_MODEL=
RUN pip install --no-cache-dir --index-url "$TORCH_INDEX" torch \
 && pip install --no-cache-dir "transformers>=5,<6" "fastapi>=0.115" "uvicorn>=0.30" huggingface_hub
RUN if [ -n "$BAKE_MODEL" ]; then \
      hf download "$BAKE_MODEL" --exclude 'onnx/*' --exclude 'imgs/*' --local-dir "/models/$(basename "$BAKE_MODEL")"; \
    fi
COPY server.py /app/server.py
RUN useradd --create-home --uid 10001 embed
USER embed
WORKDIR /app
ENV HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 HF_HUB_DISABLE_TELEMETRY=1 PYTHONUNBUFFERED=1
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=120s \
  CMD python -c "import urllib.request as u; u.urlopen('http://127.0.0.1:8080/healthz', timeout=3)"
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8080", "--no-access-log"]
