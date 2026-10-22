# Hosted cluster backup and restore to a DR hub

A runbook for backing up a hosted control plane (HCP) from the primary
hub, and restoring it onto a DR hub, with OADP. It covers hosted clusters
on the **Agent platform** with bare-metal workers.

Starting point:

- The **primary hub** (OpenShift with ACM/MCE) is up, and the hosted
  cluster runs on it.
- The **DR hub** is up, with the same ACM/MCE and OADP versions, in a
  **different network segment**.
- After a restore on the DR hub, the hosted cluster's API VIP changes to
  an address in the DR segment, and so does the hub ingress VIP that the
  workers reach for OAuth, Konnectivity and Ignition.

The workers do not move. A DR restore moves the control plane only. The
worker nodes keep running their workloads through the hub outage and
reconnect to the restored control plane once DNS points at it.

## Contents

- [Before you start: what gets backed up](#before-you-start-what-gets-backed-up)
- [1. S3 object storage](#1-s3-object-storage)
- [2. OADP on both hubs](#2-oadp-on-both-hubs)
- [3. DR hub readiness](#3-dr-hub-readiness)
- [4. Backup](#4-backup)
- [5. Restore on the DR hub](#5-restore-on-the-dr-hub)
- [6. After the restore](#6-after-the-restore)
- [Appendix A: troubleshooting](#appendix-a-troubleshooting)
- [Appendix B: validated versions](#appendix-b-validated-versions)

## Before you start: what gets backed up

Set these once per shell. Every command below uses them.

```bash
HC_NAME=<hosted-cluster-name>         # the HostedCluster's name
HC_NS=<hosted-cluster-namespace>      # namespace the HostedCluster and NodePools live in
HCP_NS=${HC_NS}-${HC_NAME}            # control-plane namespace HyperShift creates
AGENT_NS=<agent-namespace>            # namespace holding the InfraEnv, Agents, BareMetalHosts, NMStateConfigs
```

A backup covers three namespaces:

| Namespace | Holds | Why it is needed |
|---|---|---|
| `$HC_NS` | HostedCluster, NodePools, their pull-secret and SSH-key secrets | The cluster's definition |
| `$HCP_NS` | The hosted control plane: etcd and its PVCs, kube-apiserver, certificates, CAPI objects | The cluster's state |
| `$AGENT_NS` | InfraEnv, Agents, BareMetalHosts and their BMC secrets, NMStateConfigs | Lets the restored control plane re-adopt the existing workers |

**Keep the InfraEnv in its own namespace, separate from the
HostedCluster.** That keeps the Agent records, and so the workers'
identity, intact through a restore. The restored cluster then re-adopts
the running workers instead of needing them rebooted into the discovery
ISO.

**Check whether `$HC_NS` is also the ACM ManagedCluster namespace.** ACM
creates a namespace named after the managed cluster. If the HostedCluster
lives in a namespace with that same name (`HC_NS == HC_NAME`), the backup
also captures ACM's `<cluster>-import` secret, which holds a token only
the primary hub accepts. See [5.6](#56-import-secret) for how to handle
it.

**Publish the hosted cluster's API by DNS name, not by IP.** The
HostedCluster's `APIServer` service must use `LoadBalancer` with a
`hostname`:

```bash
oc get hostedcluster $HC_NAME -n $HC_NS \
  -o jsonpath='{.spec.services[?(@.service=="APIServer")].servicePublishingStrategy}{"\n"}'
# {"loadBalancer":{"hostname":"api.<hosted-cluster-domain>"},"type":"LoadBalancer"}
```

The serving certificate, every kubeconfig and every kubelet use that
name. A DR restore then only needs a DNS change. If the API is published
by IP instead, the address change after a restore breaks the certificate
and every client.

## 1. S3 object storage

One bucket serves both hubs. Put it somewhere that survives the loss of
the primary site: a cloud region, or an S3-compatible store at the DR
site or a third site. Do **not** put it on storage that the primary hub
itself provides.

> **On-premise S3.** The commands in this section use AWS S3. With an
> on-premise S3-compatible store (ODF Multicloud Object Gateway, Ceph
> RGW, MinIO, or a storage appliance's S3 service), do the equivalent
> with that store's own tooling:
>
> | Step | AWS (as shown) | On-premise equivalent |
> |---|---|---|
> | Create the bucket | `aws s3api create-bucket` | The store's console, CLI or API. On ODF, an ObjectBucketClaim, created on storage that isn't on the primary hub |
> | Encryption at rest | `put-bucket-encryption` | The store's server-side or volume-level encryption |
> | Block public access | `put-public-access-block` | Bucket policy or ACLs that grant access to the Velero user only |
> | Velero identity and policy | IAM user plus `put-user-policy` | A store user or access key with a bucket policy allowing the same S3 actions as [1.2](#12-access-policy) |
> | Object lock, replication | S3 Object Lock, replication rules | The store's own features, where it has them |
> | Testing access ([1.4](#14-verify-access-from-both-sites)) | `aws s3 ...` | The same commands with `--endpoint-url https://<s3-endpoint>`, plus `--ca-bundle <ca.pem>` for a private CA |
>
> In the DPA ([2.4](#24-dataprotectionapplication)), keep `provider: aws`
> and add the S3-compatible settings shown there as comments:
> - `s3Url`: the store's endpoint.
> - `s3ForcePathStyle: "true"`: most on-premise stores don't support
>   virtual-hosted bucket names.
> - `region`: whatever value the store accepts (often any string, such
>   as `us-east-1`, or the store's own region name).
> - `objectStorage.caCert`: the endpoint's CA, base64-encoded, if it uses
>   a private CA.
> - `checksumAlgorithm: ""`: needed if the store rejects the checksums
>   newer AWS SDKs send by default.
>
> The endpoint must be reachable, with valid TLS, from **both** hubs'
> network segments.

### 1.1 Bucket

- **One bucket and prefix per hub pair.** Both hubs' DPAs use the same
  values.
- **Encrypt at rest** (SSE-S3, SSE-KMS or the appliance's equivalent).
  Backups contain the hosted cluster's secrets and CA keys.
- **No lifecycle expiration rule on the Velero prefix.** Velero expires
  backups itself, through each backup's `ttl`. A lifecycle rule deletes
  objects out from under it and leaves broken backups behind.
- **Optional: object lock or a replicated copy** as protection against
  deletion or ransomware. Set this up with the storage team, and test a
  restore with it in place.

AWS example:

```bash
BUCKET=<bucket>; REGION=<region>
aws s3api create-bucket --bucket $BUCKET --region $REGION \
  --create-bucket-configuration LocationConstraint=$REGION
aws s3api put-bucket-encryption --bucket $BUCKET \
  --server-side-encryption-configuration \
  '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"}}]}'
aws s3api put-public-access-block --bucket $BUCKET --public-access-block-configuration \
  BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
```

### 1.2 Access policy

Give Velero a dedicated identity with access to this bucket only. With
CSI snapshots plus the data mover, Velero needs only S3 permissions, not
EC2 or snapshot permissions:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": ["s3:GetObject", "s3:PutObject", "s3:DeleteObject",
                 "s3:AbortMultipartUpload", "s3:ListMultipartUploadParts"],
      "Resource": ["arn:aws:s3:::<bucket>/*"]
    },
    {
      "Effect": "Allow",
      "Action": ["s3:ListBucket", "s3:GetBucketLocation", "s3:ListBucketMultipartUploads"],
      "Resource": ["arn:aws:s3:::<bucket>"]
    }
  ]
}
```

```bash
aws iam create-user --user-name oadp-hcp
aws iam put-user-policy --user-name oadp-hcp --policy-name oadp-hcp-bucket \
  --policy-document file://oadp-hcp-policy.json
aws iam create-access-key --user-name oadp-hcp
```

On an S3-compatible store, create the equivalent user and a bucket
policy with the same actions. Rotate the key on your normal schedule:
update the secret on both hubs, then check that the backup location goes
back to `Available`.

### 1.3 Credentials for Velero

Velero reads the access key from a credentials file in AWS format. Save
it as `credentials-velero`. [1.4](#14-verify-access-from-both-sites)
tests it, and [2.3](#23-credentials-secret) puts it into a secret on each
hub. Delete the file once both hubs have the secret:

```ini
[default]
aws_access_key_id=<access-key-id>
aws_secret_access_key=<secret-access-key>
```

### 1.4 Verify access from both sites

Before configuring OADP, check that the key works and that the endpoint
is reachable from **each hub's network**, the DR segment included. Run
this from a host in each segment:

```bash
export AWS_SHARED_CREDENTIALS_FILE=./credentials-velero
aws s3 ls s3://<bucket>/ --region <region>
#   S3-compatible store: add --endpoint-url https://<s3-endpoint>
#   (and --ca-bundle <ca.pem> for a private CA)
echo test | aws s3 cp - s3://<bucket>/<prefix>/access-check --region <region>
aws s3 rm s3://<bucket>/<prefix>/access-check --region <region>
```

An `InvalidAccessKeyId` or `SignatureDoesNotMatch` error means the key is
wrong; `NoSuchBucket`, `AccessDenied` or a region error point at the
bucket, region or policy; a TLS error means the CA is missing.

## 2. OADP on both hubs

Do this on **both** hubs. The DR hub must be ready to restore before you
need it.

### 2.1 Operator

Install the OADP operator into `openshift-adp`, from the channel that
supports your OpenShift version. Use the same OADP version on both hubs.

```bash
oc get csv -n openshift-adp -o custom-columns=NAME:.metadata.name,VERSION:.spec.version,PHASE:.status.phase
```

### 2.2 Requirements for hosted clusters

The DataProtectionApplication (DPA) must have:

| Setting | Why |
|---|---|
| `defaultPlugins` includes `hypershift` | Marks the HostedCluster at backup time so HyperShift runs its recovery logic after a restore, and handles control-plane specifics (etcd, pods, Agent platform objects) in both directions |
| `defaultPlugins` includes `csi` | CSI volume snapshots of the etcd PVCs |
| `defaultPlugins` includes `openshift` and `aws` | OpenShift resource handling, and the S3 object-store client (`aws` works for any S3-compatible store) |
| `nodeAgent.enable: true`, `uploaderType: kopia` | The node agent runs the CSI data mover, which copies the snapshots into S3 |
| A backup location that both hubs share | The DR hub can only restore backups it can see |

**Back up etcd with CSI snapshots plus the data mover, not file-system
backup.** A file copy of a live etcd volume is not a consistent
point-in-time image. A CSI snapshot is.

**The hub's etcd PVCs must come from a CSI driver with snapshot
support**, with a VolumeSnapshotClass for that driver labelled for Velero
(see [2.6](#26-volumesnapshotclass)).

### 2.3 Credentials secret

On both hubs, from the credentials file in [1.3](#13-credentials-for-velero):

```bash
oc create secret generic cloud-credentials -n openshift-adp \
  --from-file cloud=credentials-velero --dry-run=client -o yaml | oc apply -f -
rm -f credentials-velero
```

### 2.4 DataProtectionApplication

With the credentials secret in place, create the DPA:

```yaml
apiVersion: oadp.openshift.io/v1alpha1
kind: DataProtectionApplication
metadata:
  name: dpa
  namespace: openshift-adp
spec:
  backupLocations:
    - name: default
      velero:
        provider: aws
        default: true
        accessMode: ReadWrite          # DR hub: ReadOnly until failover, see 2.7
        objectStorage:
          bucket: <bucket>
          prefix: <prefix>             # identical on both hubs
          # caCert: <base64 PEM>       # only for an S3 endpoint with a private CA
        config:
          region: <region>             # any value an S3-compatible store accepts
          profile: default
          # S3-compatible stores (ODF/MCG, Ceph RGW, MinIO, appliances) also need:
          # s3Url: https://<s3-endpoint>
          # s3ForcePathStyle: "true"
          # checksumAlgorithm: ""      # if the store rejects the AWS SDK's default checksums
        credential:
          name: cloud-credentials
          key: cloud
  configuration:
    nodeAgent:
      enable: true
      uploaderType: kopia
    velero:
      defaultPlugins:
        - openshift
        - aws
        - csi
        - hypershift
      resourceTimeout: 2h
```

There is no `snapshotLocations`. On bare metal the etcd volumes go
through CSI snapshots and the data mover, which don't use a cloud
snapshot location.

### 2.5 Verify the backup location

```bash
oc get backupstoragelocations.velero.io -n openshift-adp   # PHASE Available
velero backup-location get
```

Velero checks the location about once a minute, so give it a minute
after a change. If it stays `Unavailable`, read `.status.message` on the
BackupStorageLocation. It reports the same errors as the access check in
[1.4](#14-verify-access-from-both-sites): a wrong key, a bucket, region or
policy problem, or a missing `caCert` for a private CA.

### 2.6 VolumeSnapshotClass

Velero has no field naming a VolumeSnapshotClass. It looks one up by
label, per CSI driver. If no class for the etcd PVCs' driver carries the
label, **Velero skips the volumes and the backup still reports
`Completed`.**

```bash
# The driver behind the etcd PVCs
oc get pvc -n $HCP_NS -o custom-columns=PVC:.metadata.name,SC:.spec.storageClassName
oc get sc <storage-class> -o jsonpath='{.provisioner}{"\n"}'

# Label exactly one VolumeSnapshotClass for that driver
oc get volumesnapshotclass -o custom-columns=NAME:.metadata.name,DRIVER:.driver
oc label volumesnapshotclass <class> velero.io/csi-volumesnapshot-class=true --overwrite
```

Do the same on the DR hub, for its own CSI driver.

### 2.7 Backup location on the DR hub: read-only until failover

Both hubs point at the same bucket and prefix. That is what lets the DR
hub see the primary's backups. But a second hub with write access also
runs Velero's garbage collection and can delete backups. Configure the DR
hub's location with `accessMode: ReadOnly`, and switch it to `ReadWrite`
as part of failover ([6.2](#62-take-over-backups-on-the-dr-hub)).

### 2.8 If OADP is already configured: checks

Run on each hub:

```bash
# Operator healthy
oc get csv -n openshift-adp | grep oadp

# Plugins and node agent
oc get dpa -n openshift-adp -o jsonpath='{.items[0].spec.configuration.velero.defaultPlugins}{"\n"}'
#   must include hypershift, csi, aws, openshift
oc get dpa -n openshift-adp -o jsonpath='{.items[0].spec.configuration.nodeAgent}{"\n"}'
#   enable: true, uploaderType: kopia
oc get ds node-agent -n openshift-adp     # one pod per schedulable node, all Ready

# Velero runs the hypershift plugin
oc get deploy velero -n openshift-adp \
  -o jsonpath='{range .spec.template.spec.initContainers[*]}{.name}{"  "}{.image}{"\n"}{end}'
#   hypershift-oadp-plugin is listed

# Backup location reachable
oc get backupstoragelocations.velero.io -n openshift-adp
#   PHASE Available. Unavailable means a credential, endpoint or bucket problem;
#   .status.message on the object says which.

# Snapshot class labelled for the etcd PVCs' driver
oc get volumesnapshotclass -L velero.io/csi-volumesnapshot-class

# Same bucket and prefix on both hubs
oc get backupstoragelocations.velero.io -n openshift-adp \
  -o jsonpath='{range .items[*]}{.metadata.name}{"  "}{.spec.objectStorage.bucket}/{.spec.objectStorage.prefix}{"  "}{.spec.accessMode}{"\n"}{end}'
```

Always use the fully qualified names `backup.velero.io`,
`restore.velero.io` and `backupstoragelocations.velero.io`. Other
operators, ODF among them, define their own `Backup` kinds, so
`oc get backup` can silently query the wrong resource.

For convenience:

```bash
alias velero='oc -n openshift-adp exec deploy/velero -c velero -it -- ./velero'
```

## 3. DR hub readiness

Check this when the DR hub is built, and again in every DR drill. Most
failed restores come from something on this list.

| # | Requirement | Check |
|---|---|---|
| 1 | Same OpenShift minor, same ACM/MCE and OADP versions as the primary | `oc get clusterversion`, `oc get csv -A \| grep -E 'advanced-cluster\|multicluster-engine\|oadp'` |
| 2 | HyperShift available through MCE (the `hypershift` component and `local-cluster` hosting) | `oc get deploy operator -n hypershift` |
| 3 | An **AgentServiceConfig** exists and assisted-service is running. OADP does **not** restore it | `oc get agentserviceconfig agent`, `oc get pvc -n multicluster-engine` (all `Bound`) |
| 4 | A StorageClass with the **same name** the etcd PVCs use on the primary, or a Velero storage-class mapping (Appendix A) | `oc get sc` |
| 5 | A VolumeSnapshotClass for that driver, labelled for Velero | [2.6](#26-volumesnapshotclass) |
| 6 | OADP configured, location `Available`, the primary's backups visible | `oc get backup.velero.io -n openshift-adp` |
| 7 | A load-balancer address in the DR segment for the hosted cluster's API (see below) | `oc get ipaddresspool -n metallb-system` |
| 8 | The release image and its payload are pullable from the DR hub, from the registry or the mirror the primary uses | `oc adm release info <release-image>` from the DR hub |
| 9 | Network: the workers can reach the DR API VIP on TCP 6443 and the DR hub's ingress VIP on TCP 443 | test from a worker, or from a host in the worker segment |
| 10 | DNS: you can repoint the hosted cluster's API name, and, if the DR hub keeps the primary's apps domain, the hub's `*.apps` name | your DNS change procedure |
| 11 | If you use BareMetalHosts: the DR hub reaches the workers' BMC addresses | test from the DR hub |

### API address in the DR segment

The restored `kube-apiserver` Service in `$HCP_NS` is of type
`LoadBalancer`. On the DR hub it needs an address from the DR segment. A
dedicated single-address MetalLB pool per hosted cluster, restricted to
that cluster's control-plane namespace, makes the assignment
deterministic:

```yaml
apiVersion: metallb.io/v1beta1
kind: IPAddressPool
metadata:
  name: <hc-name>-api
  namespace: metallb-system
spec:
  addresses:
    - <dr-api-vip>/32
  autoAssign: true
  serviceAllocation:
    priority: 10
    namespaces:
      - <hcp-namespace>       # $HCP_NS
---
apiVersion: metallb.io/v1beta1
kind: L2Advertisement         # or a BGPAdvertisement, to match the DR network
metadata:
  name: <hc-name>-api
  namespace: metallb-system
spec:
  ipAddressPools:
    - <hc-name>-api
```

Use the **same pool name on both hubs**, each holding that hub's own
address. Any reference to the pool then stays valid after the restore.

### Hub ingress in the DR segment

The workers reach the control plane's OAuth, Konnectivity and Ignition
endpoints through Routes on the **hub's** ingress. After a restore these
Routes are served by the DR hub's router, on the DR hub's ingress VIP.
Which way it works depends on the DR hub's apps domain:

- **The DR hub has its own apps domain** (for example
  `*.apps.dr-hub.example.com`). The OpenShift Velero plugin drops the
  generated host names when it restores Routes, so they are regenerated
  under the DR domain, and HyperShift reconfigures the data plane to use
  them. DNS for the DR apps domain must already resolve to the DR ingress
  VIP from the workers' segment.
- **The DR hub reuses the primary's apps domain.** Then the hub's
  `*.apps` record has to move to the DR ingress VIP as part of the DNS
  cutover ([5.5](#55-dns-cutover)).

### DNS TTLs

Lower the TTL on the hosted cluster's `api` and `api-int` records (and on
the hub `*.apps` record, if it has to move) to something like 60 seconds,
**ahead of time**. DR cutover time is then bounded by that TTL, not by a
day-long cache.

## 4. Backup

### 4.1 Backup definition

`includedResources` lists only what a hosted cluster needs.
`includeClusterResources` stays unset: setting it to `true` would back up
every cluster-scoped object on the hub.

```yaml
apiVersion: velero.io/v1
kind: Backup
metadata:
  name: <hc-name>-<yyyymmdd-hhmm>
  namespace: openshift-adp
  labels:
    velero.io/storage-location: default
spec:
  includedNamespaces:
    - <hc-namespace>          # $HC_NS
    - <hcp-namespace>         # $HCP_NS
    - <agent-namespace>       # $AGENT_NS
  includedResources:
    - sa
    - role
    - rolebinding
    - pod
    - pvc
    - pv
    - configmap
    - secrets
    - services
    - deployments
    - statefulsets
    - priorityclasses
    - pdb
    - route
    - hostedcluster
    - hostedcontrolplane
    - nodepool
    - cluster
    - machinedeployment
    - machineset
    - machine
    - agentcluster
    - agentmachinetemplate
    - agentmachine
    - clusterdeployment
    - infraenv
    - agents
    - nmstateconfig
    - bmh
  excludedResources: []
  snapshotVolumes: true
  snapshotMoveData: true
  datamover: velero
  defaultVolumesToFsBackup: false
  storageLocation: default
  ttl: 720h0m0s               # 30 days
```

`nmstateconfig` matters when the workers use static networking: without
it, a worker that has to rejoin through the discovery ISO has no network
configuration on the DR hub.

### 4.2 Run and verify an on-demand backup

```bash
oc apply -f backup.yaml
oc get backup.velero.io <name> -n openshift-adp -w
oc get datauploads.velero.io -n openshift-adp -l velero.io/backup-name=<name>
```

The backup is good when, in order of how much each one tells you:

1. **One `DataUpload` per etcd member, all `Completed`**, with
   `BYTES DONE` equal to `TOTAL BYTES` (3 for a highly available control
   plane, 1 for a single-replica one). This is the proof that the etcd
   data reached S3.
2. **`status.backupItemOperationsCompleted` equals
   `status.backupItemOperationsAttempted`**, and equals the number of
   etcd volumes.
3. **`status.phase` is `Completed`.** This is the weakest signal. A
   backup can be `Completed` having snapshotted nothing (no labelled
   VolumeSnapshotClass), or `PartiallyFailed` with the etcd data intact.

```bash
oc get backup.velero.io <name> -n openshift-adp -o jsonpath='{.status.phase}  ops={.status.backupItemOperationsCompleted}/{.status.backupItemOperationsAttempted}  items={.status.progress.itemsBackedUp}/{.status.progress.totalItems}{"\n"}'
velero backup describe <name> --details
```

Take an on-demand backup before any planned change to the hub or the
hosted cluster (upgrades, NodePool changes), in addition to the schedule.

### 4.3 Automate: daily scheduled backup

Run one Schedule per hosted cluster on the primary hub. Its template is
the backup spec from [4.1](#41-backup-definition):

```yaml
apiVersion: velero.io/v1
kind: Schedule
metadata:
  name: <hc-name>-daily
  namespace: openshift-adp
spec:
  schedule: "0 2 * * *"              # daily at 02:00 UTC; pick a quiet hour
  useOwnerReferencesInBackup: false  # deleting the Schedule must not delete its backups
  template:
    includedNamespaces:
      - <hc-namespace>
      - <hcp-namespace>
      - <agent-namespace>
    includedResources:
      - sa
      - role
      - rolebinding
      - pod
      - pvc
      - pv
      - configmap
      - secrets
      - services
      - deployments
      - statefulsets
      - priorityclasses
      - pdb
      - route
      - hostedcluster
      - hostedcontrolplane
      - nodepool
      - cluster
      - machinedeployment
      - machineset
      - machine
      - agentcluster
      - agentmachinetemplate
      - agentmachine
      - clusterdeployment
      - infraenv
      - agents
      - nmstateconfig
      - bmh
    excludedResources: []
    snapshotVolumes: true
    snapshotMoveData: true
    datamover: velero
    defaultVolumesToFsBackup: false
    storageLocation: default
    ttl: 720h0m0s                    # keeps 30 daily backups
```

Backups are named `<hc-name>-daily-<timestamp>`.

```bash
oc get schedule.velero.io -n openshift-adp
oc get backup.velero.io -n openshift-adp -l velero.io/schedule-name=<hc-name>-daily
```

Recommendations:

- **Stagger schedules** when the hub runs several hosted clusters, for
  example 15 minutes apart, so the data movers don't all run at once.
- **Keep the schedule off the hub's maintenance windows.**
- **Alert on missing backups.** Velero exposes Prometheus metrics through
  the `openshift-adp-velero-metrics-svc` Service. Scrape it with user
  workload monitoring, and alert when
  `velero_backup_last_successful_timestamp{schedule="<hc-name>-daily"}`
  is older than 26 hours, or when `velero_backup_failure_total`
  increases.
- **Do not run the Schedule on the DR hub** during normal operations.
  Create it there only when the DR hub takes over
  ([6.2](#62-take-over-backups-on-the-dr-hub)).
- **Retention:** `ttl` controls it. 30 daily backups is a reasonable
  default; match it to your recovery-point policy.

## 5. Restore on the DR hub

### 5.1 Decide and fence

A restore is a decision, not just a command. Before starting:

1. **Declare the DR event**, through your normal incident or change
   process, and record who decided and when.
2. **Fence the primary hub.** It must never run the same hosted cluster's
   control plane at the same time as the DR hub. Two control planes would
   both try to reconcile the same workers, and if BareMetalHosts are in
   use, both would hold the BMC credentials. Confirm the primary is down
   and stays down (powered off, or isolated from the worker and BMC
   networks).
3. **For a planned migration** (the primary is still healthy), do this on
   the primary instead:
   ```bash
   # 1. a final backup (4.2), verified
   # 2. stop HyperShift reconciling the cluster there
   oc patch hostedcluster $HC_NAME -n $HC_NS --type merge -p '{"spec":{"pausedUntil":"true"}}'
   oc get nodepool -n $HC_NS -o name | xargs -I{} oc patch {} -n $HC_NS --type merge -p '{"spec":{"pausedUntil":"true"}}'
   # 3. stop its control plane
   oc scale deploy --all --replicas=0 -n $HCP_NS
   oc scale statefulset --all --replicas=0 -n $HCP_NS
   # 4. if BareMetalHosts are in use: stop the primary from managing them
   oc annotate bmh --all -n $AGENT_NS baremetalhost.metal3.io/detached="" --overwrite
   ```
4. **Pick the backup.** Normally the most recent one with
   `phase: Completed` and all `DataUpload`s completed. Record its name and
   its time: that time is your recovery point.
5. **Run through [section 3](#3-dr-hub-readiness).**

### 5.2 Confirm the backup on the DR hub

```bash
oc get backup.velero.io -n openshift-adp --sort-by=.status.startTimestamp
velero backup describe <backup> --details | head -40
```

If the backup isn't listed, Velero hasn't synced it from S3 yet. Check
that the backup location is `Available` and uses the same bucket and
prefix as the primary.

The namespaces being restored must not already exist on the DR hub. If an
earlier restore attempt left them behind, remove them first: delete the
HostedCluster and NodePools, wait for `$HCP_NS` to go away, then delete
the leftover namespaces.

### 5.3 BareMetalHosts: decide before restoring

*This is not validated in the drill behind this runbook, whose workers
had no BareMetalHost objects. The procedure to validate it is
[BMH-DR-test.md](BMH-DR-test.md).*

If `$AGENT_NS` holds BareMetalHosts, the DR hub's Bare Metal Operator
takes them over as soon as they're restored. A host it doesn't recognize
as already provisioned may be inspected or provisioned, which reboots a
running worker. To prevent that, restore them **detached**, so the
operator leaves them alone, with a Velero resource modifier:

```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: hcp-restore-modifiers
  namespace: openshift-adp
data:
  modifiers.yaml: |
    version: v1
    resourceModifierRules:
      - conditions:
          groupResource: baremetalhosts.metal3.io
        mergePatches:
          - patchData: |
              metadata:
                annotations:
                  baremetalhost.metal3.io/detached: ""
```

Reference it from the Restore (`spec.resourceModifier`, below). Remove
the annotation once the cluster is healthy and you've confirmed each
host's state (`oc get bmh -n $AGENT_NS`). Rehearse this in a DR drill
before relying on it.

### 5.4 Restore

```yaml
apiVersion: velero.io/v1
kind: Restore
metadata:
  name: <hc-name>-restore-<yyyymmdd-hhmm>
  namespace: openshift-adp
spec:
  backupName: <backup>
  restorePVs: true
  existingResourcePolicy: update
  excludedResources:
    - nodes
    - events
    - events.events.k8s.io
    - backups.velero.io
    - restores.velero.io
    - resticrepositories.velero.io
  # resourceModifier:                # only with BareMetalHosts, see 5.3
  #   kind: ConfigMap
  #   name: hcp-restore-modifiers
```

```bash
oc apply -f restore.yaml
oc get restore.velero.io <name> -n openshift-adp -w
oc get datadownloads.velero.io -n openshift-adp -w
```

The restore is good when:

- **There is one `DataDownload` per etcd member, all `Completed`**, with
  byte counts matching the backup's `DataUpload`s.
- **`phase` is `Completed`** and
  `restoreItemOperationsCompleted == restoreItemOperationsAttempted`.
- **Warnings are explained.** Read them with
  `velero restore describe <name>`. Two warnings are common and harmless:
  `secrets "token-<nodepool>-<hash>" not found` and
  `secrets "user-data-<nodepool>-<hash>" not found` in `$HCP_NS`.
  HyperShift's NodePool controller generates and replaces those secrets
  itself, and had already done so. The NodePool reaching its desired node
  count confirms it.

Then confirm the control plane came up and took a DR-segment address:

```bash
oc get pods -n $HCP_NS | grep -vE 'Running|Completed'     # nothing left starting or failing
oc get svc kube-apiserver -n $HCP_NS \
  -o jsonpath='{.status.loadBalancer.ingress[0].ip}{"\n"}'   # the DR API VIP
oc get hostedcluster $HC_NAME -n $HC_NS \
  -o jsonpath='{.status.conditions[?(@.type=="HostedClusterRestoredFromBackup")].reason}{"\n"}'
#   RecoveryFinished
```

The `hypershift` plugin marks the restored HostedCluster with the
`hypershift.openshift.io/restored-from-backup` annotation. HyperShift
uses it to run its recovery, sets `HostedClusterRestoredFromBackup=True`,
then removes the annotation.

### 5.5 DNS cutover

Point the names at the DR segment:

| Record | New target |
|---|---|
| `api.<hosted-cluster-domain>` | DR API VIP (the `kube-apiserver` Service address above) |
| `api-int.<hosted-cluster-domain>`, if you publish it | DR API VIP |
| `*.apps.<hub-domain>`, **only** if the DR hub reuses the primary's apps domain | DR hub ingress VIP |

The hosted cluster's own `*.apps.<hosted-cluster-domain>` does **not**
change. It is served by the hosted cluster's ingress on the workers,
which haven't moved.

Then check resolution **from a worker**, not just from your workstation.
Resolvers along the way cache the old answer for up to the record's TTL:

```bash
ssh core@<worker> 'getent hosts api.<hosted-cluster-domain>'   # the DR API VIP
```

Kubelets re-resolve and reconnect within a few minutes of the cache
expiring. Running `systemctl restart kubelet` on a worker forces it.

### 5.6 Import secret

ACM's `<hc-name>-import` secret in the ManagedCluster namespace holds a
bootstrap token that only the hub that created it accepts. If that
namespace was in the backup (`HC_NS == HC_NAME`, see
[Before you start](#before-you-start-what-gets-backed-up)), the restore
can bring back the primary's token. The managed cluster then stays in
`Importing`, with `Unauthorized` errors in the klusterlet registration
agent.

- **On an OADP version whose hypershift plugin includes the fix for
  OCPBUGS-121709**, the plugin skips that secret during restore, and MCE
  generates a new one. Confirm it in the Velero log:
  ```bash
  oc logs deploy/velero -n openshift-adp | grep "Skipping restore of ACM/MCE import secret"
  ```
- **On an earlier version**, delete the restored secret so MCE generates
  a new one:
  ```bash
  oc delete secret ${HC_NAME}-import -n $HC_NAME
  ```

Either way, the new secret has a creation time after the restore and no
`velero.io/restore-name` label:

```bash
oc get secret ${HC_NAME}-import -n $HC_NAME \
  -o jsonpath='{.metadata.creationTimestamp}  {.metadata.labels}{"\n"}'
```

### 5.7 Verify

On the DR hub:

```bash
oc get hostedcluster $HC_NAME -n $HC_NS                       # AVAILABLE True, PROGRESSING False
oc get hostedcluster $HC_NAME -n $HC_NS \
  -o jsonpath='{range .status.conditions[*]}{.type}={.status}{"\n"}{end}'
oc get nodepool -n $HC_NS                                     # CURRENT NODES = DESIRED NODES
oc get agents -n $AGENT_NS                                    # bound to the cluster, no new installs
oc get managedcluster $HC_NAME                                # JOINED True, AVAILABLE True
```

On the hosted cluster, using its admin kubeconfig from the DR hub:

```bash
oc extract secret/${HC_NAME}-admin-kubeconfig -n $HC_NS --keys=kubeconfig --to=- > hc.kubeconfig
export KUBECONFIG=$PWD/hc.kubeconfig

oc get nodes                                       # all Ready, ages unchanged (not reprovisioned)
oc get co | grep -v 'True.*False.*False'           # nothing unavailable, progressing or degraded
oc get pods -A | grep -vE 'Running|Completed'      # nothing stuck
```

Then check the workloads with their owners, and record the restore's
start and end times, the backup used, and the outcome.

## 6. After the restore

### 6.1 BareMetalHosts

If you restored them detached ([5.3](#53-baremetalhosts-decide-before-restoring)),
check each host's state, then remove the annotation so the DR hub can
manage them again:

```bash
oc get bmh -n $AGENT_NS
oc annotate bmh --all -n $AGENT_NS baremetalhost.metal3.io/detached-
```

### 6.2 Take over backups on the DR hub

The DR hub is now the active hub, so it takes the backups:

1. Switch its backup location to `accessMode: ReadWrite` (in the DPA).
2. Create the daily Schedule there ([4.3](#43-automate-daily-scheduled-backup)).
3. Take an on-demand backup right away and verify it
   ([4.2](#42-run-and-verify-an-on-demand-backup)). It is the first
   recovery point that holds the cluster's new state.

### 6.3 When the primary hub comes back

The primary still holds a copy of the HostedCluster, NodePools, Agents
and possibly BareMetalHosts, all now stale. Before reconnecting it to the
worker or BMC networks:

- **Keep it from acting on the workers.** Pause the HostedCluster and
  NodePools, scale down `$HCP_NS`, and detach any BareMetalHosts, as in
  [5.1](#51-decide-and-fence) step 3.
- **Do not delete the HostedCluster or NodePools there** while the
  primary can reach the workers or their BMCs. Deleting them releases the
  Agents and can deprovision hosts that now belong to the DR hub's
  cluster.
- **Set its backup location to `ReadOnly`** and remove its Schedule, so
  it neither adds to nor expires backups in the shared bucket.
- Plan the cleanup, or a failback, as its own change. **Failback is this
  same procedure in reverse**: back up on the DR hub, fence it, restore on
  the primary, move DNS back.

### 6.4 DR drills

Rehearse the whole restore regularly, for example twice a year and after
every hub upgrade. Measure the time from declaring the event to a healthy
cluster. Most of what goes wrong (a missing AgentServiceConfig or
StorageClass, an unlabelled VolumeSnapshotClass, DNS caching) is only
found in a real run.

## Appendix A: troubleshooting

| Symptom | Likely cause | Action |
|---|---|---|
| Backup `Completed` but no `DataUpload`s | No VolumeSnapshotClass labelled for the etcd PVCs' driver | [2.6](#26-volumesnapshotclass); take the backup again |
| Backup fails with `backup already exists in object storage` | A Backup object was deleted with `oc delete`, which leaves its data in S3 | Wait for it to resync, then `velero backup delete <name> --confirm`; never reuse a backup name |
| Backup location `Unavailable` | Credentials, endpoint, CA or bucket | `.status.message` on the BackupStorageLocation ([2.5](#25-verify-the-backup-location)) |
| DR hub doesn't list the primary's backups | Different bucket or prefix, or the location is `Unavailable` | [2.8](#28-if-oadp-is-already-configured-checks) |
| Restored PVCs stay `Pending` | No StorageClass with the source's name on the DR hub | Create one, or map storage classes with a ConfigMap in `openshift-adp` labelled `velero.io/change-storage-class: RestoreItemAction` and `velero.io/plugin-config: ""`, with data `<old-class>: <new-class>` |
| assisted-service not running on the DR hub | No AgentServiceConfig (OADP doesn't restore it), or its PVCs have no default StorageClass | Create the AgentServiceConfig; check `oc get pvc -n multicluster-engine` |
| `kube-apiserver` Service has no external IP | No load-balancer pool in the DR segment that may serve `$HCP_NS` | [Section 3](#api-address-in-the-dr-segment) |
| Workers `NotReady` after the restore | DNS still points at the old API VIP, or the DR VIP isn't reachable from the worker segment | [5.5](#55-dns-cutover); test TCP 6443 from a worker |
| `oc logs` / `oc exec` into hosted-cluster pods fail, metrics unavailable | Konnectivity can't reach the DR hub's ingress | Workers need TCP 443 to the DR ingress VIP, and the DR apps domain must resolve from the worker segment |
| ManagedCluster stuck in `Importing`, `Unauthorized` in the klusterlet logs | The primary's import secret was restored | [5.6](#56-import-secret) |
| `oc get backup` shows nothing | The short name resolved to another operator's `Backup` kind | Use `backup.velero.io` |

## Appendix B: validated versions

The flow in this runbook (CSI snapshots plus the data mover, a restore
onto a DR hub in a different network segment with a new API VIP, Agent
platform workers re-adopted without reprovisioning) was exercised end to
end with:

| Component | Version |
|---|---|
| OpenShift (both hubs) | 4.22.10 |
| ACM / MCE | 2.17.3 / 2.17.3 |
| OADP | 1.6.1 |
| HyperShift OADP plugin | Builds including the OCPBUGS-121709 fix: upstream `main`, and the `oadp-1.6` branch with the backport ([openshift/hypershift-oadp-plugin#320](https://github.com/openshift/hypershift-oadp-plugin/pull/320)) |

Two things were **not** part of that run: BareMetalHost objects (the
workers booted from the discovery ISO) and object lock on the bucket.
Rehearse both in your own drill before relying on them. The full test
record is [OCPBUGS-121709-test.md](OCPBUGS-121709-test.md).
