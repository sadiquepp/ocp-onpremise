# OpenShift AI on CPU, step by step - and how to do each step by hand

Every step the playbook takes, in order, with **what it does**, **the command
to do it yourself**, and **how to tell it worked**. Each step shows the
playbook's one-liner first, then the `oc` commands and the YAML it comes down
to - so you can run the lab, or build it by hand and understand it, or read
one step to work out why it failed.

The YAML below is what the playbook renders. Where a value is decided at run
time - the operator's channel, the CRD's API version, the StorageClass - the
step says where it comes from and how to read it off the cluster, because
copying a stale one is the most common way a hand-built install goes wrong.

Everything is run from inside `openshift-ai/`:

```bash
cd openshift-ai
export KUBECONFIG=/var/lib/libvirt/images/hub_install/auth/kubeconfig
PB="ansible-playbook -i ../inventory/hosts setup_openshift_ai.yaml \
    --vault-password-file ~/.vault_pass -e rhoai_cluster=hub"
```

`$KUBECONFIG` matters for the by-hand route: every `oc` below goes wherever it
points, and on this lab's hypervisor that is usually the hub whether you meant
it or not. The playbook sets it per run from `rhoai_clusters` and refuses to
start if the file is missing; by hand, you are the guard.

The **vault flag** matters for the playbook route. `../vault.yaml` is loaded
for one variable, `hf_token`, which only a gated Hugging Face model needs -
but `vars_files` is resolved before any task runs, so an encrypted vault.yaml
with no password fails every phase before it starts, with
`ERROR! Attempting to decrypt but no vault secrets found`. Use
`--ask-vault-pass` to type it instead, or `-e rhoai_vault=vars.yaml` to skip
the file when your model is not gated. None of the `oc` commands below care.

## Contents

