# Hosted Control Planes (Agent platform)

Hosted clusters whose control planes run as pods on a hub (a
[standard cluster](../standard-cluster/) with [ACM/MCE](../acm/) on it) and
whose workers are libvirt VMs on the bare-metal host, discovered as **Agents**
through an InfraEnv and joined to a NodePool. Connected clusters
(`hcp-cluster1/2/3`) and disconnected ones (`hcp-cluster1/2/3-d`, workers with
no route off the lab network) are rendered from the same role and can be up at
the same time.

Backing one up and restoring it onto another hub is in [`dr/`](dr/).

## Contents

- [What lives here](#what-lives-here)
- [Before you start](#before-you-start)
- [1. Create the InfraEnv](#1-create-the-infraenv)
- [2. Create the worker VMs](#2-create-the-worker-vms)
  - [BareMetalHosts instead of plain Agents](#baremetalhosts-instead-of-plain-agents)
- [3. Create the hosted cluster](#3-create-the-hosted-cluster)
- [Create a Hosted Cluster (Disconnected Deployment)](#create-a-hosted-cluster-disconnected-deployment)
- [Deploy a sample application to the hosted cluster](#deploy-a-sample-application-to-the-hosted-cluster)
- [Disconnected Hosted Cluster](#disconnected-hosted-cluster)
  - [How the workers are cut off](#how-the-workers-are-cut-off)
  - [Image signature policy on the discovery host](#image-signature-policy-on-the-discovery-host)
  - [Building it](#building-it)
- [Other hosted-cluster labs](#other-hosted-cluster-labs)
- [Cleanup](#cleanup)

## What lives here

| File | Purpose |
| --- | --- |
| `setup_bminfra.yaml` | Create the `bminfra` namespace, pull secret, InfraEnv and CAPI RBAC on a hub, and download its discovery ISO |
| `setup_hosted_cluster_vm.yaml` | Create a hosted cluster's worker VMs and boot them off the InfraEnv's discovery ISO (or, with `hosted_cluster_worker_provisioning=bmh`, as BareMetalHosts); builds the disconnected set when `disconnected_install: true` |
| `setup_hosted_cluster2_vm.yaml` | The worker VMs for `hcp-cluster2`, PXE-booted from the helper instead of the ISO |
| `create_hosted_cluster.yaml` | Render the HostedCluster/NodePool/ManagedCluster/KlusterletAddonConfig bundle for each entry in `hosted_clusters` (or `hosted_clusters_disconnected`) |
| `setup_sushy_emulator.yaml` / `cleanup-sushy-emulator.yaml` | Run / remove sushy-emulator, the Redfish BMCs the BareMetalHost path uses |
| `sushy-emulator-manual.md` | The same sushy-emulator setup as commands to run by hand |
| `hello-openshift.yaml` | A sample workload for the hosted cluster |
| `dr/` | Backup and restore of a hosted cluster onto a second hub - see [dr/README.md](dr/README.md) |
| `ansible.cfg` | Mirrors the root `ansible.cfg` and adds `../roles` to `roles_path` |

The roles stay in the shared [`roles/`](../roles/) directory
(`setup-bminfra`, `create-hosted-cluster`, `setup-sushy-emulator`), and the
playbooks share `vars.yaml`, `vault.yaml` and `inventory/hosts` with the rest of
the repository. **Run them from inside this directory**:

```bash
cd hcp
ansible-playbook -i ../inventory/hosts setup_bminfra.yaml --ask-vault-pass
```

Every playbook here picks its hub with `target_hub` (`hub` - cluster1 - by
default, `hub2` for cluster2, `compact` for the compact cluster), except that
`disconnected_install: true` always wins and selects the disconnected cluster1
(`hubd`), the only hub the `-d` clusters exist on.

## Before you start

- A hub: cluster1 built with [`standard-cluster/setup_cluster1.yaml`](../standard-cluster/)
  and ACM installed on it with [`acm/setup_acm.yaml`](../acm/).
- The hub has a default StorageClass and the AgentServiceConfig is applied -
  see [acm/README.md, Apply the AgentServiceConfig](../acm/README.md#apply-the-agentserviceconfig).
  `setup_bminfra.yaml` refuses to run without it, since an InfraEnv without
  assisted-service never produces a discovery ISO.
- **Disconnected only:** `mirror-registry/setup_mirror_registry.yaml` has run
  - that is what leaves the registry's CA at
  `/etc/pki/ca-trust/source/anchors/mirror-registry-rootCA.pem` and logs the
  bare-metal host's podman into the registry, which the disconnected renderings
  read.

## 1. Create the InfraEnv

- **Option 1**: `ACM -> Fleet Management -> Host Inventory -> Create Infrastructure Environment -> Create Environment`, fill in the form and create the environment.
- **Option 2**: let `setup_bminfra.yaml` render and apply it:

```bash
ansible-playbook -i ../inventory/hosts setup_bminfra.yaml --ask-vault-pass
```

The same command covers both modes: with `disconnected_install: true` it
targets the disconnected hub's kubeconfig instead of hub1's (the flag wins
over `target_hub`), and renders the InfraEnv differently. `-e target_hub=hub2`
creates it on cluster2, `-e target_hub=compact` on the compact cluster
([compact-cluster-hcp.md](../compact-cluster/compact-cluster-hcp.md)).

On a disconnected run the rendered pull-secret carries only the
mirror registry's credentials (read from `mirror_registry_authfile`, the
authfile `mirror-registry/setup_mirror_registry.yaml` logs podman into), not vault.yaml's
`pull_secret`. The discovery agent resolves its image through the mirror
mappings in the ConfigMap `roles/setup-hub-acm` publishes, so the pull is
against the mirror registry and is authenticated against that host - with the
Red Hat pull secret instead it fails `unauthorized` on the mirror and then
`no route to host` on `registry.redhat.io`. Same reduction the disconnected
hub's own `install-config.yaml` does, and for the same reason: nothing in a
disconnected lab should hold a working credential back to the real registries.

A note on the pull-secret, if you hit this on a connected hub:

```
The Secret "pullsecret-bminfra" is invalid: data[.dockerconfigjson]: Invalid value:
"<secret contents redacted>": invalid character '\'' looking for beginning of object key string
```

Those are Python's quotes, from `str(dict)` - and vault.yaml is not the
problem. Ansible converts a template's result back into an object when the
whole thing looks like one, so a properly quoted JSON *string* in vault.yaml
came back out of the role's `bminfra_pull_secret: "{{ pull_secret }}"` hop as a
dict, which `b64encode` then encoded as its repr. The default now ends in a
`string`/`to_json` filter (`STRING_TYPE_FILTERS`, which suppresses that
conversion), so either spelling of `pull_secret` - quoted string or YAML
mapping - renders as JSON, and the role asserts the document has an `auths`
object before rendering. Nothing to change in vault.yaml. Roles that use
`pull_secret` directly never hit this; it takes the extra variable hop.

The role applies the four manifests itself, in order (namespace, pull-secret,
InfraEnv, capi-provider RBAC). The rendered files are left in place for
inspection:

```bash
ls ../roles/setup-bminfra/templates/.rendered-*.yaml
oc get infraenv -n bminfra
```

The discovery ISO is downloaded in the same run, after the apply, so there is
no race against its expiry. If it does expire, download it from `Add Hosts` in
the ACM web UI, or let the playbook that creates the worker VMs fetch it as its
first step - it lands in `download_dir` from `vars.yaml`.

## 2. Create the worker VMs

- **Option 1**: create them by hand in virt-manager with the MAC addresses from
  `vars.yaml`, booted from the discovery ISO (download it from `Add Hosts`).
- **Option 2**: let the playbook download the ISO and create them:

```bash
ansible-playbook -i ../inventory/hosts setup_hosted_cluster_vm.yaml --ask-vault-pass
```

That builds `hosted_cluster_workers` (`c1_worker1..3`, `.41-.43`) against the
hub `target_hub` selects. With `disconnected_install: true` it builds
`hosted_cluster_disconnected_workers` (`c1-d-worker1..3`, `.44-.46`) against the
disconnected hub instead, cutting their egress first - see
[Disconnected Hosted Cluster](#disconnected-hosted-cluster).
`-e target_hub=compact` builds the compact cluster's two workers.

`setup_hosted_cluster2_vm.yaml` builds `hcp-cluster2`'s workers the other way,
PXE-booted off the helper with the InfraEnv's boot artifacts rather than the
ISO. The helper holds **one** set of PXE artifacts, so run it against the hub
whose InfraEnv those workers should join:

```bash
ansible-playbook -i ../inventory/hosts setup_hosted_cluster2_vm.yaml --ask-vault-pass
```

Once the VMs boot they register as Agents in the `bminfra` namespace. Approve
them in the ACM/MCE web UI (`Host Inventory -> bminfra -> Hosts`).

The workers go to `shut off` while joining the NodePool (the agent reboots them
after writing RHCOS). Start them again to finish the join:

```bash
virsh start c1_worker1
virsh start c1_worker2
```

### BareMetalHosts instead of plain Agents

For the hub's connected workers only, the hub can manage them as
**BareMetalHosts** with a Redfish BMC each, from sushy-emulator on this host:

```bash
ansible-playbook -i ../inventory/hosts setup_hosted_cluster_vm.yaml --ask-vault-pass \
  -e hosted_cluster_worker_provisioning=bmh
```

The VMs are defined powered off, `roles/setup-sushy-emulator` gives each one a
BMC, and a BareMetalHost plus BMC secret per VM is created in the agent
namespace. The hub then powers them on, boots the discovery ISO as virtual
media and approves the Agents itself - nothing to approve in the UI. Needs
`sushy_emulator_password` in `vault.yaml`; see
`hosted_cluster_worker_provisioning` in `vars.yaml`.

`setup_sushy_emulator.yaml` re-applies just the emulator (after rebuilding a
worker VM, changing the password, or a host reboot that left it stopped), and
`cleanup-sushy-emulator.yaml` removes it - remove the hosted cluster or its
BareMetalHosts first. The same steps by hand:
[sushy-emulator-manual.md](sushy-emulator-manual.md). How the BareMetalHosts
behave across a DR restore: [dr/BMH-DR-test.md](dr/BMH-DR-test.md).

## 3. Create the hosted cluster

Either from the ACM web UI using the discovered Agents, or rendered by
`create_hosted_cluster.yaml`.

List the hosted clusters you want in `hosted_clusters` in `vars.yaml`. One
template covers every connected cluster (a second one covers disconnected
clusters - see below); an entry is a bare name, or a dict with `name` plus any
per-cluster override. Concurrent hosted clusters on the same hub can share
`cluster_cidr`/`service_cidr` - each is its own OVN-Kubernetes cluster and those
CIDRs never leave its data plane.

```yaml
hosted_clusters:
  - hcp-cluster1
  - hcp-cluster2
  - name: hcp-cluster3
    nodepool_replicas: 3
```

Every name listed must also have an entry in `hosted_cluster_metallb_pools` -
that is what gives the cluster its own single-address MetalLB pool and keeps
its kube-apiserver VIP on the address `api`/`api-int` resolve to. See
[acm/README.md, MetalLB Address Pools](../acm/README.md#metallb-address-pools-for-hosted-clusters).

```bash
ansible-playbook -i ../inventory/hosts create_hosted_cluster.yaml --ask-vault-pass
```

- Add `-e hcp_cluster_name=hcp-cluster2` to render just one of them.
- With `disconnected_install: true` the same command renders the
  **disconnected** hosted clusters (`hosted_clusters_disconnected`) instead,
  against the disconnected hub - see the next section.
- `platform: kubevirt` entries are skipped - those are built by
  [`kubevirt/`](../kubevirt/).
- Review and apply the rendered files (one `.rendered-<cluster>.yaml` per
  entry) to the hub:

```bash
oc apply -f ../roles/create-hosted-cluster/templates/.rendered-hcp-cluster1.yaml
oc get hostedcluster,nodepool -n hcp-cluster1
```

The release payload is addressed **by version**, not by a hard-coded digest:
`hosted_cluster_release_version` defaults to `<ocp_major_version>.<ocp_minor_version>`,
so the hosted clusters run the same release the rest of the lab is built from
and there is one number to bump. Both `HostedCluster.spec.release.image` and
`NodePool.spec.release.image` come from it.

## Create a Hosted Cluster (Disconnected Deployment)

`create-hosted-cluster` renders differently when it is run disconnected, the
same way `setup-hub-acm` does. It reads the same `disconnected_install` from
`vars.yaml`, so it is the same command as the connected flow - and connected
runs render byte-for-byte what they always did.

```bash
ansible-playbook -i ../inventory/hosts create_hosted_cluster.yaml --ask-vault-pass
```

The run also picks the hub for you: a disconnected render goes to the
disconnected hub's kubeconfig without needing `-e target_hub=hubd`, since that
is the only hub the `-d` clusters exist on. (`-e target_hub=hub2` still wins -
that is the DR cutover, which moves both kinds of cluster onto the DR hub.)

What the disconnected run adds to each bundle:

- A `<cluster>-user-ca-bundle` ConfigMap in the hosted cluster's own namespace,
  holding the mirror registry CA under `ca-bundle.crt`. The HostedCluster
  references it twice: `spec.configuration.proxy.trustedCA`, which puts the CA
  in the hosted cluster's cluster-wide proxy trust bundle, and
  `spec.additionalTrustBundle`, which HyperShift carries into the nodes' own
  trust store through the ignition it generates. Both fields look for
  `ca-bundle.crt`, so one ConfigMap serves both. The mirror registry is
  self-signed, so without this every pull from it fails on an unknown
  authority.
- `spec.imageContentSources` on the HostedCluster, from
  `hosted_cluster_image_content_sources`. A hosted cluster is its own cluster
  with its own `registries.conf` - the hub's `ImageDigestMirrorSet` /
  `ImageTagMirrorSet` only redirect pulls made *by the hub*, so without this
  list the hosted cluster resolves every pullspec to its public registry.
  `registry.redhat.io/multicluster-engine` is the mapping that matters most on
  Agent platform: the agent and assisted-installer images the nodes run come
  from there, so omitting it gives you a control plane that comes up while the
  NodePool never finishes joining. The role asserts it is present
  (`hosted_cluster_required_image_content_sources`) rather than letting that
  fail silently hours later.
- A pull secret reduced to the mirror registry's own credentials, read from
  `mirror_registry_authfile`. Same reasoning as
  `roles/setup-hub-cluster-disconnected`: vault's `pull_secret` also carries
  live quay.io / registry.redhat.io credentials, and shipping those would give
  the nodes a working route back to the real registries, so a pull the mirror
  is missing could still succeed and the cluster would be disconnected only by
  accident. Set `hosted_cluster_disconnected_pull_secret: false` to render
  vault's secret unchanged while debugging.
- `spec.configuration.operatorhub.disableAllDefaultSources: true`, since the
  default catalogs resolve against `registry.redhat.io` and otherwise only show
  up as failing CatalogSource pods. Set
  `hosted_cluster_disable_default_catalog_sources: false` if you are mirroring
  them.
- An APIServer `loadBalancer.hostname` that has to resolve to this cluster's
  MetalLB address **on the hub it is running on**. Pool *names* are identical on
  every hub; the *addresses* are not (connected clusters: `hub` 160-162, `hub2`
  190-192; `-d` clusters: `hub2` 193-195, `hubd` 167-169),
  so a cluster rendered disconnected publishes a name that must resolve to its
  `hubd` address, not its `hub` one. The role resolves the hub per cluster
  (`hosted_cluster_metallb_hub`, defaulting to `target_hub`, but `hubd` for any
  cluster rendered disconnected), fails if that cluster's
  `hosted_cluster_metallb_pools[...].ip` has no entry for that hub, and prints
  the name/address pairing plus the `dig` command to check it. Re-render DNS
  after any change - `setup_bm_host.yaml --tags dns` - and the two stay in step:
  `roles/setup-dns` resolves the hub per cluster, publishing the `hubd` address
  for a `-d` cluster and `target_hub`'s for a connected one, so no override is
  needed. The default (`api.<cluster-name>.<base_domain>`) is correct by
  construction under the repo convention that a hosted cluster's DNS zone is
  named after the cluster - a cluster named `hcp-cluster1-d` gets
  `api.hcp-cluster1-d.mylab.com`. If you render a cluster disconnected under a
  name whose zone points at another hub - e.g. plain `hcp-cluster1` - set
  `api_hostname` on its `hosted_clusters` entry, or
  `hosted_cluster_api_hostname` in `vars.yaml`.

The connected and disconnected renderings are **two separate templates**
(`templates/hosted-cluster.yaml.j2` and
`templates/hosted-cluster-disconnected.yaml.j2`), the same way `setup-hub-acm`
keeps its disconnected `AgentServiceConfig` separate. `tasks/main.yml` picks
between them per cluster. They share the NodePool / ManagedCluster /
KlusterletAddonConfig tail, so a change to one usually belongs in both.

Connected renders `quay.io/openshift-release-dev/ocp-release:<version>-x86_64`.
Disconnected renders the **mirror registry's own copy** at the same version,
`<registry>:<port>/<mirror_release_repository>:<version>-x86_64` - the same
pullspec `setup-hub-cluster-disconnected` already extracts `openshift-install`
from, so it is known to exist there.

That is a preference, not a requirement. The release image is resolved **on the
hub** - HyperShift pulls it there to extract the payload and pin every component
image by digest - and the hub carries oc-mirror's `ImageTagMirrorSet`, which
redirects `quay.io/openshift-release-dev/ocp-release` by tag
(`acm_disconnected_registry_mirrors` lists it with `digest_only: false`, and
the disconnected cluster build applies ITMS as a day-2 resource). So the
canonical quay.io tag resolves through the mirror perfectly well.

`spec.imageContentSources` is a different layer: it is the hosted cluster's own
`registries.conf`, carries `ImageDigestMirrorSet` (mirror-by-digest-only)
semantics, and covers the component images - which are pulled by digest anyway,
so digest-only is exactly right there.

Naming the mirror directly buys one thing: the payload pull stops depending on
the hub's ITMS being present and correct. It costs portability, since the
pullspec names this lab's registry, so a HostedCluster restored onto a connected
hub needs its release image swapped. Set
`hosted_cluster_release_image_disconnected: "{{ hosted_cluster_release_image }}"`
to keep the canonical name and lean on the hub's ITMS instead.

Connected and disconnected clusters are **two separate lists of separate
clusters**, which is what lets a connected and a disconnected hosted cluster be
up on this lab at the same time:

```yaml
## connected - these names are reserved for connected runs
hosted_clusters:
  - hcp-cluster1
  - hcp-cluster2
  - hcp-cluster3

## disconnected - rendered instead of the above when disconnected_install: true
hosted_clusters_disconnected:
  - hcp-cluster1-d
  - hcp-cluster2-d
  - hcp-cluster3-d
```

A `-d` cluster is a cluster in its own right: its own namespace, its own
MetalLB pool (`hubd` .167-.169), its own DNS zone and its own worker VMs. It is
not `hcp-cluster1` rendered differently, and the role refuses to run if a name
appears in both lists, since the two would collide on all three. Note the
connected clusters carry **no `hubd` address** - they never run on the
disconnected hub, and a stray entry would make `setup-hub-acm` create pools
there for clusters that will never ask for them.

`setup_bm_host.yaml` renders the helper for **both** kinds in one
unconditional pass - there is no `disconnected_install` switch anywhere in
`setup-bm-host` / `setup-dns` / `setup-lb` / `setup-tftp`, so the helper never
needs rebuilding to move between connected and disconnected. That pass covers
the `-d` clusters:

- a forward zone per hosted cluster, connected and disconnected, derived from
  the two cluster lists (`<cluster-name>.<base_domain>`). One shared template
  covers every zone, which is why adding the `-d` clusters needed no new
  template.
- a matching `zone` stanza in `named.conf` and PTRs in the reverse zone, driven
  from the same lists so a zone file and its stanza can never disagree.
- a `<forwarder>` in the libvirt `default` network per zone, so those names
  resolve from the VMs on `virbr0` and not just from the helper.

Which hub each zone's `api`/`api-int` points at is resolved **per cluster**:
connected clusters follow `target_hub`, disconnected ones use `hubd`, and both
move to `hub2` under `-e target_hub=hub2` for a DR cutover. So a plain
`--tags dns` run is right for both kinds at once, and no `target_hub` override
is needed to publish a `-d` cluster on the disconnected hub.

`*.apps` is wired for the `-d` clusters too. `setup-lb` binds one ingress VIP
per hosted cluster as a secondary address on the helper and renders an haproxy
frontend/backend pair per cluster on :80 and :443, all derived from
`hosted_cluster_node_keys` rather than hand-listed:

| cluster | `*.apps` VIP | worker backends |
|---|---|---|
| hcp-cluster1 / 2 / 3 | .149 / .159 / .158 | .41-.43 / .51-.53 / .54-.56 |
| hcp-cluster1-d | .148 | .44-.46 |
| hcp-cluster2-d | .138 | .28, .29, .37 |
| hcp-cluster3-d | .199 | .96-.98 |

Worker, PTR, DHCP and `*.apps` records are all rendered only for `ip_list` keys
that exist, so a cluster whose VMs have not been built yet still gets a valid
zone carrying the `api`/`api-int` records its HostedCluster publishes, and an
haproxy section appears only once it has both a VIP and at least one worker -
never a frontend with no backend.

Review and apply the rendered bundles exactly as in the connected flow -
`../roles/create-hosted-cluster/templates/.rendered-<cluster>.yaml`. Keep
`hosted_cluster_image_content_sources` in step with
`acm_disconnected_registry_mirrors` (setup-hub-acm) and with what oc-mirror
actually published; after the hub is up,
`oc get imagedigestmirrorset,imagetagmirrorset -o yaml` is the source of truth.

## Deploy a sample application to the hosted cluster

Used by the DR walkthrough to show the application stays reachable while the
hub is down, and after the restore.

Get the kubeconfig for the hosted cluster:

```bash
oc get secret hcp-cluster1-admin-kubeconfig -n hcp-cluster1 -o jsonpath='{.data.kubeconfig}' | base64 -d > kubeconfig-hcp-cluster1.yaml
export KUBECONFIG=kubeconfig-hcp-cluster1.yaml
```

Apply the hello-openshift application:

```bash
oc apply -f hello-openshift.yaml
oc get route hello-openshift -n hello-openshift
```

Or Online Boutique, if you want a workload with a UI worth showing after the
cutover. The `no-loadgenerator` overlay drops the Locust traffic generator,
strips the `runAsUser`/`runAsGroup`/`fsGroup` values `restricted-v2` rejects,
swaps upstream's LoadBalancer Service for a Route, and creates its own
namespace:

```bash
oc apply -k https://github.com/sadiquepp/openshift/test-workloads/online-boutique/overlays/no-loadgenerator
oc get route frontend -n online-boutique -o jsonpath='{.spec.host}{"\n"}'
```

It is ~1270m CPU / ~1112Mi of requests across 11 services, which fits two
workers at 4 vCPU / 8Gi. Stateless apart from `redis-cart` on an `emptyDir`,
so what a restore brings back is the workload definitions - the same claim
hello-openshift makes, with a better demo.

## Disconnected Hosted Cluster

A hosted cluster whose worker VMs have **no route off the lab network**, so it
can only ever pull from the local mirror registry. It runs on the disconnected
cluster1 (`hubd`) built by `standard-cluster/setup_cluster1.yaml`, and is
deliberately built to sit *alongside* `hcp-cluster1` rather than replace it -
both can be up at once.

Everything hangs off the same `disconnected_install` in `vars.yaml` as the rest
of the flow. Connected runs are completely unaffected: the same playbooks and
the same role are used for both.

What is distinct about it:

| | Connected (`hcp-cluster1`) | Disconnected (`hcp-cluster1-d`) |
| --- | --- | --- |
| Worker VMs | `c1_worker1..3` | `c1-d-worker1..3` |
| Worker IPs | `.41` `.42` `.43` | `.44` `.45` `.46` |
| Internet access from workers | yes | **cut** (no NAT, egress rejected) |
| DNS zone | `hcp-cluster1.mylab.com` | `hcp-cluster1-d.mylab.com` |
| `api` / `api-int` | MetalLB `hcp-cluster1-api-pool` | MetalLB `hcp-cluster1-d-api-pool` |
| `*.apps` VIP (haproxy) | `.149` (`c1lb`) | `.148` (`c1dlb`) |
| VM folder | `/var/lib/libvirt/images/hosted_cluster` | `/var/lib/libvirt/images/hosted_cluster_disconnected` |

The `api`/`api-int` split is what makes the two clusters independent: each
hosted cluster's kube-apiserver VIP comes from its own single-address MetalLB
pool, and each has its own zone publishing that address, so nothing collides.
`*.apps` for the disconnected cluster is served by its own haproxy frontends on
`c1dlb`, whose backends are the three `c1dworker*` addresses.

### How the workers are cut off

Before any VM is created, the three worker IPs get the same two rules the
disconnected hub's nodes get (see
`roles/setup-hub-cluster-disconnected/tasks/block-node-nat.yml`, which both
share):

1. `RETURN` at the top of libvirt's `LIBVIRT_PRT` chain (**nat** table), so
   their traffic is never MASQUERADEd out to the real internet.
2. `REJECT` at the top of `LIBVIRT_FWO` (**filter** table), so the un-NATed
   packets do not leave the hypervisor either and the node fails fast instead
   of timing out.

Both matches are "from this worker, NOT to `192.168.122.0/24`", so the mirror
registry, MinIO, the helper's DNS/DHCP/TFTP, the load balancer and the workers
themselves all stay fully reachable - only egress past the lab is cut. The
rules live in the running ruleset only; libvirt rebuilds those chains when
libvirtd restarts or a network is stopped/started, so re-run the playbook (or
just `--tags disconnected-nat`) if that happens. Set
`hosted_cluster_disconnected_block_node_nat: false` to leave the workers
connected while still installing from the mirror - useful when checking whether
a failure is genuinely caused by the disconnection.

### Image signature policy on the discovery host

RHCOS ships `/etc/containers/policy.json` with a `signedBy` entry for
`registry.redhat.io` and `registry.access.redhat.com`. The mirror mappings in
`roles/setup-hub-acm`'s `mirror-config` ConfigMap redirect the *pull* to the
mirror registry, but podman still looks for the detached signature under the
image's original `registry.redhat.io` name - in Red Hat's sigstore, which a node
whose NAT has been cut cannot reach and which mirror-registry does not serve.
So a disconnected worker boots the discovery ISO and dies on:

```
podman[3161]: Trying to pull registry.redhat.io/multicluster-engine/assisted-installer-agent-rhel9@sha256:12fdad03...
podman[3161]: Error: copying system image from manifest list: Source image rejected: A signature was required, but no signature exists
```

`agent.service` never starts, so the host never registers and **no Agent ever
appears in the InfraEnv** - which looks exactly like the VMs failing to boot.

Shipping the GPG key with the ISO does not help: the keys are already on the
host (that is what `keyPaths` points at) - it is the per-image signature that is
missing, not the key to verify it with.

The fix is the InfraEnv's `spec.ignitionConfigOverride`, which writes a
`policy.json` that accepts those two registries unsigned - the same edit that
works by hand after sshing into the node, applied declaratively so the run does
not need one. `setup_hosted_cluster_vm.yaml` puts it on the live InfraEnv
(patching an InfraEnv that predates it), waits for assisted-service to rebuild
the ISO, and forces the cached copy in `download_dir` to be replaced before
booting the VMs - so nothing boots the old ISO. `setup_bminfra.yaml` renders the
same override into `.rendered-03-infraenv.yaml` for a fresh InfraEnv.

Only the disconnected path does any of this; a connected InfraEnv is rendered
exactly as before. Tunable in `vars.yaml`:

| Variable | Default | |
|---|---|---|
| `bminfra_disconnected_relax_image_policy` | `true` | set `false` to keep signature checking - only useful if you have mirrored Red Hat's sigstore |
| `bminfra_discovery_image_policy` | both Red Hat registries unsigned | the `policy.json` document itself; narrow it to one registry if you prefer |
| `bminfra_ignition_extra_files` | `[]` | extra ignition `storage.files` entries, e.g. an `/etc/containers/registries.d/` drop-in pointing `sigstore` at a mirrored signature store |
| `bminfra_ignition_version` | `3.2.0` | ignition spec version of the override |

To re-apply just this step against an existing InfraEnv:

```bash
ansible-playbook -i ../inventory/hosts setup_hosted_cluster_vm.yaml --ask-vault-pass \
  --tags discovery-ignition
```

### Building it

Exactly the same commands as a connected hosted cluster - with
`disconnected_install: true` in `vars.yaml`, each step uses the disconnected
hub's kubeconfig and the disconnected cluster list on its own (the flag wins
over `target_hub`):

```bash
# 1. worker VMs (.44-.46): NAT cut first, the hubd InfraEnv's ignition override
#    ensured (see above), then the VMs booted off that InfraEnv's ISO
ansible-playbook -i ../inventory/hosts setup_hosted_cluster_vm.yaml --ask-vault-pass

# 2. approve the discovered agents in the ACM/MCE Web UI, then render the bundle
ansible-playbook -i ../inventory/hosts create_hosted_cluster.yaml --ask-vault-pass

# 3. review and apply it
oc apply -f ../roles/create-hosted-cluster/templates/.rendered-hcp-cluster1-d.yaml
```

DNS for the zone is rendered by `setup-dns` like every other zone - the zone is
always present, and `api`/`api-int` for a `-d` cluster resolve to the
disconnected hub without any override, so a plain `dns` run is all it takes:

```bash
(cd .. && ansible-playbook -i inventory/hosts setup_bm_host.yaml --tags dns --ask-vault-pass)
```

Verify:

```bash
dig +short api.hcp-cluster1-d.mylab.com @192.168.122.21     # -> 192.168.122.167 on hubd
dig +short console-openshift-console.apps.hcp-cluster1-d.mylab.com @192.168.122.21  # -> 192.168.122.148
sudo iptables -t nat -nL LIBVIRT_PRT --line-numbers | grep hcp-cluster1-d
```

Adding a second disconnected hosted cluster is the same three edits as a
connected one: a name in `hosted_clusters_disconnected`, an entry in
`hosted_cluster_metallb_pools`, and its workers in
`hosted_cluster_disconnected_workers` (plus their `ip_list` octets, a zone and
haproxy frontends if it needs its own ingress VIP).

## Other hosted-cluster labs

- **KubeVirt platform** - workers as KubeVirt VMs on the hub's OpenShift
  Virtualization, no Agents: [`kubevirt/`](../kubevirt/README.md).
- **The compact cluster as a hub** - `hcp-compact1` on the three-node compact
  cluster, with the same `setup_bminfra.yaml` / `setup_hosted_cluster_vm.yaml` /
  `create_hosted_cluster.yaml` run with `-e target_hub=compact`:
  [compact-cluster-hcp.md](../compact-cluster/compact-cluster-hcp.md).
- **Backup and restore onto a second hub** - [`dr/`](dr/README.md).

## Cleanup

From the repository root:

```bash
ansible-playbook -i inventory/hosts cleanup.yaml --tags hosted      # the worker VMs (c1_worker*, c1-d-worker*)
```

Delete the HostedCluster on the hub first (`oc delete hostedcluster <name> -n
<name>`) so HyperShift removes its control plane, then the workers.
