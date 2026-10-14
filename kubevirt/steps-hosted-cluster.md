# KubeVirt Hosted Cluster - Step 8 in Detail

Step 8 of [steps.md](steps.md) runs as one playbook. This file breaks it into
the individual steps it performs, in order. Each step has the playbook tag
that runs just that part, and the same thing by hand with `oc`, so you can
stop and look between steps.

Starting point: steps 1-7 of [steps.md](steps.md) done.

```bash
cd kubevirt
export KUBECONFIG=/var/lib/libvirt/images/hub_install/auth/kubeconfig   # hub1
PB="ansible-playbook -i ../inventory/hosts setup_hosted_cluster_kubevirt.yaml --ask-vault-pass"

# For the by-hand steps. The values are kubevirt/vars.yaml's defaults.
M=/root/kubevirt-manifests/hub1; mkdir -p "$M"
CATALOG=redhat-operators         # kubevirt_catalog_source
CATALOG_NS=openshift-marketplace # kubevirt_catalog_source_namespace
CNV_CHANNEL=stable               # kubevirt_cnv_channel
NMSTATE_CHANNEL=stable           # kubevirt_nmstate_channel
LOCALNET=kubevirt-lab            # kubevirt_localnet_name
LOCALNET_BRIDGE=br-ex            # kubevirt_localnet_bridge
```

**Every by-hand manifest is a file.** Each block writes its manifest into
`$M` and then applies it, so it stays there to review later:

```text
cat > "$M/8.2-cnv-operator.yaml" <<EOF    # 1. write it: your shell expands $CATALOG etc.
...
EOF
oc apply -f "$M/8.2-cnv-operator.yaml"     # 2. apply it
```

Between the two, look at what you are about to apply, every variable
expanded:

```bash
cat "$M/8.2-cnv-operator.yaml"
oc apply --dry-run=server -f "$M/8.2-cnv-operator.yaml"   # the API's verdict, nothing changed
```

The hosted cluster's own manifests (8.5-8.7) are already files: the playbook
renders them into `rendered/hcp-kv1/` in 8.4.

Every step is safe to repeat, and so is a full `$PB` run afterwards: what is
already there is applied unchanged.

