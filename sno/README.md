# Single-Node OpenShift (sno, sno2)

Single-node OpenShift clusters built from an agent ISO on the bare-metal host's
KVM/libvirt, connected, with LVM Storage on a second disk when
`sno_lvm_storage` (which follows `use_lvm_storage`) is true.

They are the second cluster of several labs rather than a flow of their own:

| Cluster | Used by |
| --- | --- |
| `sno` (`sno.<base_domain>`) | [`udn-bgp-evpn/`](../udn-bgp-evpn/) (the second cluster on the fabric), [`spiffe-spire/`](../spiffe-spire/) (the second trust domain), [`kubevirt/`](../kubevirt/) (the VM host for hcp-kv2/hcp-kv3) |
| `sno2` (`sno2.<base_domain>`, off by default) | [`udn-bgp-evpn/rack2.md`](../udn-bgp-evpn/rack2.md), the routed-rack EVPN lab |

`sno3`, the MCE-only SNO of the KubeVirt lab, is built by
[`kubevirt/setup_sno3_mce.yaml`](../kubevirt/) with the same role.

## What lives here

| File | Purpose |
| --- | --- |
| `setup_sno.yaml` | Build the SNO (`roles/setup-sno`), then LVM Storage on it |
| `setup_sno2.yaml` | Build sno2 - the same role with sno2's name, install folder and VM folder - when `sno2_enabled: true` |
| `ansible.cfg` | Mirrors the root `ansible.cfg` and adds `../roles` to `roles_path` |

The role stays in the shared [`roles/`](../roles/) directory, and the
playbooks share `vars.yaml`, `vault.yaml` and `inventory/hosts` with the rest of
the repository. **Run them from inside this directory** - the `ansible.cfg`
here is what puts `../roles` on the role search path:

```bash
cd sno
ansible-playbook -i ../inventory/hosts setup_sno.yaml --ask-vault-pass
```

The labs' own build scripts call them by relative path from their directories
(`../sno/setup_sno.yaml`), which works because those directories carry the same
`roles_path`.

## Before you start

DNS is **not** done here. The zones `sno.<base_domain>` / `sno2.<base_domain>`
are rendered by `setup-dns` on the helper, which `setup_bm_host.yaml` owns, so
run that first from the repository root (add `-e sno2_enabled=true` for sno2):

```bash
(cd .. && ansible-playbook -i inventory/hosts setup_bm_host.yaml --tags dns --ask-vault-pass)
```

Pre-flight fails with that command if the zone is not being served yet.
`--ask-vault-pass` is not optional: these are connected installs that pull their
release payload with the pull secret from `vault.yaml`.

## Build the SNO

```bash
ansible-playbook -i ../inventory/hosts setup_sno.yaml --ask-vault-pass
```

Size and folders are `sno_*` in `vars.yaml` (`sno_memory`, `sno_vcpus`,
`sno_disk_gb`, `sno_install_folder`, `sno_storage_disk_gb` for the LVM Storage
disk). The kubeconfig lands in `sno_install_folder/auth/kubeconfig`.

Useful subsets:

| Tags | Does |
| --- | --- |
| `snoimage` | rebuild the agent ISO without touching the VM |
| `snovm` | rebuild the VM from the ISO already built |
| `snowait` | just block until an install already running finishes |
| `snostorage` | add LVM Storage (`lvms-vg1`) to a running SNO, attaching its disk if needed |
| `snodns` | put the SNO's node name in libvirt's DNS (the SPIFFE lab needs it) |

Rebuilding over an existing cluster needs `-e sno_force_reinstall=true`; the
guard is there because the rebuild deletes the only copy of its kubeconfig.

## Build sno2

```bash
ansible-playbook -i ../inventory/hosts setup_sno2.yaml --ask-vault-pass -e sno2_enabled=true
```

The same role, given sno2's name and its own install and VM folders
(`sno2_*` in `vars.yaml`). The folders matter most: an SNO's install folder
holds its only kubeconfig, and `setup-sno`'s pre-flight refuses a folder whose
kubeconfig belongs to another cluster, so `setup_sno.yaml -e sno_name=sno2`
fails instead of overwriting the SNO.

The one real difference is DNS: sno2 uses the helper as its only resolver
(adding a libvirt forwarder for its zone would restart `virbr0` under every VM),
so the play checks first that the helper resolves both `quay.io` and
`api.sno2.<base_domain>`. Its fabric NIC is added later by the UDN lab
(`--tags rack`) - see [udn-bgp-evpn/rack2.md](../udn-bgp-evpn/rack2.md). The
tags are `setup_sno.yaml`'s.

## Cleanup

From the repository root:

```bash
ansible-playbook -i inventory/hosts cleanup.yaml --tags sno     # the SNO
ansible-playbook -i inventory/hosts cleanup.yaml --tags sno2    # sno2
```
