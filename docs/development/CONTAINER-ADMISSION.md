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

## Rollout boundaries

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
