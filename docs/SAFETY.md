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

## File names are data, never trusted text

| Guarantee | Where | Test |
|---|---|---|
| Names that are not valid UTF-8 are scanned, saved, applied, undone, backed up and restored byte for byte | `util/text.py` (`json_dumps`, `fs_bytes`), `hashing/cache.py` | `test_scan_apply_undo_with_hostile_names`, `test_hash_cache_keys_survive_undecodable_names`, `test_backup_and_restore_keep_exact_names` |
| A name cannot crash, rewrite or spoof the terminal or TUI | `cli/console.py::SafeConsole`, `printable()`, `tui/widgets.py::ptext`, `DuplioApp.notify` | `test_safe_console_never_emits_control_sequences_from_names`, `test_printable_escapes_only_what_is_unsafe`, `test_tui_survives_hostile_names_and_bracketed_messages` |
| Different names are never merged; none is silently dropped from a mirror | `backup/mirror.py` (exact match first) | `test_mirror_keeps_source_names_that_differ_only_by_unicode_form`, `test_mirror_reports_case_collisions_on_case_insensitive_destinations`, `test_names_are_never_lost_when_mixed_encodings_collide` |
| Same name, different Unicode form is recognised as the same file when pairing for a mirror | `util/text.py::name_key` | `test_mirror_does_not_duplicate_nfc_and_nfd_spellings` |
| Maximum-length names can be written (temp names are shortened) | `util/text.py::temp_name` | `test_copy_of_maximum_length_names` |
| Content decides, not names | `compare/engine.py` | `test_identical_content_is_matched_whatever_the_names`, `test_phone_backup_check_ignores_renames` |
| Exports are lossless (JSON) or printable (CSV/HTML) | `reporting/export.py` | `test_exports_are_safe` |
| `compare --list` output is raw and uncontaminated, so it can feed `xargs -0` | `cli/cmd_scan.py` | `test_compare_list_prints_exact_bytes_and_keeps_stdout_clean` |

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

- **Backups store file contents, modification time and permission bits only.** Symbolic links, empty directories, ownership, extended attributes, ACLs, macOS resource forks and NTFS alternate data streams are not stored. Quarantine moves (renames) keep everything, but the copy-based fallback used on MTP does not.
- Detection is exact. Similar-but-not-identical files are only found for images (`--similar-images`); duplicate *folders*, truncated copies and paired files (RAW+JPEG, Live Photos, sidecars) are not recognised.
- Symbolic links are skipped when scanning, so a link is never "deduplicated" and never followed out of a root.
- Hidden temporary files left by a killed process (`.<name>.duplio-tmp-…`) are ignored by scans but are not cleaned up automatically, except inside backup repositories by `backup prune`.
- On Windows, paths longer than 260 characters and reserved device names depend on the OS configuration; Duplio reports the resulting error for the affected file and carries on.
- Hard links and clones share or replace storage. After a hard link, editing one name edits the other; reflinks do not have this problem. `duplio undo` turns them back into independent files.
- The system trash cannot be restored programmatically; Duplio tells you where the files went.
- A share mounted locally *and* opened as `smb://` at the same time cannot be recognised as the same storage in advance. The executor's post-action keeper check catches it and rolls the change back.
- Backups are not encrypted. Keep repositories on storage you trust; encryption is planned as an opt-in feature.
