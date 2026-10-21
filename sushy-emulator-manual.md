# sushy-emulator: manual setup

The steps `roles/setup-sushy-emulator` automates, as commands to run by hand
on the bare-metal host. The result is the same: each hosted cluster worker
VM gets a Redfish BMC with virtual media at

```
redfish-virtualmedia+http://192.168.122.1:8000/redfish/v1/Systems/<vm-uuid>
```

which is what the BareMetalHost's `bmc.address` points at. Background:
`hosted_cluster_worker_provisioning` in `vars.yaml`, and
[oadp/BMH-DR-test.md](oadp/BMH-DR-test.md).

Run everything as root on the bare-metal host. The values below are the
repo's defaults: `lab_network_prefix` 192.168.122, the helper at .21, port
8000, user `admin`.

```bash
LISTEN_IP=192.168.122.1          # virbr0 gateway: every hub node reaches it
PORT=8000
DNS=192.168.122.21               # the helper: resolves the hubs' *.apps names
BMC_USER=admin
BMC_PASS='<8+ characters>'       # the same value as sushy_emulator_password in vault.yaml
IMAGE=quay.io/metal3-io/sushy-tools:latest
WORKERS="c1_worker1 c1_worker2 c1_worker3"
```

## 1. Prerequisite: the worker VMs exist

sushy only exposes VMs that are defined in libvirt. For BareMetalHosts they
should be **defined but powered off**, with an **empty CD-ROM**: the hub
powers them on and inserts the discovery ISO itself.

To create just the VMs that way, without sushy or the BareMetalHosts:

```bash
ansible-playbook -i inventory/hosts setup_hosted_cluster_vm.yaml \
  -e hosted_cluster_worker_provisioning=bmh --tags bmh-vms
```

It creates each disk only if it's missing, so re-running it can't wipe a
worker. It defines a VM only if it isn't defined yet. It needs neither the
vault nor a hub. (Without `--tags`, the same command goes on to set up
sushy and the BareMetalHosts, which is the automated path.)

To check:

```bash
for vm in $WORKERS; do virsh domstate $vm; done        # shut off
virsh domblklist c1_worker1 --details | grep cdrom     # cdrom with no source (-)
```

## 2. Install podman and htpasswd

```bash
dnf install -y podman httpd-tools
podman pull $IMAGE
```

## 3. Collect the VMs' UUIDs

A Redfish System id is the libvirt domain UUID. The allow-list in step 5
uses them, and so do the BareMetalHosts.

```bash
for vm in $WORKERS; do echo "$vm $(virsh domuuid $vm)"; done
UUIDS=$(for vm in $WORKERS; do virsh domuuid $vm; done | sed '/^$/d')
```

## 4. BMC credentials

The emulator's basic auth accepts bcrypt hashes only, hence `-B`:

```bash
mkdir -p /etc/sushy && chmod 750 /etc/sushy
htpasswd -bBc /etc/sushy/htpasswd "$BMC_USER" "$BMC_PASS"
chmod 640 /etc/sushy/htpasswd
```

## 5. Emulator config

`SUSHY_EMULATOR_ALLOWED_INSTANCES` restricts the BMCs to the worker VMs.
Without it, every domain on the host (the hubs, the helper, everything)
would get a BMC anyone with the password could power-cycle. Never leave it
empty.

```bash
ALLOWED=$(printf "'%s', " $UUIDS | sed 's/, $//')

cat > /etc/sushy/sushy-emulator.conf <<EOF
SUSHY_EMULATOR_LISTEN_IP = '0.0.0.0'
SUSHY_EMULATOR_LISTEN_PORT = 8000
SUSHY_EMULATOR_SSL_CERT = None
SUSHY_EMULATOR_SSL_KEY = None
SUSHY_EMULATOR_OS_CLOUD = None
SUSHY_EMULATOR_LIBVIRT_URI = 'qemu:///system'
SUSHY_EMULATOR_IGNORE_BOOT_DEVICE = False
SUSHY_EMULATOR_VMEDIA_VERIFY_SSL = False
SUSHY_EMULATOR_AUTH_FILE = '/etc/sushy/htpasswd'
SUSHY_EMULATOR_ALLOWED_INSTANCES = [$ALLOWED]
EOF
chmod 640 /etc/sushy/sushy-emulator.conf
cat /etc/sushy/sushy-emulator.conf | tail -1     # three UUIDs
```

Why each setting:
- `LISTEN_IP 0.0.0.0` and port 8000 are **inside** the container. The unit in
  step 6 publishes it on `$LISTEN_IP:$PORT` only.
- No TLS: the BareMetalHosts use `redfish-virtualmedia+http://`, on a network
  private to this host.
- `IGNORE_BOOT_DEVICE False`: virtual media boot depends on Ironic setting a
  one-time boot from CD, then from disk.
- `VMEDIA_VERIFY_SSL False`: sushy downloads the ISO from the hub, whose
  certificate the container doesn't trust.

## 6. systemd unit

