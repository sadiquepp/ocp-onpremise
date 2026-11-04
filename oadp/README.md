# OADP: wiring Velero to S3, and checking it works

Generic OADP for any cluster this repository builds: the one-time S3 bucket and
IAM user, the `cloud-credentials` secret and `DataProtectionApplication`
(`setup_oadp.yaml`), the choice between file-system and CSI-snapshot backups,
a small hello-openshift backup/restore smoke test, and the OADP problems worth
recognising.

The **OADP operator** itself is installed by
[`acm/setup_acm.yaml`](../acm/README.md) (`roles/setup-hub-acm`, alongside
ACM/MCE and MetalLB). `roles/setup-oadp` only wires it up to a bucket, which
needs AWS credentials that do not exist when the cluster is first built.

Backing up a **hosted cluster** and restoring it onto another hub - the
Backup/Restore playbooks, their templates and the DR cutover - is in
[`hcp/dr/`](../hcp/dr/README.md), which builds on this directory.

## Contents

- [What lives here](#what-lives-here)
- [One-time AWS setup (per bucket, not per hub)](#one-time-aws-setup-per-bucket-not-per-hub)
- [Configure OADP on a cluster](#configure-oadp-on-a-cluster)
- [A local S3 target for the disconnected lab](#a-local-s3-target-for-the-disconnected-lab)
- [OADP configuration](#oadp-configuration)
- [Backup methods: fs vs csi](#backup-methods-fs-vs-csi)
  - [What `csi` needs](#what-csi-needs)
- [Smoke test: hello-openshift with a PVC](#smoke-test-hello-openshift-with-a-pvc)
- [Why there is no CSI smoke test](#why-there-is-no-csi-smoke-test)
- [Troubleshooting](#troubleshooting)
  - [If no `DataUpload` is created at all](#if-no-dataupload-is-created-at-all)
  - [`oc get backup` shows nothing, `oc get backup.velero.io` does](#oc-get-backup-shows-nothing-oc-get-backupveleroio-does)
  - [Recovering from `backup already exists in object storage`](#recovering-from-backup-already-exists-in-object-storage)
  - [If a backup reports thousands of items](#if-a-backup-reports-thousands-of-items)

## What lives here

| File | Purpose |
| --- | --- |
| `setup_oadp.yaml` | Create the `cloud-credentials` secret and the `DataProtectionApplication` on a cluster (`roles/setup-oadp`); with `oadp_backup_method=csi`, also label the VolumeSnapshotClass Velero uses |
| `hello-openshift-oadp.yaml` | Smoke-test app: a namespace, a Deployment and a PVC |
| `hello-openshift-oadp-backup.yaml` / `hello-openshift-oadp-restore.yaml` | Velero `Backup` and `Restore` of that namespace (fs method) |
| `ansible.cfg` | Mirrors the root `ansible.cfg` and adds `../roles` to `roles_path` |

`setup_oadp.yaml` shares `vars.yaml`, `vault.yaml` and `inventory/hosts` with
the rest of the repository; the role stays in the shared [`roles/`](../roles/)
directory. **Run it from inside this directory** - the `ansible.cfg` here is
what puts `../roles` on the role search path. The `oc apply` commands below
assume the same working directory.

## One-time AWS setup (per bucket, not per hub)

```bash
export BUCKET=adp-backup-bucket-xjtvvs   # must be globally unique - pick your own
export REGION=ap-south-1

aws s3api create-bucket --bucket $BUCKET --region $REGION \
  --create-bucket-configuration LocationConstraint=$REGION

cat > adp-policy.json <<EOF
{
    "Version": "2012-10-17",
    "Statement": [
        {
            "Effect": "Allow",
            "Action": [
                "ec2:DescribeVolumes",
                "ec2:DescribeSnapshots",
                "ec2:CreateTags",
                "ec2:CreateVolume",
                "ec2:CreateSnapshot",
                "ec2:DeleteSnapshot"
            ],
            "Resource": "*"
        },
        {
            "Effect": "Allow",
            "Action": [
                "s3:GetObject",
                "s3:DeleteObject",
                "s3:PutObject",
                "s3:AbortMultipartUpload",
                "s3:ListMultipartUploadParts"
            ],
            "Resource": ["arn:aws:s3:::${BUCKET}/*"]
        },
        {
            "Effect": "Allow",
            "Action": [
                "s3:ListBucket",
                "s3:GetBucketLocation",
                "s3:ListBucketMultipartUploads"
            ],
            "Resource": ["arn:aws:s3:::${BUCKET}"]
        }
    ]
}
EOF

aws iam create-user --user-name adp-user
aws iam put-user-policy --user-name adp-user --policy-name adp-policy --policy-document file://adp-policy.json
aws iam create-access-key --user-name adp-user
```

Set `oadp_bucket_name` and `oadp_aws_region` in `vars.yaml` to match, and
add the access key from the last command to `vault.yaml`:

```bash
ansible-vault edit vault.yaml
```
```yaml
oadp_aws_access_key_id: 'AKIA...'
oadp_aws_secret_access_key: '...'
```

This bucket and IAM user are reused by every cluster you point OADP at -
cluster1 and cluster2 in the DR walkthrough - so you only do this section once
per lab, not once per cluster.

## Configure OADP on a cluster

The cluster needs the OADP operator first ([`acm/setup_acm.yaml`](../acm/README.md)).
Then point it at your bucket:

```bash
cd oadp
ansible-playbook -i ../inventory/hosts setup_oadp.yaml --ask-vault-pass                       # cluster1 (hub)
ansible-playbook -i ../inventory/hosts setup_oadp.yaml --ask-vault-pass -e target_hub=hub2    # cluster2 (hub2)
```

For CSI snapshot backups, add the method so the role also labels the
VolumeSnapshotClass Velero needs (it fails with a clear message if ODF
has not created one yet):

```bash
ansible-playbook -i ../inventory/hosts setup_oadp.yaml --ask-vault-pass -e oadp_backup_method=csi
oc get volumesnapshotclass -L velero.io/csi-volumesnapshot-class
```

The playbook refuses to start if `oadp_aws_access_key_id` /
`oadp_aws_secret_access_key` are missing from `vault.yaml` or still look like
placeholders, and the role waits for the `BackupStorageLocation` to report
`Available` - that is the check that proves the key actually works.

Idempotent - re-running against the same cluster just reconciles the
secret/DPA. Every cluster points at the same bucket/prefix, so a second
cluster's Velero can see backups the first one created; run it there with the
same `oadp_backup_method` you backed up with, so the snapshot class is
labelled there too before a restore.

`target_hub` selects the kubeconfig: `hub2` means cluster2, anything else
cluster1. The disconnected builds (`hubd`, `hub2d`) are not selectable yet -
name the kubeconfig with `-e hub_kubeconfig=<install folder>/auth/kubeconfig`
(see [hcp/dr/README.md](../hcp/dr/README.md#build-the-dr-hub-cluster2)).

## A local S3 target for the disconnected lab

A disconnected lab cannot reach AWS. [`minio/`](../minio/README.md) builds the
lab's own S3-compatible store, with a bucket for OADP (`minio_bucket_name`,
deliberately distinct from `oadp_bucket_name`). `setup_oadp.yaml` still renders
the DataProtectionApplication against AWS S3 - pointing it at MinIO (an
`s3Url`, `s3ForcePathStyle` and the MinIO credentials) is not automated yet.

## OADP configuration

The `DataProtectionApplication` (`roles/setup-oadp/templates/dpa.yaml.j2`)
uses:

- **Velero plugins**: `openshift`, `aws`, `csi`, `hypershift`
- **Uploader**: Kopia (node-agent), used both for filesystem backup and as the
  data mover behind CSI snapshots
- **Storage**: AWS S3 with a shared bucket/prefix (`oadp_bucket_name`,
  `oadp_backup_prefix`) across clusters
- **Volume method**: `oadp_backup_method` - `fs` (default) or `csi`, see below

`oadp_hypershift_plugin_image` replaces the hypershift plugin image for testing
a plugin fix ahead of an OADP release - see
[hcp/dr/OCPBUGS-121709-test.md](../hcp/dr/OCPBUGS-121709-test.md).

## Backup methods: fs vs csi

The hosted control plane's state lives in etcd, on a PVC in the
HyperShift control-plane namespace. How Velero captures that PVC is the
one real choice in this flow, and `oadp_backup_method` in `vars.yaml`
makes it:

| | `fs` (default) | `csi` |
| --- | --- | --- |
| Velero fields | `defaultVolumesToFsBackup: true` | `snapshotVolumes: true`, `snapshotMoveData: true`, `datamover: velero` |
| How the data moves | node-agent mounts the volume and streams the files to S3 with Kopia | CSI driver snapshots the volume; the data mover copies the snapshot to S3 |
| Consistency | file-by-file while etcd keeps writing | point-in-time, taken atomically by the storage layer |
| Runs on | any StorageClass, CSI or not - including LVM Storage (`lvms-vg1`) | only a CSI StorageClass with a VolumeSnapshotClass - here, Ceph via ODF external mode |
| Use for a control-plane restore | no - a file-level copy of a live etcd volume is not point-in-time | yes - this is the method the [DR walkthrough](../hcp/dr/README.md) uses |
| Hosted-cluster templates ([`hcp/dr/templates/`](../hcp/dr/templates/)) | `backup-hcp-cluster.yaml.j2`, `restore-hcp-cluster.yaml.j2` | `backup-hcp-cluster-csi.yaml.j2`, `restore-hcp-cluster-csi.yaml.j2` |
| Object names | `<cluster>-backup` / `<cluster>-restore` | `<cluster>-backup-csi` / `<cluster>-restore-csi` |

Both write to the same bucket, prefix and `DataProtectionApplication`;
the two methods differ only in the Backup/Restore CRs. The names differ
so both can be demonstrated against one bucket without colliding, and
the `fs` names are unchanged from before this option existed.

Demonstrating `csi` is the reason the Ceph cluster exists in this lab:
LVM Storage ships no VolumeSnapshotClass, so it can only ever do `fs`.
See [ceph/README.md](../ceph/README.md) for building the cluster and
attaching it with `ceph/setup_ceph_odf.yaml`.

### What `csi` needs

- The hub's PVs come from a CSI driver with snapshot support. On this
  lab that means `use_lvm_storage: false` plus `ceph/setup_ceph.yaml` and
  `ceph/setup_ceph_odf.yaml`, which leave the hub with
  `ocs-external-storagecluster-ceph-rbd` as the default StorageClass.
- A `VolumeSnapshotClass` for that driver labelled
  `velero.io/csi-volumesnapshot-class=true`. Velero has no field for
  naming one - it looks the class up by that label, per driver, and if
  no class carries it **the volume is skipped and the Backup still
  reports `Completed`**. `setup_oadp.yaml` applies the label when run
  with `-e oadp_backup_method=csi`; `hcp/dr/backup_hosted_cluster.yaml`
  refuses to run a csi backup if nothing carries it.
- The `csi` plugin and the node-agent in the DPA. Both are already
  there (`roles/setup-oadp/templates/dpa.yaml.j2`) - the data mover
  reuses the same node-agent the `fs` method uses, so no DPA change is
  needed to switch methods.

Everything else - the bucket, the credentials, the resource list, the
DR cutover - is identical.

## Smoke test: hello-openshift with a PVC

A small, self-contained app plus a Backup/Restore pair to sanity-check OADP
itself - bucket, credentials, node-agent - before running it against a real
hosted cluster. It uses the `fs` method; there is no CSI equivalent, for a
reason worth reading before debugging a CSI backup - see
[Why there is no CSI smoke test](#why-there-is-no-csi-smoke-test).

On the cluster being backed up (cluster1 in the DR walkthrough):

```bash
oc apply -f hello-openshift-oadp.yaml
```

Write some persistent data to the PVC, and check it is there:

```bash
POD=`oc get pod -n hello-openshift-oadp -o jsonpath='{.items[0].metadata.name}'`
oc exec -it $POD -n hello-openshift-oadp -- sh -c 'echo "Hello, World!" > /var/data/hello.txt'
oc exec -it $POD -n hello-openshift-oadp -- sh -c 'cat /var/data/hello.txt'
```

Back it up, and check the backup status periodically until it shows as
completed:

```bash
oc apply -f hello-openshift-oadp-backup.yaml
oc get backup.velero.io -n openshift-adp hello-openshift-oadp-backup -o yaml
```

Then, on a second cluster configured against the same bucket (cluster2, after
`setup_oadp.yaml -e target_hub=hub2`), restore it and check the data came back:

```bash
oc apply -f hello-openshift-oadp-restore.yaml
POD=`oc get pod -n hello-openshift-oadp -o jsonpath='{.items[0].metadata.name}'`
oc exec -it $POD -n hello-openshift-oadp -- sh -c 'cat /var/data/hello.txt'
```

## Why there is no CSI smoke test

There is deliberately no CSI equivalent of the hello-openshift smoke test
above. On any hub this repo builds it cannot work, and the reason is
worth knowing before you debug a CSI backup of your own.

The DPA loads the `hypershift` plugin on every hub. That plugin's
`AppliesTo()` is an **empty ResourceSelector**, which in Velero means its
BackupItemAction runs against every item of every backup - PVs,
ServiceAccounts, Events, everything. Whenever the item's namespace is not
a hosted control plane it fails:

```
error executing custom action (groupResource=persistentvolumes, ...):
  rpc error: code = Unknown desc = error getting HCP namespace: no HostedControlPlane found
```

Upstream's `pkg/common.ShouldEndPluginExecution` is supposed to skip the
plugin for backups that do not look like HCP backups (it tests whether
`includedResources` contains `*`, `hostedcluster`, `hostedcontrolplane`
or `nodepool`), but the plugin build shipped with OADP 1.5 here errors
regardless - an explicit resource list naming none of those keywords
makes no difference.

**Why that kills CSI specifically.** In
`pkg/backup/item_backupper.go`, Velero collects pod volumes for
file-system backup *before* calling `executeActions`, but the CSI
snapshot is an action *inside* it - and `executeActions` returns on the
first action error:

```go
updatedItem, ..., err := action.Execute(obj, ib.backupRequest.Backup)
if err != nil {
    return nil, itemFiles, errors.Wrapf(err, "error executing custom action (...)")
}
```

So on a non-HCP namespace:

| | outcome |
| --- | --- |
| `fs` | works - the PodVolumeBackup was already set up before the failing action, so data is backed up and the Backup merely reports `PartiallyFailed` |
| `csi` | never runs - the hypershift action fails on the PVC first, so no VolumeSnapshot and no DataUpload are ever created |

That is why the fs smoke test above is useful despite its error count,
and why a CSI one would only ever look like broken storage.

**Validate the CSI method on the real thing instead.** In a hosted
control plane's namespace the plugin resolves the HCP, returns success,
and the CSI action runs normally:

```bash
(cd ../hcp/dr && ansible-playbook -i ../../inventory/hosts backup_hosted_cluster.yaml --ask-vault-pass \
  -e hcp_cluster_name=hcp-cluster1 -e oadp_backup_method=csi)
oc get datauploads.velero.io -n openshift-adp -w
```

A `DataUpload` reaching phase `Completed` is the proof that CSI snapshot
plus data mover works; the Backup's own phase is the weaker signal.

## Troubleshooting

### If no `DataUpload` is created at all

`oc get datauploads.velero.io -n openshift-adp` returning nothing means Velero
never attempted the volume. Check, in this order:

```bash
# 1. a snapshot class labelled for Velero, for this PVC's driver?
oc get volumesnapshotclass -L velero.io/csi-volumesnapshot-class

# 2. did the PVC bind to a CSI driver?
oc get pvc -n <namespace>
oc get pv <pv-name> -o jsonpath='{.spec.storageClassName}{"  driver="}{.spec.csi.driver}{"\n"}'

# 3. is CSI enabled in this Velero?
oc get deployment velero -n openshift-adp \
  -o jsonpath='{.spec.template.spec.containers[0].args}{"\n"}'   # expect --features=EnableCSI

# 4. did an item action fail before the CSI one got to run?
velero backup logs <backup-name> -n openshift-adp | grep 'level=error' | head -3
```

(1)-(3) are the prerequisites. (4) is the case described above: a
`no HostedControlPlane found` error on the PVC means the CSI action was
never reached, and the storage configuration is not at fault.

### `oc get backup` shows nothing, `oc get backup.velero.io` does

Always qualify the group. More than one installed operator defines a
`Backup` kind, so the short name is ambiguous and kubectl resolves it to
one of the others - `oc get backup` then lists an empty, unrelated
resource and gives no hint that it looked in the wrong place.

On this lab the competing CRD arrives with ODF itself: NooBaa pulls in
CloudNativePG, which defines `backups.postgresql.cnpg.noobaa.io`. So the
collision appears exactly when you take the Ceph path. Ask the cluster
what it has rather than assuming:

```bash
oc api-resources | grep -i backup           # every group defining Backup
oc get backup -n openshift-adp              # whichever one won, likely empty
oc get backup.velero.io -n openshift-adp    # what you meant
```

The same care applies to `restore.velero.io`, `datauploads.velero.io` and
`datadownloads.velero.io`. Everything in this repo qualifies them.

### Recovering from `backup already exists in object storage`

A Backup that fails immediately with

```yaml
  failureReason: backup already exists in object storage
  phase: Failed
```

means the name is still taken in the bucket. It happens after
`oc delete backup`, which deletes the Kubernetes object and leaves the
data behind - Velero refuses to overwrite an existing backup directory.

Velero re-syncs backups from the bucket about once a minute, so the
object comes back on its own; then delete it properly:

```bash
alias velero='oc -n openshift-adp exec deployment/velero -c velero -it -- ./velero'
oc get backup.velero.io -n openshift-adp | grep <backup-name>   # wait for it to reappear
velero backup delete <backup-name> --confirm
oc get deletebackuprequests -n openshift-adp          # processed, then gone
```

If it will not come back, remove that one backup's directory from S3 by
hand - `oadp_bucket_name` and `oadp_backup_prefix` in `vars.yaml` give
the path:

```bash
aws s3 ls s3://<oadp_bucket_name>/<oadp_backup_prefix>/backups/
aws s3 rm s3://<oadp_bucket_name>/<oadp_backup_prefix>/backups/<backup-name>/ --recursive
```

Delete only that one directory. Do not clear the prefix or reorganise the
bucket: `backuprepositories.velero.io` indexes what is under it, and
rearranging the folder structure means recreating the DPA, backups and
restores.

### If a backup reports thousands of items

A one-namespace backup collecting 4000+ items is backing up the whole
cluster: `includeClusterResources: true` does not mean "the
cluster-scoped objects this namespace needs", it means **every**
cluster-scoped object there is. On an ACM hub that is thousands of
`AppliedManifestWork`s, each one erroring through the hypershift plugin.
Leave the field unset and Velero includes only what is associated with
the namespace's own objects - above all the PV behind the PVC.
