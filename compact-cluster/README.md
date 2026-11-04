# Three-node compact cluster

A three-node compact OpenShift cluster (three schedulable masters, no
workers), installed from a single agent ISO, with every node's address on a
bond over two NICs.

| File | What it is |
| --- | --- |
| [compact-cluster.md](compact-cluster.md) | Lab, connected: three VMs on the hypervisor, active-backup (mode 1) bonds. Manual steps and the automated path. |
| [compact-cluster-hcp.md](compact-cluster-hcp.md) | Lab, connected: the compact cluster as a management cluster - ACM/MCE/MetalLB (`acm/setup_acm.yaml -e target_hub=compact`) and one two-worker hosted cluster, `hcp-compact1`, with MetalLB ingress inside it. Side by side with the hub's hosted clusters. |
| [compact-cluster-disconnected.md](compact-cluster-disconnected.md) | Lab, disconnected (`compactd`): installed only from the lab's mirror registry, with the nodes' internet egress blocked. |
| [compact-cluster-baremetal.md](compact-cluster-baremetal.md) | Physical servers, connected, with LACP (802.3ad) bonds. Manual steps only. |
| [compact-cluster-baremetal-disconnected.md](compact-cluster-baremetal-disconnected.md) | Physical servers, from your own mirror registry, with LACP bonds. Self-contained manual steps. |
| [sno-to-compact.md](sno-to-compact.md) | One page: what changes between the single-node cluster (`sno/setup_sno.yaml`) and this one, why the move is a reinstall, and what the API and ingress VIPs are. |
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
# and attach it after, both from inside ../ceph (compact-cluster.md,
# ../ceph/README.md)
#   cd ../ceph
#   ansible-playbook -i ../inventory/hosts setup_ceph.yaml --ask-vault-pass
#   ansible-playbook -i ../inventory/hosts setup_ceph_odf.yaml --ask-vault-pass \
#     -e ceph_odf_target_kubeconfig=/var/lib/libvirt/images/compact_install/auth/kubeconfig

# disconnected (builds the mirror registry first)
ansible-playbook -i ../inventory/hosts setup_compact_cluster_disconnected.yaml \
  -e disconnected_install=true --ask-vault-pass
