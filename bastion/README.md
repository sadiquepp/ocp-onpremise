# Bastion VM

A big-disk guest for building and scanning container images. Nothing else in
the lab depends on it, and nothing in the install flow imports it - run it when
you need it, skip it otherwise.

## What lives here

| File | Purpose |
| --- | --- |
| `setup_bastion.yaml` | Create the bastion VM, then install the build and scan tooling inside it |
| `ansible.cfg` | Mirrors the root `ansible.cfg` and adds `../roles` to `roles_path` |

The roles stay in the shared [`roles/`](../roles/) directory
(`setup-bastion-vm`, `setup-rhsm`, `setup-bastion`), and the playbook shares
`vars.yaml`, `vault.yaml` and `inventory/hosts` with the rest of the
repository. **Run it from inside this directory** - the `ansible.cfg` here is
what puts `../roles` on the role search path:

```bash
cd bastion
ansible-playbook -i ../inventory/hosts setup_bastion.yaml --ask-vault-pass
```

## Build it


A big-disk guest for building and scanning container images. Nothing else in the
lab depends on it, and nothing in the install flow imports it — run it when you
need it, skip it otherwise.

It exists because the bare-metal host does not have the room. Building container
images is disk-hungry: the hardened-vs-UBI image comparison in
[`sadiquepp/openshift`](https://github.com/sadiquepp/openshift/tree/main/test-workloads/online-boutique/hardened)
pulls a dozen base images, produces two full stacks of twelve service images,
and saves each one to a tar in order to scan it. On a host whose `/` is nearly
full that either fails partway or fills `/` — and filling `/` on the hypervisor
takes the whole lab with it.

```bash
ansible-playbook -i ../inventory/hosts setup_bastion.yaml --ask-vault-pass
```

256G disk, 8 vCPU, 16G RAM by default (`bastion_*` in `vars.yaml`). It reuses the
base image `setup_bm_host.yaml` prepared, so run that first. Registry trust and
login additionally need `mirror-registry/setup_mirror_registry.yaml`; without it, run:

```bash
ansible-playbook -i ../inventory/hosts setup_bastion.yaml --ask-vault-pass \
  -e bastion_trust_mirror_registry=false -e bastion_registry_login=false
```

**If `/var/lib/libvirt/images` is itself on a small `/`,** point the disk
somewhere with room — that is the whole point of the exercise:

```bash
ansible-playbook -i ../inventory/hosts setup_bastion.yaml --ask-vault-pass \
  -e bastion_disk_dir=/data/libvirt/images
```

The role refuses to create the disk if the filesystem holding `bastion_disk_dir`
has less free space than `bastion_disk_size`, and names the filesystem and the
shortfall when it does. Pass `-e bastion_require_free_space=false` to overcommit
the sparse qcow2 on purpose.

**Tags can be used on their own.** Address discovery and the ssh wait are tagged
`always`, so the in-memory `bastion` group is rebuilt on every run regardless of
what else is selected — `--tags repos,scanners` works without recreating the VM.
That matters because the group is built by `add_host` and exists only for the
duration of a run; nothing is written to `inventory/hosts`.

**This VM has no address reservation.** Unlike helper and mirror-registry there
is no `ip_list` entry and no `ip-dhcp-host` entry on the `default` network — it
takes whatever libvirt's DHCP gives it, which is fine because nothing in the lab
needs to reach it at a predictable address. The playbook prints the address and
adds the host to the in-memory inventory for its own later plays. To find it
again:

```bash
virsh domifaddr bastion
```

**The clone is pinned to a branch.** `main` in `sadiquepp/openshift` does not yet
carry `test-workloads/online-boutique/hardened` — the build and scan tooling this
bastion exists to run — so `bastion_git_repos` pins that entry to the branch that
does. Drop the `version:` line once it merges, otherwise the paths in
`/root/.bastion-env` point at a directory that is not there.

What it installs: podman, buildah, skopeo, git, jq and friends; `grype` with its
vulnerability database primed (~1GB, pre-pulled so the first scan does not pay
for it, and so a database that downloads but cannot unpack fails here rather
than mid-comparison); `oc` and `kubectl`; the mirror registry's CA in the trust
bundle plus a `podman login` to it; and a clone of `sadiquepp/openshift` under
`/root/work`. `trivy` is available but off by default (`bastion_install_trivy`)
— the two scanners use different databases, so the second is a cross-check
rather than a requirement.

Then, on the bastion:

```bash
source /root/.bastion-env     # sets REGISTRY and NAMESPACE
cd /root/work/openshift/test-workloads/online-boutique/hardened
./preflight.sh --build-check
```

Extra dnf repositories, if the packages you want are not in the subscription's,
go in `bastion_extra_dnf_repos` (passed through to `yum_repository`); extra RHSM
repository IDs go in `bastion_rhsm_repos`.

## Cleanup

Not covered by `cleanup.yaml`. Remove it by hand on the bare-metal host:

```bash
virsh destroy bastion; virsh undefine bastion --remove-all-storage
```
