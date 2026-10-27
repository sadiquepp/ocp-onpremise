# IP allocation

Every address on the lab network, `192.168.122.0/24` (`lab_network_prefix`,
libvirt's `default` network on `virbr0`), in ascending order. Empty rows are
unallocated; consecutive free addresses share one row.

The source of truth is `vars.yaml`. This file only describes it, so when you
add or move an address there, update this table too. The table is generated
from these maps:

| Map in `vars.yaml` | What it holds |
| --- | --- |
| `ip_list` | every VM, and every VIP the helper's haproxy or a cluster's keepalived binds |
| `hosted_cluster_metallb_pools` | each hosted cluster's API VIP, per hub it can run on |
| `hosted_cluster_node_keys.<cluster>.apps_vip` | a hosted cluster's `*.apps` VIP when MetalLB inside it serves ingress |

## Rules for picking an address

- **VMs need `.2`-`.99`.** A VM's MAC is `52:54:00:e2:54:<octet>`, two digits,
  so a three-digit octet cannot become a MAC. Single digits (`.2`-`.9`) are
  zero-padded to `:02`-`:09`.
- **VIPs belong in `.100`-`.199`.** A VIP has no MAC, so it does not use up a
  two-digit octet. The helper haproxy's VIPs (`ip_list.*lb`), the compact
  clusters' keepalived VIPs and the MetalLB addresses live there, each at its
  old address plus 100.
  `roles/setup-bm-host` gives no `ip_list` VIP a libvirt DHCP reservation, and
  `roles/setup-hub-acm` refuses a MetalLB address that is any `ip_list`
  entry's, that is inside libvirt's DHCP pool, or that two clusters share.
- **`.100`-`.200` is also the helper dhcpd range** in `roles/setup-dhcp`. No
  playbook deploys that role, which is why the VIPs can live there. Deploying
  it would collide with them.
- **`.201`-`.249` is libvirt's DHCP pool**, handed out dynamically. Never put a
  static address there.
- The same last octet is reused on the fabric (`192.168.140.<octet>`) and in
  the VTEP block (`100.64.0.<octet>`) by the UDN/BGP/EVPN lab, so a node keeps
  one number everywhere. See [Other lab networks](#other-lab-networks).

## Moving the helper's VIPs on a lab that is up

The haproxy VIPs (`ip_list.*lb`) moved from two digits to `.1xx` (`.39` to
`.139` and so on). On a lab built before that, one run moves them:

```bash
ansible-playbook -i inventory/hosts setup_bm_host.yaml --ask-vault-pass --tags dns,lb
```

`dns` republishes the zones and the hypervisor's `/etc/hosts`. `lb` binds the
new addresses on the helper, removes the old ones, and reloads haproxy. The
zones and libvirt's dnsmasq cap TTLs at 60s, so clients may keep the old address
for up to a minute. The clusters need no change: their certificates name
`api.<cluster>`, not an address.

The compact clusters' VIPs (`.17`/`.18` to `.117`/`.118`, `.6`/`.7` to
`.106`/`.107`) are different: they are set in each cluster's `install-config`,
so a compact cluster built on the old addresses keeps them until it is
reinstalled. Re-rendering DNS before then points its `api` and `*.apps` at
addresses it does not answer on, so run `dns` together with the reinstall, or
point the names back by hand until then.

`virtnet` is not needed. It only drops the VIPs' unused DHCP reservations, and
it restarts the `default` network under every VM, so leave it for the next
rebuild.

## 192.168.122.0/24

| Address | Type | What | Defined by | Notes |
| --- | --- | --- | --- | --- |
| `.0` | Network | 192.168.122.0/24 network address |  |  |
| `.1` | Gateway | libvirt `default` network (virbr0): gateway, NAT, dnsmasq DNS and DHCP |  |  |
| `.2` | VM | sno2, second single-node OpenShift (rack2) | `ip_list.sno2` | only built when `sno2_enabled` is true |
| `.3` | VM | compactd (disconnected three-node compact cluster) master1 | `ip_list.compactd_master1` |  |
| `.4` | VM | compactd (disconnected three-node compact cluster) master2 | `ip_list.compactd_master2` |  |
| `.5` | VM | compactd (disconnected three-node compact cluster) master3 | `ip_list.compactd_master3` |  |
| `.6`–`.7` |  |  |  |  |
| `.8` | VM | hcp-compact1 worker1 (hosted cluster managed by the compact cluster) | `ip_list.hcpc1worker1` |  |
| `.9` | VM | hcp-compact1 worker2 (hosted cluster managed by the compact cluster) | `ip_list.hcpc1worker2` |  |
| `.10` | VM | hub2d (disconnected DR hub) bootstrap | `ip_list.hub2d_bootstrap` |  |
| `.11` | VM | hub2d (disconnected DR hub) master1 | `ip_list.hub2d_master1` |  |
| `.12` | VM | hub2d (disconnected DR hub) master2 | `ip_list.hub2d_master2` |  |
| `.13` | VM | hub2d (disconnected DR hub) master3 | `ip_list.hub2d_master3` |  |
| `.14` | VM | hub2d (disconnected DR hub) worker1 | `ip_list.hub2d_worker1` |  |
| `.15` | VM | hub2d (disconnected DR hub) worker2 | `ip_list.hub2d_worker2` |  |
| `.16` | VM | hub2d (disconnected DR hub) worker3 | `ip_list.hub2d_worker3` |  |
| `.17`–`.19` |  |  |  |  |
| `.20` | VM | sno, single-node OpenShift: node, API and *.apps | `ip_list.sno` |  |
| `.21` | VM | helper VM: DNS (named), haproxy, PXE/HTTP | `ip_list.helper_server` |  |
| `.22` | VM | mirror registry VM (registry.hub.mylab.com:8443) | `ip_list.registry` |  |
| `.23` | VM | MinIO VM, S3 target for OADP backups | `ip_list.minio` |  |
| `.24` | VM | Ceph 9 node 1 | `ip_list.ceph1` |  |
| `.25` | VM | Ceph 9 node 2 | `ip_list.ceph2` |  |
| `.26` | VM | Ceph 9 node 3 | `ip_list.ceph3` |  |
| `.27` | VM | Ceph 9 admin node | `ip_list.cephadmin` |  |
| `.28` | VM | hcp-cluster2-d worker1 | `ip_list.c2dworker1` |  |
| `.29` | VM | hcp-cluster2-d worker2 | `ip_list.c2dworker2` |  |
| `.30` | VM | hub (hub1) bootstrap | `ip_list.bootstrap` |  |
| `.31` | VM | hub (hub1) master1 | `ip_list.master1` |  |
| `.32` | VM | hub (hub1) master2 | `ip_list.master2` |  |
| `.33` | VM | hub (hub1) master3 | `ip_list.master3` |  |
| `.34` | VM | hub (hub1) worker1 | `ip_list.worker1` |  |
| `.35` | VM | hub (hub1) worker2 | `ip_list.worker2` |  |
| `.36` | VM | hub (hub1) worker3 | `ip_list.worker3` |  |
| `.37` | VM | hcp-cluster2-d worker3 | `ip_list.c2dworker3` |  |
| `.38`–`.39` |  |  |  |  |
| `.40` | VM | containerlab VM (UDN/BGP/EVPN fabric: leaf1, spine, leaf2) | `ip_list.clab` |  |
| `.41` | VM | hcp-cluster1 worker1 | `ip_list.c1worker1` |  |
| `.42` | VM | hcp-cluster1 worker2 | `ip_list.c1worker2` |  |
| `.43` | VM | hcp-cluster1 worker3 | `ip_list.c1worker3` |  |
| `.44` | VM | hcp-cluster1-d worker1 | `ip_list.c1dworker1` |  |
| `.45` | VM | hcp-cluster1-d worker2 | `ip_list.c1dworker2` |  |
| `.46` | VM | hcp-cluster1-d worker3 | `ip_list.c1dworker3` |  |
| `.47` | VM | UDN lab external client VM | `ip_list.udnclient` |  |
| `.48`–`.49` |  |  |  |  |
| `.50` | VM | UDN lab tenant client VM: blue | `ip_list.udnclient-blue` |  |
| `.51` | VM | hcp-cluster2 worker1 | `ip_list.c2worker1` |  |
| `.52` | VM | hcp-cluster2 worker2 | `ip_list.c2worker2` |  |
| `.53` | VM | hcp-cluster2 worker3 | `ip_list.c2worker3` |  |
| `.54` | VM | hcp-cluster3 worker1 | `ip_list.c3worker1` |  |
| `.55` | VM | hcp-cluster3 worker2 | `ip_list.c3worker2` |  |
| `.56` | VM | hcp-cluster3 worker3 | `ip_list.c3worker3` |  |
| `.57` | VM | UDN lab tenant client VM: red | `ip_list.udnclient-red` |  |
| `.58`–`.63` |  |  |  |  |
| `.64` | VM | compact (three-node compact cluster) master1 | `ip_list.compact_master1` |  |
| `.65` | VM | compact (three-node compact cluster) master2 | `ip_list.compact_master2` |  |
| `.66` | VM | compact (three-node compact cluster) master3 | `ip_list.compact_master3` |  |
| `.67`–`.69` |  |  |  |  |
| `.70` | VM | UDN lab tenant client VM: orange and green | `ip_list.udnclient-og` |  |
| `.71` | Reserved | reserved for a virtual BMC for hcp-cluster2 (only referenced in commented-out lines of setup_hosted_cluster2_vm.yaml) | `ip_list.c2vbmc` |  |
| `.72` | VM | hubd (disconnected hub) bootstrap | `ip_list.hubd_bootstrap` |  |
| `.73` | VM | hubd (disconnected hub) master1 | `ip_list.hubd_master1` |  |
| `.74` | VM | hubd (disconnected hub) master2 | `ip_list.hubd_master2` |  |
| `.75` | VM | hubd (disconnected hub) master3 | `ip_list.hubd_master3` |  |
| `.76` | VM | hubd (disconnected hub) worker1 | `ip_list.hubd_worker1` |  |
| `.77` | VM | hubd (disconnected hub) worker2 | `ip_list.hubd_worker2` |  |
| `.78` | VM | hubd (disconnected hub) worker3 | `ip_list.hubd_worker3` |  |
| `.79` |  |  |  |  |
| `.80` | VM | hub2 (DR hub) bootstrap | `ip_list.hub2_bootstrap` |  |
| `.81` | VM | hub2 (DR hub) master1 | `ip_list.hub2_master1` |  |
| `.82` | VM | hub2 (DR hub) master2 | `ip_list.hub2_master2` |  |
| `.83` | VM | hub2 (DR hub) master3 | `ip_list.hub2_master3` |  |
| `.84` | VM | hub2 (DR hub) worker1 | `ip_list.hub2_worker1` |  |
| `.85` | VM | hub2 (DR hub) worker2 | `ip_list.hub2_worker2` |  |
| `.86` | VM | hub2 (DR hub) worker3 | `ip_list.hub2_worker3` |  |
| `.87` | VM | UDN lab tenant client VM: purple | `ip_list.udnclient-purple` |  |
| `.88` | VM | UDN lab namespace client VM (one netns per tenant) | `ip_list.udnnsclient` |  |
| `.89`–`.95` |  |  |  |  |
| `.96` | VM | hcp-cluster3-d worker1 | `ip_list.c3dworker1` |  |
| `.97` | VM | hcp-cluster3-d worker2 | `ip_list.c3dworker2` |  |
| `.98` | VM | hcp-cluster3-d worker3 | `ip_list.c3dworker3` |  |
| `.99` |  |  |  |  |
| `.100` | MetalLB VIP | hcp-compact1 API (kube-apiserver LoadBalancer) on the compact cluster | `hosted_cluster_metallb_pools.hcp-compact1.ip.compact` | pool `hcp-compact1-api-pool` |
| `.101` | MetalLB VIP | hcp-compact1 *.apps (MetalLB inside the hosted cluster, in front of its routers) | `hosted_cluster_node_keys.hcp-compact1.apps_vip` |  |
| `.102`–`.105` |  |  |  |  |
| `.106` | VIP | compactd API VIP (keepalived on its masters) | `ip_list.compactd_api` |  |
| `.107` | VIP | compactd *.apps ingress VIP (keepalived on its masters) | `ip_list.compactd_ingress` |  |
| `.108`–`.116` |  |  |  |  |
| `.117` | VIP | compact API VIP (keepalived on its masters) | `ip_list.compact_api` |  |
| `.118` | VIP | compact *.apps ingress VIP (keepalived on its masters) | `ip_list.compact_ingress` |  |
| `.119` | VIP | hub2d (disconnected DR hub) API (6443), machine-config (22623) and *.apps (80/443) VIP on the helper's haproxy | `ip_list.hub2dlb` |  |
| `.120`–`.137` |  |  |  |  |
| `.138` | VIP | hcp-cluster2-d *.apps VIP on the helper's haproxy | `ip_list.c2dlb` |  |
| `.139` | VIP | hub (hub1) API (6443), machine-config (22623) and *.apps (80/443) VIP on the helper's haproxy | `ip_list.hublb` |  |
| `.140`–`.147` |  |  |  |  |
| `.148` | VIP | hcp-cluster1-d *.apps VIP on the helper's haproxy | `ip_list.c1dlb` |  |
| `.149` | VIP | hcp-cluster1 *.apps VIP on the helper's haproxy | `ip_list.c1lb` |  |
| `.150`–`.157` |  |  |  |  |
| `.158` | VIP | hcp-cluster3 *.apps VIP on the helper's haproxy | `ip_list.c3lb` |  |
| `.159` | VIP | hcp-cluster2 *.apps VIP on the helper's haproxy | `ip_list.c2lb` |  |
| `.160` | MetalLB VIP | hcp-cluster1 API (kube-apiserver LoadBalancer) on hub (hub1) | `hosted_cluster_metallb_pools.hcp-cluster1.ip.hub` | pool `hcp-cluster1-api-pool` |
| `.161` | MetalLB VIP | hcp-cluster2 API (kube-apiserver LoadBalancer) on hub (hub1) | `hosted_cluster_metallb_pools.hcp-cluster2.ip.hub` | pool `hcp-cluster2-api-pool` |
| `.162` | MetalLB VIP | hcp-cluster3 API (kube-apiserver LoadBalancer) on hub (hub1) | `hosted_cluster_metallb_pools.hcp-cluster3.ip.hub` | pool `hcp-cluster3-api-pool` |
| `.163`–`.166` |  |  |  |  |
| `.167` | MetalLB VIP | hcp-cluster1-d API (kube-apiserver LoadBalancer) on hubd (disconnected hub) | `hosted_cluster_metallb_pools.hcp-cluster1-d.ip.hubd` | pool `hcp-cluster1-d-api-pool` |
| `.168` | MetalLB VIP | hcp-cluster2-d API (kube-apiserver LoadBalancer) on hubd (disconnected hub) | `hosted_cluster_metallb_pools.hcp-cluster2-d.ip.hubd` | pool `hcp-cluster2-d-api-pool` |
| `.169` | MetalLB VIP | hcp-cluster3-d API (kube-apiserver LoadBalancer) on hubd (disconnected hub) | `hosted_cluster_metallb_pools.hcp-cluster3-d.ip.hubd` | pool `hcp-cluster3-d-api-pool` |
| `.170`–`.178` |  |  |  |  |
| `.179` | VIP | hubd (disconnected hub) API (6443), machine-config (22623) and *.apps (80/443) VIP on the helper's haproxy | `ip_list.hubdlb` |  |
| `.180`–`.188` |  |  |  |  |
| `.189` | VIP | hub2 (DR hub) API (6443), machine-config (22623) and *.apps (80/443) VIP on the helper's haproxy | `ip_list.hub2lb` |  |
| `.190` | MetalLB VIP | hcp-cluster1 API (kube-apiserver LoadBalancer) on hub2 (DR hub) | `hosted_cluster_metallb_pools.hcp-cluster1.ip.hub2` | pool `hcp-cluster1-api-pool` |
| `.191` | MetalLB VIP | hcp-cluster2 API (kube-apiserver LoadBalancer) on hub2 (DR hub) | `hosted_cluster_metallb_pools.hcp-cluster2.ip.hub2` | pool `hcp-cluster2-api-pool` |
| `.192` | MetalLB VIP | hcp-cluster3 API (kube-apiserver LoadBalancer) on hub2 (DR hub) | `hosted_cluster_metallb_pools.hcp-cluster3.ip.hub2` | pool `hcp-cluster3-api-pool` |
| `.193` | MetalLB VIP | hcp-cluster1-d API (kube-apiserver LoadBalancer) on hub2 (DR hub) | `hosted_cluster_metallb_pools.hcp-cluster1-d.ip.hub2` | pool `hcp-cluster1-d-api-pool` |
| `.194` | MetalLB VIP | hcp-cluster2-d API (kube-apiserver LoadBalancer) on hub2 (DR hub) | `hosted_cluster_metallb_pools.hcp-cluster2-d.ip.hub2` | pool `hcp-cluster2-d-api-pool` |
| `.195` | MetalLB VIP | hcp-cluster3-d API (kube-apiserver LoadBalancer) on hub2 (DR hub) | `hosted_cluster_metallb_pools.hcp-cluster3-d.ip.hub2` | pool `hcp-cluster3-d-api-pool` |
| `.196`–`.198` |  |  |  |  |
| `.199` | VIP | hcp-cluster3-d *.apps VIP on the helper's haproxy | `ip_list.c3dlb` |  |
| `.200` |  |  |  |  |
| `.201`–`.249` | DHCP pool | libvirt DHCP pool (virbr0), handed out dynamically |  |  |
| `.250`–`.254` |  |  |  |  |
| `.255` | Broadcast | 192.168.122.255 |  |  |

## Other lab networks

The UDN/BGP/EVPN lab's networks, for reference. Each is described in
[udn-bgp-evpn/clab-fabric.md](udn-bgp-evpn/clab-fabric.md).

| Network | Use | Allocation |
| --- | --- | --- |
| `192.168.140.0/24` | containerlab fabric, untagged (`virbr1`) | leaf1 `.1`; each node and the containerlab VM on its `ip_list` octet |
| `192.168.141.0/24`-`192.168.146.0/24` | VRF-Lite per-tenant VLANs on the fabric: blue 110, red 120, orange 130, green 140, purple 150, violet 160 | leaf1 `.1`; each node on its `ip_list` octet |
| `100.64.0.0/24` | EVPN VTEPs (`udn_bgp_vtep_prefix`) | each node's VTEP on its `ip_list` octet, e.g. worker1 `100.64.0.34` |
| `10.0.0.0/24` | fabric router loopbacks | leaf1 `.1`, leaf2 `.2`, spine `.254` |
| `10.1.0.0/30`, `10.1.0.4/30` | leaf1-spine and leaf2-spine links | leaf `.1`/`.6`, spine `.2`/`.5` |
| `10.215.10.0/24`-`10.219.10.0/24`, `10.227.10.0/24` | per-tenant client segments (`udn_client_segments`): blue VLAN 210 through violet 260 | leaf `.1`, tenant client VM `.20`, namespace client VM `.21` |
| `172.30.30.0/24` | containerlab management network (Docker) | assigned by containerlab |