```

To remove a cluster, run `cleanup.yaml --tags compact` or `--tags compactd`
from the repository root. The Ceph cluster is not part of either. Remove it
with `cleanup.yaml --tags ceph`, and only if no hub uses it.

The roles stay in the shared `../roles/`: `setup-compact-cluster` (reused by
both playbooks, and sharing the disconnected hub's egress-block tasks),
`setup-lvm-storage`, and for Ceph `setup-ceph-*`.

## How the API and ingress VIPs work

The compact cluster has no external load balancer. `platform: baremetal` runs
**keepalived** on every master, as a static pod in `openshift-kni-infra`. For
each VIP, keepalived puts the address on `br-ex` on **one** master at a time
and announces it with gratuitous ARP. What happens to the traffic after it
lands is different for the two VIPs. The examples use the lab's addresses:
masters `192.168.122.64`-`.66`, API VIP `.117`, ingress VIP `.118`
([ip-allocation.md](../ip-allocation.md)).

### API VIP: keepalived, a NAT rule, then haproxy across all three masters

```
oc, kubelets (api-int) ──► api.compact.mylab.com:6443 ──► 192.168.122.117 (API VIP)
                                                              │ held by keepalived on one master
                                                              ▼
              nat PREROUTING  dst .117 tcp/6443 ──► DNAT .117:9445     (from other hosts)
              nat OUTPUT -o lo dst .117 tcp/6443 ──► REDIRECT :9445    (the holder's own clients)
                                                              │
                                                              ▼
              haproxy :9445   static pod in openshift-kni-infra, mode tcp (TLS passes through)
              balance roundrobin, health check GET /readyz over TLS every 5s
                     │                        │                        │
                     ▼                        ▼                        ▼
              .64:6443 master1         .65:6443 master2         .66:6443 master3
                              kube-apiserver on each node IP
```

- **haproxy listens on 9445, not 6443**, because kube-apiserver already binds
  `*:6443` on every master, including the VIP. The `haproxy-monitor` container
  next to haproxy adds the two `OCP_API_LB_REDIRECT` rules while haproxy is
  healthy. haproxy connects to the backends on their **node** IPs, so its own
  traffic does not match the rules again.
- **If haproxy fails**, the rules are removed and `VIP:6443` falls through to
  the local kube-apiserver. The API stays up, but all of it goes through one
  node.
- The machine-config server (`:22623`) is not behind haproxy. It answers on
  the VIP holder directly, which is why keepalived also checks it.

### Ingress VIP: keepalived only, straight into the router pod

```
browser ──► console-openshift-console.apps.compact.mylab.com:443 ──► 192.168.122.118 (ingress VIP)
                                                              │ held by keepalived, only on a master
                                                              │ running a ready router-default pod
                                                              ▼
              router-default pod on that master   HAProxy, hostNetwork: listens on the node's :80 / :443
              no NAT and no extra proxy in front   picks the Route by Host header (HTTP) or SNI (TLS)
                     │                        │
                     ▼                        ▼
              app pod IPs (Endpoints) on any master, over the OVN pod network
```

- Ingress is **active/passive**. All `*.apps` traffic enters through the one
  router on the VIP holder. The other router pod is the standby.
- The default IngressController runs **2 replicas**, so only 2 of the 3
  masters can hold the ingress VIP. Scale it to 3 if the VIP should be able to
  use any master:
  `oc patch ingresscontroller/default -n openshift-ingress-operator --type merge -p '{"spec":{"replicas":3}}'`
- Hitting a master's own IP on `:443` reaches the same router, as long as that
  master runs one. Keep the hostname, because the router routes by name:
  `curl -k --resolve console-openshift-console.apps.compact.mylab.com:443:192.168.122.64 https://console-openshift-console.apps.compact.mylab.com/`

### How keepalived picks the master

Each VIP is a VRRP instance in `/etc/keepalived/keepalived.conf` on every
master. Track scripts add their `weight` to the base priority while they pass.
The highest priority holds the VIP. A script with no weight puts the instance
in FAULT when it fails, and a FAULT node never holds the VIP.

| VRRP instance | Base | Check (weight) | Healthy holder |
|---|---|---|---|
| `compact_API_0` (id 152) | 40 | `chk_ocp_lb` haproxy healthy (+20), `chk_ocp_both` haproxy **or** local apiserver (+5), `chk_mcs` machine-config server (+3) | 68 |
| `compact_INGRESS_0` (id 87) | 20 | `chk_ingress` router `:1936/healthz` (no weight: FAULT on failure), `chk_ingress_ready` `:1936/healthz/ready` (+10), `chk_default_ingress` a ready `router-default-*` pod on this node (+50) | 80 |

`chk_default_ingress.sh` runs `crictl pods --namespace openshift-ingress --state Ready`
on the host and looks for `router-default-<hash>-<hash>`. With only the default
IngressController this just backs up `chk_ingress`. Once you add sharded
IngressControllers, it keeps the `*.apps` VIP on masters that run the
**default** router.

Failover takes about 2 to 6 seconds: the ingress checks run every second and
fail after 2 misses, the API checks every 2 seconds after 3 misses. The new
holder sends gratuitous ARP, so clients and the switch learn the new MAC.

### haproxy's other ports

From `/etc/haproxy/haproxy.cfg` on each master:

| Section | Port | Job |
|---|---|---|
| `frontend main` / `backend masters` | 9445 | The API traffic redirected from `VIP:6443`, roundrobin to the three apiservers |
| `health_check_http_url` | 9444 `/haproxy_ready` | haproxy is up |
| `monitor_check_http_url` | 9454 `/haproxy_monitor` | Fails when no apiserver backend is healthy |
| `stats` | localhost:29445 `/haproxy_stats` | Stats page |

Timeouts are 24 hours (`86400s`), so long `oc` watches and log streams are
not cut off.

### Check it

```bash
# Which master holds each VIP
for n in 64 65 66; do
  ssh -i ~/.ssh/lab_rsa core@192.168.122.$n "ip -br addr show br-ex | grep -oE '192.168.122.11[78]'" \
    | sed "s/^/.$n holds /"
done

# The API redirect, on the API VIP holder (the counters count new connections)
sudo iptables -t nat -L PREROUTING -v -n | grep OCP_API_LB_REDIRECT

# Where the router pods run, and that they use the host network
oc get pods -n openshift-ingress -o wide
oc get ingresscontroller default -n openshift-ingress-operator \
  -o jsonpath='{.status.endpointPublishingStrategy.type}{"\n"}'    # HostNetwork

# On the ingress VIP holder: HAProxy listening on the node's ports
sudo ss -ltnp | grep -E ':(80|443|1936) '
```

[sno-to-compact.md](sno-to-compact.md) has the short version, and why a single
node needs neither VIP.
