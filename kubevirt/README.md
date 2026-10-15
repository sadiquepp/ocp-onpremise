# Hosted clusters on the KubeVirt platform

A hosted cluster whose control plane runs as pods on the hub and whose
workers are **KubeVirt VMs the NodePool creates on the hub**. You don't make
VMs, boot ISOs or approve Agents: `replicas: 2` gives you two VMs, and
scaling, repair and upgrade all go through the NodePool.

In this lab the hub's nodes are themselves libvirt VMs, so the workers are
**nested**:

```
L0  RHEL host            kvm nested=1 (roles/setup-bm-host)
 └─ L1  hub VMs           --cpu host-passthrough → /dev/kvm inside
     │                    MCE (HyperShift), MetalLB, OpenShift Virtualization
     ├─ hcp-kv1-hcp-kv1   control-plane pods: etcd, kube-apiserver, ...
     └─ L2  worker VMs    hcp-kv1's nodes, one per NodePool replica
```

**Commands only, start to finish: [steps.md](steps.md).** The host, Ceph and
the hub are built with the same root playbooks as the backup/restore flow
([../steps.md](../steps.md)), and their commands are repeated there; this directory
automates only what that flow doesn't do.

Nested virtualisation is fine for learning the flow (ingress, kubevirt-csi,
backup and restore), but not for performance numbers, and Red Hat does not
support it. There is no third level: the hosted cluster cannot run OpenShift
Virtualization itself.

