# OCPBUGS-121709: testing the import-secret fix on OADP 1.6

How the fix from
[openshift/hypershift-oadp-plugin#316](https://github.com/openshift/hypershift-oadp-plugin/pull/316)
is built for OADP 1.6 and tested with the hub1 → hub2 restore in
[steps.md](../steps.md).

## The problem

When a hosted cluster is restored onto a different ACM/MCE hub, Velero
also restores the `<hc-name>-import` secret from the source hub. Its
bootstrap ServiceAccount token was minted by hub1, so hub2 rejects it. The
klusterlet cannot create its CSR and the `ManagedCluster` stays in
`Importing` (or `Pending`/`Unknown`).

The workaround today is
[step 12 of steps.md](../steps.md#12-exclude-acms-import-secret-from-the-backup):
label the secret `velero.io/exclude-from-backup=true` before the backup.

## The fix

The fix is in the plugin's **restore** path, not in the backup. The secret
is still in the backup. `pkg/core/restore.go` gains a `Secret` case in the
plugin's restore step:

```go
case kind == "Secret":
    if strings.HasSuffix(metadata.GetName(), common.ImportSecretSuffix) {   // "-import"
        p.log.Infof("Skipping restore of ACM/MCE import secret %s/%s (contains stale bootstrap token)", ...)
        return velero.NewRestoreItemActionExecuteOutput(input.Item).WithoutRestore(), nil
    }
```

`WithoutRestore()` makes Velero drop the item. This is the same way the
plugin already skips Pods and the etcd StatefulSet. On hub2, MCE then sees
no import secret for the managed cluster, generates a new one with a token
from hub2, and the import completes.

According to the commit message, the plugin's backup step cannot drop
items, so the restore step is the only place the plugin can do this.

Consequences:

- The fix only has to run on the **destination** hub, where the restore
  happens. Backups taken with any plugin version, including older backups
  that contain the secret, restore correctly.
- The plugin only acts on backups whose `includedResources` names
  `hostedcluster`, `hostedcontrolplane` or `nodepool` (or `*`).
  `oadp/templates/backup-hcp-cluster*.yaml.j2` include `hostedcluster`.
- It matches by name suffix only. Any secret ending in `-import` in the
  restored namespaces is skipped, not just ACM's.

Status: merged to upstream `main` on 2026-09-28 (`213d7da`, merge
`7aa4288`). It was not on the `oadp-1.6` branch when this test was
written, so there is no OADP release image with the fix yet.

## 1. Get a plugin image with the fix

There are two ways to get the image:

- **You already have an image**, e.g. one the developer built: follow
  1.0, then 1.4, and skip the build steps.
- **You build it yourself** from `oadp-1.6` plus the cherry-pick: follow
  1.1–1.4.

Either way, the result is an image reference in `$IMG`. Section 3.1
passes it to the DPA override.

### 1.0 If you already have an image: just use it

Set `IMG` to the image you were given and skip to 1.4. For example, the
developer's build of upstream `main`:

```bash
IMG=quay.io/jparrill/hypershift-oadp-plugin:main-check-skip-import
```

Pin it by digest so the results name exactly what was tested. Someone can
push a new image to a tag, but a digest never changes:

```bash
skopeo inspect --override-arch amd64 docker://$IMG | jq '{Digest, Architecture, Created}'
skopeo inspect --override-arch amd64 --config docker://$IMG | jq '.config.Entrypoint'
# Architecture must be "amd64"; Entrypoint must copy /plugins/* to /target

IMG=quay.io/jparrill/hypershift-oadp-plugin@sha256:<Digest from above>
```

Ask whoever built the image which commit it came from, and record it in
the results table. An image built from upstream `main` is fine under
OADP 1.6. Compared with `oadp-1.6`, `main`'s plugin code adds only:

- this fix;
- a one-line `HCPEtcdBackup` status change, which the `csi` backup
  method does not use.

Its Velero dependency is the same 1.18.x line. If hub2 is disconnected,
mirror the image into the mirror registry and use that address in `IMG`.

### 1.1 Cherry-pick onto `oadp-1.6`

```bash
git clone https://github.com/openshift/hypershift-oadp-plugin.git
cd hypershift-oadp-plugin
git checkout -b oadp-1.6-ocpbugs-121709 origin/oadp-1.6
git cherry-pick -x 213d7da419d079b6376096664fc8d4a87a708347
```

The only conflict is `ARCHITECTURE.md`, which does not exist on `oadp-1.6`.
Drop it:

```bash
git rm ARCHITECTURE.md
git cherry-pick --continue --no-edit
```

`pkg/common/types.go`, `pkg/core/restore.go` and `pkg/core/restore_test.go`
apply cleanly.

### 1.2 Unit tests (Go 1.26)

```bash
GOFLAGS=-mod=vendor go test ./pkg/...
GOFLAGS=-mod=vendor go test ./pkg/core/ -run TestRestoreExecuteImportSecretExclusion -v
```

The new test covers three cases: `my-hosted-cluster-import` is skipped,
`pull-secret` is restored, and `import-credentials` is restored.

### 1.3 Build and push

The hubs are x86_64. Push to a registry hub2 can pull from: quay.io, or
the mirror registry if the hub is disconnected.

```bash
IMG=quay.io/<you>/hypershift-oadp-plugin:oadp-1.6-ocpbugs-121709
```

**Option A: the repo's Dockerfile.** Its builder image is
`registry.ci.openshift.org/openshift/release:rhel-9-release-golang-1.26-openshift-4.23`,
which may need a login to the OpenShift CI registry.

```bash
podman build --platform linux/amd64 -t $IMG -f Dockerfile .
podman push $IMG
```

**Option B: build the binary locally** and package it in the same
runtime image:

```bash
CGO_ENABLED=0 GOOS=linux GOARCH=amd64 GOFLAGS=-mod=vendor \
  go build -o bin/hypershift-oadp-plugin .

cat > Containerfile.local <<'EOF'
FROM registry.access.redhat.com/ubi9-minimal
RUN mkdir /plugins
COPY bin/hypershift-oadp-plugin /plugins/
USER 65532:65532
ENTRYPOINT ["/bin/bash", "-c", "cp /plugins/* /target/."]
EOF

podman build --platform linux/amd64 -t $IMG -f Containerfile.local .
podman push $IMG
```

Keep the `ENTRYPOINT`. OADP runs plugin images as init containers that
copy the binary into `/target`. (`Dockerfile.oadp` is the downstream build
and needs Red Hat's internal brew registry.)

### 1.4 Check that the image contains the fix

Run this for either kind of image, the one you were given or the one you
built:

```bash
podman pull --platform linux/amd64 $IMG
cid=$(podman create $IMG); podman cp $cid:/plugins/hypershift-oadp-plugin /tmp/plugin; podman rm $cid
grep -ac "Skipping restore of ACM/MCE import secret" /tmp/plugin   # 1 or more
```

## 2. Run the backup without the workaround

Follow [steps.md](../steps.md) up to step 11 as usual. Then make sure the
import secret **is** backed up, so the restore actually exercises the fix.

### 2.1 Skip step 12: remove the exclude label

```bash
oc label secret hcp-cluster1-import -n hcp-cluster1 velero.io/exclude-from-backup-
oc get secret hcp-cluster1-import -n hcp-cluster1 --show-labels   # no velero.io/exclude-from-backup
```

MCE reconciles this secret. Check the label again right before step 13.

### 2.2 Step 13: backup

```bash
ansible-playbook -i inventory/hosts backup_hosted_cluster.yaml --ask-vault-pass \
  -e hcp_cluster_name=hcp-cluster1 -e oadp_backup_method=csi
```

Confirm the secret is in the backup:

```bash
oc exec deploy/velero -c velero -n openshift-adp -- \
  ./velero backup describe hcp-cluster1-backup-csi --details \
  | grep hcp-cluster1-import
```

You can also download the backup tarball and look for
`resources/secrets/namespaces/hcp-cluster1/hcp-cluster1-import.json`.

## 3. Restore on hub2 with the patched plugin

Run steps 14–19 as usual. Hub2 must be on OADP 1.6.

### 3.1 Step 20 with the patched plugin image

**This is the step that makes hub2 use the new image.** It works the same
whether you were given `$IMG` (1.0) or built it (1.1–1.3). Run step 20
(`setup_oadp.yaml`) with `-e oadp_hypershift_plugin_image`:

```bash
export KUBECONFIG=/var/lib/libvirt/images/hub2_install/auth/kubeconfig

ansible-playbook -i inventory/hosts setup_oadp.yaml --ask-vault-pass \
  -e target_hub=hub2 -e oadp_backup_method=csi \
  -e oadp_hypershift_plugin_image=$IMG
```

How the image gets from that variable into Velero:

1. `roles/setup-oadp/templates/dpa.yaml.j2` adds this to the DPA when the
   variable is set:
   ```yaml
   spec:
     unsupportedOverrides:
       hypershiftPluginImageFqin: <IMG>
   ```
2. The OADP operator reads that key when it builds the `velero`
   Deployment. In the `oadp-1.6` operator source,
   `getHypershiftPluginImage()` in `pkg/credentials/credentials.go` uses
   it instead of `RELATED_IMAGE_HYPERSHIFT_VELERO_PLUGIN`.
3. The operator sets that image on the `hypershift-oadp-plugin` init
   container of `deploy/velero`, and the Deployment rolls out. The init
   container copies the plugin binary into the Velero pod.
   A tag (not a digest) gets `imagePullPolicy: Always`, so re-pushing the
   same tag is pulled again on the next rollout.

The playbook prints the image it ended up with ("hypershift plugin
image: ..."). To check by hand:

```bash
oc get dpa dpa-instance -n openshift-adp -o jsonpath='{.spec.unsupportedOverrides}{"\n"}'
oc rollout status deploy/velero -n openshift-adp
oc get deploy velero -n openshift-adp \
  -o jsonpath='{.spec.template.spec.initContainers[?(@.name=="hypershift-oadp-plugin")].image}{"\n"}'
oc get pod -n openshift-adp -l deploy=velero \
  -o jsonpath='{range .items[*]}{.metadata.name}{"  "}{.status.initContainerStatuses[?(@.name=="hypershift-oadp-plugin")].imageID}{"\n"}{end}'
```

The last command shows the digest the pod actually pulled. Record it in
the results table. Select the pod with `deploy=velero`, not
`component=velero`: the node-agent pods carry `component=velero` too, and
they have no plugin init container.

If you'd rather not re-run the playbook, this patch does the same thing:

```bash
oc patch dpa dpa-instance -n openshift-adp --type merge \
  -p '{"spec":{"unsupportedOverrides":{"hypershiftPluginImageFqin":"'"$IMG"'"}}}'
```

`unsupportedOverrides` is for testing only. Running step 20 again
without the variable removes the override and goes back to the
operator's image.

### 3.2 Step 21: restore

```bash
ansible-playbook -i inventory/hosts restore_hosted_cluster.yaml --ask-vault-pass \
  -e hcp_cluster_name=hcp-cluster1 -e target_hub=hub2 -e oadp_backup_method=csi
```

Continue with steps 22 (DNS cutover) and 23 (verify).

## 4. Verify

```bash
# 1. The plugin skipped the secret
oc logs deploy/velero -n openshift-adp | grep "Skipping restore of ACM/MCE import secret"

# 2. The secret on hub2 was generated by MCE, not restored by Velero
oc get secret hcp-cluster1-import -n hcp-cluster1 \
  -o jsonpath='{.metadata.creationTimestamp}{"\n"}{.metadata.labels}{"\n"}'
oc get restore.velero.io hcp-cluster1-restore-csi -n openshift-adp \
  -o jsonpath='{.status.startTimestamp}{"\n"}'

# 3. The cluster imported
oc get managedcluster hcp-cluster1
```

| Check | Pass when |
|---|---|
| Velero log | Contains `Skipping restore of ACM/MCE import secret hcp-cluster1/hcp-cluster1-import` |
| Import secret | `creationTimestamp` is after the restore started, and there are no `velero.io/restore-name` / `velero.io/backup-name` labels |
| ManagedCluster | `JOINED=True`, `AVAILABLE=True`, not stuck in `Importing` |
| Hosted cluster | Step 23 checks pass |

### Negative control (optional)

Repeat section 3 with the same backup, but run step 20 **without**
`-e oadp_hypershift_plugin_image`. That puts the operator's stock plugin
image back. The stock plugin restores `hcp-cluster1-import`: it carries
`velero.io/restore-name`, and the `ManagedCluster` stays in `Importing`.
This shows the backup really contained the stale secret and the fix is
what changed the outcome.

## 5. Results

**Outcome: PASS.** Hub2 restored the hosted cluster from a backup that
contained the stale `hcp-cluster1-import` secret. The patched plugin
skipped that secret, MCE on hub2 generated a new one, and the cluster
imported (`JOINED=True`, `AVAILABLE=True`) without the step 12
workaround.

| Item | Value |
|---|---|
| Date | 2026-10-21 |
| OCP version (hub1 / hub2) | 4.22.10 / 4.22.10. Hub1 was shut down before this was captured; it was built from the same `vars.yaml`, which pins the release (`ocp_major_version: "4.22"`, `ocp_minor_version: 10`) |
| ACM / MCE version | hub2: ACM 2.17.3 / MCE 2.17.3. Hub1: same subscription (`release-2.17`, automatic approval); exact z-stream not captured |
| OADP version | hub2: `oadp-operator.v1.6.1`. Hub1: same subscription (`stable`, automatic approval); exact z-stream not captured |
| Plugin image (tag and pulled digest) | `quay.io/jparrill/hypershift-oadp-plugin:main-check-skip-import`, pinned as `@sha256:9ecba766f3be1bfe5f631ac016bba160f6528c3493c233abf1b890b2de4a0237` |
| Image source (given / self-built) and source commit | Given: developer's build of upstream `main`, created 2026-10-19. Source commit: (pending from developer) |
| Image checks (1.4) | `amd64`; entrypoint `cp /plugins/* /target/.`; binary contains "Skipping restore of ACM/MCE import secret" (1 match) |
| Plugin image on hub2 (3.1) | DPA `unsupportedOverrides.hypershiftPluginImageFqin` set to the digest; `deploy/velero` rolled out; init container `hypershift-oadp-plugin` image and pod `velero-56d49c4857-m6nld` pulled `imageID` both equal the digest |
| Backup method | csi |
| Import secret in the backup (2.2) | Yes: `velero backup describe hcp-cluster1-backup-csi --details` lists `hcp-cluster1/hcp-cluster1-import` (step 12 exclude label removed) |
| Unit tests | PASS: `go test ./pkg/...` and `TestRestoreExecuteImportSecretExclusion` (3/3) on `oadp-1.6` @ `62cec95f` + cherry-pick of `213d7da` |
| Velero log shows skip | PASS: `07:03:30Z level=info msg="Skipping restore of ACM/MCE import secret hcp-cluster1/hcp-cluster1-import (contains stale bootstrap token)" ... logSource=".../pkg/core/restore.go:226" pluginName=hypershift-oadp-plugin restore=openshift-adp/hcp-cluster1-restore-csi` |
| Import secret regenerated on hub2 | PASS: restore started `2026-10-21T07:03:17Z`; `hcp-cluster1-import` created `2026-10-21T07:17:51Z`; its only label is `managedcluster-import-controller.open-cluster-management.io/import-secret`, with no `velero.io/restore-name` / `velero.io/backup-name`, so MCE created it and Velero did not |
| ManagedCluster state | PASS: `hcp-cluster1` `HUB ACCEPTED=true`, `JOINED=True`, `AVAILABLE=True`, URL `https://api.hcp-cluster1.mylab.com:6443` |
| HostedCluster recovery | `HostedClusterRestoredFromBackup=True`, reason `RecoveryFinished`, "Hosted cluster recovery finished", at `2026-10-21T07:11:27Z`. All other HostedCluster conditions healthy (`Available=True`, `Degraded=False`, `Progressing=False`; `EtcdBackupSucceeded=Unknown` is expected with the `csi` method). The `hypershift.openshift.io/restored-from-backup` annotation the plugin sets is gone, as expected: the HyperShift operator deletes it once the condition is `True` |
| Timeline (UTC) | 07:03:17 restore started; 07:03:30 plugin skipped the import secret; 07:11:27 HostedCluster recovery finished; 07:17:51 MCE created the new import secret |
| NodePool after restore | `nodepool-hcp-cluster1-1`: desired 2, current 2, version 4.22.10, `UPDATINGVERSION=False`, `UPDATINGCONFIG=False`, no message. The restore warning about its token secret had no effect |
| Hosted cluster after restore | Nodes `c1worker1`, `c1worker2` `Ready` (v1.35.6); `online-boutique` 11/11 pods `Running` |
| Restore outcome | `hcp-cluster1-restore-csi` `Completed`, 0 errors. CSI data movement for `data-etcd-0/1/2`: 3 of 3 completed |
| Restore warning (1) | `hcp-cluster1-hcp-cluster1: secrets "token-nodepool-hcp-cluster1-1-6e74db43" not found`. Unrelated to the import secret: it is a NodePool token secret in the control-plane namespace, which HyperShift's NodePool controller creates and rotates itself, so it was most likely replaced while Velero was processing it (Existing Resource Policy: `update`). The nodes rejoined `Ready` |
| Backup visible on hub2 | `hcp-cluster1-backup-csi` synced from S3 |
| Negative control | Not run |

Notes:

- Hub1 ran the stock OADP plugin image for the backup. The fix runs at
  restore time, so only hub2 needs the patched image.
- The unit tests ran on a self-built `oadp-1.6` cherry-pick. The image
  under test is the developer's `main` build. Both contain the same
  `restore.go` change.

### 5.1 Evidence

Commands as run and their output, verbatim, in the order of the test.
Each block says which cluster it ran against.

**Unit tests** (self-built `oadp-1.6` + cherry-pick of `213d7da`, section 1.2)

```console
$ GOFLAGS=-mod=vendor go test ./pkg/...
ok  	github.com/openshift/hypershift-oadp-plugin/pkg/azblobsas	0.092s
ok  	github.com/openshift/hypershift-oadp-plugin/pkg/common	0.163s
ok  	github.com/openshift/hypershift-oadp-plugin/pkg/core	0.244s
?   	github.com/openshift/hypershift-oadp-plugin/pkg/core/types	[no test files]
ok  	github.com/openshift/hypershift-oadp-plugin/pkg/core/validation	0.022s
ok  	github.com/openshift/hypershift-oadp-plugin/pkg/etcdbackup	0.220s
?   	github.com/openshift/hypershift-oadp-plugin/pkg/platform/agent	[no test files]
?   	github.com/openshift/hypershift-oadp-plugin/pkg/platform/aws	[no test files]
ok  	github.com/openshift/hypershift-oadp-plugin/pkg/s3presign	0.024s
ok  	github.com/openshift/hypershift-oadp-plugin/pkg/version	0.018s

$ GOFLAGS=-mod=vendor go test ./pkg/core/ -run TestRestoreExecuteImportSecretExclusion -v
=== RUN   TestRestoreExecuteImportSecretExclusion
=== RUN   TestRestoreExecuteImportSecretExclusion/When_restoring_an_ACM/MCE_import_secret,_it_should_skip_restore_to_avoid_stale_bootstrap_token
time="2026-10-19T02:20:53-04:00" level=info msg="Skipping restore of ACM/MCE import secret clusters/my-hosted-cluster-import (contains stale bootstrap token)"
=== RUN   TestRestoreExecuteImportSecretExclusion/When_restoring_a_regular_secret,_it_should_restore_normally
=== RUN   TestRestoreExecuteImportSecretExclusion/When_restoring_a_secret_with_import_in_the_middle_of_the_name,_it_should_restore_normally
--- PASS: TestRestoreExecuteImportSecretExclusion (0.11s)
    --- PASS: TestRestoreExecuteImportSecretExclusion/When_restoring_an_ACM/MCE_import_secret,_it_should_skip_restore_to_avoid_stale_bootstrap_token (0.11s)
    --- PASS: TestRestoreExecuteImportSecretExclusion/When_restoring_a_regular_secret,_it_should_restore_normally (0.00s)
    --- PASS: TestRestoreExecuteImportSecretExclusion/When_restoring_a_secret_with_import_in_the_middle_of_the_name,_it_should_restore_normally (0.00s)
PASS
ok  	github.com/openshift/hypershift-oadp-plugin/pkg/core	0.134s
```

**Plugin image checks** (workstation, section 1.4)

```console
$ IMG=quay.io/jparrill/hypershift-oadp-plugin:main-check-skip-import
$ skopeo inspect --override-arch amd64 docker://$IMG | jq '{Digest, Architecture, Created}'
{
  "Digest": "sha256:9ecba766f3be1bfe5f631ac016bba160f6528c3493c233abf1b890b2de4a0237",
  "Architecture": "amd64",
  "Created": "2026-10-19T10:09:05.717076323+02:00"
}
$ skopeo inspect --override-arch amd64 --config docker://$IMG | jq '.config.Entrypoint'
[
  "/bin/bash",
  "-c",
  "cp /plugins/* /target/."
]
$ podman pull --platform linux/amd64 $IMG
$ cid=$(podman create $IMG); podman cp $cid:/plugins/hypershift-oadp-plugin /tmp/plugin; podman rm $cid
$ grep -ac "Skipping restore of ACM/MCE import secret" /tmp/plugin
1
```

**The import secret is in the backup** (hub1, after step 13)

```console
$ oc exec deploy/velero -c velero -n openshift-adp -- \
    ./velero backup describe hcp-cluster1-backup-csi --details | grep hcp-cluster1-import
    - hcp-cluster1/hcp-cluster1-import
```

**Hub2 runs the patched plugin** (hub2, after step 20)

```console
$ oc get dpa dpa-instance -n openshift-adp -o jsonpath='{.spec.unsupportedOverrides}{"\n"}'
{"hypershiftPluginImageFqin":"quay.io/jparrill/hypershift-oadp-plugin@sha256:9ecba766f3be1bfe5f631ac016bba160f6528c3493c233abf1b890b2de4a0237"}

$ oc rollout status deploy/velero -n openshift-adp
deployment "velero" successfully rolled out

$ oc get deploy velero -n openshift-adp \
    -o jsonpath='{.spec.template.spec.initContainers[?(@.name=="hypershift-oadp-plugin")].image}{"\n"}'
quay.io/jparrill/hypershift-oadp-plugin@sha256:9ecba766f3be1bfe5f631ac016bba160f6528c3493c233abf1b890b2de4a0237

$ oc get pod -n openshift-adp -l deploy=velero \
    -o jsonpath='{range .items[*]}{.metadata.name}{"  "}{.status.initContainerStatuses[?(@.name=="hypershift-oadp-plugin")].imageID}{"\n"}{end}'
velero-56d49c4857-m6nld  quay.io/jparrill/hypershift-oadp-plugin@sha256:9ecba766f3be1bfe5f631ac016bba160f6528c3493c233abf1b890b2de4a0237

$ oc get backup.velero.io hcp-cluster1-backup-csi -n openshift-adp
NAME                      AGE
hcp-cluster1-backup-csi   3m55s
```

**The plugin skipped the secret** (hub2, after step 21)

```console
$ oc logs deploy/velero -n openshift-adp | grep "Skipping restore of ACM/MCE import secret"
Defaulted container "velero" out of: velero, openshift-velero-plugin (init), velero-plugin-for-aws (init), hypershift-oadp-plugin (init)
time="2026-10-21T07:03:30Z" level=info msg="Skipping restore of ACM/MCE import secret hcp-cluster1/hcp-cluster1-import (contains stale bootstrap token)" cmd=/plugins/hypershift-oadp-plugin logSource="/go/src/github.com/openshift/hypershift-oadp-plugin/pkg/core/restore.go:226" pluginName=hypershift-oadp-plugin process=restore restore=openshift-adp/hcp-cluster1-restore-csi type=hcp-restore
```

**MCE generated a new import secret** (hub2). It was created after the
restore started and carries no `velero.io/*` labels.

```console
$ oc get restore.velero.io hcp-cluster1-restore-csi -n openshift-adp -o jsonpath='{.status.startTimestamp}{"\n"}'
2026-10-21T07:03:17Z

$ oc get secret hcp-cluster1-import -n hcp-cluster1 \
    -o jsonpath='{.metadata.creationTimestamp}{"\n"}{.metadata.labels}{"\n"}'
2026-10-21T07:17:51Z
{"managedcluster-import-controller.open-cluster-management.io/import-secret":""}
```

**HostedCluster recovery finished** (hub2). The plugin's
`restored-from-backup` annotation is no longer present; HyperShift removes
it once this condition is `True`.

```console
$ oc get hostedcluster hcp-cluster1 -n hcp-cluster1 \
    -o jsonpath='{.status.conditions[?(@.type=="HostedClusterRestoredFromBackup")]}{"\n"}' | jq
{
  "lastTransitionTime": "2026-10-21T07:11:27Z",
  "message": "Hosted cluster recovery finished",
  "observedGeneration": 1,
  "reason": "RecoveryFinished",
  "status": "True",
  "type": "HostedClusterRestoredFromBackup"
}

$ oc get hostedcluster hcp-cluster1 -n hcp-cluster1 -o json | jq '.metadata.annotations | keys'
[
  "create-external-hub-kubeconfig",
  "hypershift.openshift.io/HasBeenAvailable",
  "kubectl.kubernetes.io/last-applied-configuration",
  "metallb.io/address-pool"
]

$ oc get hostedcluster hcp-cluster1 -n hcp-cluster1 \
    -o jsonpath='{range .status.conditions[*]}{.type}{"="}{.status}{"\n"}{end}'
ClusterVersionUpgradeable=True
ClusterVersionAvailable=True
ClusterVersionSucceeding=True
ClusterVersionProgressing=False
ClusterVersionReleaseAccepted=True
Degraded=False
EtcdAvailable=True
KubeAPIServerAvailable=True
InfrastructureReady=True
ExternalDNSReachable=True
ValidHostedControlPlaneConfiguration=True
ValidReleaseInfo=True
ValidIDPConfiguration=True
HostedClusterRestoredFromBackup=True
DataPlaneConnectionAvailable=True
ControlPlaneConnectionAvailable=True
EtcdBackupSucceeded=Unknown
AutoNodeEnabled=False
Available=True
ValidConfiguration=True
SupportedHostedCluster=True
ValidProxyConfiguration=True
IgnitionEndpointAvailable=True
ReconciliationActive=True
ValidReleaseImage=True
Progressing=False
PlatformCredentialsFound=True
ReconciliationSucceeded=True
ClusterVersionRetrievedUpdates=True
```

**NodePool after the restore** (hub2)

```console
$ oc get NodePool -n hcp-cluster1
NAME                      CLUSTER        DESIRED NODES   CURRENT NODES   AUTOSCALING   AUTOREPAIR   VERSION   UPDATINGVERSION   UPDATINGCONFIG   MESSAGE
nodepool-hcp-cluster1-1   hcp-cluster1   2               2               False         False        4.22.10   False             False
```

**The cluster imported** (hub2)

```console
$ oc get managedcluster hcp-cluster1
NAME           HUB ACCEPTED   MANAGED CLUSTER URLS                      JOINED   AVAILABLE   AGE
hcp-cluster1   true           https://api.hcp-cluster1.mylab.com:6443   True     True        5m6s
```

**Restore outcome and its one warning** (hub2)

```console
$ oc get restore.velero.io hcp-cluster1-restore-csi -n openshift-adp \
    -o jsonpath='{.status.phase}  warnings={.status.warnings}  errors={.status.errors}{"\n"}'
Completed  warnings=1  errors=

$ oc exec deploy/velero -c velero -n openshift-adp -- \
    ./velero restore describe hcp-cluster1-restore-csi | sed -n '/Warnings:/,/Errors:/p'
Warnings:
  Velero:     <none>
  Cluster:    <none>
  Namespaces:
    hcp-cluster1-hcp-cluster1:  secrets "token-nodepool-hcp-cluster1-1-6e74db43" not found

Backup:  hcp-cluster1-backup-csi
...
CSI Snapshot Restores:
  hcp-cluster1-hcp-cluster1/data-etcd-2:
    Data Movement: specify --details for more information
  hcp-cluster1-hcp-cluster1/data-etcd-1:
    Data Movement: specify --details for more information
  hcp-cluster1-hcp-cluster1/data-etcd-0:
    Data Movement: specify --details for more information

Existing Resource Policy:   update
ItemOperationTimeout:       4h0m0s
...
Restore Item Operations:  3 of 3 completed successfully, 0 failed (specify --details for more information)
```

**The hosted cluster after the restore** (hosted cluster kubeconfig)

```console
$ oc get nodes
NAME        STATUS   ROLES    AGE   VERSION
c1worker1   Ready    worker   16h   v1.35.6
c1worker2   Ready    worker   16h   v1.35.6

$ oc get po -n online-boutique
NAME                                     READY   STATUS    RESTARTS        AGE
adservice-6d857b79-j86b2                 1/1     Running   0               5h56m
cartservice-59c9b67cfb-dn7rn             1/1     Running   0               5h56m
checkoutservice-cb96487df-qw5rx          1/1     Running   0               5h56m
currencyservice-686d78b4bc-n2klz         1/1     Running   0               5h56m
emailservice-688545b8df-nm6sn            1/1     Running   2 (5h55m ago)   5h56m
frontend-b7c754496-82zzx                 1/1     Running   0               5h56m
paymentservice-869d5b5cb9-thjfm          1/1     Running   0               5h56m
productcatalogservice-859bfdcc-6567f     1/1     Running   0               5h56m
recommendationservice-86ddcd7749-j7mp4   1/1     Running   2 (5h55m ago)   5h56m
redis-cart-79d76df4b-phz9x               1/1     Running   0               5h56m
shippingservice-88557f497-778tq          1/1     Running   0               5h56m
```

**Versions** (hub2)

```console
$ oc get clusterversion -o jsonpath='{.items[0].status.desired.version}{"\n"}'
4.22.10
$ oc get csv -n openshift-adp -o custom-columns=NAME:.metadata.name,VERSION:.spec.version
NAME                                    VERSION
metallb-operator.v4.22.0-202610140833   4.22.0-202610140833
oadp-operator.v1.6.1                    1.6.1
$ oc get csv -n open-cluster-management -o custom-columns=NAME:.metadata.name,VERSION:.spec.version
NAME                                    VERSION
advanced-cluster-management.v2.17.3     2.17.3
metallb-operator.v4.22.0-202610140833   4.22.0-202610140833
$ oc get csv -n multicluster-engine -o custom-columns=NAME:.metadata.name,VERSION:.spec.version
NAME                                    VERSION
metallb-operator.v4.22.0-202610140833   4.22.0-202610140833
multicluster-engine.v2.17.3             2.17.3
```

## 6. Run 2: `oadp-1.6` image

The fix was backported to the plugin's `oadp-1.6` branch in
[openshift/hypershift-oadp-plugin#320](https://github.com/openshift/hypershift-oadp-plugin/pull/320)
(merged `f573a597`, 2026-10-21). The backported plugin code (`pkg/`) is
identical to the self-built `oadp-1.6` cherry-pick from section 1.1, and its
`restore.go` change is byte-identical to the `main` commit that Run 1's image
was built from. What differs from Run 1 is the build line: OADP 1.6
dependencies (Velero 1.18.4 fork, client-go 0.35) instead of `main`'s. That
makes this the image closest to what an OADP 1.6.z release ships.

**Method.** Restore the **same backup** as Run 1 (`hcp-cluster1-backup-csi`,
which contains `hcp-cluster1/hcp-cluster1-import`) onto a freshly rebuilt
hub2. No new backup is needed, and hub1 stays shut down. The fix only acts at
restore time, and Run 1's hub2 already holds MCE's regenerated import secret,
so it can't be reused.

1. Remove Run 1's hub2: `ansible-playbook -i inventory/hosts cleanup-hub2.yaml`,
   then `virsh list --all | grep hub2` (no rows).
2. [steps.md](../steps.md) steps 15-19, as in Run 1.
3. Check and pin the image (section 1.0), then step 20 with
   `-e oadp_hypershift_plugin_image=quay.io/jparrill/hypershift-oadp-plugin@sha256:<digest>`,
   and confirm the pulled `imageID` (section 3.1).
4. Step 21 (restore), then step 22 (DNS: the restored kube-apiserver gets the
   same hub2 address as in Run 1, so this should already be correct) and the
   checks in section 4.

| Item | Value |
|---|---|
| Date | 2026-10-22 |
| Plugin image (tag and pulled digest) | `quay.io/jparrill/hypershift-oadp-plugin:oadp-1.6-check-skip-import`, pinned as `@sha256:f32db28e12563586894fc0e3a744212666904c4291506ccda405947a4deb42de`, created 2026-10-21 12:41 +02:00 (after the backport merged) |
| Image source commit | (expected `f573a597` or later on `oadp-1.6`) |
| Image checks (1.4) | `amd64`; entrypoint `cp /plugins/* /target/.`; binary contains "Skipping restore of ACM/MCE import secret" (1 match) |
| Plugin image on hub2 (3.1) | DPA `unsupportedOverrides.hypershiftPluginImageFqin` set to the digest; `deploy/velero` rolled out; init container image and pod `velero-c4f4f67c4-nxxjg` pulled `imageID` both equal the digest |
| Backup restored | `hcp-cluster1-backup-csi` (same as Run 1), synced from S3 onto the rebuilt hub2 |
| Velero log shows skip | PASS: `01:36:32Z ... "Skipping restore of ACM/MCE import secret hcp-cluster1/hcp-cluster1-import (contains stale bootstrap token)" ... logSource=".../pkg/core/restore.go:226" restore=openshift-adp/hcp-cluster1-restore-csi` |
| Import secret regenerated on hub2 | PASS: restore started `2026-10-22T01:36:22Z`; `hcp-cluster1-import` created `2026-10-22T01:51:02Z`; only label `managedcluster-import-controller.open-cluster-management.io/import-secret`, no `velero.io/*` labels |
| HostedCluster recovery | PASS: `HostedClusterRestoredFromBackup=True`, `RecoveryFinished`, at `2026-10-22T01:44:34Z`; `restored-from-backup` annotation removed by HyperShift; all other conditions healthy (`Available=True`, `Degraded=False`, `Progressing=False`) |
| ManagedCluster state | PASS: `hcp-cluster1` `HUB ACCEPTED=true`, `JOINED=True`, `AVAILABLE=True` |
| NodePool / nodes / workload | Nodes `c1worker1`, `c1worker2` `Ready` (v1.35.6, age 34h: not reprovisioned); `online-boutique` 11/11 pods `Running`. NodePool `nodepool-hcp-cluster1-1`: desired 2, current 2, 4.22.10, not updating, no message |
| Restore outcome and warnings | `Completed`, 0 errors, 2 warnings; CSI data movement for `data-etcd-0/1/2`: 3 of 3 completed. Warnings: `secrets "token-nodepool-hcp-cluster1-1-6e74db43" not found` and `secrets "user-data-nodepool-hcp-cluster1-1-6e74db43" not found` in `hcp-cluster1-hcp-cluster1`. Both are NodePool secrets (token and ignition user-data, same hash suffix) that HyperShift's NodePool controller generates and replaces itself; Run 1 had the first of the two. The NodePool reconciled to 2/2, so neither is a problem |
| Versions (hub2) | OCP 4.22.10; ACM 2.17.3 / MCE 2.17.3; `oadp-operator.v1.6.1` |
| Timeline (UTC) | 01:36:22 restore started; 01:36:32 plugin skipped the import secret; 01:44:34 HostedCluster recovery finished; 01:51:02 MCE created the new import secret |
| **Outcome** | **PASS.** Same result as Run 1 with the `oadp-1.6` build: the plugin skipped the stale import secret, MCE generated a new one, and the cluster imported without the step 12 workaround |

### 6.1 Evidence

**Plugin image checks** (workstation)

```console
$ IMG=quay.io/jparrill/hypershift-oadp-plugin:oadp-1.6-check-skip-import
$ skopeo inspect --override-arch amd64 docker://$IMG | jq '{Digest, Architecture, Created}'
{
  "Digest": "sha256:f32db28e12563586894fc0e3a744212666904c4291506ccda405947a4deb42de",
  "Architecture": "amd64",
  "Created": "2026-10-21T12:41:42.187813252+02:00"
}
$ skopeo inspect --override-arch amd64 --config docker://$IMG | jq '.config.Entrypoint'
[
  "/bin/bash",
  "-c",
  "cp /plugins/* /target/."
]
$ podman pull --platform linux/amd64 $IMG
$ cid=$(podman create $IMG); podman cp $cid:/plugins/hypershift-oadp-plugin /tmp/plugin; podman rm $cid
$ grep -ac "Skipping restore of ACM/MCE import secret" /tmp/plugin
1
```

**Hub2 runs the `oadp-1.6` plugin** (hub2, after step 20)

```console
$ oc get dpa dpa-instance -n openshift-adp -o jsonpath='{.spec.unsupportedOverrides}{"\n"}'
{"hypershiftPluginImageFqin":"quay.io/jparrill/hypershift-oadp-plugin@sha256:f32db28e12563586894fc0e3a744212666904c4291506ccda405947a4deb42de"}

$ oc rollout status deploy/velero -n openshift-adp
deployment "velero" successfully rolled out

$ oc get deploy velero -n openshift-adp \
    -o jsonpath='{.spec.template.spec.initContainers[?(@.name=="hypershift-oadp-plugin")].image}{"\n"}'
quay.io/jparrill/hypershift-oadp-plugin@sha256:f32db28e12563586894fc0e3a744212666904c4291506ccda405947a4deb42de

$ oc get pod -n openshift-adp -l deploy=velero \
    -o jsonpath='{range .items[*]}{.metadata.name}{"  "}{.status.initContainerStatuses[?(@.name=="hypershift-oadp-plugin")].imageID}{"\n"}{end}'
velero-c4f4f67c4-nxxjg  quay.io/jparrill/hypershift-oadp-plugin@sha256:f32db28e12563586894fc0e3a744212666904c4291506ccda405947a4deb42de

$ oc get backup.velero.io hcp-cluster1-backup-csi -n openshift-adp
NAME                      AGE
hcp-cluster1-backup-csi   4s
```

**The plugin skipped the secret** (hub2, after step 21)

```console
$ oc logs deploy/velero -n openshift-adp | grep "Skipping restore of ACM/MCE import secret"
Defaulted container "velero" out of: velero, openshift-velero-plugin (init), velero-plugin-for-aws (init), hypershift-oadp-plugin (init)
time="2026-10-22T01:36:32Z" level=info msg="Skipping restore of ACM/MCE import secret hcp-cluster1/hcp-cluster1-import (contains stale bootstrap token)" cmd=/plugins/hypershift-oadp-plugin logSource="/go/src/github.com/openshift/hypershift-oadp-plugin/pkg/core/restore.go:226" pluginName=hypershift-oadp-plugin process=restore restore=openshift-adp/hcp-cluster1-restore-csi type=hcp-restore
```

**MCE generated a new import secret** (hub2)

```console
$ oc get restore.velero.io hcp-cluster1-restore-csi -n openshift-adp -o jsonpath='{.status.startTimestamp}{"\n"}'
2026-10-22T01:36:22Z

$ oc get secret hcp-cluster1-import -n hcp-cluster1 \
    -o jsonpath='{.metadata.creationTimestamp}{"\n"}{.metadata.labels}{"\n"}'
2026-10-22T01:51:02Z
{"managedcluster-import-controller.open-cluster-management.io/import-secret":""}
```

**HostedCluster recovery finished** (hub2)

```console
$ oc get hostedcluster hcp-cluster1 -n hcp-cluster1 \
    -o jsonpath='{.status.conditions[?(@.type=="HostedClusterRestoredFromBackup")]}{"\n"}' | jq
{
  "lastTransitionTime": "2026-10-22T01:44:34Z",
  "message": "Hosted cluster recovery finished",
  "observedGeneration": 1,
  "reason": "RecoveryFinished",
  "status": "True",
  "type": "HostedClusterRestoredFromBackup"
}

$ oc get hostedcluster hcp-cluster1 -n hcp-cluster1 -o json | jq '.metadata.annotations | keys'
[
  "create-external-hub-kubeconfig",
  "hypershift.openshift.io/HasBeenAvailable",
  "kubectl.kubernetes.io/last-applied-configuration",
  "metallb.io/address-pool"
]

$ oc get hostedcluster hcp-cluster1 -n hcp-cluster1 \
    -o jsonpath='{range .status.conditions[*]}{.type}{"="}{.status}{"\n"}{end}'
ClusterVersionSucceeding=True
ClusterVersionProgressing=False
ClusterVersionReleaseAccepted=True
ClusterVersionUpgradeable=True
ClusterVersionAvailable=True
Degraded=False
EtcdAvailable=True
KubeAPIServerAvailable=True
InfrastructureReady=True
ExternalDNSReachable=True
ValidHostedControlPlaneConfiguration=True
ValidReleaseInfo=True
ValidIDPConfiguration=True
HostedClusterRestoredFromBackup=True
DataPlaneConnectionAvailable=True
ControlPlaneConnectionAvailable=True
EtcdBackupSucceeded=Unknown
AutoNodeEnabled=False
Available=True
ValidConfiguration=True
SupportedHostedCluster=True
ValidProxyConfiguration=True
IgnitionEndpointAvailable=True
ReconciliationActive=True
ValidReleaseImage=True
Progressing=False
PlatformCredentialsFound=True
ReconciliationSucceeded=True
ClusterVersionRetrievedUpdates=True
```

**The hosted cluster after the restore** (hosted cluster kubeconfig)

```console
$ oc get nodes
NAME        STATUS   ROLES    AGE   VERSION
c1worker1   Ready    worker   34h   v1.35.6
c1worker2   Ready    worker   34h   v1.35.6

$ oc get po -n online-boutique
NAME                                     READY   STATUS    RESTARTS      AGE
adservice-6d857b79-j86b2                 1/1     Running   0             24h
cartservice-59c9b67cfb-dn7rn             1/1     Running   0             24h
checkoutservice-cb96487df-qw5rx          1/1     Running   0             24h
currencyservice-686d78b4bc-n2klz         1/1     Running   0             24h
emailservice-688545b8df-nm6sn            1/1     Running   2 (24h ago)   24h
frontend-b7c754496-82zzx                 1/1     Running   0             24h
paymentservice-869d5b5cb9-thjfm          1/1     Running   0             24h
productcatalogservice-859bfdcc-6567f     1/1     Running   0             24h
recommendationservice-86ddcd7749-j7mp4   1/1     Running   2 (24h ago)   24h
redis-cart-79d76df4b-phz9x               1/1     Running   0             24h
shippingservice-88557f497-778tq          1/1     Running   0             24h
```

**The cluster imported** (hub2)

```console
$ oc get managedcluster hcp-cluster1
NAME           HUB ACCEPTED   MANAGED CLUSTER URLS                      JOINED   AVAILABLE   AGE
hcp-cluster1   true           https://api.hcp-cluster1.mylab.com:6443   True     True        8m4s
```

**NodePool and restore outcome** (hub2)

```console
$ oc get nodepool -n hcp-cluster1
NAME                      CLUSTER        DESIRED NODES   CURRENT NODES   AUTOSCALING   AUTOREPAIR   VERSION   UPDATINGVERSION   UPDATINGCONFIG   MESSAGE
nodepool-hcp-cluster1-1   hcp-cluster1   2               2               False         False        4.22.10   False             False

$ oc get restore.velero.io hcp-cluster1-restore-csi -n openshift-adp \
    -o jsonpath='{.status.phase}  warnings={.status.warnings}  errors={.status.errors}{"\n"}'
Completed  warnings=2  errors=

$ oc exec deploy/velero -c velero -n openshift-adp -- \
    ./velero restore describe hcp-cluster1-restore-csi | sed -n '/Warnings:/,/Errors:/p'
Warnings:
  Velero:     <none>
  Cluster:    <none>
  Namespaces:
    hcp-cluster1-hcp-cluster1:  secrets "token-nodepool-hcp-cluster1-1-6e74db43" not found
                                secrets "user-data-nodepool-hcp-cluster1-1-6e74db43" not found
...
CSI Snapshot Restores:
  hcp-cluster1-hcp-cluster1/data-etcd-2:
    Data Movement: specify --details for more information
  hcp-cluster1-hcp-cluster1/data-etcd-1:
    Data Movement: specify --details for more information
  hcp-cluster1-hcp-cluster1/data-etcd-0:
    Data Movement: specify --details for more information

Existing Resource Policy:   update
...
Restore Item Operations:  3 of 3 completed successfully, 0 failed (specify --details for more information)
```

**Versions** (hub2)

```console
$ oc get clusterversion -o jsonpath='{.items[0].status.desired.version}{"\n"}'
4.22.10
$ oc get csv -n openshift-adp -o custom-columns=NAME:.metadata.name,VERSION:.spec.version
NAME                                    VERSION
metallb-operator.v4.22.0-202610140833   4.22.0-202610140833
oadp-operator.v1.6.1                    1.6.1
$ oc get csv -n open-cluster-management -o custom-columns=NAME:.metadata.name,VERSION:.spec.version
NAME                                    VERSION
advanced-cluster-management.v2.17.3     2.17.3
metallb-operator.v4.22.0-202610140833   4.22.0-202610140833
$ oc get csv -n multicluster-engine -o custom-columns=NAME:.metadata.name,VERSION:.spec.version
NAME                                    VERSION
metallb-operator.v4.22.0-202610140833   4.22.0-202610140833
multicluster-engine.v2.17.3             2.17.3
```

## 7. Run 3: OADP 1.5 on OCP 4.21

The same test with **OADP 1.5** for both the backup and the restore, using
the plugin's `oadp-1.5` backport
([openshift/hypershift-oadp-plugin#326](https://github.com/openshift/hypershift-oadp-plugin/pull/326),
merged `2b0a1c4d`, 2026-10-22). Its `restore.go` change is line-for-line
the same as the one in Runs 1 and 2.

OADP 1.5 supports OCP 4.19-4.21, and 4.22's catalog only offers 1.6, so
this run rebuilds the whole lab on **4.21**:
- `vars.yaml`: `ocp_major_version: "4.21"`, `ocp_minor_version: 35` (4.21.35).
- `oadp_operator_channel` / `oadp_operator_starting_csv` pin the OADP 1.5
  CSV, and `acm_operator_channel` is set to whatever the 4.21 catalog
  offers.
- Workers in agent mode, as in Runs 1 and 2.
- Ceph stays on **RHCS 9**. Its kernel RBD issue was seen on OCP 4.20 and is
  fixed on 4.22; 4.21 is untested. **Gate:** right after step 7, run the
  README's RBD smoke test (`ceph-rbd-smoke` PVC and pod). If the pod stays
  `ContainerCreating` with `failed to add secret to kernel`, stop: 4.21 has
  the issue too. Then rebuild Ceph on RHCS 8
  (`ceph_container_image: .../rhceph/rhceph-8-rhel9:latest`), or use an
  rbd-nbd StorageClass (README, Ceph requirements). Record the result either
  way: it answers the 4.21 question.
- Step 12 is skipped, so the import secret is in the backup.
- Hub1 runs **stock OADP 1.5** for the backup. Hub2 runs
  `quay.io/jparrill/hypershift-oadp-plugin:oadp-1.5-check-skip-import`,
  pinned by digest, for the restore.

| Item | Value |
|---|---|
| Date | |
| OCP (hub1 / hub2) | hub1: 4.21.35 / hub2: (pending) |
| ACM / MCE | hub1: ACM `advanced-cluster-management.v2.17.3` (channel `release-2.17`, offered on 4.21); MCE `multicluster-engine.v2.17.3` |
| OADP CSV (hub1 / hub2) | hub1: `oadp-operator.v1.5.8` (channel `stable`, unpinned: the 4.21 catalog has no 1.6) / hub2: (pending) |
| Ceph RHCS 9 RBD on OCP 4.21 | **Works.** krbd maps RHCS 9 images on 4.21: `noobaa-db-pg-cluster-1` and `-2` `Running` for 77 min on `ocs-external-storagecluster-ceph-rbd`, data on `/dev/rbd0` (`/var/lib/postgresql/data`); the assisted-service PVCs mounted too. (On 4.20 the same mount failed with `failed to add secret to kernel`, error 524.) |
| Plugin image on hub1 (backup) | stock OADP 1.5 |
| Plugin image on hub2 (tag and pulled digest) | `quay.io/jparrill/hypershift-oadp-plugin:oadp-1.5-check-skip-import` |
| Image checks (1.4) | |
| Backup with OADP 1.5 (hub1, stock plugin) | `hcp-cluster1-backup-csi`: 3 `DataUpload`s `Completed` (242,427,252 / 242,450,804 / 242,486,460 bytes, BYTES DONE = TOTAL); `phase: Completed`, operations 3/3, items 369/369, 08:48:29Z-08:50:31Z (2 min) |
| Import secret in the backup | Yes: `velero backup describe --details` lists `hcp-cluster1/hcp-cluster1-import`; the secret carried only `managedcluster-import-controller.open-cluster-management.io/import-secret` (no exclude label) |
| Velero log shows skip | |
| Import secret regenerated on hub2 | |
| HostedCluster recovery | |
| ManagedCluster state | |
| NodePool / nodes / workload | |
| Restore outcome and warnings | |
| **Outcome** | |

### 7.1 Evidence

**Ceph RHCS 9 RBD mounts on OCP 4.21** (hub1, after step 8)

```console
$ oc get pods -n openshift-storage | grep noobaa-db-pg
noobaa-db-pg-cluster-1                                            1/1     Running   0              77m
noobaa-db-pg-cluster-2                                            1/1     Running   0              76m

$ oc exec -n openshift-storage pod/noobaa-db-pg-cluster-1 -- df -h | grep -E 'rbd|pgdata'
/dev/rbd0        49G  337M   49G   1% /var/lib/postgresql/data

$ oc get pvc -n multicluster-engine
NAME                                          STATUS   VOLUME                                     CAPACITY   ACCESS MODES   STORAGECLASS
assisted-service                              Bound    pvc-ce18fed5-b9af-4fdc-b3bf-e488cb69099d   100Gi      RWO            ocs-external-storagecluster-ceph-rbd
image-service-data-assisted-image-service-0   Bound    pvc-65ec2286-9fa9-4a6c-8b90-361ce15f2b6c   50Gi       RWO            ocs-external-storagecluster-ceph-rbd
postgres                                      Bound    pvc-5684fb0b-2721-4cfc-afaf-59c7e4381ec7   10Gi       RWO            ocs-external-storagecluster-ceph-rbd
```

**Operator versions** (hub1)

```console
$ oc get csv -n openshift-adp
NAME                                    DISPLAY            VERSION               REPLACES               PHASE
metallb-operator.v4.21.0-202610151411   MetalLB Operator   4.21.0-202610151411                          Succeeded
oadp-operator.v1.5.8                    OADP Operator      1.5.8                 oadp-operator.v1.5.7   Succeeded

$ oc get sub advanced-cluster-management -n open-cluster-management \
    -o jsonpath='{.status.installedCSV}{"  "}{.status.state}{"\n"}'
advanced-cluster-management.v2.17.3  AtLatestKnown
```

**Backup with OADP 1.5** (hub1)

```console
$ oc get backupstoragelocations.velero.io -n openshift-adp
NAME      PHASE       LAST VALIDATED   AGE   DEFAULT
default   Available   32s              48s   true

$ oc get volumesnapshotclass -L velero.io/csi-volumesnapshot-class
NAME                                                 DRIVER                                  DELETIONPOLICY   AGE    CSI-VOLUMESNAPSHOT-CLASS
ocs-external-storagecluster-cephfsplugin-snapclass   openshift-storage.cephfs.csi.ceph.com   Delete           146m
ocs-external-storagecluster-rbdplugin-snapclass      openshift-storage.rbd.csi.ceph.com      Delete           146m   true

$ oc get secret hcp-cluster1-import -n hcp-cluster1 --show-labels
NAME                  TYPE     DATA   AGE   LABELS
hcp-cluster1-import   Opaque   4      55m   managedcluster-import-controller.open-cluster-management.io/import-secret=

$ oc get datauploads.velero.io -n openshift-adp
NAME                            STATUS      STARTED   BYTES DONE   TOTAL BYTES   STORAGE LOCATION   AGE     NODE
hcp-cluster1-backup-csi-66f56   Completed   2m36s     242450804    242450804     default            3m1s    worker2
hcp-cluster1-backup-csi-8hxkj   Completed   2m49s     242486460    242486460     default            3m7s    worker1
hcp-cluster1-backup-csi-phs62   Completed   2m53s     242427252    242427252     default            3m12s   worker3

$ oc exec deploy/velero -c velero -n openshift-adp -- \
    ./velero backup describe hcp-cluster1-backup-csi --details | grep hcp-cluster1-import
    - hcp-cluster1/hcp-cluster1-import
```

```console
$ oc get backup.velero.io hcp-cluster1-backup-csi -n openshift-adp -o yaml
status:
  backupItemOperationsAttempted: 3
  backupItemOperationsCompleted: 3
  completionTimestamp: "2026-10-23T08:50:31Z"
  expiration: "2026-12-03T23:48:29Z"
  formatVersion: 1.1.0
  hookStatus: {}
  phase: Completed
  progress:
    itemsBackedUp: 369
    totalItems: 369
  startTimestamp: "2026-10-23T08:48:29Z"
  version: 1
```

**OCP and MCE versions** (hub1)

```console
$ oc get clusterversion -o jsonpath='{.items[0].status.desired.version}{"\n"}'
4.21.35
$ oc get csv -n multicluster-engine -o custom-columns=NAME:.metadata.name,VERSION:.spec.version
NAME                                    VERSION
metallb-operator.v4.21.0-202610151411   4.21.0-202610151411
multicluster-engine.v2.17.3             2.17.3
```