| Step | What | Tag |
|---|---|---|
| [8.1](#81-preflight) | Preflight | `preflight` |
| [8.2](#82-openshift-virtualization) | OpenShift Virtualization on hub1 | `mgmt` |
| [8.3](#83-wildcard-routes-on-hub1s-router) | Wildcard routes on hub1's router | `mgmt` |
| [8.4](#84-render-the-bundle) | Render the bundle | `cluster -e kubevirt_apply=false` |
| [8.5](#85-hostedcluster---the-control-plane) | HostedCluster - the control plane | by hand |
| [8.6](#86-nodepool---the-worker-vms) | NodePool - the worker VMs | by hand |
| [8.7](#87-import-into-acm) | Import into ACM | by hand |
| [8.8](#88-wait-and-get-the-kubeconfig) | Wait, and get the kubeconfig | `wait` |
| [8.9](#89-verify) | Verify | `verify` |

`mgmt` runs 8.2 and 8.3 together. 8.5-8.7 have no tag of their own, because
in the playbook they run as one block under `cluster`; by hand you apply the
rendered files one at a time. The [MetalLB variant](#metallb-variant) adds
three steps.

## 8.1 Preflight

```bash
$PB --tags preflight
```

By hand:

```bash
oc get crd hostedclusters.hypershift.openshift.io ipaddresspools.metallb.io   # both exist
oc get ipaddresspool hcp-kv1-api-pool -n metallb-system \
  -o jsonpath='{.spec.addresses}{"\n"}'                    # ["192.168.122.250-192.168.122.250"]
cat /sys/module/kvm_intel/parameters/nested                # Y
dig +short api.hcp-kv1.hub.mylab.com @192.168.122.21       # 192.168.122.250
oc get sc                                                  # ocs-external-storagecluster-ceph-rbd (default)
```

## 8.2 OpenShift Virtualization

```bash
$PB --tags mgmt          # 8.2 and 8.3
```

By hand - the operator:

```bash
cat > "$M/8.2-cnv-operator.yaml" <<EOF
apiVersion: v1
kind: Namespace
metadata:
  name: openshift-cnv
---
apiVersion: operators.coreos.com/v1
kind: OperatorGroup
metadata:
  name: kubevirt-hyperconverged-group
  namespace: openshift-cnv
spec:
  targetNamespaces:
    - openshift-cnv
---
apiVersion: operators.coreos.com/v1alpha1
kind: Subscription
metadata:
  name: hco-operatorhub
  namespace: openshift-cnv
spec:
  channel: ${CNV_CHANNEL}
  name: kubevirt-hyperconverged
  installPlanApproval: Automatic
  source: ${CATALOG}
  sourceNamespace: ${CATALOG_NS}
EOF
oc apply -f "$M/8.2-cnv-operator.yaml"

until oc get crd hyperconvergeds.hco.kubevirt.io >/dev/null 2>&1; do sleep 10; done
```

Then the instance, and wait for it:

```bash
cat > "$M/8.2-hyperconverged.yaml" <<EOF
apiVersion: hco.kubevirt.io/v1beta1
kind: HyperConverged
metadata:
  name: kubevirt-hyperconverged
  namespace: openshift-cnv
spec: {}
EOF
oc apply -f "$M/8.2-hyperconverged.yaml"

oc -n openshift-cnv wait hyperconverged/kubevirt-hyperconverged \
  --for=condition=Available --timeout=20m
oc get nodes -l kubevirt.io/schedulable=true               # the 3 workers
```

A worker missing from that list has no `/dev/kvm`: nested virtualisation did
not reach it.

## 8.3 Wildcard routes on hub1's router

hcp-kv1's apps are `*.apps.hcp-kv1.apps.hub.mylab.com`, served by one
wildcard Route on hub1. The router admits wildcard routes only when told to.
This rolls the router pods once.

```bash
cat > "$M/8.3-ingresscontroller-wildcards.yaml" <<EOF
spec:
  routeAdmission:
    wildcardPolicy: WildcardsAllowed
EOF
oc -n openshift-ingress-operator patch ingresscontroller default --type=merge \
  --patch-file "$M/8.3-ingresscontroller-wildcards.yaml"
oc -n openshift-ingress rollout status deployment/router-default --timeout=10m
```

## 8.4 Render the bundle

```bash
$PB --tags cluster -e kubevirt_apply=false
ls rendered/hcp-kv1/
```

```
01-hosted-cluster.yaml   Namespace hcp-kv1, pull-secret and ssh-key Secrets, HostedCluster
03-nodepool.yaml         NodePool: 2 x 4 vCPU / 16Gi VMs, 32Gi root disks
04-acm-import.yaml       ManagedCluster, KlusterletAddonConfig
```

(`02-nad.yaml` appears only with the MetalLB variant.) Read them before
applying; `01` holds the pull secret, which is why the directory is 0700.

## 8.5 HostedCluster - the control plane

```bash
oc apply -f rendered/hcp-kv1/01-hosted-cluster.yaml

oc get hostedcluster hcp-kv1 -n hcp-kv1 -w     # PROGRESS Partial; AVAILABLE comes after the workers
```

HyperShift creates the control-plane namespace and starts the control plane
in it. In another terminal:

```bash
oc get pods -n hcp-kv1-hcp-kv1                 # etcd-0/1/2, kube-apiserver, ... Running
oc get svc kube-apiserver -n hcp-kv1-hcp-kv1   # EXTERNAL-IP 192.168.122.250, from hcp-kv1-api-pool
oc get route -n hcp-kv1-hcp-kv1                # oauth, konnectivity, ignition-server, oidc
curl -k https://api.hcp-kv1.hub.mylab.com:6443/version
```

An `EXTERNAL-IP` stuck at `<pending>` means the pool is missing; go back to
8.1.

## 8.6 NodePool - the worker VMs

```bash
oc apply -f rendered/hcp-kv1/03-nodepool.yaml
```

The NodePool creates the VMs; nothing else is needed. Follow them on hub1:

```bash
oc get dv -n hcp-kv1-hcp-kv1 -w        # root disks: CDI imports the RHCOS image, ImportScheduled -> Succeeded
oc get vm,vmi -n hcp-kv1-hcp-kv1 -w    # 2 VMs; VMIs Scheduling -> Running, on hub1's workers
oc get nodepool -n hcp-kv1 -w          # CURRENT NODES 0 -> 2
```

Each VM fetches its Ignition from the `ignition-server` Route, reboots once,
and joins. The first boot is the slow part on nested VMs: 15-30 minutes.

To watch one boot:

```bash
virtctl console -n hcp-kv1-hcp-kv1 $(oc get vmi -n hcp-kv1-hcp-kv1 -o name | head -1 | cut -d/ -f2)
```

(`virtctl` is on hub1's console: `?` menu > Command line tools.)

## 8.7 Import into ACM

MCE's hypershift add-on usually does this by itself: it creates the
ManagedCluster once hcp-kv1's control plane is available, so it may already
be there:

```bash
oc get managedcluster hcp-kv1          # JOINED/AVAILABLE True
oc get klusterletaddonconfig -n hcp-kv1
```

`04` is the same ManagedCluster (hosted on `local-cluster`, klusterlet in
Hosted mode) plus the KlusterletAddonConfig, so applying it either way is
harmless: it merges into what is there and adds anything missing.

```bash
oc apply -f rendered/hcp-kv1/04-acm-import.yaml
```

JOINED/AVAILABLE only need hcp-kv1's API: in Hosted mode the klusterlet runs
on hub1, not on the workers. 8.8 is what waits for the nodes.

## 8.8 Wait, and get the kubeconfig

```bash
$PB --tags wait
```

By hand:

```bash
oc wait hostedcluster/hcp-kv1 -n hcp-kv1 --for=condition=Available --timeout=60m

mkdir -p /var/lib/libvirt/images/hcp-kv1
oc extract -n hcp-kv1 secret/$(oc get hostedcluster hcp-kv1 -n hcp-kv1 -o jsonpath='{.status.kubeconfig.name}') \
  --keys=kubeconfig --to=/var/lib/libvirt/images/hcp-kv1 --confirm

oc --kubeconfig /var/lib/libvirt/images/hcp-kv1/kubeconfig get nodes -w     # 2 Ready
```

## 8.9 Verify

```bash
$PB --tags verify
```

By hand: [steps.md, step 9](steps.md#9-verify).

## MetalLB variant

With `ingress: metallb` (or `-e kubevirt_ingress=metallb`), three steps
change. Destroy a cluster built the default way first:
`$PB --tags destroy -e kubevirt_cluster=hcp-kv1`.

**Instead of 8.3: the localnet network on hub1.** `$PB --tags mgmt -e kubevirt_ingress=metallb`.
By hand - the NMState operator, skipped if `oc get crd
nodenetworkconfigurationpolicies.nmstate.io` already answers (udn-bgp-evpn
installs it too):

```bash
cat > "$M/metallb-1-nmstate-operator.yaml" <<EOF
apiVersion: v1
kind: Namespace
metadata:
  name: openshift-nmstate
---
apiVersion: operators.coreos.com/v1
kind: OperatorGroup
metadata:
  name: openshift-nmstate
  namespace: openshift-nmstate
spec:
  targetNamespaces:
    - openshift-nmstate
---
apiVersion: operators.coreos.com/v1alpha1
kind: Subscription
metadata:
  name: kubernetes-nmstate-operator
  namespace: openshift-nmstate
spec:
  channel: ${NMSTATE_CHANNEL}
  name: kubernetes-nmstate-operator
  installPlanApproval: Automatic
  source: ${CATALOG}
  sourceNamespace: ${CATALOG_NS}
EOF
oc apply -f "$M/metallb-1-nmstate-operator.yaml"

until oc get crd nmstates.nmstate.io >/dev/null 2>&1; do sleep 10; done
```

Then the NMState instance, and the bridge mapping on hub1's workers:

```bash
cat > "$M/metallb-2-nmstate.yaml" <<EOF
apiVersion: nmstate.io/v1
kind: NMState
metadata:
  name: nmstate
EOF
oc apply -f "$M/metallb-2-nmstate.yaml"

until oc get crd nodenetworkconfigurationpolicies.nmstate.io >/dev/null 2>&1; do sleep 10; done

cat > "$M/metallb-3-nncp-${LOCALNET}.yaml" <<EOF
apiVersion: nmstate.io/v1
kind: NodeNetworkConfigurationPolicy
metadata:
  name: ${LOCALNET}-bridge-mapping
spec:
  nodeSelector:
    node-role.kubernetes.io/worker: ""
  desiredState:
    ovn:
      bridge-mappings:
        - localnet: ${LOCALNET}
          bridge: ${LOCALNET_BRIDGE}
          state: present
EOF
oc apply -f "$M/metallb-3-nncp-${LOCALNET}.yaml"
oc wait nncp/${LOCALNET}-bridge-mapping --for=condition=Available --timeout=10m
```

**Between 8.5 and 8.6: the NAD.** Render with
`$PB --tags cluster -e kubevirt_ingress=metallb -e kubevirt_apply=false`,
apply `01`, then once `hcp-kv1-hcp-kv1` exists:

```bash
oc apply -f rendered/hcp-kv1/02-nad.yaml
oc get net-attach-def -n hcp-kv1-hcp-kv1      # kubevirt-lab
```

Then `03`: the VMs come up with one NIC, on the lab network, with a libvirt
DHCP address (`oc get vmi -n hcp-kv1-hcp-kv1 -o wide` shows .201-.249).

**After 8.8: MetalLB inside the hosted cluster.**

```bash
$PB --tags ingress -e kubevirt_ingress=metallb
```

By hand it is Step 8 of
[../compact-cluster/compact-cluster-hcp.md](../compact-cluster/compact-cluster-hcp.md#step-8---ingress-metallb-inside-the-hosted-cluster)
against hcp-kv1's kubeconfig, with `192.168.122.251` as the address. Then:

```bash
oc --kubeconfig /var/lib/libvirt/images/hcp-kv1/kubeconfig \
  -n openshift-ingress get svc metallb-ingress    # EXTERNAL-IP 192.168.122.251
curl -kI https://console-openshift-console.apps.hcp-kv1.hub.mylab.com
```
