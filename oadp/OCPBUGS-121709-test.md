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
