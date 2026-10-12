# Safety guarantees

Duplio's core rule is that **a bug, a crash, a changed file, a locked phone or a
misconfigured mount must never cost you data.** Each guarantee below names the
code that enforces it and the test that proves it.

## Detection never lies in the dangerous direction

| Guarantee | Where | Test |
|---|---|---|
| Files are called identical only on a cryptographic digest, or after a byte-for-byte comparison | `compare/engine.py::_verify_stage` | `test_non_cryptographic_hash_forces_byte_compare`, `test_byte_verification` |
| Same size and same sampled regions is not enough | engine full-hash stage | `test_all_mode_finds_exact_groups_only` |
| A file that changes size mid-scan is never matched | `hashing/core.py::hash_file(expected_size=…)` | `test_file_changing_size_mid_scan_is_reported` |
| Hard links are not duplicates of each other | `_collapse_aliases` | `test_hardlinks_are_aliases_not_duplicates` |
| Overlapping roots (nested, symlinked, bind-mounted, one phone via ADB and MTP) are refused | `scan/inventory.py::check_overlaps` | `test_overlapping_roots_refused`, `test_same_phone_via_adb_and_mtp_refused` |
| "Is it backed up?" never reports *safe* or *missing* for a file it could not compare | `_split` / `_drop` taint tracking | `test_unique_mode_marks_unreadable_as_unverified`, `test_unreadable_reference_never_yields_false_unique` |
| Cached digests never cause an action | the executor always re-reads | `test_same_size_same_mtime_content_change_is_caught` |
| Recently modified files are not cached | `HashCache.put` racy window | `test_cache_ignores_fresh_files` |

## Plans cannot be unsafe

| Guarantee | Where | Test |
|---|---|---|
| Every group keeps at least one copy | `planner.decide`, `_assert_invariants`, and the TUI toggle | `test_decide_always_keeps_one`, `test_review_toggle_apply_and_undo` |
| Reference locations, protected paths and read-only storage are never targeted, whatever the rules | `removal_block_reason` | `test_reference_and_protected_are_never_removed` |
| Only identical (`CERTAIN`) groups get actions; similar images only reversible ones | `build_plan`, `build_similar_plan` | `test_similar_plan_only_reversible` |
| App bundles, photo libraries, `Android/data`, `node_modules`… are protected by default | `scan/filters.py::DEFAULT_PROTECT` | `test_scan_filter_defaults` |

## Applying is paranoid

| Guarantee | Where | Test |
|---|---|---|
| A file changed after the scan is skipped | `Executor._process_inner` | `test_changed_victim_is_skipped` |
| A content change hidden behind an unchanged size and time is caught | fresh re-hash | `test_same_size_same_mtime_content_change_is_caught` |
| A changed or missing kept copy blocks its group | `_verify_content` | `test_changed_keeper_blocks_group`, `test_missing_keeper_blocks_group` |
| Two names for one file are never acted on | `same_file` and the post-action keeper check | `test_alias_paths_are_never_both_acted_on` |
| Permanent deletion needs explicit confirmation everywhere | `Executor.run`, CLI `--i-understand`, TUI typed `DELETE` | `test_delete_requires_confirmation`, `test_delete_needs_explicit_acknowledgement`, `test_delete_needs_typed_confirmation` |
| A dry run changes nothing and writes no journal | `ApplyOptions.dry_run` | `test_dry_run_changes_nothing` |
| Renames never overwrite | `vfs/local.py::rename_noreplace` (`renameat2`) | `test_rename_noreplace_never_clobbers` |
| Writes are atomic: a failure leaves nothing behind | `LocalBackend.open_write`, `SMBBackend.open_write` | `test_open_write_is_atomic_and_no_clobber`, `test_smb_write_semantics` |
| Storage that cannot rename gets copy → verify → delete | `Executor._quarantine` fallback | `test_quarantine_fallback_when_rename_impossible` |
| Every change is journaled before it happens; crashes are diagnosable | `plan/journal.py`, `undo.inspect_incomplete` | `test_interrupted_operation_is_detected` |
| Quarantine, hard links and clones can be undone, with metadata restored | `plan/undo.py` | `test_quarantine_apply_and_undo`, `test_hardlink_apply_and_undo` |

## Backups protect, never destroy

| Guarantee | Where | Test |
|---|---|---|
| Snapshots appear only after their data is durable | `Repository._write_snapshot` ordering | `test_backup_restore_roundtrip` |
| Corruption is detected and never restored silently | `Repository.get` re-hash | `test_check_detects_corruption_and_missing` |
| Restore never overwrites a different file unless asked | `Repository.restore(conflict=…)` | `test_restore_conflicts` |
| Concurrent writers are locked out; stale locks are reclaimed | `Repository.lock` | `test_lock_blocks_concurrent_writers` |
| A retention policy cannot delete everything; the newest snapshot is always kept | `retention.apply_policy` | `test_retention_keeps_newest_and_refuses_empty` |
| A mirror never deletes: replaced and removed files go to versions | `run_mirror` | `test_mirror_archives_instead_of_deleting` |
| A mirror stops when the source looks empty (locked phone) or most of the destination would change | `plan_mirror` brake | `test_mirror_brake_on_empty_or_massive_change` |
| A phone import frees space only for files verified in the library during the same run | `ingest._free_source` | `test_ingest_never_frees_files_only_known_from_ledger` |

## Known limits

- Hard links and clones share or replace storage. After a hard link, editing one name edits the other; reflinks do not have this problem. `duplio undo` turns them back into independent files.
- The system trash cannot be restored programmatically; Duplio tells you where the files went.
- A share mounted locally *and* opened as `smb://` at the same time cannot be recognised as the same storage in advance. The executor's post-action keeper check catches it and rolls the change back.
- Backups are not encrypted. Keep repositories on storage you trust; encryption is planned as an opt-in feature.
