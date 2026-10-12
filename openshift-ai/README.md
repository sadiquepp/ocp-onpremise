# Red Hat OpenShift AI on a lab cluster, with no GPU

Installs Red Hat OpenShift AI on one of this repository's clusters, serves a
small language model on **CPU** through KServe, and puts a chat bot in front
of it. The last step asserts that the app generated a prompt of its own and
the model answered it, over the Route - so the lab either works or says why.

Everything lives in this directory: the playbook, one role, its templates,
the chat bot's source, and the documentation. It is **additive** to the rest
of the repository - nothing in the hub, hosted-cluster, OADP, Ceph or UDN
flows reads any of it, and none of those flows change whether or not you ever
run it.

| Document | What it covers |
| --- | --- |
| This file | What it builds, why no GPU shapes it, the phases, the variables, the traps |
| [openshift-ai-steps.md](openshift-ai-steps.md) | **Every step by hand**, with the `oc` commands and the YAML - what the playbook does, done yourself |
| [troubleshooting.md](troubleshooting.md) | Each way it has actually failed, and what the failure looks like |

## Contents

- [What it builds](#what-it-builds)
- [The no-GPU problem, and what this lab does about it](#the-no-gpu-problem-and-what-this-lab-does-about-it)
- [What it costs the cluster](#what-it-costs-the-cluster)
- [Requirements](#requirements)
- [Building it](#building-it)
- [The phases](#the-phases)
- [Using it](#using-it)
- [Variables worth knowing](#variables-worth-knowing)
- [A different model](#a-different-model)
- [Disconnected](#disconnected)
- [Removing it](#removing-it)
- [Notes and constraints](#notes-and-constraints)

## What it builds

```
redhat-ods-operator          the rhods-operator Subscription, AllNamespaces
  |
  +-- DSCInitialization      applications namespace; Service Mesh Removed
  +-- DataScienceCluster     dashboard + workbenches + kserve
        |                    kserve: RawDeployment, Knative serving Removed
        v
redhat-ods-applications      the dashboard, the KServe controller, the webhooks
                             -> https://rhods-dashboard-redhat-ods-applications.apps.<cluster>

rhoai-chatbot                the data science project
  |
  +-- PVC rhoai-models       20Gi on the default StorageClass (lvms-vg1)
  +-- Job rhoai-fetch-<hash> downloads Qwen2.5-0.5B-Instruct into it
  +-- ServingRuntime         llama.cpp's OpenAI-API server, CPU
  +-- InferenceService       the model, as a plain Deployment + Service
  |     qwen2-5-0-5b-instruct-predictor:8080/v1/chat/completions
  +-- Deployment chatbot     one file of standard-library Python
        Service + Route      -> https://chatbot-rhoai-chatbot.apps.<cluster>
```

The chat bot offers a page to type into, and - the part the lab checks -
`GET /api/demo`, where the app writes a prompt itself, sends it, and returns
the answer:

```console
$ curl -sk https://chatbot-rhoai-chatbot.apps.hub.mylab.com/api/demo
{
  "prompt": "In two sentences, what is Red Hat OpenShift AI?",
  "reply": "Red Hat OpenShift AI is a platform for building and serving
            machine-learning models on OpenShift. [...]",
  "error": "",
  "took_seconds": 6.4,
  "model_name": "qwen2-5-0-5b-instruct",
  "model_endpoint": "http://qwen2-5-0-5b-instruct-predictor.rhoai-chatbot.svc.cluster.local:8080"
}
```

## The no-GPU problem, and what this lab does about it

No GPU is the constraint the whole design follows from, and it is worth being
precise about what it rules out, because the obvious route does not work.

**The product's own vLLM runtimes are accelerator builds.** OpenShift AI
ships ServingRuntime templates out of the box, and on a cluster with a GPU you
pick one and are finished. The vLLM ones are CUDA or ROCm images. Red Hat does
ship a **vLLM CPU ServingRuntime for KServe**, but its supported
architectures are IBM Power (ppc64le), IBM Z (s390x) and ARM - not the x86-64
this lab runs on, where a CPU vLLM build also wants AVX-512 and dies on an
illegal instruction without it.

**So the default runtime here is a custom one.** A custom ServingRuntime is
itself a documented OpenShift AI feature, so this is the intended way out
rather than a workaround. It runs
[llama.cpp](https://github.com/ggml-org/llama.cpp)'s server, which:

- is a plain x86-64 CPU build and needs nothing beyond AVX2;
- loads one GGUF file, so the model is a few hundred MiB rather than a few
  GiB;
- serves `/v1/models` and `/v1/chat/completions` - the OpenAI API, which is
  all the chat bot asks of it.

The other runtime is still there: `-e rhoai_runtime=vllm-cpu` uses the
product's CPU vLLM image, taken from the template the installed OpenShift AI
put on the cluster when there is one. Use it on ppc64le, s390x or arm64, or on
an x86-64 host that really has AVX-512. The preflight phase reads the nodes'
CPU flags and tells you which case you are in, and refuses the combination
that cannot work.

**KServe runs in RawDeployment mode.** The alternative, Serverless, needs
OpenShift Serverless (Knative) *and* Service Mesh - three more operators and
their control planes on a lab hub. RawDeployment needs neither, so
`kserve.serving` is `Removed` and the DSCInitialization's `serviceMesh` is
`Removed` too. An InferenceService becomes an ordinary Deployment and Service.
What that costs: no scale-to-zero, no Knative revisions, no canary traffic
split. For one small model that takes a minute to load, none of it is missed.

**The model is small on purpose.** Qwen2.5-0.5B-Instruct, quantised to
Q4_K_M: Apache-2.0, instruction-tuned, public, and about 400 MiB. On a lab
CPU it answers a short prompt in a handful of seconds. It is not a good model.
It is a model that answers, which is what a demo of the serving path needs.

## What it costs the cluster

| | |
| --- | --- |
| Namespaces added | `redhat-ods-operator`, `redhat-ods-applications`, `redhat-ods-monitoring`, `rhoai-chatbot` |
| Operators added | one - `rhods-operator`, AllNamespaces |
| Operators NOT needed | Serverless, Service Mesh, Authorino, Node Feature Discovery, the NVIDIA GPU operator |
| Storage | one 20Gi PVC on the default StorageClass |
| DNS records added | none - both Routes are under the cluster's existing `*.apps` wildcard |
| VMs added | none |
| Anything else in the repository changed | nothing |

The floor the preflight phase enforces, across all schedulable nodes'
allocatable capacity: **12 cores and 40 GiB**. That is not a product
requirement - it is what these objects need with room to roll a deployment.
The lab's default hub (3 masters + 3 workers) clears it comfortably; the
lab's default SNO does not, and the preflight says so rather than letting the
operands sit Pending for ever.

## Requirements

- A cluster from this repository, built and reachable: `hub`, `hub2` or `sno`.
  The playbook reads its kubeconfig from the base `vars.yaml`
  (`hub_install_folder` and friends) and refuses to run if the file is not
  there, rather than falling back to the ambient `KUBECONFIG`.
- cluster-admin on it. The preflight phase checks with `oc auth can-i` before
  anything is installed.
- A **default StorageClass**. The base build gives the cluster `lvms-vg1` when
  `use_lvm_storage: true`; name another with `-e rhoai_storage_class=<name>`.
- Egress to `huggingface.co` **from the cluster**, for the one Job that
  downloads the weights. Nothing else in the lab reaches the internet. See
  [Disconnected](#disconnected) if there is none.
- The OpenShift AI operator in the cluster's catalog. On a connected cluster
  that is `redhat-operators`; on a disconnected one it has to have been
  mirrored.

## Building it

Run it from inside this directory - `ansible.cfg` is read from the working
directory, not from the playbook's:

```bash
cd openshift-ai
./build-lab.sh --cluster hub
```

That runs every phase in order. By hand, phase by phase:

```bash
cd openshift-ai
PB="ansible-playbook -i ../inventory/hosts setup_openshift_ai.yaml \
    --vault-password-file ~/.vault_pass -e rhoai_cluster=hub"

$PB --tags preflight     # changes nothing - run this first
$PB --tags operator
$PB --tags dsc
$PB --tags project
$PB --tags model
$PB --tags chatbot
$PB --tags verify
```

**The vault flag is not optional**, even though no phase here reads a secret
unless your model is gated. `../vault.yaml` is loaded for one variable,
`hf_token`, and `vars_files` is resolved before any task runs - so an
encrypted vault.yaml with no password fails the run before it starts:

```
ERROR! Attempting to decrypt but no vault secrets found
```

Use `--ask-vault-pass` instead to type it, or skip the file altogether when
you have no password to hand and your model is not gated:

```bash
# note: no vault flag at all in this one, so it does not use $PB
ansible-playbook -i ../inventory/hosts setup_openshift_ai.yaml --tags preflight \
  -e rhoai_cluster=hub -e rhoai_vault=vars.yaml
```

`build-lab.sh` works this out for itself: `--vault-password-file
~/.vault_pass` when that file exists, `--ask-vault-pass` when it does not,
neither when there is no `../vault.yaml` at all, and `--no-vault` to skip it.

Each phase runs **only** when named: a bare run does nothing but check the
kubeconfig, deliberately. Every phase is idempotent, so an interrupted run is
resumed by running it again - `./build-lab.sh --cluster hub --from model`.

To do the same thing with `oc` and your own YAML, so you can see each object
as it goes on, follow [openshift-ai-steps.md](openshift-ai-steps.md).

## The phases

| Tag | What it does | Roughly |
| --- | --- | --- |
| `preflight` | Reads only. Is the operator in the catalog and on what channel; is anything installed already; is there a default StorageClass; has the cluster the CPU and memory; do the nodes' CPU flags suit the chosen runtime. | seconds |
| `operator` | Namespace, AllNamespaces OperatorGroup, Subscription. Waits for the CSV to reach `Succeeded` and the two CRDs to be Established. Installs the operator and nothing else - an OpenShift AI operator with no DataScienceCluster runs no dashboard and no KServe. | 5-10 min |
| `dsc` | A DSCInitialization and a DataScienceCluster. Non-destructive: an existing CR of either kind is **patched** with the fields this lab needs, never replaced, so a cluster already running OpenShift AI with other components on keeps them. Reports the dashboard's Route. | 5-15 min |
| `project` | The `rhoai-chatbot` namespace, labelled so the dashboard lists it as a data science project, and the model PVC. | seconds |
| `model` | A Job that downloads the weights onto the PVC, a ServingRuntime, an InferenceService. Waits for the predictor to roll out and then asks the model a question through its Service. | 5-20 min |
| `chatbot` | The app's code as a ConfigMap, then a Deployment, Service and Route. No image is built. | 1 min |
| `verify` | Asserts only, from the bottom up: CSV Succeeded, DSC Ready with kserve Managed, InferenceService Ready, the model completes a chat request over the Service, and the app's `/api/demo` comes back answered over the Route. | 1-5 min |
| `cleanup` | Removes it all again. See [Removing it](#removing-it). | 2 min |

## Using it

Open the chat bot's Route in a browser, from a machine that uses the lab's
helper for DNS. Type something; or press **Demo prompt**, which sends the
prompt in `rhoai_chatbot_demo_prompt`.

The first answer after the model pod starts is the slow one - the weights are
being read off the PVC and the KV cache built. After that, a short prompt on
a lab CPU comes back in a few seconds.

From the command line:

```bash
# the app's own prompt, answered - the same thing --tags verify asserts on
curl -sk https://chatbot-rhoai-chatbot.apps.hub.mylab.com/api/demo

# is the app's view of the model healthy
curl -sk https://chatbot-rhoai-chatbot.apps.hub.mylab.com/api/health

# your own question
curl -sk -X POST -H 'Content-Type: application/json' \
  -d '{"message":"Name three OpenShift AI components."}' \
  https://chatbot-rhoai-chatbot.apps.hub.mylab.com/api/chat
```

The model's own OpenAI API is **in-cluster only** by default - the chat bot
dials the Service, and an unauthenticated LLM endpoint on the lab's wildcard
is not something to publish without being asked. To publish it anyway:

```bash
ansible-playbook -i ../inventory/hosts setup_openshift_ai.yaml --tags model \
  --vault-password-file ~/.vault_pass \
  -e rhoai_cluster=hub -e rhoai_model_expose_route=true
```

In the dashboard, the project appears under **Data Science Projects** and the
model under **Model Serving**, with the custom runtime listed under
**Settings -> Serving runtimes**.

## Variables worth knowing

Everything this lab owns is in [vars.yaml](vars.yaml), prefixed `rhoai_`, and
documented there. The ones most often changed:

| Variable | Default | What it is |
| --- | --- | --- |
| `rhoai_cluster` | *(required, via `-e`)* | `hub`, `hub2` or `sno` |
| `rhoai_runtime` | `llamacpp` | or `vllm-cpu` - see [above](#the-no-gpu-problem-and-what-this-lab-does-about-it) |
| `rhoai_model_gguf_repo` | `Qwen/Qwen2.5-0.5B-Instruct-GGUF` | the GGUF repository, for `llamacpp` |
| `rhoai_model_file_match` | `q4_k_m` | which quantisation in it to take |
| `rhoai_model_hf_repo` | `Qwen/Qwen2.5-0.5B-Instruct` | the safetensors repository, for `vllm-cpu` |
| `rhoai_model_name` | `qwen2-5-0-5b-instruct` | the name the model answers under |
| `rhoai_components_managed` | `dashboard`, `workbenches`, `kserve` | which DataScienceCluster components are `Managed` |
| `rhoai_storage_class` | `auto` | `auto` = the cluster's default class |
| `rhoai_model_memory_limit` | `8Gi` | the model server's memory ceiling |
| `rhoai_model_context_length` | `4096` | the context window the server starts with |
| `rhoai_model_expose_route` | `false` | publish the model's own OpenAI API |
| `rhoai_min_cpu_cores` / `rhoai_min_memory_gib` | `12` / `40` | the preflight's floor |
| `hf_token` *(in `../vault.yaml`)* | unset | only a **gated** model needs one |

## A different model

Any instruction-tuned model with a chat template works, as long as it fits in
`rhoai_model_memory_limit` on a CPU. For `llamacpp`, point it at a GGUF
repository and say which quantisation:

```bash
ansible-playbook -i ../inventory/hosts setup_openshift_ai.yaml --tags model \
  --vault-password-file ~/.vault_pass \
  -e rhoai_cluster=hub \
  -e rhoai_model_gguf_repo=TinyLlama/TinyLlama-1.1B-Chat-v1.0-GGUF \
  -e rhoai_model_file_match=q4_k_m \
  -e rhoai_model_name=tinyllama-1-1b-chat
```

The exact file name is never written down: the fetch Job lists the repository
through the Hugging Face API and takes the first `.gguf` whose name contains
`rhoai_model_file_match`, because quantisation file names differ per
repository and change when a repository is re-uploaded. If nothing matches,
the Job's log prints every `.gguf` the repository has.

Two things to keep in mind:

- **A base model is not a chat model.** Without a chat template,
  `/v1/chat/completions` returns no choices and the verify phase says so.
  Pick an `-Instruct` or `-Chat` variant.
- **Each model gets its own directory on the PVC**
  (`rhoai_model_dir`, derived from the name and the runtime), and the fetch
  Job is named after a hash of what it downloads - so switching models adds a
  download rather than overwriting one, and switching back is free.

## Disconnected

Two things reach outside the cluster, and both have to be dealt with.

**The operator.** It has to be in the mirrored catalog. Add `rhods-operator`
to the `ImageSetConfiguration` that `setup_mirror_registry.yaml` runs
`oc-mirror` with, alongside the operators already there, and re-mirror. The
lab resolves the CatalogSource name the same way the base roles do, and
follows `acm_catalog_source` from the root `vars.yaml` when it is set, so the
two can never point at different catalogs. If the operator is not in the
catalog, the preflight phase fails with the catalog's own package list.

**The weights.** The fetch Job is the one thing in this lab that reaches the
internet. Either:

- point it at a mirror of the Hugging Face API inside the cluster -
  `-e rhoai_hf_endpoint=https://hf-mirror.internal` - which needs that mirror
  to answer `/api/models/<repo>` and `/<repo>/resolve/main/<file>` the way
  Hugging Face does; or
- put the files on the PVC yourself and skip the Job. The "Fetch the weights
  by hand" section of [openshift-ai-steps.md](openshift-ai-steps.md) has the
  `oc rsync` for it.

If your egress goes through a TLS-inspecting proxy, the DSCInitialization's
`trustedCABundle` is `Managed`, so the cluster's own trust bundle is injected
into the namespaces - add your CA's PEM to the cluster-wide additional trust
bundle and the Job will use it.

## Removing it

```bash
./build-lab.sh --cluster hub --only cleanup
```

or

```bash
ansible-playbook -i ../inventory/hosts setup_openshift_ai.yaml --tags cleanup \
  --vault-password-file ~/.vault_pass -e rhoai_cluster=hub
```

That removes the app, the model, the project namespace (the PVC with it), the
DataScienceCluster, the DSCInitialization, the Subscription, the CSV and the
three OpenShift AI namespaces. Two switches:

```bash
-e rhoai_cleanup_keep_models=true   # keep the namespace and the PVC, so a
                                    # rebuild does not re-download the weights
-e rhoai_cleanup_operator=false     # leave OpenShift AI installed; remove
                                    # only this lab's own objects
```

**The CRDs are left in place**, as OLM leaves them. Deleting a CRD deletes
every CR of that kind across the cluster, and these are shared with anything
else ever installed from the same operator. `oc get crd | grep -E
'opendatahub|kserve'` lists them if you want them gone by hand.

## Notes and constraints

- **One cluster per run.** Every phase takes `-e rhoai_cluster=<name>` and
  there is no cross-cluster phase. The guard is deliberate: without it `oc`
  would talk to the ambient `KUBECONFIG`, which on the lab's hypervisor is
  usually the hub - so a SNO run would install a cluster-wide operator on the
  hub and look like it had worked.
- **One replica of the model, and no autoscaling.** The PVC is
  ReadWriteOnce, because the lab's default StorageClass is block storage on
  one node and offers nothing else; a second predictor replica on another node
  could not mount it. `minReplicas` and `maxReplicas` are both 1.
- **The CRs are rendered against the installed CRD, not against a version
  written down here.** The DataScienceCluster and DSCInitialization APIs have
  gone v1 -> v2 -> v3, and the component names under `.spec.components` moved
  with them. The `dsc` phase reads the storage version and the component list
  out of the CRD on the cluster and only ever writes fields that version's
  schema has, so a newer operator gives a readable error instead of an
  "unknown field" or a silently pruned one.
- **An existing OpenShift AI install is patched, not replaced.** Only a CR
  this lab creates from nothing gets the full "everything not in
  `rhoai_components_managed` is Removed" layout.
- **No image is built, anywhere.** The chat bot is one file of
  standard-library Python mounted into `ubi9/python-311`; the fetch Job is a
  POSIX shell script in `ubi-minimal`. Nothing to build, nothing to push, no
  wheel to mirror.
- **The model is not fast and is not clever.** 0.5B parameters, Q4, on a CPU.
  Judge the serving path, not the answers.
- **Nothing here is secured for anything but a lab.** The chat bot's Route is
  unauthenticated, and so is the model's if you expose it. Do not put either
  on a network you do not own.
