# Mirror Registry (disconnected lab)

A standalone `registry` VM running the Quay `mirror-registry` appliance, filled
with `oc-mirror` v2: the only place a disconnected cluster pulls release and
operator images from. It is shared infrastructure - built once, before any
disconnected cluster - and everything with `disconnected_install: true` reads
it: [`standard-cluster/`](../standard-cluster/), [`acm/`](../acm/),
[`hcp/`](../hcp/), the disconnected [compact cluster](../compact-cluster/),
[`bastion/`](../bastion/).

## What lives here

| File | Purpose |
| --- | --- |
| `setup_mirror_registry.yaml` | Create the `registry` VM, register it, install `mirror-registry`, run `oc-mirror`, and trust its CA / log podman in on this host |
| `ansible.cfg` | Mirrors the root `ansible.cfg` and adds `../roles` to `roles_path` |

The roles stay in the shared [`roles/`](../roles/) directory
(`setup-mirror-registry-vm`, `setup-rhsm`, `setup-mirror-registry`,
`setup-mirror-registry-trust`), and the playbook shares `vars.yaml`,
`vault.yaml` and `inventory/hosts` with the rest of the repository. **Run it
from inside this directory** - the `ansible.cfg` here is what puts `../roles`
on the role search path:

```bash
cd mirror-registry
ansible-playbook -i ../inventory/hosts setup_mirror_registry.yaml --ask-vault-pass
```

`compact-cluster/setup_compact_cluster_disconnected.yaml` imports this playbook
as its first step.

## Build it


**Skip this entirely when `disconnected_install: false`.**

With `disconnected_install: true` this is a prerequisite for everything that
follows. Run it **after** `setup_bm_host.yaml` (repository root) - it needs the helper's DNS to
resolve `registry.<hub_domain>` - and **before** any cluster is built, since a
disconnected install pulls every image from it:

```bash
ansible-playbook -i ../inventory/hosts setup_mirror_registry.yaml --ask-vault-pass
```

The playbook refuses to run unless `disconnected_install: true`, so it is the
one step that cannot be reached by accident from a connected lab.

It builds a standalone `registry` VM (`.22`), which stays connected to the
internet - it is the thing that holds the images - installs the Quay
`mirror-registry` appliance on it, and fills it with `oc-mirror` v2:

| Tag | Step |
| --- | --- |
| `mirror-vm` | creates the `registry` VM - a 500G sparse disk expanded from the RHEL9 base image with `virt-resize`, then defined and booted |
| `rhsm` | registers it with subscription-manager (hence `org_id` / `activation_key`) |
| `mirror` | installs `mirror-registry` (Quay + its root CA), logs podman in, renders `/root/mirror/imageset-config.yaml` and runs `oc mirror ... --v2` into the registry |
| `mirror-trust` | copies the registry's root CA into **this** host's trust store and logs the bare-metal host's podman in, writing `mirror_registry_authfile` (`/root/.docker/config.json`) |

Before running it:

- `mirror_registry_password`, `org_id` and `activation_key` must be in
  `vault.yaml` - the playbook asserts all three.
- Review the mirror block in `vars.yaml`: `mirror_registry_disk_size` (500G,
  sparse), `mirror_registry_memory` / `mirror_registry_vcpus`,
  `mirror_ocp_version` (defaults to
  `<ocp_major_version>.<ocp_minor_version>`, i.e. what the rest of the lab
  installs) and `mirror_catalog_operator_packages`. That last list is what gets
  mirrored beyond the release payload; it already covers the operators
  the clusters and `acm/` install (ACM, MCE, LVM Storage, MetalLB, OADP, cincinnati),
  so add anything else your lab needs **before** the mirror runs rather than
  after.
- Mirroring the release payload plus those catalogs pulls thousands of images -
  budget the disk and the time.

The `mirror-trust` step is what the rest of the flow actually reads: the cluster
install asserts the CA is at
`/etc/pki/ca-trust/source/anchors/mirror-registry-rootCA.pem` before it starts,
and `setup-bminfra` / `create-hosted-cluster` build their mirror-only pull
secrets from the authfile.

**If `oc-mirror` fails, do not rebuild the VM.** The registry, the tools, the
pull secret and the ImageSetConfiguration are all in place by then, and
oc-mirror v2 resumes from its workspace, so each attempt only pulls what is
still missing. Finish it by hand on the VM - the playbook prints this same
command when it fails:

```bash
ssh root@192.168.122.22
cd /root/mirror
oc mirror -c imageset-config.yaml --workspace file://oc-mirror-output/ \
  docker://registry.hub.mylab.com:8443 --v2
# logs: /root/mirror/oc-mirror-output/working-dir/logs/ and /root/mirror/.oc-mirror.log
```

Re-running the playbook's `mirror` tag will **skip** the mirror once the
registry answers `podman login`, which is why finishing it by hand is the way
out. When it completes, run the trust step and carry on:

```bash
ansible-playbook -i ../inventory/hosts setup_mirror_registry.yaml --ask-vault-pass --tags mirror-trust
```

Verify before building a cluster:

```bash
# on the bare-metal host - the two things every later step reads
ls -l /etc/pki/ca-trust/source/anchors/mirror-registry-rootCA.pem
grep -o 'registry[^"]*:8443' /root/.docker/config.json

# on the mirror-registry VM - what oc-mirror published for the cluster to apply
ssh root@192.168.122.22 'find /root/mirror/oc-mirror-output -type d -name cluster-resources'
```

That last directory holds the `ImageDigestMirrorSet` / `ImageTagMirrorSet` /
`CatalogSource` the cluster install stages and applies as day-2 resources; the
cluster install fails with a clear message if it is not there yet.

## Cleanup

From the repository root. A plain `cleanup.yaml` run only **shuts the registry
down** - its VM, its 500G disk and everything `oc-mirror` put there are kept, so
a later disconnected run starts with `virsh start registry` instead of
mirroring again. To remove it for good (VM, disks, and this host's CA trust and
podman login for it):

```bash
ansible-playbook -i inventory/hosts cleanup.yaml --tags registry-destroy
```

See [Cleanup](../README.md#cleanup) in the root README.