```bash
cat > /etc/systemd/system/sushy-emulator.service <<EOF
[Unit]
Description=sushy-emulator: Redfish BMCs for the hosted cluster worker VMs
Wants=network-online.target
After=network-online.target libvirtd.service

[Service]
Restart=on-failure
RestartSec=10
ExecStartPre=-/usr/bin/podman rm -f sushy-emulator
ExecStart=/usr/bin/podman run --rm --name sushy-emulator \\
  --security-opt label=disable \\
  --publish $LISTEN_IP:$PORT:8000 \\
  --dns $DNS \\
  --volume /etc/sushy:/etc/sushy:ro \\
  --volume /var/run/libvirt:/var/run/libvirt \\
  $IMAGE \\
  sushy-emulator --config /etc/sushy/sushy-emulator.conf
ExecStop=/usr/bin/podman stop -t 10 sushy-emulator

[Install]
WantedBy=multi-user.target
EOF
grep -E 'publish|dns' /etc/systemd/system/sushy-emulator.service   # values filled in
```

Why:
- `--security-opt label=disable`: the container uses the libvirt socket under
  `/var/run/libvirt`, which SELinux would otherwise deny.
- `--dns` set to the helper: the BMC side, meaning sushy, downloads the
  discovery ISO from the hub's image service, so it must resolve the hubs'
  `*.apps` names. libvirt's dnsmasq on virbr0 won't work here: it only
  answers queries arriving on virbr0.

## 7. Firewall

Only needed when firewalld runs. Open the port in the zone virbr0 belongs to,
since that's where the hubs' Ironic connects from:

```bash
systemctl is-active firewalld && {
  ZONE=$(firewall-cmd --get-zone-of-interface=virbr0 || echo libvirt)
  firewall-cmd --zone=$ZONE --add-port=$PORT/tcp
  firewall-cmd --permanent --zone=$ZONE --add-port=$PORT/tcp
}
```

## 8. Start it

```bash
systemctl daemon-reload
systemctl enable --now sushy-emulator
systemctl status sushy-emulator --no-pager
journalctl -u sushy-emulator -n 20 --no-pager
```

## 9. Verify

The same request Ironic makes first. It checks the port, the credentials and
the allow-list together:

```bash
curl -s -u "$BMC_USER:$BMC_PASS" http://$LISTEN_IP:$PORT/redfish/v1/Systems | python3 -m json.tool
#   Members: exactly the three worker UUIDs from step 3, nothing else

curl -s -o /dev/null -w '%{http_code}\n' http://$LISTEN_IP:$PORT/redfish/v1/Systems
#   401 without credentials

UUID=$(virsh domuuid c1_worker1)
curl -s -u "$BMC_USER:$BMC_PASS" http://$LISTEN_IP:$PORT/redfish/v1/Systems/$UUID \
  | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["Name"], d["PowerState"], d["Boot"])'
curl -s -u "$BMC_USER:$BMC_PASS" http://$LISTEN_IP:$PORT/redfish/v1/Managers/$UUID/VirtualMedia | python3 -m json.tool
#   virtual media devices (Cd) listed
```

From a hub node, to prove the hub side can reach it:

```bash
oc debug node/<hub-node> -- chroot /host \
  curl -s -o /dev/null -w '%{http_code}\n' -u "$BMC_USER:$BMC_PASS" http://$LISTEN_IP:$PORT/redfish/v1/Systems
#   200
```

And that sushy can resolve the hub's image service, which it needs for
virtual media:

```bash
podman exec sushy-emulator getent hosts \
  assisted-image-service-multicluster-engine.apps.<hub-domain>
```

## 10. Next: the BareMetalHosts

Per worker, in the agent namespace: a BMC secret, and a BareMetalHost whose
`bmc.address` is
`redfish-virtualmedia+http://$LISTEN_IP:$PORT/redfish/v1/Systems/<uuid>`.
`setup_hosted_cluster_vm.yaml -e hosted_cluster_worker_provisioning=bmh`
renders them from `roles/setup-bminfra/templates/05-baremetalhosts.yaml.j2`.
That template is also the reference if you create them by hand: label
`infraenvs.agent-install.openshift.io: <infraenv>`, annotations
`inspect.metal3.io: disabled` and
`bmac.agent-install.openshift.io/hostname`, plus `bootMode: legacy`,
`automatedCleaningMode: disabled` and `online: true`.

## Day 2

| Task | Command |
|---|---|
| A worker VM was rebuilt (new UUID) | Redo steps 3 and 5, then `systemctl restart sushy-emulator`, and update that BareMetalHost's `bmc.address` |
| Change the password | Step 4 with the new value, `systemctl restart sushy-emulator`, and update the BMC secrets on the hub |
| Watch what the hubs do | `journalctl -u sushy-emulator -f \| grep -E '"(POST\|PATCH\|DELETE) '` (power actions and virtual media) |
| Remove it | `systemctl disable --now sushy-emulator; rm -rf /etc/systemd/system/sushy-emulator.service /etc/sushy; systemctl daemon-reload`, then close the firewall port. Or `ansible-playbook -i inventory/hosts cleanup-sushy-emulator.yaml` |