- [0. Before anything: is this cluster up to it](#0-before-anything-is-this-cluster-up-to-it)
- [1. The operator](#1-the-operator)
- [2. DSCInitialization](#2-dscinitialization)
- [3. DataScienceCluster](#3-datasciencecluster)
- [4. The project and the model's storage](#4-the-project-and-the-models-storage)
- [5. Fetch the weights](#5-fetch-the-weights)
- [5b. Fetch the weights by hand (no egress)](#5b-fetch-the-weights-by-hand-no-egress)
- [6. The ServingRuntime](#6-the-servingruntime)
- [7. The InferenceService](#7-the-inferenceservice)
- [8. Ask the model directly](#8-ask-the-model-directly)
- [9. The chat bot](#9-the-chat-bot)
- [10. Check the whole thing](#10-check-the-whole-thing)
- [11. Take it down again](#11-take-it-down-again)
- [Doing it all in the dashboard instead](#doing-it-all-in-the-dashboard-instead)

---

## 0. Before anything: is this cluster up to it

```bash
$PB --tags preflight
```

Reads only. Five questions, and it is worth knowing what each one is for
because each is a way this install fails late and confusingly.

### Is the cluster reachable, and are you cluster-admin

```bash
oc get clusterversion version -o jsonpath='{.status.desired.version}'; echo
oc auth can-i create subscriptions.operators.coreos.com --all-namespaces
oc auth can-i create operatorgroups.operators.coreos.com --all-namespaces
oc auth can-i create customresourcedefinitions.apiextensions.k8s.io
```

All four want `yes`. Installing an operator writes a cluster-scoped
OperatorGroup and the DataScienceCluster is cluster-scoped too, so a
kubeconfig that is only a project admin gets through the reads and then fails
half way.

### Is OpenShift AI in the catalog, and on what channel

```bash
# every package the catalog carries, with its default channel
oc get packagemanifests -n openshift-marketplace -l catalog=redhat-operators \
  -o 'jsonpath={range .items[*]}{.metadata.name}{" "}{.status.defaultChannel}{"\n"}{end}' \
  | grep rhods

# the channels that package offers, and the CSV each one currently points at
oc get packagemanifests -n openshift-marketplace -l catalog=redhat-operators \
  -o 'jsonpath={range .items[?(@.metadata.name=="rhods-operator")].status.channels[*]}{.name}{" "}{.currentCSV}{"\n"}{end}'
```

Take the channel from here rather than typing `stable`: the default channel
moves between releases, and a Subscription on a channel the package does not
offer sits there with no InstallPlan and no obvious complaint. On a
disconnected cluster, `-l catalog=` is the mirrored CatalogSource's name
instead - `oc get catalogsource -n openshift-marketplace` shows it.

### Is anything installed already

```bash
oc get subscriptions.operators.coreos.com -n redhat-ods-operator
oc get dscinitialization,datasciencecluster
```

If either CR exists, somebody has installed OpenShift AI here. The playbook
patches rather than replaces in that case; by hand, read step 2 and 3 before
you apply anything.

### Is there a default StorageClass

```bash
oc get storageclass
oc get storageclass -o 'jsonpath={range .items[?(@.metadata.annotations.storageclass\.kubernetes\.io/is-default-class=="true")]}{.metadata.name}{"\n"}{end}'
```

Expect `lvms-vg1` on this lab. With no default class and no class named, the
model's PVC sits `Pending` for ever and the model pod never starts.

### Has the cluster the room, and the right CPU

```bash
# allocatable CPU and memory per schedulable node
oc get nodes -o 'jsonpath={range .items[?(@.spec.unschedulable!=true)]}{.metadata.name}{"\t"}{.status.allocatable.cpu}{"\t"}{.status.allocatable.memory}{"\t"}{.status.nodeInfo.architecture}{"\n"}{end}'

# and the CPU's instruction sets, which decide which runtime can work
oc debug node/"$(oc get nodes -o jsonpath='{.items[0].metadata.name}')" \
  --quiet -- chroot /host grep -m1 '^flags' /proc/cpuinfo | tr ' ' '\n' | grep -E '^(avx2|avx512f)$'
```

The lab's floor is 12 cores and 40 GiB across the cluster. For the CPU flags:

| What you see | What it means |
| --- | --- |
| `avx2` and `avx512f` | either runtime works |
| `avx2` only | the default `llamacpp` runtime. `vllm-cpu` on x86-64 would die on an illegal instruction, and the preflight refuses it |
| neither | a very old or heavily masked CPU. A libvirt guest with a masked CPU model hides flags the host has - `host-passthrough` shows them |

---

## 1. The operator

```bash
$PB --tags operator
```

Three objects. The channel comes from step 0; `redhat-operators` is the
catalog on a connected cluster.

```yaml
apiVersion: v1
kind: Namespace
metadata:
  name: redhat-ods-operator
  labels:
    openshift.io/cluster-monitoring: "true"
---
# No targetNamespaces - an AllNamespaces OperatorGroup, which is the only
# install mode this operator offers. It creates its applications namespace
# itself and reconciles DataScienceClusters, which are cluster-scoped.
apiVersion: operators.coreos.com/v1
kind: OperatorGroup
metadata:
  name: redhat-ods-operator-operatorgroup
  namespace: redhat-ods-operator
spec: {}
---
apiVersion: operators.coreos.com/v1alpha1
kind: Subscription
metadata:
  name: rhods-operator
  namespace: redhat-ods-operator
spec:
  name: rhods-operator
  channel: stable            # <- from step 0, not from here
  source: redhat-operators   # <- the CatalogSource, from step 0
  sourceNamespace: openshift-marketplace
  installPlanApproval: Automatic
```

```bash
oc apply -f -   # paste the above, or:
oc apply -f /var/lib/libvirt/images/openshift-ai/hub/01-operator.yaml   # what the playbook rendered
```

### Did it work

```bash
# the Subscription names the CSV it installed
oc -n redhat-ods-operator get subscriptions.operators.coreos.com rhods-operator \
  -o jsonpath='{.status.installedCSV}'; echo

# and that CSV reaches Succeeded - this is the one to wait on
oc -n redhat-ods-operator get csv -w
```

`Succeeded`, and then:

```bash
oc wait --for=condition=Established --timeout=300s \
  crd/dscinitializations.dscinitialization.opendatahub.io \
  crd/datascienceclusters.datasciencecluster.opendatahub.io
oc -n redhat-ods-operator get pods
```

### If it does not

```bash
# the Subscription's conditions say why there is no InstallPlan
oc -n redhat-ods-operator get subscriptions.operators.coreos.com rhods-operator \
  -o 'jsonpath={range .status.conditions[*]}{.type}={.status} {.message}{"\n"}{end}'
# on a disconnected cluster, the bundle unpack job is usually the one failing
oc -n openshift-marketplace get pods
```

An unpack job that cannot pull means the `ImageContentSourcePolicy` does not
cover the operator's images. **An operator alone runs nothing** - no
dashboard, no KServe - until there is a DataScienceCluster, which is the next
two steps.

---

## 2. DSCInitialization

```bash
$PB --tags dsc    # this does steps 2 and 3 together
```

The cluster-wide settings: which namespace the operands live in, monitoring,
the trust bundle, and - the one that matters here - Service Mesh **off**.

### First, read the API version off the cluster

Do not copy an `apiVersion` from a blog. These CRs have gone `v1` -> `v2` ->
`v3`, and the field names moved with them:

```bash
# the storage version the installed CRD serves
oc get crd dscinitializations.dscinitialization.opendatahub.io \
  -o 'jsonpath={range .spec.versions[?(@.storage==true)]}{.name}{"\n"}{end}'

# and the fields that version actually has
oc get crd dscinitializations.dscinitialization.opendatahub.io -o json \
  | python3 -c 'import json,sys; v=[x for x in json.load(sys.stdin)["spec"]["versions"] if x["storage"]][0]; print(v["name"], sorted(v["schema"]["openAPIV3Schema"]["properties"]["spec"]["properties"]))'
```

That second command is exactly what the playbook's `crd-shape.yml` does, and
why it never writes a field the installed operator does not know.

### Then apply it

```yaml
apiVersion: dscinitialization.opendatahub.io/v2   # <- from the command above
kind: DSCInitialization
metadata:
  name: default-dsci
spec:
  applicationsNamespace: redhat-ods-applications
  monitoring:
    managementState: Managed
    namespace: redhat-ods-monitoring
  # Removed on purpose. Service Mesh is a dependency of KServe's SERVERLESS
  # mode only, and this lab runs KServe in RawDeployment. Left Managed, the
  # operator demands the Service Mesh operator and the DSCI goes to Error.
  serviceMesh:
    managementState: Removed
  trustedCABundle:
    managementState: Managed
    customCABundle: ""
```

**If one already exists**, do not apply a second. Patch the one field:

```bash
oc get dscinitialization    # find its name
oc patch dscinitialization <name> --type=merge \
  -p '{"spec":{"serviceMesh":{"managementState":"Removed"}}}'
```

`applicationsNamespace` in particular is where the operands already live;
moving it orphans them.

### Did it work

```bash
oc get dscinitialization <name> \
  -o 'jsonpath={.status.phase}{"\n"}{range .status.conditions[*]}{.type}={.status} {.message}{"\n"}{end}'
```

Want `Ready`.

---

## 3. DataScienceCluster

Which components run. Read the component names off the CRD the same way - they
are not the same list in every release:

```bash
oc get crd datascienceclusters.datasciencecluster.opendatahub.io -o json \
  | python3 -c 'import json,sys; v=[x for x in json.load(sys.stdin)["spec"]["versions"] if x["storage"]][0]; c=v["schema"]["openAPIV3Schema"]["properties"]["spec"]["properties"]["components"]["properties"]; print("version:", v["name"]); print("components:", ", ".join(sorted(c))); print("kserve fields:", ", ".join(sorted(c["kserve"].get("properties", {}))))'
```

Then switch on the three this lab needs and switch everything else off - each
of the others is more pods than a lab hub has room for, and none is on the
path from "install the operator" to "a chat bot answers":

```yaml
apiVersion: datasciencecluster.opendatahub.io/v2   # <- from the command above
kind: DataScienceCluster
metadata:
  name: default-dsc
spec:
  components:
    dashboard:
      managementState: Managed
    workbenches:
      managementState: Managed
    kserve:
      managementState: Managed
      # Plain Deployments and Services for every InferenceService. The
      # alternative, Serverless, needs the OpenShift Serverless and Service
      # Mesh operators as well.
      defaultDeploymentMode: RawDeployment
      # An ordinary ClusterIP Service for the predictor, which is what the
      # chat bot dials by name. Some versions default this to Headless.
      rawDeploymentServiceConfig: Headed
      # The Knative half of KServe, off because RawDeployment does not use it.
      serving:
        managementState: Removed
    codeflare:            {managementState: Removed}
    datasciencepipelines: {managementState: Removed}
    kueue:                {managementState: Removed}
    modelmeshserving:     {managementState: Removed}
    modelregistry:        {managementState: Removed}
    ray:                  {managementState: Removed}
    trainingoperator:     {managementState: Removed}
    trustyai:             {managementState: Removed}
```

Drop any component from that list that your CRD does not have, and add any it
has that is missing here (`feastoperator` and `llamastackoperator` appear in
newer releases). **If a DataScienceCluster already exists**, patch instead -
leave somebody else's components alone:

```bash
oc patch datasciencecluster <name> --type=merge -p '{"spec":{"components":{
  "dashboard":{"managementState":"Managed"},
  "workbenches":{"managementState":"Managed"},
  "kserve":{"managementState":"Managed","defaultDeploymentMode":"RawDeployment",
            "rawDeploymentServiceConfig":"Headed",
            "serving":{"managementState":"Removed"}}}}}'
```

### Did it work

```bash
oc get datasciencecluster <name> -o jsonpath='{.status.phase}'; echo
oc -n redhat-ods-applications get pods
oc wait --for=condition=Established --timeout=600s \
  crd/inferenceservices.serving.kserve.io crd/servingruntimes.serving.kserve.io

# the dashboard
oc -n redhat-ods-applications get route
```

Pods stuck `Pending` here mean the cluster is out of room - step 0's capacity
check is what that was for. `oc -n redhat-ods-applications describe pod
<name> | tail -20` names the resource it could not get.

---

## 4. The project and the model's storage

```bash
$PB --tags project
```

```yaml
apiVersion: v1
kind: Namespace
metadata:
  name: rhoai-chatbot
  labels:
    # What makes this an OpenShift AI "data science project" rather than an
    # ordinary namespace: the dashboard lists namespaces with this label, so
    # the model shows up under Model Serving. Nothing on the serving path
    # depends on it.
    opendatahub.io/dashboard: "true"
    modelmesh-enabled: "false"
  annotations:
    openshift.io/display-name: "CPU chat bot"
---
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: rhoai-models
  namespace: rhoai-chatbot
spec:
  # ReadWriteOnce, because the lab's default class is block storage on one
  # node and offers nothing else. Enough here - the fetch Job finishes before
  # the model server starts - but it does mean one predictor replica only.
  accessModes: [ReadWriteOnce]
  resources:
    requests:
      storage: 20Gi
  storageClassName: lvms-vg1     # <- from step 0
```

### Did it work

```bash
oc -n rhoai-chatbot get pvc
```

`Pending` is **correct** here if the class is `WaitForFirstConsumer`, which
`lvms-vg1` is: the volume is created when the first pod wants it, which is the
next step. Check which you have with:

```bash
oc get storageclass lvms-vg1 -o jsonpath='{.volumeBindingMode}'; echo
```

---

## 5. Fetch the weights

```bash
$PB --tags model    # this does steps 5, 6 and 7 together
```

The model is downloaded onto the PVC once and served from it - the shape the
product documents, and the only one that needs no S3 endpoint in the lab.

The playbook does it with a Job whose script is
[`roles/setup-openshift-ai/files/fetch-model.sh`](roles/setup-openshift-ai/files/fetch-model.sh).
By hand, the quickest equivalent is one pod:

```bash
# which file to take: list the repository and pick a quantisation
curl -s https://huggingface.co/api/models/Qwen/Qwen2.5-0.5B-Instruct-GGUF \
  | grep -o '"rfilename":"[^"]*"' | sed 's/.*:"//;s/"$//' | grep '\.gguf$'
```

```bash
oc -n rhoai-chatbot run fetch --restart=Never --image=registry.access.redhat.com/ubi9/ubi-minimal:latest \
  --overrides='{"spec":{"containers":[{"name":"fetch","image":"registry.access.redhat.com/ubi9/ubi-minimal:latest",
    "command":["/bin/sh","-c","set -eu; d=/models/qwen2-5-0-5b-instruct-llamacpp; mkdir -p $d; cd $d; curl -fLO https://huggingface.co/Qwen/Qwen2.5-0.5B-Instruct-GGUF/resolve/main/qwen2.5-0.5b-instruct-q4_k_m.gguf; chmod -R a+rX /models; ls -l $d"],
    "volumeMounts":[{"name":"m","mountPath":"/models"}]}],
    "volumes":[{"name":"m","persistentVolumeClaim":{"claimName":"rhoai-models"}}]}}'

oc -n rhoai-chatbot logs -f pod/fetch
```

The directory name matters: it is the `storageUri` subpath in step 7. The
playbook derives it as `<rhoai_model_name>-<rhoai_runtime>` so two runtimes
never share a half-populated directory.

### Did it work

```bash
oc -n rhoai-chatbot run ls --rm -i --restart=Never --image=registry.access.redhat.com/ubi9/ubi-minimal:latest \
  --overrides='{"spec":{"containers":[{"name":"ls","image":"registry.access.redhat.com/ubi9/ubi-minimal:latest",
    "command":["ls","-lR","/models"],"volumeMounts":[{"name":"m","mountPath":"/models"}]}],
    "volumes":[{"name":"m","persistentVolumeClaim":{"claimName":"rhoai-models"}}]}}'
# and the PVC is Bound now that a pod has claimed it
oc -n rhoai-chatbot get pvc rhoai-models
```

A 401 from Hugging Face means the repository is gated: put `hf_token` in
`../vault.yaml` (the playbook turns it into a Secret) or pass
`-H "Authorization: Bearer <token>"` by hand.

---

## 5b. Fetch the weights by hand (no egress)

On a cluster with no route to Hugging Face, download the file somewhere that
has one and push it in. `oc rsync` needs a running pod with the PVC mounted:

```bash
# 1. on a machine with egress
curl -fLO https://huggingface.co/Qwen/Qwen2.5-0.5B-Instruct-GGUF/resolve/main/qwen2.5-0.5b-instruct-q4_k_m.gguf

# 2. a pod that just holds the PVC open
oc -n rhoai-chatbot run holder --restart=Never --image=registry.access.redhat.com/ubi9/ubi-minimal:latest \
  --overrides='{"spec":{"containers":[{"name":"holder","image":"registry.access.redhat.com/ubi9/ubi-minimal:latest",
    "command":["sleep","3600"],"volumeMounts":[{"name":"m","mountPath":"/models"}]}],
    "volumes":[{"name":"m","persistentVolumeClaim":{"claimName":"rhoai-models"}}]}}'
oc -n rhoai-chatbot wait --for=condition=Ready pod/holder --timeout=300s

# 3. copy it in, under the directory step 7 will ask for
oc -n rhoai-chatbot exec holder -- mkdir -p /models/qwen2-5-0-5b-instruct-llamacpp
oc -n rhoai-chatbot cp qwen2.5-0.5b-instruct-q4_k_m.gguf \
  holder:/models/qwen2-5-0-5b-instruct-llamacpp/
oc -n rhoai-chatbot exec holder -- sh -c 'chmod -R a+rX /models; ls -l /models/qwen2-5-0-5b-instruct-llamacpp'

# 4. and let go of the PVC - it is ReadWriteOnce, so the model server cannot
#    start on another node while this pod holds it
oc -n rhoai-chatbot delete pod holder
```

Then skip the playbook's fetch by running steps 6 and 7 by hand, or let it run
and find the weights already there - `fetch-model.sh` writes a
`.fetch-complete` marker and exits early, so re-running it is a no-op only if
that marker is there. Writing it yourself is enough:

```bash
oc -n rhoai-chatbot exec holder -- sh -c \
  'echo "placed by hand" > /models/qwen2-5-0-5b-instruct-llamacpp/.fetch-complete'
```

---

## 6. The ServingRuntime

*How* a model is served. This is the custom one - the reason is in
[README.md](README.md#the-no-gpu-problem-and-what-this-lab-does-about-it):
the product's vLLM runtimes are accelerator builds, and its CPU vLLM runtime
is for ppc64le/s390x/ARM rather than x86-64.

```yaml
apiVersion: serving.kserve.io/v1alpha1
kind: ServingRuntime
metadata:
  name: llamacpp-cpu-runtime
  namespace: rhoai-chatbot
  labels:
    opendatahub.io/dashboard: "true"
  annotations:
    openshift.io/display-name: "llama.cpp CPU runtime (OpenAI API, GGUF)"
    # No accelerator - the whole point. The dashboard uses this to suggest
    # runtimes, and an empty list keeps it from offering this one for a GPU.
    opendatahub.io/recommended-accelerators: '[]'
spec:
  multiModel: false
  supportedModelFormats:
    - name: gguf
      # Off: the InferenceService names this runtime explicitly. A runtime
      # that volunteers for a format can be picked for somebody else's model
      # in the same namespace.
      autoSelect: false
  protocolVersions: [v2]
  containers:
    - name: kserve-container          # the name KServe looks for
      image: ghcr.io/ggml-org/llama.cpp:server
      command: ["/bin/sh", "-c"]
      args:
        - |
          set -eu
          gguf="$(ls -1 /mnt/models/*.gguf 2>/dev/null | head -1 || true)"
          if [ -z "$gguf" ]; then
            echo "No .gguf in /mnt/models."; ls -la /mnt/models || true; exit 1
          fi
          bin=""
          for p in "$(command -v llama-server 2>/dev/null || true)" \
                   /app/llama-server /llama-server /usr/local/bin/llama-server \
                   /usr/bin/llama-server /app/server; do
            if [ -n "$p" ] && [ -x "$p" ]; then bin="$p"; break; fi
          done
          [ -n "$bin" ] || { echo "no llama-server in this image"; exit 1; }
          exec "$bin" --host 0.0.0.0 --port 8080 --model "$gguf" \
            --alias qwen2-5-0-5b-instruct --ctx-size 4096 --threads 4 --metrics
      ports:
        - containerPort: 8080
          protocol: TCP
      # KServe only generates probes for a container that has none, and the
      # ones it generates are for its own v1/v2 inference protocols, which an
      # OpenAI-API server does not serve. The startup probe is the one that
      # matters: loading a model off a lab PVC takes minutes, and without it
      # the liveness probe kills the container before it finishes.
      startupProbe:
        httpGet: {path: /health, port: 8080}
        periodSeconds: 10
        failureThreshold: 90
      readinessProbe:
        httpGet: {path: /health, port: 8080}
        periodSeconds: 10
      livenessProbe:
        httpGet: {path: /health, port: 8080}
        periodSeconds: 30
      env:
        - {name: HOME, value: /tmp}
        - {name: HF_HOME, value: /tmp/hf}
        - {name: XDG_CACHE_HOME, value: /tmp/cache}
        - {name: HF_HUB_OFFLINE, value: "1"}
      volumeMounts:
        - {name: scratch, mountPath: /tmp}
      securityContext:
        allowPrivilegeEscalation: false
        runAsNonRoot: true
        capabilities: {drop: [ALL]}
        seccompProfile: {type: RuntimeDefault}
  volumes:
    - name: scratch
      emptyDir: {sizeLimit: 2Gi}
```

Two notes if you are writing your own:

- **`--alias`** (vLLM: `--served-model-name`) is the name the model answers
  under in `/v1/models`, and it has to match what the client sends as
  `"model"`. KServe also understands the placeholder `{{.Name}}` here and
  replaces it with the InferenceService's name - which is what the product's
  own vLLM templates use. This lab writes the name out instead, so the served
  name does not depend on a substitution whose behaviour varies between KServe
  versions.
- **The binary is looked for, not assumed.** Its path differs between
  llama.cpp image builds, and a wrong path shows up as a `CrashLoopBackOff`
  with "no such file or directory" and nothing else to go on.

For the vLLM CPU variant instead:

```yaml
      command: [python, -m, vllm.entrypoints.openai.api_server]
      args:
        - --host=0.0.0.0
        - --port=8080
        - --model=/mnt/models
        - --served-model-name=qwen2-5-0-5b-instruct
        - --max-model-len=4096
        - --device=cpu
        - --dtype=bfloat16
```

...and it needs a `/dev/shm` of its own (`emptyDir` with `medium: Memory`) and
a full safetensors snapshot on the PVC rather than one GGUF. Find the image
the installed OpenShift AI shipped:

```bash
oc -n redhat-ods-applications get templates \
  -o 'jsonpath={range .items[*]}{.metadata.name}{" "}{.objects[0].spec.containers[0].image}{"\n"}{end}' \
  | grep -i vllm
```

### Did it work

```bash
oc -n rhoai-chatbot get servingruntime
```

A ServingRuntime on its own starts nothing. It is a description; the next step
is what uses it.

---

## 7. The InferenceService

"Serve **these** weights with **that** runtime."

```yaml
apiVersion: serving.kserve.io/v1beta1
kind: InferenceService
metadata:
  name: qwen2-5-0-5b-instruct
  namespace: rhoai-chatbot
  labels:
    opendatahub.io/dashboard: "true"
  annotations:
    # Said explicitly, not inherited: an InferenceService that does not say so
    # would change mode if somebody flipped the cluster default, and in
    # Serverless mode this one would never start - the lab has no Knative.
    serving.kserve.io/deploymentMode: RawDeployment
    # No external Route. The chat bot dials the Service from inside the
    # cluster; 'exposed' publishes an unauthenticated LLM endpoint on the
    # lab's wildcard instead.
    networking.kserve.io/visibility: cluster-local
spec:
  predictor:
    minReplicas: 1
    maxReplicas: 1
    model:
      modelFormat:
        name: gguf              # matches supportedModelFormats in step 6
      runtime: llamacpp-cpu-runtime
      # pvc://<claim>/<path> - KServe mounts the claim at /mnt/models with
      # this path as the subPath. No storage-initializer runs, nothing is
      # copied, and the mount is read-only: the weights are read where the
      # fetch job left them.
      storageUri: pvc://rhoai-models/qwen2-5-0-5b-instruct-llamacpp
      resources:
        requests: {cpu: "2", memory: 4Gi}
        limits:   {cpu: "4", memory: 8Gi}
```

### Did it work

In RawDeployment mode KServe makes a Deployment and a Service named
`<name>-predictor`:

```bash
oc -n rhoai-chatbot get deployment,svc,pod -l serving.kserve.io/inferenceservice=qwen2-5-0-5b-instruct
oc -n rhoai-chatbot rollout status deployment/qwen2-5-0-5b-instruct-predictor --timeout=900s
oc -n rhoai-chatbot get inferenceservice qwen2-5-0-5b-instruct
```

Watch the rollout **before** the InferenceService's own condition: a pod that
cannot pull its image or cannot find its weights says so there, while the
InferenceService just stays not-Ready with nothing to read.

```bash
oc -n rhoai-chatbot logs -l serving.kserve.io/inferenceservice=qwen2-5-0-5b-instruct \
  -c kserve-container --tail=50 -f
```

| What you see | What it is |
| --- | --- |
| `Pending`, "Insufficient memory" | the cluster has less free than `requests` - lower them, or free something |
| `CrashLoopBackOff`, "No .gguf in /mnt/models" | the `storageUri` subpath does not match where step 5 put the file |
| killed with signal 4 (illegal instruction) | the CPU vLLM build on a CPU without AVX-512 - use the `llamacpp` runtime |
| up for a long time, never Ready | normal for the first minutes: it is reading the weights. The startup probe allows 15 minutes |

---

## 8. Ask the model directly

Before putting an app in front of it, prove the model itself answers. From
inside the cluster, which is where the chat bot will be:

```bash
oc -n rhoai-chatbot run probe --rm -i --restart=Never --quiet \
  --image=registry.access.redhat.com/ubi9/ubi-minimal:latest --command -- \
  /bin/sh -c 'curl -sS http://qwen2-5-0-5b-instruct-predictor.rhoai-chatbot.svc.cluster.local:8080/v1/models'
```

```bash
oc -n rhoai-chatbot run probe --rm -i --restart=Never --quiet \
  --image=registry.access.redhat.com/ubi9/ubi-minimal:latest --command -- \
  /bin/sh -c 'curl -sS -X POST -H "Content-Type: application/json" -d @- \
    http://qwen2-5-0-5b-instruct-predictor.rhoai-chatbot.svc.cluster.local:8080/v1/chat/completions' <<'JSON'
{"model":"qwen2-5-0-5b-instruct",
 "messages":[{"role":"user","content":"In two sentences, what is Red Hat OpenShift AI?"}],
 "max_tokens":256,"temperature":0.2,"stream":false}
JSON
```

| What comes back | What it means |
| --- | --- |
| `choices[0].message.content` with text in it | the whole serving path works |
| `404 page not found` | the runtime is not an OpenAI-API server, or the port is wrong |
| 200 with `"choices": []` | the model has no chat template - a **base** model rather than an instruct/chat one |
| `"model does not exist"` | the `"model"` you sent is not what `--alias` / `--served-model-name` set |
| nothing, connection refused | the predictor Service has no ready endpoints - back to step 7 |

This is what the playbook's `model-probe.yml` does, and it is the check worth
keeping: it exercises the Service, the runtime, the weights on the PVC and the
chat template inside the model, rather than just that a pod is `Running`.

---

## 9. The chat bot

```bash
$PB --tags chatbot
```

The app is a single standard-library Python file -
[`roles/setup-openshift-ai/files/chatbot.py`](roles/setup-openshift-ai/files/chatbot.py).
No image is built: it goes into a ConfigMap and is mounted into the UBI python
image, so there is nothing to push to a registry and no wheel to mirror.

```bash
# the code
oc -n rhoai-chatbot create configmap rhoai-chatbot-code \
  --from-file=roles/setup-openshift-ai/files/chatbot.py

# the Deployment, Service and Route the playbook renders
oc apply -f /var/lib/libvirt/images/openshift-ai/hub/30-chatbot.yaml
```

The parts of that Deployment worth knowing:

```yaml
          env:
            # The predictor Service from step 7, in-cluster and by name.
            - name: MODEL_ENDPOINT
              value: http://qwen2-5-0-5b-instruct-predictor.rhoai-chatbot.svc.cluster.local:8080
            - name: MODEL_NAME
              value: qwen2-5-0-5b-instruct
            - name: SYSTEM_PROMPT
              value: "You are a concise assistant running on a CPU-only OpenShift AI cluster."
            - name: DEMO_PROMPT
              value: "In two sentences, what is Red Hat OpenShift AI?"
          # Both probes hit /healthz, which answers as soon as the process is
          # up and says nothing about the model. A probe that went through to
          # the model would restart this pod for the whole of the model's cold
          # start - the one time you want to read /api/health and see why.
          readinessProbe:
            httpGet: {path: /healthz, port: http}
```

The pod template also carries a checksum of `chatbot.py`, because a ConfigMap
change is not a pod change: without it, edited code sits in the ConfigMap
while the old process keeps running.

### Did it work

```bash
oc -n rhoai-chatbot rollout status deployment/chatbot --timeout=300s
oc -n rhoai-chatbot get route chatbot -o jsonpath='{.spec.host}'; echo
curl -sk "https://$(oc -n rhoai-chatbot get route chatbot -o jsonpath='{.spec.host}')/api/health"
```

The app's four ways in:

| Path | What it does |
| --- | --- |
| `GET /` | the chat page |
| `POST /api/chat` | `{"message": "...", "history": [...]}` -> the reply |
| `GET /api/demo` | **the app writes its own prompt**, sends it, returns the answer - no browser needed, which is why the verify phase asserts on this one |
| `GET /api/health` | is the model reachable, and what does it say it serves |
| `GET /healthz` | 200 as soon as the process is up, for the pod's probes |

---

## 10. Check the whole thing

```bash
$PB --tags verify
```

Asserts only, from the bottom up, so the first failure is the lowest broken
link rather than a symptom of it. By hand, the same five:

```bash
# 1. the operator
oc -n redhat-ods-operator get csv

# 2. the DataScienceCluster, and kserve's mode
oc get datasciencecluster -o 'jsonpath={range .items[*]}{.metadata.name}{" "}{.status.phase}{" kserve="}{.spec.components.kserve.managementState}{" mode="}{.spec.components.kserve.defaultDeploymentMode}{"\n"}{end}'

# 3. the model's objects and its pod
oc -n rhoai-chatbot get servingruntime,inferenceservice,pod

# 4. the model answers - step 8
# 5. the app's own prompt, answered, over the Route
curl -sk "https://$(oc -n rhoai-chatbot get route chatbot -o jsonpath='{.spec.host}')/api/demo"
```

Step 5 is the one the whole lab is for: a prompt the app generated, answered
by a model on a CPU, over the Route a browser would take.

---

## 11. Take it down again

```bash
$PB --tags cleanup
# keep the weights for next time:
$PB --tags cleanup -e rhoai_cleanup_keep_models=true
# leave OpenShift AI installed, remove only this lab's objects:
$PB --tags cleanup -e rhoai_cleanup_operator=false
```

By hand, in this order - the order matters:

```bash
# 1. the app and the model
oc -n rhoai-chatbot delete deployment/chatbot service/chatbot route/chatbot \
  configmap/rhoai-chatbot-code --ignore-not-found
oc -n rhoai-chatbot delete inferenceservice/qwen2-5-0-5b-instruct \
  servingruntime/llamacpp-cpu-runtime --ignore-not-found
oc -n rhoai-chatbot delete job -l app=rhoai-model-fetch --ignore-not-found

# 2. the namespace, the PVC with it
oc delete namespace rhoai-chatbot

# 3. the DataScienceCluster FIRST, and wait for it. It owns the dashboard, the
#    KServe controller and the webhooks; removing the operator while they
#    still exist leaves them with no controller to clean them up - and a
#    KServe webhook with no backend makes every later InferenceService create
#    hang.
oc delete datasciencecluster --all --timeout=600s
oc delete dscinitialization --all --timeout=300s

# 4. then the operator
oc -n redhat-ods-operator delete subscriptions.operators.coreos.com --all
oc -n redhat-ods-operator delete csv --all
oc delete namespace redhat-ods-applications redhat-ods-monitoring redhat-ods-operator
```

**Leave the CRDs.** Deleting a CRD deletes every CR of that kind across the
cluster, and these are shared with anything else ever installed from the same
operator. `oc get crd | grep -E 'opendatahub|kserve'` lists them if you
really want them gone.

A namespace that stays `Terminating` has an object with a finalizer in it:

```bash
oc get all,inferenceservice,servingruntime -n <ns>
```

---

## Doing it all in the dashboard instead

Steps 4 to 7 have a UI, and it is worth seeing once:

1. **Data Science Projects -> Create project** - the same thing as the
   labelled namespace in step 4.
2. **Settings -> Serving runtimes -> Add serving runtime** - paste the
   ServingRuntime from step 6. Choose *single-model serving platform*, and
   REST as the API protocol.
3. In the project, **Models -> Deploy model**: pick that runtime, choose
   *existing cluster storage* and the `rhoai-models` PVC, and give the folder
   path from step 5 (`qwen2-5-0-5b-instruct-llamacpp`). The model's name here
   becomes the InferenceService's name, so it has to match the `--alias` in
   the runtime.
4. Leave **"Make deployed models available through an external route"**
   unticked - that is `networking.kserve.io/visibility: exposed`, and the chat
   bot does not need it.

What the dashboard will **not** do is step 1 to 3 (it is how you get a
dashboard in the first place) or step 9. For those, the playbook or the `oc`
above.
