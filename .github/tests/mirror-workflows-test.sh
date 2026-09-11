#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
TMP_DIR=$(mktemp -d)
trap 'rm -rf "$TMP_DIR"' EXIT

DIGEST_A="sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
DIGEST_B="sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
FAKE_BIN="$TMP_DIR/bin"
FAKE_LOG="$TMP_DIR/regctl.log"
FAKE_STATE_DIR="$TMP_DIR/state"
mkdir -p "$FAKE_BIN" "$FAKE_STATE_DIR"

extract_run_step() {
  local workflow_file=$1
  local step_name=$2
  local output_file=$3

  awk -v wanted="$step_name" '
    $0 == "      - name: " wanted { in_step = 1; next }
    in_step && $0 == "        run: |" { in_run = 1; next }
    in_run && $0 == "" { print; next }
    in_run && substr($0, 1, 10) == "          " { print substr($0, 11); next }
    in_run { exit }
    END { if (!in_run) exit 1 }
  ' "$workflow_file" > "$output_file"
}

cat > "$FAKE_BIN/regctl" <<'EOF'
#!/usr/bin/env bash

set -euo pipefail

printf '%s\n' "$*" >> "$FAKE_LOG"

if [[ "$1 $2" == "image digest" ]]; then
  ref=$3
  if [[ "$ref" == "$FAKE_TARGET_REF" ]]; then
    count_file="$FAKE_STATE_DIR/target-lookups"
    count=0
    if [[ -f "$count_file" ]]; then
      count=$(<"$count_file")
    fi
    count=$((count + 1))
    printf '%s\n' "$count" > "$count_file"

    if [[ "$count" -eq 1 ]]; then
      case "$FAKE_TARGET_INITIAL" in
        existing)
          printf '%s\n' "$FAKE_SOURCE_DIGEST"
          ;;
        missing)
          echo "failed to request manifest head: request failed: not found [http 404]" >&2
          exit 1
          ;;
        error)
          echo "failed to request manifest head: request failed: too many requests [http 429]" >&2
          exit 1
          ;;
      esac
    else
      printf '%s\n' "$FAKE_FINAL_DIGEST"
    fi
    exit 0
  fi

  if [[ "$ref" == "$FAKE_SOURCE_TAG_REF" ]]; then
    printf '%s\n' "$FAKE_SOURCE_DIGEST"
    exit 0
  fi

  if [[ "$ref" == "$FAKE_SOURCE_PIN_REF" ]]; then
    case "$FAKE_SOURCE_PIN_STATUS" in
      found)
        printf '%s\n' "$FAKE_SOURCE_DIGEST"
        ;;
      missing)
        echo "failed to request manifest head: request failed: not found [http 404]" >&2
        exit 1
        ;;
      error)
        echo "failed to request manifest head: request failed: service unavailable [http 503]" >&2
        exit 1
        ;;
    esac
    exit 0
  fi
fi

if [[ "$1 $2" == "image copy" ]]; then
  count_file="$FAKE_STATE_DIR/copies"
  count=0
  if [[ -f "$count_file" ]]; then
    count=$(<"$count_file")
  fi
  printf '%s\n' "$((count + 1))" > "$count_file"
  [[ "$FAKE_COPY_STATUS" == "success" ]]
  exit
fi

echo "unexpected regctl command: $*" >&2
exit 99
EOF

cat > "$FAKE_BIN/sleep" <<'EOF'
#!/usr/bin/env bash
exit 0
EOF

chmod +x "$FAKE_BIN/regctl" "$FAKE_BIN/sleep"

DOCKER_SCRIPT="$TMP_DIR/docker.sh"
HELM_SCRIPT="$TMP_DIR/helm.sh"
extract_run_step \
  "$ROOT_DIR/.github/workflows/mirror-docker-image.yml" \
  "Copy image and OCI referrers" \
  "$DOCKER_SCRIPT"
extract_run_step \
  "$ROOT_DIR/.github/workflows/mirror-helm-chart.yml" \
  "Copy OCI chart and referrers" \
  "$HELM_SCRIPT"

reset_case() {
  rm -f "$FAKE_LOG" "$FAKE_STATE_DIR/target-lookups" "$FAKE_STATE_DIR/copies"
  export FAKE_TARGET_INITIAL=missing
  export FAKE_SOURCE_DIGEST="$DIGEST_A"
  export FAKE_FINAL_DIGEST="$DIGEST_A"
  export FAKE_SOURCE_PIN_STATUS=found
  export FAKE_COPY_STATUS=success
}

