# Container host hardening (tenant isolation)

Tenants get an **interactive terminal** into their container, so each tenant is
treated as **hostile code**. The model: *maximal freedom inside the sandbox, hard
walls around it* — a tenant may do anything to **their own** container (install
deps, run a server on their allotted resources, break their own agent), but must
never be able to touch **another tenant, the host, or AEX's keys/network reputation**.

The `container-management` role applies the **per-container** controls
(`read_only` rootfs + **exec** tmpfs, `no-new-privileges`, mem/cpu/pids caps,
per-tenant named home volume; `cap_drop: ALL` + the minimal caps the runtime needs
to drop privileges — see note). The controls below are **daemon/host-level** — NOT
settable per container and MUST be configured on every provider host (aex-native-scm
and any `-01/02/03` scale-out).

> **Why not a forced non-root `--user`:** the AEX agent base runs **s6-overlay as
> PID 1**, which must start as root to set up `/run` and then drop to its own
> `hermes` user (UID 10000). Forcing `--user 10000` makes s6 fail. Container-root
> is safe here because of `userns-remap` (§1), and the agent process ends up as
> hermes after s6's drop. The role therefore leaves `container_user` empty,
> `cap_drop: ALL` + `cap_add: [CHOWN,SETUID,SETGID,DAC_OVERRIDE,FOWNER]` (the caps
> s6 needs to chown /run + drop privileges; shed afterward), and tmpfs `/run`,`/tmp`
> with **exec** (Docker's default tmpfs is `noexec`; s6 execs from `/run`).
> Verified 2026-06 against `ghcr.io/holonym-foundation/aex-agent-base`.

## 1. user-namespace remap (the primary cross-tenant defense)
Without this, container-root == host-root, so a container escape reads **every
tenant's home volume** off `/var/lib/docker/volumes`. With it, container-root maps
to an unprivileged, **per-tenant-distinct** host UID — so a tenant's files on disk
(and the agent's `hermes`-owned state) are owned by a host UID a sibling cannot
read even after a container escape. This — not a forced `--user` — is what gives
the cross-tenant guarantee (the runtime starts s6 as container-root then drops to
hermes internally; see the note above).

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

Minimum `DOCKER-USER` baseline (block metadata + SMTP + host lateral). **Ordering
matters:** `-I` prepends, so the LAST insert is matched FIRST. The docker bridge
subnet must be RETURN-ed *above* any RFC1918/link-local DROP or you break the
container's own gateway/DNS and kill all egress. Applied ruleset (top = first match):
```
# resolve these per host first:
BR_SUBNET=$(docker network inspect bridge -f '{{range .IPAM.Config}}{{.Subnet}}{{end}}')   # e.g. 172.17.0.0/16
HOST_IP=$(ip -4 route get 1.1.1.1 | awk '{print $7; exit}')

iptables -I DOCKER-USER -p tcp --dport 25  -j DROP                 # SMTP egress (anti-spam)
iptables -I DOCKER-USER -p tcp --dport 465 -j DROP
iptables -I DOCKER-USER -p tcp --dport 587 -j DROP
iptables -I DOCKER-USER -d "$HOST_IP/32" -m conntrack --ctstate NEW -j DROP  # block reaching the host
iptables -I DOCKER-USER -d "$BR_SUBNET" -j RETURN                 # ALLOW bridge gw/DNS (must sit above the DROPs below)
iptables -I DOCKER-USER -d 169.254.0.0/16 -j DROP                 # link-local
iptables -I DOCKER-USER -d 169.254.169.254/32 -j DROP             # cloud metadata (re-assert on top, first match)
iptables-save > /etc/iptables/rules.v4   # persist (iptables-persistent)
```
Verify after applying: a fresh container can still reach the internet + chain RPC
but **cannot** reach `169.254.169.254`. **Applied + verified on aex-native-scm
(167.233.97.235) 2026-06.**

## 3b. Inter-container isolation (no sibling↔sibling)
Tenants share the host's default `docker0` bridge, and Docker's inter-container
communication (ICC) is **on by default** — so tenant A can reach tenant B's
container over the network (scan ports, hit B's agent's listeners, exploit/extract
from any service B runs). userns-remap protects the *volume*, not network
reachability. Close it:
```json
// /etc/docker/daemon.json
{ "icc": false }
```
Restart docker, then belt-and-suspenders in `DOCKER-USER` (safe because tenant
DNS resolves via EXTERNAL resolvers, not the bridge gateway, and egress packets
are dst=external so they don't match):
```
iptables -I DOCKER-USER -s 172.17.0.0/16 -d 172.17.0.0/16 -j DROP   # block container<->container
```
Verify: two test containers — B cannot reach A's published port, but both still
reach the internet + chain RPC. **Applied + verified on aex-native-scm 2026-06.**
End-state (future): controlled, platform-rate-limited, *paid* inbound to a
container (per-tenant ingress), not the open shared bridge.

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
