# ACM, MCE and MetalLB: making a cluster a hosted-control-plane hub

`setup_acm.yaml` turns a [standard cluster](../standard-cluster/) - or the
[compact cluster](../compact-cluster/) - into a management hub for hosted
control planes. It runs
[`roles/setup-hub-acm`](../roles/setup-hub-acm/), which installs:

- **ACM** and its `MultiClusterHub` - which brings **MCE**, HyperShift and
  assisted-service with it - plus the `Provisioning` CR metal3 needs before an
  AgentServiceConfig can be applied;
- the **MetalLB** operator and instance, and one single-address
  `IPAddressPool` + `L2Advertisement` per hosted cluster (see
  [MetalLB Address Pools for Hosted Clusters](#metallb-address-pools-for-hosted-clusters));
- the **OADP** operator, so a hub is ready for [`oadp/`](../oadp/) and the
  backup/restore flow in [`hcp/dr/`](../hcp/dr/);

and renders - but does not apply - the **AgentServiceConfig**, for you to
review and apply once the hub has a default StorageClass.

It used to be the `acm` tag of the hub playbooks; it is its own playbook now so
a cluster can be built without becoming a hub, and become one later.

## Contents

- [What lives here](#what-lives-here)
- [Install it](#install-it)
- [Narrower re-runs](#narrower-re-runs)
- [Disconnected deployment](#disconnected-deployment)
- [Apply the AgentServiceConfig](#apply-the-agentserviceconfig)
- [MetalLB Address Pools for Hosted Clusters](#metallb-address-pools-for-hosted-clusters)
  - [DR cutover](#dr-cutover)
  - [What actually pins the address](#what-actually-pins-the-address)
  - [Notes and constraints](#notes-and-constraints)
  - [Verifying](#verifying)
- [Operator versions](#operator-versions)

## What lives here

| File             | Purpose |
| ---------------- | ------- |
| `setup_acm.yaml` | Install ACM/MCE, MetalLB and the OADP operator on cluster1 or cluster2 (connected or disconnected) or the compact cluster, create the hosted clusters' MetalLB pools, render the AgentServiceConfig |
| `ansible.cfg`    | Mirrors the root `ansible.cfg` and adds `../roles` to `roles_path` |

The role stays in the shared [`roles/`](../roles/) directory. **Run the
playbook from inside this directory** - the `ansible.cfg` here is what puts
`../roles` on the role search path:

```bash
cd acm
ansible-playbook -i ../inventory/hosts setup_acm.yaml --ask-vault-pass
```

## Install it

The cluster has to be up first ([standard-cluster/](../standard-cluster/)); the
playbook stops with the command to build it if the kubeconfig is not there.

```bash
ansible-playbook -i ../inventory/hosts setup_acm.yaml --ask-vault-pass                     # cluster1
ansible-playbook -i ../inventory/hosts setup_acm.yaml --ask-vault-pass -e target_hub=hub2  # cluster2
```

`target_hub` picks the cluster and `disconnected_install` its disconnected
build, exactly as the cluster playbooks do:

| `target_hub` | `disconnected_install` | Cluster | Kubeconfig from | `metallb_hub` |
| --- | --- | --- | --- | --- |
| `hub` (default) | `false` | cluster1 | `hub_install_folder` | `hub` |
| `hub` | `true` | cluster1 disconnected (`hubd`) | `hub_disconnected_install_folder` | `hubd` |
| `hub2` | `false` | cluster2 | `hub2_install_folder` | `hub2` |
| `hub2` | `true` | cluster2 disconnected (`hub2d`) | `hub2_disconnected_install_folder` | `hub2` |
| `compact` | `false` (required) | the compact cluster | `compact_install_folder` | `compact` |

`target_hub` defaults to the value in `vars.yaml` (`hub`). Keep it at `hub` or
`hub2` for the disconnected builds and set `disconnected_install` instead -
`-e target_hub=hubd` is refused with that advice.

`-e target_hub=compact` makes the connected three-node compact cluster
([`compact-cluster/`](../compact-cluster/)) a management cluster: its
kubeconfig, the `oc` in its install folder, and `metallb_hub=compact`, so its
only pools are for the hosted clusters with a `compact` address
(`hcp-compact1`). It refuses `disconnected_install: true`. The walkthrough is
[compact-cluster-hcp.md](../compact-cluster/compact-cluster-hcp.md):

```bash
ansible-playbook -i ../inventory/hosts setup_acm.yaml --ask-vault-pass -e target_hub=compact
```

`metallb_hub` selects each hosted cluster's address out of
`hosted_cluster_metallb_pools`. cluster2 gets `hub2` in **both** modes: as the
DR hub, a hosted cluster restored onto it comes up on hub2's addresses whichever
way cluster2 was built. What `disconnected_install` decides there is *which*
clusters get pools - see [below](#metallb-address-pools-for-hosted-clusters).

The run waits for the cluster operators to be healthy, then for the operators it
subscribes to, then creates the `MultiClusterHub` - expect 15-25 minutes.
Check it:

```bash
export KUBECONFIG=/var/lib/libvirt/images/hub_install/auth/kubeconfig
oc get multiclusterhub -n open-cluster-management          # Running
oc get csv -A | grep -E 'advanced-cluster-management|multicluster-engine|metallb|oadp'
oc get ipaddresspool,l2advertisement -n metallb-system
```

## Narrower re-runs

Against a hub that is already up:

```bash
# only the hosted clusters' MetalLB API pools - all a hub needs after a hosted
# cluster is added to hosted_cluster_metallb_pools in vars.yaml
ansible-playbook -i ../inventory/hosts setup_acm.yaml --ask-vault-pass --tags metallb

# only the pinned-OADP InstallPlan approval / CSV wait (oadp_operator_starting_csv)
ansible-playbook -i ../inventory/hosts setup_acm.yaml --ask-vault-pass --tags oadp
```

Add `-e target_hub=hub2` (and/or `-e disconnected_install=true`) to point either
at another cluster. The whole playbook is idempotent, so re-running it without
tags is safe too.

## Disconnected deployment

`setup-hub-acm` renders differently when it is run disconnected. It reads the
same `disconnected_install` from `vars.yaml` as the rest of the flow, so with
that set to `true` this happens as part of the run above and there is nothing
extra to run. Connected runs are unaffected.

What the disconnected run does on top of the connected one:

- Points every operator `Subscription` (ACM, MetalLB, OADP - and LVM Storage,
  which the cluster build installs) at the CatalogSource oc-mirror generated
  instead of the built-in `redhat-operators`, which a disconnected hub has
  disabled. The name is derived from `mirror_catalog_index`
  (`...redhat-operator-index:v4.21` → `cs-redhat-operator-index-v4-21`);
  override `acm_catalog_source` in `vars.yaml` if oc-mirror named it something
  else. The role checks the CatalogSource exists before subscribing, since a
  Subscription naming a missing catalog just sits in `ResolutionFailed`.
- Publishes the mirror registry CA as the `registry-config` ConfigMap in
  `openshift-config`, keyed `<registry-host>..<port>`, and sets it as
  `spec.additionalTrustedCA` on `image.config.openshift.io/cluster` so every
  node's CRI-O trusts the mirror. This is a MachineConfig change - the role
  waits for the pools to roll it out.
- Creates the `mirror-config` ConfigMap in `multicluster-engine` (mirror
  registry CA + `registries.conf`) and renders
  `AgentServiceConfig.spec.mirrorRegistryRef` pointing at it, so
  assisted-service and the discovery ISOs it builds pull through the mirror.
  The mappings come from `acm_disconnected_registry_mirrors`; the role warns
  about any source the hub's own IDMS/ITMS redirects that is missing from it.
- Downloads each `agent_service_os_images` RHCOS live ISO to the bare-metal
  host and pushes it to the helper's web server
  (`/var/www/html/bootp`, served on port 8080), then rewrites the `osImages`
  urls to point there - the `mirror.openshift.com` urls are unreachable from a
  disconnected hub. Set `agent_service_os_images_disconnected` in `vars.yaml`
  to skip this and use urls you staged yourself.
- Creates one `ClusterImageSet` per osImage version
  (`openshift-4-21-0`, ...) pointing at the mirror registry's copy of the
  release payload, since ACM/HyperShift will not offer a version that has no
  ClusterImageSet and cannot resolve the quay.io pullspec anyway.

The ISOs are ~1.3G each and the helper is built from the 10G RHEL9 base image,
so `roles/setup-bm-host` expands that base image into a `helper_disk_size`
(100G, sparse) disk with `virt-resize`. An **existing** helper is not resized -
if the role stops with "the helper has only N G free", either delete
`/var/lib/libvirt/images/helper_disk.qcow2` and re-run `setup_bm_host.yaml`, or
grow the disk in place:

```bash
virsh shutdown helper
qemu-img resize /var/lib/libvirt/images/helper_disk.qcow2 100G
virsh start helper
# on the helper
growpart /dev/vda 4 && xfs_growfs /
```

The rendered files are left in `roles/setup-hub-acm/files/` for review:
`.rendered-05-agentserviceconfig.yaml` (the disconnected rendering, applied
manually as below), plus `.rendered-06-registry-ca-configmap.yaml`,
`.rendered-07-mirror-config-configmap.yaml` and
`.rendered-08-clusterimagesets.yaml`, which the role applies itself.

## Apply the AgentServiceConfig

The AgentServiceConfig configures the Central Infrastructure Management (CIM)
service that the Agent-platform hosted clusters in [`hcp/`](../hcp/) need. The
role renders it to `roles/setup-hub-acm/files/.rendered-05-agentserviceconfig.yaml`
and stops there: review it (customise the `osImages` to the ones you want for
the hosted clusters) and apply it yourself.

- Make sure ACM and the `MultiClusterHub` are fully operational first.
- The hub also needs a **default StorageClass** by this point. The
  AgentServiceConfig provisions three PVCs (`databaseStorage` 10Gi,
  `filesystemStorage` 100Gi, `imageStorage` 50Gi) and none of them names a
  `storageClassName`, so they bind against the default class - without one
  they stay `Pending` and assisted-service never starts. On the LVM path
  that class is `lvms-vg1` and the cluster build created it. On the Ceph path
  (`use_lvm_storage: false`) run `ceph/setup_ceph.yaml` and
  `ceph/setup_ceph_odf.yaml` ([ceph/README.md](../ceph/README.md)) **before**
  this step, so `ocs-external-storagecluster-ceph-rbd` exists and is the default
  (`ceph_odf_rbd_default_sc`). Confirm with `oc get sc`.

```bash
oc get sc                                # exactly one class marked (default)
oc apply -f ../roles/setup-hub-acm/files/.rendered-05-agentserviceconfig.yaml
oc get pvc -n multicluster-engine        # the three should reach Bound
```

The same applies on the DR hub: OADP does not restore AgentServiceConfigs, so
cluster2 gets its own applied before the restore
([hcp/dr/README.md](../hcp/dr/README.md#build-the-dr-hub-cluster2)).

Next: the InfraEnv and the hosted clusters, in [hcp/README.md](../hcp/README.md).

## MetalLB Address Pools for Hosted Clusters

Each hosted cluster gets its **own single-address MetalLB pool**, so its
kube-apiserver VIP can only ever be the address DNS publishes for it on the hub
it is running on.

The **pool name is the same on every hub**; the **address it holds is not**.
The DR hub sits on its own network segment where hub1's addresses are not
routable, so each cluster carries one address per hub:

| Hosted cluster   | Pool                      | hub (cluster1)    | hub2 (cluster2, DR) | hubd (cluster1, disconnected) |
| ---------------- | ------------------------- | ----------------- | ------------------- | ----------------------------- |
| `hcp-cluster1`   | `hcp-cluster1-api-pool`   | `192.168.122.160` | `192.168.122.190`   | -                             |
| `hcp-cluster2`   | `hcp-cluster2-api-pool`   | `192.168.122.161` | `192.168.122.191`   | -                             |
| `hcp-cluster3`   | `hcp-cluster3-api-pool`   | `192.168.122.162` | `192.168.122.192`   | -                             |
| `hcp-cluster1-d` | `hcp-cluster1-d-api-pool` | -                 | `192.168.122.193`   | `192.168.122.167`             |
| `hcp-cluster2-d` | `hcp-cluster2-d-api-pool` | -                 | `192.168.122.194`   | `192.168.122.168`             |
| `hcp-cluster3-d` | `hcp-cluster3-d-api-pool` | -                 | `192.168.122.195`   | `192.168.122.169`             |

A `-` is a deliberate reservation, not an omission. The connected three never
run on the disconnected hub and the `-d` clusters never run on hub1, so those
addresses do not exist and nothing renders a pool for them. hub2 is the one hub
in both columns: it is the DR restore target for both sets, because the
disconnected bundle pins the canonical quay.io release digest and a `-d`
cluster therefore restores onto a connected hub unchanged.

Keeping the pool names identical across hubs is what lets a HostedCluster's
`metallb.io/address-pool` annotation survive an OADP restore onto the DR hub
unchanged - the cluster still belongs to the same pool, that pool just holds a
different, locally routable address there.

All of it comes from one map in `vars.yaml`, so the pool, the DNS record and
the HostedCluster annotation cannot drift apart:

```yaml
hosted_cluster_metallb_pools:
  hcp-cluster1:
    pool: hcp-cluster1-api-pool
    ip:
      hub: 60
      hub2: 90
  hcp-cluster2:
    pool: hcp-cluster2-api-pool
    ip: {hub: 61, hub2: 91}
  hcp-cluster3:
    pool: hcp-cluster3-api-pool
    ip: {hub: 62, hub2: 92}

  hcp-cluster1-d:
    pool: hcp-cluster1-d-api-pool
    ip: {hub2: 93, hubd: 67}
  hcp-cluster2-d:
    pool: hcp-cluster2-d-api-pool
    ip: {hub2: 94, hubd: 68}
  hcp-cluster3-d:
    pool: hcp-cluster3-d-api-pool
    ip: {hub2: 95, hubd: 69}
```

Three variables select what a hub gets:

- **`metallb_hub`** - which hub `setup-hub-acm` is configuring. Set by
  `setup_acm.yaml` from `target_hub` and `disconnected_install` - `hub`, `hubd`,
  `hub2` or `compact`, per the table in [Install it](#install-it).
- **`target_hub`** - which hub is currently authoritative. Picks the kubeconfig
  for this playbook and for the `hcp/` and `oadp/` playbooks, and selects the
  address `setup-dns` publishes as `api`/`api-int`. Defaults to `hub`.
- **`disconnected_install`** - which *set* of hosted clusters the run is about:
  `hosted_clusters_disconnected` when true, `hosted_clusters` when false. The
  same switch `roles/create-hosted-cluster` renders from, so a hub gets pools
  for exactly the clusters that hub run would create there.

Carrying the hub's address is necessary but not sufficient - the cluster must
also be in the run's install mode. This only ever matters on hub2, the one hub
holding addresses for both sets:

| Run | `disconnected_install` | Pools rendered |
| --- | --- | --- |
| `setup_acm.yaml` | `false` | `hcp-cluster1/2/3-api-pool` (.160-.162) |
| `setup_acm.yaml` | `true` | `hcp-cluster{1,2,3}-d-api-pool` (.167-.169) |
| `setup_acm.yaml -e target_hub=hub2` | `false` | `hcp-cluster1/2/3-api-pool` (.190-.192) |
| `setup_acm.yaml -e target_hub=hub2` | `true` | `hcp-cluster{1,2,3}-d-api-pool` (.193-.195) |

So `oc get IPAddressPool -n metallb-system` on a connected hub2 shows **three**
pools, not six. The `-d` clusters' hub2 addresses stay reserved in the map; the
pools for them are created by re-running just the `metallb` tag in disconnected
mode, which is the step before restoring a `-d` cluster onto hub2.

This is **the one place a `-e disconnected_install=` override is genuinely
useful**: hub2 is the DR target for both kinds of cluster, so a hub2 you built
connected may still need the `-d` pools, and that is a single day-2 action
rather than a change of mode for the whole lab:

```bash
ansible-playbook -i ../inventory/hosts setup_acm.yaml --ask-vault-pass --tags metallb \
  -e target_hub=hub2 -e disconnected_install=true
```

Careful: that combination resolves the *disconnected* cluster2's kubeconfig
(`hub2_disconnected_install_folder`). To add the `-d` pools to a cluster2 that
was built **connected**, also name its kubeconfig - an `-e` beats the play
variable:

```bash
ansible-playbook -i ../inventory/hosts setup_acm.yaml --ask-vault-pass --tags metallb \
  -e target_hub=hub2 -e disconnected_install=true \
  -e acm_kubeconfig=/var/lib/libvirt/images/hub2_install/auth/kubeconfig
```

`roles/setup-hub-acm` prints the split it resolved (`renders pools for: ... |
not rendered here: ...`) on every run, so you can see which set a hub got
without reading the map.

Switching modes also cleans up after itself, since `oc apply` does not prune: a
hub2 built when both sets were rendered would otherwise keep all six pools
forever. The role deletes the other mode's `IPAddressPool`/`L2Advertisement`
pairs - only ones named in `hosted_cluster_metallb_pools` that carry an address
for this hub, never anything else on the cluster - and **skips any pool whose
address a Service currently holds**, so a connected re-run cannot strip the VIP
from a `-d` cluster restored onto the same hub. It reports what it did
(`deleted:` / `in use ..., kept:` / `absent:`) per pool. Set
`metallb_prune_other_mode_pools: false` to leave both modes' pools in place.

Three things are generated from the map:

1. `roles/setup-hub-acm` renders one `IPAddressPool` per cluster of this run's
   install mode that holds a `metallb_hub` address (a single address, e.g.
   `192.168.122.160-192.168.122.160`) and one `L2Advertisement` per pool, so each
   address is advertised independently.
2. `roles/setup-dns` points that cluster's `api` and `api-int` A records at
   `target_hub`'s address for it.
3. `roles/create-hosted-cluster` annotates the HostedCluster with
   `metallb.io/address-pool: <pool>`.

If a hub is genuinely on a different subnet rather than a different block of
the same `/24`, give it its own prefix and the addresses and DNS records both
follow:

```yaml
hosted_cluster_metallb_network_prefixes:
  hub2: "192.168.150"
```

### DR cutover

The DR hub's pools for the connected clusters are created when ACM is installed
on it (`setup_acm.yaml -e target_hub=hub2` passes `metallb_hub: hub2`), so after
restoring the hosted clusters onto hub2 the only remaining step is to move DNS.
(Restoring a `-d` cluster there needs its pool created first - see the
`disconnected_install` note above.) From the repository root:

```bash
ansible-playbook -i inventory/hosts setup_bm_host.yaml --tags dns \
  --ask-vault-pass -e target_hub=hub2
```

`api`/`api-int` for every hosted cluster now resolve to the hub2 addresses
(`.190`/`.191`/`.192`), which is where their restored kube-apiservers actually
came up. Set `target_hub: hub2` in `vars.yaml` to make the cutover permanent.
The whole procedure is in
[hcp/dr/README.md](../hcp/dr/README.md#point-dns-at-the-dr-hub).

### What actually pins the address

Each pool carries a `serviceAllocation` constraint naming that hosted
cluster's control-plane namespace:

```yaml
  serviceAllocation:
    priority: 10
    namespaces:
      - hcp-cluster1-hcp-cluster1   # <hostedcluster-namespace>-<hostedcluster-name>
```

That constraint is what enforces the mapping. **The
`metallb.io/address-pool` annotation on the HostedCluster does not, on its
own** - MetalLB reads that annotation from the LoadBalancer *Service*, and
HyperShift does not copy HostedCluster annotations onto the `kube-apiserver`
Service it generates. Keep the annotation for what it is: the record, on the
HostedCluster itself, of which pool that cluster belongs to, and the value to
pass if you ever need to force a reallocation by hand:

```bash
oc -n hcp-cluster1-hcp-cluster1 annotate svc/kube-apiserver \
  metallb.io/address-pool=hcp-cluster1-api-pool --overwrite
```

### Notes and constraints

- This replaces the old shared `hcp-ip-pool` (hub `60-63`, hubd `64-67`,
  hub2 `90-93`), which let any hosted cluster take any free address in the
  range. `.63` was left free, and `.67` and `.93` went to the `-d`
  clusters' own pools. `roles/setup-hub-acm` deletes the
  old pool and its `l2advertisement` before applying the new ones, since
  MetalLB rejects overlapping pools - set
  `metallb_remove_legacy_shared_pool: false` to skip that. Every pool has
  since moved to `.100`-`.199` (each old address plus 100), so the VMs have
  `.60`-`.63`, `.67`-`.69` and `.90`-`.95` back.
- **Moving the addresses on a hub that is already up.** The pool names do not
  change, only the address each one holds, so re-running the `metallb` tag
  re-applies them in place, and the DNS run publishes the new addresses:
  ```bash
  ansible-playbook -i ../inventory/hosts setup_acm.yaml --ask-vault-pass --tags metallb
  (cd .. && ansible-playbook -i inventory/hosts setup_bm_host.yaml --ask-vault-pass --tags dns)
  ```
  Then run [Verifying](#verifying) for each hosted cluster: the Service's
  address and DNS must agree, now on `.1xx`. The zones have a 60s TTL, so
  clients follow within a minute. Not yet run on a hub with hosted clusters
  up: if a `kube-apiserver` Service keeps its old address, delete it
  (`oc delete svc kube-apiserver -n <hcp namespace>`) - HyperShift recreates
  it and MetalLB gives it the pool's new address. Simplest is to move them
  while no hosted cluster is running.
- All the hubs can carry the pools at once, since MetalLB only ARPs for an
  address once a Service is assigned it. Just don't run the same hosted
  cluster on two hubs at the same time.
- Every pool is namespace-constrained and there is no longer a catch-all pool
  on the hub, so a LoadBalancer Service outside these control-plane
  namespaces will sit at `<pending>` until you give it a pool of its own.
- Adding a fourth hosted cluster means adding it to **both**
  `hosted_cluster_metallb_pools` (with an address for every hub) and
  `hosted_clusters`, then re-running `setup_acm.yaml --tags metallb` (pools)
  and `setup_bm_host.yaml --tags dns` (records). Both roles fail fast on a
  missing entry.

### Verifying

```bash
oc get ipaddresspool,l2advertisement -n metallb-system
oc get svc kube-apiserver -n hcp-cluster1-hcp-cluster1 \
  -o jsonpath='{.status.loadBalancer.ingress[0].ip}{"\n"}'
dig +short api.hcp-cluster1.mylab.com @192.168.122.21
```

The last two must agree - that is the whole point of this layout.

## Operator versions

`acm_operator_channel`, `oadp_operator_channel` and `oadp_operator_starting_csv`
in `vars.yaml` choose which ACM and OADP the hub installs. An OLM catalog only
offers what supports the cluster's OpenShift version, so on another version
read the catalog on the freshly built cluster before running this playbook:

```bash
oc get packagemanifest advanced-cluster-management -n openshift-marketplace \
  -o jsonpath='{range .status.channels[*]}{.name}{"  "}{.currentCSV}{"\n"}{end}'
oc get packagemanifest redhat-oadp-operator -n openshift-marketplace \
  -o jsonpath='{range .status.channels[*]}{.name}{": "}{.entries[*].name}{"\n"}{end}'
```

A pinned `oadp_operator_starting_csv` makes the Subscription ask for manual
approval; the role approves only the InstallPlan that installs exactly that
CSV (tag `oadp`), so a later plan offering an upgrade stays unapproved.
