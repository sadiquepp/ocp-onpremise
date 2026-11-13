# Standard Clusters (cluster1 and cluster2)

Two independent, general-purpose OpenShift clusters built on the bare-metal
host's KVM/libvirt (UPI: bootstrap, three masters and three workers booted from
RHCOS + ignition), each optionally with **LVM Storage** as its default
StorageClass. Nothing else is installed on top - no ACM, no MetalLB, no OADP.

What a cluster is used for is decided by the playbooks you run on it next:

- [`acm/`](../acm/) turns either one into a hosted-control-plane **hub**
  (ACM/MCE, MetalLB, the OADP operator).
- [`hcp/`](../hcp/) builds hosted clusters on such a hub, and
  [`hcp/dr/`](../hcp/dr/) backs one up from cluster1 and restores it onto
  cluster2 - the **DR** use of cluster2.
- [`ceph/`](../ceph/) attaches Ceph (RHCS 8 or 9, `ceph_version`) storage through ODF instead of LVM
  Storage; [`oadp/`](../oadp/) wires a cluster's OADP to an S3 bucket.

cluster2 is not tied to DR. It is the same kind of cluster as cluster1 on its
own IP block, subdomain and VM/install directories, so it can serve any
purpose that needs a second cluster.

## Contents

- [What lives here](#what-lives-here)
- [Names: cluster1 is `hub`, cluster2 is `hub2`](#names-cluster1-is-hub-cluster2-is-hub2)
- [Before you start](#before-you-start)
- [Choose the cluster's PV storage](#choose-the-clusters-pv-storage)
- [Build cluster1](#build-cluster1)
- [Build cluster2](#build-cluster2)
- [LVM Storage on a cluster that is already up](#lvm-storage-on-a-cluster-that-is-already-up)
- [Shut a cluster down](#shut-a-cluster-down)
- [Next steps](#next-steps)
- [Cleanup](#cleanup)

## What lives here

| File                  | Purpose |
| --------------------- | ------- |
| `setup_cluster1.yaml` | Build cluster1 - connected (`hub`) or, with `disconnected_install: true`, disconnected (`hubd`) - plus LVM Storage when `use_lvm_storage: true` |
| `setup_cluster1_agent.yaml` | Build the same connected cluster1 (`hub`) from an agent ISO instead: six nodes, static addressing, `platform: none` behind the same helper haproxy and DNS names, no new addresses. An alternative to `setup_cluster1.yaml`, not an addition - build one or the other. It exists so the nodes' default gateway can be chosen at install time, which is what puts `br-ex` on the BGP/EVPN fabric ([../udn-bgp-evpn/primary-uplink.md](../udn-bgp-evpn/primary-uplink.md)) |
| `setup_cluster2.yaml` | Build cluster2 - connected (`hub2`) or disconnected (`hub2d`) - plus LVM Storage when `use_lvm_storage: true` |
| `shutdown_cluster.yaml` | Stop a cluster's VMs without deleting anything - cluster1 by default, any of the four with `target_hub` / `disconnected_install` |
| `ansible.cfg`         | Mirrors the root `ansible.cfg` and adds `../roles` to `roles_path` |

The roles stay in the shared [`roles/`](../roles/) directory
(`setup-hub-cluster`, `setup-hub-cluster-disconnected`, `setup-hub-cluster2`,
`setup-hub-cluster2-disconnected`, `setup-lvm-storage`), and the playbooks
share `vars.yaml`, `vault.yaml` and `inventory/hosts` with the rest of the
repository. **Run them from inside this directory**:

```bash
cd standard-cluster
ansible-playbook -i ../inventory/hosts setup_cluster1.yaml --ask-vault-pass
```

Ansible reads `ansible.cfg` from the *working directory*, not the playbook's,
and the one here is what adds `../roles` to the role search path. Run from the
repository root as `ansible-playbook standard-cluster/setup_cluster1.yaml` and
the play fails with `the role 'setup-hub-cluster' was not found`.

> **Upgrading from a checkout with `setup_hub_cluster.yaml` /
> `setup_hub_cluster2.yaml` at the root?** These are those two playbooks,
> renamed, with one change: they **no longer install ACM, MetalLB or the
> OADP operator**. That is [`acm/setup_acm.yaml`](../acm/) now, run after the
> cluster is up. `--tags acm` / `--tags metallb` against the cluster playbook
> become a run of `acm/setup_acm.yaml` (with `--tags metallb` for the latter),
> and `--skip-tags acm` is simply no longer needed.

## Names: cluster1 is `hub`, cluster2 is `hub2`

The variables kept the names they had when both clusters were always hubs, so
in `vars.yaml`, in `ip_list` and in every `target_hub` value:

| Cluster | Connected | Disconnected (`disconnected_install: true`) |
| --- | --- | --- |
| cluster1 | `hub` - `hub_install_folder`, `ip_list.hub_*`, `hub_domain` | `hubd` - `hub_disconnected_install_folder`, `ip_list.hubd_*`, `hub_disconnected_domain` |
| cluster2 | `hub2` - `hub2_install_folder`, `ip_list.hub2_*`, `hub2_domain` | `hub2d` - `hub2_disconnected_install_folder`, `ip_list.hub2d_*` (`.10-.16`, haproxy VIP `.119`), `hub2_disconnected_domain` |

All four are separate clusters with their own IP blocks, subdomains and
VM/install directories, so any combination can be up at once.

## Before you start

- `setup_bm_host.yaml` (repository root) has run: the helper VM provides the
  DNS records, DHCP reservations and haproxy frontends each cluster needs. They
  are rendered from `ip_list`, so a cluster whose `ip_list` entries were added
  after the helper was built needs `setup_bm_host.yaml` (its `dns`/`lb` tags are
  enough) re-run first, or `api`/`api-int`/`*.apps` will not resolve.
- **Disconnected only:** `mirror-registry/setup_mirror_registry.yaml` has run.
  The play fails fast if the mirror registry CA has not been trusted on this
  host. See [Setup Mirror Registry](../README.md#setup-mirror-registry-disconnected-only).
- `disconnected_install` and `use_lvm_storage` are set in `vars.yaml` the way
  you want them - see [Choose Connected or Disconnected](../README.md#choose-connected-or-disconnected)
  and the next section.

## Choose the cluster's PV storage

A cluster needs a PV provider before anything with a PVC (the hosted clusters'
etcd, the AgentServiceConfig, the hello-openshift sample used in the
backup/restore walkthrough) will schedule. There are two options, and the
choice has to be made **before** the cluster is built, because the build acts
on it:

- **LVM Storage** (default) - `use_lvm_storage: true` in `vars.yaml`. Nothing
  else to do: `roles/setup-lvm-storage` installs it as the last step of the
  cluster build (tag `lvm`), right after the cluster operators settle, so the
  cluster comes up with `lvms-vg1` as its default StorageClass on the workers'
  500G `storage_worker<N>.qcow2` disks. Good enough to stand up hosted clusters
  and run workloads on them. The SNO (`sno/setup_sno.yaml`) follows the same
  switch: it gets a second disk and LVM Storage after its install.
- **Ceph (RHCS 8 or 9, `ceph_version`) via ODF external mode** - set `use_lvm_storage: false`, then build
  the Ceph cluster and attach it with the two playbooks in [`ceph/`](../ceph/)
  once the cluster is up, before anything needs a PVC (for a hub: before the
  AgentServiceConfig step in [`acm/`](../acm/README.md#apply-the-agentserviceconfig)).
  See [ceph/README.md](../ceph/README.md) for the whole flow, which release
  to pick (`ceph_version` 9 needs OCP 4.21+, use 8 on 4.20), and why the two should not both run on the same cluster.

> **For hosted-cluster backup and restore, pick Ceph.** Restoring a hosted
> control plane needs a CSI VolumeSnapshot of its etcd volume, and LVM Storage
> provides no VolumeSnapshotClass - so `oadp_backup_method=csi` is not
> available on it at all. See
> [hcp/dr/README.md](../hcp/dr/README.md#choose-how-the-control-plane-volumes-are-captured).

## Build cluster1

```bash
ansible-playbook -i ../inventory/hosts setup_cluster1.yaml --ask-vault-pass
```

One playbook, both modes - which cluster it builds follows
`disconnected_install` in `vars.yaml`:

- **`false`** - installs `roles/setup-hub-cluster` into `hub_install_folder`.
  This is cluster1 (`hub`), pulling from quay.io / registry.redhat.io.
- **`true`** - installs `roles/setup-hub-cluster-disconnected` into
  `hub_disconnected_install_folder` instead. This is `hubd`, a **separate,
  fully independent cluster** rather than a different way of building cluster1:
  its own IP block (`ip_list.hubd_*`), its own subdomain
  (`hub_disconnected_domain`) and its own VM/install directories.

Everything else about a disconnected run follows from the same flag: the
cluster is wired to pull every release and operator image solely from the local
mirror registry, its nodes are cut off from the real internet before anything
boots, and oc-mirror's `ImageDigestMirrorSet`/`ImageTagMirrorSet`/`CatalogSource`
are applied as day-2 resources once the cluster operators are healthy (applied
on day 2 rather than as install manifests, because an `ImageTagMirrorSet` has
no `install-config.yaml` equivalent and the bootstrap and in-cluster MCO would
never agree on the rendered MachineConfig).

The kubeconfig ends up in `<install folder>/auth/kubeconfig`:

```bash
export KUBECONFIG=/var/lib/libvirt/images/hub_install/auth/kubeconfig    # connected
export KUBECONFIG=/var/lib/libvirt/images/hubd_install/auth/kubeconfig   # disconnected
oc get nodes,co
oc get sc        # lvms-vg1 (default) with use_lvm_storage: true
```

## Build cluster2

```bash
ansible-playbook -i ../inventory/hosts setup_cluster2.yaml --ask-vault-pass
```

`setup_cluster2.yaml` follows `disconnected_install` the same way:

- **`false`** - installs `roles/setup-hub-cluster2` into `hub2_install_folder`.
  This is cluster2 (`hub2`), pulling from quay.io / registry.redhat.io.
- **`true`** - installs `roles/setup-hub-cluster2-disconnected` into
  `hub2_disconnected_install_folder`. This is **`hub2d`**, standing in the same
  relation to cluster2 that `hubd` does to cluster1: its own IP block
  (`ip_list.hub2d_*`, `.10-.16`, haproxy VIP `.119`), its own subdomain
  (`hub2_disconnected_domain`, `hub2d.mylab.com`) and its own VM/install
  directories, wired to pull every image from the mirror registry with its
  nodes cut off from the real internet before anything boots.

The disconnected build needs the mirror registry standing (the play fails fast
if its CA is not trusted here) and the `hub2d_*` entries present in `ip_list`
when `setup_bm_host.yaml` last ran.

When cluster2 is the DR hub of [`hcp/dr/`](../hcp/dr/), it is built after
cluster1 has been shut down, and [`acm/setup_acm.yaml`](../acm/) is run against
it with `-e target_hub=hub2` - see
[hcp/dr/README.md](../hcp/dr/README.md#build-the-dr-hub-cluster2).

## LVM Storage on a cluster that is already up

LVM Storage is the last step of the build, tag `lvm`. To add it - and nothing
else - to a cluster that is already running (one built with
`use_lvm_storage: false`, or before LVM Storage moved into the build):

```bash
ansible-playbook -i ../inventory/hosts setup_cluster1.yaml --ask-vault-pass --tags lvm
ansible-playbook -i ../inventory/hosts setup_cluster2.yaml --ask-vault-pass --tags lvm
(cd ../sno && ansible-playbook -i ../inventory/hosts setup_sno.yaml --ask-vault-pass --tags snostorage)    # the SNO's equivalent
```

The rest of the build is not idempotent - `qemu-img create` overwrites an
existing disk and `virt-install` fails on an existing domain - so never re-run
these playbooks without `--tags lvm` against a cluster that is up. Tear it down
first (below).

## Shut a cluster down

`shutdown_cluster.yaml` stops a cluster's VMs - gracefully, or with
`-e force_shutdown=true` as a hard power-off - and deletes nothing: the domains
stay defined, the disks and DNS/LB records stay, and `virsh start` brings the
cluster back. The hosted-cluster DR walkthrough uses it as its "primary hub is
gone" step. It picks the cluster like [`acm/setup_acm.yaml`](../acm/README.md#install-it)
does:

```bash
ansible-playbook -i ../inventory/hosts shutdown_cluster.yaml                                                  # cluster1 (hub)
ansible-playbook -i ../inventory/hosts shutdown_cluster.yaml -e target_hub=hub2                               # cluster2 (hub2)
ansible-playbook -i ../inventory/hosts shutdown_cluster.yaml -e disconnected_install=true                     # hubd
ansible-playbook -i ../inventory/hosts shutdown_cluster.yaml -e target_hub=hub2 -e disconnected_install=true  # hub2d
```

Both default from `vars.yaml`, so check them there if you have set either one
permanently - after a DR cutover with `target_hub: hub2`, a bare run stops
cluster2.

## Next steps

| To | Run |
| --- | --- |
| Make the cluster a hosted-control-plane hub (ACM/MCE, MetalLB, OADP operator) | [`acm/setup_acm.yaml`](../acm/README.md) (`-e target_hub=hub2` for cluster2) |
| Use Ceph instead of LVM Storage | [`ceph/`](../ceph/README.md) |
| Build hosted clusters on the hub | [`hcp/`](../hcp/README.md) |
| Back a hosted cluster up and restore it onto cluster2 | [`hcp/dr/`](../hcp/dr/README.md) |
| Wire OADP to S3 | [`oadp/`](../oadp/README.md) |
| GitOps, UDN/BGP/EVPN, SPIFFE/SPIRE, KubeVirt on cluster1 | [`gitops/`](../gitops/), [`udn-bgp-evpn/`](../udn-bgp-evpn/), [`spiffe-spire/`](../spiffe-spire/), [`kubevirt/`](../kubevirt/) |

## Cleanup

From the repository root - each tag removes one cluster's VMs, VM folder and
install folder and leaves everything else alone:

```bash
ansible-playbook -i inventory/hosts cleanup.yaml --tags hub      # cluster1
ansible-playbook -i inventory/hosts cleanup.yaml --tags hub2     # cluster2
ansible-playbook -i inventory/hosts cleanup.yaml --tags hubd     # disconnected cluster1, plus its staged oc-mirror manifests and egress rules
ansible-playbook -i inventory/hosts cleanup.yaml --tags hub2d    # disconnected cluster2, the same way
```

To stop a cluster without deleting anything, see
[Shut a cluster down](#shut-a-cluster-down).
