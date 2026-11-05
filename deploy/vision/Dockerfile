# One image definition, two builds. Only the PyTorch wheels differ:
#   CPU: docker build -f deploy/vision/Dockerfile -t doclib-vision:cpu deploy/vision
#   GPU: docker build -f deploy/vision/Dockerfile -t doclib-vision:gpu \
#          --build-arg TORCH_INDEX=https://download.pytorch.org/whl/cu124 deploy/vision
# Weights are mounted at /models, or baked in for air-gapped sites with --build-arg BAKE_MODEL=zai-org/GLM-OCR.
FROM python:3.12-slim-bookworm
ARG TORCH_INDEX=https://download.pytorch.org/whl/cpu
ARG BAKE_MODEL=

RUN pip install --no-cache-dir --index-url "$TORCH_INDEX" torch torchvision \
 && pip install --no-cache-dir "transformers>=5,<6" accelerate "fastapi>=0.115" "uvicorn>=0.30" "pillow>=10" huggingface_hub
RUN if [ -n "$BAKE_MODEL" ]; then \
      hf download "$BAKE_MODEL" --local-dir "/models/$(basename "$BAKE_MODEL")"; \
    fi
COPY server.py /app/server.py
RUN useradd --create-home --uid 10001 vision
USER vision
WORKDIR /app
# No runtime downloads or vendor telemetry: the server only ever reads local weights.
ENV HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 HF_HUB_DISABLE_TELEMETRY=1 PYTHONUNBUFFERED=1
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=300s \
  CMD python -c "import urllib.request as u; u.urlopen('http://127.0.0.1:8000/healthz', timeout=3)"
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
