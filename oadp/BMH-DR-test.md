# BareMetalHost workers across a DR restore: test procedure

What happens to hosted cluster workers that the hub manages as
**BareMetalHosts** (with a BMC) when the hosted cluster is restored onto the
DR hub with OADP. This covers what
[OCPBUGS-121709-test.md](OCPBUGS-121709-test.md) did not: there, the workers
booted the discovery ISO by hand, so no hub ever had a BMC it could use to
power-cycle them.

It validates the BareMetalHost guidance in
[hosted-cluster-dr-runbook.md](hosted-cluster-dr-runbook.md) (5.3 and 6.1).

## Why this needs testing

A restored BareMetalHost arrives on the DR hub **without its status**. The DR
hub's Bare Metal Operator (Ironic) holds the host's BMC credentials and may
treat it as a new host: register it, inspect it, or provision it, and so
**reboot a worker that is running production**. Restoring the hosts
`detached` should prevent that. Nobody has measured either behaviour in this
lab.

## Setup: workers as BareMetalHosts

The same three worker VMs as always (`c1_worker1..3`, same MACs and
addresses), but each with a Redfish BMC from sushy-emulator on the bare-metal
host, and a BareMetalHost on the hub. See `hosted_cluster_worker_provisioning`
in `vars.yaml`.

### S1. Vault

```bash
ansible-vault edit vault.yaml
```
```yaml
sushy_emulator_password: '<8+ characters>'
```

### S2. Build the lab up to the InfraEnv

Follow [steps.md](../steps.md) steps 1-8 as usual (hub1, Ceph, ACM, InfraEnv).

### S3. Step 9, with BareMetalHosts

Replace step 9's first command with:

```bash
ansible-playbook -i inventory/hosts setup_hosted_cluster_vm.yaml --ask-vault-pass \
  -e hosted_cluster_worker_provisioning=bmh
```

This:
- defines the three VMs **powered off**;
- makes sure the libvirt storage pool `default` exists (sushy uploads the
  virtual media ISO into it);
- runs sushy-emulator (podman, under systemd) on the bare-metal host, exposing
  only those three VMs, at
  `redfish-virtualmedia+http://192.168.122.1:8000/redfish/v1/Systems/<uuid>`;
- creates a BMC secret and a BareMetalHost per VM in `bminfra`.

To set up sushy-emulator by hand instead, see
[sushy-emulator-manual.md](../sushy-emulator-manual.md).

The hub then powers the VMs on and boots them from the discovery ISO as
virtual media. **There is no Agent approval in the UI**: the BareMetalAgent
controller approves them.

```bash
export KUBECONFIG=/var/lib/libvirt/images/hub_install/auth/kubeconfig
oc get bmh -n bminfra -w          # registering -> provisioning -> provisioned
oc get agents -n bminfra          # three, APPROVED true
systemctl status sushy-emulator   # on the bare-metal host
```

Then create the hosted cluster as in step 9 (`create_hosted_cluster.yaml`,
`oc apply` the rendered file). The NodePool takes two of the three Agents.
The third host (normally `c1worker3`) stays as a **spare**, booted into
discovery. Case 2 uses it.

Continue with steps 10-11 (workload, OADP on hub1).

### S4. Baseline on hub1, before the backup

Record these. Each case compares against them.

```bash
# BareMetalHosts: state, power, and whether BMAC already detached them
oc get bmh -n bminfra -o custom-columns='NAME:.metadata.name,STATE:.status.provisioning.state,ONLINE:.spec.online,POWERED:.status.poweredOn,DETACHED:.metadata.annotations.baremetalhost\.metal3\.io/detached,ERROR:.status.errorType'
oc get bmh -n bminfra -o yaml > bmh-hub1-before-backup.yaml

oc get agents -n bminfra -o custom-columns='NAME:.metadata.name,HOST:.spec.hostname,APPROVED:.spec.approved,CLUSTER:.spec.clusterDeploymentName.name,STAGE:.status.progress.currentStage'

# Boot IDs: they change on every reboot, so they prove whether a worker rebooted
export KUBECONFIG=~/hcp-cluster1.kubeconfig
oc get nodes -o custom-columns='NAME:.metadata.name,BOOTID:.status.nodeInfo.bootID'
```

**Note the `DETACHED` column.** The BareMetalAgent controller may already
detach hosts once their Agent is installed. If it does, the backup carries the
annotation, and the restore keeps it whether or not you ask for it. That
changes what Cases 1 and 3 can show, so record it.

### S5. Backup

Steps 12-13 as usual. Then confirm the BareMetalHosts and their BMC secrets
are in the backup:

```bash
oc exec deploy/velero -c velero -n openshift-adp -- \
  ./velero backup describe hcp-cluster1-backup-csi --details \
  | grep -E 'baremetalhosts|bminfra/c1worker[0-9]-bmc' -A4
```

Then step 14 (shut down hub1). Leave sushy-emulator running: it is on the
bare-metal host, not on a hub, and it is how the DR hub reaches the BMCs.

### S6. During every restore

Watch, in separate terminals, from the moment the Restore is created:

```bash
# Redfish calls the DR hub makes: power actions and virtual media
journalctl -u sushy-emulator -f | grep -E '"(POST|PATCH|DELETE) '

# VM power state on the bare-metal host
watch -n 5 'virsh list --all | grep c1_worker'

# BareMetalHosts on the DR hub
oc get bmh -n bminfra -w
```

