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

## Network drives are checked before they are trusted

| Guarantee | Where | Test |
|---|---|---|
| Nothing is written to an unmounted mount point (which would fill the local disk) | `vfs/guards.py::check_destination` | `test_unmounted_mount_point_is_refused` |
| A destination missing its marker file is refused | `write_sentinel` / `check_destination(sentinel=…)` | `test_sentinel_roundtrip`, `test_cli_sentinel_and_guarded_mirror` |
| A hung server produces an error, not a frozen program | `probe_responsive` | `test_unresponsive_backend_is_reported_not_hung` |
| Not enough free space stops the run before the first byte | `check_destination(needed_bytes=…)` | `test_space_check`, `test_not_enough_space_stops_before_copying` |
| Transient errors are retried; permanent ones (permission, exists, verification) never are | `vfs/retry.py` | `test_transient_classification`, `test_retry_gives_up_and_never_retries_permanent_errors`, `test_copy_verified_retries_a_dropped_connection` |
| A bandwidth limit also covers verification reads | `transfer.copy_verified(limiter=…)` | `test_copy_verified_respects_limiter` |
| Names a destination cannot store are rewritten without losing or merging files | `util/names.py`, `merge.plan_merge` | `test_windows_problem_names_are_flagged_and_fixed`, `test_names_are_fixed_for_windows_style_destinations` |

## Merge and sync never destroy

| Guarantee | Where | Test |
|---|---|---|
| A file the destination already has (under any name) is not copied again | `compare/diff.py` | `test_copies_only_what_is_missing` |
| A different file at the same path is never overwritten silently; replaced versions are kept | `merge.plan_merge`, `execute_action` | `test_conflict_rename_keeps_both`, `test_conflict_replace_and_undo` |
| A file that appears at the target after planning is not clobbered | no-clobber `open_write` | `test_never_overwrites_a_file_that_appeared_meanwhile` (merge and sync) |
| A source file changed since the comparison is skipped | `execute_action` re-stat | `test_source_changed_since_comparison_is_skipped`, `test_file_changed_after_planning_is_left_for_next_time` |
| A failed or cancelled copy leaves no partial file | `copy_verified` / `open_write` | `test_failed_copy_leaves_no_partial_file`, `test_cancel_stops_cleanly` |
| `--move` removes a source only after the copy is verified, and leaves it if the copy is damaged | `merge._remove_sources` via `Executor` | `test_move_quarantines_sources_after_verification`, `test_move_leaves_the_source_alone_if_the_copy_is_damaged` |
| `undo` never removes the only remaining copy, nor a copy edited since | `undo._merge_still_safe` | `test_undo_refuses_to_remove_the_only_copy`, `test_undo_keeps_a_copy_that_was_edited` |
| The first sync of a pair never deletes | `plan_sync` (no history) | `test_first_run_unions_the_folders_and_never_deletes` |
| A deletion moves the file to the quarantine; an edit on the other side beats a deletion | `sync._delete`, decision table | `test_deletion_is_moved_aside_not_destroyed`, `test_delete_on_one_side_but_edit_on_the_other_keeps_the_edit` |
| An empty side, a mass deletion or an unreadable side stops deletions | `sync._apply_brakes` | `test_empty_side_stops_the_run`, `test_mass_deletion_stops_the_run`, `test_unreadable_side_blocks_all_deletions` |
| Undoing a sync resets its history, so the next run cannot misread the restored files as deletions | `undo._forget_sync_history` | `test_undo_reverses_the_whole_sync_and_resets_history` |
| Hard-link snapshots are never visible until complete, are read-only, and deleting one leaves the others intact | `backup/snapshots.py` | `test_interrupted_run_leaves_no_snapshot`, `test_first_snapshot_is_a_complete_read_only_copy`, `test_forgetting_one_snapshot_never_harms_another` |
| Old versions are destroyed only by an explicit `versions prune --apply`, and unrecognised folders never | `backup/versions.py` | `test_list_and_prune_versions` |
| A checksum manifest tells corruption from edits | `backup/manifest.py` | `test_manifest_finds_every_kind_of_problem` |

## Encrypted repositories

| Guarantee | Where | Test |
|---|---|---|
| No file content, path, source or label is readable in the repository | `backup/crypto.py`, `Repository._seal_meta` | `test_nothing_readable_is_stored` |
| A modified object is detected; objects cannot be swapped or replaced by plaintext | AES-GCM with the object id as associated data | `test_tampered_object_is_detected`, `test_objects_cannot_be_swapped`, `test_plaintext_objects_are_rejected_in_an_encrypted_repository` |
| A tampered file list is reported and blocks pruning | `Repository.check`, `prune` | `test_tampered_file_list_is_reported_not_trusted` |
| A known file cannot be tested for | keyed (HMAC) object ids | `test_roundtrip_dedup_and_keyed_ids` |
| A wrong password opens nothing; a hostile config cannot demand terabytes of key-derivation memory | `RepoCrypto.unlock` | `test_password_rules`, `test_hostile_key_derivation_cost_is_refused` |
| Changing the password does not touch the data | `Repository.change_password` | `test_change_password_keeps_the_data` |

## Notifications cannot hurt a job

A failed webhook, command or e-mail is a warning, never a failure (`test_failed_delivery_is_reported_not_raised`); messages
reach commands through environment variables, never through the shell command line
(`test_command_target_gets_the_message_in_the_environment`); webhook URLs, which carry tokens, are never printed
(`test_target_parsing_and_masking`).

## Known limits

- **Backups store file contents, modification time and permission bits only.** Symbolic links, empty directories, ownership, extended attributes, ACLs, macOS resource forks and NTFS alternate data streams are not stored. Quarantine moves (renames) keep everything, but the copy-based fallback used on MTP does not.
- Detection is exact. Similar-but-not-identical files are only found for images (`--similar-images`); duplicate *folders*, truncated copies and paired files (RAW+JPEG, Live Photos, sidecars) are not recognised.
- Symbolic links are skipped when scanning, so a link is never "deduplicated" and never followed out of a root.
- Hidden temporary files left by a killed process (`.<name>.duplio-tmp-…`) are ignored by scans but are not cleaned up automatically, except inside backup repositories by `backup prune`.
- On Windows, paths longer than 260 characters and reserved device names depend on the OS configuration; Duplio reports the resulting error for the affected file and carries on.
- Hard links and clones share or replace storage. After a hard link, editing one name edits the other; reflinks do not have this problem. `duplio undo` turns them back into independent files.
- The system trash cannot be restored programmatically; Duplio tells you where the files went.
- A share mounted locally *and* opened as `smb://` at the same time cannot be recognised as the same storage in advance. The executor's post-action keeper check catches it and rolls the change back.
- **A lost repository password cannot be recovered.** Encryption protects contents and names; it does not hide how many objects there are, their sizes or their timestamps.
- `merge`, `sync` and `snapshot` copy file contents and modification times only, with the same limits as backups (no symbolic links, ownership, extended attributes or empty folders). Hard-link snapshots keep permission bits.
- `sync` does not detect renames: a renamed file is a deletion plus a new file (the content is copied again; nothing is lost). It also cannot pair names that differ only by case or Unicode form on a drive that distinguishes them; those are left alone and reported.
- `merge --move` and `--clean-source` re-read and re-hash both copies before removing a source file, so a move reads each file about three times.
- `watch` polls; a change that is made and reverted within one interval is not seen.