run_case() {
  local expected_status=$1
  local script=$2
  local output_file=$3
  local status

  if PATH="$FAKE_BIN:$PATH" bash -e -o pipefail "$script" > "$output_file" 2>&1; then
    status=0
  else
    status=$?
  fi

  if [[ "$expected_status" == success && "$status" -ne 0 ]]; then
    cat "$output_file" >&2
    echo "expected success, got exit ${status}" >&2
    exit 1
  fi
  if [[ "$expected_status" == failure && "$status" -eq 0 ]]; then
    cat "$output_file" >&2
    echo "expected failure, got success" >&2
    exit 1
  fi
}

copy_count() {
  if [[ -f "$FAKE_STATE_DIR/copies" ]]; then
    cat "$FAKE_STATE_DIR/copies"
  else
    echo 0
  fi
}

assert_copy_count() {
  local expected=$1
  local actual
  actual=$(copy_count)
  if [[ "$actual" != "$expected" ]]; then
    echo "expected ${expected} copies, got ${actual}" >&2
    exit 1
  fi
}

configure_docker_refs() {
  export SOURCE_IMAGE=source.example/image
  export TARGET_PACKAGE=owner/image
  export VERSION=v1
  export REGISTRY=target.example
  export FAKE_SOURCE_TAG_REF="${SOURCE_IMAGE}:${VERSION}"
  export FAKE_SOURCE_PIN_REF="${SOURCE_IMAGE}@${FAKE_SOURCE_DIGEST}"
  export FAKE_TARGET_REF="${REGISTRY}/${TARGET_PACKAGE}:${VERSION}"
}

configure_helm_refs() {
  export CHART_REPO_URL=oci://source.example/charts
  export CHART_NAME=demo
  export CHART_VERSION=1.0.0
  export TARGET_PACKAGE=owner/charts
  export REGISTRY=target.example
  export FAKE_SOURCE_TAG_REF="source.example/charts/${CHART_NAME}:${CHART_VERSION}"
  export FAKE_SOURCE_PIN_REF="source.example/charts/${CHART_NAME}@${FAKE_SOURCE_DIGEST}"
  export FAKE_TARGET_REF="${REGISTRY}/${TARGET_PACKAGE}/${CHART_NAME}:${CHART_VERSION}"
}

export FAKE_LOG FAKE_STATE_DIR

reset_case
configure_docker_refs
run_case success "$DOCKER_SCRIPT" "$TMP_DIR/docker-new-target.out"
grep -Fxq \
  "image copy --referrers --force-recursive ${FAKE_SOURCE_PIN_REF} ${FAKE_TARGET_REF}" \
  "$FAKE_LOG"

reset_case
configure_docker_refs
export FAKE_TARGET_INITIAL=error
run_case failure "$DOCKER_SCRIPT" "$TMP_DIR/docker-target-error.out"
assert_copy_count 0
grep -Fq "refusing to treat it as absent" "$TMP_DIR/docker-target-error.out"

reset_case
configure_docker_refs
export FAKE_TARGET_INITIAL=existing
export FAKE_SOURCE_PIN_STATUS=missing
run_case success "$DOCKER_SCRIPT" "$TMP_DIR/docker-source-missing.out"
assert_copy_count 0
grep -Fq "keeping ${FAKE_TARGET_REF} unchanged" "$TMP_DIR/docker-source-missing.out"

reset_case
configure_docker_refs
export FAKE_TARGET_INITIAL=existing
export FAKE_SOURCE_PIN_STATUS=error
run_case failure "$DOCKER_SCRIPT" "$TMP_DIR/docker-source-error.out"
assert_copy_count 0
grep -Fq "refusing to modify ${FAKE_TARGET_REF}" "$TMP_DIR/docker-source-error.out"

reset_case
configure_docker_refs
export FAKE_COPY_STATUS=failure
run_case failure "$DOCKER_SCRIPT" "$TMP_DIR/docker-retries.out"
assert_copy_count 5

reset_case
configure_docker_refs
export FAKE_FINAL_DIGEST="$DIGEST_B"
run_case failure "$DOCKER_SCRIPT" "$TMP_DIR/docker-mismatch.out"
grep -Fq "does not match expected digest" "$TMP_DIR/docker-mismatch.out"

reset_case
configure_helm_refs
run_case success "$HELM_SCRIPT" "$TMP_DIR/helm-new-target.out"
grep -Fxq \
  "image copy --referrers --force-recursive ${FAKE_SOURCE_PIN_REF} ${FAKE_TARGET_REF}" \
  "$FAKE_LOG"

reset_case
configure_helm_refs
export FAKE_TARGET_INITIAL=error
run_case failure "$HELM_SCRIPT" "$TMP_DIR/helm-target-error.out"
assert_copy_count 0
grep -Fq "refusing to treat it as absent" "$TMP_DIR/helm-target-error.out"

echo "mirror workflow tests passed"