A worker reboot shows up as a `POST .../Actions/ComputerSystem.Reset` or
`.../VirtualMedia/.../InsertMedia` for its UUID, a VM briefly leaving
`running`, and a new boot ID on the node.

## Case 1: restore with the BareMetalHosts detached

The runbook's precaution. Expectation: **the DR hub takes no power or virtual
media action on any worker.**

1. Hub2 up to step 20 as in [steps.md](../steps.md), with the fixed hypershift
   plugin image or the import-secret workaround (see
   [OCPBUGS-121709-test.md](OCPBUGS-121709-test.md)).
2. Restore detached:
   ```bash
   ansible-playbook -i inventory/hosts restore_hosted_cluster.yaml --ask-vault-pass \
     -e hcp_cluster_name=hcp-cluster1 -e target_hub=hub2 -e oadp_backup_method=csi \
     -e oadp_restore_detach_bmh=true
   ```
   The playbook applies the `hcp-restore-bmh-detached` modifier ConfigMap and
   references it from the Restore.
3. Steps 22-23 (DNS cutover, verify).
4. Check:
   ```bash
   oc get bmh -n bminfra -o custom-columns='NAME:.metadata.name,STATE:.status.provisioning.state,POWERED:.status.poweredOn,DETACHED:.metadata.annotations.baremetalhost\.metal3\.io/detached,ERROR:.status.errorType'
   export KUBECONFIG=~/hcp-cluster1.kubeconfig
   oc get nodes -o custom-columns='NAME:.metadata.name,BOOTID:.status.nodeInfo.bootID'
   ```
   **Pass:** every BareMetalHost has the detached annotation, there is no
   Redfish `POST`/`PATCH` in the sushy log since the restore, and the boot IDs
   match S4.
5. **Re-attach the installed workers.** Remove the annotation from the two
   hosts in use, one at a time, and watch for 10 minutes each:
   ```bash
   oc annotate bmh c1worker1 -n bminfra baremetalhost.metal3.io/detached-
   ```
   Record what the DR hub does: the state it moves the host to, any Redfish
   calls, and whether the boot ID changes. **This is the question the runbook
   leaves open:** is it safe to re-attach after a restore, or should
   installed workers stay detached?

## Case 2: the DR hub provisions the spare

Expectation: **the DR hub can boot a host from its own InfraEnv and add it to
the hosted cluster.**

After the restore, the spare (`c1worker3`) is still running the discovery
agent it booted from **hub1's** ISO, so it cannot reach hub2's
assisted-service. Re-attaching it should make hub2 boot it from hub2's ISO.

1. Re-attach the spare:
   ```bash
   oc annotate bmh c1worker3 -n bminfra baremetalhost.metal3.io/detached-
   oc get bmh c1worker3 -n bminfra -w
   ```
   Expect a virtual media insert and a reset for its UUID in the sushy log,
   then a new Agent registration on hub2:
   ```bash
   oc get agents -n bminfra -w
   ```
2. Scale the NodePool to take it:
   ```bash
   oc scale nodepool nodepool-hcp-cluster1-1 -n hcp-cluster1 --replicas=3
   oc get nodepool -n hcp-cluster1 -w
   ```
3. **Pass:** the NodePool reaches 3 current nodes, and `c1worker3` is `Ready`
   in the hosted cluster, installed from hub2.

## Case 3: restore with the BareMetalHosts as backed up

No precaution. Expectation: unknown, and that is the point. This shows
whether the precaution in Case 1 is needed, and what happens without it.

This case needs a DR hub that has never seen these hosts. So it is a separate
round: rebuild hub2 (steps 17-20) and restore the **same backup** again, with
`oadp_restore_detach_bmh` left at its default (`false`):

```bash
ansible-playbook -i inventory/hosts restore_hosted_cluster.yaml --ask-vault-pass \
  -e hcp_cluster_name=hcp-cluster1 -e target_hub=hub2 -e oadp_backup_method=csi
```

Watch as in S6, then record:
- each BareMetalHost's states on hub2, in order;
- every Redfish call from the sushy log, with its time and target UUID;
- whether any VM left `running`;
- the boot IDs against S4;
- whether the hosted cluster came back healthy anyway.

If S4 showed the hosts already detached in the backup, Case 3 behaves like
Case 1. Record that as the finding: BMAC's own detach would be what protects
the workers.

## Cleanup

```bash
ansible-playbook -i inventory/hosts cleanup-sushy-emulator.yaml   # after the BareMetalHosts are gone
```

The worker VMs are removed by the usual cleanup playbooks.

## Results

| Item | Value |
|---|---|
| Date | |
| OCP / ACM / MCE / OADP | |
| sushy-tools image | |
| Hosts detached in the backup already (S4) | |
| BareMetalHost states on hub1 before backup | |
| BareMetalHosts and BMC secrets in the backup (S5) | |
| **Case 1** Redfish calls during restore | |
| Case 1 boot IDs unchanged | |
| Case 1 hosted cluster healthy | |
| Case 1 after re-attaching installed workers | |
| **Case 2** spare rebooted into hub2's ISO | |
| Case 2 NodePool 3/3, new node Ready | |
| **Case 3** Redfish calls during restore | |
| Case 3 boot IDs unchanged | |
| Case 3 hosted cluster healthy | |
| Conclusion for the runbook (5.3, 6.1) | |

Notes:

### Evidence

Paste each command and its output here, in the order above.
