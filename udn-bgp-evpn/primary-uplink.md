# br-ex facing the fabric (`clab_fabric_uplink: primary`)

The rest of this directory wires each node into the fabric with a **second
NIC** on an isolated bridge, and never moves a default route. This page is the
other wiring: the node has **one** uplink, `br-ex`, and the fabric's border
leaf is at the other end of it - the node's BGP peer and its default gateway
at one address. That is the shape a production cluster has.

It is one switch in `vars.yaml`, it is **EVPN only**, and it is **not yet
proven on a live lab** - see [What has and has not been tested](#what-has-and-has-not-been-tested).

```yaml
clab_fabric_uplink: primary     # default: secondary
```

## What changes

```
secondary (default)                         primary

virbr0 192.168.122.0/24                     virbr0 192.168.122.0/24
  lab host .1   <- nodes' gateway             lab host .1   <- leaf1's upstream (NAT), DNS
  nodes' br-ex                                leaf1 .38     <- nodes' gateway AND BGP peer
                                              nodes' br-ex  <- BGP session + VXLAN underlay
virbr1 192.168.140.0/24
  leaf1 .1      <- BGP peer                 virbr1  (clients only: leaf1 .1, leaf2 trunk,
  nodes' 2nd NIC <- session + underlay               the namespace client VM)

leaf1 -- spine -- leaf2                     leaf1 -- spine -- leaf2      (unchanged)
```

| | secondary | primary |
| --- | --- | --- |
| Node NIC facing the fabric | a hot-plugged second NIC, addressed by an NNCP | `br-ex`, addressed by the install |
| Node's BGP peer | `192.168.140.1` | `192.168.122.38` (`ip_list.fabric_gw`) |
| Node's default gateway | `192.168.122.1`, never touched | `192.168.122.38`, set at install |
| Hub build | `setup_cluster1.yaml` (UPI, DHCP) | `setup_cluster1_agent.yaml` (agent ISO, static) |
| Build order | clusters, then fabric | fabric, then clusters |
| Hub nodes on the fabric | the three workers | all six |
| Phases | default, shared, vrflite, evpn | evpn |
| Fabric-side MTU on the node link | 9000 | 1500 |
| Forwarding / `rp_filter` repair on the fabric NIC | needed | not applicable |

What does **not** change: the VTEP is still a `/32` on `vtep0` in
`100.64.0.0/24`, reached by a static route on leaf1 - only the next hop is now
the node's lab address. The tenants, VNIs, CUDNs, `RouteAdvertisements`, the
spine and leaf2 are identical. So are the hub's name, addresses, VM names,
DNS zone and load balancer.

The pattern adds **one address** to the lab: `.38`, leaf1's leg on virbr0.
The hub reuses its own six addresses and `ip_list.hublb`; there is no API VIP
and no ingress VIP.

## Why the hub is built differently

A node's default gateway cannot be changed after install on `br-ex` - NMState
may not modify an OVN-Kubernetes-managed `br-ex` - so it has to be right when
the node first boots. The UPI hub takes its addressing from libvirt's DHCP,
which hands every VM on virbr0 the same router. An agent-based install writes
static addressing into `agent-config.yaml`, where the gateway is a value:

```yaml
routes:
  config:
    - destination: 0.0.0.0/0
      next-hop-address: 192.168.122.38      # lab_node_gateway
      next-hop-interface: enp1s0
```

`roles/setup-hub-cluster-agent` does that for the hub, and
`roles/setup-sno` already did it for the SNO (`sno_gateway`). Both read
`lab_node_gateway` from `vars.yaml`, which follows the switch.

The hub stays `platform: none`: `api`, `api-int` and `*.apps` are the helper's
haproxy, exactly as before. The agent checks those three names from every
node before it installs anything, so DNS and the load balancer have to be up
first - they are, they are built by the `bmhost` step.

## Why the fabric comes first

The nodes pull their release payload through their gateway, and their gateway
is a container. So the fabric is built before either cluster, which is only
possible because in this mode it attaches nothing to them. Both cluster
pre-flights ping the gateway and name the command if it does not answer.

DNS, the API and ingress never pass through leaf1: `192.168.122.1`, the
helper and the load balancer are all on-link. If the containerlab VM is down
the clusters keep running; they lose off-subnet reachability (image pulls
from the internet) and their fabric sessions, and nothing else.

## Build it

From a clean host:

```bash
# vars.yaml: clab_fabric_uplink: primary
cd udn-bgp-evpn
./build-lab.sh
```

`build-lab.sh` reads the switch from `vars.yaml`: it builds the fabric before
the clusters, uses `setup_cluster1_agent.yaml` for the hub, and refuses
`--vrflite` and `--shared`. `./build-lab.sh --list` shows the order in force.

Switching a lab that is already built is a rebuild of the fabric and of both
clusters:

```bash
ansible-playbook -i inventory/hosts cleanup.yaml --tags udnlab,hub,sno --ask-vault-pass
```

By hand, in order:

```bash
# 1. helper: DNS and the load balancer (repository root)
ansible-playbook -i inventory/hosts setup_bm_host.yaml --ask-vault-pass -e clab_topology=evpn

# 2. the fabric - no node NICs, leaf1 on the lab network
cd udn-bgp-evpn
ansible-playbook -i ../inventory/hosts setup_udn_bgp_lab.yaml --ask-vault-pass --tags fabric -e clab_topology=evpn

# 3. the clusters, in either order or in parallel
cd ../standard-cluster
ansible-playbook -i ../inventory/hosts setup_cluster1_agent.yaml --ask-vault-pass
cd ../sno
ansible-playbook -i ../inventory/hosts setup_sno.yaml --ask-vault-pass

# 4. the cluster half, once per cluster
cd ../udn-bgp-evpn
ansible-playbook -i ../inventory/hosts setup_udn_bgp_lab.yaml --ask-vault-pass --tags evpn -e clab_topology=evpn -e udn_bgp_cluster=hub
ansible-playbook -i ../inventory/hosts setup_udn_bgp_lab.yaml --ask-vault-pass --tags evpn -e clab_topology=evpn -e udn_bgp_cluster=sno
```

## What the fabric half does

- **No node NICs.** `node-nics.yml` is skipped for the first segment.
- **The containerlab VM gets a third NIC**, on the libvirt `default` network
  (MAC `52:54:00:e2:57:40`), enslaved to an in-guest bridge `br-uplink`
  (`tasks/uplink.yml`). It is a second NIC on the network the VM's management
  NIC is already on, so the bridge script flushes any DHCP lease off it and
  sets `arp_ignore=1` on the bridge - otherwise the VM answers ARP for its own
  management address from the bridge, and ssh to it fails intermittently. In
  host mode leaf1 joins `virbr0` directly and none of this is built.
- **leaf1 gets `eth2`** on that bridge: `192.168.122.38/24`, MTU 1500, and
  its default route replaced with `via 192.168.122.1 dev eth2`. ICMP redirects
  are turned off, because a gateway that forwards back out of the interface a
  packet arrived on would otherwise tell every node to bypass it.
- **leaf1's neighbours and VTEP routes** use the nodes' lab addresses over
  `eth2`. Its `LAB-MGMT` route-maps still keep `192.168.122.0/24` out of BGP
  in both directions.
- **`fabric-verify.yml`** checks the address, the default route, and that
  leaf1 reaches `192.168.122.1` over `eth2`.

## What the cluster half does

`roles/setup-udn-bgp` repoints the fabric addressing at the lab network for
the run - the same mechanism a rack segment uses - so the `FRRConfiguration`
neighbour, the session checks and the stale-nexthop repair all follow without
their own branches. It skips what only exists for a second NIC: the MAC
check, the `fabric-untagged-*` NNCPs, and the forwarding and `rp_filter`
repair and checks. It refuses `default`, `shared` and `vrflite`.

A cluster on a rack segment (`fabric_segment`, sno2) is unaffected: it keeps
its own NIC on its own bridge behind its own leaf.

## Check it

```bash
# every node routes by the leaf
oc debug node/worker1 --quiet -- chroot /host ip -4 route show default
# default via 192.168.122.38 dev br-ex ...

# seven sessions on leaf1: six hub nodes and the SNO, all on 192.168.122.x
docker exec clab-udnbgp-leaf1 vtysh -c 'show bgp summary'

# the node learned the VTEP block and leaf2's loopback over br-ex
oc debug node/worker1 --quiet -- chroot /host ip route show 100.64.0.0/24
# 100.64.0.0/24 nhid ... via 192.168.122.38 dev br-ex proto bgp ...

# both clusters' pod MACs behind their own VTEPs
docker exec clab-udnbgp-leaf2 vtysh -c 'show evpn mac vni 400'

scripts/udn-xcluster-curl.sh <hub-kubeconfig> <sno-kubeconfig>
```

## What has and has not been tested

**Run on a live lab with this wiring:** both clusters built and wired in, then
`scripts/udn-web-demo.sh --proxy` and `scripts/udn-xcluster-curl.sh`. Both
were clean: all nine ingress hostnames returned their own tenant's page,
green and purple crossed between the clusters bridged, violet crossed routed,
and no tenant reached another. So the agent-based hub behind the helper's
haproxy, leaf1 as the nodes' gateway, and VXLAN arriving on `br-ex` for a
`vtep0` address all work.

Checked without a lab: every playbook passes a syntax check, every edited
task file parses, and the templates were rendered in both modes. With the
switch at `secondary` the fabric's rendered `frr.conf`, containerlab topology
and bridge script are byte-identical to what they were before this page
existed.

Not yet confirmed on a live lab:

- **The workshop's day 0.** `./build-lab.sh --workshop` on this wiring - the
  `prep` step and the `check-hub` / `check-sno` steps in particular.
- **Three expected outputs in
  [workshop-primary-uplink.md](workshop-primary-uplink.md)** that were
  written from the design rather than read off a lab: X5 (each VXLAN packet
  seen twice on `virbr0`), X6 (no ICMP redirect from leaf1, so no `cache`
  entry on the node) and X9 (six hub rows back at `NoNeg`).
- **Live migration (Lab 12)** on this wiring.
- **MTU.** A pod's 1400 bytes plus 50 of VXLAN fits in 1500. An external
  endpoint behind leaf2 sits on a 9000-byte link and can send a datagram that
  does not fit; TCP is clamped by the pod's MSS and is unaffected.
- **The uplink bridge after a reboot of the containerlab VM.**
