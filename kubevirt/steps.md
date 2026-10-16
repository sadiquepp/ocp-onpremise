# KubeVirt Hosted Cluster - Steps

Connected lab, Ceph 9 / ODF external storage on hub1, and a hosted cluster
(`hcp-kv1`) whose workers are KubeVirt VMs that its NodePool creates on hub1,
nested on this host. Optionally a second one (`hcp-kv2`) with its control
plane on hub1 and its workers on the single-node cluster, and a third
(`hcp-kv3`) with its control plane on a second SNO running MCE, managed from
hub1's ACM. Commands only, start to finish. Explanations:
[README.md](README.md).

Steps 1-6 are the same commands as steps 1, 3-7 of the backup/restore flow
([../steps.md](../steps.md)), repeated here so this file stands alone. They
run from the repository root; steps 8 onwards run from `kubevirt/`.

## Contents

- [1. Prepare](#1-prepare)
- [2. Credentials and lab variables](#2-credentials-and-lab-variables)
- [3. Bare metal host](#3-bare-metal-host)
- [4. Ceph cluster](#4-ceph-cluster)
- [5. Hub1](#5-hub1)
- [6. Attach Ceph to hub1](#6-attach-ceph-to-hub1)
- [7. Check the hub can run the workers](#7-check-the-hub-can-run-the-workers)
- [8. OpenShift Virtualization and the hosted cluster](#8-openshift-virtualization-and-the-hosted-cluster)
  - step by step: [steps-hosted-cluster.md](steps-hosted-cluster.md)
- [9. Verify](#9-verify)
- [10. Workload](#10-workload)
  - [Storage passthrough: a PVC from hub1's storage](#storage-passthrough-a-pvc-from-hub1s-storage)
- [hcp-kv2: workers on the SNO (optional)](#hcp-kv2-workers-on-the-sno-optional)
- [hcp-kv3: control plane on sno3, VMs on the SNO, managed from hub1 (optional)](#hcp-kv3-control-plane-on-sno3-vms-on-the-sno-managed-from-hub1-optional)
- [MetalLB ingress instead (optional)](#metallb-ingress-instead-optional)
- [Cleanup](#cleanup)

## 1. Prepare

```bash
subscription-manager register
yum install ansible-core -y
ansible-galaxy collection install community.libvirt
ansible-galaxy collection install community.crypto

git clone https://github.com/sadiquepp/ocp-onpremise.git
cd ocp-onpremise
mkdir -p /opt/lab-images
cp rhel-9.8-x86_64-kvm.qcow2 /opt/lab-images/   # base_image_dir in vars.yaml
```

## 2. Credentials and lab variables

```bash
ansible-vault create vault.yaml
```

```yaml
org_id: XXXX
activation_key: YYYYY
pull_secret: 'ZZZZZ...'
ssh_key:
ceph_dashboard_password: '...'
```

The `oadp_*` keys from ../steps.md are only needed to back the cluster up;
add them if you will.

In `vars.yaml`:

```yaml
use_lvm_storage: false        # hub on Ceph, not LVM Storage
```

`hcp-kv1` is already in `vars.yaml` (`hosted_clusters`, with
`platform: kubevirt`), so the next steps give it its API name and API address
pool along with everything else.

## 3. Bare metal host

```bash
ansible-playbook -i inventory/hosts setup_bm_host.yaml --ask-vault-pass

cat /sys/module/kvm_intel/parameters/nested              # Y   (kvm_amd on AMD)
dig +short api.hcp-kv1.hub.mylab.com @192.168.122.21     # 192.168.122.250
```

If `nested` is `N`, reboot the host now, before any VM exists: this step wrote
`/etc/modprobe.d/kvm-nested.conf`, and it applies when the module loads.

## 4. Ceph cluster

```bash
ansible-playbook -i inventory/hosts setup_ceph.yaml --ask-vault-pass
ssh root@192.168.122.27 ceph -s          # HEALTH_OK, 3 mons, 9 OSDs
```

## 5. Hub1

```bash
ansible-playbook -i inventory/hosts setup_hub_cluster.yaml --ask-vault-pass \
  -e hub_disk_cache_unsafe=true

export KUBECONFIG=/var/lib/libvirt/images/hub_install/auth/kubeconfig
oc get ipaddresspool hcp-kv1-api-pool -n metallb-system \
  -o jsonpath='{.spec.addresses}{"\n"}'  # ["192.168.122.250-192.168.122.250"]
```

`hub_disk_cache_unsafe=true` builds hub1's disks with `cache=unsafe`. It is off
by default; on this lab's spinning disks hub1's etcd is too slow for the hosted
control planes without it. The cost is hub1's disks if the host crashes:
[README, hub1's disks](README.md#hub1s-disks-cacheunsafe).

> **Testing hcp-kv2 or hcp-kv3 too? Start their SNOs now, in parallel.**
> Each install takes the better part of an hour, and none of them needs
> hub1. Step 3 already gave both SNOs their DNS and DHCP entries, and each
> build keeps its own installer in its own folder. In two more terminals
> (tmux), from the repository root:
>
> ```bash
> # the SNO - hcp-kv2's and hcp-kv3's worker VMs (hcp-kv2 section below)
> ansible-playbook -i inventory/hosts setup_sno.yaml --ask-vault-pass \
>   -e sno_lvm_storage=true -e sno_memory=98304 -e sno_vcpus=16
>
> # sno3 - hcp-kv3's control plane: build, MCE and MetalLB, all without hub1
> cd kubevirt
> ansible-playbook -i ../inventory/hosts setup_sno3_mce.yaml --ask-vault-pass --tags sno,mce,metallb
> ```
>
> Check the host has the memory first (`free -g`): 96 GiB for the SNO and
> 40 GiB for sno3, on top of hub1 and Ceph. Three installs at once also share
> the host's disks, so each runs a little slower than on its own.
>
> sno3's import into hub1's ACM waits for hub1. The hcp-kv3 section below runs
> the whole `setup_sno3_mce.yaml`: it skips the build, finds MCE and MetalLB
> already there, and does the import. Likewise the hcp-kv2 section's
> `setup_sno.yaml` is then already done.

## 6. Attach Ceph to hub1

```bash
export KUBECONFIG=/var/lib/libvirt/images/hub_install/auth/kubeconfig
ansible-playbook -i inventory/hosts setup_ceph_odf.yaml --ask-vault-pass
oc get storagecluster -n openshift-storage
oc get sc                                 # ocs-external-storagecluster-ceph-rbd (default)
```

No AgentServiceConfig, bminfra or libvirt worker VMs: those are for Agent
clusters. The NodePool creates this cluster's workers itself.

## 7. Check the hub can run the workers

```bash
oc get nodes -o wide                      # 3 masters, 3 workers, Ready
oc debug node/worker1.hub.mylab.com -- grep -c -E 'vmx|svm' /proc/cpuinfo   # > 0
```

## 8. OpenShift Virtualization and the hosted cluster

```bash
cd kubevirt
ansible-playbook -i ../inventory/hosts setup_hosted_cluster_kubevirt.yaml --ask-vault-pass
```

On hub1, this:
- re-checks steps 3-7;
- installs OpenShift Virtualization and lets the router admit wildcard routes;
- renders and applies hcp-kv1's HostedCluster and NodePool;
- waits for both worker VMs to join as Ready nodes.

It takes 30-60 minutes, most of it the nested workers booting. The rendered
files are in `kubevirt/rendered/hcp-kv1/`.

hcp-kv2 is in `vars.yaml` too, but its workers go on the SNO: until that is
built, this run says "Leaving out hcp-kv2" and builds hcp-kv1 alone. Once the
SNO exists, a plain run handles both.

To do this one step at a time instead, by tag or by hand with `oc`, see
[steps-hosted-cluster.md](steps-hosted-cluster.md).

## 9. Verify

```bash
export KUBECONFIG=/var/lib/libvirt/images/hub_install/auth/kubeconfig
oc get hostedcluster,nodepool -n hcp-kv1
oc get vm,vmi,dv -n hcp-kv1-hcp-kv1                # the workers, from the hub

export KUBECONFIG=/var/lib/libvirt/images/hcp-kv1/kubeconfig
oc get nodes                                       # 2 Ready
oc get co | grep -v 'True.*False.*False'
oc get storageclass                                # a kubevirt-csi class, backed by hub1's Ceph RBD
curl -kI https://console-openshift-console.apps.hcp-kv1.apps.hub.mylab.com
```

`*.apps.hcp-kv1.apps.hub.mylab.com` reaches the hosted cluster through hub1's
router, so it needs no DNS record of its own.

## 10. Workload

```bash
export KUBECONFIG=/var/lib/libvirt/images/hcp-kv1/kubeconfig
oc apply -f ../hello-openshift.yaml

oc apply -k https://github.com/sadiquepp/openshift/test-workloads/online-boutique/overlays/no-loadgenerator
oc get pods -n online-boutique
oc get route frontend -n online-boutique -o jsonpath='{.spec.host}{"\n"}'
```

### Storage passthrough: a PVC from hub1's storage

`hello-openshift-pvc.yaml` is hello-openshift with its page on a PVC. The PVC
uses `kubevirt-csi-infra-default`, which kubevirt-csi turns into a disk on
hub1's default StorageClass (Ceph RBD here) hotplugged into the worker VM.
Each pod start appends a line to the page, so it shows whether the data
followed the pod:

```bash
export KUBECONFIG=/var/lib/libvirt/images/hcp-kv1/kubeconfig
oc apply -f hello-openshift-pvc.yaml
oc get pvc -n hello-pvc                       # Bound, kubevirt-csi-infra-default
oc get pod -n hello-pvc -o wide               # Running - note the NODE
URL=https://$(oc get route hello-openshift -n hello-pvc -o jsonpath='{.spec.host}')
curl -k $URL                                  # one line: pod and node

# the same volume, seen from hub1
HUB=/var/lib/libvirt/images/hub_install/auth/kubeconfig
oc --kubeconfig $HUB get dv,pvc -n hcp-kv1-hcp-kv1   # a new pvc-... beside the two -rhcos disks, on ocs-external-storagecluster-ceph-rbd
oc --kubeconfig $HUB get vmi -n hcp-kv1-hcp-kv1 \
  -o jsonpath='{range .items[*]}{.metadata.name}{"\t"}{.status.volumeStatus[*].name}{"\n"}{end}'   # hotplugged into one VM
```

Then move the pod to the other worker. The disk detaches from one VM and
attaches to the other:

```bash
NODE=$(oc get pod -n hello-pvc -o jsonpath='{.items[0].spec.nodeName}')
oc adm cordon $NODE
oc delete pod -n hello-pvc -l app=hello-openshift
oc get pod -n hello-pvc -o wide -w            # Running on the other node
curl -k $URL                                  # two lines, two nodes: same data
oc adm uncordon $NODE
oc delete -f hello-openshift-pvc.yaml         # the PVC goes, and its disk on hub1 with it
```

On a hub built with LVM Storage instead of Ceph the first half works the
same, but the move fails with an attach error whenever the two worker VMs
run on different hub workers: the disk lives on one hub worker's local
storage, and only a VM on that worker can attach it.

## hcp-kv2: workers on the SNO (optional)

A second hosted cluster beside hcp-kv1: its control plane on hub1 too, its
worker VMs on the single-node cluster. Explanation:
[README.md](README.md#workers-on-another-cluster-hcp-kv2-on-the-sno).

From the repository root. The SNO needs LVM Storage (the Ceph flow turns it
off) and room for the workers beside OpenShift and OpenShift Virtualization:
96 GiB for four 12 GiB workers, hcp-kv2's two and hcp-kv3's two. Check the
host has it (`free -g`, "available"). For hcp-kv2 alone, 64 GiB
(`-e sno_memory=65536`) is enough. Started in
[step 5](#5-hub1)? Then only the checks below are left:

```bash
ansible-playbook -i inventory/hosts setup_sno.yaml --ask-vault-pass \
  -e sno_lvm_storage=true -e sno_memory=98304 -e sno_vcpus=16

export KUBECONFIG=/var/lib/libvirt/images/sno_install/auth/kubeconfig
oc get nodes                                  # sno Ready
oc get sc                                     # lvms-vg1 (default)
```

LVMS's disk is `sno_storage_disk_gb`, 512 GB, thin on the host. An SNO built
with a smaller one: [README, Grow the SNO's storage disk](README.md#grow-the-snos-storage-disk).

hcp-kv2's API name and API pool. A lab built from steps 3-5 already has
them; on a hub1 built before hcp-kv2 was in `vars.yaml`, render both again:

```bash
ansible-playbook -i inventory/hosts setup_bm_host.yaml --tags dns --ask-vault-pass
ansible-playbook -i inventory/hosts setup_hub_cluster.yaml --tags metallb --ask-vault-pass

dig +short api.hcp-kv2.hub.mylab.com @192.168.122.21     # 192.168.122.252
oc --kubeconfig /var/lib/libvirt/images/hub_install/auth/kubeconfig \
  get ipaddresspool hcp-kv2-api-pool -n metallb-system \
  -o jsonpath='{.spec.addresses}{"\n"}'                  # ["192.168.122.252-192.168.122.252"]
```

Then the cluster - OpenShift Virtualization and wildcard routes go on the SNO
this time, the control plane on hub1:

```bash
cd kubevirt
ansible-playbook -i ../inventory/hosts setup_hosted_cluster_kubevirt.yaml --ask-vault-pass \
  -e kubevirt_cluster=hcp-kv2
```

Verify:

```bash
export KUBECONFIG=/var/lib/libvirt/images/hub_install/auth/kubeconfig
oc get hostedcluster,nodepool -n hcp-kv2
oc get pods -n hcp-kv2-hcp-kv2 | grep -E 'etcd|kube-apiserver'   # control plane: hub1

export KUBECONFIG=/var/lib/libvirt/images/sno_install/auth/kubeconfig
oc get vm,vmi,dv -n hcp-kv2-hcp-kv2                              # workers: the SNO
oc get route -n hcp-kv2-hcp-kv2                                  # the *.apps passthrough

export KUBECONFIG=/var/lib/libvirt/images/hcp-kv2/kubeconfig
oc get nodes
curl -kI https://console-openshift-console.apps.hcp-kv2.apps.sno.mylab.com
```

Remove it with `--tags destroy -e kubevirt_cluster=hcp-kv2`; that also clears
its namespace and ServiceAccount off the SNO.

## hcp-kv3: control plane on sno3, VMs on the SNO, managed from hub1 (optional)

Three clusters: hub1's ACM manages, a second SNO (`sno3`) runs MCE alone and
hosts hcp-kv3's control plane, and the SNO from the hcp-kv2 section runs its
worker VM. Explanation:
[README.md](README.md#three-clusters-acm-on-hub1-mce-on-sno3-vms-on-the-sno-hcp-kv3).
The SNO first (the hcp-kv2 section above), then, from the repository root,
sno3's DNS zone and hcp-kv3's API name - under `hub.mylab.com`, so no network
rebuild:

```bash
ansible-playbook -i inventory/hosts setup_bm_host.yaml --tags dns --ask-vault-pass
dig +short api.sno3.hub.mylab.com @192.168.122.21        # 192.168.122.63
dig +short api.hcp-kv3.hub.mylab.com @192.168.122.21     # 192.168.122.253
```

sno3 - built, MCE and MetalLB on it, and imported into hub1 with hosted-cluster
discovery (40 GiB / 12 vCPU, about an hour). Started in [step 5](#5-hub1)? The
same command then skips the build and only does the import:

```bash
cd kubevirt
ansible-playbook -i ../inventory/hosts setup_sno3_mce.yaml --ask-vault-pass

export KUBECONFIG=/var/lib/libvirt/images/sno3_install/auth/kubeconfig
oc get multiclusterengine                                 # Available
oc -n hypershift get deploy operator                      # the HyperShift operator
oc -n metallb-system get ipaddresspool                    # hcp-kv3-api-pool

export KUBECONFIG=/var/lib/libvirt/images/hub_install/auth/kubeconfig
oc get managedcluster sno3 sno                            # both JOINED, AVAILABLE True
oc get managedclusteraddon -n sno3                        # hypershift-addon Available
```

hcp-kv3 - the KubeVirt playbook, against sno3:

```bash
ansible-playbook -i ../inventory/hosts setup_hosted_cluster_kubevirt.yaml --ask-vault-pass \
  -e kubevirt_mgmt=sno3
```

Verify, on each of the three:

```bash
oc --kubeconfig /var/lib/libvirt/images/sno3_install/auth/kubeconfig \
  get hostedcluster,nodepool -n hcp-kv3                   # control plane: sno3
oc --kubeconfig /var/lib/libvirt/images/sno_install/auth/kubeconfig \
  get vm,vmi -n hcp-kv3-hcp-kv3                           # 2 worker VMs: the SNO
oc --kubeconfig /var/lib/libvirt/images/hub_install/auth/kubeconfig \
  get discoveredcluster -n sno3                           # hub1 found it ...
oc --kubeconfig /var/lib/libvirt/images/hub_install/auth/kubeconfig \
  get managedcluster sno3-hcp-kv3                         # ... and manages it
curl -kI https://console-openshift-console.apps.hcp-kv3.apps.sno.mylab.com
```

Remove it with `--tags destroy -e kubevirt_mgmt=sno3`. sno3 goes with
`cleanup.yaml --tags sno3`, from the repository root (see Cleanup).
That also takes anything hcp-kv3 left behind: its ManagedClusters on hub1,
its VM namespace on the SNO.

## MetalLB ingress instead (optional)

This serves `*.apps.hcp-kv1.hub.mylab.com` on `192.168.122.251`, from MetalLB
inside the hosted cluster, with the workers on the lab network through a
localnet NAD. `baseDomainPassthrough` can't be changed after creation, so a
cluster built the default way is destroyed first:

```bash
cd kubevirt
ansible-playbook -i ../inventory/hosts setup_hosted_cluster_kubevirt.yaml --ask-vault-pass \
  --tags destroy -e kubevirt_cluster=hcp-kv1
ansible-playbook -i ../inventory/hosts setup_hosted_cluster_kubevirt.yaml --ask-vault-pass \
  -e kubevirt_ingress=metallb

dig +short x.apps.hcp-kv1.hub.mylab.com @192.168.122.21  # 192.168.122.251
curl -kI https://console-openshift-console.apps.hcp-kv1.hub.mylab.com
```

To keep it that way, set `ingress: metallb` on hcp-kv1's `hosted_clusters`
entry in `vars.yaml`.

## Cleanup

```bash
cd kubevirt
ansible-playbook -i ../inventory/hosts setup_hosted_cluster_kubevirt.yaml --ask-vault-pass \
  --tags destroy
ansible-playbook -i ../inventory/hosts setup_hosted_cluster_kubevirt.yaml --ask-vault-pass \
  --tags destroy -e kubevirt_mgmt=sno3          # hcp-kv3, if built

cd ..
ansible-playbook -i inventory/hosts cleanup-hub.yaml
ansible-playbook -i inventory/hosts cleanup-ceph.yaml
ansible-playbook -i inventory/hosts cleanup.yaml
```

`--tags destroy` removes hcp-kv2 as well while the SNO is up, its SNO side
included - run it before the SNO goes. `cleanup.yaml` removes the SNO with
everything else; `cleanup.yaml --tags sno` removes only it.

`cleanup.yaml` removes sno3 too; `cleanup.yaml --tags sno3` removes only it:
- **On hub1:** its ManagedCluster, and `sno3-hcp-kv3`.
- **On the SNO:** hcp-kv3's VM namespace and ClusterRole.
- **On the host:** the VM and its disks, `sno3/` and `sno3_install/`.
- **In kubevirt/:** the rendered files for sno3 and hcp-kv3.

Each cluster is only touched while it still answers, so it is safe in
any order. The `--tags destroy -e kubevirt_mgmt=sno3` above is still the
clean way to remove hcp-kv3: it deletes the HostedCluster on sno3 before
the VM goes.
