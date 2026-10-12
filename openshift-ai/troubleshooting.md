# OpenShift AI on CPU - troubleshooting

Each case below is a way this lab fails, what the failure actually looks like,
and what to do. In rough order of how often it happens.

The quickest way to find out which case you are in is the preflight phase -
it reads only, so it is safe at any point:

```bash
cd openshift-ai
ansible-playbook -i ../inventory/hosts setup_openshift_ai.yaml --tags preflight \
  -e rhoai_cluster=hub
```

## Contents

- [0. "Attempting to decrypt but no vault secrets found"](#0-attempting-to-decrypt-but-no-vault-secrets-found)
- [0b. "No node ... can run a pod" on a healthy cluster](#0b-no-node--can-run-a-pod-on-a-healthy-cluster)
- [0c. A message that contradicts itself ("is Ready=True" in a failure)](#0c-a-message-that-contradicts-itself-is-readytrue-in-a-failure)
- [1. Operands sit Pending for ever](#1-operands-sit-pending-for-ever)
- [2. The operator installs, and nothing happens](#2-the-operator-installs-and-nothing-happens)
- [3. The DSCInitialization goes to Error about Service Mesh](#3-the-dscinitialization-goes-to-error-about-service-mesh)
- [4. "unknown field" applying the DataScienceCluster](#4-unknown-field-applying-the-datasciencecluster)
- [5. The model PVC stays Pending](#5-the-model-pvc-stays-pending)
- [6. The fetch Job cannot reach Hugging Face](#6-the-fetch-job-cannot-reach-hugging-face)
- [7. The fetch Job finds no file to download](#7-the-fetch-job-finds-no-file-to-download)
- [8. The predictor crashes with "No .gguf in /mnt/models"](#8-the-predictor-crashes-with-no-gguf-in-mntmodels)
- [9. The predictor is killed with signal 4](#9-the-predictor-is-killed-with-signal-4)
- [10. The predictor never becomes Ready](#10-the-predictor-never-becomes-ready)
- [11. The model answers 200 with no choices](#11-the-model-answers-200-with-no-choices)
- [12. "The model does not exist"](#12-the-model-does-not-exist)
- [13. The chat bot's Route times out](#13-the-chat-bots-route-times-out)
- [14. Answers are very slow](#14-answers-are-very-slow)
- [15. A namespace stays Terminating after cleanup](#15-a-namespace-stays-terminating-after-cleanup)
- [16. InferenceService creates hang after cleanup](#16-inferenceservice-creates-hang-after-cleanup)

---

## 0. "Attempting to decrypt but no vault secrets found"

**Looks like**: every phase, including `preflight`, fails immediately:

```console
$ ansible-playbook -i ../inventory/hosts setup_openshift_ai.yaml --tags preflight -e rhoai_cluster=hub
ERROR! Attempting to decrypt but no vault secrets found
```

**Cause**: `../vault.yaml` exists and is encrypted, and no vault password was
given. The playbook loads it for one variable - `hf_token`, which only a
**gated** Hugging Face model needs - but `vars_files` is resolved before any
task runs, so the file has to be readable even on a phase that reads nothing
from it.

**Fix**, any of three:

```bash
# the password in a file (what build-lab.sh looks for)
ansible-playbook ... --vault-password-file ~/.vault_pass ...

# or type it
ansible-playbook ... --ask-vault-pass ...

# or skip the file entirely - fine unless your model is gated
ansible-playbook ... -e rhoai_vault=vars.yaml ...
```

`build-lab.sh` works it out for itself, and takes `--no-vault` for the third
case:

```bash
./build-lab.sh --cluster hub --only preflight
./build-lab.sh --cluster hub --no-vault --only preflight
```

## 0b. "No node ... can run a pod" on a healthy cluster

**Looks like**: `preflight` fails although `oc get nodes` shows every node
`Ready`:

```
TASK [setup-openshift-ai : Fail if no node is schedulable]
fatal: [localhost]: FAILED! => "No schedulable node on hub."
```

**If you are seeing the message above verbatim, update this directory** - that
wording is from a version with a bug in it, and the fix is in the repository.
The task filtered the node list with a kubectl jsonpath filter:

```
.items[?(@.spec.unschedulable!=true)]
```

which matches **nothing on any cluster**. A schedulable node has no
`spec.unschedulable` key at all - its `spec` is often literally `{}` - and a
jsonpath filter whose left-hand side resolves to no value skips that item
rather than treating it as "not true". So the filter dropped every node it was
meant to keep. `capacity.yml` now reads `oc get nodes -o json` and decides in
Ansible.

**If you are on the current version**, the message names each node and why it
does not count:

```
No node on hub can run a pod (2 node(s) seen): a is cordoned; b is Ready=Unknown.
```

- `cordoned` → `oc adm uncordon <node>`
- `Ready=False` / `Ready=Unknown` → `oc describe node <node>`, and the kubelet
  on it

A node that is cordoned or NotReady is excluded from the capacity total too,
and the summary line says which ones:

```
capacity:  5 of 6 node(s) can run a pod, with 157.5 allocatable cores
           and 219.5 GiB allocatable memory in total (amd64) [...]
           Not counted: worker3.hub.mylab.com.
```

## 0c. A message that contradicts itself ("is Ready=True" in a failure)

**Looks like**: a failure whose own text says everything is fine:

```
fatal: [localhost]: FAILED! => "No node on hub can run a pod (6 node(s) seen):
  master1 is Ready=True; master2 is Ready=True; [...]"
```

**If you see this, update this directory** - it is fixed in the repository.

**The cause is worth knowing**, because it can be written again anywhere in
this repository. On **ansible-core 2.18 and earlier**, a template whose whole
value renders as something that looks like a Python literal is converted to
that type. So this:

```yaml
rhoai_cap_ready: "{{ ...status of the Ready condition... }}"   # renders True
```

is a Python **bool** on 2.18, and a **string** on 2.19 and later. Which means
`rhoai_cap_ready == 'True'` is False on exactly the nodes that are Ready -
while `~` still prints it as `True`, so the message reads like nonsense.

The same conversion turns a rendered JSON object back into a dict, so
indexing the model's reply as a string fails with "dict object has no element
0" on 2.18 and works on 2.21.

Two rules, for anything set by a `set_fact` or a task-level `vars` rather than
read from a registered `.stdout`:

| Instead of | Write |
| --- | --- |
| `myvar == 'True'` | `myvar \| bool` |
| `myvar \| length` on text that might be `42` or `True` | `myvar \| string \| length` |
| `myjson[0] == '{'` to test for JSON | `myjson is mapping` first, then `\| from_json` |

A value read straight off a registered result - `result.stdout`,
`result.stdout_lines` - is a genuine string and needs none of this. So is a
list element: a template rendering a list keeps its elements' types.

`roles/setup-openshift-ai/tasks/capacity.yml` and `tasks/model-probe.yml`
carry comments at the places this bit, and the repository's offline test suite
now runs on both an older and a newer ansible-core so a difference between
them cannot hide again.

## 1. Operands sit Pending for ever

**Looks like**: the CSV reaches `Succeeded`, the DataScienceCluster never
reaches `Ready`, and `oc -n redhat-ods-applications get pods` shows pods
`Pending`.

```bash
oc -n redhat-ods-applications get pods --field-selector=status.phase!=Running
oc -n redhat-ods-applications describe pod "$(
  oc -n redhat-ods-applications get pods \
    --field-selector=status.phase!=Running \
    -o jsonpath='{.items[0].metadata.name}')" | tail -20
```

"Insufficient memory" or "Insufficient cpu" in the events. The cluster has
less free than OpenShift AI's operands want, and nothing will change that by
waiting.

**Fix**: give the cluster more. This is what the preflight's floor - 12 cores
and 40 GiB across all schedulable nodes - is for, and the one case where
lowering it is a mistake rather than a judgement call. If you have measured it
and disagree:

```bash
-e rhoai_min_cpu_cores=8 -e rhoai_min_memory_gib=24
```

The lab's default SNO does not clear the floor. A hub (3 masters + 3 workers)
does.

## 2. The operator installs, and nothing happens

**Looks like**: the CSV is `Succeeded`, `oc -n redhat-ods-operator get pods`
shows the operator running, and there is no dashboard, no Route, no KServe.

**This is correct.** An OpenShift AI operator with no DataScienceCluster runs
nothing but itself - it is waiting for one. Run the next phase:

```bash
ansible-playbook -i ../inventory/hosts setup_openshift_ai.yaml --tags dsc \
  -e rhoai_cluster=hub
```

## 3. The DSCInitialization goes to Error about Service Mesh

**Looks like**:

```bash
oc get dscinitialization default-dsci \
  -o 'jsonpath={range .status.conditions[*]}{.type}={.status} {.message}{"\n"}{end}'
```

...mentions `servicemeshoperator`, an istio control plane, or a missing
ServiceMeshControlPlane CRD.

**Cause**: `spec.serviceMesh.managementState` is `Managed`. Service Mesh is a
dependency of KServe's **Serverless** mode only, and this lab runs
RawDeployment.

**Fix**:

```bash
# default-dsci is what this lab creates; an existing one may be called
# something else, so read it rather than assuming
DSCI=$(oc get dscinitialization -o jsonpath='{.items[0].metadata.name}')
oc patch dscinitialization "$DSCI" --type=merge \
  -p '{"spec":{"serviceMesh":{"managementState":"Removed"}}}'
```

The `dsc` phase does this to an existing DSCInitialization automatically. It
is the one field it changes on a CR it did not create.

## 4. "unknown field" applying the DataScienceCluster

**Looks like**: `oc apply` rejects the CR, naming a field under
`.spec.components`; or the lab fails with

> rhoai_components_managed names <x>, which
> datasciencecluster.opendatahub.io/v3 has no field for.

**Cause**: the component layout was reorganised between the v1, v2 and v3
shapes of this CR, and the name you have is from a different one.

**Fix**: ask the installed CRD what it knows, and set
`rhoai_components_managed` from that list:

```bash
oc get crd datascienceclusters.datasciencecluster.opendatahub.io -o json \
  | python3 -c 'import json,sys; v=[x for x in json.load(sys.stdin)["spec"]["versions"] if x["storage"]][0]; c=v["schema"]["openAPIV3Schema"]["properties"]["spec"]["properties"]["components"]["properties"]; print(v["name"], ":", ", ".join(sorted(c)))'
```

The lab's own error message prints that list too. It never writes a fixed
apiVersion or a fixed field list for exactly this reason.

**By hand, compare the two lists** rather than reading them - this prints the
components a manifest names that the cluster has no field for, which are the
lines to delete:

```bash
oc get crd datascienceclusters.datasciencecluster.opendatahub.io -o json \
  | python3 -c 'import json,sys; v=[x for x in json.load(sys.stdin)["spec"]["versions"] if x["storage"]][0]; print("\n".join(sorted(v["schema"]["openAPIV3Schema"]["properties"]["spec"]["properties"]["components"]["properties"])))' \
  | sort > /tmp/dsc-cluster.txt
sed -n 's/^    \([a-z][a-z0-9]*\):.*/\1/p' "$M/03-dsc.yaml" | sort > /tmp/dsc-manifest.txt
comm -23 /tmp/dsc-manifest.txt /tmp/dsc-cluster.txt
```

How far apart the versions are: a 2.x cluster has `codeflare`,
`datasciencepipelines` and `modelmeshserving`; a 3.x one has none of those and
carries `aigateway`, `aipipelines`, `mcplifecycleoperator`, `mlflowoperator`,
`ogx`, `sparkoperator` and `trainer` instead. `dashboard`, `workbenches` and
`kserve` - the three this lab needs - are in both.
`openshift-ai-steps.md` step 3 has a one-liner that writes the whole
component block from the CRD, so there is nothing to compare.

## 5. The model PVC stays Pending

**Looks like**: `oc -n rhoai-chatbot get pvc` shows `Pending`.

**First, is it actually wrong?** A `WaitForFirstConsumer` class - which
`lvms-vg1` is - leaves a PVC `Pending` until a pod wants it. That is normal
between the `project` and `model` phases.

```bash
oc get storageclass lvms-vg1 -o jsonpath='{.volumeBindingMode}'; echo
```

**If it is `Immediate`, or a pod is already waiting on it**:

```bash
oc -n rhoai-chatbot describe pvc rhoai-models | tail -15
```

| Events say | Fix |
| --- | --- |
| no StorageClass / no default class | the base build adds `lvms-vg1` with `use_lvm_storage: true`; or `-e rhoai_storage_class=<name>` |
| no volume plugin matched | the named class does not exist - `oc get storageclass` |
| insufficient capacity | 20Gi is the default; `-e rhoai_model_pvc_size=10Gi`, or give LVMS a bigger disk |

## 6. The fetch Job cannot reach Hugging Face

**Looks like**:

```bash
oc -n rhoai-chatbot logs -l app=rhoai-model-fetch --tail=50
```

> ERROR: could not list Qwen/Qwen2.5-0.5B-Instruct-GGUF.

with a curl connect error or a 401 above it.

| What curl said | What it is |
| --- | --- |
| `Failed to connect` / timeout | no egress from the cluster to `huggingface.co` |
| `401` | the repository is **gated** - put `hf_token` in `../vault.yaml` |
| `404` | the repository name is wrong |
| TLS / certificate error | a TLS-inspecting proxy whose CA the cluster does not trust |

**Fixes**, in order of preference:

1. Point the Job at a mirror inside the cluster: `-e
   rhoai_hf_endpoint=https://hf-mirror.internal`. It has to answer
   `/api/models/<repo>` and `/<repo>/resolve/main/<file>` the way Hugging Face
   does.
2. Put the weights on the PVC yourself - "Fetch the weights by hand (no
   egress)" in [openshift-ai-steps.md](openshift-ai-steps.md).
3. For the TLS case, add your CA to the cluster-wide additional trust bundle.
   The DSCInitialization's `trustedCABundle` is `Managed`, so it reaches the
   Job's namespace.

This Job is the **only** thing in this lab that reaches the internet.

## 7. The fetch Job finds no file to download

**Looks like**:

> ERROR: no .gguf in <repo> whose name contains 'q4_k_m'.
>   The repository offers:
>     ...

The Job prints every `.gguf` the repository has. Pick a fragment of one:

```bash
-e rhoai_model_file_match=q8_0
```

For the `vllm-cpu` runtime the equivalent is "no top-level .safetensors file",
which means the repository ships its weights in another format - use a
repository that has safetensors, or the `llamacpp` runtime and a GGUF one.

## 8. The predictor crashes with "No .gguf in /mnt/models"

**Looks like**: `CrashLoopBackOff`, and

```bash
oc -n rhoai-chatbot logs -l serving.kserve.io/inferenceservice=qwen2-5-0-5b-instruct -c kserve-container
```

prints the "No .gguf in /mnt/models" message followed by an `ls` of what is
there.

**Cause**: the InferenceService's `storageUri` subpath does not match the
directory the fetch Job wrote into. KServe mounts the PVC at `/mnt/models`
with that path as the subPath, so a mismatch means an empty directory rather
than an error from KServe.

**Fix**: look at what is on the PVC and make them agree.

```bash
oc -n rhoai-chatbot get inferenceservice qwen2-5-0-5b-instruct -o jsonpath='{.spec.predictor.model.storageUri}'; echo
oc -n rhoai-chatbot logs -l app=rhoai-model-fetch --tail=5
```

The playbook derives both from `rhoai_model_dir`
(`<rhoai_model_name>-<rhoai_runtime>`), so they only drift if one phase was
run with a different `-e` than the other.

## 9. The predictor is killed with signal 4

**Looks like**: the pod restarts immediately with no log output, and

```bash
oc -n rhoai-chatbot get pod -l serving.kserve.io/inferenceservice=qwen2-5-0-5b-instruct \
  -o jsonpath='{.items[0].status.containerStatuses[0].lastState}'; echo
```

shows `"signal": 4` / `reason: Error` with a very short runtime.

**Cause**: signal 4 is SIGILL - an illegal instruction. On x86-64 this is
almost always the CPU vLLM build on a CPU without AVX-512.

**Fix**:

```bash
-e rhoai_runtime=llamacpp
```

llama.cpp needs nothing beyond AVX2. If you want vLLM, the nodes' CPUs have to
have `avx512f`:

```bash
oc debug node/"$(oc get nodes -o jsonpath='{.items[0].metadata.name}')" \
  --quiet -- chroot /host grep -m1 '^flags' /proc/cpuinfo \
  | tr ' ' '\n' | grep avx512f
```

A libvirt guest with a masked CPU model hides flags the host has - the lab's
VMs want `host-passthrough` for vLLM to be an option at all. The preflight
phase refuses this combination before anything is installed.

## 10. The predictor never becomes Ready

**Looks like**: the pod is `Running`, `0/1 Ready`, for a long time.

**First, is it just loading?** Reading a few hundred MiB off a lab PVC and
building the KV cache takes minutes. The startup probe allows 15 minutes
before giving up. Watch it:

```bash
oc -n rhoai-chatbot logs -l serving.kserve.io/inferenceservice=qwen2-5-0-5b-instruct \
  -c kserve-container -f
```

llama.cpp prints its load progress and then "server is listening".

**If it is listening and still not Ready**, the probe is not reaching
`/health`:

```bash
oc -n rhoai-chatbot describe pod -l serving.kserve.io/inferenceservice=qwen2-5-0-5b-instruct | grep -A3 Readiness
```

A probe on a path the runtime does not serve is the usual cause in a
hand-written ServingRuntime - KServe generates probes for its own v1/v2
inference protocols, which an OpenAI-API server does not have, so the runtime
has to declare its own. This lab's does.

**If it is OOM-killed** (`reason: OOMKilled`), the model plus its KV cache is
over `rhoai_model_memory_limit`. Either raise it or shrink the context:

```bash
-e rhoai_model_memory_limit=12Gi
-e rhoai_model_context_length=2048
```

## 11. The model answers 200 with no choices

**Looks like**: `/v1/chat/completions` returns `{"choices": []}`, or the lab
fails with "no choices in the model's reply".

**Cause**: the model has no chat template, which a **base** model does not.
`/v1/chat/completions` needs one to turn messages into a prompt.

**Fix**: use an instruction-tuned model - an `-Instruct` or `-Chat` variant.
The lab's default, `Qwen2.5-0.5B-Instruct`, has one.

## 12. "The model does not exist"

**Looks like**: a 404 or 400 from `/v1/chat/completions` naming the model.

**Cause**: the `"model"` in the request is not the name the server serves
under. Ask the server:

```bash
oc -n rhoai-chatbot run probe --rm -i --restart=Never --quiet \
  --image=registry.access.redhat.com/ubi9/ubi-minimal:latest --command -- \
  /bin/sh -c 'curl -sS http://qwen2-5-0-5b-instruct-predictor.rhoai-chatbot.svc.cluster.local:8080/v1/models'
```

The name comes from the runtime's `--alias` (llama.cpp) or
`--served-model-name` (vLLM), both of which the lab sets from
`rhoai_model_name`. They drift only if the ServingRuntime and the
InferenceService were applied with different values.

## 13. The chat bot's Route times out

**Looks like**: the browser hangs, or `curl` to the Route times out, while the
model answers fine from inside the cluster (step 8 of
[openshift-ai-steps.md](openshift-ai-steps.md)).

Work down the path:

```bash
# 1. is the app running at all
oc -n rhoai-chatbot get pod -l app=chatbot
oc -n rhoai-chatbot logs deploy/chatbot --tail=30

# 2. does it answer in-cluster
oc -n rhoai-chatbot run probe --rm -i --restart=Never --quiet \
  --image=registry.access.redhat.com/ubi9/ubi-minimal:latest --command -- \
  /bin/sh -c 'curl -sS http://chatbot.rhoai-chatbot.svc.cluster.local:8080/healthz'

# 3. does the Route's name resolve where you are
getent hosts "$(oc -n rhoai-chatbot get route chatbot -o jsonpath='{.spec.host}')"
```

If 3 is empty, the machine you are on is not using the lab's helper for DNS -
the Route is under the cluster's `*.apps` wildcard, which only the helper
answers for. The repository's README has a section on resolving the lab's
names from the hypervisor.

If the app's log shows it waiting on the model, that is case 14.

## 14. Answers are very slow

A 0.5B model at Q4 on a lab CPU should answer a short prompt in a few seconds
after the first one. If it takes much longer:

| Check | |
| --- | --- |
| How many threads | `--threads` follows `rhoai_model_cpu_limit`. More than the pod's CPU limit makes it *slower*, not faster, because the cgroup throttles it |
| Is it being throttled | `oc -n rhoai-chatbot adm top pod` against `rhoai_model_cpu_limit` |
| Is the prompt long | the context is sent on every turn. The app keeps the last 6 turns (`HISTORY_TURNS`) for this reason |
| Is the model bigger than you think | a 7B at Q4 on a CPU is tens of seconds per answer, not seconds. That is the model, not the lab |

`rhoai_chatbot_max_tokens` caps the answer's length, and on a CPU every token
is wall-clock time - 256 is the default for that reason.

## 15. A namespace stays Terminating after cleanup

**Looks like**: `oc get ns` shows `rhoai-chatbot` or
`redhat-ods-applications` `Terminating` and it never goes.

Something in it has a finalizer whose controller is gone:

```bash
# the two this lab can leave behind
oc get all,inferenceservice,servingruntime -n rhoai-chatbot
oc get ns rhoai-chatbot -o jsonpath='{.status.conditions}' | python3 -m json.tool

oc get all -n redhat-ods-applications
oc get ns redhat-ods-applications -o jsonpath='{.status.conditions}' | python3 -m json.tool
```

The usual cause is deleting the operator before the DataScienceCluster, which
leaves KServe objects with no controller to finalise them. The cleanup phase
deletes the DataScienceCluster first and waits for it, for this reason. To
recover, remove the finalizer from the object the conditions name:

```bash
oc -n rhoai-chatbot patch inferenceservice qwen2-5-0-5b-instruct \
  -p '{"metadata":{"finalizers":null}}' --type=merge
```

## 16. InferenceService creates hang after cleanup

**Looks like**: on a cluster where OpenShift AI was removed, `oc apply` of any
InferenceService hangs and then fails with a webhook timeout.

**Cause**: KServe's admission webhook is still registered, and its backend is
gone. Removing the operator before the DataScienceCluster does this.

**Fix**: find the orphaned webhook configuration and remove it.

```bash
# what is still registered
oc get mutatingwebhookconfigurations,validatingwebhookconfigurations \
  | grep -E 'kserve|opendatahub'
```

Then remove them. `xargs -p` asks before each one, so you can read what is
about to go - these are cluster-scoped, and deleting a webhook that something
else still uses is not a small mistake:

```bash
oc get mutatingwebhookconfigurations,validatingwebhookconfigurations -o name \
  | grep -E 'kserve|opendatahub' \
  | xargs -r -p -n1 oc delete
```

Re-installing the operator and creating a DataScienceCluster also fixes it,
and is the safer move if you were going to anyway.
