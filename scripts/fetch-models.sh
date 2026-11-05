#!/bin/sh
# Downloads the model weights into Docker volumes. Safe to run again: anything already downloaded is skipped.
#   scripts/fetch-models.sh            the embedding model (BGE-M3, about 2.2 GB)
#   scripts/fetch-models.sh --vision   also the vision model (GLM-OCR, about 2.5 GB)
set -eu
cd "$(dirname "$0")/.."

fetch() { # image, dockerfile folder, volume, Hugging Face repo, folder name, a file that proves it worked, extra args
  image=$1; context=$2; volume=$3; repo=$4; folder=$5; proof=$6; shift 6
  docker volume create "$volume" >/dev/null
  verify() { docker run --rm -v "$volume:/models:ro" --entrypoint sh "$image" -c "test -f '/models/$folder/$proof'"; }
  echo "==> Building $image"
  docker build -q -t "$image" "$context" >/dev/null
  if verify 2>/dev/null; then
    echo "==> $repo is already downloaded"
  else
    echo "==> Downloading $repo (this can take several minutes)"
    docker run --rm --user root -e HF_HUB_OFFLINE=0 -e TRANSFORMERS_OFFLINE=0 -e HF_HOME=/tmp/hf -v "$volume:/models" \
      --entrypoint hf "$image" download "$repo" "$@" --local-dir "/models/$folder"
    docker run --rm --user root -v "$volume:/models" --entrypoint chown "$image" -R 10001 /models
    # The download tool can exit successfully without fetching what we need, so check for the files themselves.
    verify || { echo "ERROR: $repo did not download correctly: $proof is missing" >&2; exit 1; }
  fi
  echo "==> $repo is ready"
}

fetch doclib-embed:cpu deploy/embed doclib-embed-models BAAI/bge-m3 bge-m3 pytorch_model.bin --exclude 'onnx/*' --exclude 'imgs/*'
if [ "${1:-}" = "--vision" ]; then
  fetch doclib-vision:cpu deploy/vision doclib-vision-models zai-org/GLM-OCR GLM-OCR config.json
fi
