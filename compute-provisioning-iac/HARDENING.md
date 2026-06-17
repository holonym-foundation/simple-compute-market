# Container host hardening (tenant isolation)

Tenants get an **interactive terminal** into their container, so each tenant is
treated as **hostile code**. The model: *maximal freedom inside the sandbox, hard
walls around it* — a tenant may do anything to **their own** container (install
deps, run a server on their allotted resources, break their own agent), but must
never be able to touch **another tenant, the host, or AEX's keys/network reputation**.

The `container-management` role applies the **per-container** controls
(`cap_drop: ALL`, non-root `user`, `read_only` rootfs + tmpfs, `no-new-privileges`,
mem/cpu/pids caps, per-tenant named home volume). The controls below are
**daemon/host-level** — they are NOT settable per container and MUST be configured
on every provider host (aex-native-scm and any `-01/02/03` scale-out).

## 1. user-namespace remap (the primary cross-tenant defense)
Without this, container-root == host-root, so a container escape reads **every
tenant's home volume** off `/var/lib/docker/volumes`. With it, container-root maps
to an unprivileged host UID — and the per-tenant non-root `user` (10001 by default)
means a tenant's files on disk are owned by a UID a sibling cannot read even after
an escape.

`/etc/docker/daemon.json`:
```json
{ "userns-remap": "default" }
```
Restart docker. (Pull the agent base image after enabling — remap re-namespaces image storage.)

## 2. Keep the default seccomp + AppArmor profiles
Never run tenant containers `--privileged` and never `--security-opt seccomp=unconfined`.
The default profiles block the bulk of escape syscalls. The role already sets
`no-new-privileges` and drops all capabilities.

## 3. Egress controls (network-reputation + legal exposure)
A tenant can "run a server" → spam, port-scanning, crypto-mining, or hosting illegal
content, all from **AEX's IPs**. Open egress is good UX (agents call arbitrary APIs),
so **open-but-watched**, not allowlisted:
- Rate-limit new outbound connections per container in the `DOCKER-USER` iptables chain.
- Block obvious abuse (SMTP/25 egress, RFC1918 lateral movement to the host/other tenants' subnets, metadata endpoints `169.254.169.254`).
- Monitor for mining CPU signatures, scan bursts, and abuse complaints; the deploy kill-switch + `container-stop` are the response.

Minimum `DOCKER-USER` baseline (block lateral + metadata, rate-limit the rest):
```
iptables -I DOCKER-USER -d 169.254.169.254 -j DROP
iptables -I DOCKER-USER -d 172.16.0.0/12 -m conntrack --ctstate NEW -j DROP   # host/sibling lateral
iptables -I DOCKER-USER -p tcp --dport 25 -j DROP                              # SMTP egress
```

## 4. Per-tenant disk quota
The home volume must be quota-capped (project quota on the volumes filesystem, or a
sized volume driver) so one tenant cannot fill the host disk and DoS the others.

## 5. Never mount the docker socket into a tenant
`/var/run/docker.sock` in a tenant = instant host takeover. No tenant container should
ever bind-mount it.

## Cross-tenant home-volume guarantee (defense-in-depth)
- **Mount-level:** each tenant container mounts ONLY its own `<name>-home` volume.
- **Process-level:** non-root `user` + `cap_drop: ALL` + `no-new-privileges` + default seccomp → escape is hard.
- **Host-level:** `userns-remap` → even container-root != host-root, and per-tenant UIDs mean a sibling's volume files are unreadable on disk.
- **Secrets:** the tenant's wallet is **WaaP/MPC** — at most one key *share* / a spend-capped session token lives in the home volume, never a full private key, so reading it grants nothing beyond the tenant's own already-capped authority.
