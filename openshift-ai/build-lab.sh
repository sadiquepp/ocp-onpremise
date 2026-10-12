#!/usr/bin/env bash
#
# Build the OpenShift AI lab - the operator, a DataScienceCluster, one small
# language model served on CPU, and a chat bot - in one command.
#
#   ./build-lab.sh --cluster hub            # everything, on the hub
#   ./build-lab.sh --cluster hub --list     # what the steps are
#   ./build-lab.sh --cluster hub --dry-run  # print the commands, run nothing
#   ./build-lab.sh --cluster hub --only preflight    # one step
#   ./build-lab.sh --cluster hub --from model        # resume at a step
#   ./build-lab.sh --cluster hub --runtime vllm-cpu  # the CPU vLLM runtime
#   ./build-lab.sh --cluster hub --no-vault # do not read ../vault.yaml at all
#   ./build-lab.sh --cluster hub --only cleanup      # remove it all again
#   ./build-lab.sh --cluster hub -e @my-vars.yaml    # extra args, passed to
#                                                    # every playbook
#
# --cluster is required and has no default on purpose: these steps install a
# cluster-wide operator, and which cluster that happens to is not a thing to
# guess at. It is hub, hub2 or sno - the keys of rhoai_clusters in vars.yaml.
#
# WHAT IT DOES NOT DO: build the cluster. Unlike spiffe-spire/build-lab.sh,
# this one has no base-lab steps - OpenShift AI is additive to a cluster that
# already exists, so build one first with the repository's own playbooks
# (../setup_hub_cluster.yaml, ../setup_sno.yaml) and point this at it.
#
# HOW LONG. The operator step is OLM and a few GiB of image pulls; the dsc
# step rolls the dashboard and the KServe controller; the model step
# downloads a few hundred MiB and then waits for a model to load on a CPU. On
# a lab hub the whole run is usually 20-40 minutes, nearly all of it waiting.
# Every step is idempotent, so a run that is interrupted can be resumed with
# --from.
set -euo pipefail

STEPS=(preflight operator dsc project model chatbot verify)

usage() { sed -n '2,/^set -/p' "$0" | sed '$d; s/^# \{0,1\}//'; }

list_steps() {
  cat <<'EOF'
Steps, in order:

  preflight  is the operator in the catalog, is there a default StorageClass,
             has the cluster the CPU and memory, do its CPUs suit the runtime.
             Changes nothing.
  operator   Namespace, OperatorGroup, Subscription; waits for the CSV.
  dsc        DSCInitialization and DataScienceCluster; waits for both, and
             reports the dashboard's Route.
  project    the data science project namespace and the model PVC.
  model      the fetch Job, the ServingRuntime and the InferenceService;
             waits for the model to answer.
  chatbot    the chat bot's ConfigMap, Deployment, Service and Route.
  verify     asserts only, from the operator up to the app's own demo prompt
             answered over the Route.

Not in the sequence, run it with --only:

  cleanup    remove everything above from that cluster.
EOF
}

CLUSTER=""
RUNTIME=""
ONLY=""
FROM=""
DRY=0
NO_VAULT=0
WANT_LIST=0
EXTRA=()

while (($#)); do
  case "$1" in
    --cluster) CLUSTER="${2:?--cluster needs a name}"; shift 2 ;;
    --runtime) RUNTIME="${2:?--runtime needs llamacpp or vllm-cpu}"; shift 2 ;;
    --only)    ONLY="${2:?--only needs a step}"; shift 2 ;;
    --from)    FROM="${2:?--from needs a step}"; shift 2 ;;
    --no-vault) NO_VAULT=1; shift ;;
    --dry-run) DRY=1; shift ;;
    --list)    WANT_LIST=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *)         EXTRA+=("$1"); shift ;;
  esac
done

((WANT_LIST)) && { list_steps; exit 0; }

if [[ -z "$CLUSTER" ]]; then
  echo "error: --cluster is required (hub, hub2 or sno - see rhoai_clusters in vars.yaml)." >&2
  echo "       ./build-lab.sh --help" >&2
  exit 2
fi

# A step name that is not a step is a typo, and a typo that runs six other
# steps against a cluster is worse than an error message.
valid_step() {
  local s
  for s in "${STEPS[@]}" cleanup; do [[ "$s" == "$1" ]] && return 0; done
  return 1
}
for s in ${ONLY:+$ONLY} ${FROM:+$FROM}; do
  valid_step "$s" || { echo "error: '$s' is not a step. --list shows them." >&2; exit 2; }
done
[[ -n "$ONLY" && -n "$FROM" ]] && { echo "error: --only and --from are exclusive." >&2; exit 2; }

# ../vault.yaml is read for one variable, hf_token, which only a GATED model
# needs - but vars_files is resolved before any task runs, so an encrypted
# vault.yaml has to be openable even on a phase that reads nothing from it, or
# the run dies with "Attempting to decrypt but no vault secrets found".
#
# So: --vault-password-file when the usual file is there, --ask-vault-pass
# when it is not, nothing at all when there is no ../vault.yaml - and with
# --no-vault, point the playbook's first-found list at its own vars.yaml so
# the vault is never opened. That last one is for "I have no password to hand"
# and is fine for every phase unless the model is gated.
vault_args() {
  if ((NO_VAULT)); then
    printf -- '-e\nrhoai_vault=vars.yaml\n'
    return 0
  fi
  [[ -r ../vault.yaml ]] || return 0
  if [[ -r ~/.vault_pass ]]; then
    printf -- '--vault-password-file\n%s\n' "$HOME/.vault_pass"
  else
    printf -- '--ask-vault-pass\n'
  fi
}

INVENTORY=../inventory/hosts
inv() { [[ -r "$INVENTORY" ]] && printf -- '-i\n%s\n' "$INVENTORY"; return 0; }

say() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }

run_step() {
  local step="$1"
  local -a cmd=(ansible-playbook)
  mapfile -t -O "${#cmd[@]}" cmd < <(inv)
  cmd+=(setup_openshift_ai.yaml --tags "$step" -e "rhoai_cluster=$CLUSTER")
  [[ -n "$RUNTIME" ]] && cmd+=(-e "rhoai_runtime=$RUNTIME")
  mapfile -t -O "${#cmd[@]}" cmd < <(vault_args)
  ((${#EXTRA[@]})) && cmd+=("${EXTRA[@]}")

  say "$step on $CLUSTER"
  if ((DRY)); then
    printf '   '; printf ' %q' "${cmd[@]}"; printf '\n'
    return 0
  fi
  "${cmd[@]}"
}

if [[ -n "$ONLY" ]]; then
  run_step "$ONLY"
  exit 0
fi

started=0
for step in "${STEPS[@]}"; do
  if [[ -n "$FROM" && $started -eq 0 ]]; then
    [[ "$step" == "$FROM" ]] && started=1 || continue
  fi
  run_step "$step"
done

if ((DRY)); then
  exit 0
fi

cat <<EOF

Done. The chat bot's Route is in the chatbot step's output, and the
OpenShift AI dashboard's is in the dsc step's. To take it all down again:

  ./build-lab.sh --cluster $CLUSTER --only cleanup
EOF
