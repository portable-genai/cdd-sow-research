#!/usr/bin/env bash
set -euo pipefail

usage() {
  echo "Usage: $0 --image-prefix REGISTRY/PROJECT/REPOSITORY [--tag TAG] [--push] [--sign]"
}

image_prefix=""
image_tag="$(git rev-parse --verify HEAD)"
push=false
sign=false

while [[ $# -gt 0 ]]; do
  case "$1" in
    --image-prefix)
      image_prefix="${2:-}"
      shift 2
      ;;
    --tag)
      image_tag="${2:-}"
      shift 2
      ;;
    --push)
      push=true
      shift
      ;;
    --sign)
      sign=true
      push=true
      shift
      ;;
    *)
      usage >&2
      exit 2
      ;;
  esac
done

if [[ -z "$image_prefix" ]] || [[ ! "$image_tag" =~ ^[A-Za-z0-9._-]{7,128}$ ]]; then
  usage >&2
  exit 2
fi
if [[ "$push" == true && "$sign" != true ]]; then
  echo "Production push requires --sign; unsigned release promotion is refused." >&2
  exit 2
fi
command -v docker >/dev/null

quarantine_suffix="${image_tag}-quarantine-$$"
api_tag="${image_prefix}/doc1-api:${quarantine_suffix}"
ui_tag="${image_prefix}/doc1-ui:${quarantine_suffix}"
api_release_tag="${image_prefix}/doc1-api:${image_tag}"
ui_release_tag="${image_prefix}/doc1-ui:${image_tag}"

npm --prefix ui ci
npm --prefix ui run build:loader
docker build --pull --tag "$api_tag" .
docker build --pull --tag "$ui_tag" --file ui/Dockerfile ui

loader_sri="$(tr -d '\r\n' < ui/public/embed/v1/cdd-agent.js.sri)"
loader_sha256="$(sha256sum ui/public/embed/v1/cdd-agent.js | awk '{print $1}')"
api_local_id="$(docker inspect --format='{{.Id}}' "$api_tag")"
ui_local_id="$(docker inspect --format='{{.Id}}' "$ui_tag")"
api_digest=""
ui_digest=""

if [[ "$push" == true ]]; then
  command -v trivy >/dev/null
  trivy image --exit-code 1 --severity HIGH,CRITICAL "$api_tag"
  trivy image --exit-code 1 --severity HIGH,CRITICAL "$ui_tag"
  docker push "$api_tag"
  docker push "$ui_tag"
  api_digest="$(docker inspect --format='{{range .RepoDigests}}{{println .}}{{end}}' "$api_tag" | grep -F "${image_prefix}/doc1-api@" | head -1)"
  ui_digest="$(docker inspect --format='{{range .RepoDigests}}{{println .}}{{end}}' "$ui_tag" | grep -F "${image_prefix}/doc1-ui@" | head -1)"
  test -n "$api_digest"
  test -n "$ui_digest"
  command -v cosign >/dev/null
  trivy image --exit-code 1 --severity HIGH,CRITICAL "$api_digest"
  trivy image --exit-code 1 --severity HIGH,CRITICAL "$ui_digest"
  cosign sign --yes "$api_digest"
  cosign sign --yes "$ui_digest"
  docker buildx imagetools create --tag "$api_release_tag" "$api_digest"
  docker buildx imagetools create --tag "$ui_release_tag" "$ui_digest"
fi

SOURCE_COMMIT="$(git rev-parse --verify HEAD)" \
API_DIGEST="$api_digest" \
UI_DIGEST="$ui_digest" \
API_LOCAL_ID="$api_local_id" \
UI_LOCAL_ID="$ui_local_id" \
LOADER_SHA256="$loader_sha256" \
LOADER_SRI="$loader_sri" \
node -e '
  const result = {
    source_commit: process.env.SOURCE_COMMIT,
    api_image: process.env.API_DIGEST || null,
    ui_image: process.env.UI_DIGEST || null,
    api_local_image_id: process.env.API_LOCAL_ID,
    ui_local_image_id: process.env.UI_LOCAL_ID,
    loader_sha256: process.env.LOADER_SHA256,
    loader_sri: process.env.LOADER_SRI,
  };
  process.stdout.write(JSON.stringify(result, null, 2) + "\n");
'