- [What it builds](#what-it-builds)
- [Ingress: the hub's router, or MetalLB](#ingress-the-hubs-router-or-metallb)
- [Workers on another cluster: hcp-kv2 on the SNO](#workers-on-another-cluster-hcp-kv2-on-the-sno)
  - [Grow the SNO's storage disk](#grow-the-snos-storage-disk)
- [Three clusters: ACM on hub1, MCE on sno3, VMs on the SNO (hcp-kv3)](#three-clusters-acm-on-hub1-mce-on-sno3-vms-on-the-sno-hcp-kv3)
- [Before you start](#before-you-start)
- [Run it](#run-it)
- [Add a cluster](#add-a-cluster)
- [What it changes outside this directory](#what-it-changes-outside-this-directory)
- [Remove a cluster](#remove-a-cluster)
- [Troubleshooting](#troubleshooting)

## What it builds

| Piece | Where | Comes from |
|---|---|---|
| Host, Ceph, hub1, ODF on Ceph | | root playbooks, [steps.md](steps.md) steps 1-6 |
| API address `192.168.122.250` | hub MetalLB, pool `hcp-kv1-api-pool` | `setup_hub_cluster.yaml` (step 6), from `hosted_cluster_metallb_pools` |
| `api.hcp-kv1.hub.mylab.com` | helper DNS | `setup_bm_host.yaml` (step 4) |
| OpenShift Virtualization | hub, `openshift-cnv` | `--tags mgmt` (`roles/kubevirt-mgmt`) |
| HostedCluster, NodePool, Secrets | hub, namespace `hcp-kv1` | `--tags cluster` (`roles/kubevirt-hosted-cluster`) |
| Control plane | hub, namespace `hcp-kv1-hcp-kv1` | HyperShift |
| Worker VMs, their root disks | hub, namespace `hcp-kv1-hcp-kv1` | the NodePool (Cluster API KubeVirt provider + CDI) |
| PVCs inside the hosted cluster | hub, as DataVolumes hot-plugged into the VMs | kubevirt-csi (`storageDriver: Default`) |

hcp-kv2 is built the same way, except that its worker VMs, their disks and
its `*.apps` Route are on the SNO, and OpenShift Virtualization goes there
too: [Workers on another cluster](#workers-on-another-cluster-hcp-kv2-on-the-sno).

The kube-apiserver is a MetalLB LoadBalancer on the hub, like the Agent
clusters' (`hcp-cluster1/2/3`). OAuth, OIDC, Konnectivity and Ignition are
Routes on the hub's router, under `*.apps.hub.mylab.com`.

The rendered bundle for each cluster is left in `kubevirt/rendered/<name>/`
(ignored by git, as it holds the pull secret):

```
01-hosted-cluster.yaml  Namespace, pull-secret and ssh-key Secrets, HostedCluster
02-nad.yaml             the localnet NAD (ingress: metallb only)
03-nodepool.yaml        NodePool
04-acm-import.yaml      ManagedCluster, KlusterletAddonConfig (kubevirt_acm_import only)
```

The import into ACM is MCE's own: its hypershift add-on creates the
ManagedCluster and KlusterletAddonConfig once the control plane is
available. `kubevirt_acm_import: true` renders and applies them here instead,
for a hub with that turned off.

## Ingress: the hub's router, or MetalLB

Each cluster's `*.apps` takes one of two paths, set per cluster with
`ingress:` on its `hosted_clusters` entry (default `kubevirt_ingress: route`
in `vars.yaml`).

**`route` (default): through the hub's router.**

```
client → *.apps.hub.mylab.com wildcard (already in DNS) → hub router
       → SNI *.apps.hcp-kv1.apps.hub.mylab.com
       → wildcard passthrough Route + Service in hcp-kv1-hcp-kv1
       → worker VM (virt-launcher pod) → hosted cluster's router → app
```

- The HostedCluster sets `baseDomainPassthrough: true`, so the cluster's apps
  live under the hub's apps domain: `*.apps.hcp-kv1.apps.hub.mylab.com`.
- No DNS record and no address per cluster: the hub's existing wildcard covers
  it, and the router tells clusters apart by hostname (TLS SNI).
- The hub's default IngressController must admit wildcard routes
  (`routeAdmission.wildcardPolicy: WildcardsAllowed`). `--tags mgmt` sets it,
  which rolls the hub's router pods once.
- The workers have one NIC, on the hub's pod network.

**`metallb`: MetalLB inside the hosted cluster.**

```
client → *.apps.hcp-kv1.hub.mylab.com → 192.168.122.251 (apps_vip)
       → MetalLB L2 on a worker VM's NIC on the lab network
       → Service openshift-ingress/metallb-ingress → router → app
```

- The cluster has its own domain (`hosted_cluster_domains`, `hub.mylab.com`
  for hcp-kv1) and its own `*.apps` address
  (`hosted_cluster_node_keys['hcp-kv1'].apps_vip`, `.251`). setup-dns already
  publishes both.
- The address only works if the worker VMs are **on the lab network**, where
  clients can see MetalLB's ARP. On the default pod network a VM sits behind
  virt-launcher's NAT and its ARP never leaves the hub node. So:
  - `--tags mgmt` installs NMState and maps an OVN-Kubernetes **localnet**
    (`kubevirt-lab`) onto `br-ex` on the hub's workers. `br-ex` is already on
    their lab-network NIC, so the hub VMs need no extra NIC.
  - Each cluster gets a **Multus NAD** for that localnet in its control-plane
    namespace, and its NodePool attaches the VMs to it with
    `attachDefaultNetwork: false`. One NIC, on `192.168.122.0/24`, with an
    address from libvirt's DHCP (`.201-.249`). The default route is on the
    same NIC, so replies leave the way requests came in.
  - The NAD has no `subnets`, so OVN does no IP port security, and a VM can
    answer for `.251`, an address it was never assigned.
- `--tags ingress` then runs `../roles/setup-hcp-ingress-metallb`, the role
  `compact-cluster-hcp.md` uses for hcp-compact1, unchanged.

Switch one run's clusters to MetalLB with `-e kubevirt_ingress=metallb`, or
set it on the entry:

```yaml
hosted_clusters:
  - name: hcp-kv1
    platform: kubevirt
    ingress: metallb
```

`baseDomainPassthrough` is immutable: to move a cluster between the two
modes, destroy it and create it again.

## Workers on another cluster: hcp-kv2 on the SNO

hcp-kv2 keeps its control plane on the hub like hcp-kv1, but its worker VMs
run on the single-node cluster (`../setup_sno.yaml`) - HyperShift's "external
infrastructure". It is the same playbook; `infra: sno` on its
`hosted_clusters` entry is the whole difference, and both clusters run side
by side.

```
L0  RHEL host
 ├─ L1  hub VMs                    MCE, MetalLB, OpenShift Virtualization
 │   ├─ hcp-kv1-hcp-kv1            hcp-kv1's control plane AND worker VMs
 │   └─ hcp-kv2-hcp-kv2            hcp-kv2's control plane only
 │         └ Secret infra-kubeconfig-hcp-kv2 (in hcp-kv2) ──┐
 └─ L1  SNO VM                     OpenShift Virtualization, LVM Storage
     └─ hcp-kv2-hcp-kv2  <──────── ServiceAccount hcp-infra ┘
           L2 worker VMs, their disks, kubevirt-csi volumes,
           the *.apps passthrough Route
```

| | hcp-kv1 | hcp-kv2 |
|---|---|---|
| Control plane | hub, `hcp-kv1-hcp-kv1` | hub, `hcp-kv2-hcp-kv2` |
| API | `api.hcp-kv1.hub.mylab.com` → `.250` (hub MetalLB) | `api.hcp-kv2.hub.mylab.com` → `.252` (hub MetalLB) |
| OAuth, Konnectivity, Ignition | hub's router | hub's router |
| Worker VMs, disks | hub, Ceph RBD | SNO, `lvms-vg1` |
| `*.apps` | `*.apps.hcp-kv1.apps.hub.mylab.com`, hub's router | `*.apps.hcp-kv2.apps.sno.mylab.com`, SNO's router |

What the playbook does for it, on top of hcp-kv1's steps:

- `--tags mgmt` runs against **each** cluster that hosts VMs - the hub for
  hcp-kv1, the SNO for hcp-kv2: OpenShift Virtualization, and wildcard routes
  on that cluster's router.
- `--tags cluster` renders `00-infra-access.yaml` and applies it **to the
  SNO**: the VM namespace `hcp-kv2-hcp-kv2`, a ServiceAccount `hcp-infra`
  with the Role HyperShift documents for this (VMs, DataVolumes, Services,
  EndpointSlices, Routes, NetworkPolicies, snapshots - that namespace only),
  its token, and a ClusterRole to read the SNO's network config. A kubeconfig
  for that ServiceAccount - the SNO's API and CA, from its admin kubeconfig -
  goes into `01-hosted-cluster.yaml` as the Secret
  `spec.platform.kubevirt.credentials` names.
- The waits, the report and `--tags destroy` look for the VMs on the SNO;
  destroy also removes the SNO namespace and the ClusterRole.

**The SNO.** It is built by its own root playbook, and two of its defaults
do not suit this:

- **Storage.** `sno_lvm_storage` follows `use_lvm_storage`, which the Ceph flow
  sets `false` - leaving the SNO with no StorageClass for the root disks. Pass
  `-e sno_lvm_storage=true` (or add it later with `--tags snostorage`).
  `lvms-vg1` is ReadWriteOnce; on one node there is nowhere to migrate to
  anyway.
- **Size.** The default 12 vCPU / 32 GiB is mostly taken by OpenShift and
  OpenShift Virtualization themselves; a 12 GiB worker does not fit beside
  them. Build it at 64 GiB / 16 vCPU for hcp-kv2's two 4 vCPU / 12 GiB
  workers if the host has room (`free -g`), or at 48 GiB with one worker
  (`nodepool_replicas: 1` on hcp-kv2's entry).

- **Disk.** The worker VMs' root disks (32Gi each) come out of LVMS's thin
  pool, which is 90% of the SNO's second disk. `sno_storage_disk_gb` is 512,
  so hcp-kv2's and hcp-kv3's workers fit. The disk is a thin qcow2 on the
  host, so it uses host space only as it fills. OpenShift Virtualization's
  built-in OS images, ~55 GB, are turned off (`kubevirt_common_boot_images`).
  They filled a 100 GB pool and paused hcp-kv2's workers.

[steps.md](steps.md#hcp-kv2-workers-on-the-sno-optional) has the commands.

### Grow the SNO's storage disk

`sno_storage_disk_gb` sizes the disk only when it is created. To grow the
disk of a running SNO, here from 100 GB to 512 GB, without a rebuild or
reboot:

```bash
# on the host: grow the disk under the running VM - still thin, the file grows as written
virsh domblklist sno                    # vdb -> .../sno_storage.qcow2
virsh blockresize sno vdb 512G

# in the SNO: let LVM see the new size
SNO=/var/lib/libvirt/images/sno_install/auth/kubeconfig
oc --kubeconfig $SNO debug node/sno -q -- chroot /host sh -c \
  'lsblk /dev/vdb; pvresize /dev/vdb; vgs vg1; lvs -o lv_name,lv_size,data_percent,metadata_percent vg1/thin-pool-1'
```

`vgs` should show `vg1` at ~512g with most of it free. LVMS may extend the
thin pool to 90% of the larger VG by itself within a few minutes. If `lvs`
still shows `thin-pool-1` at 89.90g, extend it and its metadata yourself:

```bash
oc --kubeconfig $SNO debug node/sno -q -- chroot /host sh -c \
  'lvextend -l 90%VG vg1/thin-pool-1; lvextend --poolmetadatasize +1G vg1/thin-pool-1;
   lvs -o lv_name,lv_size,data_percent,metadata_percent vg1/thin-pool-1'
oc --kubeconfig $SNO get lvmcluster -n openshift-storage -o jsonpath='{.items[0].status.state}{"\n"}'   # Ready
```

The metadata must grow with the data: a thin pool whose metadata fills up
fails the same way as a full one.

The SNO is the same one the udn-bgp-evpn and spiffe-spire labs build and use
(`sno_install_folder`). They can share it, but hcp-kv2's workers take most of
its memory, so size it for both or run one at a time.

A run over every cluster leaves hcp-kv2 out, with a message, while the SNO's
kubeconfig doesn't exist - so building only hcp-kv1 needs no SNO. Other
infrastructure clusters are a `kubevirt_infras` entry in `vars.yaml` away.
Only `ingress: route` is wired for them.

## Three clusters: ACM on hub1, MCE on sno3, VMs on the SNO (hcp-kv3)

The layout where the cluster that manages, the cluster that hosts control
planes and the cluster that runs VMs are three different ones:

| Cluster | Role | Built by |
|---|---|---|
| hub1 | ACM: manages sno3, the SNO and every hosted cluster | `../setup_hub_cluster.yaml`, unchanged |
| sno3 (`192.168.122.63`, `sno3.hub.mylab.com`) | MCE only: hcp-kv3's control plane, MetalLB for its API (`.253`) | `setup_sno3_mce.yaml` |
| sno | OpenShift Virtualization: hcp-kv3's worker VM | `../setup_sno.yaml`, as for hcp-kv2 |

```
hub1 (ACM) ── imports ──> sno3 (MCE)   hypershift-addon on sno3 reports its hosted
     │                     │            clusters to hub1 as DiscoveredClusters
     │                     └ hcp-kv3-hcp-kv3: etcd, kube-apiserver (.253), ...
     │                         └ Secret infra-kubeconfig ──> sno (VMs)
     └─ imports ──> sno3-hcp-kv3 (the hosted cluster), and sno (to see its VMs)
```

`setup_sno3_mce.yaml`:

- **builds sno3** with `roles/setup-sno` - 40 GiB / 12 vCPU, LVM Storage, under
  `hub.mylab.com` so the existing DNS forwarder covers it (no libvirt network
  rebuild). Skipped once its kubeconfig exists, so the playbook re-runs.
- **installs MCE alone** (no ACM), with MCE's own import of the hosted clusters
  it creates turned off (`autoImportDisabled`) - hub1 imports them instead.
- **installs MetalLB** and, through `roles/setup-hub-acm`'s own MetalLB tasks
  with `metallb_hub=sno3`, the API pool of every hosted cluster with an `sno3`
  address: `hcp-kv3-api-pool` at `.253`.
- **imports sno3 into hub1's ACM** the way Red Hat documents for MCE clusters:
  a `noOperator` KlusterletConfig (`postfix: mce-import`) so ACM's klusterlet
  sits beside MCE's own, ACM's add-on agents in their own namespace, and the
  hypershift-addon set to discover hosted clusters (`disableHOManagement`,
  `disableMetrics`). Red Hat applies those add-on settings hub-wide; hub1 hosts
  its own hosted clusters, so here they are attached to sno3's add-ons only.
- **imports the SNO into hub1's ACM**, so ACM's search and virtualization views
  show the VMs it runs.

hcp-kv3 is then the KubeVirt playbook run against sno3
(`-e kubevirt_mgmt=sno3`): the same steps as hcp-kv2, with the control plane
on sno3 (`SingleReplica` - one node), and at the end hub1's discovered copy of
it imported (`importAsManagedCluster`) as `sno3-hcp-kv3`. Runs against the hub
leave hcp-kv3 out, and runs against sno3 leave hcp-kv1/kv2 out, each saying so.

**What ACM manages.** The hosted cluster (`sno3-hcp-kv3`) fully - policies,
apps, search, console. Its lifecycle is the HostedCluster and NodePool on sno3.
Its worker VM shows up under the SNO in ACM's VM views; change it through the
NodePool, not there, since the NodePool owns it.

**Not tested on a real lab.** The import and discovery follow Red Hat's
documented procedure as implemented in Red Hat's own test suite (ocs-ci); the
per-cluster scoping of the add-on settings is this repository's, and is the
part to watch on a first run: `oc get managedclusteraddon -n sno3` on hub1.

## Before you start

Build the host, Ceph and hub1 with steps 1-6 of [steps.md](steps.md): the
same root playbooks, and the same commands, as steps 1 and 3-7 of the
backup/restore flow ([../steps.md](../steps.md)). Its steps 8 and 9
(AgentServiceConfig, bminfra, libvirt worker VMs) are for Agent clusters and
are not needed here.

What that gives this lab, and why it matters here:

- **MCE and MetalLB** on hub1 (step 6).
- **hcp-kv1's API pool and DNS name.** hcp-kv1 is in the root `vars.yaml`, so
  step 4 publishes `api.hcp-kv1.hub.mylab.com` and step 6 creates
  `hcp-kv1-api-pool` at `.250`. On a hub built before hcp-kv1 was added,
  re-run `setup_hub_cluster.yaml --tags metallb` and `setup_bm_host.yaml --tags dns`.
- **Ceph RBD as the default StorageClass** (step 7, with
  `use_lvm_storage: false`). CDI gives the worker root disks ReadWriteMany
  block, so the VMs live-migrate when a hub node drains, and kubevirt-csi
  volumes inside the hosted cluster come from the same pool.
  `hello-openshift-pvc.yaml` tests that passthrough: a PVC in the hosted
  cluster, its disk on hub1's Ceph, moved between workers
  ([steps.md, step 10](steps.md#storage-passthrough-a-pvc-from-hub1s-storage)). A hub on LVM
  Storage works too, but its RWO disks can't migrate, so a drain stops the
  workers on that node.
- **Nested KVM** on the host. Step 4 writes `options kvm-intel nested=1`; it
  takes effect once the module reloads, so reboot the host if
  `/sys/module/kvm_intel/parameters/nested` isn't `Y`. On an AMD host, use
  `kvm-amd`.
- **Room on the hub.** Its three workers are 32 vCPU / 48 GiB each, and carry
  both the hosted control plane and the worker VMs (4 vCPU / 16 GiB each by
  default).
- **For hcp-kv2 only, the SNO** (`../setup_sno.yaml`), with LVM Storage and
  room for its workers - see
  [Workers on another cluster](#workers-on-another-cluster-hcp-kv2-on-the-sno).
  Without it a run builds hcp-kv1 alone and says hcp-kv2 was left out.

The preflight checks all of these and names the step that fixes each one.

## Run it

One run does everything below. To go one step at a time, by tag or by hand
with `oc`, follow [steps-hosted-cluster.md](steps-hosted-cluster.md).

```bash
cd kubevirt
ansible-playbook -i ../inventory/hosts setup_hosted_cluster_kubevirt.yaml --ask-vault-pass
```

A plain run does every phase except `destroy`:

| Tag | Does |
|---|---|
| `preflight` | Checks only: MCE and MetalLB on the hub, each cluster's API pool, nested KVM, `api.<name>` resolving to its address (and `*.apps` for MetalLB clusters), an infrastructure cluster that answers, a StorageClass for the disks where the VMs run |
| `mgmt` | Each cluster that runs worker VMs (the hub; the SNO for `infra: sno`): OpenShift Virtualization (and a check that some node can run VMs); wildcard routes (`route`); NMState + localnet bridge mapping (`metallb`) |
| `cluster` | Render and apply each cluster, wait for its nodes, MetalLB ingress where set, then the report |
| `wait` | Just the waits, for clusters already applied |
| `ingress` | MetalLB inside each `ingress: metallb` cluster |
| `verify` | Report VMs, nodes and operators not yet Available; reach the console through its ingress |
| `destroy` | Delete the clusters; never runs unless named |

Useful options:

```bash
-e kubevirt_cluster=hcp-kv1     # one cluster
-e kubevirt_apply=false         # render kubevirt/rendered/<name>/ only
-e kubevirt_ingress=metallb     # MetalLB ingress for this run's clusters
```

Expect 30-60 minutes for a first run. Most of it is the nested workers: CDI
imports the RHCOS image, each VM boots and reboots, then the hosted cluster's
operators roll out on it.

When it finishes:

```bash
export KUBECONFIG=/var/lib/libvirt/images/hcp-kv1/kubeconfig
oc get nodes,co
oc --kubeconfig /var/lib/libvirt/images/hub_install/auth/kubeconfig \
   -n hcp-kv1-hcp-kv1 get vm,vmi,dv          # the workers, as seen from the hub
```

## Add a cluster

All in the root `vars.yaml`, next to hcp-kv1. Then, from the repository
root, `setup_bm_host.yaml --tags dns` (its API name) and
`setup_hub_cluster.yaml --tags metallb` (its API pool), and this playbook:

```yaml
hosted_clusters:
  - name: hcp-kv4
    platform: kubevirt
    nodepool_replicas: 3          # optional overrides - see kubevirt/vars.yaml
    # infra: sno                  # VMs on the SNO instead of the hub
hosted_cluster_metallb_pools:
  hcp-kv4:
    pool: hcp-kv4-api-pool
    ip:
      hub: 254                    # .250 kv1, .251 kv1's apps, .252 kv2, .253 kv3 (on sno3): .254 is the last
hosted_cluster_domains:
  hcp-kv4: "{{ hub_domain }}"     # resolvable from every VM with no network change
```

The cluster CIDRs (`10.132.0.0/14`, `172.31.0.0/16`) are internal to each
hosted cluster, so every cluster can reuse them. They must not overlap the
hub's own pod and service networks.

## What it changes outside this directory

The playbook, both roles, their templates and `vars.yaml` are all here. The
rest of the lab knows about KubeVirt clusters through a few lines:

- **Root `vars.yaml`**: hcp-kv1's and hcp-kv2's entries in `hosted_clusters`,
  `hosted_cluster_metallb_pools`, `hosted_cluster_domains` and
  `hosted_cluster_node_keys`. That is the data `setup-dns` and `setup-hub-acm`
  already read, so the API name, its zone and the hub's API pool come out the
  same way as for every other hosted cluster.
- **`roles/create-hosted-cluster`** skips `platform: kubevirt` entries, so
  `create_hosted_cluster.yaml` never renders hcp-kv1 as an Agent cluster.
  Naming one with `-e hcp_cluster_name` fails with the command to use instead.

Everything else - the host, Ceph, the hub, MCE, MetalLB, the API pool, DNS,
ODF - comes from the root playbooks ([steps.md](steps.md) steps 1-6) and is
not repeated here.

## Remove a cluster

```bash
ansible-playbook -i ../inventory/hosts setup_hosted_cluster_kubevirt.yaml --ask-vault-pass \
  --tags destroy -e kubevirt_cluster=hcp-kv1
```

This deletes, in order: the ManagedCluster, the NodePool (and with it the VMs
and their disks), the HostedCluster (and its control-plane namespace,
including the NAD), and the `hcp-kv1` namespace.

It leaves the hub's OpenShift Virtualization, wildcard-route policy, localnet
bridge mapping and API pool alone, since other clusters may use them.

For a cluster with `infra` (hcp-kv2) it then also deletes, on the SNO, the VM
namespace `hcp-kv2-hcp-kv2` (ServiceAccount, Role and token with it) and the
ClusterRole and binding that read its network config. The SNO's OpenShift
Virtualization and wildcard-route policy stay.

## Troubleshooting

| Symptom | Likely cause | Check |
|---|---|---|
| Preflight: `nested` is not `Y` | The module was loaded before `kvm-nested.conf` existed | Stop the VMs, `modprobe -r kvm_intel && modprobe kvm_intel`, or reboot |
| No node labelled `kubevirt.io/schedulable=true` | The hub VMs don't see VMX/SVM | `oc debug node/<worker> -- grep -c -E 'vmx\|svm' /proc/cpuinfo`; the hub VMs need `--cpu host-passthrough` |
| VMs stay `Pending`/`Provisioning` | Root-disk PVC unbound, or no room | `oc -n hcp-kv1-hcp-kv1 get dv,pvc,vmi`; `oc describe` the virt-launcher pod |
| HostedCluster waits on kube-apiserver with no address | The API pool is missing, or holds another address | `oc -n metallb-system get ipaddresspool hcp-kv1-api-pool -o yaml`; `oc -n hcp-kv1-hcp-kv1 get svc kube-apiserver` |
| Nodes never join | The VMs can't fetch Ignition or reach the API | `virtctl console -n hcp-kv1-hcp-kv1 <vm>`; from the VM, `curl -k https://api.hcp-kv1.hub.mylab.com:6443/version` |
| Nodes `Ready,SchedulingDisabled`; routers and console Pending | Auto-repair is replacing the workers (NotReady past `kubevirt_mhc_timeout`) and the drain hangs | `oc -n hcp-kv1-hcp-kv1 get machines.cluster.x-k8s.io` shows `Deleting`; raise the NodePool's `hypershift.openshift.io/machine-health-check-timeout`, then `oc annotate machines.cluster.x-k8s.io -n hcp-kv1-hcp-kv1 --all machine.cluster.x-k8s.io/exclude-node-draining=""` |
| hcp-kv2: "Leaving out hcp-kv2" | The SNO isn't built (`/var/lib/libvirt/images/sno_install/auth/kubeconfig` is missing) | Build it - [steps.md, hcp-kv2](steps.md#hcp-kv2-workers-on-the-sno-optional) |
| Worker VMs `Running` but paused (`PausedIOError`, event "not enough space on volume: rhcos"); nodes join, then `NodeStatusUnknown` | The LVMS thin pool is full - on the SNO, usually OpenShift Virtualization's built-in OS images | `oc --kubeconfig <sno> debug node/sno -- chroot /host lvs` (`thin-pool-1` at 100%); `oc --kubeconfig <sno> get pvc -n openshift-virtualization-os-images`. Turn the OS images off (the playbook does): `oc patch hyperconverged kubevirt-hyperconverged -n openshift-cnv --type=merge -p '{"spec":{"workloadSources":{"enableCommonBootImageImport":false}}}'` - with `v1` (4.22); `"unknown field"` means the wrong path. Once `oc get dataimportcron -n openshift-virtualization-os-images` is empty, delete those PVCs, then `virtctl unpause vmi <vm> -n hcp-kv2-hcp-kv2` |
| hcp-kv2: VMs `Pending` on the SNO | Not enough memory beside OpenShift Virtualization, or no StorageClass | `oc --kubeconfig <sno> describe vmi -n hcp-kv2-hcp-kv2`; `oc --kubeconfig <sno> get sc`; a bigger SNO or `nodepool_replicas: 1` |
| hcp-kv2: HostedCluster or NodePool errors naming the infra kubeconfig, no VMs on the SNO | The hub's pods can't reach `api.sno.mylab.com:6443`, or the token is wrong | `oc debug node/worker1.hub.mylab.com -- chroot /host curl -k https://api.sno.mylab.com:6443/version`; `oc get secret infra-kubeconfig-hcp-kv2 -n hcp-kv2`; `oc --kubeconfig <sno> get sa,secret -n hcp-kv2-hcp-kv2` |
| hcp-kv2: `ValidKubeVirtInfraNetworkPolicyRBAC=False` | The SNO-side ClusterRole to read its network config is missing | `oc --kubeconfig <sno> apply -f rendered/hcp-kv2/00-infra-access.yaml` |
| hcp-kv2: console unreachable | The SNO's router doesn't admit the wildcard Route | `oc --kubeconfig <sno> -n hcp-kv2-hcp-kv2 get route`; `oc --kubeconfig <sno> -n openshift-ingress-operator get ingresscontroller default -o jsonpath='{.spec.routeAdmission}'` |
| `HostedCluster` create fails: `x509: certificate signed by unknown authority` from `operator.hypershift.svc` | HyperShift's webhook serving cert isn't signed by its `webhook-serving-ca` | `openssl verify` the `manager-serving-cert` secret against `webhook-serving-ca` in `hypershift`; delete `manager-serving-cert` and `oc -n hypershift rollout restart deployment/operator` |
| `route`: console unreachable | The wildcard Route isn't admitted | `oc -n hcp-kv1-hcp-kv1 get route`; `oc -n openshift-ingress-operator get ingresscontroller default -o jsonpath='{.spec.routeAdmission}'` |
| `metallb`: VMs get no address | The localnet bridge mapping isn't applied, or there's no DHCP on the segment | `oc get nncp kubevirt-lab-bridge-mapping`; `oc -n hcp-kv1-hcp-kv1 get vmi -o wide` |
| `metallb`: `metallb-ingress` stays `<pending>` | Pool or L2Advertisement missing in the hosted cluster | `oc --kubeconfig <hosted> -n metallb-system get ipaddresspool,l2advertisement,pods` |
