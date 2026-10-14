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
  re-run `setup_hub_cluster.yaml --tags acm` and `setup_bm_host.yaml --tags dns`.
- **Ceph RBD as the default StorageClass** (step 7, with
  `use_lvm_storage: false`). CDI gives the worker root disks ReadWriteMany
  block, so the VMs live-migrate when a hub node drains, and kubevirt-csi
  volumes inside the hosted cluster come from the same pool. A hub on LVM
  Storage works too, but its RWO disks can't migrate, so a drain stops the
  workers on that node.
- **Nested KVM** on the host. Step 4 writes `options kvm-intel nested=1`; it
  takes effect once the module reloads, so reboot the host if
  `/sys/module/kvm_intel/parameters/nested` isn't `Y`. On an AMD host, use
  `kvm-amd`.
- **Room on the hub.** Its three workers are 32 vCPU / 48 GiB each, and carry
  both the hosted control plane and the worker VMs (4 vCPU / 16 GiB each by
  default).

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
| `preflight` | Checks only: MCE and MetalLB on the hub, each cluster's API pool, nested KVM, `api.<name>` resolving to its address (and `*.apps` for MetalLB clusters), a StorageClass for the disks |
| `mgmt` | Hub: OpenShift Virtualization (and a check that some node can run VMs); wildcard routes (`route`); NMState + localnet bridge mapping (`metallb`) |
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
`setup_hub_cluster.yaml --tags acm` (its API pool), and this playbook:

```yaml
hosted_clusters:
  - name: hcp-kv2
    platform: kubevirt
    nodepool_replicas: 3          # optional overrides - see kubevirt/vars.yaml
hosted_cluster_metallb_pools:
  hcp-kv2:
    pool: hcp-kv2-api-pool
    ip:
      hub: 252                    # .250-.254 are the free octets
hosted_cluster_domains:
  hcp-kv2: "{{ hub_domain }}"     # resolvable from every VM with no network change
hosted_cluster_node_keys:
  hcp-kv2:
    apps_vip: 253                 # only used with ingress: metallb
```

The cluster CIDRs (`10.132.0.0/14`, `172.31.0.0/16`) are internal to each
hosted cluster, so every cluster can reuse them. They must not overlap the
hub's own pod and service networks.

## What it changes outside this directory

The playbook, both roles, their templates and `vars.yaml` are all here. The
rest of the lab knows about KubeVirt clusters through a few lines:

- **Root `vars.yaml`**: hcp-kv1's entries in `hosted_clusters`,
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

## Troubleshooting

| Symptom | Likely cause | Check |
|---|---|---|
| Preflight: `nested` is not `Y` | The module was loaded before `kvm-nested.conf` existed | Stop the VMs, `modprobe -r kvm_intel && modprobe kvm_intel`, or reboot |
| No node labelled `kubevirt.io/schedulable=true` | The hub VMs don't see VMX/SVM | `oc debug node/<worker> -- grep -c -E 'vmx\|svm' /proc/cpuinfo`; the hub VMs need `--cpu host-passthrough` |
| VMs stay `Pending`/`Provisioning` | Root-disk PVC unbound, or no room | `oc -n hcp-kv1-hcp-kv1 get dv,pvc,vmi`; `oc describe` the virt-launcher pod |
| HostedCluster waits on kube-apiserver with no address | The API pool is missing, or holds another address | `oc -n metallb-system get ipaddresspool hcp-kv1-api-pool -o yaml`; `oc -n hcp-kv1-hcp-kv1 get svc kube-apiserver` |
| Nodes never join | The VMs can't fetch Ignition or reach the API | `virtctl console -n hcp-kv1-hcp-kv1 <vm>`; from the VM, `curl -k https://api.hcp-kv1.hub.mylab.com:6443/version` |
| Nodes `Ready,SchedulingDisabled`; routers and console Pending | Auto-repair is replacing the workers (NotReady past `kubevirt_mhc_timeout`) and the drain hangs | `oc -n hcp-kv1-hcp-kv1 get machines.cluster.x-k8s.io` shows `Deleting`; raise the NodePool's `hypershift.openshift.io/machine-health-check-timeout`, then `oc annotate machines.cluster.x-k8s.io -n hcp-kv1-hcp-kv1 --all machine.cluster.x-k8s.io/exclude-node-draining=""` |
| `HostedCluster` create fails: `x509: certificate signed by unknown authority` from `operator.hypershift.svc` | HyperShift's webhook serving cert isn't signed by its `webhook-serving-ca` | `openssl verify` the `manager-serving-cert` secret against `webhook-serving-ca` in `hypershift`; delete `manager-serving-cert` and `oc -n hypershift rollout restart deployment/operator` |
| `route`: console unreachable | The wildcard Route isn't admitted | `oc -n hcp-kv1-hcp-kv1 get route`; `oc -n openshift-ingress-operator get ingresscontroller default -o jsonpath='{.spec.routeAdmission}'` |
| `metallb`: VMs get no address | The localnet bridge mapping isn't applied, or there's no DHCP on the segment | `oc get nncp kubevirt-lab-bridge-mapping`; `oc -n hcp-kv1-hcp-kv1 get vmi -o wide` |
| `metallb`: `metallb-ingress` stays `<pending>` | Pool or L2Advertisement missing in the hosted cluster | `oc --kubeconfig <hosted> -n metallb-system get ipaddresspool,l2advertisement,pods` |
