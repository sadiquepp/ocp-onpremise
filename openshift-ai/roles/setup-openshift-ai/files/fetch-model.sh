#!/bin/sh
# Download a model from Hugging Face onto the PVC the model server reads.
#
# Runs as the only container of the fetch Job (tasks/model.yml), mounted from
# a ConfigMap, with the model PVC at /models. POSIX sh and curl only - the
# image is ubi-minimal, which has no jq, no python and no huggingface-cli, and
# installing any of them would mean reaching a package repository as well as
# Hugging Face.
#
# Two modes, because the two runtimes this lab offers want different files:
#
#   gguf      one quantised .gguf file, for llama.cpp. The file NAME is not
#             passed in - the repository is listed through the Hugging Face
#             API and the first .gguf whose name contains FILE_MATCH is taken,
#             because quantisation file names differ per repository and change
#             when a repository is re-uploaded.
#   snapshot  the whole repository minus the duplicates, for vLLM: the
#             safetensors weights, the config and the tokenizer.
#
# Idempotent: a DEST that already holds .fetch-complete is left alone, so
# re-running the model phase does not re-download hundreds of MiB. Everything
# is written to a .partial directory first and moved into place at the end, so
# a Job that is killed half way leaves no DEST that looks finished.
#
# Env, all set by templates/model-fetch.yaml.j2:
#   MODE         gguf | snapshot
#   HF_ENDPOINT  https://huggingface.co, or a mirror of it
#   HF_REPO      e.g. Qwen/Qwen2.5-0.5B-Instruct-GGUF
#   FILE_MATCH   gguf mode: fragment to match in the file name, e.g. q4_k_m
#   DEST         where the files end up, e.g. /models/qwen...-llamacpp
#   HF_TOKEN     optional; only a gated repository needs one
set -eu

: "${MODE:?MODE must be gguf or snapshot}"
: "${HF_ENDPOINT:?HF_ENDPOINT must be set}"
: "${HF_REPO:?HF_REPO must be set}"
: "${DEST:?DEST must be set}"

PARTIAL="${DEST}.partial"
MARKER="${DEST}/.fetch-complete"

log() { echo "$(date -u +%H:%M:%S) $*"; }

# curl, with the token only when there is one. An empty Authorization header
# is not the same as no header: Hugging Face answers 401 to it.
hf_curl() {
  if [ -n "${HF_TOKEN:-}" ]; then
    curl --fail --location --silent --show-error \
         --retry 5 --retry-delay 10 --retry-connrefused \
         --header "Authorization: Bearer ${HF_TOKEN}" "$@"
  else
    curl --fail --location --silent --show-error \
         --retry 5 --retry-delay 10 --retry-connrefused "$@"
  fi
}

if [ -f "$MARKER" ]; then
  log "already fetched; $MARKER says:"
  cat "$MARKER"
  exit 0
fi

log "listing ${HF_REPO} at ${HF_ENDPOINT}"
if ! hf_curl -o /tmp/model-info.json "${HF_ENDPOINT}/api/models/${HF_REPO}"; then
  log "ERROR: could not list ${HF_REPO}."
  log "  A connected cluster reaches ${HF_ENDPOINT} directly; a disconnected"
  log "  one needs rhoai_hf_endpoint pointed at something inside it, or the"
  log "  weights put on the PVC by hand - see openshift-ai/README.md."
  log "  A 401 here means the repository is gated: put hf_token in vault.yaml."
  exit 1
fi

# The API returns {"siblings":[{"rfilename":"config.json"},...]}. One field,
# one shape, no nesting - grep is honest about this one.
grep -o '"rfilename"[[:space:]]*:[[:space:]]*"[^"]*"' /tmp/model-info.json \
  | sed 's/.*"\([^"]*\)"$/\1/' \
  | sort -u > /tmp/model-files.txt

if [ ! -s /tmp/model-files.txt ]; then
  log "ERROR: ${HF_REPO} listed no files. The API answered:"
  head -c 500 /tmp/model-info.json
  exit 1
fi
log "$(wc -l < /tmp/model-files.txt) file(s) in the repository"

case "$MODE" in
  gguf)
    : "${FILE_MATCH:?FILE_MATCH must be set in gguf mode}"
    # Lower-cased comparison on both sides: repositories are inconsistent
    # about Q4_K_M vs q4_k_m.
    WANT="$(tr '[:upper:]' '[:lower:]' < /tmp/model-files.txt \
            | grep '\.gguf$' | grep -- "$(echo "$FILE_MATCH" | tr '[:upper:]' '[:lower:]')" \
            | head -1 || true)"
    # Recover the file's real spelling from the unfolded list.
    if [ -n "$WANT" ]; then
      WANT="$(awk -v w="$WANT" 'tolower($0)==w {print; exit}' /tmp/model-files.txt)"
    fi
    if [ -z "$WANT" ]; then
      log "ERROR: no .gguf in ${HF_REPO} whose name contains '${FILE_MATCH}'."
      log "  The repository offers:"
      grep '\.gguf$' /tmp/model-files.txt | sed 's/^/    /' || log "    no .gguf files at all"
      log "  Set rhoai_model_file_match to a fragment of one of those."
      exit 1
    fi
    FILES="$WANT"
    ;;
  snapshot)
    # What vLLM needs: the config, the tokenizer and the safetensors. What is
    # deliberately left out:
    #   *.gguf, *.bin, *.pth  the same weights again in another format - on a
    #                         repository that ships both, downloading both
    #                         doubles the PVC for nothing
    #   anything in a subdirectory ("original/", "onnx/") - those are
    #                         alternative packagings of the same model
    #   the documentation and the git metadata
    FILES="$(grep -v '/' /tmp/model-files.txt \
             | grep -v -E '\.(gguf|bin|pth|md|png|jpg|jpeg|svg)$' \
             | grep -v -E '^\.git' || true)"
    if ! echo "$FILES" | grep -q '\.safetensors$'; then
      log "ERROR: ${HF_REPO} has no top-level .safetensors file, so there is"
      log "  nothing for vLLM to load. Files considered:"
      echo "$FILES" | sed 's/^/    /'
      exit 1
    fi
    ;;
  *)
    log "ERROR: MODE is '${MODE}'; it is gguf or snapshot."
    exit 1
    ;;
esac

log "will fetch:"
echo "$FILES" | sed 's/^/    /'

rm -rf "$PARTIAL"
mkdir -p "$PARTIAL"

for f in $FILES; do
  log "fetching ${f}"
  hf_curl -o "${PARTIAL}/${f}" "${HF_ENDPOINT}/${HF_REPO}/resolve/main/${f}"
  ls -l "${PARTIAL}/${f}"
done

# The model server runs as a different random UID in the same namespace, and
# gets the namespace's fsGroup on this volume - but world-readable costs
# nothing and removes one way for the server to come up and then fail to open
# its own weights.
chmod -R a+rX "$PARTIAL"

{
  echo "repo:    ${HF_REPO}"
  echo "mode:    ${MODE}"
  echo "fetched: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo "files:"
  echo "$FILES" | sed 's/^/  /'
} > "${PARTIAL}/.fetch-complete"

rm -rf "$DEST"
mv "$PARTIAL" "$DEST"
log "done: ${DEST}"
ls -l "$DEST"
