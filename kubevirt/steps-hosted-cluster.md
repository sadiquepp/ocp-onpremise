# KubeVirt Hosted Cluster - Step 8 in Detail

Step 8 of [steps.md](steps.md) runs as one playbook. This file breaks it into
the individual steps it performs, in order. Each step has the playbook tag
that runs just that part, and the same thing by hand with `oc`, so you can
stop and look between steps.

Starting point: steps 1-7 of [steps.md](steps.md) done.

## Contents

- [8.1 Preflight](#81-preflight)
- [8.2 OpenShift Virtualization](#82-openshift-virtualization)
- [8.3 Wildcard routes on hub1's router](#83-wildcard-routes-on-hub1s-router)
- [8.4 Render the bundle](#84-render-the-bundle)
- [8.5 HostedCluster - the control plane](#85-hostedcluster---the-control-plane)
- [8.6 NodePool - the worker VMs](#86-nodepool---the-worker-vms)
- [8.7 Import into ACM](#87-import-into-acm)
- [8.8 Wait, and get the kubeconfig](#88-wait-and-get-the-kubeconfig)
- [8.9 Verify](#89-verify)
- [MetalLB variant](#metallb-variant)
- [hcp-kv2: workers on the SNO](#hcp-kv2-workers-on-the-sno)
- [sno3 and hcp-kv3: control plane on an MCE cluster](#sno3-and-hcp-kv3-control-plane-on-an-mce-cluster)
  - [S.1 Build sno3](#s1-build-sno3)
  - [S.2 MCE on sno3](#s2-mce-on-sno3)
  - [S.3 MetalLB on sno3, and hcp-kv3's API pool](#s3-metallb-on-sno3-and-hcp-kv3s-api-pool)
  - [S.4 Import sno3 into hub1's ACM, with hosted-cluster discovery](#s4-import-sno3-into-hub1s-acm-with-hosted-cluster-discovery)
  - [S.5 Import the SNO into hub1's ACM](#s5-import-the-sno-into-hub1s-acm)
  - [K.1-K.9 hcp-kv3: 8.1-8.9 against sno3](#k1-k9-hcp-kv3-81-89-against-sno3)
    - [K.1 Preflight](#k1-preflight)
    - [K.2-K.3 OpenShift Virtualization and wildcard routes, on the SNO](#k2-k3-openshift-virtualization-and-wildcard-routes-on-the-sno)
    - [K.4 Render, twice](#k4-render-twice)
    - [K.5 HostedCluster, on sno3](#k5-hostedcluster-on-sno3)
    - [K.6 NodePool, on sno3; the VM comes up on the SNO](#k6-nodepool-on-sno3-the-vm-comes-up-on-the-sno)
    - [K.7 Import into ACM, from discovery](#k7-import-into-acm-from-discovery)
    - [K.8 Wait, and get the kubeconfig](#k8-wait-and-get-the-kubeconfig)
    - [K.9 Verify](#k9-verify)

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

The hosted cluster's own manifests (8.5, 8.6) are already files: the playbook
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
| [8.7](#87-import-into-acm) | Import into ACM | none - MCE does it |
| [8.8](#88-wait-and-get-the-kubeconfig) | Wait, and get the kubeconfig | `wait` |
| [8.9](#89-verify) | Verify | `verify` |

`mgmt` runs 8.2 and 8.3 together. 8.5 and 8.6 have no tag of their own,
because in the playbook they run as one block under `cluster`; by hand you
apply the rendered files one at a time. 8.7 is MCE's own: you only check it. The [MetalLB variant](#metallb-variant) adds
three steps; [hcp-kv2](#hcp-kv2-workers-on-the-sno), with its workers on the SNO, changes four;
[sno3 and hcp-kv3](#sno3-and-hcp-kv3-control-plane-on-an-mce-cluster) builds the MCE cluster
step by step, then runs these against it.

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
apiVersion: hco.kubevirt.io/v1
kind: HyperConverged
metadata:
  name: kubevirt-hyperconverged
  namespace: openshift-cnv
spec:
  workloadSources:
    enableCommonBootImageImport: false   # kubevirt_common_boot_images
EOF
oc apply -f "$M/8.2-hyperconverged.yaml"

oc -n openshift-cnv wait hyperconverged/kubevirt-hyperconverged \
  --for=condition=Available --timeout=20m
oc get nodes -l kubevirt.io/schedulable=true               # the 3 workers
```

A worker missing from that list has no `/dev/kvm`: nested virtualisation did
not reach it.

`enableCommonBootImageImport: false` stops OpenShift Virtualization
downloading its CentOS, Fedora and RHEL images, one 30Gi PVC each. The worker
VMs boot RHCOS and never use them, and on an LVMS cluster such as the SNO they
fill the thin pool until the workers are paused. On a cluster where they were
already downloaded, check the DataImportCrons have gone, then delete them:
`oc get dataimportcron -n openshift-virtualization-os-images` (empty), then
`oc delete dv,pvc --all -n openshift-virtualization-os-images`.

The field is under `spec.workloadSources` in the `hco.kubevirt.io/v1` API,
which OpenShift Virtualization 4.22 serves. An operator that serves only
`v1beta1` has it at the top of `spec` instead
(`spec.enableCommonBootImageImport`); the playbook picks the right one. To
see which applies: `oc get crd hyperconvergeds.hco.kubevirt.io -o
jsonpath='{.spec.versions[*].name}'`.

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
```

(`02-nad.yaml` appears only with the MetalLB variant, `04-acm-import.yaml`
only with `-e kubevirt_acm_import=true` - see 8.7.) Read them before
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

Nothing to apply. MCE's hypershift add-on imports every HostedCluster by
itself once its control plane is available: it creates the ManagedCluster
(hosted on `local-cluster`, klusterlet in Hosted mode) and, with ACM, the
KlusterletAddonConfig, with its labels left to ACM's `auto-detect`. Check:

```bash
oc get managedcluster hcp-kv1          # HUB ACCEPTED, JOINED, AVAILABLE True
oc get klusterletaddonconfig -n hcp-kv1
```

On a hub with that auto-import turned off, render the same objects with
`kubevirt_acm_import` and apply them:

```bash
$PB --tags cluster -e kubevirt_apply=false -e kubevirt_acm_import=true
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

## hcp-kv2: workers on the SNO

The same steps with `-e kubevirt_cluster=hcp-kv2`, except where its VMs run.
The control plane, API pool and ACM import are on hub1 as above; the VMs,
their disks and the `*.apps` Route are on the SNO, so four steps change:
8.2/8.3 run on the SNO, 8.4 runs twice with `00` applied to the SNO in
between, 8.6 is watched on the SNO, and 8.9 probes the SNO's router.
Before 8.1, the SNO and hcp-kv2's DNS and pool:
[steps.md, hcp-kv2](steps.md#hcp-kv2-workers-on-the-sno-optional).

```bash
# from kubevirt/, as at the top
export KUBECONFIG=/var/lib/libvirt/images/hub_install/auth/kubeconfig   # hub1
PB="ansible-playbook -i ../inventory/hosts setup_hosted_cluster_kubevirt.yaml --ask-vault-pass -e kubevirt_cluster=hcp-kv2"
SNO=/var/lib/libvirt/images/sno_install/auth/kubeconfig
M=/root/kubevirt-manifests/hub1   # the 8.2/8.3 files below were written there for hub1
```

This block sets everything the section uses, so it works in a fresh shell.
The 8.2/8.3 files must exist; written once for hub1, they are reused here
unchanged. If you never did 8.2 and 8.3 by hand, run their `cat > ...`
blocks first, after the setup block at the top of this file.

**8.2 and 8.3 on the SNO.** `$PB --tags mgmt` does both there. By hand, the
same files as for hub1, applied to the SNO:

```bash
oc --kubeconfig $SNO apply -f "$M/8.2-cnv-operator.yaml"
until oc --kubeconfig $SNO get crd hyperconvergeds.hco.kubevirt.io >/dev/null 2>&1; do sleep 10; done
oc --kubeconfig $SNO apply -f "$M/8.2-hyperconverged.yaml"
oc --kubeconfig $SNO -n openshift-cnv wait hyperconverged/kubevirt-hyperconverged --for=condition=Available --timeout=20m
oc --kubeconfig $SNO -n openshift-ingress-operator patch ingresscontroller default --type=merge \
  --patch-file "$M/8.3-ingresscontroller-wildcards.yaml"
oc --kubeconfig $SNO -n openshift-ingress rollout status deployment/router-default --timeout=10m
```

**8.4 renders one more file, and runs twice.** For a cluster with
`infra: sno`, the same render command also writes `00-infra-access.yaml`:
what HyperShift needs on the SNO (the VM namespace `hcp-kv2-hcp-kv2`, a
ServiceAccount `hcp-infra` with its Role and token, and read access to the
SNO's network config). `01` needs that ServiceAccount's token for its
`infra-kubeconfig-hcp-kv2` Secret, and the token only exists once `00` is
applied - so render, apply `00`, render again:

1. Render - the same command as 8.4:

   ```bash
   $PB --tags cluster -e kubevirt_apply=false
   ls rendered/hcp-kv2/          # 00-infra-access.yaml  01-hosted-cluster.yaml  03-nodepool.yaml
   grep -A1 'not here yet' rendered/hcp-kv2/01-hosted-cluster.yaml   # no Secret yet: apply 00, render again
   ```

2. Apply `00` to the SNO, and check it issued the token:

   ```bash
   oc --kubeconfig $SNO apply -f rendered/hcp-kv2/00-infra-access.yaml
   oc --kubeconfig $SNO -n hcp-kv2-hcp-kv2 get secret hcp-infra-token \
     -o jsonpath='{.data.token}' | wc -c                  # > 0
   ```

3. Render again - the same command. It now reads the token from the SNO, and
   `01` carries the Secret:

   ```bash
   $PB --tags cluster -e kubevirt_apply=false
   grep -c 'name: infra-kubeconfig-hcp-kv2' rendered/hcp-kv2/01-hosted-cluster.yaml   # 2: the Secret, and the credentials naming it
   ```

(A full run - no `-e kubevirt_apply=false` - does all three itself.)

**8.5 HostedCluster, on hub1.** Check what `01` points at, then apply it:

```bash
grep -A4 'credentials:' rendered/hcp-kv2/01-hosted-cluster.yaml   # infraKubeConfigSecret infra-kubeconfig-hcp-kv2, infraNamespace hcp-kv2-hcp-kv2
grep 'baseDomain:' rendered/hcp-kv2/01-hosted-cluster.yaml        # apps.sno.mylab.com: *.apps goes through the SNO's router

oc apply -f rendered/hcp-kv2/01-hosted-cluster.yaml
oc get hostedcluster hcp-kv2 -n hcp-kv2 -w     # PROGRESS Partial; AVAILABLE comes after the workers
```

The control plane runs on hub1, as for hcp-kv1. In another terminal:

```bash
oc get pods -n hcp-kv2-hcp-kv2                 # etcd-0/1/2, kube-apiserver, ... Running, on hub1
oc get svc kube-apiserver -n hcp-kv2-hcp-kv2   # EXTERNAL-IP 192.168.122.252, from hcp-kv2-api-pool
oc get hostedcluster hcp-kv2 -n hcp-kv2 -o jsonpath='{range .status.conditions[?(@.type=="ValidConfiguration")]}{.status} {.message}{"\n"}{end}'
oc get hostedcluster hcp-kv2 -n hcp-kv2 -o jsonpath='{range .status.conditions[?(@.type=="ValidKubeVirtInfraNetworkPolicyRBAC")]}{.status} {.message}{"\n"}{end}'
curl -k https://api.hcp-kv2.hub.mylab.com:6443/version
```

`ValidConfiguration` True means HyperShift logged in to the SNO with that
Secret and found OpenShift Virtualization there. False with 401/403 in the
message means `00` or the token is wrong: go back to step 2 of 8.4.
`ValidKubeVirtInfraNetworkPolicyRBAC` False means `00`'s ClusterRole for the
SNO's network config is missing. The namespace `hcp-kv2-hcp-kv2` exists on
both clusters: the control plane on hub1, the VMs on the SNO.

**8.6 NodePool, on hub1; the VMs come up on the SNO.**

```bash
oc apply -f rendered/hcp-kv2/03-nodepool.yaml
```

Follow them on the SNO:

```bash
oc --kubeconfig $SNO get dv -n hcp-kv2-hcp-kv2 -w       # root disks on lvms-vg1: ImportScheduled -> Succeeded
oc --kubeconfig $SNO get vm,vmi -n hcp-kv2-hcp-kv2 -w   # 2 VMs, 4 vCPU / 12Gi; VMIs Scheduling -> Running
oc get nodepool -n hcp-kv2 -w                           # on hub1: CURRENT NODES 0 -> 2
```

Nothing of the NodePool's appears in `oc get vm -n hcp-kv2-hcp-kv2` on hub1.
A VMI stuck in `Scheduling` on the SNO is usually memory: `oc --kubeconfig
$SNO describe vmi ...` says so. To watch one boot:

```bash
virtctl --kubeconfig $SNO console -n hcp-kv2-hcp-kv2 \
  $(oc --kubeconfig $SNO get vmi -n hcp-kv2-hcp-kv2 -o name | head -1 | cut -d/ -f2)
```

**8.7 and 8.8** are as for hcp-kv1, on hub1, with `hcp-kv2` for `hcp-kv1`.

**8.9** probes `console-openshift-console.apps.hcp-kv2.apps.sno.mylab.com`,
through the SNO's router:

```bash
oc --kubeconfig $SNO get route -n hcp-kv2-hcp-kv2               # the *.apps passthrough, on the SNO
curl -kI https://console-openshift-console.apps.hcp-kv2.apps.sno.mylab.com
```

## sno3 and hcp-kv3: control plane on an MCE cluster

The three-cluster layout ([steps.md, hcp-kv3](steps.md#hcp-kv3-control-plane-on-sno3-vms-on-the-sno-managed-from-hub1-optional)):
hub1's ACM manages, sno3 runs MCE alone and hosts hcp-kv3's control plane, and
the SNO runs its worker VM. First sno3 itself - `setup_sno3_mce.yaml`, one tag
per step - then hcp-kv3, which is 8.1-8.9 against sno3.

Before S.1: the SNO built (the hcp-kv2 section of [steps.md](steps.md#hcp-kv2-workers-on-the-sno-optional)),
and, from the repository root, sno3's zone and hcp-kv3's API name
(`setup_bm_host.yaml --tags dns`).

Once, write the variables every step below uses to a file:

```bash
mkdir -p /root/kubevirt-manifests/sno3
cat > /root/kubevirt-manifests/sno3.env <<'EOF'
cd /root/ocp-onpremise/kubevirt
SNO3PB="ansible-playbook -i ../inventory/hosts setup_sno3_mce.yaml --ask-vault-pass"
HUB=/var/lib/libvirt/images/hub_install/auth/kubeconfig     # hub1: ACM
SNO3=/var/lib/libvirt/images/sno3_install/auth/kubeconfig   # sno3: MCE, hcp-kv3's control plane
SNO=/var/lib/libvirt/images/sno_install/auth/kubeconfig     # the SNO: the VMs
M3=/root/kubevirt-manifests/sno3                            # the by-hand files
CATALOG=redhat-operators                                    # kubevirt_catalog_source
CATALOG_NS=openshift-marketplace                            # kubevirt_catalog_source_namespace
EOF
```

(Change the `cd` if your clone is not `/root/ocp-onpremise`.) Every block
from S.1 on starts by loading that file, so each works in a fresh shell, or
on its own when you jump to it. Every `oc` names its cluster with
`--kubeconfig`; an empty `$HUB` shows up as `unknown shorthand flag: 'f' in
-f`, an empty `$CATALOG` as `spec.source: Required value`. To check:

```bash
. /root/kubevirt-manifests/sno3.env
echo "HUB=$HUB SNO3=$SNO3 SNO=$SNO M3=$M3 CATALOG=$CATALOG/$CATALOG_NS"
```

| Step | What | Tag |
|---|---|---|
| S.1 | Build sno3 | `sno` |
| S.2 | MCE on sno3 | `mce` |
| S.3 | MetalLB on sno3, hcp-kv3's API pool | `metallb` |
| S.4 | Import sno3 into hub1's ACM, with hosted-cluster discovery | `import` |
| S.5 | Import the SNO into hub1's ACM - optional, for ACM's view of the VMs | `import-infra` |
| K.1-K.9 | hcp-kv3: 8.1-8.9 against sno3 | the KubeVirt playbook's tags |

### S.1 Build sno3

```bash
. /root/kubevirt-manifests/sno3.env
$SNO3PB --tags sno
```

`roles/setup-sno` with sno3's values - the same build as the SNO's, by hand
too: [../setup_sno.yaml](../setup_sno.yaml) and its role. Skipped once
`$SNO3` exists. Check:

```bash
. /root/kubevirt-manifests/sno3.env
oc --kubeconfig $SNO3 get nodes                 # sno3 Ready
oc --kubeconfig $SNO3 get sc                    # lvms-vg1 (default) - the control planes' etcd lands here
dig +short api.sno3.hub.mylab.com @192.168.122.21   # 192.168.122.63
```

### S.2 MCE on sno3

```bash
. /root/kubevirt-manifests/sno3.env
$SNO3PB --tags mce
```

By hand - MCE alone, no ACM; no channel, so OLM follows the package's default:

```bash
. /root/kubevirt-manifests/sno3.env
cat > "$M3/s2-mce-operator.yaml" <<EOF
apiVersion: v1
kind: Namespace
metadata:
  name: multicluster-engine
---
apiVersion: operators.coreos.com/v1
kind: OperatorGroup
metadata:
  name: multicluster-engine
  namespace: multicluster-engine
spec:
  targetNamespaces:
    - multicluster-engine
---
apiVersion: operators.coreos.com/v1alpha1
kind: Subscription
metadata:
  name: multicluster-engine
  namespace: multicluster-engine
spec:
  name: multicluster-engine
  installPlanApproval: Automatic
  source: ${CATALOG}
  sourceNamespace: ${CATALOG_NS}
EOF
oc --kubeconfig $SNO3 apply -f "$M3/s2-mce-operator.yaml"
until oc --kubeconfig $SNO3 get crd multiclusterengines.multicluster.openshift.io >/dev/null 2>&1; do sleep 10; done

cat > "$M3/s2-multiclusterengine.yaml" <<EOF
apiVersion: multicluster.openshift.io/v1
kind: MultiClusterEngine
metadata:
  name: multiclusterengine
spec:
  overrides:
    components:
      - name: hypershift
        enabled: true
EOF
oc --kubeconfig $SNO3 apply -f "$M3/s2-multiclusterengine.yaml"
oc --kubeconfig $SNO3 get multiclusterengine -w                 # PHASE Available
oc --kubeconfig $SNO3 -n hypershift rollout status deployment/operator
```

Then turn off MCE's own import of the hosted clusters it creates - hub1
imports them (S.4, K.7), and two importers would fight over the klusterlet.
Add `autoImportDisabled` to the variables already there:

```bash
. /root/kubevirt-manifests/sno3.env
oc --kubeconfig $SNO3 -n multicluster-engine get addondeploymentconfig hypershift-addon-deploy-config \
  -o jsonpath='{.spec.customizedVariables}{"\n"}'               # what is there now
oc --kubeconfig $SNO3 -n multicluster-engine patch addondeploymentconfig hypershift-addon-deploy-config \
  --type=json -p '[{"op":"add","path":"/spec/customizedVariables/-","value":{"name":"autoImportDisabled","value":"true"}}]'
```

(`/spec/customizedVariables/-` needs the list to exist; if the first command
printed nothing, use `--type=merge -p '{"spec":{"customizedVariables":[{"name":"autoImportDisabled","value":"true"}]}}'`.)

### S.3 MetalLB on sno3, and hcp-kv3's API pool

```bash
. /root/kubevirt-manifests/sno3.env
$SNO3PB --tags metallb
```

By hand - the operator, then the instance, then hcp-kv3's single-address
pool, bound to its control-plane namespace exactly as on hub1:

```bash
. /root/kubevirt-manifests/sno3.env
cat > "$M3/s3-metallb-operator.yaml" <<EOF
apiVersion: v1
kind: Namespace
metadata:
  name: metallb-system
---
apiVersion: operators.coreos.com/v1
kind: OperatorGroup
metadata:
  name: metallb-operator
  namespace: metallb-system
---
apiVersion: operators.coreos.com/v1alpha1
kind: Subscription
metadata:
  name: metallb-operator-sub
  namespace: metallb-system
spec:
  channel: stable
  name: metallb-operator
  source: ${CATALOG}
  sourceNamespace: ${CATALOG_NS}
EOF
oc --kubeconfig $SNO3 apply -f "$M3/s3-metallb-operator.yaml"
until oc --kubeconfig $SNO3 get crd ipaddresspools.metallb.io >/dev/null 2>&1; do sleep 10; done

cat > "$M3/s3-metallb-hcp-kv3.yaml" <<EOF
apiVersion: metallb.io/v1beta1
kind: MetalLB
metadata:
  name: metallb
  namespace: metallb-system
---
apiVersion: metallb.io/v1beta1
kind: IPAddressPool
metadata:
  name: hcp-kv3-api-pool
  namespace: metallb-system
spec:
  addresses:
    - 192.168.122.253-192.168.122.253
  autoAssign: true
  avoidBuggyIPs: true
  serviceAllocation:
    priority: 10
    namespaces:
      - hcp-kv3-hcp-kv3
---
apiVersion: metallb.io/v1beta1
kind: L2Advertisement
metadata:
  name: hcp-kv3-api-pool-l2
  namespace: metallb-system
spec:
  ipAddressPools:
    - hcp-kv3-api-pool
EOF
oc --kubeconfig $SNO3 apply -f "$M3/s3-metallb-hcp-kv3.yaml"
oc --kubeconfig $SNO3 get ipaddresspool -n metallb-system      # hcp-kv3-api-pool
```

### S.4 Import sno3 into hub1's ACM, with hosted-cluster discovery

```bash
. /root/kubevirt-manifests/sno3.env
$SNO3PB --tags import
```

By hand, on hub1. An MCE cluster already runs its own klusterlet and
add-ons, so ACM's go beside them: a `noOperator` KlusterletConfig, and the
add-ons' agents in their own namespace. The hypershift-addon reports sno3's
hosted clusters to hub1 and leaves sno3's HyperShift operator to sno3's MCE.
Red Hat's procedure sets these hub-wide; hub1 hosts its own hosted clusters,
so they are attached to sno3's add-ons only (`spec.configs`).

First the add-ons' namespace, on sno3. Nothing else creates it: without it,
every add-on manifest hub1 sends fails with `namespaces
"open-cluster-management-agent-addon-discovery" not found`, and the
hypershift-addon - the one that reports hcp-kv3 - never runs:

```bash
. /root/kubevirt-manifests/sno3.env
oc --kubeconfig $SNO3 create namespace open-cluster-management-agent-addon-discovery
```

Then the import, on hub1:

```bash
. /root/kubevirt-manifests/sno3.env
cat > "$M3/s4-hub-import.yaml" <<EOF
apiVersion: config.open-cluster-management.io/v1alpha1
kind: KlusterletConfig
metadata:
  name: mce-import-klusterlet-config
spec:
  installMode:
    type: noOperator
    noOperator:
      postfix: mce-import
---
apiVersion: addon.open-cluster-management.io/v1alpha1
kind: AddOnDeploymentConfig
metadata:
  name: mce-addon-ns-config
  namespace: multicluster-engine
spec:
  agentInstallNamespace: open-cluster-management-agent-addon-discovery
---
apiVersion: addon.open-cluster-management.io/v1alpha1
kind: AddOnDeploymentConfig
metadata:
  name: mce-hypershift-addon-config
  namespace: multicluster-engine
spec:
  agentInstallNamespace: open-cluster-management-agent-addon-discovery
  customizedVariables:
    - name: disableHOManagement
      value: "true"
    - name: disableMetrics
      value: "true"
---
apiVersion: v1
kind: Namespace
metadata:
  name: sno3
EOF
for a in work-manager managed-serviceaccount cluster-proxy; do
cat >> "$M3/s4-hub-import.yaml" <<EOF
---
apiVersion: addon.open-cluster-management.io/v1alpha1
kind: ManagedClusterAddOn
metadata:
  name: ${a}
  namespace: sno3
spec:
  configs:
    - group: addon.open-cluster-management.io
      resource: addondeploymentconfigs
      name: mce-addon-ns-config
      namespace: multicluster-engine
EOF
done
cat >> "$M3/s4-hub-import.yaml" <<EOF
---
apiVersion: addon.open-cluster-management.io/v1alpha1
kind: ManagedClusterAddOn
metadata:
  name: hypershift-addon
  namespace: sno3
spec:
  configs:
    - group: addon.open-cluster-management.io
      resource: addondeploymentconfigs
      name: mce-hypershift-addon-config
      namespace: multicluster-engine
---
apiVersion: cluster.open-cluster-management.io/v1
kind: ManagedCluster
metadata:
  name: sno3
  annotations:
    agent.open-cluster-management.io/klusterlet-config: mce-import-klusterlet-config
  labels:
    cloud: auto-detect
    vendor: auto-detect
    cluster.open-cluster-management.io/clusterset: default
spec:
  hubAcceptsClient: true
  leaseDurationSeconds: 60
EOF
oc --kubeconfig $HUB apply -f "$M3/s4-hub-import.yaml"

# the import itself: hub1 uses sno3's kubeconfig once to install its klusterlet
oc --kubeconfig $HUB -n sno3 create secret generic auto-import-secret \
  --from-literal=autoImportRetry=5 --from-file=kubeconfig=$SNO3

oc --kubeconfig $HUB get managedcluster sno3 -w                  # JOINED, AVAILABLE True
oc --kubeconfig $HUB get managedclusteraddon -n sno3             # hypershift-addon Available
oc --kubeconfig $SNO3 get pods -n open-cluster-management-agent-addon-discovery   # ACM's agents, beside MCE's own
```

If the add-ons stay unavailable, this per-cluster scoping is the first thing
to check: `oc --kubeconfig $HUB get managedclusteraddon -n sno3 -o yaml`
shows the config each one resolved.

The console's Nodes column for sno3 reads 0 for a few minutes after it
joins: the count comes from `ManagedClusterInfo`, which the work-manager agent
fills in once it runs in `open-cluster-management-agent-addon-discovery`. It
should change to 1 by itself. If it stays at 0, check the
`klusterlet-addon-workmgr` pod there, and
`oc --kubeconfig $HUB get managedclusterinfo sno3 -n sno3 -o jsonpath='{.status.nodeList[*].name}'`.

### S.5 Import the SNO into hub1's ACM

```bash
. /root/kubevirt-manifests/sno3.env
$SNO3PB --tags import-infra
```

Optional: hcp-kv3 does not need it. HyperShift reaches the SNO through the
infra kubeconfig Secret, not through ACM, and K.7's import of hcp-kv3 rests on
S.4 alone. What this adds is visibility: an ordinary import, so ACM's search
and virtualization views show the VMs the SNO runs, hcp-kv3's worker among
them. Skip it and come back any time. By hand:

```bash
. /root/kubevirt-manifests/sno3.env
cat > "$M3/s5-sno-import.yaml" <<EOF
apiVersion: v1
kind: Namespace
metadata:
  name: sno
---
apiVersion: cluster.open-cluster-management.io/v1
kind: ManagedCluster
metadata:
  name: sno
  labels:
    cloud: auto-detect
    vendor: auto-detect
    cluster.open-cluster-management.io/clusterset: default
spec:
  hubAcceptsClient: true
---
apiVersion: agent.open-cluster-management.io/v1
kind: KlusterletAddonConfig
metadata:
  name: sno
  namespace: sno
spec:
  clusterName: sno
  clusterNamespace: sno
  clusterLabels:
    cloud: auto-detect
    vendor: auto-detect
  applicationManager:
    enabled: true
  policyController:
    enabled: true
  searchCollector:
    enabled: true
  certPolicyController:
    enabled: true
EOF
oc --kubeconfig $HUB apply -f "$M3/s5-sno-import.yaml"
oc --kubeconfig $HUB -n sno create secret generic auto-import-secret \
  --from-literal=autoImportRetry=5 --from-file=kubeconfig=$SNO
oc --kubeconfig $HUB get managedcluster sno -w                   # JOINED, AVAILABLE True
```

### K.1-K.9 hcp-kv3: 8.1-8.9 against sno3

8.1-8.9 again, for hcp-kv3. Three clusters are involved, and each `oc` says
which one with `--kubeconfig`:

| Cluster | Variable | What of hcp-kv3's is there |
|---|---|---|
| sno3 | `$SNO3` | HostedCluster and NodePool (ns `hcp-kv3`), the control plane (ns `hcp-kv3-hcp-kv3`), the API at `.253` |
| the SNO | `$SNO` | The worker VM, its disk, the `*.apps` passthrough Route (ns `hcp-kv3-hcp-kv3`) |
| hub1 | `$HUB` | Only the DiscoveredCluster and the ManagedCluster `sno3-hcp-kv3` - nothing of HyperShift's |

hcp-kv3 is `SingleReplica` (sno3 has one node), with one worker of 4 vCPU and
12Gi. Once, add the playbook and hcp-kv3's kubeconfig path to the
variables:

```bash
cat > /root/kubevirt-manifests/hcp-kv3.env <<'EOF'
. /root/kubevirt-manifests/sno3.env                         # HUB, SNO3, SNO, M3; cd kubevirt/
PB="ansible-playbook -i ../inventory/hosts setup_hosted_cluster_kubevirt.yaml --ask-vault-pass -e kubevirt_mgmt=sno3 -e kubevirt_cluster=hcp-kv3"
M=/root/kubevirt-manifests/hub1                             # the 8.2/8.3 files, reused on the SNO
KV3=/var/lib/libvirt/images/hcp-kv3/kubeconfig              # hcp-kv3's admin kubeconfig, from K.8
EOF
```

Every block below starts by loading it.

| Step | What | Where | Tag |
|---|---|---|---|
| K.1 | Preflight | sno3, the SNO | `preflight` |
| K.2, K.3 | OpenShift Virtualization, wildcard routes | the SNO | `mgmt` |
| K.4 | Render, twice | the SNO in between | `cluster -e kubevirt_apply=false` |
| K.5 | HostedCluster | sno3 | by hand |
| K.6 | NodePool | sno3; the VM on the SNO | by hand |
| K.7 | Import into ACM, from discovery | hub1 | `wait` |
| K.8 | Wait, and get the kubeconfig | sno3 | `wait` |
| K.9 | Verify | all three | `verify` |

#### K.1 Preflight

```bash
. /root/kubevirt-manifests/hcp-kv3.env
$PB --tags preflight
```

By hand. sno3 does what hub1 does in 8.1, plus the SNO is checked as the VMs'
cluster:

```bash
. /root/kubevirt-manifests/hcp-kv3.env
oc --kubeconfig $SNO3 get crd hostedclusters.hypershift.openshift.io ipaddresspools.metallb.io   # both: S.2, S.3
oc --kubeconfig $SNO3 -n hypershift get deployment operator                                     # sno3's own HyperShift operator
oc --kubeconfig $SNO3 get ipaddresspool hcp-kv3-api-pool -n metallb-system \
  -o jsonpath='{.spec.addresses}{"\n"}'                    # ["192.168.122.253-192.168.122.253"]
dig +short api.hcp-kv3.hub.mylab.com @192.168.122.21       # 192.168.122.253
oc --kubeconfig $SNO3 get sc                               # lvms-vg1 (default): hcp-kv3's etcd
oc --kubeconfig $SNO get nodes                             # the SNO answers
oc --kubeconfig $SNO get sc                                # lvms-vg1 (default): the worker's root disk
cat /sys/module/kvm_intel/parameters/nested                # Y
```

#### K.2-K.3 OpenShift Virtualization and wildcard routes, on the SNO

Already done if hcp-kv2 is built: hcp-kv3's VM goes on the same SNO. To
confirm, or do it:

```bash
. /root/kubevirt-manifests/hcp-kv3.env
$PB --tags mgmt
```

By hand - the hub1 files of 8.2 and 8.3, applied to the SNO (write them first
with 8.2 and 8.3 if `$M` is empty):

```bash
. /root/kubevirt-manifests/hcp-kv3.env
oc --kubeconfig $SNO apply -f "$M/8.2-cnv-operator.yaml"
until oc --kubeconfig $SNO get crd hyperconvergeds.hco.kubevirt.io >/dev/null 2>&1; do sleep 10; done
oc --kubeconfig $SNO apply -f "$M/8.2-hyperconverged.yaml"
oc --kubeconfig $SNO -n openshift-cnv wait hyperconverged/kubevirt-hyperconverged --for=condition=Available --timeout=20m
oc --kubeconfig $SNO -n openshift-ingress-operator patch ingresscontroller default --type=merge \
  --patch-file "$M/8.3-ingresscontroller-wildcards.yaml"
oc --kubeconfig $SNO -n openshift-ingress rollout status deployment/router-default --timeout=10m
```

sno3 needs neither: it runs no VMs, and its router only serves the control
plane's own Routes (oauth, ignition-server, konnectivity).

#### K.4 Render, twice

As for hcp-kv2: `00-infra-access.yaml` is for the SNO, and `01` can only carry
the infra kubeconfig Secret once `00` has issued its token.

1. Render:

   ```bash
   . /root/kubevirt-manifests/hcp-kv3.env
   $PB --tags cluster -e kubevirt_apply=false
   ls rendered/hcp-kv3/          # 00-infra-access.yaml  01-hosted-cluster.yaml  03-nodepool.yaml
   grep -A1 'not here yet' rendered/hcp-kv3/01-hosted-cluster.yaml   # no Secret yet
   ```

2. Apply `00` to the SNO, and check it issued the token:

   ```bash
   . /root/kubevirt-manifests/hcp-kv3.env
   oc --kubeconfig $SNO apply -f rendered/hcp-kv3/00-infra-access.yaml
   oc --kubeconfig $SNO -n hcp-kv3-hcp-kv3 get secret hcp-infra-token \
     -o jsonpath='{.data.token}' | wc -c                  # > 0
   ```

   Its namespace, ServiceAccount and ClusterRole are hcp-kv3's own; hcp-kv2's
   on the same SNO are untouched.

3. Render again:

   ```bash
   . /root/kubevirt-manifests/hcp-kv3.env
   $PB --tags cluster -e kubevirt_apply=false
   grep -c 'name: infra-kubeconfig-hcp-kv3' rendered/hcp-kv3/01-hosted-cluster.yaml   # 2
   ```

(A full run - no `-e kubevirt_apply=false` - does all three itself, and K.5
and K.6 with them.)

#### K.5 HostedCluster, on sno3

```bash
. /root/kubevirt-manifests/hcp-kv3.env
grep -A4 'credentials:' rendered/hcp-kv3/01-hosted-cluster.yaml          # infra-kubeconfig-hcp-kv3, infraNamespace hcp-kv3-hcp-kv3
grep 'baseDomain:\|AvailabilityPolicy' rendered/hcp-kv3/01-hosted-cluster.yaml   # apps.sno.mylab.com; SingleReplica x2

oc --kubeconfig $SNO3 apply -f rendered/hcp-kv3/01-hosted-cluster.yaml
oc --kubeconfig $SNO3 get hostedcluster hcp-kv3 -n hcp-kv3 -w   # PROGRESS Partial; AVAILABLE comes after the worker
```

sno3's HyperShift operator starts the control plane on sno3. In another
terminal:

```bash
. /root/kubevirt-manifests/hcp-kv3.env
oc --kubeconfig $SNO3 get pods -n hcp-kv3-hcp-kv3                 # etcd-0, one kube-apiserver, ... Running
oc --kubeconfig $SNO3 get svc kube-apiserver -n hcp-kv3-hcp-kv3   # EXTERNAL-IP 192.168.122.253, from hcp-kv3-api-pool
oc --kubeconfig $SNO3 get route -n hcp-kv3-hcp-kv3                # oauth, konnectivity, ignition-server: *.apps.sno3.hub.mylab.com
oc --kubeconfig $SNO3 get hostedcluster hcp-kv3 -n hcp-kv3 -o jsonpath='{range .status.conditions[?(@.type=="ValidConfiguration")]}{.status} {.message}{"\n"}{end}'
oc --kubeconfig $SNO3 get hostedcluster hcp-kv3 -n hcp-kv3 -o jsonpath='{range .status.conditions[?(@.type=="ValidKubeVirtInfraNetworkPolicyRBAC")]}{.status} {.message}{"\n"}{end}'
curl -k https://api.hcp-kv3.hub.mylab.com:6443/version
```

The two conditions mean what they do for hcp-kv2: `ValidConfiguration` True is
sno3 logged in to the SNO with the Secret; False with 401/403 sends you back
to step 2 of K.4. An `EXTERNAL-IP` stuck at `<pending>` is S.3's pool.
`oc --kubeconfig $HUB get hostedcluster -A` does not list hcp-kv3: hub1 never
sees it as a HostedCluster.

#### K.6 NodePool, on sno3; the VM comes up on the SNO

```bash
. /root/kubevirt-manifests/hcp-kv3.env
oc --kubeconfig $SNO3 apply -f rendered/hcp-kv3/03-nodepool.yaml
```

Follow it:

```bash
. /root/kubevirt-manifests/hcp-kv3.env
oc --kubeconfig $SNO get dv -n hcp-kv3-hcp-kv3 -w       # root disk on lvms-vg1: ImportScheduled -> Succeeded
oc --kubeconfig $SNO get vm,vmi -n hcp-kv3-hcp-kv3 -w   # 1 VM, 4 vCPU / 12Gi; VMI Scheduling -> Running
oc --kubeconfig $SNO3 get nodepool -n hcp-kv3 -w        # CURRENT NODES 0 -> 1
```

The SNO now runs hcp-kv2's two workers and this one: a VMI stuck in
`Scheduling` is memory (`oc --kubeconfig $SNO describe vmi ...`). To watch it
boot:

```bash
. /root/kubevirt-manifests/hcp-kv3.env
virtctl --kubeconfig $SNO console -n hcp-kv3-hcp-kv3 \
  $(oc --kubeconfig $SNO get vmi -n hcp-kv3-hcp-kv3 -o name | head -1 | cut -d/ -f2)
```

#### K.7 Import into ACM, from discovery

This is where hcp-kv3 differs most from hcp-kv1. MCE on sno3 does not import
it (S.2 turned that off), and hub1 has no HostedCluster to import from. Once
hcp-kv3's API is up, sno3's hypershift-addon reports it to hub1 as a
DiscoveredCluster named `sno3-hcp-kv3`, and setting `importAsManagedCluster`
on it imports it. `$PB --tags wait` does this as part of K.8. By hand:

```bash
. /root/kubevirt-manifests/hcp-kv3.env
oc --kubeconfig $SNO3 get managedcluster                 # local-cluster only: sno3's MCE did not import it
oc --kubeconfig $HUB get discoveredcluster -n sno3 \
  -o custom-columns=NAME:.metadata.name,DISPLAY:.spec.displayName,TYPE:.spec.type,STATUS:.spec.status
                                                         # DISPLAY sno3-hcp-kv3, TYPE MultiClusterEngineHCP
DC=$(oc --kubeconfig $HUB get discoveredcluster -n sno3 \
  -o jsonpath='{.items[?(@.spec.displayName=="sno3-hcp-kv3")].metadata.name}')
oc --kubeconfig $HUB -n sno3 patch discoveredcluster $DC --type=merge \
  -p '{"spec":{"importAsManagedCluster":true}}'
oc --kubeconfig $HUB get managedcluster sno3-hcp-kv3 -w  # JOINED, AVAILABLE True
```

sno3's own console lists hcp-kv3 as **Pending import**, and keeps doing so.
That is expected: with its auto-import off (S.2), sno3's MCE hosts hcp-kv3 but
does not manage it. Hub1 does. Do not import it from sno3's console: a cluster
registers with one hub only, and a second import would give it a second
klusterlet competing with hub1's.

No DiscoveredCluster after a few minutes: check S.4's hypershift-addon
(`oc --kubeconfig $HUB get managedclusteraddon hypershift-addon -n sno3`).
As in 8.7, JOINED/AVAILABLE need only hcp-kv3's API, not the worker.

#### K.8 Wait, and get the kubeconfig

```bash
. /root/kubevirt-manifests/hcp-kv3.env
$PB --tags wait
```

By hand - as 8.8, against sno3:

```bash
. /root/kubevirt-manifests/hcp-kv3.env
oc --kubeconfig $SNO3 wait hostedcluster/hcp-kv3 -n hcp-kv3 --for=condition=Available --timeout=60m

mkdir -p $(dirname $KV3)
oc --kubeconfig $SNO3 extract -n hcp-kv3 \
  secret/$(oc --kubeconfig $SNO3 get hostedcluster hcp-kv3 -n hcp-kv3 -o jsonpath='{.status.kubeconfig.name}') \
  --keys=kubeconfig --to=$(dirname $KV3) --confirm

oc --kubeconfig $KV3 get nodes -w                        # 1 Ready
```

#### K.9 Verify

```bash
. /root/kubevirt-manifests/hcp-kv3.env
$PB --tags verify
```

By hand - [steps.md, step 9](steps.md#9-verify) with `$KV3`, and the
`*.apps` path through the SNO's router:

```bash
. /root/kubevirt-manifests/hcp-kv3.env
oc --kubeconfig $KV3 get clusterversion,co               # Available, none Degraded
oc --kubeconfig $SNO get route -n hcp-kv3-hcp-kv3        # the *.apps passthrough, on the SNO
curl -kI https://console-openshift-console.apps.hcp-kv3.apps.sno.mylab.com
oc --kubeconfig $HUB get managedcluster sno3-hcp-kv3     # in ACM, on hub1
```

Remove it with `$PB --tags destroy`: that deletes `sno3-hcp-kv3` from hub1
first, then the NodePool and HostedCluster on sno3, then its namespace and
access on the SNO.
