#!/usr/bin/env bash
set -euo pipefail

usage() {
  echo "Usage: $0 --image-prefix REGISTRY/PROJECT/REPOSITORY [--tag TAG] [--push] [--sign] [--cosign-key PATH|KMS_URI]"
}

image_prefix=""
image_tag="$(git rev-parse --verify HEAD)"
push=false
sign=false
# Empty means KEYLESS signing (Sigstore/Fulcio), which is the production path and stays the
# default. Keyless needs an interactive OIDC flow or an ambient workload identity, so it
# cannot run on a workstation with no browser and no metadata server — which is exactly the
# reference deployment's situation. --cosign-key names a private key instead. Both produce a
# real, verifiable signature; what differs is who vouches for the identity behind it: Fulcio
# ties the signature to an authenticated identity with a transparency-log entry, a local key
# ties it only to whoever holds the file. A reference deployment says which one it used.
#
# --cosign-key also accepts a KMS URI ("gcpkms://projects/..."), and that is the third
# answer rather than a convenience: the private key never leaves the KMS, so signing is an
# IAM-controlled, audit-logged call instead of custody of a file, which is the one property a
# local key cannot offer and keyless cannot deliver without a browser. A URI is not a path,
# so the -f check below is scoped to the file form; asserting a file that exists for a
# gcpkms:// value would refuse the stronger option for failing a test that does not apply to
# it. The scheme is what distinguishes them, and a bare relative path carries none.
cosign_key=""

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
    --cosign-key)
      cosign_key="${2:-}"
      shift 2
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
# --platform linux/amd64 is NOT optional. Cloud Run refuses an image whose manifest does not
# support amd64/linux, and a plain `docker build` on an Apple Silicon workstation produces
# exactly that: an arm64-only image that pushes, scans, signs and promotes without complaint
# and is then rejected at deploy time by the one system that matters. Pinning the platform
# here means the artefact a maintainer builds locally is the artefact that can actually run.
# --no-cache is as load-bearing as --platform above, and for a neighbouring reason. The
# runtime stage applies Debian security updates on top of a digest-pinned base, precisely
# because a digest pin freezes unpatched packages. But the instruction that does it is
# unchanged from build to build, so with a warm cache Docker reuses the layer and the image
# ships the patch level of whenever that cache was filled -- while the Dockerfile comment
# claims the opposite in place. That is what the 2026-08-26 promotion did: openssl 3.5.6
# with 3.5.7-1~deb13u2 released and fixed, and only the blocking trivy pass caught it.
# A cached security layer is a stale one, so a release build resolves every layer again.
docker build --platform linux/amd64 --pull --no-cache --tag "$api_tag" .
docker build --platform linux/amd64 --pull --no-cache --tag "$ui_tag" --file ui/Dockerfile ui

loader_sri="$(tr -d '\r\n' < ui/public/embed/v1/cdd-agent.js.sri)"
loader_sha256="$(sha256sum ui/public/embed/v1/cdd-agent.js | awk '{print $1}')"
api_local_id="$(docker inspect --format='{{.Id}}' "$api_tag")"
ui_local_id="$(docker inspect --format='{{.Id}}' "$ui_tag")"
api_digest=""
ui_digest=""

if [[ "$push" == true ]]; then
  command -v trivy >/dev/null
  # Two passes, deliberately. The BLOCKING pass gates on findings that have a fix available,
  # because that is the set a promotion decision can act on: refusing to ship over a
  # vulnerability with no released patch does not make anything safer, it just makes the gate
  # impossible to satisfy and trains people to bypass it. The base image genuinely carries
  # such findings — on 2026-08-24 python:3.14-slim shipped 14 HIGH and 3 CRITICAL Debian
  # advisories with no fixed version, perl-base among them.
  #
  # The REPORTING pass then prints everything, unfixed included, and does not gate. That
  # split is the point: --ignore-unfixed alone would quietly shrink what anyone ever sees,
  # which is how "no findings" comes to mean "none we chose to look at". The operator still
  # reads the full list on every promotion; only the automatic refusal is scoped to what can
  # actually be fixed today.
  trivy image --exit-code 1 --ignore-unfixed --severity HIGH,CRITICAL "$api_tag"
  trivy image --exit-code 1 --ignore-unfixed --severity HIGH,CRITICAL "$ui_tag"
  echo "--- full scan including findings with no fix available (reporting only) ---"
  trivy image --exit-code 0 --severity HIGH,CRITICAL "$api_tag"
  trivy image --exit-code 0 --severity HIGH,CRITICAL "$ui_tag"
  docker push "$api_tag"
  docker push "$ui_tag"
  api_digest="$(docker inspect --format='{{range .RepoDigests}}{{println .}}{{end}}' "$api_tag" | grep -F "${image_prefix}/doc1-api@" | head -1)"
  ui_digest="$(docker inspect --format='{{range .RepoDigests}}{{println .}}{{end}}' "$ui_tag" | grep -F "${image_prefix}/doc1-ui@" | head -1)"
  test -n "$api_digest"
  test -n "$ui_digest"
  command -v cosign >/dev/null
  trivy image --exit-code 1 --ignore-unfixed --severity HIGH,CRITICAL "$api_digest"
  trivy image --exit-code 1 --ignore-unfixed --severity HIGH,CRITICAL "$ui_digest"
  if [[ -n "$cosign_key" ]]; then
    if [[ "$cosign_key" != *"://"* ]]; then
      test -f "$cosign_key"
    fi
    cosign sign --yes --key "$cosign_key" "$api_digest"
    cosign sign --yes --key "$cosign_key" "$ui_digest"
  else
    cosign sign --yes "$api_digest"
    cosign sign --yes "$ui_digest"
  fi
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
