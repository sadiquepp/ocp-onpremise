# UDN over EVPN on a real fabric - the OpenShift side

Everything else in this directory builds a fabric out of three FRR containers
so there is something to peer with. This document assumes the opposite: the
fabric is real, the network team has already configured it for **blue, red,
orange, green and purple**, and the only question left is what to do on the
cluster.

It is written from the same role this repo automates
(`roles/setup-udn-bgp`), with the lab-specific scaffolding removed and the
places where a real fabric legitimately differs called out. Where a choice
only makes sense on a simulated fabric, it says so and gives the real answer
instead.

> **Scope, stated honestly.** This repo has only ever been run against the
> containerlab fabric. The cluster-side objects below are exactly what it
> applies and are exercised on every run; the *differences* flagged for real
> hardware are reasoned from the APIs and from the lab's own constraints, not
> from a production deployment. Items that need validating on real kit are
> marked **[verify on site]**.

> **`ClusterUserDefinedNetwork`, never `cudn`.** The short name does not work
> reliably against this API. Every `oc` line below spells it out.

---

## Contents

- [1. Who does what](#1-who-does-what)
- [2. The contract with the network team](#2-the-contract-with-the-network-team)
  - [2.1 Per tenant](#21-per-tenant)
  - [2.2 Per fabric](#22-per-fabric)
  - [2.3 Two things people forget](#23-two-things-people-forget)
- [3. Cluster prerequisites](#3-cluster-prerequisites)
- [4. Cluster network configuration](#4-cluster-network-configuration)
- [5. The node's fabric interface](#5-the-nodes-fabric-interface)
  - [5.1 If the uplink already exists](#51-if-the-uplink-already-exists)
  - [5.2 If you are adding one](#52-if-you-are-adding-one)
  - [5.3 IP forwarding](#53-ip-forwarding)
- [6. The VTEP](#6-the-vtep)
- [7. BGP peering](#7-bgp-peering)
- [8. The five networks](#8-the-five-networks)
  - [8.1 Layer3 tenants: blue, red, orange](#81-layer3-tenants-blue-red-orange)
  - [8.2 Layer2 tenants: green and purple](#82-layer2-tenants-green-and-purple)
  - [8.3 Namespaces](#83-namespaces)
- [9. Advertise them](#9-advertise-them)
- [10. Verify, in this order](#10-verify-in-this-order)
- [11. The failures that look like success](#11-the-failures-that-look-like-success)
- [12. What from this repo still applies](#12-what-from-this-repo-still-applies)
- [13. Test case: a compact cluster and an SNO on one fabric](#13-test-case-a-compact-cluster-and-an-sno-on-one-fabric)
  - [13.1 The topology](#131-the-topology)
  - [13.2 What changes per cluster](#132-what-changes-per-cluster)
  - [13.3 Splitting green between the two clusters](#133-splitting-green-between-the-two-clusters)
  - [13.4 Test workloads and shell helpers](#134-test-workloads-and-shell-helpers)
  - [13.5 Test 1: live migration on the compact cluster, watched from the SNO](#135-test-1-live-migration-on-the-compact-cluster-watched-from-the-sno)
  - [13.6 Test 2: one Layer2 network across both clusters](#136-test-2-one-layer2-network-across-both-clusters)
  - [13.7 Pass criteria](#137-pass-criteria)

---

## 1. Who does what

| | Fabric (network team) | Cluster (you) |
|---|---|---|
| **Underlay** | Reachability between every node and its leaf, and a route to **each node's VTEP address** from every leaf | An address on each node's fabric interface, and that interface forwarding |
| **BGP** | A session per node, right ASN, both address families activated | `FRRConfiguration` naming the leaf; the `RouteAdvertisements` that makes OVN-Kubernetes activate EVPN on it |
| **VTEP** | A route to each node's VTEP `/32` - a route to the block alone cannot pick the node; VXLAN decap | The `VTEP` CR in `Unmanaged` mode, and a `/32` per node inside its CIDRs |
| **Tenants** | VNI per tenant, route targets, VRF or bridge domain per tenant, external gateway for Layer3 | `ClusterUserDefinedNetwork` per tenant carrying the same VNI and RT |
| **Advertising** | Import what the cluster sends, export what the cluster needs | `RouteAdvertisements` selecting the networks and the FRR config |

The dividing line is the BGP session. Everything above it the fabric decides
and the cluster must match; everything below it the cluster decides and the
fabric must accept. **A VNI or a route target that disagrees across that line
produces a tunnel that forms and carries nothing**, with both ends reporting
health.

---

## 2. The contract with the network team

Agree these before applying anything. The table is the deliverable - fill in
the right-hand columns with the network team and keep it, because every value
in it appears in a manifest below and a mismatch is silent.

### 2.1 Per tenant

The repo's own values for the five tenants, as a starting point:

| Tenant | Topology | EVPN type | VNI | Route target | Subnet | Per-node subnet |
|---|---|---|---|---|---|---|
| blue | Layer3 | `ipVRF` | 101 | `65000:101` | `10.200.0.0/16` | `/24` |
| red | Layer3 | `ipVRF` | 201 | `65000:201` | `10.200.0.0/16` | `/24` |
| orange | Layer3 | `ipVRF` | 301 | `65000:301` | `10.202.0.0/16` | `/24` |
| green | Layer2 | `macVRF` | 400 | `65000:400` | `10.204.0.0/16` | n/a |
| purple | Layer2 | `macVRF` | 500 | `65000:500` | `10.204.0.0/16` | n/a |

Three things in that table are load-bearing and worth saying out loud to the
network team:

**blue and red deliberately share `10.200.0.0/16`.** That is the point of
per-tenant VRFs - two tenants carrying the same addresses in isolation. The
fabric must therefore hold them in **separate VRFs with separate route
targets**; a fabric that leaks both into one table gets two paths for one
prefix and non-deterministic forwarding. orange overlaps with nothing and is
the control: a route to `10.202.0.0/16` means exactly one thing.

**green and purple share `10.204.0.0/16` as well**, but they are Layer2, so
the fabric holds them as two **bridge domains**, not VRFs. Same addresses,
different VNIs, no routing between them.

**Layer2 means the tenant's default gateway is not in the cluster.** A
`macVRF` tenant is a stretched broadcast domain; anything that routes out of
it sits on the fabric, inside the tenant's subnet. The network team owns that
address and you must exclude it from cluster IPAM - see
[2.3](#23-two-things-people-forget).

### 2.2 Per fabric

| Item | Repo's value | Yours | Notes |
|---|---|---|---|
| Cluster ASN | 64512 | | One per cluster. Two clusters sharing an ASN is a silent failure. |
| Leaf ASN | 64513 | | Whatever the leaf the nodes peer with uses. |
| RT administrator field | 65000 | | The `<x>:<vni>` left-hand side. Must be identical on both sides. |
| Node fabric subnet | `192.168.140.0/24` | | The nodes' addresses on the leaf. |
| VTEP block | `100.64.0.0/24` | | Must not collide with anything else. See [6](#6-the-vtep). |
| Underlay MTU | 9000 | | VXLAN adds 50 bytes to every frame. Must exceed pod MTU + 50 end to end. |
| BGP timers | hold 9s / keepalive 3s | | Aggressive; a real fabric usually wants its own. |
| TCP-MD5 | optional, off by default | | If on, **both ends or neither** - see [11](#11-the-failures-that-look-like-success). |

### 2.3 Two things people forget

**The Layer2 external endpoint must be excluded from cluster IPAM.** green's
and purple's gateways live inside `10.204.0.0/16` on the fabric side of the
tunnel. OVN-Kubernetes knows nothing about them and will happily hand the
same address to a pod - two machines on one address in one broadcast domain.
That is a duplicate-address bug, not the deliberate overlap this design is
about. Get the addresses from the network team and put them in the CUDN's
reserved-subnets field.

**Two clusters on one stretched Layer2 network need the prefix split.** Each
cluster's IPAM allocates from the subnet knowing nothing about the other, so
both hand out the low addresses and two pods end up on one address. There is
no cross-cluster IPAM to lean on. Carve the prefix per cluster and express it
as reserved subnets on each - cluster A reserves B's half and vice versa.
Skip this entirely if only one cluster is on the VNI.

> **The reserved-subnets field is named differently across releases.** It is
> `excludeSubnets` on some topologies and something else on `layer2`, and
> getting it wrong is silent: Kubernetes prunes a field the CRD does not
> declare with no error, no warning and no event, so your manifest and the
> live object disagree and nothing says so. Ask the API which name it wants
> before writing it:
>
> ```bash
> oc explain clusteruserdefinednetwork.spec.network.layer2 | grep -iE 'exclude|reserved'
> ```
>
> Then read the applied object back and confirm the field survived.

---

## 3. Cluster prerequisites

```bash
# 4.19+ for RouteAdvertisements; EVPN transport needs 4.21+ (VTEP CRD) and is
# most usable on 4.22.
oc get clusterversion version -o jsonpath='{.status.desired.version}{"\n"}'

# The NMState operator, if you will be configuring node interfaces from the
# cluster. Not needed if the uplink is already in place - see 5.1.
oc get csv -n openshift-nmstate | grep nmstate
oc get nmstate cluster
```

OVN-Kubernetes must be the network plugin. This does not work on
OpenShiftSDN, and it does not work with a third-party CNI.

---

## 4. Cluster network configuration

Four settings, and they are best applied as **one patch** so the cluster pays
for one ovnkube-node rollout rather than four:

```bash
oc patch network.operator.openshift.io cluster --type=merge -p '{
  "spec": {
    "additionalRoutingCapabilities": { "providers": ["FRR"] },
    "defaultNetwork": {
      "ovnKubernetesConfig": {
        "routeAdvertisements": "Enabled",
        "gatewayConfig": {
          "routingViaHost": true,
          "ipForwarding": "Global"
        }
      }
    }
  }
}'
```

What each one is for:

| Setting | Why |
|---|---|
| `additionalRoutingCapabilities: [FRR]` | Deploys frr-k8s, which is what actually holds the BGP sessions. `routeAdvertisements` cannot be enabled without it, so this has to be in the same patch. |
| `routeAdvertisements: Enabled` | Installs the `RouteAdvertisements` CRD and the controller that turns a CR into generated FRR config. |
| `routingViaHost: true` | **Local gateway mode. VRF-Lite and EVPN are only implemented in it.** In shared gateway mode the CRs are accepted and quietly do nothing. |
| `ipForwarding: Global` | Under the default `Restricted`, ovnkube-node holds `net.ipv4.ip_forward` at 0 and forwards only across `br-ex` and `ovn-k8s-mpN`. Your fabric interface is neither, so replies from the fabric are dropped in the routing decision with every control-plane signal green. |

Then wait for it properly - `Progressing` has to go **True before False**, or
you are not waiting at all:

```bash
oc wait clusteroperator/network --for=condition=Progressing=True  --timeout=60s || true
oc wait clusteroperator/network --for=condition=Progressing=False --timeout=900s
oc wait clusteroperator/network --for=condition=Available=True    --timeout=900s
```

Confirm the CRDs arrived:

```bash
oc get crd routeadvertisements.k8s.ovn.org vteps.k8s.ovn.org
```

And confirm frr-k8s is actually **ready on every node**, not merely that the
DaemonSet exists:

```bash
oc get daemonset frr-k8s -n openshift-frr-k8s \
  -o jsonpath='{.status.desiredNumberScheduled}/{.status.numberReady}{"\n"}'
```

A node without an frr-k8s pod has nothing listening on TCP/179. The leaf sits
in `Connect` forever, nothing logs, and that node's pods are unreachable over
the fabric while the cluster stays green.

---

## 5. The node's fabric interface

### 5.1 If the uplink already exists

On most real deployments the nodes are already cabled to the fabric and
addressed - a bond, a VLAN sub-interface, or just `br-ex` itself. Then there
is **nothing to create**. Check what you have:

```bash
oc get nodes -o name | while read -r n; do
  echo "== $n"; oc debug "$n" --quiet -- chroot /host ip -br addr show 2>/dev/null
done
```

You need, per node: an address the leaf can reach, and forwarding on that
interface ([5.3](#53-ip-forwarding)). Skip to [6](#6-the-vtep).

### 5.2 If you are adding one

A second NIC dedicated to the fabric is the conservative choice on a live
cluster, because nothing routes out of it until BGP puts something there. One
`NodeNetworkConfigurationPolicy` per node:

```yaml
apiVersion: nmstate.io/v1
kind: NodeNetworkConfigurationPolicy
metadata:
  name: fabric-worker1
spec:
  nodeSelector:
    kubernetes.io/hostname: worker1
  capture:
    # Match on MAC, not on name: interface names depend on PCI enumeration.
    fabric-nic: interfaces.mac-address=="52:54:00:E2:55:22"
  desiredState:
    interfaces:
      - name: "{{ capture.fabric-nic.interfaces.0.name }}"
        type: ethernet
        state: up
        mtu: 9000
        ipv4:
          enabled: true
          dhcp: false
          address:
            - ip: 192.168.140.34
              prefix-length: 24
        ipv6:
          enabled: false
```

Note what it does **not** do: no gateway, no DNS, no default route, no routes
at all. The node keeps reaching everything it currently reaches by its
existing path. That is the property that makes this safe to apply to a
working cluster, and it is worth preserving.

```bash
oc get nncp
oc wait nncp/fabric-worker1 --for=condition=Available --timeout=300s
```

### 5.3 IP forwarding

Traffic from the fabric to a pod arrives on this interface and has to be
forwarded to `ovn-k8s-mpN`. With forwarding off the kernel drops it in the
routing decision, leaving **nothing on the wire** - the reply is visible on
the NIC with `tcpdump` and never appears on `mp0` - while every control-plane
signal stays green. The one direct piece of evidence:

```bash
ip route get <pod ip> from <fabric subnet gateway> iif <nic>
RTNETLINK answers: No route to host
```

`EHOSTUNREACH`, which the kernel substitutes only when forwarding is off on
the incoming interface. A missing route gives `ENETUNREACH`, "Network is
unreachable" - different message, different problem.

`ipForwarding: Global` in [section 4](#4-cluster-network-configuration) is
what fixes this properly, because OVN-Kubernetes then sets
`net.ipv4.ip_forward=1` itself and stops reverting it. Check it landed:

```bash
for n in $(oc get nodes -o name); do
  echo -n "$n: "
  oc debug "$n" --quiet -- chroot /host sysctl -n net.ipv4.ip_forward
done
```

> **Do not solve this with a Tuned profile alone.** The Node Tuning Operator
> runs tuned in `no_daemon` mode - it applies the profile once and exits, so
> nothing re-asserts the value afterwards. Its `Profile` CR still reports
> `Applied=True`, which is a receipt for one past write, not a statement
> about the node now. Anything that writes `ip_forward` later wins
> permanently. This repo learned that the hard way, with three identically
> configured workers reading 1, 0 and 0.

---

## 6. The VTEP

The VTEP CR tells OVN-Kubernetes which addresses are tunnel endpoints. The
fabric uses them as the next hop for every EVPN route, so **every leaf must be
able to reach each node's own VTEP address** - see "How the fabric reaches it"
below for why a route to the block is not enough.

**Use `Unmanaged`, and set it explicitly.** The other mode, `Managed` - where
OVN-Kubernetes would allocate an address per node itself - is **not
implemented** as of OpenShift 4.22 and 4.23. A `Managed` VTEP comes up
`Accepted=False`, `reason: ManagedModeNotSupported`, and ovnkube-node deletes
its EVPN devices and clears the node annotation. And `mode` **defaults to
`Managed`**, so a VTEP CR written without a `mode:` line is rejected the same
way.

| What | Source, `openshift/ovn-kubernetes` |
|---|---|
| cluster-manager rejects `Managed` | [`release-4.22` `clustermanager/vtep/controller.go` L169, L457](https://github.com/openshift/ovn-kubernetes/blob/e2082ef4a1aaad8fa5acc7b24880394b60a4e8ae/go-controller/pkg/clustermanager/vtep/controller.go#L169) - same lines on [`release-4.23`](https://github.com/openshift/ovn-kubernetes/blob/69562fb7062b12b271c3b7393680594fbfe0c9f7/go-controller/pkg/clustermanager/vtep/controller.go#L169) |
| ovnkube-node tears it down | [`release-4.22` `node/controllers/evpn/evpn_node_controller.go` L351](https://github.com/openshift/ovn-kubernetes/blob/e2082ef4a1aaad8fa5acc7b24880394b60a4e8ae/go-controller/pkg/node/controllers/evpn/evpn_node_controller.go#L351) |
| `mode` defaults to it | [`release-4.22` `crd/vtep/v1/types.go` L81](https://github.com/openshift/ovn-kubernetes/blob/e2082ef4a1aaad8fa5acc7b24880394b60a4e8ae/go-controller/pkg/crd/vtep/v1/types.go#L81) - `+kubebuilder:default=Managed` |

Checked at `release-4.22` synced from upstream 2026-08-18; upstream `main`
said the same. To re-check a newer release:

```bash
git clone -q --depth 1 --branch release-4.24 --filter=blob:none --sparse \
    https://github.com/openshift/ovn-kubernetes ovnk && cd ovnk
git sparse-checkout set go-controller/pkg/clustermanager/vtep
grep -n "not yet implemented" go-controller/pkg/clustermanager/vtep/controller.go \
  || echo "the rejection is gone - read the controller before relying on Managed"
```

On a live cluster the Reason column answers it: `oc get vtep -o wide`.

`Unmanaged` means **you** put the address on each node and OVN-Kubernetes
discovers it; it never creates one. One `NodeNetworkConfigurationPolicy` per
node, **masters included** (constraint 4 below):

```yaml
apiVersion: nmstate.io/v1
kind: NodeNetworkConfigurationPolicy
metadata:
  name: vtep-<node>
spec:
  nodeSelector:
    kubernetes.io/hostname: <node>
  desiredState:
    interfaces:
      - name: vtep0
        type: dummy
        state: up
        ipv4:
          enabled: true
          dhcp: false
          address:
            - ip: 100.64.0.<n>        # unique per node
              prefix-length: 32
        ipv6:
          enabled: false
```

Then the CR - addresses first, CR second. A CR created against nodes that do
not have their address yet goes straight to a failed status.

```yaml
apiVersion: k8s.ovn.org/v1
kind: VTEP
metadata:
  name: evpn-vtep
spec:
  cidrs:
    - 100.64.0.0/24
  mode: Unmanaged        # never omit: the default is the unimplemented Managed
```

**Why a dummy, and why `/32`.** The VTEP address is the node's identity in
the overlay, not an address of any one link - the same shape as a router
loopback, so it stays up whatever happens to an interface, and works on a
master with no fabric NIC. It has to be a `/32`: a `/24` on the dummy would
install a connected route claiming the whole block is on-link via `vtep0`,
and every outbound tunnel to another VTEP would be routed into a dummy
interface and vanish. What OVN-Kubernetes builds on top is a flow-based VXLAN
device whose `local` address is this one; the dummy only holds the address.

**How the fabric reaches it.** A dummy is not attached to any link, so a leaf
reaches `100.64.0.<n>` by routing, with the node's fabric-interface address as
the next hop; Linux accepts a packet for any local address on whichever
interface it arrives. The FRR configuration in [7](#7-bgp-peering) advertises
nothing from the nodes, so the per-node `/32`s reach the leaf some other way:
static routes (what the lab's `leaf1` does), the IGP, or a per-node
advertisement of the node's own `/32` - never the `/24`, which every node
would claim identically and which shadows the leaf's route on the node
itself (see [7](#7-bgp-peering)). The leaf originates the `/24` back to the
nodes. **[verify on site]**

Four constraints worth knowing before you pick the block:

1. **It must exist nowhere else.** Not the node subnet, not any tenant
   prefix, not the cluster network. A VTEP address that collides with a real
   one produces tunnels that form and then deliver to the wrong place.
2. **Exactly one address per node may fall inside the CIDRs.** If a node has
   two, the CR goes to a failed status rather than picking one. Before
   widening the block, check nothing already on the nodes is inside it.
3. **In `Unmanaged` mode `cidrs` can be edited freely** - added, removed,
   reordered, resized - though changing a CIDR that is in use disrupts
   traffic. The append-only rule you may read about belongs to `Managed`; the
   CRD's CEL validation enforces it even though the mode itself does nothing
   yet.
4. **Every node needs an address, including masters**, even though no tenant
   pod runs on one. A single unannotated node fails the whole CR with
   `reason: AllocationFailed`.

Check it took:

```bash
oc get vtep evpn-vtep -o yaml | tail -20
oc get nodes -o custom-columns='NODE:.metadata.name,VTEP:.metadata.annotations.k8s\.ovn\.org/vteps'
```

The annotation key is `k8s.ovn.org/vteps`. (Not `node-vtep-ips`, which reads
as empty on every node and looks exactly like a failure.)

---

## 7. BGP peering

One `FRRConfiguration` for the session, carrying **both** address families:

```yaml
apiVersion: frrk8s.metallb.io/v1beta1
kind: FRRConfiguration
metadata:
  name: fabric-peering-evpn
  namespace: openshift-frr-k8s
  labels:
    routeAdvertisements: fabric-evpn
spec:
  bgp:
    routers:
      - asn: 64512                      # this cluster
        neighbors:
          - address: 192.168.140.1      # the leaf
            asn: 64513
            holdTime: 9s
            keepaliveTime: 3s
            port: 179
            toReceive:
              allowed:
                mode: all
            toAdvertise:
              allowed:
                mode: filtered          # nothing - see "Advertise nothing" below
  nodeSelector: {}
```

Why each part:

- **No address family is named, and none can be.** The `FRRConfiguration`
  neighbor has no such field (`oc explain
  frrconfiguration.spec.bgp.routers.neighbors`: `dualStackAddressFamily` is
  IPv4 + IPv6 unicast). OVN-Kubernetes activates `l2vpn evpn` on this
  neighbor itself, in the `ovnk-generated-*` configuration it writes once a
  `RouteAdvertisements` selects an EVPN network and names this CR by its
  label: `activate`, `allowas-in origin` and `advertise-all-vni` under
  `address-family l2vpn evpn`, plus a `vni` block per MAC-VRF and a VRF per
  IP-VRF. Until then the leaf sees the session up for unicast and `NoNeg` for
  EVPN - expected, not a fault. Do not add `addressFamilies` or `allowAsIn`:
  the API server prunes unknown fields silently, and the lab carried both for
  a while without either doing anything.
- **The unicast session is the underlay.** It is how a node learns a route to
  the *other* VTEPs (`toReceive`). Without one the EVPN session comes up,
  routes appear on both sides, and no tunnel ever forms because neither end
  can reach the other's endpoint. That failure looks exactly like a
  data-plane problem and is not one.
- **Advertise nothing - above all not the VTEP block.** No `prefixes:` under
  the router and no VTEP prefix in `toAdvertise`. A node that originates
  `100.64.0.0/24` ranks its own path above the leaf's on weight (32768
  against 0), and BGP never installs a route it originates, so the leaf's
  route is never installed either: `show ip route 100.64.0.0/24` in the
  node's frr container says `Network not in table`, while every session is
  Established. The node only owns one `/32` in the block anyway; the leaf
  learns the `/32`s some other way (section 6). The lab carried exactly this
  mistake for a while.
- **AS-path loops are handled for you.** Every node of a cluster shares its
  ASN, so a route one node originates comes back to another with that ASN
  already in its path. OVN-Kubernetes' generated configuration adds
  `neighbor <leaf> allowas-in origin` to every address family on every
  neighbor it touches (`rawconfig.go` in the route-advertisements
  controller), so there is nothing to set here.
- **`nodeSelector: {}`** applies it to every node. Every node that peers needs
  it; a node the CR does not select simply never peers, and nothing says so.
- **Do not hand-write VNIs, route distinguishers or route targets here.**
  OVN-Kubernetes generates a second, additive `FRRConfiguration` from each
  CUDN's `evpn` block and the `RouteAdvertisements` CR. Two sources for one
  VNI is how you get a tunnel that forms and carries nothing.

If the fabric requires TCP-MD5, create the secret first - a neighbor whose
`passwordSecret` names a Secret that does not exist does not come up:

```bash
oc create secret generic fabric-key -n openshift-frr-k8s \
  --type=kubernetes.io/basic-auth --from-literal=password='<key>'
```

```yaml
            passwordSecret:
              name: fabric-key
              namespace: openshift-frr-k8s
```

`password` and `passwordSecret` are mutually exclusive; the Secret must be
`kubernetes.io/basic-auth` with the key `password`, in the frr-k8s namespace.

---

## 8. The five networks

### 8.1 Layer3 tenants: blue, red, orange

```yaml
apiVersion: k8s.ovn.org/v1
kind: ClusterUserDefinedNetwork
metadata:
  name: blue
  labels:
    bgp: "enabled"          # what RouteAdvertisements selects on
spec:
  namespaceSelector:
    matchLabels:
      udn-tenant: blue
  network:
    topology: Layer3
    layer3:
      role: Primary
      subnets:
        - cidr: 10.200.0.0/16
          hostSubnet: 24
    transport: EVPN
    evpn:
      vtep: evpn-vtep
      ipVRF:
        vni: 101
        routeTarget: "65000:101"
```

Repeat for **red** (`10.200.0.0/16`, VNI 201) and **orange**
(`10.202.0.0/16`, VNI 301). blue and red intentionally carry the same subnet;
their isolation comes from the VRF, which is the property worth testing.

`ipVRF` is required for Layer3 and rejected for Layer2. A Layer3 network is a
set of routed prefixes, so what EVPN carries for it is **type-5** routes.

### 8.2 Layer2 tenants: green and purple

```yaml
apiVersion: k8s.ovn.org/v1
kind: ClusterUserDefinedNetwork
metadata:
  name: green
  labels:
    bgp: "enabled"
spec:
  namespaceSelector:
    matchLabels:
      udn-tenant: green
  network:
    topology: Layer2
    layer2:
      role: Primary
      subnets:
        - 10.204.0.0/16
      # The field name comes from the cluster - check it first, see 2.3.
      # Everything the cluster must NOT allocate: the fabric-side gateway,
      # and any other cluster's half of the prefix.
      reservedSubnets:
        - 10.204.255.10/32      # green's gateway, on the fabric
        - 10.204.128.0/17       # the other cluster's half, if there is one
      ipam:
        lifecycle: Persistent
    transport: EVPN
    evpn:
      vtep: evpn-vtep
      macVRF:
        vni: 400
        routeTarget: "65000:400"
```

Repeat for **purple** (VNI 500, its own gateway address).

`macVRF` is required for Layer2 and rejected for Layer3. A Layer2 network is
a broadcast domain, so what EVPN carries is MAC reachability - **type-2**
routes on an L2VNI, preceded by **type-3** (IMET) routes that build the
ingress-replication flood list. Type-3 comes first: it is what lets ARP cross
the fabric, which is what produces the first type-2.

This is also the combination that makes a live migration invisible to
anything outside the cluster - the MAC moves, a type-2 follows it, and the
fabric reprograms.

> `ipam.lifecycle: Persistent` keeps a pod's address across restarts. On a
> stretched Layer2 network that matters more than usual, because the address
> is what the rest of the broadcast domain has in its ARP tables.

### 8.3 Namespaces

The `namespaceSelector` is what attaches a namespace to a network, and it is
matched **at namespace creation**. Create each namespace with its labels
already on it - labelling an existing namespace afterwards does nothing:

```bash
for t in blue red orange green purple; do
  cat <<EOF | oc apply -f -
apiVersion: v1
kind: Namespace
metadata:
  name: $t
  labels:
    udn-tenant: $t
    k8s.ovn.org/primary-user-defined-network: ""
EOF
done
```

`k8s.ovn.org/primary-user-defined-network` is not optional. Without it the
CUDN sits at `NetworkCreated=False` (`NetworkAttachmentDefinitionSyncError`)
while the namespace and its pods come up perfectly happily on the default
cluster network. Only the key's presence is read; the empty value is
deliberate. A namespace that was created without either label has to be
deleted and recreated.

Then confirm each network was realised into a NetworkAttachmentDefinition -
the CUDN being accepted is not the same as it existing:

```bash
oc get ClusterUserDefinedNetwork -o custom-columns=\
'NAME:.metadata.name,TRANSPORT:.spec.network.transport,TOPOLOGY:.spec.network.topology'
oc get net-attach-def -A
```

---

## 9. Advertise them

One CR covers all five, by selecting the label they share:

```yaml
apiVersion: k8s.ovn.org/v1
kind: RouteAdvertisements
metadata:
  name: udn-evpn
spec:
  targetVRF: auto
  networkSelectors:
    - networkSelectionType: ClusterUserDefinedNetworks
      clusterUserDefinedNetworkSelector:
        networkSelector:
          matchLabels:
            bgp: "enabled"
  nodeSelector: {}
  frrConfigurationSelector:
    matchLabels:
      routeAdvertisements: fabric-evpn
  advertisements:
    - PodNetwork
```

- **`targetVRF: auto`** keeps each tenant in its own VRF. Setting it to the
  string `default` leaks every tenant into one table and destroys the
  isolation; leaving it unset does the same.
- **`frrConfigurationSelector` must match exactly one** `FRRConfiguration`.
  Matching more than one is a common cause of `Accepted=False`.

```bash
oc get routeadvertisements -o wide
```

`Accepted` is what to look for, and one that is not accepted says why in
`.status.conditions`. The usual causes: `targetVRF` set to `default`,
overlapping subnets leaked into the default VRF, two CRs selecting the same
network, and a selector matching several FRRConfigurations.

---

## 10. Verify, in this order

The order matters. Each step is meaningless if the one above it failed, and
checking them out of order is how a control-plane problem gets diagnosed as a
data-plane one.

**1. The session exists.** On the leaf, every node should be `Established`.
From the cluster side:

```bash
oc get frrnodestate -o wide
oc get frrnodestate <node> -o jsonpath='{.status.runningConfig}' | grep neighbor
```

`frrnodestate` shows the config actually loaded on each node and whether the
last reload succeeded - which is how you catch a cluster-wide CR that applied
unevenly.

**2. The underlay works.** Each node must have a route to the other VTEPs. If
it does not, everything below will form and carry nothing.

**3. Forwarding is on.** Per [5.3](#53-ip-forwarding). Everything above this
is control plane and stays green while it is broken.

**4. The routes are there.** On the fabric, per tenant VNI: type-5 for blue,
red and orange; type-2 and type-3 for green and purple.

**5. The tunnel carries traffic.** Pod to pod inside one tenant, across
nodes. Then pod to something on the fabric side of the same tenant.

**6. Isolation holds.** blue must not reach red, despite sharing a subnet.
This is the test that says the VRFs are real, and it is the one that fails
when `targetVRF` is wrong.

For 5 and 6 the scripts in `scripts/` work against any fabric - they only
talk to the clusters:

```bash
scripts/udn-reachability.sh          # the matrix, by ping
scripts/udn-vrf-isolation.sh         # isolation, with AMBIG where ping cannot tell
scripts/udn-xcluster-curl.sh <kubeconfig> <kubeconfig>   # pod to pod across clusters
```

`udn-xcluster-curl.sh` is worth singling out: it curls rather than pings
precisely because green and purple can hold the *same address*, and a page
can name its own tenant where ICMP cannot.

---

## 11. The failures that look like success

Every one of these was hit for real building the lab this document comes
from. They share a shape: a green control plane over a broken data path.

| Symptom | Cause | How to tell |
|---|---|---|
| BGP `Established`, routes both sides, no traffic | No route to the remote VTEP - the unicast AF is missing or the underlay does not carry the block | `ip route get <remote vtep>` on the node |
| Replies visible on the fabric NIC with `tcpdump`, never on `mp0` | Forwarding off on that interface | `ip route get <pod> from <gw> iif <nic>` → `No route to host` |
| CRs accepted, nothing happens at all | Shared gateway mode. VRF-Lite and EVPN are only implemented with `routingViaHost: true` | `oc get network.operator cluster -o jsonpath='{...gatewayConfig.routingViaHost}'` |
| Tenants can reach each other despite separate VRFs | `targetVRF` set to `default` or unset | `oc get routeadvertisements -o yaml` |
| Tunnel forms, carries nothing | VNI or route target disagrees across the BGP session | Compare the CUDN's `evpn` block against the fabric's VRF config, by hand |
| One node's pods unreachable, everything green | No frr-k8s pod there, or its session never came up | `oc get daemonset frr-k8s -n openshift-frr-k8s -o wide`, then the leaf's neighbor state |
| Session stuck in `Connect`, 0 messages either way, ping fine | TCP-MD5 on one end only. The signature is in the TCP header, so ICMP is unaffected and the kernel discards before BGP sees anything - silently, at both ends | Compare the leaf's `password` line against `frrnodestate`; both ends or neither |
| A field you applied is simply absent from the live object | The CRD does not declare it. Kubernetes prunes unknown fields with no error, no warning and no event | Read the object back and diff it against what you applied |
| Two pods in different clusters share an address | No prefix split on a stretched Layer2 network | See [2.3](#23-two-things-people-forget) |
| A sysctl you set reverts later | Something wrote the global knob. `ip_forward` and `conf.all.forwarding` are the same value, and a write to it propagates to **every** interface | Report `all=` alongside the per-interface value |

The general lesson, if there is one: **check that a thing is true, not that
you asked for it to be true.** A CR is accepted long before it works, a
DaemonSet existing is not a pod running, and `Applied=True` can be a receipt
for a write that something else has since undone.

---

## 12. What from this repo still applies

| Still useful | Skip |
|---|---|
| `roles/setup-udn-bgp` - everything in [4](#4-cluster-network-configuration) to [9](#9-advertise-them) is what it applies | `roles/setup-clab-fabric` - it builds the simulated fabric |
| `scripts/udn-reachability.sh`, `udn-vrf-isolation.sh`, `udn-xcluster-curl.sh`, `udn-snapshot.sh` - they only talk to clusters | `scripts/udn-web-demo.sh --proxy` - the tenant ingress lives on the lab's client VM |
| The tenant table in `vars.yaml` - subnets, VNIs and route targets in one place | `clab_*` variables, the VLAN numbers (VRF-Lite only), `build-lab.sh` steps 1-3 |
| [bgp-evpn.md](bgp-evpn.md) - the packet walkthroughs are fabric-independent | [clab-fabric.md](clab-fabric.md) - how the simulated fabric is built |
| [README.md](README.md) - the design notes and troubleshooting table | |

To drive a real fabric with this repo's automation, the cluster half runs on
its own:

```bash
ansible-playbook -i ../inventory/hosts setup_udn_bgp_lab.yaml \
  --tags evpn -e udn_bgp_cluster=<cluster>
```

You would need to replace the `clab_*` values with your fabric's. The
per-node VTEP policies stay: `Managed` mode, which would make them
unnecessary, is not implemented - see [6](#6-the-vtep).

---

## 13. Test case: a compact cluster and an SNO on one fabric

A worked test plan for one specific estate: a **three-node compact cluster**
and a **single-node OpenShift (SNO)** cluster, both attached to the same
EVPN fabric. It shows two things:

1. **Live migration** - this repo's live-migration test
   ([bgp-evpn.md](bgp-evpn.md#live-migration-on-the-layer2-tenant),
   [workshop.md Lab 12](workshop.md#lab-12-live-migration-across-a-stretched-layer2)),
   moved onto real hardware. A VM moves between the compact cluster's
   nodes while a pod **in the SNO** pings it.
2. **Cross-cluster Layer2** - one `green` broadcast domain on L2VNI 400,
   spanning both clusters. Pods in the two clusters reach each other bridged,
   with no router between them, while `purple` stays isolated from them.

> **Status: a plan, not a measurement.** The procedure is the one this repo
> runs on its hub + SNO containerlab lab, where both tests pass (see
> [HANDOFF.md](HANDOFF.md) and the measured run in
> [bgp-evpn.md](bgp-evpn.md#measured-a-migration-watched-from-the-other-cluster)).
> This exact topology - a compact cluster rather than a hub with dedicated
> workers, and a real fabric rather than FRR containers - has not been run.
> The outputs below are the **expected shape**, based on the lab's measured
> output. Replace them with yours on the first run. Fabric-side commands are
> FRR syntax and marked **[verify on site]**. Your network operating system
> will have an equivalent for each.

### 13.1 The topology

```
  +----------------------------------- fabric -----------------------------------+
  |  leaf(s), AS 64513: transit for VNI 400 / RT 65000:400 (and 500 for purple)  |
  |  green's external gateway 10.204.255.10, if the network team builds one      |
  +--------+------------------+------------------+--------------------+----------+
           |                  |                  |                    |
     192.168.140.11     192.168.140.12     192.168.140.13       192.168.140.21
        master-0           master-1           master-2               sno
   VTEP 100.64.0.11   VTEP 100.64.0.12   VTEP 100.64.0.13     VTEP 100.64.0.21
   \_______________ compact, AS 64512 _______________/        sno, AS 64515
     green pods from 10.204.0.0/17                         green pods from 10.204.128.0/17
     the VM lives here and migrates                        the observer
```

| | compact | sno |
|---|---|---|
| Nodes | `master-0`, `master-1`, `master-2` - control plane **and** worker | `sno` - control plane and worker |
| Cluster ASN | `64512` | `64515` - **must differ**, see [13.2](#132-what-changes-per-cluster) |
| Fabric address | `192.168.140.11`-`.13` | `192.168.140.21` |
| VTEP `/32` | `100.64.0.11`-`.13` | `100.64.0.21` |
| Tenants | green, purple (blue/red/orange optional) | green, purple |
| green allocates from | `10.204.0.0/17` | `10.204.128.0/17`, minus the fabric's block |
| OpenShift Virtualization | yes - three nodes give the VM somewhere to move | no - a single node has no migration target |
| Role in Test 1 | runs the VM | watches it move |

The addresses are examples in this document's own scheme (section
[2.2](#22-per-fabric)). Substitute yours, but keep the shape. Both clusters
can sit behind the same leaf or different leaves. Only the AS paths in the
expected output change.

**What a compact cluster changes, and what it does not:**

- **Every node is a master.** Constraint 4 in [section 6](#6-the-vtep)
  ("every node needs a VTEP address, including masters") is therefore not an
  edge case here - it is all three nodes. Each gets its own `vtep-<node>`
  policy and its own `/32`, and the leaf needs a route to each `/32`.
- **Masters must be schedulable**, or no tenant pod, test pod or VM lands
  anywhere. A compact install sets this. Check it:

  ```bash
  oc get scheduler cluster -o jsonpath='{.spec.mastersSchedulable}{"\n"}'   # true
  oc get nodes                                                             # ROLES: control-plane,master,worker
  ```

- **The test DaemonSet below selects on `node-role.kubernetes.io/worker`.**
  Compact and SNO nodes both carry that label, so one manifest works on both
  clusters.
- **Live-migration traffic shares the masters with etcd.** By default it uses
  the pod network. On a busy compact cluster, consider a dedicated migration
  network (`spec.liveMigrationConfig.network` on the `HyperConverged` CR).
  This test does not need one: a 1Gi idle VM migrates in seconds.

### 13.2 What changes per cluster

Run sections [3](#3-cluster-prerequisites) to [9](#9-advertise-them) on
**each** cluster on its own. Almost everything is identical. These are the
only differences, and each one is a silent failure if you get it wrong:

| Object | compact | sno | Why it matters |
|---|---|---|---|
| `FRRConfiguration` `spec.bgp.routers[0].asn` | `64512` | `64515` | The leaf passes the compact cluster's routes to the SNO with AS path `64513 64512`. If the SNO were also 64512, it would drop every one of them as a loop. That is what the lab saw: a healthy session, the right routes on the leaf, and an empty table on the far side, with no error anywhere. |
| Leaf neighbour statements | 3 neighbours, `remote-as 64512` | 1 neighbour, `remote-as 64515` | Four sessions in total on the fabric side. |
| `vtep-<node>` NNCPs | 3 policies, `.11`-`.13` | 1 policy, `.21` | Every VTEP address unique across **both** clusters. The leaf needs a `/32` route to all four. |
| `VTEP` CR | `cidrs: [100.64.0.0/24]`, `Unmanaged` | the same | Each cluster's CR only checks its own nodes, so sharing the block is fine. "Exactly one address per node inside the CIDRs" is checked per cluster. |
| green / purple CUDN `reservedSubnets` | the SNO's half | the compact cluster's half | [13.3](#133-splitting-green-between-the-two-clusters). |
| green / purple CUDN `evpn.macVRF` | `vni: 400` / `500`, `routeTarget: "65000:400"` / `"65000:500"` | **identical** | This is what makes it one network. **Set `routeTarget` explicitly on both.** FRR's automatic route target is `<ASN>:<VNI>`, so two clusters with different ASNs would export `64512:400` and `64515:400` and never import each other's routes. |

On the fabric side, ask the network team to confirm four things for VNI 400
and 500 specifically:

1. **Transit leaves keep the routes.** A leaf or spine with no local VNI 400
   discards every EVPN route whose route target it does not import - unless
   it is told to keep them (FRR: `bgp retain route-target all` under
   `address-family l2vpn evpn`, which is what this lab's leaf1 and spine do).
   This is the most common cause of healthy sessions and an empty table on
   the far side. **[verify on site]**
2. **Next hops arrive unchanged.** The compact cluster's type-2 and type-3
   routes must reach the SNO with the **originating node's VTEP** as next hop
   (`100.64.0.1x`), never a leaf or spine address. Tunnels go node to node;
   a rewritten next hop pulls every frame through a box that has no VNI 400
   to decapsulate it into. In the lab the spine needs `set ip next-hop
   unchanged` for this. **[verify on site]**
3. **Every VTEP `/32` is routed** - all three masters, not just the one the
   VM starts on. Test 1 moves the VM onto every master in turn, and that
   is how a missing `/32` shows up.
4. **Duplicate-address detection will see the gateway MAC on more than one
   VTEP.** See [13.6, check 8](#check-8-the-one-thing-that-is-shared-and-should-not-be).
   It is expected, and the team should know before it pages anyone.

### 13.3 Splitting green between the two clusters

[Section 2.3](#23-two-things-people-forget) explains why the split is needed:
each cluster's IPAM allocates from `10.204.0.0/16` without knowing about the
other. This is the split for this topology.

**Give the multi-node cluster the low half.** OVN-Kubernetes needs addresses
at the bottom of the subnet in **every** cluster: `.1` is the Layer2 switch's
gateway, and each node takes a management-port address after it (`.2`, and
on a three-node cluster `.3` and `.4`). Reserving any of them is
**fatal**, not rejected:

```
failed to exclude subnet 10.204.0.2/32 for cluster_udn_green_ovn_layer2_switch:
  failed to reserve IP 10.204.0.2: provided IP is already allocated
F failed to run ovnkube
```

ovnkube-node goes into CrashLoopBackOff, and **all** pod networking on that
node goes with it, not just green. Put the compact cluster in the low half
and its infrastructure addresses are inside its own half, with nothing to
carve out. The SNO goes in the high half and leaves `.1` and `.2` free -
exactly one node's worth, which is what the lab's SNO runs on.

compact - reserve the SNO's half. The fabric's block `10.204.255.0/24` is
inside it, so the gateway is covered too:

```yaml
      reservedSubnets:
        - 10.204.128.0/17       # the SNO's half, including the fabric's 10.204.255.0/24
```

sno - reserve the compact cluster's half **except `.1` and `.2`**, then the
fabric's block a second time, because it is inside this cluster's half:

```yaml
      reservedSubnets:
        - 10.204.0.3/32         # .0-.2 stay free: .1 gateway, .2 this node's management port
        - 10.204.0.4/30
        - 10.204.0.8/29
        - 10.204.0.16/28
        - 10.204.0.32/27
        - 10.204.0.64/26
        - 10.204.0.128/25
        - 10.204.1.0/24
        - 10.204.2.0/23
        - 10.204.4.0/22
        - 10.204.8.0/21
        - 10.204.16.0/20
        - 10.204.32.0/19
        - 10.204.64.0/18
        - 10.204.255.0/24       # the fabric's block on green: gateway .10, any test hosts
```

That list is the lab's own (`evpn_l2_excludes.sno` in `vars.yaml`), and it
is the right size for a reason:

- **Too small, and the node crashes**, as above.
- **Too big, and the split does nothing.** IPAM hands out the lowest free
  address, so any address left unreserved in the low half is where the SNO's
  first pods go - inside the compact cluster's range. A 16-address hole put
  the lab SNO's pods on `.3` and `.5`.

Do the same for purple with its own fabric block. Two more rules:

- **Decide the split before creating the CUDN.** Changing the network spec
  of a CUDN that is in use means deleting and recreating it, and every pod
  on it.
- **Read the field back after applying it.** The field name differs between
  topologies, and Kubernetes silently drops a field the CRD does not declare
  (see the box in [2.3](#23-two-things-people-forget)):

  ```bash
  oc get ClusterUserDefinedNetwork green -o jsonpath='{.spec.network.layer2.reservedSubnets}{"\n"}'
  ```

  Empty output means the split is not there, and both clusters will
  allocate the same low addresses.

### 13.4 Test workloads and shell helpers

Both tests run from one shell holding both kubeconfigs:

```bash
export COMPACT_KUBECONFIG=/path/to/compact/kubeconfig
export SNO_KUBECONFIG=/path/to/sno/kubeconfig
oc_c() { oc --kubeconfig "$COMPACT_KUBECONFIG" "$@"; }
oc_s() { oc --kubeconfig "$SNO_KUBECONFIG" "$@"; }
FRR_NS=openshift-frr-k8s

# The tenant address of a pod: ovn-udn1, NOT status.podIP. For a primary UDN,
# podIP is the cluster-network address on eth0 and has nothing to do with
# the fabric.
udnip() { "$1" -n "$2" exec "$3" -- ip -o -4 addr show ovn-udn1 | awk '{split($4,a,"/"); print a[1]}'; }

SNO_NODE=$(oc_s get nodes -o jsonpath='{.items[0].metadata.name}')
```

Two workloads per tenant per cluster, cut down from the role's
`templates/workload.yaml.j2` and `templates/webload.yaml.j2`:

- **`udn-test`**, a DaemonSet with ping, curl, `ip` and tcpdump. Every node
  gets one, so every compact master has a pod to test from.
- **`udn-web`**, a web page that says **which tenant and which cluster**
  answered. This matters because green and purple can hold the same address,
  and a ping reply cannot say which of them answered.

```bash
cat > udn-test-workload.yaml <<'EOF'
apiVersion: v1
kind: ServiceAccount
metadata: {name: udn-test, namespace: @TENANT@}
---
# NET_RAW/NET_ADMIN need the privileged SCC. Pod Security labels alone are not
# enough: without this binding the DaemonSet creates no pods at all.
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata: {name: udn-test-privileged, namespace: @TENANT@}
roleRef: {apiGroup: rbac.authorization.k8s.io, kind: ClusterRole, name: "system:openshift:scc:privileged"}
subjects: [{kind: ServiceAccount, name: udn-test, namespace: @TENANT@}]
---
apiVersion: apps/v1
kind: DaemonSet
metadata: {name: udn-test, namespace: @TENANT@}
spec:
  selector: {matchLabels: {app: udn-test}}
  template:
    metadata: {labels: {app: udn-test}}
    spec:
      serviceAccountName: udn-test
      nodeSelector: {node-role.kubernetes.io/worker: ""}   # compact masters and the SNO both match
      tolerations: [{operator: Exists, effect: NoSchedule}]
      containers:
        - name: shell
          image: registry.redhat.io/rhel9/support-tools:latest
          command: ["/bin/bash", "-c", "sleep infinity"]
          securityContext: {capabilities: {add: ["NET_RAW", "NET_ADMIN"]}}
---
apiVersion: apps/v1
kind: Deployment
metadata: {name: udn-web, namespace: @TENANT@, labels: {app: udn-web}}
spec:
  replicas: 1
  selector: {matchLabels: {app: udn-web}}
  template:
    metadata: {labels: {app: udn-web}}
    spec:
      initContainers:
        - name: render-page
          image: registry.redhat.io/rhel9/support-tools:latest
          env: [{name: NODE_NAME, valueFrom: {fieldRef: {fieldPath: spec.nodeName}}}]
          command:
            - /bin/bash
            - -c
            - |
              udn=$(ip -o -4 addr show ovn-udn1 | awk '{print $4}')
              { echo "I am @TENANT@ on @CLUSTER@"; echo
                echo "tenant:   @TENANT@"; echo "cluster:  @CLUSTER@"
                echo "node:     $NODE_NAME"; echo "udn:      $udn"; } > /page/index.html
          volumeMounts: [{name: page, mountPath: /page}]
      containers:
        - name: httpd
          image: registry.access.redhat.com/ubi9/httpd-24:latest   # serves on 8080
          volumeMounts: [{name: page, mountPath: /var/www/html}]
      volumes: [{name: page, emptyDir: {}}]
EOF

for t in green purple; do
  oc_c label ns "$t" pod-security.kubernetes.io/enforce=privileged --overwrite
  oc_s label ns "$t" pod-security.kubernetes.io/enforce=privileged --overwrite
  sed "s/@TENANT@/$t/g; s/@CLUSTER@/compact/g" udn-test-workload.yaml | oc_c apply -f -
  sed "s/@TENANT@/$t/g; s/@CLUSTER@/sno/g"     udn-test-workload.yaml | oc_s apply -f -
done
oc_c -n green get pods -o wide; oc_s -n green get pods -o wide
```

Expect three `udn-test` pods on the compact cluster (one per master) and one
on the SNO, plus one `udn-web` in each. `scripts/udn-xcluster-curl.sh` looks
for exactly these labels and this page format, so Test 2 can use it
unchanged.

---

### 13.5 Test 1: live migration on the compact cluster, watched from the SNO

**What it proves.** A VM on green moves from one compact master to another,
and keeps its IP and MAC. A pod in a **different cluster** keeps pinging it
throughout. When the VM moves, the SNO starts tunnelling to the new
master's VTEP, because a BGP route told it to. Nothing re-addresses
anything, and nothing outside the compact cluster is reconfigured.

This is why green is Layer2 with `ipam.lifecycle: Persistent`. A migration
replaces the `virt-launcher` pod. On a Layer3 network the new node owns a
different slice of the subnet, so the VM would come back with a different
address. Persistent IPAM ties the address to the VM, not to the pod.

> Test 1's observer is in the SNO, so it relies on the stretch that Test 2
> examines in detail. If the ping in step 4 never gets a single reply, run
> Test 2 checks 1-4 first.

#### Step 1. OpenShift Virtualization on the compact cluster

Install it as in [bgp-evpn.md, "Install OpenShift
Virtualization"](bgp-evpn.md#install-openshift-virtualization): the
namespace, OperatorGroup and Subscription, wait for the CSV, then a
`HyperConverged` with `spec: {}`. Run those blocks with
`KUBECONFIG=$COMPACT_KUBECONFIG`. On a disconnected cluster, point the
Subscription at your mirrored catalog. The SNO does not need it.

Then confirm there is somewhere to migrate **to**:

```bash
oc_c -n openshift-cnv get hyperconverged kubevirt-hyperconverged \
  -o jsonpath='{.status.conditions[?(@.type=="Available")].status}{"\n"}'   # True
oc_c get nodes -l kubevirt.io/schedulable=true                              # all three masters
```

On bare metal, the nested-virtualisation caveat in bgp-evpn.md does not
apply. If a master is missing from that list, check that virtualisation
extensions are enabled in its firmware.

#### Step 2. The VM

The lab's VM, unchanged except for the namespace. It uses green, the
namespace's primary network, through the `l2bridge` binding, so the VM's own
MAC and IP sit on the UDN switch rather than NATed behind the pod's:

```bash
cat <<'EOF' | oc_c apply -f -
apiVersion: kubevirt.io/v1
kind: VirtualMachine
metadata:
  name: green-vm
  namespace: green
spec:
  runStrategy: Always
  template:
    spec:
      domain:
        devices:
          disks:
            - {name: rootdisk, disk: {bus: virtio}}
            - {name: cloudinit, disk: {bus: virtio}}
          interfaces:
            - name: green-net
              binding:
                name: l2bridge          # on the UDN's switch itself - its own MAC and IP
        resources:
          requests:
            memory: 1Gi
      networks:
        - name: green-net
          pod: {}                       # "the namespace's primary network" = green
      volumes:
        - name: rootdisk
          containerDisk:                # immutable and present on every node: migratable
            image: quay.io/containerdisks/fedora:41
        - name: cloudinit
          cloudInitNoCloud:
            userData: |
              #cloud-config
              password: green
              chpasswd: { expire: False }
EOF
oc_c -n green wait vm/green-vm --for=condition=Ready --timeout=10m
```

If `l2bridge` is rejected as an unknown binding, check `spec.network.binding`
on the `HyperConverged`. If you use a PVC rather than a `containerDisk`, it
must be RWX, or the VM is not migratable.

#### Step 3. Before: four views of where the VM is

Save each output. You will run the same commands after the migration and
compare.

**a. The VM** - address, MAC, node:

```bash
oc_c -n green get vmi green-vm -o custom-columns=\
NAME:.metadata.name,IP:.status.interfaces[0].ipAddress,MAC:.status.interfaces[0].mac,NODE:.status.nodeName
VMIP=$(oc_c -n green get vmi green-vm -o jsonpath='{.status.interfaces[0].ipAddress}')
MAC=$(oc_c -n green get vmi green-vm -o jsonpath='{.status.interfaces[0].mac}')
```

```
NAME       IP           MAC                 NODE
green-vm   10.204.0.9   0a:58:0a:cc:00:09   master-0
```

The address is from the compact cluster's half, `10.204.0.0/17`. The MAC is
derived from it: `0a:58` followed by `10.204.0.9` in hex. That is why a VM
that keeps its IP also keeps its MAC.

**b. The SNO's VXLAN forwarding table** - which VTEP the *other cluster*
encapsulates to for that MAC. The device names come from the VTEP CR's name
(`evpn-vtep`):

```bash
oc_s debug node/$SNO_NODE --quiet -- chroot /host \
  bridge fdb show dev evx4-evpn-vtep | grep "$MAC"
```

```
0a:58:0a:cc:00:09 vlan 2 extern_learn master evbr-evpn-vtep
0a:58:0a:cc:00:09 dst 100.64.0.11 src_vni 400 self extern_learn
```

`dst 100.64.0.11` is master-0's VTEP.

**c. The BGP route behind it**, from the SNO's FRR. FRR runs in the
frr-k8s pod, not on the host, so `vtysh` under `oc debug` reports "failed to
connect to any daemons":

```bash
oc_s -n $FRR_NS exec ds/frr-k8s -c frr -- \
  vtysh -c 'show bgp l2vpn evpn route type macip' | grep -A2 "$MAC"
```

```
 *>  [2]:[0]:[48]:[0a:58:0a:cc:00:09]
                    100.64.0.11                            0 64513 64512 i
                    RT:65000:400 ET:8
 *>  [2]:[0]:[48]:[0a:58:0a:cc:00:09]:[32]:[10.204.0.9]
                    100.64.0.11                            0 64513 64512 i
                    RT:65000:400 ET:8
```

Next hop is master-0's own VTEP (the fabric left it unchanged). The AS path
`64513 64512` is the leaf, then the compact cluster. If the clusters are
behind different leaves, the spine's and the other leaf's ASNs appear in
between. `ET:8` is VXLAN. There is no `MM:` yet: the MAC has never moved.

**d. The fabric's view**, if you have access to the leaf. **[verify on site]**

```bash
# FRR: show evpn mac vni 400 | grep 0a:58:0a:cc:00:09
# NX-OS: show l2route evpn mac evi 400 | Arista EOS: show bgp evpn route-type mac-ip 0a58.0acc.0009
```

The MAC behind `100.64.0.11`.

#### Step 4. Migrate while the SNO pings

Window 1 - start a ping **from the SNO**, across clusters, and leave it
running:

```bash
S_POD=$(oc_s -n green get pods -l app=udn-test -o name | head -1)
oc_s -n green exec "$S_POD" -- ping -D -i 0.2 -w 120 "$VMIP"    # 5/s for two minutes
```

`-w 120` stops ping by itself after two minutes, so it prints its summary.
Ctrl-C would kill your local `oc` instead, and the summary would be lost.
`-D` timestamps each reply, so you can see where any gap fell.

If the network team has built green's external gateway, run a second
observer **from the fabric** in another window: from `10.204.255.10`, or any
host in green's bridge domain, `ping -i 0.2 <vm ip>`. That shows that
nothing outside both clusters noticed the move either. **[verify on site]**

Window 2 - within the two minutes, migrate:

```bash
cat <<'EOF' | oc_c apply -f -
apiVersion: kubevirt.io/v1
kind: VirtualMachineInstanceMigration
metadata:
  name: green-vm-migrate-1
  namespace: green
spec:
  vmiName: green-vm
EOF
oc_c -n green get vmim green-vm-migrate-1 -w
# Scheduling, Scheduled, PreparingTarget, TargetReady, Running, Succeeded - ctrl-c
```

If it refuses, the reason is on the VMI:
`oc_c -n green get vmi green-vm -o jsonpath='{.status.conditions}' | python3 -m json.tool`.

#### Step 5. After: the same four views

Re-run 3a-3d. Expected:

```
NAME       IP           MAC                 NODE
green-vm   10.204.0.9   0a:58:0a:cc:00:09   master-1

0a:58:0a:cc:00:09 vlan 2 extern_learn master evbr-evpn-vtep
0a:58:0a:cc:00:09 dst 100.64.0.12 src_vni 400 self extern_learn

 *>  [2]:[0]:[48]:[0a:58:0a:cc:00:09]
                    100.64.0.12                            0 64513 64512 i
                    RT:65000:400 ET:8 MM:1
```

And the ping summary, once the deadline is up. The lab's two measured runs:

```
144 packets transmitted, 144 received, 0% packet loss               <- 1/s
597 packets transmitted, 595 received, 0.335008% packet loss        <- -i 0.2 -w 120
```

| | Before | After | What changed it |
|---|---|---|---|
| VM IP / MAC | `10.204.0.9` / `0a:58:0a:cc:00:09` | **the same** | `ipam.lifecycle: Persistent` kept the IP. The MAC follows from the IP |
| VM node | master-0 | master-1 | the migration |
| SNO's BGP next hop | `100.64.0.11` | `100.64.0.12` | master-1 now advertises the MAC with its own VTEP, and master-0 withdrew its route |
| `MM:` | absent | `MM:1` | **MAC Mobility.** The sequence number goes up on every move, so every speaker prefers the newer location even if the old withdraw arrives late |
| SNO FDB `dst` | `100.64.0.11` | `100.64.0.12` | zebra on the SNO, acting on that route |

`extern_learn` on the FDB entry proves the control plane made the change.
The VXLAN port has learning turned off, so the data plane *cannot* write
that entry. Only zebra, acting on a BGP route, can. And nothing between the
clusters could have redirected the traffic: the fabric only ever sees the
outer packet, addressed to a VTEP `/32`.

**Expect no loss, or a handful of packets - not a stream.** The cutover is
the VM pausing for the last memory copy plus the new route reaching the
SNO. At five pings a second, one or two lost is about 0.4 s. Losses that
continue after `Succeeded` mean the SNO is still sending to the old master.
Compare the FDB `dst` with the VM's node.

#### Step 6. Visit every master

Migrate twice more, `green-vm-migrate-2` and `-3`, so the VM lands on each
master in turn. Keep a ping running for each. One migration only tests the
two VTEPs involved. Visiting all three tests that the leaf routes **every**
compact master's `/32`, which is how a missing route on a real fabric shows
up: the migration reports `Succeeded`, and the cross-cluster ping stops dead
and does not recover.

| Migration | VM node (example) | SNO FDB `dst` | `MM:` |
|---|---|---|---|
| start | master-0 | `100.64.0.11` | absent |
| 1 | master-1 | `100.64.0.12` | 1 |
| 2 | master-2 | `100.64.0.13` | 2 |
| 3 | master-0 | `100.64.0.11` | 3 |

The scheduler picks the target, so your order may differ. If a migration
picks a node the VM has already been on, run another, or add a temporary
`nodeSelector` to the VM to steer it.

Leave about a minute between migrations. Fabric duplicate-address detection
counts MAC moves - FRR's default is 5 moves in 180 s - and three quick
migrations plus the gateway MAC noise from 13.6 check 8 can get close to
the limit. A MAC frozen by DAD stops following the VM. **[verify on site]**
for your NOS's thresholds.

---

### 13.6 Test 2: one Layer2 network across both clusters

**What it proves.** green on the compact cluster and green on the SNO are
**one broadcast domain**. A pod in one cluster reaches a pod in the other
on-link, bridged - same subnet, no router hop, the remote pod's own MAC in
the ARP table. The traffic is carried as VXLAN between node VTEPs, and the
control plane is EVPN type-3 (flood list) and type-2 (MAC) routes crossing
from one AS to the other through the fabric. purple uses the same addresses
on a different VNI and stays completely separate.

Run the checks in order. Each one depends on the one before it.

#### Check 1. Sessions: four peers, both address families

On the leaf: three neighbours in AS 64512 and one in AS 64515, all
`Established` for IPv4 unicast **and** l2vpn evpn. **[verify on site]**
(FRR: `show bgp summary`, `show bgp l2vpn evpn summary`.) From each cluster:

```bash
oc_c get frrnodestate -o wide; oc_s get frrnodestate -o wide
oc_s -n $FRR_NS exec ds/frr-k8s -c frr -- vtysh -c 'show bgp l2vpn evpn summary'
```

`NoNeg` for EVPN on the cluster side means no `RouteAdvertisements` has
selected an EVPN network yet ([section 7](#7-bgp-peering)).

#### Check 2. Underlay: each cluster can reach the other's VTEPs

```bash
oc_s debug node/$SNO_NODE --quiet -- chroot /host \
  sh -c 'for v in 100.64.0.11 100.64.0.12 100.64.0.13; do ip route get $v; done'
oc_c debug node/master-0 --quiet -- chroot /host ip route get 100.64.0.21
```

Each should resolve via the leaf. If this fails, every check below will
"form and carry nothing" ([section 11](#11-the-failures-that-look-like-success), first row).

#### Check 3. Type-3: each cluster is in the other's flood list

Type-3 (IMET) routes are what let ARP cross between the clusters, so they
come before anything else. On the SNO, VNI 400 should list all three compact
VTEPs, originated by AS 64512:

```bash
oc_s -n $FRR_NS exec ds/frr-k8s -c frr -- \
  vtysh -c 'show bgp l2vpn evpn route type multicast' | grep -E '100\.64\.0\.1[123]'
oc_s debug node/$SNO_NODE --quiet -- chroot /host \
  bridge fdb show dev evx4-evpn-vtep | grep 00:00:00:00:00:00
```

```
00:00:00:00:00:00 dst 100.64.0.11 src_vni 400 self permanent
00:00:00:00:00:00 dst 100.64.0.12 src_vni 400 self permanent
00:00:00:00:00:00 dst 100.64.0.13 src_vni 400 self permanent
```

Plus the fabric's own VTEP(s) if the team built a gateway on VNI 400, and
the same again for VNI 500. On a compact master, the mirror image: the
SNO's `100.64.0.21` plus the other two masters.

```bash
oc_c debug node/master-0 --quiet -- chroot /host \
  bridge fdb show dev evx4-evpn-vtep | grep 00:00:00:00:00:00
```

**A cluster missing from the other's flood list is the classic fabric-side
failure**: a transit leaf without `retain route-target all`, or route
targets that differ (auto-derived on one side - see
[13.2](#132-what-changes-per-cluster)). Sessions are healthy and the table
on the far side is empty.

#### Check 4. The split held

```bash
for t in green purple; do
  echo "== $t compact"; oc_c -n $t get pods -l app=udn-test -o name | while read -r p; do udnip oc_c $t $p; done
  echo "== $t sno";     oc_s -n $t get pods -l app=udn-test -o name | while read -r p; do udnip oc_s $t $p; done
done
```

compact's addresses must be in `10.204.0.0/17` and the SNO's in
`10.204.128.0/17`. An SNO pod on a low address (`10.204.0.x`) means its
reservation was dropped or is too small ([13.3](#133-splitting-green-between-the-two-clusters)).
green and purple on the **same** cluster may well hold the **same** address.
That is intended, and it is why Check 7 uses curl.

#### Check 5. Pod to pod across clusters, bridged

From the SNO's green pod to green on every compact master, then back:

```bash
S_POD=$(oc_s -n green get pods -l app=udn-test -o name | head -1)
S_IP=$(udnip oc_s green "$S_POD")
for p in $(oc_c -n green get pods -l app=udn-test -o name); do
  ip=$(udnip oc_c green "$p")
  echo "== sno -> $ip ($p)"; oc_s -n green exec "$S_POD" -- ping -c3 -W1 "$ip" | grep -E 'ttl|loss'
  echo "== $ip -> sno $S_IP"; oc_c -n green exec "$p" -- ping -c3 -W1 "$S_IP" | grep -E 'ttl|loss'
done
```

```
== sno -> 10.204.0.5 (pod/udn-test-abcde)
64 bytes from 10.204.0.5: icmp_seq=1 ttl=64 time=0.61 ms
3 packets transmitted, 3 received, 0% packet loss
```

**`ttl=64` is the result**, not just the reply. The inner packet crossed
two clusters and the fabric and was never routed. The ARP table confirms
it - the remote pod's own MAC, not a router's:

```bash
oc_s -n green exec "$S_POD" -- ip neigh show dev ovn-udn1
# 10.204.0.5 lladdr 0a:58:0a:cc:00:05 REACHABLE      <- 0a:58 + the compact pod's own IP
```

A reply with `ttl=63` or lower means something routed the packet. The two
pods are then not in one broadcast domain, whatever the configuration says.

#### Check 6. Full-size frames cross

A real fabric is where MTU problems appear. Ping at the UDN interface's
full MTU with fragmentation forbidden:

```bash
MTU=$(oc_s -n green exec "$S_POD" -- cat /sys/class/net/ovn-udn1/mtu)
oc_s -n green exec "$S_POD" -- ping -c3 -M do -s $((MTU - 28)) "$(udnip oc_c green "$(oc_c -n green get pods -l app=udn-test -o name | head -1)")"
```

Small pings work and this one fails: some link between the two VTEPs has an
MTU below pod MTU + 50 ([2.2](#22-per-fabric)). TCP connects and then stalls
on the first full segment. That looks like an application bug, and it
isn't.

#### Check 7. Type-2 programmed by BGP, and isolation by identity

The compact pod's MAC on the SNO is behind its own master's VTEP, and was
written by the control plane. Use the MAC `ip neigh` showed in Check 5:

```bash
oc_s debug node/$SNO_NODE --quiet -- chroot /host \
  bridge fdb show dev evx4-evpn-vtep | grep 0a:58:0a:cc:00:05
# 0a:58:0a:cc:00:05 dst 100.64.0.11 src_vni 400 self extern_learn
```

`dst` is the VTEP of the master that pod runs on (`oc_c -n green get pods
-o wide`). `extern_learn` means BGP put it there, as in Test 1.

Then the whole matrix, by HTTP, in both directions:

```bash
scripts/udn-xcluster-curl.sh "$COMPACT_KUBECONFIG" "$SNO_KUBECONFIG"
```

It detects EVPN mode (a tenant present in two clusters) and curls every
`udn-web` from every tenant's `udn-test` pod in both clusters. Pass:

- green in each cluster gets **"I am green on compact"** and **"I am green
  on sno"** - itself, across the boundary, both ways. The same for purple.
- green never gets an answer from purple, and purple never from green,
  **including where they hold the same address**. The page names its own
  tenant, so an address collision is settled, not reported as AMBIG.

This is the isolation that matters for a stretched L2: the VNI and the
route target are the boundary, not the addresses.

#### Check 8. The one thing that is shared and should not be

Both clusters put green's gateway on `10.204.0.1`, and the SNO's management
port on `.2` duplicates one of the compact cluster's. OVN-Kubernetes derives
the MAC from the IP, so **the same MAC (`0a:58:0a:cc:00:01`) is advertised
from more than one VTEP, in two clusters**:

```bash
oc_s -n $FRR_NS exec ds/frr-k8s -c frr -- \
  vtysh -c 'show bgp l2vpn evpn route type macip' | grep -A1 '0a:58:0a:cc:00:01'
```

This is a known limitation, not a misconfiguration
([HANDOFF.md](HANDOFF.md), finding 1). `reservedSubnets` cannot fix it,
because every cluster needs these addresses. What it means for this test
case:

- **Pod to pod, both checks above, and the live migration are unaffected.**
  They never use the gateway: same-subnet traffic is on-link.
- **Traffic leaving green through the cluster's gateway** (`10.204.0.1`) goes
  to whichever cluster's router answered the ARP. Do not test north-south
  across the stretch and read the result as an EVPN fault.
- **The fabric may flag the gateway MAC** as moving, or freeze it under
  duplicate-address detection. Tell the network team in advance. **[verify on site]**

---

### 13.7 Pass criteria

| # | Test | Pass | Points at, when it fails |
|---|---|---|---|
| 1.1 | VM before/after | Node changed. IP and MAC **identical** | `ipam.lifecycle: Persistent` missing on green, or a Layer3 network |
| 1.2 | SNO ping during migration | 0 to a few packets lost at 5/s. Nothing lost after `Succeeded` | Losses continue: SNO FDB still points at the old VTEP. Check type-2 propagation |
| 1.3 | SNO FDB / route after | `dst` and next hop = new master's VTEP, `extern_learn`, `MM:` incremented | No `MM:` change: the route did not cross. Next hop is a leaf address: next hop rewritten ([13.2](#132-what-changes-per-cluster) item 2) |
| 1.4 | All three masters visited | Ping recovers after every migration | Ping dies on one node: the leaf has no `/32` for that master's VTEP |
| 2.1 | Sessions | 4 peers, IPv4 + EVPN `Established` | [Section 10](#10-verify-in-this-order) step 1, TCP-MD5 row in [11](#11-the-failures-that-look-like-success) |
| 2.2 | Underlay | `ip route get <remote VTEP>` via the leaf, both ways | Unicast AF missing, or the fabric does not carry the `/32`s |
| 2.3 | Type-3 | Each cluster's VTEPs in the other's flood list, VNI 400 and 500 | Transit leaf discarding routes (`retain route-target all`), or auto route targets |
| 2.4 | IPAM split | compact in `10.204.0.0/17`, SNO in `10.204.128.0/17` | `reservedSubnets` pruned (wrong field name) or the hole too large |
| 2.5 | Pod to pod | Replies both ways, **`ttl=64`**, remote pod's own MAC in `ip neigh` | `ttl` < 64: something is routing. No reply with 2.3 passing: forwarding, [5.3](#53-ip-forwarding) |
| 2.6 | Full-size frames | DF ping at `MTU - 28` succeeds | Underlay MTU < pod MTU + 50 somewhere on the path |
| 2.7 | `udn-xcluster-curl.sh` | Same tenant answers across clusters, every cross-tenant cell silent | A cross-tenant answer: VNI / route target shared between green and purple |
| 2.8 | Gateway MAC | Known and explained, not chased | - |
