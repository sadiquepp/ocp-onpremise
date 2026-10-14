# KubeVirt Hosted Cluster - Steps

Connected lab, Ceph 9 / ODF external storage on hub1, and a hosted cluster
(`hcp-kv1`) whose workers are KubeVirt VMs that its NodePool creates on hub1,
nested on this host. Commands only. Explanations:
[README.md](README.md).

The host, Ceph and hub1 are built exactly as in the backup/restore flow,
[../steps.md](../steps.md). This file only adds what that flow doesn't do:
OpenShift Virtualization on the hub, and the KubeVirt hosted cluster itself.

## 1. Host, Ceph and hub1 - from ../steps.md

Run these from the repository root, unchanged:

| ../steps.md | |
|---|---|
| 1. Prepare | as written |
| 2. S3 bucket and IAM user | only if you will also back the cluster up |
| 3. Credentials and lab variables | as written - `use_lvm_storage: false` is what puts the hub on Ceph |
| 4. Bare metal host | as written |
| 5. Ceph cluster | as written |
| 6. Hub1 | as written |
| 7. Attach Ceph to hub1 | as written |
| 8. ACM inventory | **skip** - AgentServiceConfig and bminfra are for Agent clusters |
| 9. Hosted cluster | **skip** - that is the Agent cluster; this file replaces it |

`hcp-kv1` is already in the root `vars.yaml`, so step 4 publishes
`api.hcp-kv1.hub.mylab.com` and step 6 creates its API address pool on hub1.

## 2. Check the hub can host it

```bash
cat /sys/module/kvm_intel/parameters/nested        # Y   (kvm_amd on AMD)
dig +short api.hcp-kv1.hub.mylab.com @192.168.122.21    # 192.168.122.250

export KUBECONFIG=/var/lib/libvirt/images/hub_install/auth/kubeconfig
oc get ipaddresspool hcp-kv1-api-pool -n metallb-system \
  -o jsonpath='{.spec.addresses}{"\n"}'            # ["192.168.122.250-192.168.122.250"]
oc get sc                                          # ocs-external-storagecluster-ceph-rbd (default)
```

If `nested` is `N`: stop every VM and reboot the host (step 4 wrote
`/etc/modprobe.d/kvm-nested.conf`; it applies when the module loads).

If the pool is missing (a hub built before `hcp-kv1` was in `vars.yaml`):

```bash
ansible-playbook -i inventory/hosts setup_hub_cluster.yaml --tags acm --ask-vault-pass
```

## 3. OpenShift Virtualization and the hosted cluster

```bash
cd kubevirt
ansible-playbook -i ../inventory/hosts setup_hosted_cluster_kubevirt.yaml --ask-vault-pass
```

This does, on hub1: the checks above, OpenShift Virtualization, wildcard
routes on its router; then renders and applies hcp-kv1's HostedCluster and
NodePool, and waits for both workers to be Ready. 30-60 minutes, most of it
the nested workers booting.

Rendered files: `kubevirt/rendered/hcp-kv1/`.

## 4. Verify

```bash
oc get hostedcluster,nodepool -n hcp-kv1
oc get vm,vmi,dv -n hcp-kv1-hcp-kv1                # the workers, from the hub

export KUBECONFIG=/var/lib/libvirt/images/hcp-kv1/kubeconfig
oc get nodes                                       # 2 Ready
oc get co | grep -v 'True.*False.*False'
oc get storageclass                                # a kubevirt-csi class, backed by hub1's Ceph RBD
curl -kI https://console-openshift-console.apps.hcp-kv1.apps.hub.mylab.com
```

`*.apps.hcp-kv1.apps.hub.mylab.com` reaches the hosted cluster through hub1's
router - no DNS record of its own.

## 5. Workload

```bash
export KUBECONFIG=/var/lib/libvirt/images/hcp-kv1/kubeconfig
oc apply -f ../hello-openshift.yaml

oc apply -k https://github.com/sadiquepp/openshift/test-workloads/online-boutique/overlays/no-loadgenerator
oc get pods -n online-boutique
oc get route frontend -n online-boutique -o jsonpath='{.spec.host}{"\n"}'
```

## MetalLB ingress instead (optional)

`*.apps.hcp-kv1.hub.mylab.com` on `192.168.122.251`, from MetalLB inside the
hosted cluster, with the workers on the lab network through a localnet NAD.
`baseDomainPassthrough` is immutable, so a cluster built the default way is
destroyed first:

```bash
ansible-playbook -i ../inventory/hosts setup_hosted_cluster_kubevirt.yaml --ask-vault-pass \
  --tags destroy -e kubevirt_cluster=hcp-kv1
ansible-playbook -i ../inventory/hosts setup_hosted_cluster_kubevirt.yaml --ask-vault-pass \
  -e kubevirt_ingress=metallb

dig +short x.apps.hcp-kv1.hub.mylab.com @192.168.122.21  # 192.168.122.251
curl -kI https://console-openshift-console.apps.hcp-kv1.hub.mylab.com
```

To keep it, set `ingress: metallb` on hcp-kv1's `hosted_clusters` entry.

## Cleanup

```bash
cd kubevirt
ansible-playbook -i ../inventory/hosts setup_hosted_cluster_kubevirt.yaml --ask-vault-pass \
  --tags destroy
```

Then the hub, Ceph and host as in [../steps.md](../steps.md#cleanup).
