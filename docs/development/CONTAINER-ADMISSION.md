# Capability container preparation and admission

This is the SCM half of the trusted, outside-tenant capability handoff. It is
not a credential issuer or evidence that any runtime has been activated.
Integration: [AEX #513](https://github.com/holonym-foundation/aex/issues/513),
parent [AEX #480](https://github.com/holonym-foundation/aex/issues/480),
master [AEX programme](https://github.com/holonym-foundation/internal-docs/issues/2695).

## Protocol

1. Seller selects the actual host and immutable resource image. For capability
   requests, the container name is deterministic from the settled escrow UID.
2. The existing `POST /api/v1/hosts/{host}/containers/` automatically **prepares**
   any create request containing `AEX_CAPABILITY_DIRECTORY`. Preparation saves
   the complete request, SHA-256 digest, stable escrow-derived job ID and a
   one-hour expiry in the existing job database. It does not enqueue, pull an
   image, invoke Ansible, create a volume, or start Docker. Legacy non-capability
   containers and VM submission retain their existing path.
3. The trusted coordinator reads `GET /api/v1/jobs/{job_id}` over private
   authenticated transport. It must verify primary AEX ownership and admission,
   enroll the exact final environment/image/lease, deliver the scoped receiver
   projection and verify the root-protected host mount approval. Request params
   may contain private configuration: never publish the full response as CI
   evidence or logs. The job request digest binds the whole internal request;
   the AEX environment digest separately binds just the final environment.
4. Only after those checks, the coordinator calls
   `POST /api/v1/hosts/{host}/containers/prepared/{job_id}/admit` with
   `{"request_digest":"sha256:…","admission_id":"<UUID>"}` and both
   `X-Admin-Key` and `X-Admission-Key`. `PROVISIONING_CONTAINER_ADMISSION_KEY`
   must be a separate coordinator credential; empty, absent, or equal-to-admin
   configuration disables admission. Never give it to the app, seller or tenant.
   The endpoint checks identity/digest/expiry; it does **not** independently
   prove enrollment. The coordinator and protected host guard supply that proof.
5. Admission durably transitions prepared to queued. The worker atomically
   claims queued to running and rechecks request integrity and expiry before
   Ansible. The host mount guard remains mandatory and is not replaced by a
   digest or an HTTP success. Ordinary seller polling observes the same job.

## Recovery and reconciliation

- Identical preparation returns the existing job; changing host, container,
  image or environment under the same escrow conflicts. A retry never extends
  expiry or creates another job. An expired/cancelled record remains a fence.
- Retry the **same admission UUID and digest** after a lost response/enqueue.
  A queued admission can be re-enqueued; duplicate dispatch cannot claim a
  running/completed job. A different UUID conflicts. No new admission is
  permitted after the preparation expires.
- Worker failure after claim is `uncertain`, with no automatic create retry.
  A crash may leave `running`; neither restart nor repeated admission starts
  it again. Reconcile actual host/volume/lease evidence before any release.
- Capability provisioning timeout/lost response retains the seller allocation
  as `held` (or retains the existing reservation if persisting the hold fails).
  The escrow remains fenced; no fulfillment, usable connection, refund, or
  replacement capacity is claimed. Repeated settle calls do not start new work.
- No automatic cleanup or force-release is introduced. Before the first
  capability submission, the seller freezes a private SQLite continuation:
  original order and encoded demand, negotiated duration, exact allocation,
  deterministic job/container, normalized request digest, and a fixed observation
  deadline (submission snapshot time plus duration). That window never renews.
  The full private request can contain configuration: do not expose this table
  through buyer APIs, logs, or CI evidence.
- The existing repeated settle POST and authenticated status GET can observe
  only that recorded job. They never reserve capacity, create or admit a job,
  sign, register a new lease, or release an allocation. Missing legacy snapshots,
  changed identities, retry metadata, invalid admission/result or lost responses
  provide no recovery authority. A restart preserves the same fence.
- A matching succeeded job records `provisioned_pending_settlement` internally.
  The public escrow remains `provisioning`, its reason explicitly says settlement
  is unverified, and its allocation remains held. No credentials, connection
  details or fulfillment UID are published. Immediate capability success uses
  this same gate and does not run the legacy unjournaled signing tail.
- This observation is not proof of primary enrollment, mounted credentials,
  useful delivery, runtime health now, or a verified chain settlement. A separate
  exact transaction-policy/journal/receipt completion adapter and fixed-deadline
  stop/lease orchestration remain required before marking ready. No continuation
  observation authorizes that adapter; no deployment is implied by these sources.

## Manual retained settlement handoff (source only)

`market_storefront.utils.capability_settlement_handoff` is the manual bridge from
one exact `provisioned_pending_settlement` record to a separately reviewed host
operation. It is not called by settle POST, status GET, a scheduler, or a tenant.
Run it only in a trusted, single-threaded root host process with packaged SCM
code. No sudo/SSH rule, launcher, session, approval producer or installation is
provided by this SCM change. Its two CLI modes are `dispatch` and `observe`;
stdin must be exactly canonical sorted JSON plus newline containing only
`schema: 1` and `operationId: "sha256:<64 lowercase hex>"`.

The deployable source entrypoint is
`storefront/scripts/scm-capability-settlement-handoff`. A separately reviewed
installation must copy that exact wrapper to
`/usr/local/lib/aex-scm-settlement/continue` (root-owned 0555), and pin the hashes
of exactly these four package files below the fixed root-owned, non-writable
`/usr/local/lib/aex-scm-settlement/package` directory:

- `market_storefront/__init__.py`
- `market_storefront/utils/__init__.py`
- `market_storefront/utils/settlement_continuation.py`
- `market_storefront/utils/capability_settlement_handoff.py`

The wrapper executes fixed `/usr/bin/python3 -I -S`, inserts only that protected
package root and runs the manual module. Both package initializers are docstring
only; the two modules use stdlib only. No global SCM virtualenv, mutable seller
checkout, caller `PYTHONPATH`, user site or `.pth` initialization is permitted.
After independent installation/approval review, the invocation shape is
`/usr/local/lib/aex-scm-settlement/continue dispatch` (or `observe`) with the
canonical reference supplied on stdin. This is a trusted host command, never an
instruction to run as root inside the seller container or mount Docker there.
The wrapper source does not install itself or authorize any reference.

The fixed, root-owned, mode-0444 approval is
`/run/aex-scm-authority/continuation.json`, projected by the trusted host boundary.
It has exactly these fields (no caller-supplied transaction, path or environment):

| Fields | Binding |
| --- | --- |
| `schema`, `stage`, `operationId` | Version 1; `fulfill` or `claim`; independently approved operation digest |
| `databasePath` | Exactly `/var/lib/aex-scm/fresh-staging/seller/agent.db`; never the historical seller DB |
| `databaseUid`, `databaseGid` | Positive non-root integers independently verified against the fresh runtime identity; no assumed numeric app UID |
| `escrowUid`, `jobId` | Nonzero lowercase escrow UID and exact retained deterministic provisioning UUID |
| `snapshotDigest`, `requestDigest`, `admissionId`, `resultDigest` | Exact frozen private snapshot, original request, UUIDv4 admission and observed provisioning result |
| `notBefore`, `expiresAt` | Integer Unix seconds, at most one hour, inside the original nonrenewable continuation window |
| `fulfillment` | Null for fulfill; for claim, exact prior `operationId`, nonzero `uid` and `txHash`, `receiptDigest`, `canonicalConfirmationDigest` |

Digest fields are SHA-256 of canonical sorted JSON (UTF-8, compact separators,
no nonfinite numbers), prefixed `sha256:`. The result digest uses the existing
continuation's result SHA-256. Duplicate/unknown approval keys are refused.
Claim approval must come from independently verified canonical fulfillment
receipts; a root-file boolean or the seller's succeeded job is not chain proof.
The host must independently check the ABI, signer, escrow, deployment bindings,
fee budget, global stage/nonce claim and fulfillment receipt before any signing.
This bridge additionally requires a prior retained fulfillment operation and its
matching public transaction hash; it does not itself verify RPC receipts.

All ancestors of the seller leaf must be root-owned, mode 0711 or 0755; the
dedicated seller leaf is the approved UID/GID, mode 0700, and its existing SQLite
file is mode 0600, regular and single-link. Root-private signer directories stay
separate: do not make them traversable to satisfy this database contract.
Canonical no-follow path, inode and ownership checks cover the DB and existing
WAL/SHM/journal files. Each bounded database operation runs in a short child that
permanently clears supplementary groups and drops GID/UID. The root parent never
opens SQLite read-write, never restores child privileges and never creates seller
sidecars as root. Missing files or an unknown UID refuse; no database is created.

A durable intent is committed before the fixed root-owned mode-0555 command
`/usr/local/lib/aex-scm-action-host/dispatch` receives the reference-only stdin.
Every repeat dispatch, changed operation or lost acknowledgement refuses to sign
again. Manual `observe` calls only
`/usr/local/lib/aex-scm-action-host/observe`; it requires the same prior intent,
can run after expiry and accepts only an `uncertain` historical result. No
transaction is constructed, signed or retried by observation. The bridge's outer
command budget is 510 seconds against the host's 480-second aggregate budget;
the host must still recheck the policy window before signing and broadcasting.
A timeout does not prove that descendants stopped or that nothing was signed.

Only bounded public `{ok:true,result:{schema,operationId,outcome,txHash,nonce}}`
results are retained. Missing, malformed, repeated, contradictory or failed output
becomes uncertainty without echoing output. Submitted is not settled. Immutable
intent/observation rows reject UPDATE, DELETE and INSERT OR REPLACE even with
SQLite's default recursive triggers disabled. Idempotent observation checks
compare the already-retained exact result instead of hiding conflicts with IGNORE.
No escrow readiness/fulfillment, allocation release, listing close or tenant
credential publication is performed by either mode.

The initial settlement reservation now stores immutable `settlement_mode` before
background work. `ListingService.claim` permits its historical SDK path only for
an explicit original `legacy` row bound to the listing, with no contradictory
capability job or continuation. Capability, missing and pre-migration NULL rows
fail closed. Unknown rows are not inferred legacy or backfilled; UPDATE,
delete/reinsert and REPLACE cannot downgrade their recorded mode. This is a
fresh-state rollout, not permission to upgrade historical Hetzner obligations.

Stdlib regression tests execute the actual initial dispatch, insert and claim
methods plus real temporary SQLite/fork/pipe paths. Local tests mock credential
changes and are not privileged-worker acceptance. A separate explicit Linux CI
permission step runs real group/GID/UID drops in a new disposable `/run` fixture;
it uses no signer, network, host command or installed seller paths. Neither test
level proves live operation approval, useful output, settled costs or stop.

Integration tracking: [AEX #581](https://github.com/holonym-foundation/aex/issues/581),
parent [infrastructure #1347](https://github.com/holonym-foundation/internal-docs/issues/1347),
master [AEX programme #2695](https://github.com/holonym-foundation/internal-docs/issues/2695).

## Rollout boundaries

### Pre-escrow capacity hold (new capability seller source)

The buyer-facing negotiation controller now reserves a real `compute_allocations`
slot **before** escrow payment. This is not the admin-only manual reservation hook,
an availability count, or AEX's separate checkout hold. The new
`buyer_capacity_holds` table is initialized with the seller schema; no historical
hold/owner classification is inferred or backfilled. No deployed database is
changed by publishing this source.

The exact immutable binding has `schema:1`, `chainId:84532`, `requestDigest`
(AEX original request, bare lowercase SHA-256), `approvalDigest`, `policyRevision`,
`configDigest`, `proposalDigest` (each `sha256:` plus lowercase SHA-256),
`checkoutHoldId` (UUIDv4), `checkoutIntentDigest` (bare SHA-256), `ownerWallet`,
`buyer`, `seller` (canonical nonzero lowercase addresses), `listingId`,
`negotiationId` (`neg_` plus UUIDv4's 32 lowercase hex digits, matching the actual
negotiation producer), `amountAtomic` (positive decimal string, no USD conversion),
and `durationSeconds` (1–3600). Unknown fields refuse. The checkout references
remain opaque seller-side; only AEX can prove that its independently authenticated
controller and PR381 checkout intent are current. A buyer signature cannot certify
the controller's consent or transform a checkout reference into seller capacity.

The seller compares authenticated negotiation ownership, terminal agreement,
amount/duration and exact persisted proposal skeleton under the allocation lock.
`proposalDigest` hashes the **persisted skeleton**, not a reconstructed final
amount/arbiter tuple; the agreed amount is separate. `configDigest` hashes the
exact final tenant environment, which is compared at consumption and fulfillment.
Canonical JSON uses recursively sorted ASCII object keys, UTF-8 string values,
compact separators and safe integer numbers only. Raw environment is not in the
receipt. The buyer's independently reviewed escrow contract/token/arbiter/demand
and expiration remain necessary: this hash is not a substitute for that authority.
The accepted proposal's expiration must equal the actual attestation deadline,
not merely be in the future.

The private authenticated protocol is:

* `POST /api/v1/negotiate/{negotiationId}/capacity-hold` with
  `{buyer_address,binding}`. EIP-191 operation `capacity_hold_reserve` signs
  resource `{negotiationId}:{sha256(binding)}` using the existing timestamp format.
* `GET /api/v1/negotiate/{negotiationId}/capacity-hold/{holdId}?buyer_address=…`
  uses operation `capacity_hold_status`, resource `{negotiationId}:{holdId}`.
* `POST` to that same hold path takes `{buyer_address,action:"arm"|"cancel"}`;
  operation is `capacity_hold_arm` or `capacity_hold_cancel`, same resource.

Receipt: `{schema:1,holdId,binding,allocationId,resourceId,status,expiresAt,escrowUid}`.
Hold/allocation IDs are UUIDv4; resource ID is the existing inventory identity.
Status/action replies additionally contain `paymentAuthorized` and `retry:false`.
The former is true **only** for the first successfully acknowledged `held` →
`payment_pending` transition. It is seller bookkeeping, not wallet/signer approval.
Before any approval/escrow signing, the buyer must persist that exact receipt and
apply its independent source-owned policy and one-shot operation journal. After a
lost arm/transaction acknowledgement, observe/reconcile the original identity;
never treat a repeated arm response as authorization to pay again.

Hold plus allocation are committed in one `BEGIN IMMEDIATE` transaction. Same
request/negotiation retries return the original receipt without extending its
maximum five-minute TTL. Unarmed expiry/cancel releases only that allocation;
allocation attempts also reap expired unarmed holds under the same lock.
Arming and consumption sample the wall clock after acquiring the writer lock.
Reciprocal SQLite insert guards prevent a hold and a legacy escrow racing across
an awaited chain read from taking the same negotiation down separate paths.
`payment_pending` never expires or cancels automatically, even if no escrow UID
has arrived. It requires independent reconciliation before any future release
mechanism; this source supplies no payment-absence assertion or automatic refund.

Capability `POST /settle/{escrowUid}` requires `capacity_hold_id` and an empty SSH
key. After the existing chain verification, it consumes that exact armed hold,
binds its original allocation to the verified escrow, and provisions without a
second allocation. Changed config/owner/proposal/allocation fails closed. A hold
cannot be downgraded into non-capability settlement by removing the environment
marker. Historical non-capability calls without a hold retain their existing
allocation path. Prepared/pending and lost-reply behavior remain non-settled.

Authenticated HTTP consumer/signature tests and real temporary SQLite tests run
in the existing Storefront CI suite. Standalone stdlib checks execute the actual
allocator/settlement/fulfillment bodies with synthetic dependencies. They do not
prove installed endpoints, AEX checkout integration, independent signer policy,
escrow funding, receipt authenticity over an untrusted transport, or live capacity.
Buyer/fleet settlement producers and paired-image smoke fixtures must explicitly
carry/seed this hold before upgrading to this seller source. Existing installed
images and historical seller databases remain untouched.

Preparation needs no schema migration: status is a string and preparation metadata
lives in the existing JSON params; `_build_params` passes only explicit Ansible fields.
The seller continuation adds a private `capability_settlement_continuations` table
on first capability submission. Existing rows are never retrofitted from request
parameters. Known static inference/ingest/TAP/wallet credential fields are refused
before snapshot persistence; other private configuration remains private DB state.

Negotiation creation now persists the signature-verified EIP-191 buyer in the
existing `negotiation_threads.buyer` column before returning its ID. Continue,
settle POST and status GET require that exact owner before dispatch or disclosure.
The caller-controlled `buyer_agent_url` / `their_agent_id` is never authority,
even when it looks like a wallet address. Historical rows without this verified
binding fail closed and need separately reviewed provenance-based migration or a
fresh negotiation, not a first-poller ownership claim. This also closes the old
status endpoint's valid-but-unrelated-signer access to settlement credentials.

Install seller/provisioner/controller versions together. Do not run an older
worker against admitted jobs: it lacks the atomic claim and admission guard.
Before rollback, quiesce submissions and resolve queued/running jobs; preserve
the database and exact request/admission evidence, never erase prepared fences.

Tests cover SQLite persistence, request conflicts, expiry, credentials, HTTP
prepare/admit, duplicate delivery and seller capacity holding. They do not prove
primary enrollment, real receiver delivery, Docker mounts, running-agent utility,
settled inference cost, stop acceptance, or seven-day qualification. Those remain
required in the coordinated AEX staging rehearsal before launch acceptance.
