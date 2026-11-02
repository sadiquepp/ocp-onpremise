# MinIO: a local S3 store for the disconnected lab

A disconnected deployment cannot reach AWS S3, but plenty of things in it want
an object store: OADP backups, and later Loki, Tempo or anything else that
speaks S3. `setup_minio.yaml` builds one on the lab network - a `minio` VM
running MinIO - with one bucket per consumer.

It is shared infrastructure, like the [mirror registry](../README.md#setup-mirror-registry-disconnected-only):
built once, outside any cluster, and still reachable from clusters whose
egress to the internet has been cut (the disconnected nodes' egress rules
exempt the whole lab network).

## Contents

- [What lives here](#what-lives-here)
- [What gets built](#what-gets-built)
- [Build it](#build-it)
- [Buckets](#buckets)
- [Using it](#using-it)
- [Cleanup](#cleanup)

## What lives here

| File | Purpose |
| --- | --- |
| `setup_minio.yaml` | Create the `minio` VM, register it, install MinIO and create the buckets in `minio_buckets` |
| `ansible.cfg` | Mirrors the root `ansible.cfg` and adds `../roles` to `roles_path` |

The roles stay in the shared [`roles/`](../roles/) directory
(`setup-minio-vm`, `setup-rhsm`, `setup-minio`), and the playbook shares
`vars.yaml`, `vault.yaml` and `inventory/hosts` with the rest of the
repository. **Run it from inside this directory** - the `ansible.cfg` here is
what puts `../roles` on the role search path:

```bash
cd minio
ansible-playbook -i ../inventory/hosts setup_minio.yaml --ask-vault-pass
```

## What gets built

| | |
| --- | --- |
| VM | `minio`, `192.168.122.23` (`ip_list.minio`), 4 vCPU / 4 GiB, a 100G disk expanded from the RHEL 9 base image (`minio_*` in `vars.yaml`) |
| Name | `minio.<hub_domain>` (`minio_domain`) |
| S3 API | `http://minio.<hub_domain>:9000` (`minio_api_port`) |
| Console | `http://minio.<hub_domain>:9001` (`minio_console_port`) |
| Data | `/data/minio` on the VM (`minio_data_dir`) |
| Service | `minio` systemd unit, plus the `mc` client aliased as `local` on the VM |

Plain HTTP - there is no TLS on the endpoint yet, so a consumer has to be
configured for an insecure/HTTP S3 endpoint.

## Build it

Before running it:

- `disconnected_install: true` in `vars.yaml` - the playbook refuses to run
  otherwise, so the connected lab is never affected.
- `setup_bm_host.yaml` (repository root) has run: the VM is built from the base
  image it prepares, and the helper serves its DNS name.
- In `vault.yaml`: `minio_root_user` / `minio_root_password` (MinIO's root
  credentials) and `org_id` / `activation_key` (to register the VM with
  subscription-manager). The playbook asserts all four.

```bash
ansible-playbook -i ../inventory/hosts setup_minio.yaml --ask-vault-pass
```

Tags, to re-run one part: `minio-vm` (create the VM), `rhsm` (register it),
`minio` (install/configure MinIO and create the buckets).

Check it:

```bash
curl -s -o /dev/null -w '%{http_code}\n' http://192.168.122.23:9000/minio/health/live   # 200
ssh root@192.168.122.23 /usr/local/bin/mc ls local                                    # the buckets
```

## Buckets

`minio_buckets` in `vars.yaml` lists every bucket the role creates - one per
consumer, so their data and access stay separate:

```yaml
minio_buckets:
  - "{{ minio_bucket_name }}"   # adp-backup-disconnected - OADP
  # - loki-chunks
  # - tempo-traces
```

Add a bucket and re-run just the MinIO part; buckets that already exist are
left alone:

```bash
ansible-playbook -i ../inventory/hosts setup_minio.yaml --ask-vault-pass --tags minio
```

`minio_bucket_name` stays its own variable because OADP refers to it, and it is
deliberately distinct from `oadp_bucket_name` (the AWS bucket), so switching a
cluster's backups between the two is a visible choice.

## Using it

Any S3 client points at `http://minio.<hub_domain>:9000` with path-style
addressing, region `us-east-1` (MinIO's default), and either the root
credentials or - better - a MinIO user created per consumer with `mc admin user
add` on the VM.

- **OADP** - [`oadp/`](../oadp/README.md). `setup_oadp.yaml` still renders the
  DataProtectionApplication against AWS S3; pointing it at MinIO (an `s3Url`,
  `s3ForcePathStyle: "true"` and MinIO credentials in `cloud-credentials`) is
  not automated yet.
- **Loki, Tempo, ...** - not wired up yet; each gets its own bucket here and
  its own object-storage secret on the cluster.

## Cleanup

From the repository root - destroys and undefines the VM and deletes its disk,
buckets included:

```bash
ansible-playbook -i inventory/hosts cleanup.yaml --tags minio
```
