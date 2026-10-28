# Ceph 9 Storage for Hub PVs (ODF External Mode)

The hub's PVs come from **LVM Storage** by default - one `LVMCluster` over the
hub nodes' own spare disks, installed by `roles/setup-hub-acm` as part of hub
bring-up. The alternative in this directory builds a **standalone Red Hat Ceph
Storage 9 cluster** on the same bare-metal host and hands it to the hub through
**OpenShift Data Foundation (ODF) in external mode**: ODF is installed on the
hub, but no Ceph daemon runs there - ceph-csi simply talks to the external
cluster's mons and provisions RBD (and CephFS) PVs out of it.

It is a drop-in replacement for LVM Storage as the hub's PV provider, and it
brings what LVM Storage does not: a CSI **VolumeSnapshotClass**
(`ocs-external-storagecluster-rbdplugin-snapclass`), RWX volumes through
CephFS, and storage that lives outside the cluster and survives rebuilding it.

## Contents

- [What lives here](#what-lives-here)
- [When to use it](#when-to-use-it)
- [What gets built](#what-gets-built)
- [Requirements](#requirements)
- [Switching the hub off LVM Storage](#switching-the-hub-off-lvm-storage)
- [Build the Ceph cluster](#build-the-ceph-cluster)
- [Attach it to OpenShift](#attach-it-to-openshift)
- [Verifying](#verifying)
- [Rebuilding the Ceph cluster](#rebuilding-the-ceph-cluster)
- [Cleanup](#cleanup)
- [Playbooks and roles](#playbooks-and-roles)
- [Notes and constraints](#notes-and-constraints)

## What lives here

| File                  | Purpose                                                              |
| --------------------- | -------------------------------------------------------------------- |
| `setup_ceph.yaml`     | Build the standalone Ceph 9 cluster (ceph1-3 + cephadmin)            |
| `setup_ceph_odf.yaml` | Install ODF and attach that Ceph cluster to a hub in external mode   |
| `ansible.cfg`         | Mirrors the root `ansible.cfg` and adds `../roles` to `roles_path`   |
| `ceph-odf/`           | Created at run time by `setup_ceph_odf.yaml` - the extracted exporter script and the connection JSON. **Git-ignored: it holds live cluster credentials** |

Like [`compact-cluster/`](../compact-cluster/) and [`kubevirt/`](../kubevirt/),
the directory shares `vars.yaml`, `vault.yaml`, `inventory/hosts` and `roles/`
with the rest of the repository; the roles themselves stay in the shared
[`roles/`](../roles/) directory. **Run the playbooks from inside this
directory**:

```bash
cd ceph
ansible-playbook -i ../inventory/hosts setup_ceph.yaml --ask-vault-pass
ansible-playbook -i ../inventory/hosts setup_ceph_odf.yaml --ask-vault-pass
```

Ansible reads `ansible.cfg` from the *working directory*, not the playbook's,
and the one here is what adds `../roles` to the role search path. Run from the
repository root as `ansible-playbook ceph/setup_ceph.yaml` and the plays fail
with `the role 'setup-ceph-vm' was not found`.

> **Upgrading from a checkout where these playbooks sat at the repository
> root?** `playbook_dir` is now `ceph/`, so the exporter script and connection
> JSON are written to `ceph/ceph-odf/` instead of `./ceph-odf/`. Nothing reads
> the old directory any more - `setup_ceph_odf.yaml` re-extracts and
> re-exports on every run - so delete it (`rm -rf ceph-odf`) or move it
> (`mv ceph-odf ceph/`). Either way it stays git-ignored.

The Ceph cluster is entirely independent of the HCP flow - no other playbook in
this repo imports `setup_ceph.yaml`, and skipping it changes nothing else.

## When to use it

The choice between LVM Storage and Ceph has to be made **before** the hub is
built, because the hub build acts on it:

- **LVM Storage** (default) - `use_lvm_storage: true` in `vars.yaml`. Nothing
  else to do: `roles/setup-lvm-storage` installs it as part of the cluster
  build itself. Good enough to stand up hosted clusters and run workloads on
  them.
- **Ceph 9 via ODF external mode** - set `use_lvm_storage: false`, then build
  the Ceph cluster and attach it with the two playbooks here, **before** the
  AgentServiceConfig step - that step's PVCs need a default StorageClass to
  exist already.

Pick Ceph when the workload needs something LVM Storage cannot give: volume
snapshots (LVM Storage ships no VolumeSnapshotClass), RWX volumes, or storage
that outlives the cluster consuming it. The
[compact cluster](../compact-cluster/compact-cluster.md) (Step 12b) and the
[KubeVirt hosted clusters](../kubevirt/steps.md) consume it too, with the same
two playbooks.

## What gets built

`setup_ceph.yaml` creates four VMs on the same libvirt network as the rest of
the lab, addressed from `ip_list` in `vars.yaml`:

| VM          | Address          | Role                                                          |
| ----------- | ---------------- | ------------------------------------------------------------- |
| `ceph1`     | `192.168.122.24` | mon + mgr + osd ("all-in-one"); `cephadm bootstrap` runs here |
| `ceph2`     | `192.168.122.25` | mon + mgr + osd                                               |
| `ceph3`     | `192.168.122.26` | mon + mgr + osd                                               |
| `cephadmin` | `192.168.122.27` | `_admin` label only - `ceph.conf` + admin keyring, no daemons |

Each storage node gets `ceph_osd_disks_per_node` raw disks (3 x 100G by
default, sparse qcow2) on top of a 60G OS disk, so a default run is 9 OSDs and
roughly 1.1T of thin-provisioned image files. It is also 80G of RAM
(`ceph_storage_memory` x 3 + `ceph_admin_memory`) and 52 vCPUs - size the
bare-metal host accordingly, or trim those vars first.

`cephadmin` is where `ceph` / `cephadm shell` commands run, and where the ODF
exporter script runs later. It holds the admin keyring but never runs a daemon,
so day-2 work never has to happen on a node that is also serving I/O.

There is no Satellite anywhere in this flow. The nodes register directly with
`subscription-manager` using the same `org_id` / `activation_key` every other VM
in this lab uses, and cephadm pulls
`registry.redhat.io/rhceph/rhceph-9-rhel9:latest` authenticated with the same
`pull_secret`, which `setup-ceph-prereqs` installs as each node's podman
authfile - no separate registry credentials. Without RHCS entitlements, point
`ceph_container_registry` / `ceph_container_image` at the public upstream image
instead; `vars.yaml` carries the values commented in place.

The variables that shape the cluster all live in the root
[`vars.yaml`](../vars.yaml): the sizing knobs under "Ceph path only" in the
START HERE block at the top, everything else in the Ceph block that starts at
`ceph_storage_node_names`:

| Variable | Default | What it controls |
| -------- | ------- | ---------------- |
| `ip_list.ceph1`-`ceph3`, `ip_list.cephadmin` | `24`-`26`, `27` | Last octet of each node's address |
| `ceph_osd_disks_per_node` / `ceph_osd_disk_size` | `3` / `100G` | Raw OSD disks per storage node, and their size |
| `ceph_storage_memory` / `ceph_storage_vcpus` | `24576` / `16` | RAM (MiB) and vCPUs per storage node |
| `ceph_admin_memory` / `ceph_admin_vcpus` | `8192` / `4` | RAM (MiB) and vCPUs for `cephadmin` |
| `ceph_os_disk_size` | `60G` | OS disk per node |
| `ceph_container_registry` / `ceph_container_image` | `registry.redhat.io` / `rhceph/rhceph-9-rhel9:latest` | Image cephadm deploys |
| `ceph_dashboard_user` | `admin` | Dashboard admin user (password in `vault.yaml`) |
| `ceph_odf_rbd_pool` | `ocs-storagecluster-cephblockpool` | RBD pool exported to ODF |
| `ceph_odf_enable_cephfs` / `ceph_odf_cephfs_name` | `true` / `ocs-storagecluster-cephfilesystem` | Whether a CephFS (and MDS) is exported too |
| `ceph_odf_storagecluster_name` | `ocs-external-storagecluster` | External `StorageCluster` name - StorageClass names derive from it |
| `ceph_odf_rbd_default_sc` | `true` | Mark the RBD StorageClass the cluster default |
| `ceph_odf_export_dir` | `{{ playbook_dir }}/ceph-odf` | Where the connection JSON lands on the controller (`ceph/ceph-odf/`) |

## Requirements

**OCP 4.21 or later on the cluster consuming the storage.** On **4.20**, the
in-kernel RBD client could not attach to an RHCS 9
(Tentacle) cluster: `rbd map` failed with `failed to add secret to kernel` and
the CSI attach ended in error 524, so the PV bound but never mounted. This is a
client-side kernel gap - the cluster health, the CSI user's caps and the image
features all checked out, and the same image mapped by hand from a RHEL 9 node.
It works on **4.21** (krbd mounts RHCS 9 images: `/dev/rbd0` under the NooBaa
database, 2026-10-23) and on **4.22**. On any other version, run the RBD smoke
test under [Verifying](#verifying) before building anything on the storage.
`ocp_major_version` in `vars.yaml` drives both the hub's OCP version and the ODF
channel the consumer role subscribes to (`stable-{{ ocp_major_version }}`), so
changing it moves both together.

On a version where the smoke test fails, take krbd out of the path and mount through
rbd-nbd in userspace instead, by giving the RBD StorageClass
`parameters.mounter: rbd-nbd`. StorageClass parameters are immutable, so this
means exporting the generated class, renaming it, adding the parameter and
applying that as a second class - not patching the one ODF created:

```bash
oc get sc ocs-external-storagecluster-ceph-rbd -o yaml > rbd-nbd-sc.yaml
# edit: metadata.name -> ocs-external-storagecluster-ceph-rbd-nbd,
#       drop metadata.uid/resourceVersion/creationTimestamp,
#       add   parameters.mounter: rbd-nbd
oc apply -f rbd-nbd-sc.yaml
```

Not needed on 4.22+, and not something this repo renders for you.

**vault.yaml** reuses what the lab already requires - `org_id`,
`activation_key` and `pull_secret` - and adds one new value,
`ceph_dashboard_password`: the initial password `cephadm bootstrap` sets for
the Ceph dashboard's admin user (`ceph_dashboard_user` in `vars.yaml`).
`setup_ceph.yaml` asserts all of them up front rather than failing halfway
through a bootstrap.

```bash
ansible-vault edit vault.yaml     # from the repository root
```

```yaml
ceph_dashboard_password: '...'
```

**inventory/hosts** needs the `ceph`, `ceph_admin` and `ceph_nodes` groups that
`setup_ceph.yaml`'s second play targets. They are rendered from
`inventory/hosts.j2` by `setup_bm_host.yaml`, so an inventory generated before
the Ceph groups existed has to be re-rendered. From the repository root:

```bash
ansible-playbook -i inventory/hosts setup_bm_host.yaml --ask-vault-pass
```

The ODF consumer plays also need `oc` in `/usr/bin` on the hypervisor - every
hub build puts it there; on a hypervisor that has never built a hub, copy it
in from the cluster's install directory first.

## Switching the hub off LVM Storage

`use_lvm_storage` in `vars.yaml` gates `roles/setup-lvm-storage` - the LVM
Storage operator Subscription and the `LVMCluster` CR - in every hub build and
the SNO's storage step:

```yaml
use_lvm_storage: false
```

Set it before building the hub. Flipping it on an existing hub does not
uninstall anything - the role only skips those two steps on the next run, so an
already-installed LVM Storage has to be removed by hand.

That matters because **LVM Storage and ODF both want the `openshift-storage`
namespace**. LVM Storage's operator, its `LVMCluster`, ODF's operator and the
external `StorageCluster` all land there, and this repo has not tested them
sharing it. Run one or the other on a given hub:

- switch the hub to Ceph from the start (`use_lvm_storage: false`), or
- remove LVM Storage from the hub before running `setup_ceph_odf.yaml`, or
- leave the hub on LVM Storage and point the ODF plays at a different cluster
  entirely: `-e ceph_odf_target_kubeconfig=/path/to/other/kubeconfig`.

If the two do end up coexisting, at least keep the default StorageClass
unambiguous by setting `ceph_odf_rbd_default_sc: false`.

## Build the Ceph cluster

```bash
cd ceph
ansible-playbook -i ../inventory/hosts setup_ceph.yaml --ask-vault-pass
```

Three ordered plays, tagged so each can be re-run on its own
(`--tags ceph-vm`, `rhsm`, `ceph-prereqs`, `ceph-cluster`):

1. **`setup-ceph-vm`** creates the four VMs from `rhel9_kvm_image`, expands the
   OS partition into a 60G disk, and attaches the raw OSD disks to ceph1-3.
2. **`setup-rhsm` + `setup-ceph-prereqs`** register each node directly with
   `subscription-manager`, enable `rhceph_tools_repo`, install `cephadm` and
   `ceph-common`, and drop `pull_secret` as the node's podman authfile.
3. **`setup-ceph-cluster`** (delegated to ceph1) runs `cephadm bootstrap`,
   distributes the cluster's SSH key, adds ceph2/ceph3 as `mon,mgr,osd` hosts
   and `cephadmin` as `_admin`, places mon/mgr by label, and deploys OSDs on
   every available device.

The third play is a one-shot cluster **creation** role, not a day-2 reconciler:
everything after the bootstrap check is gated on `/etc/ceph/ceph.conf` not
already existing. Re-running against a live cluster re-checks the prereqs and
leaves the cluster alone. The bootstrap output - including the dashboard URL -
is printed on the first run only.

```bash
ssh root@192.168.122.27 ceph -s
ssh root@192.168.122.27 ceph orch host ls
ssh root@192.168.122.27 ceph osd tree
```

Expect `HEALTH_OK` with 3 mons, 3 mgrs (1 active, 2 standby) and 9 OSDs before
moving on - the export step will happily produce a JSON blob against a degraded
cluster, and you will find out later.

## Attach it to OpenShift

```bash
cd ceph
ansible-playbook -i ../inventory/hosts setup_ceph_odf.yaml --ask-vault-pass
```

Targets the same hub as every other playbook here (`target_hub`, defaulting to
`hub`); pass `-e target_hub=hub2` for the second hub, or
`-e ceph_odf_target_kubeconfig=...` for something else entirely (the compact
cluster, for example). Three ordered plays, because ODF has to be installed
*before* the export can run:

1. **`setup-ceph-odf-consumer`, stage `install`** (`--tags ceph-odf-install`) -
   creates `openshift-storage`, subscribes to the ODF operator on channel
   `stable-{{ ocp_major_version }}`, waits for the `StorageCluster` CRD, enables
   the `odf-console` plugin, and extracts the exporter script **from the
   installed operator** - the ConfigMap `rook-ceph-external-cluster-script-config`
   (`.data.script`) on ODF 4.19+, falling back to the
   `external.features.ocs.openshift.io/export-script` CSV annotation on 4.18 and
   below.
2. **`setup-ceph-odf-export`** (`--tags ceph-odf-export`, delegated to
   `cephadmin`) - creates the RBD pool `ceph_odf_rbd_pool` and, if
   `ceph_odf_enable_cephfs`, the CephFS `ceph_odf_cephfs_name` plus an MDS to
   serve it. The exporter creates *users*, not pools, so these have to exist
   first. It then runs the version-matched script with `--v2-port-enable` and
   fetches the resulting JSON - fsid, mon endpoints, scoped CSI credentials - to
   `ceph_odf_export_dir` (`ceph/ceph-odf/`) on the controller.
3. **`setup-ceph-odf-consumer`, stage `create`** (`--tags ceph-odf-create`) -
   loads that JSON into the `rook-ceph-external-cluster-details` secret and
   applies the external `StorageCluster` named `ceph_odf_storagecluster_name`.

The exported JSON holds live cluster credentials. `ceph-odf/` is in
`.gitignore`; keep it that way.

To attach more than one cluster, run `setup_ceph_odf.yaml` once per cluster,
each with its own `target_hub` or `ceph_odf_target_kubeconfig`. Each run
overwrites `ceph-odf/external-cluster-details.json`, which is fine - the file is
only read by the same run's `create` stage.

## Verifying

```bash
oc get storagecluster -n openshift-storage
oc get pods -n openshift-storage
oc get sc
```

A healthy external `StorageCluster` reaches `Ready`, and `oc get sc` shows
`ocs-external-storagecluster-ceph-rbd` (marked `(default)` when
`ceph_odf_rbd_default_sc` is true) alongside
`ocs-external-storagecluster-cephfs`. Then prove it end to end - binding is not
mounting, and the kernel issue under [Requirements](#requirements) only shows
up on the mount:

```bash
oc apply -f - <<'EOF'
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: ceph-rbd-smoke
spec:
  accessModes: [ReadWriteOnce]
  resources:
    requests:
      storage: 1Gi
  storageClassName: ocs-external-storagecluster-ceph-rbd
---
apiVersion: v1
kind: Pod
metadata:
  name: ceph-rbd-smoke
spec:
  containers:
    - name: smoke
      image: registry.access.redhat.com/ubi9/ubi-minimal
      command: ["sleep", "3600"]
      volumeMounts:
        - name: vol
          mountPath: /data
  volumes:
    - name: vol
      persistentVolumeClaim:
        claimName: ceph-rbd-smoke
EOF

oc get pvc ceph-rbd-smoke
oc wait --for=condition=Ready pod/ceph-rbd-smoke --timeout=180s
oc exec ceph-rbd-smoke -- sh -c 'echo ok > /data/f && cat /data/f'
```

A PVC that goes `Bound` while the pod stays `ContainerCreating` with
`rbd: map failed ... failed to add secret to kernel` is the kernel issue
above (seen on 4.20; 4.21 and 4.22 work), not a Ceph or credentials problem.

Clean up the smoke test afterwards:

```bash
oc delete pod/ceph-rbd-smoke pvc/ceph-rbd-smoke
```

The hub also needs a **default StorageClass** before the AgentServiceConfig is
applied: its three PVCs (`databaseStorage` 10Gi, `filesystemStorage` 100Gi,
`imageStorage` 50Gi) name no `storageClassName`, so without a default they sit
`Pending` and assisted-service never starts. On the Ceph path that default is
`ocs-external-storagecluster-ceph-rbd`, which is why `ceph_odf_rbd_default_sc`
defaults to true:

```bash
oc get sc      # exactly one class marked (default)
```

## Rebuilding the Ceph cluster

`setup_ceph.yaml` never reconfigures a live cluster, so a fresh cluster means
destroying the VMs first. From the repository root:

```bash
ansible-playbook -i inventory/hosts cleanup.yaml --tags ceph
```

Verify it actually removed everything - a partial teardown leaves stale
OSD disks that the rebuilt cluster cannot claim, and you get a Ceph
cluster with no OSDs:

```bash
virsh list --all | grep -E 'ceph[123]|cephadmin'          # expect no rows
ls /var/lib/libvirt/images/ | grep -E '^ceph|^cephadmin'  # expect nothing
```

If anything is left, remove it by hand. Then build again - a brand-new
cluster on the same four VMs, with no pool, image or CSI user carried over:

```bash
cd ceph
ansible-playbook -i ../inventory/hosts setup_ceph.yaml --ask-vault-pass
```

Wait for `HEALTH_OK` with 3 mons and 9 OSDs (`ssh root@192.168.122.27 ceph -s`),
then re-run `setup_ceph_odf.yaml` against each cluster that should consume it.
The StorageClass names derive from `ceph_odf_storagecluster_name`, so the
rebuilt cluster presents `ocs-external-storagecluster-ceph-rbd` and
`ocs-external-storagecluster-cephfs` under the same names as before.

## Cleanup

Ceph has its own tag in the repository-wide cleanup playbook, which removes the
four VMs and their OS and OSD disks and touches nothing else. **Every PV
provisioned from the cluster is lost with it.** From the repository root:

```bash
ansible-playbook -i inventory/hosts cleanup.yaml --tags ceph
```

That does not uninstall ODF from the clusters it was attached to; remove the
`StorageCluster` and the ODF operator from `openshift-storage` by hand if the
cluster is staying.

## Playbooks and roles

| Playbook / role | Purpose |
| --------------- | ------- |
| [`setup_ceph.yaml`](setup_ceph.yaml) | Build the standalone Ceph 9 cluster (ceph1-3 + cephadmin) |
| [`setup_ceph_odf.yaml`](setup_ceph_odf.yaml) | Install ODF and attach that Ceph cluster to a hub in external mode |
| [`roles/setup-ceph-vm`](../roles/setup-ceph-vm) | Creates the ceph1-3 + cephadmin VMs and attaches the raw OSD disks |
| [`roles/setup-rhsm`](../roles/setup-rhsm) | Registers each node with subscription-manager (shared with the rest of the lab) |
| [`roles/setup-ceph-prereqs`](../roles/setup-ceph-prereqs) | Enables the RHCS 9 tools repo, installs cephadm, and installs `pull_secret` as each node's podman authfile |
| [`roles/setup-ceph-cluster`](../roles/setup-ceph-cluster) | Runs `cephadm bootstrap` on ceph1, expands the cluster onto ceph2/ceph3 + cephadmin, places mon/mgr/osd daemons, and enables msgr2 |
| [`roles/setup-ceph-odf-export`](../roles/setup-ceph-odf-export) | Creates the RBD pool (+ optional CephFS) and runs the version-matched exporter on cephadmin to produce the connection JSON |
| [`roles/setup-ceph-odf-consumer`](../roles/setup-ceph-odf-consumer) | Installs the ODF operator and extracts its exporter script (stage `install`), then creates the external-cluster secret and `StorageCluster` (stage `create`) |

## Notes and constraints

- **The exporter script must match the installed operator.** Do not substitute
  a copy from a GitHub branch. An older upstream script emits the CSI secrets
  as `adminID`/`adminKey`, while a current ODF operator reads `userID`/`userKey`
  - the operator finds no usable credential, falls back to DNS SRV mon
  discovery, and the `StorageCluster` sits in `Progressing` with
  `unable to get monitor info from DNS SRV` / `RADOS object not found`. Nothing
  says "wrong key names". This is why `setup_ceph_odf.yaml` installs ODF first
  and pulls the script out of the operator, and why `ceph_odf_exporter_local_path`
  points at a file the playbook writes rather than one you supply.
- **Do not add `reconcileStrategy: ignore` to the StorageCluster's
  `managedResources.cephBlockPools`.** It reads like the right thing in external
  mode - the operator is not managing the pools - but what it actually suppresses
  is the *StorageClass* creation the operator is responsible for, so no
  StorageClass ever appears and nothing can provision. The template deliberately
  sets only `defaultStorageClass` (from `ceph_odf_rbd_default_sc`), which is what
  the ODF console generates.
- **The mons have to be answering msgr2.** The exporter runs with
  `--v2-port-enable`, so the endpoints it hands ODF are the v2 port 3300; if the
  mons are only bound to the v1 port 6789 those endpoints are dead and CSI
  cannot reach the cluster. A Ceph 9 cluster bootstrapped by cephadm has msgr2
  on already - confirm with `ceph mon dump` (each mon should show a `v2:` entry)
  and, in the unlikely case it does not, `ceph mon enable-msgr2` turns it on.
- The Ceph cluster is entirely independent of the HCP flow - no other playbook
  in this repo imports `setup_ceph.yaml`, and skipping it changes nothing else.
- `setup_ceph_odf.yaml` can be pointed at more than one cluster, but each
  export re-runs the exporter against the same Ceph cluster, so every consumer
  ends up on the same pool. That is fine for a lab; it is not isolation.
