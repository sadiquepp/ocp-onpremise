# Three-node compact cluster with bonded NICs (agent-based installer)

This guide builds a **three-node compact OpenShift cluster** on the lab
hypervisor from a single **agent ISO**. Every node has **two NICs bonded in
active-backup mode (mode 1)**, and the node's address lives on the bond.

For the same cluster installed only from the lab's mirror registry, with the
nodes cut off from the internet, see
[compact-cluster-disconnected.md](compact-cluster-disconnected.md).

For physical servers instead of lab VMs, see
[compact-cluster-baremetal.md](compact-cluster-baremetal.md). That guide
bonds the NICs with LACP (802.3ad) instead of active-backup.

The whole flow is automated by `setup_compact_cluster.yaml` and the
`roles/setup-compact-cluster` role. This document walks through the same steps
by hand, so you can see what each one does, run them one at a time, or debug a
step that failed. Each section names the role task file that automates it.

- [What you get](#what-you-get)
- [Choose the storage: LVM Storage or Ceph](#choose-the-storage-lvm-storage-or-ceph)
- [The automated path](#the-automated-path)
- [Manual deployment](#manual-deployment)
  1. [Prerequisites](#step-1---prerequisites)
  2. [Set the shell variables](#step-2---set-the-shell-variables)
  3. [Check the addresses are free](#step-3---check-the-addresses-are-free)
  4. [DNS](#step-4---dns)
  5. [Get the installer and client](#step-5---get-the-installer-and-client)
  6. [Write install-config.yaml](#step-6---write-install-configyaml)
  7. [Write agent-config.yaml with the bonds](#step-7---write-agent-configyaml-with-the-bonds)
  8. [Build the agent ISO](#step-8---build-the-agent-iso)
  9. [Create the three VMs, each with two NICs](#step-9---create-the-three-vms-each-with-two-nics)
  10. [Watch the install](#step-10---watch-the-install)
  11. [Verify the cluster and the bonds](#step-11---verify-the-cluster-and-the-bonds)
  12. [Add storage](#step-12---add-storage): [LVM Storage](#12a---lvm-storage) or [Ceph via ODF](#12b---ceph-via-odf-external-mode)
  13. [Test bond failover](#step-13---test-bond-failover)
  14. [Clean up](#step-14---clean-up)
- [Troubleshooting](#troubleshooting)

---

## What you get

```
                       libvirt 'default' network (virbr0, 192.168.122.0/24)
   ───────┬──────────────┬─────────────────┬──────────────┬───────────────┬─────────
          │              │                 │              │               │
     ┌────┴────┐   enp1s0│  enp2s0   enp1s0│  enp2s0 enp1s0│  enp2s0      .1 gateway + dnsmasq
     │ helper  │   ┌─────┴──┴──┐     ┌─────┴──┴──┐   ┌────┴──┴───┐        (resolver for the nodes)
     │  .21    │   │   bond0   │     │   bond0   │   │   bond0   │
     │ named   │   │  mode 1   │     │  mode 1   │   │  mode 1   │
     └─────────┘   │ master1   │     │ master2   │   │ master3   │
                   │   .64     │     │   .65     │   │   .66     │
                   └───────────┘     └───────────┘   └───────────┘
                     rendezvous
              API VIP .117 (api, api-int) and ingress VIP .118 (*.apps)
              float between the three masters (keepalived)
```

| What | Value | Where it is set |
|---|---|---|
| Cluster name / zone | `compact` / `compact.mylab.com` | `compact_name`, `base_domain` in `vars.yaml` |
| master1 (rendezvous host) | `192.168.122.64` | `ip_list.compact_master1` |
| master2 | `192.168.122.65` | `ip_list.compact_master2` |
| master3 | `192.168.122.66` | `ip_list.compact_master3` |
| API VIP: `api`, `api-int` | `192.168.122.117` | `ip_list.compact_api` |
| Ingress VIP: `*.apps` | `192.168.122.118` | `ip_list.compact_ingress` |
| Bond | `bond0`, `active-backup`, `miimon=100`, `primary=enp1s0` | `compact_bond_*` in role defaults |
| Bond port 1 (primary) | `enp1s0`, MAC `52:54:00:e2:54:<octet>` | `compact_primary_mac_prefix` |
| Bond port 2 (backup) | `enp2s0`, MAC `52:54:00:e2:56:<octet>` | `compact_secondary_mac_prefix` |
| Per node | 8 vCPU, 24 GiB RAM, 150 GB disk (`vda`, RHCOS) | `compact_vcpus`, `compact_memory`, `compact_disk_gb` |
| Storage disk per node | LVM Storage: 500 GB, empty (`vdb`). Ceph: none | `compact_storage_disk_gb`, `compact_lvm_storage` |
| Default StorageClass | LVM Storage: `lvms-vg1` (thin-provisioned XFS). Ceph: `ocs-external-storagecluster-ceph-rbd` | `use_lvm_storage` in `vars.yaml`, see [Choose the storage](#choose-the-storage-lvm-storage-or-ceph) |

**Why these addresses.** The nodes are two digits because this lab builds
MACs by appending the octet: `52:54:00:e2:54:100` is not a MAC. The VIPs
build no MAC, so they sit in `.100`-`.199` with the lab's other VIPs, at their
old `.17`/`.18` plus 100. All five are clear of every hosted-cluster MetalLB
address (`hosted_cluster_metallb_pools`) and of libvirt's DHCP pool
(`.201`-`.249`). See [ip-allocation.md](../ip-allocation.md).

The VIPs are fixed at install time (`apiVIPs`/`ingressVIPs` in
`install-config.yaml`), so a cluster built on `.17`/`.18` keeps them until it
is reinstalled. Re-rendering DNS before then points `api` and `*.apps` at the
new addresses, which the old cluster does not answer on.

**Why `e2:56` for the second NIC.** `52:54:00:e2:54:<octet>` is the lab-wide
scheme, and `52:54:00:e2:55:<octet>` is already taken by the containerlab
fabric NIC. The backup port of the bond takes the next prefix.

**How it differs from the SNO** (`setup_sno.yaml`). The structure is the same:
connected install, static addressing, one agent ISO, four stages. Two things
change:

- **`platform: baremetal` with two VIPs instead of `platform: none`.** Three
  nodes need `api` and `*.apps` to survive one node going down. The baremetal
  platform runs keepalived, haproxy and CoreDNS on the nodes and moves the VIPs
  between them, so there is no external load balancer to build.
- **Bonded networking.** Each VM gets two NICs on the same network. The agent
  configures `bond0` (mode 1) over them before the install starts, and the
  node's address, default route and DNS all live on the bond.

---

## Choose the storage: LVM Storage or Ceph

Decide this **before you build the cluster**. It changes how the VMs are
built, not only what is installed on them afterwards.

| | LVM Storage (default) | Ceph 9 via ODF external mode |
|---|---|---|
| Where the data lives | A 500 GB disk (`vdb`) on each node, local to that node | A separate Ceph cluster: `ceph1`-`ceph3` (`.24`-`.26`) and `cephadmin` (`.27`), built by `setup_ceph.yaml` |
| Default StorageClass | `lvms-vg1` | `ocs-external-storagecluster-ceph-rbd` |
| Other classes | none | `ocs-external-storagecluster-cephfs` (RWX) |
| CSI volume snapshots | No VolumeSnapshotClass | Yes, RBD and CephFS |
| A rebuild of the compact cluster | Deletes the data with the disks | Leaves the data on Ceph |
| Extra load on the hypervisor | 3 x 500 GB thin disks | About 80 GiB RAM, 52 vCPUs and 1.1 TB of thin disks for the four Ceph VMs ([README](../README.md#what-gets-built)) |

Pick **Ceph** when a pod must not be tied to one node's disk. The usual case
is a hosted control plane's etcd ([compact-cluster-hcp.md](compact-cluster-hcp.md)).
It then sits on Ceph RBD, can be snapshotted, and can be backed up with
`oadp_backup_method=csi`. LVM Storage ships no VolumeSnapshotClass, so OADP can
only copy those volumes file by file.

**Set the choice in `vars.yaml`:**

```yaml
# LVM Storage: the default, nothing to change
use_lvm_storage: true

# Ceph: no LVM Storage, ODF external mode instead
use_lvm_storage: false
```

With `use_lvm_storage: false`, the compact build:

- creates no `vdb` storage disk on the nodes (`tasks/vm.yml`), and
- skips the LVM Storage step (`--tags compactstorage`).

`compact_lvm_storage` follows `use_lvm_storage`. `use_lvm_storage` is the
lab-wide switch: a hub, hub2, SNO or disconnected compact cluster (`compactd`)
built afterwards also comes up without LVM Storage. `compactd` has no Ceph
path, so build it with `-e compact_lvm_storage=true`. To change only the compact cluster, leave `vars.yaml` alone and pass
`-e compact_lvm_storage=false` to `setup_compact_cluster.yaml` instead.

**Why not both.** LVM Storage and ODF both install into the `openshift-storage`
namespace, each with its own OperatorGroup. This repo has not tested them
together. And if `lvms-vg1` is already the default StorageClass when ODF
arrives, PVCs that name no class, etcd's included, still land on LVM Storage.
To move a cluster that already has LVM Storage, see
[Switching a cluster that already has LVM Storage](#switching-a-cluster-that-already-has-lvm-storage).

**What Ceph needs** in addition to everything else in this guide:

- **OCP 4.22 or later** on the compact cluster. Below 4.22, a Ceph 9 RBD
  volume binds but never mounts
  ([README, Requirements](../README.md#requirements)). `ocp_major_version` in
  `vars.yaml` is `"4.22"`.
- **`ceph_dashboard_password`** in `vault.yaml`, next to `org_id`,
  `activation_key` and `pull_secret`.
- The `ceph`, `ceph_admin` and `ceph_nodes` groups in `inventory/hosts`.
  `setup_bm_host.yaml` renders them; an inventory rendered before they existed
  needs that playbook run again.
- **A connected cluster.** The ODF Subscription comes from `redhat-operators`.
  [compact-cluster-disconnected.md](compact-cluster-disconnected.md) does not
  cover Ceph.

---

## The automated path

```bash
# 0. Ceph only (use_lvm_storage: false), from the repository root: the Ceph
#    cluster. It does not depend on the compact cluster.
ansible-playbook -i inventory/hosts setup_ceph.yaml --ask-vault-pass
ssh root@192.168.122.27 ceph -s          # HEALTH_OK, 3 mons, 9 OSDs

# 1. DNS, from the repository root: the zone on the helper, and api/*.apps in
#    the hypervisor's /etc/hosts
ansible-playbook -i inventory/hosts setup_bm_host.yaml --tags dns --ask-vault-pass

# 2. The cluster, from inside compact-cluster/: pre-flight, ISO, three VMs,
#    wait for the install, then LVM Storage (skipped when use_lvm_storage is
#    false)
cd compact-cluster
ansible-playbook -i ../inventory/hosts setup_compact_cluster.yaml --ask-vault-pass

# 3. Ceph only, back at the repository root: ODF on the compact cluster,
#    attached to the Ceph cluster from step 0
cd ..
test -x /usr/bin/oc || cp /var/lib/libvirt/images/compact_install/oc /usr/bin/oc
ansible-playbook -i inventory/hosts setup_ceph_odf.yaml --ask-vault-pass \
  -e ceph_odf_target_kubeconfig=/var/lib/libvirt/images/compact_install/auth/kubeconfig
```

For Ceph, pass **`ceph_odf_target_kubeconfig`**. `setup_ceph_odf.yaml` knows
only `target_hub=hub` and `target_hub=hub2`, and treats any other value,
`compact` included, as `hub`. Without the kubeconfig it installs ODF on
**hub1**. Step 3 also needs `oc` in `/usr/bin`: the ODF plays run plain `oc`,
and the compact build keeps its own `oc` in `compact_install/`, off the
`PATH`. A hub build puts one in `/usr/bin`.

Run the two compact playbooks from **inside `compact-cluster/`**. Ansible
reads `ansible.cfg` from the working directory, and the one in this folder
adds `../roles` to `roles_path`. Run from the repository root, Ansible cannot
find `setup-compact-cluster`. The shared playbooks (`setup_bm_host.yaml`,
`setup_mirror_registry.yaml`, `setup_ceph.yaml`, `setup_ceph_odf.yaml`,
`cleanup.yaml`) stay at the repository root and run from there.

| Tag | What it does | Role file |
|---|---|---|
| `compactpreflight` | Checks only. Always runs before the other stages. | `tasks/preflight.yml` |
| `compactimage` | Downloads the installer, renders the manifests and builds the ISO. | `tasks/image.yml` |
| `compactvm` | Builds the three VMs from the ISO that is already there. | `tasks/vm.yml` |
| `compactwait` | Waits for bootstrap and for install-complete, then checks the bonds. | `tasks/wait.yml` |
| `compactstorage` | Installs LVM Storage and waits for the `lvms-vg1` StorageClass. Attaches the storage disk to any node built without one. Skipped when `use_lvm_storage` (or `compact_lvm_storage`) is false. | `tasks/storage.yml`, `roles/setup-lvm-storage` |
| `acm` | Only when asked for (also tagged `never`). ACM/MCE, MetalLB and OADP, making this a management cluster for hosted clusters. See [compact-cluster-hcp.md](compact-cluster-hcp.md). | `roles/setup-hub-acm` |
| `hcpingress` | Only when asked for. MetalLB inside each hosted cluster this cluster manages, for its `*.apps`. | `roles/setup-hcp-ingress-metallb` |

To rebuild over an existing cluster, add `-e compact_force_reinstall=true`.
The rebuild wipes `auth/kubeconfig`. To remove the cluster, run
`ansible-playbook -i inventory/hosts cleanup.yaml --tags compact` from the
repository root.

The rendered `install-config.yaml` and `agent-config.yaml` are kept in
`/var/lib/libvirt/images/compact_install/rendered/`. The installer deletes the
originals when it builds the ISO.

---

## Manual deployment

Run every step **on the hypervisor as root**, unless the step says otherwise.

### Step 1 - Prerequisites

*Automated by: `tasks/preflight.yml`*

```bash
# libvirt tooling: this has to be the hypervisor
which virsh qemu-img virt-install

# nmstatectl: openshift-install runs it on THIS host to validate each node's
# bond configuration. Without it, `agent create image` fails with
# "failed to validate network yaml for host 0, install nmstate package".
which nmstatectl || dnf install -y nmstate

# The lab ssh key, injected into every node for `ssh core@<node>`
ls ~/.ssh/lab_rsa ~/.ssh/lab_rsa.pub
```

You also need the **pull secret** from
[console.redhat.com](https://console.redhat.com/openshift/install/pull-secret).
This is a connected install, so the nodes pull the release payload from
quay.io. In the lab it is in `vault.yaml` as `pull_secret`. For the manual
steps, save it to a file:

```bash
ansible-vault view vault.yaml   # copy the pull_secret value
vi /root/pull-secret.json       # paste it here, as one line of JSON
```

**Ceph only: build the Ceph cluster now.** *Automated by: `setup_ceph.yaml`*

Skip this for LVM Storage. With Ceph
([Choose the storage](#choose-the-storage-lvm-storage-or-ceph)), build it
first: it takes a while, and nothing in it depends on the compact cluster. The
compact nodes reach it directly, because the Ceph VMs are on the same libvirt
`default` network. Step 12 attaches the compact cluster to it.

From the repository root:

```bash
grep -q '^use_lvm_storage: false' vars.yaml && echo "Ceph path"
ansible-vault view vault.yaml | grep -q '^ceph_dashboard_password:' && echo "dashboard password set"
grep -A4 '^\[ceph' inventory/hosts       # the ceph, ceph_admin and ceph_nodes groups

ansible-playbook -i inventory/hosts setup_ceph.yaml --ask-vault-pass
```

The playbook builds four VMs from `rhel9_kvm_image` and registers them with
`subscription-manager`. It then runs `cephadm bootstrap` on `ceph1`, adds
`ceph2` and `ceph3` as mon, mgr and OSD hosts, and makes `cephadmin` the admin
node. The [README](../README.md#build-the-ceph-cluster) describes each play.

```bash
ssh root@192.168.122.27 ceph -s           # HEALTH_OK, 3 mons, 3 mgrs, 9 OSDs
ssh root@192.168.122.27 ceph osd tree
```

Wait for `HEALTH_OK` before Step 12. The export in Step 12 also runs against a
degraded cluster, and the problem shows up later as PVCs that do not bind.

If the Ceph cluster already exists, for example because a hub uses it, do not
build a second one. See [Sharing Ceph with a hub](#sharing-ceph-with-a-hub).

### Step 2 - Set the shell variables

The commands in the rest of this guide use these variables. Set them once in
the shell you will use for the whole build.

```bash
export LAB=192.168.122                       # lab_network_prefix
export DOMAIN=mylab.com                      # base_domain
export CLUSTER=compact                       # compact_name
export ZONE=$CLUSTER.$DOMAIN
export OCP=4.22.10                           # ocp_major_version.ocp_minor_version
export WORK=/var/lib/libvirt/images/compact_install
export VMDIR=/var/lib/libvirt/images/compact

export API_VIP=$LAB.117
export INGRESS_VIP=$LAB.118
export NODES="master1:64 master2:65 master3:66"   # hostname:octet, rendezvous host first
export RENDEZVOUS=$LAB.64

export MAC1=52:54:00:e2:54                   # bond port 1 (enp1s0), the lab-wide scheme
export MAC2=52:54:00:e2:56                   # bond port 2 (enp2s0)

export STORAGE=lvm                           # lvm or ceph: use_lvm_storage true / false
```

### Step 3 - Check the addresses are free

*Automated by: `tasks/preflight.yml` (the ip_list, MetalLB, MAC and existing-VM checks)*

The agent sets every address statically, so nothing tells you about a
conflict. A duplicate address shows up much later as a cluster that is
reachable only some of the time.

```bash
# Nothing should answer on any of the five addresses
for o in 17 18 64 65 66; do
  ping -c1 -W1 $LAB.$o >/dev/null && echo "$LAB.$o IS IN USE" || echo "$LAB.$o free"
done

# No other DHCP reservation should hold them. setup-bm-host already reserves
# them for these MACs, so finding these exact MACs here is expected.
virsh net-dumpxml default | grep -E "\.(17|18|64|65|66)'"

# No domain with these names should exist yet
virsh list --all | grep -E 'compact_master[123]'
```

### Step 4 - DNS

*Automated by: `setup_bm_host.yaml --tags dns` (roles `setup-dns` and `setup-bm-host`)*

The nodes do not need this zone for the install itself. The baremetal
platform runs CoreDNS on each node for `api-int` and `*.apps`, and the nodes
use libvirt's dnsmasq (`.1`) to reach quay.io. **The hypervisor** does need
it: `openshift-install agent wait-for` connects to `api.compact.mylab.com`,
and so does every `oc` command afterwards.

**4a. The zone on the helper** (`ssh root@$LAB.21`):

```bash
cat > /var/named/compact.mylab.com.db <<'EOF'
$TTL 1D
@	IN SOA	helper.hub.mylab.com. root.hub.mylab.com. (
					0	; serial
					1D	; refresh
					1H	; retry
					1W	; expire
					3H )	; minimum
		NS	helper.hub.mylab.com.
master1        A	192.168.122.64
master2        A	192.168.122.65
master3        A	192.168.122.66
api		A	192.168.122.117
api-int		A	192.168.122.117
*.apps		A	192.168.122.118
EOF

cat >> /etc/named.rfc1912.zones <<'EOF'
zone "compact.mylab.com" IN {
	type master;
	file "compact.mylab.com.db";
	allow-update { none; };
};
EOF

# Reverse records: add these lines to /var/named/named-reverse.hub.mylab.com.db
#   64 		IN PTR 		master1.compact.mylab.com.
#   65 		IN PTR 		master2.compact.mylab.com.
#   66 		IN PTR 		master3.compact.mylab.com.
#   17 		IN PTR 		api.compact.mylab.com.
#   18 		IN PTR 		ingress.apps.compact.mylab.com.

chown root:named /var/named/compact.mylab.com.db
named-checkzone compact.mylab.com /var/named/compact.mylab.com.db
named-checkconf && systemctl restart named

dig +short @localhost api.compact.mylab.com            # 192.168.122.117
dig +short @localhost test.apps.compact.mylab.com      # 192.168.122.118
```

**4b. The hypervisor's `/etc/hosts`** (back on the hypervisor). The hypervisor
does not resolve through the helper, so put the names in `/etc/hosts`.
`*.apps` cannot be a wildcard there, so list each route you need. Skip this if
`setup_bm_host.yaml --tags dns` has run: its marked block already has these
names, and lines added here sit outside it, so no later run updates or removes
them when an address moves.

```bash
cat >> /etc/hosts <<EOF
$API_VIP  api.$ZONE api-int.$ZONE
$INGRESS_VIP  console-openshift-console.apps.$ZONE
$INGRESS_VIP  oauth-openshift.apps.$ZONE
$INGRESS_VIP  downloads-openshift-console.apps.$ZONE
EOF

getent hosts api.$ZONE    # must print the API VIP, not anything else
```

**4c. (Optional) A libvirt forwarder.** This lets *other* VMs on the network
resolve `compact.mylab.com` through dnsmasq. The playbook adds a
`<forwarder domain='compact.mylab.com' addr='192.168.122.21'/>` line in
`default-network.xml.j2`, and it takes effect when you run
`setup_bm_host.yaml --tags virtnet`. **Be careful: that tag redefines the
default network, which disconnects every running VM from virbr0.** The install
does not need the forwarder, so you can add it later.

### Step 5 - Get the installer and client

*Automated by: `tasks/image.yml`*

Use the installer that matches the cluster version. The ISO records the
release image of the installer that built it.

Start from an **empty** `$WORK`. If it holds an earlier build, its
`.openshift_install_state.json` and `auth/` would be reused in the new ISO.
Emptying it also deletes the old cluster's `auth/kubeconfig`, so back that up
first if you still need it.

```bash
rm -rf $WORK
mkdir -p $WORK $VMDIR
cd $WORK
curl -LO https://mirror.openshift.com/pub/openshift-v4/clients/ocp/$OCP/openshift-install-linux.tar.gz
curl -LO https://mirror.openshift.com/pub/openshift-v4/clients/ocp/$OCP/openshift-client-linux.tar.gz
tar xzf openshift-install-linux.tar.gz
tar xzf openshift-client-linux.tar.gz
./openshift-install version
./oc version --client
```

`agent create image` runs `oc` to extract the base ISO from the release
payload. Step 8 puts this directory at the front of `PATH` so the installer
uses this `oc`.

### Step 6 - Write install-config.yaml

*Automated by: `templates/install-config.yaml.j2`*

```bash
cat > $WORK/install-config.yaml <<EOF
apiVersion: v1
baseDomain: $DOMAIN
compute:
- name: worker
  replicas: 0                 # no workers ...
controlPlane:
  name: master
  replicas: 3                 # ... and three masters = compact; masters become schedulable
metadata:
  name: $CLUSTER
networking:
  clusterNetwork:
  - cidr: 10.128.0.0/14
    hostPrefix: 23
  machineNetwork:
  - cidr: $LAB.0/24           # must contain every node address AND both VIPs
  networkType: OVNKubernetes
  serviceNetwork:
  - 172.30.0.0/16
platform:
  baremetal:
    apiVIPs:
    - $API_VIP                # api / api-int, held by keepalived on one master
    ingressVIPs:
    - $INGRESS_VIP            # *.apps, held by keepalived where a router runs
    provisioningNetwork: Disabled
pullSecret: '$(cat /root/pull-secret.json)'
sshKey: '$(cat ~/.ssh/lab_rsa.pub)'
EOF
```

Notes:

- The agent installer reads `compute: 0` with `controlPlane: 3` and builds a
  compact cluster. You do not need to edit `mastersSchedulable`.
- With the agent installer, `platform: baremetal` needs only the VIPs. There
  are no BMCs and no `hosts:` list here. `agent-config.yaml` describes the
  hosts.

### Step 7 - Write agent-config.yaml with the bonds

*Automated by: `templates/agent-config.yaml.j2`*

This is the step that adds the bonds. Each host entry has two parts:

- **`interfaces`** lists the host's two MACs. The agent compares them with the
  NICs it finds when it boots, picks the host entry that matches, and maps the
  NIC names in `networkConfig` to the real interfaces. Both bond ports are
  listed, so the agent can identify the host from either NIC.
- **`networkConfig`** is the [nmstate](https://nmstate.io) configuration that
  is applied before the install starts:
  - `bond0`, `type: bond`, `mode: active-backup` (mode 1), with both NICs as
    ports and the static IP **on the bond**.
  - `miimon: "100"`. Without link monitoring the bond never detects a dead
    port, so active-backup never fails over.
  - `primary: enp1s0`. The bond goes back to the first NIC when that NIC
    recovers.
  - `mac-address` on the bond is pinned to the **first NIC's MAC**. Otherwise
    the bond takes the MAC of whichever port is enslaved first, and that can
    change from one boot to the next. The DHCP reservation and every ARP cache
    know this address by the first NIC's MAC.
  - Both NICs are listed as `type: ethernet` with no IP of their own. This
    ties each name to its MAC.
  - The default route uses `next-hop-interface: bond0`, so it survives the
    loss of either port.

Generate the file for all three hosts:

```bash
host_block() {   # $1 = hostname, $2 = last octet
cat <<EOF
  - hostname: $1
    role: master
    interfaces:
      - name: enp1s0
        macAddress: $MAC1:$2
      - name: enp2s0
        macAddress: $MAC2:$2
    rootDeviceHints:
      deviceName: /dev/vda
    networkConfig:
      interfaces:
        - name: bond0
          type: bond
          state: up
          mac-address: $MAC1:$2
          ipv4:
            enabled: true
            dhcp: false
            address:
              - ip: $LAB.$2
                prefix-length: 24
          ipv6:
            enabled: false
          link-aggregation:
            mode: active-backup
            options:
              miimon: "100"
              primary: enp1s0
            port:
              - enp1s0
              - enp2s0
        - name: enp1s0
          type: ethernet
          state: up
          mac-address: $MAC1:$2
          ipv4:
            enabled: false
          ipv6:
            enabled: false
        - name: enp2s0
          type: ethernet
          state: up
          mac-address: $MAC2:$2
          ipv4:
            enabled: false
          ipv6:
            enabled: false
      dns-resolver:
        config:
          server:
            - $LAB.1
      routes:
        config:
          - destination: 0.0.0.0/0
            next-hop-address: $LAB.1
            next-hop-interface: bond0
            table-id: 254
EOF
}

{
cat <<EOF
apiVersion: v1alpha1
kind: AgentConfig
metadata:
  name: $CLUSTER
rendezvousIP: $RENDEZVOUS
hosts:
EOF
for n in $NODES; do host_block ${n%%:*} ${n##*:}; done
} > $WORK/agent-config.yaml
```

**Why the DNS server is `.1` and not the helper (`.21`).** named on the helper
forwards to a resolver that cannot be reached from this lab, so it returns
SERVFAIL for quay.io. The agent's pre-flight check then refuses to start the
install. libvirt's dnsmasq at `.1` resolves both external names and the lab
zones. See the comment on `sno_dns_server` in `roles/setup-sno/defaults/main.yml`.

**Keep a copy.** The next step deletes both files:

```bash
mkdir -p $WORK/rendered && cp $WORK/install-config.yaml $WORK/agent-config.yaml $WORK/rendered/
```

### Step 8 - Build the agent ISO

*Automated by: `tasks/image.yml`*

```bash
cd $WORK
PATH=$WORK:$PATH ./openshift-install --dir $WORK agent create image --log-level=info
ls -lh $WORK/agent.x86_64.iso
chmod 0644 $WORK/agent.x86_64.iso      # qemu reads it as its own user
```

The installer validates each host's `networkConfig` with `nmstatectl` at this
point. A mistake in the bond (a port name that is not in `interfaces`, a
misspelt `mode`, a wrong indent) fails here, before any VM exists. The same
ISO boots all three nodes. Each node picks its own configuration by MAC.

### Step 9 - Create the three VMs, each with two NICs

*Automated by: `tasks/vm.yml`*

For each node: create a fresh disk for RHCOS and, **with LVM Storage only**, a
second, **empty disk for LVM Storage**. Reserve the address for the first
NIC's MAC, and create a domain with **two `--network` options on the default
network**. These are the
two bond ports. libvirt assigns PCI slots in the order the NICs are listed, so
the first NIC becomes `enp1s0` and the second `enp2s0`.

```bash
for n in $NODES; do
  host=${n%%:*}; oct=${n##*:}; dom=compact_$host

  # Always a fresh disk. A disk left from an earlier install already has
  # RHCOS on it, so with boot order hd,cdrom the VM would boot that and never
  # start the agent.
  rm -f $VMDIR/${dom}_disk.qcow2 $VMDIR/${dom}_storage.qcow2
  qemu-img create -f qcow2 $VMDIR/${dom}_disk.qcow2 150G

  # The storage disk: empty, for LVM Storage (Step 12). LVMS only takes a disk
  # with nothing on it, so it is recreated on every rebuild too. Ceph needs
  # no disk on the nodes: STORAGE_DISK stays empty and virt-install gets one
  # disk fewer.
  STORAGE_DISK=
  if [ "$STORAGE" = lvm ]; then
    qemu-img create -f qcow2 -o preallocation=metadata $VMDIR/${dom}_storage.qcow2 500G
    STORAGE_DISK="--disk $VMDIR/${dom}_storage.qcow2,format=qcow2,cache=none,bus=virtio"
  fi

  # Reserve the address for the bond's MAC (the first NIC's MAC). Skip this if
  # setup-bm-host already did it.
  virsh net-dumpxml default | grep -qi "$MAC1:$oct" || \
    virsh net-update default add ip-dhcp-host \
      "<host mac='$MAC1:$oct' name='$host.$ZONE' ip='$LAB.$oct'/>" --live --config

  virt-install \
    --import \
    --name $dom \
    --memory 24576 \
    --vcpus 8 \
    --cpu host-passthrough \
    --os-variant=rhel9.4 \
    --disk $VMDIR/${dom}_disk.qcow2,format=qcow2,cache=none,bus=virtio \
    $STORAGE_DISK \
    --disk $WORK/agent.x86_64.iso,device=cdrom,bus=sata,readonly=on \
    --boot hd,cdrom \
    --network network:default,mac=$MAC1:$oct,model=virtio \
    --network network:default,mac=$MAC2:$oct,model=virtio \
    --memballoon model=virtio,freePageReporting=on \
    --noautoconsole
done

# Each domain should list two interfaces with the expected MACs, and the
# disks: vda (RHCOS), plus vdb (storage) with LVM Storage
for n in $NODES; do virsh domiflist compact_${n%%:*}; virsh domblklist compact_${n%%:*}; done
```

**The order of the two disks matters** (LVM Storage). The RHCOS disk is listed first, so it
is `vda`. The storage disk is second, so it is `vdb`. `agent-config.yaml` pins
the install to `/dev/vda` with `rootDeviceHints` (Step 7), which keeps the
installer off the storage disk. Without that pin the agent picks a disk by its
own rules, and could install RHCOS on the disk meant for LVM Storage.

The VMs use `--import` with `--boot hd,cdrom` instead of `--cdrom`. While the
disk is empty, the VM falls through to the ISO. Once RHCOS is on the disk, the
VM boots from the disk. With `--cdrom`, each node would reboot back into the
agent after it installs.

### Step 10 - Watch the install

*Automated by: `tasks/wait.yml`*

The install goes like this:

1. All three nodes boot the ISO. Each one configures `bond0` from its
   `networkConfig` and starts the agent.
2. master1 (the rendezvous host, `.64`) runs the assisted service.
   master2 and master3 register with it.
3. When all three hosts pass validation, master1 installs master2 and
   master3, which reboot from disk.
4. master1 then installs itself and reboots, and the three masters form the
   control plane. This is **bootstrap-complete**.
5. The operators settle. This is **install-complete**.

```bash
cd $WORK
./openshift-install --dir $WORK agent wait-for bootstrap-complete --log-level=info
./openshift-install --dir $WORK agent wait-for install-complete  --log-level=info
```

While you wait:

```bash
virsh console compact_master1                                     # Ctrl-] to leave
ssh -i ~/.ssh/lab_rsa core@$RENDEZVOUS 'ip -br addr show bond0'   # the bond came up with the IP
ssh -i ~/.ssh/lab_rsa core@$RENDEZVOUS 'sudo journalctl -u assisted-service -u agent -f'
# Agent UI on newer releases: http://$RENDEZVOUS:8080
```

A compact install takes longer than an SNO. The role allows 75 minutes for
bootstrap and 120 minutes for completion.

### Step 11 - Verify the cluster and the bonds

*Automated by: `tasks/wait.yml` (the `/proc/net/bonding` check)*

```bash
export KUBECONFIG=$WORK/auth/kubeconfig
alias oc=$WORK/oc

oc get nodes -o wide        # three nodes, ROLES control-plane,master,worker
oc get clusteroperators     # all Available=True, Degraded=False
oc get clusterversion
```

Check the bond on every node:

```bash
for n in $NODES; do
  ip=$LAB.${n##*:}
  echo "== ${n%%:*} ($ip)"
  ssh -i ~/.ssh/lab_rsa core@$ip "grep -E 'Bonding Mode|Currently Active|MII Status|Slave Interface|Primary Slave' /proc/net/bonding/bond0"
done
```

You should see this for each node:

```
Bonding Mode: fault-tolerance (active-backup)
Primary Slave: enp1s0 (primary_reselect always)
Currently Active Slave: enp1s0
MII Status: up
Slave Interface: enp1s0
MII Status: up
Slave Interface: enp2s0
MII Status: up
```

The same information from inside the cluster:

```bash
oc debug node/master1 -- chroot /host nmcli -f NAME,TYPE,DEVICE con show --active
# br-ex is the OVN-Kubernetes external bridge, and bond0 is its port
oc debug node/master1 -- chroot /host ovs-vsctl list-ports br-ex      # shows bond0
oc debug node/master1 -- chroot /host ip -br addr show br-ex          # the node IP
```

After the install, the node IP is on **`br-ex`**, not on `bond0`. During the
first boot, OVN-Kubernetes moves the address from the interface that holds the
default route (here `bond0`) onto the `br-ex` bridge and adds `bond0` as the
bridge's uplink port. `bond0` still does the failover underneath: if a port
fails, the bond switches to the other port and `br-ex` is not affected.
During the agent phase (Step 10), before this happens, the IP is still on
`bond0`.

Check which master holds each VIP:

```bash
for n in $NODES; do
  ssh -i ~/.ssh/lab_rsa core@$LAB.${n##*:} "ip -br addr | grep -E '$API_VIP|$INGRESS_VIP'" \
    && echo "  ^ on ${n%%:*}"
done
```

Log in to the console:

```bash
cat $WORK/auth/kubeadmin-password
# https://console-openshift-console.apps.compact.mylab.com
```

### Step 12 - Add storage

Follow the part for the storage you chose in
[Choose the storage](#choose-the-storage-lvm-storage-or-ceph):

- `STORAGE=lvm`: [12a - LVM Storage](#12a---lvm-storage)
- `STORAGE=ceph`: [12b - Ceph via ODF external mode](#12b---ceph-via-odf-external-mode)

#### 12a - LVM Storage

*Automated by: `tasks/storage.yml`, which runs `roles/setup-lvm-storage` (`--tags compactstorage`)*

LVM Storage gives the cluster a default StorageClass, `lvms-vg1`, the same
one the hub and the SNO get. Its `LVMCluster` names no `deviceSelector`, so on
every node it takes **every empty disk** it finds and builds a volume group
and a thin pool on it. On a compact cluster every node is a master and a
worker, so it runs on all three. The RHCOS disk is not empty, so each node
needs the second disk from Step 9.

**Check the storage disk on every node.** It should be there, with no
partitions and no filesystem:

```bash
for n in $NODES; do
  echo "== ${n%%:*}"
  ssh -i ~/.ssh/lab_rsa core@$LAB.${n##*:} 'lsblk -o NAME,SIZE,TYPE,FSTYPE,MOUNTPOINT /dev/vdb'
done
```

**A cluster built without the storage disk** (before Step 9 created
one) can get it now, with no reinstall and no reboot. Create it and hot-plug
it into each running VM. `--persistent` also adds it to the domain
definition, so it is still there after the next reboot:

```bash
for n in $NODES; do
  dom=compact_${n%%:*}
  virsh domblklist $dom | grep -q "${dom}_storage.qcow2" && continue
  qemu-img create -f qcow2 -o preallocation=metadata $VMDIR/${dom}_storage.qcow2 500G
  virsh attach-disk $dom $VMDIR/${dom}_storage.qcow2 vdb \
    --driver qemu --subdriver qcow2 --cache none --targetbus virtio --persistent --live
done
```

**Install the operator** from the `redhat-operators` catalog:

```bash
oc apply -f - <<EOF
apiVersion: v1
kind: Namespace
metadata:
  name: openshift-storage
  labels:
    openshift.io/cluster-monitoring: "true"
---
apiVersion: operators.coreos.com/v1
kind: OperatorGroup
metadata:
  name: openshift-storage-operatorgroup
  namespace: openshift-storage
spec:
  targetNamespaces:
  - openshift-storage
---
apiVersion: operators.coreos.com/v1alpha1
kind: Subscription
metadata:
  name: lvms
  namespace: openshift-storage
spec:
  installPlanApproval: Automatic
  name: lvms-operator
  source: redhat-operators
  sourceNamespace: openshift-marketplace
EOF

oc -n openshift-storage get csv -w     # wait for lvms-operator.v... PHASE Succeeded, then Ctrl-C
oc wait --for=condition=Established crd/lvmclusters.lvm.topolvm.io --timeout=300s
```

**Create the LVMCluster:**

```bash
oc apply -f - <<'EOF'
apiVersion: lvm.topolvm.io/v1alpha1
kind: LVMCluster
metadata:
  name: my-lvmcluster
  namespace: openshift-storage
spec:
  storage:
    deviceClasses:
      - name: vg1
        default: true
        fstype: xfs
        thinPoolConfig:
          name: thin-pool-1
          sizePercent: 90
          overprovisionRatio: 10
          chunkSizeCalculationPolicy: Static
EOF

# Ready once vg-manager has built the volume group on every node (a few minutes)
oc -n openshift-storage get lvmcluster my-lvmcluster -o jsonpath='{.status.state}{"\n"}'   # Ready
oc get storageclass                                                                          # lvms-vg1 (default)
```

Each node now has a volume group `vg1` on `/dev/vdb`:

```bash
oc -n openshift-storage get lvmvolumegroupnodestatus -o wide
oc debug node/master1 -- chroot /host vgs
```

`lvms-vg1` uses `volumeBindingMode: WaitForFirstConsumer`. A new PVC stays
`Pending` until a pod uses it. That is expected: LVM volumes are local to one
node, so the volume is created on whichever node the pod is scheduled to.
LVMS marks `lvms-vg1` as the default class only if no other class is already
the default.

#### 12b - Ceph via ODF external mode

*Automated by: `setup_ceph_odf.yaml` (from the repository root), which runs
`roles/setup-ceph-odf-consumer` and `roles/setup-ceph-odf-export`*

ODF in **external mode** runs no Ceph daemon on the compact cluster. The
operator installs ceph-csi, and ceph-csi provisions RBD and CephFS volumes out
of the Ceph cluster from Step 1. The playbook runs three plays, in this order:

1. **On the compact cluster:** create `openshift-storage`, subscribe to
   `odf-operator` (channel `stable-<ocp_major_version>`), and take the
   exporter script from the operator just installed. That script has to match
   the operator version. An older script writes the CSI credentials under key
   names a newer operator does not read.
2. **On `cephadmin`:** create the RBD pool `ocs-storagecluster-cephblockpool`
   and the CephFS filesystem `ocs-storagecluster-cephfilesystem` if they are
   missing. Then run the exporter script, which creates scoped CSI users and
   writes the fsid, mon endpoints and keys as JSON to
   `ceph-odf/external-cluster-details.json` on the hypervisor. That file holds
   credentials and is in `.gitignore`.
3. **On the compact cluster:** create the `rook-ceph-external-cluster-details`
   Secret from that JSON and the external `StorageCluster`
   `ocs-external-storagecluster`. The operator then creates the StorageClasses
   and marks the RBD class as the default (`ceph_odf_rbd_default_sc: true`).

**Check there is no LVM Storage on the cluster.** With `STORAGE=ceph` there
should be none:

```bash
oc get lvmcluster -A                           # No resources found
oc get storageclass                            # no lvms-vg1, and nothing marked (default)
```

If LVM Storage is there, remove it first, as in
[Switching a cluster that already has LVM Storage](#switching-a-cluster-that-already-has-lvm-storage).

**Run the playbook** from the repository root:

```bash
cd /path/to/ocp-onpremise                      # the repository root

# The ODF plays run plain `oc`. The compact build keeps its oc in $WORK, off
# the PATH; a hub build puts one in /usr/bin.
test -x /usr/bin/oc || cp $WORK/oc /usr/bin/oc

ansible-playbook -i inventory/hosts setup_ceph_odf.yaml --ask-vault-pass \
  -e ceph_odf_target_kubeconfig=$WORK/auth/kubeconfig
```

`ceph_odf_target_kubeconfig` is required. Do not use `-e target_hub=compact`:
the playbook knows only `hub` and `hub2`, treats every other value as `hub`,
and would install ODF on hub1. The plays set `KUBECONFIG` themselves, so the
`KUBECONFIG` exported in your shell does not change where they go.

The plays are tagged, so a failed stage can be run again on its own:
`--tags ceph-odf-install`, `ceph-odf-export`, `ceph-odf-create`.

**Check the result.** The last play waits only 60 seconds, so the
StorageCluster can still be `Progressing` when it ends. Give it a few minutes:

```bash
oc -n openshift-storage get csv                       # odf-operator, ocs-operator, ... Succeeded
oc -n openshift-storage get storagecluster            # ocs-external-storagecluster  PHASE Ready
oc get storageclass
# ocs-external-storagecluster-ceph-rbd (default)   openshift-storage.rbd.csi.ceph.com
# ocs-external-storagecluster-cephfs               openshift-storage.cephfs.csi.ceph.com
oc get volumesnapshotclass                            # one for RBD, one for CephFS
```

**Mount a volume.** A PVC that binds only shows that provisioning works. On a
node kernel that cannot map a Ceph 9 RBD image (OCP below 4.22), the PVC binds
and the pod then fails to start. Test with a pod:

```bash
oc apply -f - <<'EOF'
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: ceph-test
  namespace: default
spec:
  accessModes: [ReadWriteOnce]
  resources:
    requests:
      storage: 1Gi
---
apiVersion: v1
kind: Pod
metadata:
  name: ceph-test
  namespace: default
spec:
  containers:
  - name: test
    image: registry.access.redhat.com/ubi9/ubi-minimal
    command: ["sleep", "3600"]
    volumeMounts:
    - name: data
      mountPath: /data
  volumes:
  - name: data
    persistentVolumeClaim:
      claimName: ceph-test
EOF

oc -n default get pvc ceph-test                    # Bound, STORAGECLASS ocs-external-storagecluster-ceph-rbd
oc -n default wait pod/ceph-test --for=condition=Ready --timeout=180s
oc -n default exec ceph-test -- df -h /data        # /dev/rbd0, about 1G
ssh root@192.168.122.27 rbd ls ocs-storagecluster-cephblockpool   # csi-vol-<uuid>

oc -n default delete pod/ceph-test pvc/ceph-test
```

From here on, every PVC that names no StorageClass goes to Ceph RBD. That
includes the AgentServiceConfig's three PVCs and each hosted cluster's etcd
volumes in [compact-cluster-hcp.md](compact-cluster-hcp.md). Attach Ceph
**before** those steps: a PVC that is already bound stays on the class it got.
After the hosted cluster is up, check:

```bash
oc get pvc -n multicluster-engine              # three, Bound on ocs-external-storagecluster-ceph-rbd
oc get pvc -n hcp-compact1-hcp-compact1        # data-etcd-*, Bound on ocs-external-storagecluster-ceph-rbd
```

##### Sharing Ceph with a hub

One Ceph cluster can serve the hub and the compact cluster at the same time.
Run `setup_ceph_odf.yaml` once for each cluster, each with its own
kubeconfig. By default both runs use the same RBD pool and CephFS filesystem,
so both clusters provision into one pool. That works, but a pool of its own
keeps the compact cluster's volumes apart and makes it safe to clean up one
cluster without the other:

```bash
ansible-playbook -i inventory/hosts setup_ceph_odf.yaml --ask-vault-pass \
  -e ceph_odf_target_kubeconfig=$WORK/auth/kubeconfig \
  -e ceph_odf_rbd_pool=compact-rbd -e ceph_odf_enable_cephfs=false
```

Each run overwrites `ceph-odf/external-cluster-details.json`. That is
harmless: the cluster from the earlier run already holds its copy as a Secret.

A Ceph cluster shared with a hub also shares the hub's lifecycle. The DR
walkthrough in [steps.md](../steps.md) destroys and rebuilds Ceph with
`cleanup-ceph.yaml`. Doing that takes the compact cluster's volumes with it,
etcd included.

##### Switching a cluster that already has LVM Storage

Setting `use_lvm_storage: false` after the build uninstalls nothing. Remove
LVM Storage by hand before running `setup_ceph_odf.yaml`. Volumes on
`lvms-vg1` cannot move to Ceph, so delete or back up the PVCs that use it
first:

```bash
oc get pvc -A | grep lvms-vg1                          # delete these first

oc -n openshift-storage delete lvmcluster my-lvmcluster
oc get storageclass                                    # wait until lvms-vg1 is gone
oc -n openshift-storage delete subscription lvms
oc -n openshift-storage delete csv -l operators.coreos.com/lvms-operator.openshift-storage
oc -n openshift-storage delete operatorgroup openshift-storage-operatorgroup
```

Delete the OperatorGroup too. ODF creates its own, `openshift-storage-odf`,
and OLM installs nothing in a namespace that has two OperatorGroups. Keep the
`openshift-storage` namespace, because ODF uses it. The `vdb` disks can stay
attached to the nodes, since ODF does not use them.

### Step 13 - Test bond failover

Take down the active port of master1 and check that the bond moves to the
other port and the node stays reachable.

```bash
# In a second terminal, keep pinging the node and the API VIP
ping $LAB.64
ping $API_VIP

# Take the PRIMARY port's link down (enp1s0 inside the guest)
virsh domif-setlink compact_master1 $MAC1:64 down

ssh -i ~/.ssh/lab_rsa core@$LAB.64 "grep -E 'Currently Active|Slave Interface|MII Status' /proc/net/bonding/bond0"
# Currently Active Slave: enp2s0
# Slave Interface: enp1s0 / MII Status: down

# Put it back. Because primary=enp1s0, the bond moves back to enp1s0.
virsh domif-setlink compact_master1 $MAC1:64 up
```

Expect at most one or two lost pings while the miimon interval (100 ms)
detects the dead link and the bridge learns the bond's MAC on the other port.
Run the same test with `$MAC2:64` to take down the backup port: nothing should
change, because traffic is not using that port.

### Step 14 - Clean up

*Automated by: `cleanup.yaml --tags compact`*

```bash
for n in $NODES; do
  virsh destroy compact_${n%%:*}  2>/dev/null
  virsh undefine compact_${n%%:*} 2>/dev/null
done
rm -rf $VMDIR $WORK
```

`virsh undefine` is run without `--remove-all-storage` on purpose. The three
domains share the agent ISO as their cdrom, and removing the folders deletes
the disks anyway, the storage disks included.

The DHCP reservations, DNS zone and `/etc/hosts` entries stay in place, as
they do for every other cluster in this lab. They are derived from `ip_list`,
and a rebuild reuses them.

**Ceph** is not removed with the cluster. Its VMs and pools are separate, and
a rebuilt compact cluster can attach to the same Ceph cluster again (Step 12b).
The RBD images the cluster left behind stay in the pool. To remove the Ceph
cluster too, and only if no hub still uses it, run from the repository root:

```bash
ansible-playbook -i inventory/hosts cleanup-ceph.yaml
virsh list --all | grep -E 'ceph[123]|cephadmin'     # nothing
```

---

## Troubleshooting

| Symptom | Likely cause | Check |
|---|---|---|
| `agent create image` fails with `failed to validate network yaml for host N` | `nmstatectl` is missing, or the bond YAML is invalid | `which nmstatectl`. Check the indentation of `link-aggregation`. Every name under `port:` must also be listed under `interfaces:`. |
| A node boots the ISO but never gets an address | Its MACs do not match `agent-config.yaml`, so the agent found no host entry | `virsh domiflist compact_masterN` and compare with `rendered/agent-config.yaml` |
| The bond is up, but only one port is enslaved | The second NIC got a different name, for example after you added a device before it | `ssh core@<ip> ip -br link`. Set `compact_bond_secondary_nic` to the name the node uses. The MAC mapping in `interfaces` handles most of these cases. |
| master2/master3 never appear in the rendezvous host's list | They cannot reach `.64`, or a stale disk booted an old RHCOS instead of the ISO | `virsh console compact_master2`. Check for an old login prompt instead of the agent. |
| The agent's pre-flight reports `SERVFAIL` for quay.io | The node's DNS server points at the helper instead of `.1` | `compact_dns_server`. See Step 7. |
| `wait-for` cannot connect to the API | `api.compact.mylab.com` does not resolve to `.117` on the hypervisor | `getent hosts api.compact.mylab.com`. Run `setup_bm_host.yaml --tags dns` again. |
| Ingress and console operators are degraded | `*.apps` does not resolve to the ingress VIP, or the VIP is held by something else | `dig @192.168.122.21 x.apps.compact.mylab.com` and `ping 192.168.122.118` with the cluster down |
| Pings drop during failover and do not recover | `miimon` is 0, so the bond is not monitoring link state | `grep 'MII Polling' /proc/net/bonding/bond0`. It must not be 0. |
| The bond comes up with a different MAC on each reboot | `mac-address` is missing on `bond0` | Pin it to the primary NIC's MAC, as in Step 7 |
| LVMCluster stays `Progressing` or `Failed`, or `lvms-vg1` never appears | A node has no empty disk: `vdb` is missing, or it already holds partitions, a filesystem or an old volume group | `lsblk /dev/vdb` on each node (Step 12). `oc -n openshift-storage logs ds/vg-manager` names every device it skipped and why. Attach a fresh disk as in Step 12. |
| RHCOS was installed on the storage disk | `rootDeviceHints` was removed, or the storage disk was listed before the RHCOS disk in `virt-install` | `virsh domblklist compact_masterN`: the RHCOS disk must be `vda`. Rebuild with the disk order from Step 9. |
| A PVC stays `Pending` with no pod using it | `lvms-vg1` binds when the first pod uses the PVC (`WaitForFirstConsumer`) | Expected. Start a pod that mounts it. |
| Ceph: ODF ended up on the hub, not the compact cluster | `setup_ceph_odf.yaml` was run with `-e target_hub=compact` or without a kubeconfig | Run it again with `-e ceph_odf_target_kubeconfig=$WORK/auth/kubeconfig` (Step 12b). Remove ODF from the hub if it should not be there. |
| Ceph: `setup_ceph_odf.yaml` fails with `oc: command not found` | No `oc` in `/usr/bin` on a hypervisor that has never built a hub | `cp $WORK/oc /usr/bin/oc` (Step 12b) |
| Ceph: the ODF CSV is `Failed` with reason `TooManyOperatorGroups` | LVM Storage's OperatorGroup is still in `openshift-storage` | `oc -n openshift-storage get operatorgroup`. Delete `openshift-storage-operatorgroup`, as in Step 12b. |
| Ceph: the StorageCluster stays `Progressing` | Ceph is not `HEALTH_OK`, or the compact nodes cannot reach the mons on `.24`-`.26` | `ssh root@192.168.122.27 ceph -s`. `oc -n openshift-storage get storagecluster ocs-external-storagecluster -o yaml` shows the conditions. |
| Ceph: the PVC is `Bound` but the pod stays `ContainerCreating` with `failed to add secret to kernel` or error 524 | The node kernel cannot map a Ceph 9 RBD image: OCP below 4.22 | Build with OCP 4.22 or later. See [README, Requirements](../README.md#requirements). |
| Ceph: hosted cluster etcd or AgentServiceConfig PVCs are on `lvms-vg1` | They were created while `lvms-vg1` was the default StorageClass | Bound PVCs do not move. Remove LVM Storage, attach Ceph, then recreate them (Step 12b). |
