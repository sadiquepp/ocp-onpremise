# Hosted Cluster Backup & Restore (DR to a second hub)

Backs up a hosted cluster's control plane on **cluster1** (the primary hub)
with OADP/Velero to S3, takes cluster1 away, and restores the hosted cluster
onto **cluster2** (the DR hub) - with its etcd intact and its workloads
running. On the Ceph path the storage the backup came from is destroyed and
rebuilt in between, so the restored data can only have come from S3.

- What it proves, in one page: [summary.md](summary.md).
- The same run as commands only: [steps.md](steps.md).
- The production-shaped version of the procedure:
  [hosted-cluster-dr-runbook.md](hosted-cluster-dr-runbook.md).

```
 cluster1 (hub)                          cluster2 (hub2)
 ┌─────────────────────┐   S3 bucket    ┌─────────────────────┐
 │ ACM/MCE, OADP       │  ┌─────────┐   │ ACM/MCE, OADP       │
 │ hcp-cluster1 CP ────┼─►│ backup  │──►│ hcp-cluster1 CP     │
 └─────────────────────┘  └─────────┘   └─────────────────────┘
        shut down                          DNS cut over here
```

## Contents

- [What lives here](#what-lives-here)
- [The flow](#the-flow)
- [Choose how the control-plane volumes are captured](#choose-how-the-control-plane-volumes-are-captured)
- [Primary hub (cluster1)](#primary-hub-cluster1)
  - [Configure OADP](#configure-oadp)
  - [Exclude ACM's import secret from the backup](#exclude-acms-import-secret-from-the-backup)
  - [Backup a hosted cluster](#backup-a-hosted-cluster)
  - [What a good `csi` run looks like](#what-a-good-csi-run-looks-like)
  - [Shut down the primary hub](#shut-down-the-primary-hub)
  - [Destroy the Ceph cluster (Ceph/CSI DR demo)](#destroy-the-ceph-cluster-cephcsi-dr-demo)
- [DR hub (cluster2)](#dr-hub-cluster2)
  - [Rebuild Ceph for the DR hub (Ceph/CSI DR demo)](#rebuild-ceph-for-the-dr-hub-cephcsi-dr-demo)
  - [Build the DR hub (cluster2)](#build-the-dr-hub-cluster2)
  - [Configure OADP on the DR hub](#configure-oadp-on-the-dr-hub)
  - [Restore the hosted cluster](#restore-the-hosted-cluster)
  - [What a good `csi` restore looks like](#what-a-good-csi-restore-looks-like)
  - [Restored cluster stuck in `Importing`](#restored-cluster-stuck-in-importing)
  - [Point DNS at the DR hub](#point-dns-at-the-dr-hub)
  - [Verify the hosted cluster came back](#verify-the-hosted-cluster-came-back)
- [Templates](#templates)
- [Related tests](#related-tests)

## What lives here

| File | Purpose |
| --- | --- |
| `backup_hosted_cluster.yaml` | Back up a hosted cluster's control plane (Velero `Backup`, fs or csi) and wait for it |
| `restore_hosted_cluster.yaml` | Restore it onto another hub (Velero `Restore`), optionally with its BareMetalHosts detached |
| `templates/` | The Backup/Restore manifests the two playbooks render - one pair per backup method, plus the BareMetalHost detach modifier |
| `steps.md` | The whole walkthrough as commands only |
| `summary.md` | What the walkthrough proves, in one page |
| `hosted-cluster-dr-runbook.md` | The procedure written as a production runbook |
| `BMH-DR-test.md` | The same DR flow with workers managed as BareMetalHosts |
| `OCPBUGS-121709-test.md` | Test record for the hypershift plugin fix that would retire the import-secret workaround |
| `ansible.cfg` | Mirrors the root `ansible.cfg` |

The playbooks share `vars.yaml`, `vault.yaml` and `inventory/hosts` with the
rest of the repository. **Run them from inside this directory**:

```bash
cd hcp/dr
ansible-playbook -i ../../inventory/hosts backup_hosted_cluster.yaml --ask-vault-pass \
  -e hcp_cluster_name=hcp-cluster1
```

The commands below assume you are in `hcp/dr/`; steps that belong to another
directory are written as `(cd ../../<dir> && ...)`.

The generic OADP pieces - the S3 bucket and IAM user, `setup_oadp.yaml`, the
fs/csi comparison, the hello-openshift smoke test and OADP troubleshooting - are
in [`oadp/`](../../oadp/README.md), because they are not specific to hosted
clusters.

## The flow

| # | Where | What |
| --- | --- | --- |
| 1 | repository root | bare-metal host, helper (`setup_bm_host.yaml`); Ceph (`ceph/`) on the Ceph path |
| 2 | [`standard-cluster/`](../../standard-cluster/) | cluster1 |
| 3 | [`acm/`](../../acm/) | ACM/MCE, MetalLB, OADP operator on cluster1; AgentServiceConfig |
| 4 | [`hcp/`](../) | InfraEnv, worker VMs, `hcp-cluster1`, a workload on it |
| 5 | [`oadp/`](../../oadp/) | OADP wired to S3 on cluster1 |
| 6 | here, `standard-cluster/` | exclude the import secret, back up; shut cluster1 down (`shutdown_cluster.yaml`) |
| 7 | `ceph/` | destroy and rebuild Ceph (Ceph path) |
| 8 | `standard-cluster/`, `acm/`, `ceph/`, `oadp/` | cluster2, ACM on it, Ceph attached, AgentServiceConfig, OADP |
| 9 | here | restore, then cut DNS over to cluster2 |

In variable names and `target_hub` values cluster1 is `hub` and cluster2 is
`hub2`; the backup/restore playbooks read `target_hub` to pick the kubeconfig.

## Choose how the control-plane volumes are captured

The hosted control plane's state is etcd, on a PVC in the HyperShift
control-plane namespace. `oadp_backup_method` in `vars.yaml` decides how
Velero captures it, and every command below takes it as an override:

- **`fs`** (default) - Kopia file-system backup
  (`defaultVolumesToFsBackup: true`). Velero's node-agent mounts the
  volume and streams the files to S3. Runs on any StorageClass, LVM
  Storage included - but a file-by-file copy of a live etcd volume is not a
  point-in-time image of it, which is the property a control-plane restore
  depends on. Completing is not the same as being restorable.
- **`csi`** - CSI VolumeSnapshot plus Velero's data mover
  (`snapshotVolumes` + `snapshotMoveData` + `datamover: velero`). The
  storage layer takes a point-in-time snapshot and the data mover copies
  it to the same S3 bucket. Needs a CSI driver with snapshot support -
  which on this lab means the hub is on Ceph/ODF rather than LVM
  Storage (`use_lvm_storage: false`), and is the reason
  [the Ceph cluster](../../ceph/README.md) exists.

Treat LVM Storage as the option for demonstrating hosted clusters, and Ceph as
the one for demonstrating backup and restore.

Each method has its own Backup/Restore templates in [`templates/`](#templates)
and its own object names (`<cluster>-backup` vs `<cluster>-backup-csi`),
so both can be demonstrated against one bucket. The bucket, the
credentials and the DataProtectionApplication are shared - switching
methods changes nothing else. The full comparison and what `csi` needs:
[oadp/README.md, Backup methods](../../oadp/README.md#backup-methods-fs-vs-csi).

## Primary hub (cluster1)

Starting point: cluster1 with ACM on it, `hcp-cluster1` running with a
workload ([hcp/README.md](../README.md)), and - on the Ceph path - Ceph attached
to cluster1.

### Configure OADP

The operator is already there (installed by `acm/setup_acm.yaml`). Point it at
your bucket - for the CSI method pass it here too, so the role labels the
VolumeSnapshotClass Velero selects snapshot classes by:

```bash
(cd ../../oadp && ansible-playbook -i ../inventory/hosts setup_oadp.yaml --ask-vault-pass -e oadp_backup_method=csi)
oc get volumesnapshotclass -L velero.io/csi-volumesnapshot-class
```

The one-time S3/IAM setup and the details are in
[oadp/README.md](../../oadp/README.md#configure-oadp-on-a-cluster). Before
backing up a hosted cluster you can sanity-check OADP itself with the
[hello-openshift smoke test](../../oadp/README.md#smoke-test-hello-openshift-with-a-pvc).

### Exclude ACM's import secret from the backup

Do this **before** the backup, on the hub being backed up.

```bash
oc label secret hcp-cluster1-import -n hcp-cluster1 \
  velero.io/exclude-from-backup=true --overwrite
oc get secret hcp-cluster1-import -n hcp-cluster1 --show-labels
```

`hcp-cluster1` is the HostedCluster's namespace and *also* ACM's
ManagedCluster namespace, so `hcp-cluster1-import` - which carries a
bootstrap ServiceAccount token minted by this hub - is inside the
backup's scope. Restoring it hands the DR hub a token signed by the wrong
cluster's key, and the restored cluster hangs in `Importing` with the
klusterlet logging `Unauthorized` until the secret is deleted by hand. It
is also a 360-day credential that has no business sitting in S3.

`velero.io/exclude-from-backup` is a **label**, not an annotation -
`oc annotate` applies cleanly and does nothing. Velero reads it while
collecting items, so it only has to be set at backup time; nothing is
needed on the DR hub.

MCE owns this secret and reconciles it, so check `--show-labels` before
each backup rather than assuming the label stuck - a backup that quietly
re-includes it looks healthy right up until the restore fails.

This is a workaround, not the fix. Filed as
[OCPBUGS-121709](https://redhat.atlassian.net/browse/OCPBUGS-121709) -
**refer to that JIRA for the current status**, and keep labelling the secret on
every backup until it is resolved in the ACM/MCE version you are running.
Full write-up: [Restored cluster stuck in `Importing`](#restored-cluster-stuck-in-importing);
the fix's test record: [OCPBUGS-121709-test.md](OCPBUGS-121709-test.md).

### Backup a hosted cluster

```bash
ansible-playbook -i ../../inventory/hosts backup_hosted_cluster.yaml --ask-vault-pass \
  -e hcp_cluster_name=hcp-cluster1
```

`hcp_cluster_name` must match the HostedCluster's name/namespace - it's
used to derive both the hosting namespace and the HyperShift
control-plane namespace (`<name>-<name>`). Works unchanged for
`hcp-cluster2` or any future hosted cluster.

For a CSI snapshot backup (Backup named `hcp-cluster1-backup-csi`):

```bash
ansible-playbook -i ../../inventory/hosts backup_hosted_cluster.yaml --ask-vault-pass \
  -e hcp_cluster_name=hcp-cluster1 -e oadp_backup_method=csi
```

The playbook checks the BackupStorageLocation is `Available`, and for csi that
a VolumeSnapshotClass is labelled for Velero, before it applies anything, then
reports the `DataUpload` objects alongside the final phase. The volume data
moves in `DataUpload`s rather than inside the Backup's own progress counters,
so a csi backup sits in `WaitingForPluginOperations` for a while - watch it
with:

```bash
oc get datauploads.velero.io -n openshift-adp -w
```

Get the status of the backup and wait till it finishes before proceeding:

```bash
oc get backup.velero.io -n openshift-adp hcp-cluster1-backup -o yaml
```

It should show `phase: Completed`, with `itemsBackedUp` equal to `totalItems`:

```yaml
status:
  expiration: "2026-09-28T04:52:01Z"
  formatVersion: 1.1.0
  phase: Completed
  progress:
    itemsBackedUp: 363
    totalItems: 363
  startTimestamp: "2026-08-17T13:52:01Z"
  version: 1
```

### What a good `csi` run looks like

A known-good run of `hcp-cluster1` on this lab, for comparison when
something looks off:

```yaml
status:
  backupItemOperationsAttempted: 3
  backupItemOperationsCompleted: 3
  completionTimestamp: "2026-09-08T03:47:54Z"
  formatVersion: 1.1.0
  phase: Completed
  progress:
    itemsBackedUp: 374
    totalItems: 374
  startTimestamp: "2026-09-08T03:42:30Z"
  version: 1
```

```
$ oc get datauploads.velero.io -n openshift-adp
NAME                            STATUS      STARTED   BYTES DONE   TOTAL BYTES   STORAGE LOCATION   AGE     NODE
hcp-cluster1-backup-csi-nnb2t   Completed   8m42s     371423568    371423568     default            9m12s   worker1
hcp-cluster1-backup-csi-x76zq   Completed   8m35s     370738865    370738865     default            9m7s    worker3
hcp-cluster1-backup-csi-xdctc   Completed   8m31s     370678113    370678113     default            9m2s    worker2
```

What to check, in order of how much it tells you:

- **One `DataUpload` per etcd member**, three for a standard hosted
  control plane, each on a different worker. Their sizes should be close
  to each other - the members hold the same data - and `BYTES DONE` must
  equal `TOTAL BYTES`.
- **`backupItemOperationsCompleted` equal to `...Attempted`**, and equal
  to the number of volumes. These are the asynchronous CSI operations;
  this is the field that says the data actually moved, where
  `itemsBackedUp` only counts objects written to the archive.
- **`phase: Completed`** last. It is the weakest of the three signals:
  a backup can reach `Completed` having snapshotted nothing (if Velero
  skipped the volumes), and can read `PartiallyFailed` while the volume
  data is perfectly fine (if an unrelated item action errored).

Roughly five minutes wall clock for ~1.1G of etcd across three members,
against the Ceph cluster on the same bare-metal host.

No `DataUpload` rows at all is the failure worth recognising - see
[oadp/README.md, If no `DataUpload` is created at all](../../oadp/README.md#if-no-dataupload-is-created-at-all).

### Shut down the primary hub

```bash
(cd ../../standard-cluster && ansible-playbook -i ../inventory/hosts shutdown_cluster.yaml)
(cd ../../standard-cluster && ansible-playbook -i ../inventory/hosts shutdown_cluster.yaml -e force_shutdown=true)   # hard power-off instead of graceful
```

`standard-cluster/shutdown_cluster.yaml` stops a standard cluster's VMs -
cluster1 by default; it follows `target_hub` and `disconnected_install` like
the other playbooks. Unlike `cleanup.yaml --tags hub`, it does **not** undefine
the domains or delete disks - the VMs and their DNS/LB records stay, so the old
hub can still be inspected, or powered back on.

### Destroy the Ceph cluster (Ceph/CSI DR demo)

Only on the Ceph path, and only deliberately: **this destroys the storage
the backup was taken from.** Take the backup and confirm its `DataUpload`s
completed first; there is no going back to hub1's storage afterwards.

With `oadp_backup_method=csi` the etcd data is no longer on Ceph - the
snapshot was taken, copied to S3 by the data mover, and released. So the
bucket already holds everything the restore needs, and leaving the
original cluster running makes that impossible to show: an observer
cannot tell whether the hosted cluster came back from object storage or
from storage that never went away, and in a real disaster it would not have.

Production puts the DR site on its own storage cluster of the same type,
never the primary's. Rebuilding gets the same evidence on one bare-metal host
without standing up a second 80G Ceph cluster: after this, every RBD image the
hosted cluster's etcd ever lived on is gone.

```bash
(cd ../.. && ansible-playbook -i inventory/hosts cleanup.yaml --tags ceph)

# a partial teardown leaves stale OSD disks the rebuilt cluster cannot
# claim - you get a cluster with no OSDs. Check before rebuilding:
virsh list --all | grep -E 'ceph[123]|cephadmin'          # expect no rows
ls /var/lib/libvirt/images/ | grep -E '^ceph|^cephadmin'  # expect nothing
```

If anything is left, remove it by hand before rebuilding.

## DR hub (cluster2)

### Rebuild Ceph for the DR hub (Ceph/CSI DR demo)

A brand-new cluster on the same four VMs - no pool, no image and no CSI
user survives from hub1's:

```bash
(cd ../../ceph && ansible-playbook -i ../inventory/hosts setup_ceph.yaml --ask-vault-pass)
ssh root@192.168.122.27 ceph -s     # HEALTH_OK, 3 mons, 9 OSDs
```

Wait for `HEALTH_OK` before continuing - the export step will produce a JSON
blob against a degraded cluster and you will find out later.

The StorageClass name comes from `ceph_odf_storagecluster_name`, so the
rebuilt cluster presents `ocs-external-storagecluster-ceph-rbd` again.
That matters: Velero restores each PVC with its original
`spec.storageClassName`, and a DR hub with no class of that exact name
leaves the restored PVCs `Pending` indefinitely, with nothing in the
Restore status to explain why.

### Build the DR hub (cluster2)

Once the primary hub is shut down, build cluster2 and make it a hub:

```bash
(cd ../../standard-cluster && ansible-playbook -i ../inventory/hosts setup_cluster2.yaml --ask-vault-pass)
(cd ../../acm && ansible-playbook -i ../inventory/hosts setup_acm.yaml --ask-vault-pass -e target_hub=hub2)
```

`setup_cluster2.yaml` follows `disconnected_install`, so the same command builds
whichever DR hub the flow calls for: `hub2`, or the disconnected `hub2d`
([standard-cluster/README.md](../../standard-cluster/README.md#build-cluster2)).

> **MetalLB is the one thing the flag does not switch here.** `setup_acm.yaml`
> passes `metallb_hub=hub2` for cluster2 in both modes, because a disconnected
> DR hub is still the DR hub: a hosted cluster restored onto it comes up on
> hub2's addresses (`.190-.192` for the connected clusters, `.193-.195` for the
> `-d` ones). What `disconnected_install` decides is *which* clusters get pools
> - see [acm/README.md, MetalLB Address Pools](../../acm/README.md#metallb-address-pools-for-hosted-clusters).
> `hub2d` is deliberately not a hub name in `hosted_cluster_metallb_pools`.

> **The OADP playbooks do not know about `hub2d` yet.** `oadp/setup_oadp.yaml`,
> `backup_hosted_cluster.yaml` and `restore_hosted_cluster.yaml` still resolve
> their kubeconfig with a two-way `target_hub == 'hub2'` test, so on a
> disconnected lab `-e target_hub=hub2` points them at the *connected* hub2's
> install directory (and `hubd` has always had the same gap). Until that
> selection is widened, name the kubeconfig explicitly - `-e` beats the
> playbook's own variable:
>
> ```bash
> (cd ../../oadp && ansible-playbook -i ../inventory/hosts setup_oadp.yaml --ask-vault-pass \
>   -e target_hub=hub2 \
>   -e hub_kubeconfig=/var/lib/libvirt/images/hub2d_install/auth/kubeconfig)
> ```

On the Ceph path, build cluster2 with `use_lvm_storage: false` and attach it to
the rebuilt Ceph cluster **now**, before the AgentServiceConfig below - this
creates hub2's own ODF external StorageCluster and its own CSI credentials:

```bash
export KUBECONFIG=/var/lib/libvirt/images/hub2_install/auth/kubeconfig
(cd ../../ceph && ansible-playbook -i ../inventory/hosts setup_ceph_odf.yaml --ask-vault-pass -e target_hub=hub2)
oc get sc      # exactly one class marked (default)
```

The AgentServiceConfig provisions three PVCs - `databaseStorage` (10Gi),
`filesystemStorage` (100Gi) and `imageStorage` (50Gi) - and none of them
names a `storageClassName`, so they bind against whatever StorageClass the
cluster marks **default**. Until that exists they sit `Pending` and
assisted-service never starts. On the LVM path `lvms-vg1` is the default;
on the Ceph path it is `ocs-external-storagecluster-ceph-rbd`, which is
why `ceph_odf_rbd_default_sc` defaults to true.

AgentServiceConfigs are **not** restored by OADP. Apply the one
`setup_acm.yaml` just rendered for cluster2 before the restore:

```bash
oc apply -f ../../roles/setup-hub-acm/files/.rendered-05-agentserviceconfig.yaml
oc get pvc -n multicluster-engine    # the three should reach Bound
```

There is no need to create the InfraEnv, the HostedCluster or discover nodes -
the restore brings them back.

### Configure OADP on the DR hub

Same bucket and prefix, same method as the backup, so the snapshot class is
labelled here too and Velero syncs the backups hub1 took:

```bash
(cd ../../oadp && ansible-playbook -i ../inventory/hosts setup_oadp.yaml --ask-vault-pass \
  -e target_hub=hub2 -e oadp_backup_method=csi)
oc get backup.velero.io -n openshift-adp  # hcp-cluster1-backup-csi appears
```

If you backed up the hello-openshift smoke test on hub1, restore it now to
validate the restore path first -
[oadp/README.md](../../oadp/README.md#smoke-test-hello-openshift-with-a-pvc).

### Restore the hosted cluster

Restore with the same method the backup used - it selects both the Restore
template and the name of the Backup to restore from, and the playbook stops
with a clear message if that Backup is not visible on this hub yet:

```bash
ansible-playbook -i ../../inventory/hosts restore_hosted_cluster.yaml --ask-vault-pass \
  -e hcp_cluster_name=hcp-cluster1 -e target_hub=hub2                       # fs
oc get restore.velero.io -n openshift-adp hcp-cluster1-restore -o yaml

ansible-playbook -i ../../inventory/hosts restore_hosted_cluster.yaml --ask-vault-pass \
  -e hcp_cluster_name=hcp-cluster1 -e target_hub=hub2 -e oadp_backup_method=csi   # csi
oc get restore.velero.io -n openshift-adp hcp-cluster1-restore-csi -o yaml
```

On a csi restore the volume data comes back through `DataDownload`
objects, which is where to look if the Restore seems to stall:

```bash
oc get datadownloads.velero.io -n openshift-adp -w
```

Workers built as BareMetalHosts (`hosted_cluster_worker_provisioning=bmh`): add
`-e oadp_restore_detach_bmh=true` to restore them detached, so this hub does
not power-cycle or reprovision them. See [BMH-DR-test.md](BMH-DR-test.md).

### What a good `csi` restore looks like

The counterpart to
[What a good `csi` run looks like](#what-a-good-csi-run-looks-like) - a
verified restore of `hcp-cluster1` onto the DR hub, from the backup
recorded there:

```yaml
status:
  phase: Completed
  progress:
    itemsRestored: 379
    totalItems: 379
  restoreItemOperationsAttempted: 3
  restoreItemOperationsCompleted: 3
  startTimestamp: "2026-09-08T08:12:30Z"
  completionTimestamp: "2026-09-08T08:18:11Z"
  warnings: 1
```

```
$ oc get datadownloads.velero.io -n openshift-adp
NAME                             STATUS      STARTED   BYTES DONE   TOTAL BYTES   STORAGE LOCATION   AGE     NODE
hcp-cluster1-restore-csi-76kvd   Completed   5m18s     371423568    371423568     default            5m23s   worker2
hcp-cluster1-restore-csi-l82hx   Completed   5m18s     370678113    370678113     default            5m23s   worker1
hcp-cluster1-restore-csi-qxxlj   Completed   5m18s     370738865    370738865     default            5m23s   worker3
```

The strongest single check is that **the byte counts match the backup's
`DataUpload`s exactly** - 371423568, 370738865 and 370678113 in both
tables. Same three etcd volumes, round-tripped through S3. Which worker
each lands on differs from the backup and does not matter; the data mover
provisions a fresh PVC wherever the pod is scheduled.

Also worth noting what this proves: the Ceph cluster the backup was taken
from had been destroyed and rebuilt in between (see
[Destroy the Ceph cluster](#destroy-the-ceph-cluster-cephcsi-dr-demo)),
so none of these bytes could have come from the original storage.

A non-zero `warnings` count is normal - `itemsRestored == totalItems` and
three completed operations are what matter. Read them rather than guessing:

```bash
alias velero='oc -n openshift-adp exec deployment/velero -c velero -it -- ./velero'
velero restore describe hcp-cluster1-restore-csi --details
velero restore logs hcp-cluster1-restore-csi | grep -i 'level=warning'
```

Two appear on every run here, both benign:

- `No annotations found for <ns>/<secret>, using restore spec setting: false`
  - Velero deciding whether to restore the object's `status` subresource.
  An object with no annotations at all logs this at warn, one with any
  annotation logs the same decision at debug. Secrets have no `status`,
  and it is never counted in `status.warnings`.
- `item not found when patching managed fields .../token-nodepool-...`
  - Velero created the Secret, then found it gone when patching
  `managedFields` back. HyperShift's NodePool controller rotates those
  token secrets and had already collected the stale one. This is the
  warning the count refers to.

### Restored cluster stuck in `Importing`

If the restored HostedCluster reaches `Completed` but its ManagedCluster
sits at `Available=Unknown` / `ManagedClusterLeaseUpdateStopped`, and the
klusterlet registration agent logs

```
failed to list *v1.ManagedCluster: Unauthorized
failed to list *v1.CertificateSigningRequest: Unauthorized
```

then the agent is presenting a ServiceAccount token minted by the
**original** hub. `Unauthorized` is a 401 - the credential itself was
rejected - so this is not a missing Role or ClusterRole, which would be a
403 `Forbidden`.

The path in: this repo's Backup includes `secrets` from the
`<cluster>` namespace, and for an ACM-imported hosted cluster that is
*also* the ManagedCluster's namespace, so `<cluster>-import` - carrying
the bootstrap SA token - is inside backup scope. Restoring it hands the
DR hub a token signed by the old hub's key, referencing an SA uid the DR
hub regenerated.

Confirm by decoding the token and comparing with the live ServiceAccount:

```bash
oc get secret bootstrap-hub-kubeconfig -n klusterlet-<cluster> -o jsonpath='{.data.kubeconfig}' \
  | base64 -d | grep 'token:' | awk '{print $2}' | cut -d. -f2 \
  | tr '_-' '/+' | base64 -d | python3 -m json.tool
oc get sa <cluster>-bootstrap-sa -n <cluster> -o jsonpath='{.metadata.uid}{"\n"}'
```

An `iat` predating the restore, or a uid that differs from the live SA,
is the confirmation. Fix by deleting the transplanted import secret so
MCE regenerates it against this hub:

```bash
oc delete secret <cluster>-import -n <cluster>
```

MCE mints a new token, rebuilds the `<cluster>-hosted-klusterlet`
ManifestWork from it, and the work agent re-applies
`bootstrap-hub-kubeconfig`; the agent then files a CSR which auto-approves
if `hubAcceptsClient` is already true.

Deleting `bootstrap-hub-kubeconfig` directly does **not** help - it is
owned by that ManifestWork and gets re-applied with the same stale
content. Refresh the import secret and let MCE rebuild the chain.

To stop this recurring, exclude the import secret from the backup using
Velero's object-level opt-out, so the artifact never carries another
hub's credentials - that is the
[label step before the backup](#exclude-acms-import-secret-from-the-backup).

This is not a deviation from the upstream guide's resource list - keep
`sa` and `secrets` as documented. The collision exists only because the
HostedCluster's namespace shares its name with the ManagedCluster;
upstream's example puts the HostedCluster in `clusters`, so ACM's
namespace falls outside the backup.

The label is a workaround, not the fix - MCE reconciles the secret, so it
has to be re-applied before every backup. Filed as
[OCPBUGS-121709](https://redhat.atlassian.net/browse/OCPBUGS-121709);
**that JIRA is where the current status lives**. Check it before a run: if
it is resolved in the ACM/MCE version you are on, the exclusion is no longer
needed.

### Point DNS at the DR hub

The DR hub is on its own network segment, so the restored hosted clusters come
up on its MetalLB addresses (`.190`/`.191`/`.192`) rather than hub1's - the pool
names are the same, the addresses are not. Re-render the zone files so
`api`/`api-int` follow them:

```bash
(cd ../.. && ansible-playbook -i inventory/hosts setup_bm_host.yaml --tags dns \
  --ask-vault-pass -e target_hub=hub2)
```

Verify DNS and the restored kube-apiserver Service agree:

```bash
dig +short api.hcp-cluster1.mylab.com @192.168.122.21
oc get svc kube-apiserver -n hcp-cluster1-hcp-cluster1 \
  -o jsonpath='{.status.loadBalancer.ingress[0].ip}{"\n"}'
```

**Then check a worker node, not just the helper.** The VMs do not query the
helper directly - they get `192.168.122.1` from DHCP, which is libvirt's
dnsmasq, which forwards the lab zones to the helper **and caches the answers**.
A correct zone on the helper and a stale answer on the nodes is the normal
state for a few minutes after a cutover, and the symptom is both hosted-cluster
workers going `NotReady` while `oc get co` falls apart - the kubelets are still
dialling the hub that just went away.

```bash
ssh core@192.168.122.41 'getent hosts api.hcp-cluster1.mylab.com'   # want the DR address
```

If it still shows the old address, flush dnsmasq's cache on the bare-metal
host - this clears cached records without touching DHCP leases or disturbing
any VM:

```bash
sudo kill -HUP $(cat /var/run/libvirt/network/default.pid 2>/dev/null \
                 || cat /var/run/libvirt/dnsmasq/default.pid)
```

Kubelets re-resolve and rejoin on their own within a minute or two;
`systemctl restart kubelet` on each worker forces it.

The hosted-cluster zones are served with `$TTL 60` and this dnsmasq is capped
at `max-cache-ttl=60` (`roles/setup-bm-host/templates/default-network.xml.j2`),
so a cutover propagates in about a minute. Both are recent - a lab whose
`default` network was defined before that change still caches at the zone's
old 1-day TTL until `setup_bm_host.yaml --tags virtnet` redefines the network,
which drops `virbr0` and briefly interrupts every VM. The flush above is the
zero-downtime alternative.

Set `target_hub: hub2` in `vars.yaml` to make the cutover permanent. See
[acm/README.md, MetalLB Address Pools](../../acm/README.md#metallb-address-pools-for-hosted-clusters)
for how the per-hub addresses are defined.

### Verify the hosted cluster came back

```bash
oc get managedcluster                                   # on hub2
oc get hostedcluster,nodepool -n hcp-cluster1

export KUBECONFIG=<hosted cluster kubeconfig>
oc get nodes                     # both workers Ready
oc get co | grep -v 'True.*False.*False'
oc get pods -A | grep -vE 'Running|Completed'
oc get route -n hello-openshift
```

Nodes stay `NotReady` until DNS points at the DR hub and the kubelets
reconnect, so do the step above first. `metrics.k8s.io` errors and
Pending/Terminating pods are downstream of that and clear on their own.

The hosted cluster's kubeconfig from before the backup should keep working:
its certificates come back with the restore, and the DNS cutover points
`api.hcp-cluster1.mylab.com` at hub2.

## Templates

The DPA and credentials templates are in `roles/setup-oadp/templates/`; the
Backup/Restore manifests are here, one pair per volume method, selected by
`oadp_backup_method`:

| Method | Backup template | Restore template | Objects |
| ------ | --------------- | ---------------- | ------- |
| `fs`   | `templates/backup-hcp-cluster.yaml.j2` | `templates/restore-hcp-cluster.yaml.j2` | `<cluster>-backup` / `<cluster>-restore` |
| `csi`  | `templates/backup-hcp-cluster-csi.yaml.j2` | `templates/restore-hcp-cluster-csi.yaml.j2` | `<cluster>-backup-csi` / `<cluster>-restore-csi` |

`templates/restore-bmh-detached-modifier.yaml.j2` is the Velero resource
modifier `oadp_restore_detach_bmh` adds, which marks each restored
BareMetalHost `baremetalhost.metal3.io/detached`.

## Related tests

| Document | What it covers |
| --- | --- |
| [BMH-DR-test.md](BMH-DR-test.md) | The DR flow with the workers managed as BareMetalHosts through sushy-emulator, restored detached |
| [OCPBUGS-121709-test.md](OCPBUGS-121709-test.md) | Testing the hypershift plugin fix that would make the import-secret label unnecessary |
| [hosted-cluster-dr-runbook.md](hosted-cluster-dr-runbook.md) | The same procedure as a production runbook |
