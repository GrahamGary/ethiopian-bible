# Validated-only production and publishing

This directory is the machine-readable boundary between Scripture production and publication.

- `PRODUCTION_LOCK.json` is created atomically by `scripts/production-lock.sh`. It is intentionally ignored by Git. A live lock covers source work, creation, translation, checking, rendering, and validation. A different chapter can never replace it.
- `VALIDATED_STATE.json` protects every completed Scripture payload, plus the application files that contain or render completed Scripture, with SHA-256 hashes.
- `PUBLISH_READY.json` is the only allow-list for publication. Its `files` array contains validated, hash-pinned files only. A chapter absent from that array is not publishable.
- `content-manifest.json` owns the approved project book order in its ordered `books` array. Every book records its approved chapter total and derived `NOT_STARTED`, `IN_PROGRESS`, or `COMPLETE` status; production must never infer a different order.
- `PUBLISH_READY.json` lists itself with `validation: "self"` so the publication-control record is explicitly allow-listed without an impossible self-referential hash. The complete gate validates that record immediately before commit and again immediately before push.
- `PUBLISH_LOCK.json` is an ignored, atomic live-publication lock. Production lock acquisition and publication are mutually exclusive.
- `PUBLICATION_STATE.json` is an ignored, machine-readable local record written only after a successful `origin/main` push. `PUBLICATION_STATE.pending.json` is prepared before the push so an interrupted or failed attempt cannot be mistaken for a successful publication.
- `VALIDATION_CANDIDATE.json` is a temporary source attestation for a chapter under production or a sourceNote-only candidate for an authorized protected correction. It is intentionally ignored by Git and is never a publication record.
- `CORRECTION_AUTHORIZATION.json` is an ignored, one-operation authorization record. The correction gate binds its SHA-256 hash into the lock, recovery journal, and permanent validation audit, then removes the temporary authorization only after successful finalization.
- `CORRECTION_1_ENOCH_085_EVIDENCE.json` is the permanent, hash-protected primary-witness evidence for the single authorized Chapter 85 correction.
- `FINALIZE_TRANSACTION.json` is an ignored recovery journal created only after every validation check passes. If a run stops during metadata finalization, the next run must execute the recovery command before doing any new work.

## Required chapter workflow

1. Read `PUBLISH_READY.json`, `VALIDATED_STATE.json`, `PROGRESS.md`, the manifest, Git status, and any existing lock or recovery journal.
2. If `FINALIZE_TRANSACTION.json` exists, run `python3 scripts/validation-gate.py recover-finalize` and do not create another chapter.
3. Acquire the exact next unfinished chapter with `scripts/production-lock.sh acquire BOOK_ID CHAPTER RUN_ID`. An active lock exits with status 75. A stale lock may be resumed only for the same book and chapter; new locks use a one-hour stale interval, and production must send regular heartbeats. A lock's stored `staleAfterSeconds` remains authoritative for ordinary acquisition, so changing the default never shortens an existing lock automatically.
4. Keep the lock active through source establishment, creation, translation, checks, rendering, and validation. Run `scripts/production-lock.sh heartbeat RUN_ID` before and after each phase and at least every 30 minutes.
5. Create or resume only the locked chapter. Never change a file listed under `protectedFiles` in `VALIDATED_STATE.json`.
6. Write `.automation/VALIDATION_CANDIDATE.json` using the evidence format enforced by `validation-gate.py`. All source attestations must be true and name the primary witnesses actually used.
7. Run `python3 scripts/validation-gate.py finalize --chapter-file PATH --evidence .automation/VALIDATION_CANDIDATE.json --run-id RUN_ID`.
8. `finalize` validates source evidence, payload structure, JSON, manifest sequence, app rendering contracts, search, navigation, mobile layout, and previous-content hashes before it prepares any metadata update. It then advances the manifest, `PROGRESS.md`, `VALIDATED_STATE.json`, and `PUBLISH_READY.json` through a recoverable transaction and removes the lock last.
9. On any failure before validation completes, do not edit the progress, manifest, validated state, or publishing record. Preserve the partial locked chapter and leave the lock for same-chapter recovery.

When a validated chapter reaches its book's manifest-approved `totalChapterCount`, `finalize` marks that book `COMPLETE` and derives the next unfinished marker as Chapter 1 of the next entry in the manifest's ordered `books` array. It refuses to invent an overflow chapter or a next book. The one legacy completed-book overflow state can be corrected transactionally with `python3 scripts/validation-gate.py repair-book-transition`; interruption recovery remains `recover-finalize`.

## Confirmed stranded local-lock recovery

When an existing same-host owner is independently proven inactive but its older lock still carries a longer stored timeout, use `scripts/production-lock.sh recover-stranded BOOK_ID CHAPTER EXISTING_RUN_ID NEW_RUN_ID --inactive-owner-evidence EVIDENCE`. This explicit recovery path requires the exact current `nextUnfinished` book and chapter, the exact displaced run ID, the same host, valid protected repository records, no finalize or publication transaction, an unchanged lock throughout the check, and at least one hour without a heartbeat. It records the prior run, host, heartbeat, and operator evidence in the replacement lock. Never use it merely because another run wants access; an owner that is active or still heartbeating must not be displaced.

## Authorized protected correction workflow

The only implemented protected-file correction target is 1 Enoch 85, and the only permitted payload field is `sourceNote`. The fixed allow-list is enforced in `validation-gate.py`; it excludes every other Scripture file and preserves 1 Enoch 86 as the next unfinished chapter.

1. Create `.automation/CORRECTION_AUTHORIZATION.json` with the exact target, primary witnesses, sourceNote-only field scope, and fixed file allow-list enforced by the gate.
2. Run `python3 scripts/validation-gate.py lock acquire-correction RUN_ID`. Acquisition first runs the complete existing validation gate, verifies the old protected Chapter 85 hash, snapshots the dependent state hashes and original bytes, and binds the authorization hash into the live lock.
3. Keep the correction lock active with the ordinary heartbeat command. Put only the corrected `sourceNote` in `.automation/VALIDATION_CANDIDATE.json`, and record the direct primary-witness checks in `.automation/CORRECTION_1_ENOCH_085_EVIDENCE.json`.
4. Run `python3 scripts/validation-gate.py finalize-correction --evidence .automation/CORRECTION_1_ENOCH_085_EVIDENCE.json --run-id RUN_ID`.
5. The correction finalizer proves that only the `sourceNote` line changes, completely validates the reconstructed Chapter 85 payload, verifies every unrelated protected hash, and journals the corrected chapter, protected state, and READY record as one recoverable transaction. It does not change the manifest, progress record, chapter numbering, or next-unfinished marker. On interruption, use the ordinary `recover-finalize` command.

Publishing is dry-run by default. `scripts/publish-validated.sh` and `scripts/publish-validated.sh --dry-run` refuse a live production lock, missing or inconsistent records, and hash drift, then display the exact READY allow-list without changing Git.

`scripts/publish-validated.sh --publish` is the explicit controlled-publication path. It requires local `main` to match the existing `origin/main`, requires a clean Git index, runs the complete validation gate, stages only changed paths in `PUBLISH_READY.json`, verifies staged paths and hashes, reruns the gate, creates one checked commit, reruns the gate once more, verifies the remote branch still exists at the expected commit, and pushes only `refs/heads/main:refs/heads/main` on the existing `origin`. The push disables followed tags and submodule pushes and uses an exact old-commit lease as a compare-and-swap guard. Unvalidated, unfinished, and unrelated working-tree changes remain unstaged. A failure before commit restores only the READY paths staged by the attempt; any validation, staging, or commit-check failure prevents a push.
