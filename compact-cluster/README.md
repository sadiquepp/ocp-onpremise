# Three-node compact cluster

A three-node compact OpenShift cluster (three schedulable masters, no
workers), installed from a single agent ISO, with every node's address on a
bond over two NICs.

| File | What it is |
| --- | --- |
| [compact-cluster.md](compact-cluster.md) | Lab, connected: three VMs on the hypervisor, active-backup (mode 1) bonds. Manual steps and the automated path. |
| [compact-cluster-hcp.md](compact-cluster-hcp.md) | Lab, connected: the compact cluster as a management cluster - ACM/MCE/MetalLB (`--tags acm`) and one two-worker hosted cluster, `hcp-compact1`, with MetalLB ingress inside it. Side by side with the hub's hosted clusters. |
| [compact-cluster-disconnected.md](compact-cluster-disconnected.md) | Lab, disconnected (`compactd`): installed only from the lab's mirror registry, with the nodes' internet egress blocked. |
| [compact-cluster-baremetal.md](compact-cluster-baremetal.md) | Physical servers, connected, with LACP (802.3ad) bonds. Manual steps only. |
| [compact-cluster-baremetal-disconnected.md](compact-cluster-baremetal-disconnected.md) | Physical servers, from your own mirror registry, with LACP bonds. Self-contained manual steps. |
| [setup_compact_cluster.yaml](setup_compact_cluster.yaml) | Automates the connected lab cluster. |
| [setup_compact_cluster_disconnected.yaml](setup_compact_cluster_disconnected.yaml) | Automates the disconnected lab cluster: mirror registry, cluster, day 2. |

Both lab clusters get LVM Storage (`lvms-vg1`, the default StorageClass) on
an empty 500G disk per node by default. The connected cluster can instead use
the lab's Ceph 9 cluster through ODF external mode
(`ocs-external-storagecluster-ceph-rbd`). That suits hosted control planes,
whose etcd can then be snapshotted. Choose before building: set
`use_lvm_storage: false` in `vars.yaml`, and follow
[compact-cluster.md, Choose the storage](compact-cluster.md#choose-the-storage-lvm-storage-or-ceph).
The disconnected cluster has no Ceph path. `use_lvm_storage: false` leaves it
with no storage at all, so build it with `-e compact_lvm_storage=true`.

## Running the playbooks

The playbooks share the repository's `vars.yaml`, `vault.yaml`,
`inventory/hosts` and `roles/`, and run from **inside this directory**:
Ansible reads `ansible.cfg` from the working directory, and the one here
adds `../roles` to `roles_path`.

```bash
# DNS for both clusters, from the repository root
ansible-playbook -i inventory/hosts setup_bm_host.yaml --tags dns --ask-vault-pass

cd compact-cluster

# connected
ansible-playbook -i ../inventory/hosts setup_compact_cluster.yaml --ask-vault-pass

# connected, with Ceph (use_lvm_storage: false): build Ceph before the cluster
# and attach it after, both from the repository root (compact-cluster.md)
#   ansible-playbook -i inventory/hosts setup_ceph.yaml --ask-vault-pass
#   ansible-playbook -i inventory/hosts setup_ceph_odf.yaml --ask-vault-pass \
#     -e ceph_odf_target_kubeconfig=/var/lib/libvirt/images/compact_install/auth/kubeconfig

# disconnected (builds the mirror registry first)
ansible-playbook -i ../inventory/hosts setup_compact_cluster_disconnected.yaml \
  -e disconnected_install=true --ask-vault-pass
```

To remove a cluster, run `cleanup.yaml --tags compact` or `--tags compactd`
from the repository root. The Ceph cluster is not part of either. Remove it
with `cleanup-ceph.yaml`, and only if no hub uses it.

The roles stay in the shared `../roles/`: `setup-compact-cluster` (reused by
both playbooks, and sharing the disconnected hub's egress-block tasks),
`setup-lvm-storage`, and for Ceph `setup-ceph-*`.
