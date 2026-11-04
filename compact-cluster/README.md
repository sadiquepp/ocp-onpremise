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

### The config on a master

Taken from master1 (`192.168.122.64`) on the lab cluster. The installer
renders these files; do not edit them on the nodes. Each master has the same
files, except that `unicast_src_ip` is its own address and `unicast_peer`
lists the other two. The VRRP passwords and the stats credentials are
replaced with placeholders.

**`/etc/keepalived/keepalived.conf`**, the checks and the two VRRP instances:

```
vrrp_script chk_ocp_lb {
    script "/usr/bin/timeout 1.9 /etc/keepalived/chk_ocp_script.sh"
    interval 2
    weight 20
    rise 3
    fall 3
}

vrrp_script chk_ocp_both {
    script "/usr/bin/timeout 1.9 /etc/keepalived/chk_ocp_script_both.sh"
    interval 2
    weight 5
    rise 3
    fall 3
}

vrrp_script chk_mcs {
    script "/usr/bin/timeout 1.9 /etc/keepalived/chk_mcs_script.sh"
    interval 2
    weight 3
    rise 3
    fall 3
}

vrrp_script chk_ingress_ready {
    script "/usr/bin/timeout 0.9 /usr/bin/curl -o /dev/null -Lfs http://localhost:1936/healthz/ready"
    interval 1
    weight 10
    rise 3
    fall 2
}

vrrp_script chk_ingress {
    script "/usr/bin/timeout 0.9 /usr/bin/curl -o /dev/null -Lfs http://localhost:1936/healthz"
    interval 1
    rise 3
    fall 2
}

vrrp_script chk_default_ingress {
    script "/usr/bin/timeout 4.9 /etc/keepalived/chk_default_ingress.sh"
    interval 5
    weight 50
    rise 3
    fall 2
}

vrrp_instance compact_API_0 {
    state BACKUP
    interface br-ex
    virtual_router_id 152
    priority 40
    advert_int 1
    unicast_src_ip 192.168.122.64
    unicast_peer {
        192.168.122.65
        192.168.122.66
    }
    authentication {
        auth_type PASS
        auth_pass <api-vrrp-password>
    }
    virtual_ipaddress {
        192.168.122.117/32 label vip
    }
    track_script {
        chk_ocp_lb
        chk_ocp_both
        chk_mcs
    }
}

vrrp_instance compact_INGRESS_0 {
    state BACKUP
    interface br-ex
    virtual_router_id 87
    priority 20
    advert_int 1
    unicast_src_ip 192.168.122.64
    unicast_peer {
        192.168.122.65
        192.168.122.66
    }
    authentication {
        auth_type PASS
        auth_pass <ingress-vrrp-password>
    }
    virtual_ipaddress {
        192.168.122.118/32 label vip
    }
    track_script {
        chk_ingress
        chk_ingress_ready
        chk_default_ingress
    }
}
```

Both instances start as `BACKUP` on every master, and VRRP elects the holder
by priority: the base `priority` plus the `weight` of each passing check.
`unicast_peer` means the masters send VRRP adverts straight to each other
instead of multicasting them. `vrrp_garp_master_refresh 60` in `global_defs`
makes the holder repeat its gratuitous ARP every 60 seconds.

**`/etc/keepalived/chk_default_ingress.sh`**, the +50 check for the ingress
VIP:

```bash
#!/bin/bash
chroot /host /bin/crictl pods --namespace openshift-ingress --state Ready | grep -qE 'router-default-[[:alnum:]]{5,}-[[:alnum:]]{5,}'
```

keepalived runs in a container, so the script uses `chroot /host` to run the
node's own `crictl`.

**`/etc/haproxy/haproxy.cfg`**, the API load balancer:

```
defaults
  maxconn 40000
  mode    tcp
  retries 3
  timeout connect      10s
  timeout client       86400s
  timeout server       86400s
  timeout tunnel       86400s
frontend  main
  bind :::9445 v4v6
  default_backend masters
listen health_check_http_url
  bind :::9444 v4v6
  mode http
  monitor-uri /haproxy_ready
listen monitor_check_http_url
  bind :::9454 v4v6
  mode http
  monitor-uri /haproxy_monitor
  monitor fail if { nbsrv(masters) lt 1 }
listen stats
  bind localhost:29445
  mode http
  stats enable
  stats uri /haproxy_stats
  stats auth <user>:<password>
backend masters
   timeout check 10s
   option  httpchk GET /readyz HTTP/1.0
   balance roundrobin
   server master1 192.168.122.64:6443 weight 1 verify none check check-ssl inter 5s fall 3 rise 1
   server master2 192.168.122.65:6443 weight 1 verify none check check-ssl inter 5s fall 3 rise 1
   server master3 192.168.122.66:6443 weight 1 verify none check check-ssl inter 5s fall 3 rise 1
```

`mode tcp` means haproxy passes the TLS stream through untouched. The
apiserver's own certificate reaches the client.

**The redirect rules**, from `sudo iptables -t nat -S | grep OCP_API_LB_REDIRECT`:

```
-A PREROUTING -d 192.168.122.117/32 -p tcp -m tcp --dport 6443 -m comment --comment OCP_API_LB_REDIRECT -j DNAT --to-destination 192.168.122.117:9445
-A OUTPUT -d 192.168.122.117/32 -o lo -p tcp -m tcp --dport 6443 -m comment --comment OCP_API_LB_REDIRECT -j REDIRECT --to-ports 9445
```

These are why `VIP:6443` reaches haproxy and not the kube-apiserver that also
listens on `*:6443`. nat `PREROUTING` runs before the kernel picks a
listening socket, so by the time it looks, the destination port is already
9445. The holder's own clients go through nat `OUTPUT` before their packets
loop back over `lo`. Only the first packet of a connection goes through the
nat table. conntrack applies the same rewrite to the rest of the connection,
and rewrites the replies back to `:6443`, so the client never sees 9445. The
same applies when `haproxy-monitor` removes the rules: only **new**
connections fall back to the local apiserver, and existing ones keep their
translation until they reconnect.

RHCOS uses the `iptables-nft` backend, so `sudo nft list ruleset` shows the
same two rules as nftables rules.

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
