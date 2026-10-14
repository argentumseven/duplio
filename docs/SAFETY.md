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
| "Already safe elsewhere" is never answered from remembered digests (a copy that rotted without changing size or time is noticed); `--use-cache` is an explicit opt-in | `compare`, `diff`, `merge`, `sync`, `mirror`, `ingest` and the TUI read fresh | `test_compare_notices_rot_in_the_backup`, `test_diff_notices_rot`, `test_merge_does_not_skip_a_file_whose_copy_rotted`, `test_mirror_checksum_repairs_a_rotted_copy`, `test_ingest_does_not_trust_a_rotted_library_copy` |
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
| A kept copy is checked again for every irreversible step (delete, link); for undoable steps an earlier check is reused only while the file's size, times and identity are unchanged | `Executor._keeper_verified` | `test_a_kept_copy_that_changes_during_the_run_stops_later_removals`, `test_an_unchanged_kept_copy_is_hashed_once_for_reversible_steps_and_every_time_for_deletes` |
| Two names for one file are never acted on | `same_file` and the post-action keeper check | `test_alias_paths_are_never_both_acted_on` |
| Permanent deletion needs explicit confirmation everywhere | `Executor.run`, CLI `--i-understand`, TUI typed `DELETE` | `test_delete_requires_confirmation`, `test_delete_needs_explicit_acknowledgement`, `test_delete_needs_typed_confirmation` |
| A dry run changes nothing and writes no journal | `ApplyOptions.dry_run` | `test_dry_run_changes_nothing` |
| Renames never overwrite | `vfs/local.py::rename_noreplace` (`renameat2`) | `test_rename_noreplace_never_clobbers` |
| Writes are atomic: a failure leaves nothing behind | `LocalBackend.open_write`, `SMBBackend.open_write` | `test_open_write_is_atomic_and_no_clobber`, `test_smb_write_semantics` |
| Storage that cannot rename gets copy → verify → delete | `Executor._quarantine` fallback | `test_quarantine_fallback_when_rename_impossible` |
| Every change is journaled before it happens, with the names of the files it will create; a crash is settled from the facts on disk (`duplio recover`), and `undo` does so first | `plan/journal.py`, `plan/reconcile.py` | `test_interrupted_operation_is_detected`, `test_recover_settles_a_quarantine_that_happened_but_was_never_recorded`, `test_recover_puts_back_a_file_that_a_delete_had_set_aside`, `test_a_torn_last_line_does_not_swallow_the_records_written_after_it` |
| An error that arrives after a change took effect is journaled as done, not failed, so `undo` still reverses it | `reconcile.record_failure` | the `error` sweeps below |
| Quarantine, hard links and clones can be undone, with metadata restored | `plan/undo.py` | `test_quarantine_apply_and_undo`, `test_hardlink_apply_and_undo` |

## Backups protect, never destroy

| Guarantee | Where | Test |
|---|---|---|
| Snapshots appear only after their data is durable | `Repository._write_snapshot` ordering | `test_backup_restore_roundtrip` |
| Corruption is detected and never restored silently | `Repository.get` re-hash | `test_check_detects_corruption_and_missing` |
| Restore never overwrites a different file unless asked | `Repository.restore(conflict=…)` | `test_restore_conflicts` |
| Concurrent writers are locked out; a lock is a lease (see below) | `Repository.lock` | `test_lock_blocks_concurrent_writers` |
| A retention policy cannot delete everything; the newest snapshot is always kept | `retention.apply_policy` | `test_retention_keeps_newest_and_refuses_empty` |
| A mirror never deletes: replaced and removed files go to versions | `run_mirror` | `test_mirror_archives_instead_of_deleting` |
| A mirror stops when the source looks empty (locked phone) or most of the destination would change | `plan_mirror` brake | `test_mirror_brake_on_empty_or_massive_change` |
| A phone import frees space only for files verified in the library during the same run, and reads the library copy again right before each deletion | `ingest._free_source` | `test_ingest_never_frees_files_only_known_from_ledger`, `test_ingest_rechecks_the_library_copy_right_before_deleting_the_original` |

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

## Network servers (SFTP, WebDAV, FTP) are not trusted either

A server is another computer, and what it sends back is data, not instructions. These rows apply to `sftp://`, `davs://`
and `ftps://` locations (and their unencrypted forms). The `remote` fixture in `tests/test_remote_contract.py` runs the shared
rows against a real WebDAV, FTP (with and without `MLSD`) and SFTP server.

| Guarantee | Where | Test |
|---|---|---|
| A backslash in a name is part of the name, never a separator: `a\b.txt` and `a/b.txt` stay two different files (mixing them up would hash, and then act on, the wrong one) | `ListingBackend.normalize` | `test_a_backslash_is_part_of_a_name_not_a_folder_separator` (all four server kinds) |
| A name a server sends cannot leave the folder it is copied into (`..`, a path separator, an empty name) | `vfs/remote.py::unsafe_name`, the walker | `test_names_that_would_escape_the_folder_are_refused`, `test_names_the_server_sends_cannot_escape` (FTP and SFTP) |
| A server or user name cannot become an option of the `ssh` program (`sftp://-oProxyCommand=…`) | `vfs/sftp.py::parse_sftp` | `test_names_that_ssh_could_read_as_options_are_refused` |
| A changed SSH host key is refused, with an explanation; a new one is remembered by `ssh` itself | `explain_ssh_failure`, `StrictHostKeyChecking=accept-new` | `test_a_changed_host_key_is_refused_with_an_explanation`, `test_ssh_is_started_with_safe_defaults` |
| A password is asked for only after keys fail, goes to ssh through a private helper that answers password prompts and nothing else, and is never on a command line; the helper is removed afterwards | `SftpBackend._open_session`, `_askpass_script` | `test_a_password_is_used_only_when_no_key_works_and_never_for_other_questions`, `test_a_wrong_password_is_refused_once_without_hammering_the_server` |
| FTP over TLS verifies the server's certificate | `FtpBackend._connect` | `test_ftp_over_tls_is_verified_and_works` |
| A file without a size or a modification time is reported as unreadable, never given a made-up one; an FTP server with neither `MLSD` nor `MDTM` is refused | `Child.problem`, `FtpBackend._learn` | `test_a_file_without_size_or_time_is_reported_not_guessed`, `test_files_without_times_are_reported_not_guessed`, `test_a_server_that_cannot_report_times_is_refused` |
| A scan's size and time for a file are the same facts a later check sees | listing and `stat` use one source per server | `test_stat_agrees_with_the_listing` |
| Binary files are never altered by a text-mode transfer (FTP switches to text mode for listings) | `ftp._binary` | `test_binary_files_survive_after_listings` |
| A file is written to a hidden temporary name, its size checked, moved into place without replacing anything unless asked, and then looked up under the final name; any failure leaves nothing behind | `open_write` of each backend | `test_writing_never_replaces_unless_asked`, `test_an_error_while_writing_leaves_nothing_behind`, `test_a_large_upload_round_trips` |
| A server that stores a file under a different name than asked is noticed | `_check_stored` | `test_the_drive_probe_runs_and_cleans_up` (WsgiDAV cuts a name at `?`) |
| Overwriting keeps the old file until the new one is in place, and puts it back on failure | `FtpBackend._put_in_place`, `SftpBackend._commit` | `test_an_overwrite_keeps_the_old_file_until_the_new_one_is_in_place`, `test_overwrite_with_and_without_the_atomic_rename_extension` |
| Only single files are deleted: never a folder (WebDAV `DELETE` would remove everything inside it) | `remove` of each backend | `test_remove_deletes_single_files_and_refuses_folders`, `test_remove_deletes_files_but_refuses_folders` |
| Empty folders are removed only when verified empty (SFTP and FTP) or when they are Duplio's own temporary folders (WebDAV) | `remove_empty_dirs` | `test_empty_folders_go_but_never_ones_with_files` |
| Renames never replace | `rename` of each backend | `test_rename_does_not_overwrite` |
| A hostile listing cannot exhaust memory or run code: documents with a DTD are refused, SFTP packets over 8 MiB are refused, truncated answers are errors | `parse_multistatus`, `SftpSession._read_packet`, `Reader` | `test_a_listing_with_a_dtd_is_refused`, `test_an_enormous_packet_is_not_allocated`, `test_truncated_attributes_are_a_protocol_error_not_a_crash`, `test_a_greeting_from_something_that_is_not_sftp_is_refused` |
| A dropped connection is an error that the retry layer understands, never a short file | `RangedReader.readinto` | `test_a_lost_connection_is_reported_and_the_next_call_reconnects`, `test_a_connection_closed_by_the_server_is_noticed_before_use` |
| Backup repositories work on all of them | | `test_a_backup_repository_works_on_the_server` |

## Pictures from slow drives are for looking at

| Guarantee | Where | Test |
|---|---|---|
| A picture never decides anything: identity comes from re-reading the real files; similar-image groups are never `CERTAIN`, so only reversible actions apply | `compare/engine.py`, `plan/planner.py` | `test_similar_plan_only_reversible` |
| A picture of a big photo on a slow drive costs a few kilobytes, once | `preview/exif.py`, `preview/service.py` | `test_a_photo_on_a_slow_drive_costs_kilobytes_the_first_time_and_nothing_after`, `test_one_picture_serves_a_group_and_the_next_groups_are_fetched_ahead` |
| A picture with no small preview inside is not downloaded unless it is small or you ask | `PreviewService.get` | `test_a_picture_without_a_preview_is_not_downloaded_unless_small_or_asked` |
| A file that exists only in the cloud is never downloaded to make a picture, unless asked | `offline` flag from the file attributes | `test_a_cloud_only_file_is_never_fetched_unasked`, `test_cloud_only_files_wait_until_asked`, `test_placeholder_files_are_recognised` |
| Pictures are kept only until Duplio closes unless persistence is chosen; the saved index holds no file names | `preview/store.py`, `preview/settings.py` | `test_session_store_disappears_when_closed`, `test_store_index_holds_no_file_names`, `test_modes_and_where_things_are_kept` |
| An edited file is never shown as its old self | the key includes size and modification time | `test_a_changed_file_gets_a_new_key`, `test_a_changed_file_is_fetched_again` |
| A damaged or hostile image cannot crash the decoders or make them read without limit | `preview/exif.py`, `preview/jpeg.py` | `test_garbage_and_truncated_files_never_raise`, `test_offsets_past_the_end_of_the_file_are_ignored`, `test_the_byte_budget_stops_a_file_that_asks_for_too_much`, `test_progressive_and_non_jpeg_data_are_refused_cleanly` |
| Folder listings remembered by the folder browser are never used by scans, comparisons or anything that changes files | `vfs/listing.py` is used by `tui/picker.py` only | `test_a_server_is_asked_once_while_you_walk_up_and_down`, `test_fast_local_disks_are_never_remembered` |

## The window adds no new powers

The window (`gui/`) is another front end for the same engines, so every guarantee above holds for it. What it adds are
rules about showing and asking.

| Guarantee | Where | Test |
|---|---|---|
| Looking changes nothing: comparing, previewing a sync, previewing an import and searching for duplicates only read | every page's job reads and returns a result; only a separate button starts a change | `test_compare_shows_a_against_b_and_copies_both_ways`, `test_import_brings_only_new_pictures_into_the_library` |
| Every copy, removal, sync, import and permanent deletion is announced with the full list of what will happen, and nothing happens if the answer is no (putting files back is one click, since it only restores) | `Context.confirm` before `run_merge`, `apply_plan`, `run_sync`, `ingest`, `purge_quarantine` | the flow tests record each question and answer it |
| Copying A → B or B → A never replaces or alters the side being copied from, and never replaces a file on the receiving side | `plan_merge` (clash: kept under a new name), `swap_sides` for the reverse direction | `test_compare_shows_a_against_b_and_copies_both_ways`, `test_swapping_gives_the_same_answer_as_comparing_the_other_way_round` |
| Only the files you clicked are copied when you clicked some | the plan is cut down to the selected entries before it is shown | `test_copy_only_what_is_selected` |
| "Is it backed up?" counts a file as safe only when an identical file exists in B, whatever its name; a file that is missing, a different version, or unreadable is never counted as safe | `backup_verdict`, content comparison with no remembered digests | `test_the_backup_verdict_never_calls_missing_files_safe`, `test_is_it_backed_up_says_no_then_yes` |
| At least one copy of every duplicate always stays, whatever is clicked; a reference or protected copy cannot be marked for removal | `GroupRow.toggle`, `keep_only`, `decide` | `test_one_copy_always_stays_however_the_marks_are_clicked`, `test_a_reference_folder_is_never_offered_for_removal` |
| Removed duplicates go to the quarantine and come back with one button | `apply_plan`, `undo`, History & undo | `test_duplicates_removed_to_quarantine_and_put_back`, `test_history_lists_the_copy_and_undoes_it` |
| A picture shown is a picture of the file in that tile; looking at pictures never decides anything | `Tile.key` includes the side and the file; previews are for display only (see above) | `test_thumbnails_become_upright_pictures_that_fit_the_box` |
| Work never freezes the window and never touches Tk from a worker thread; a failure is shown, not swallowed | `gui/jobs.py` (worker threads, a cancel button), thumbnails are made on workers and turned into images on the window's thread | `test_a_job_reports_its_result_and_its_progress`, `test_a_job_can_be_stopped_and_a_failure_is_kept_not_lost` |

## Looking at a big folder stays safe and bounded

| Guarantee | Where | Test |
|---|---|---|
| A picture is fetched from a drive once at a time: when the window and the background fetcher want the same one, the second waits for the first | `PreviewService.get` | `test_two_requests_for_one_picture_read_the_drive_once` |
| When the picture buffer is full, fetching stops and says why; it does not throw away pictures of the open folder to fetch them again | `FolderPrefetcher`, `ThumbStore.trim` (the open folder's pictures are trimmed last, and the size limit still holds) | `test_a_full_buffer_stops_the_fetch_instead_of_throwing_pictures_away`, `test_pictures_of_the_folder_on_screen_are_trimmed_last_but_the_limit_always_holds`, `test_browse_says_so_when_the_buffer_is_full_instead_of_throwing_pictures_away` |
| An almost full disk, or a drive that stops answering, stops the fetch with a reason; it does not keep hammering | `FolderPrefetcher` | `test_an_almost_full_disk_stops_the_fetch`, `test_a_drive_that_stops_answering_stops_the_fetch_with_a_reason` |
| A full disk or a damaged index never loses a fetched picture or stops the window | `PreviewService.get`, `ThumbStore._open_index` | `test_a_full_disk_does_not_lose_a_picture_that_was_just_fetched`, `test_totals_survive_a_restart_and_a_damaged_index_is_started_again` |
| The pictures held in memory have a byte budget that shrinks when the computer is short of memory; what was let go comes back from the buffer, not the drive | `Thumbnailer` | `test_finished_pictures_are_kept_up_to_a_byte_budget_and_come_back_from_the_buffer_not_the_drive`, `test_the_picture_budget_shrinks_when_the_computer_is_short_of_memory_and_comes_back` |
| Scrolling cannot pile up unbounded work; read-ahead waits for what is on screen | `Thumbnailer` (bounded queues, back-pressure) | `test_scrolling_cannot_pile_up_unbounded_work`, `test_read_ahead_waits_for_what_is_on_screen` |
| Cloud-only files are skipped, not downloaded, by the background fetch | `FolderPrefetcher` | `test_cloud_only_files_are_skipped_not_downloaded` |
| Browsing a remote folder reads a small part of each photo, once, and walking away and back reads nothing | the whole path | `test_browsing_a_remote_folder_reads_each_picture_once_and_only_a_little_of_it` |
| A picture from an earlier folder is never shown on another file | pictures are keyed by the file's location, size and modification time | `test_a_picture_from_an_earlier_scan_never_appears_on_a_file_of_the_next` |
| Sorting and filtering change only what is shown, never what is selected or changed | `gui/sorting.py` | `test_the_two_folder_pages_sort_what_they_show` |

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

## Found by independent review, now fixed (each has a regression test)

| Guarantee | Where | Test |
|---|---|---|
| A snapshot summary that cannot be read is reported by `check` and stops `prune`; it is never silently left out (which made its chunks look unused) | `Repository._read_summaries`, `check`, `prune` | `test_prune_refuses_when_a_snapshot_summary_cannot_be_read`, `test_a_flaky_read_of_one_summary_cannot_make_prune_destroy_its_data` |
| A record cannot point at another snapshot or folder: the file name is the identity, and `forget` validates it | `Repository._read_summaries`, `SnapshotSet.snapshots/forget` | `test_a_summary_cannot_name_another_snapshot`, `test_forget_uses_the_folder_name_not_the_id_inside_the_record`, `test_forget_is_locked_too_and_checks_names` |
| Two snapshot runs cannot overlap (lock file in the destination) | `backup/lockfile.py`, `SnapshotSet._lock` | `test_a_second_snapshot_run_cannot_wipe_the_first_ones_work` |
| A source written to while it is copied is discarded, not kept torn under its real name | `merge._changed_during_copy` | `test_a_source_written_to_during_the_copy_is_not_reported_as_copied` |
| A repository whose key block was removed is refused instead of silently becoming plaintext | `Repository._refuse_downgrade` | `test_removing_the_key_block_does_not_turn_backups_into_plaintext` |
| Key-derivation memory is capped as a whole (128·r·n) | `RepoCrypto.unlock` | `test_key_derivation_cost_is_capped_by_memory` |
| Names that differ only in Unicode form are verified separately in a manifest | `manifest.verify_manifest` | `test_manifest_keeps_unicode_variants_apart` |
| Snapshots keep permission bits (minus write) and notice permission-only changes | `snapshots._make_read_only`, `_same_permissions` | `test_snapshots_keep_permissions_and_notice_chmod` |
| A file outside the filters on one side is not read as "deleted" there | `sync.plan_sync` | `test_a_file_outside_the_filters_on_one_side_is_not_a_deletion` |
| Sync deletions wait when any copy failed or the run was cancelled | `sync.run_sync` | `test_deletions_wait_when_a_copy_failed` |
| `--ignore-dst` also applies to each side's own history | `sync.plan_sync` | `test_ignore_dst_also_applies_to_each_sides_own_history` |
| Sync and merge never delete or replace protected paths | `sync.plan_sync`, `merge._conflict` | `test_sync_never_deletes_or_replaces_protected_paths`, `test_merge_never_replaces_a_protected_destination_file` |
| `sync --reset --dry-run` keeps the history | `cli/cmd_sync.py` | `test_reset_with_dry_run_keeps_the_history` |
| A hard-linked pair does not hide other files of the same content; an unreadable file of the same size blocks "only here" | `compare/diff.py` | `test_a_hard_linked_pair_does_not_hide_identical_copies`, `test_an_unreadable_file_of_the_same_size_blocks_only_here` |
| A copy that was written but could not be verified or cleaned up is removed before a retry, never left under the real name | `transfer.copy_verified` | `test_a_copy_whose_readback_failed_and_whose_cleanup_failed_is_not_left_behind` |
| After a crash, `duplio recover` says where the previous version went | `undo._describe_incomplete` | `test_recover_explains_a_crash_between_archiving_and_writing` |
| Backslashes in Linux file names stay part of the name; keep-both names obey the destination's rules | `merge.render_layout`, `_conflict` | `test_a_backslash_in_a_linux_file_name_is_part_of_the_name`, `test_keep_both_names_respect_the_destination_rules` |
| Retention always keeps the newest snapshot that had no read errors | `retention.apply_policy` | `test_retention_keeps_the_newest_snapshot_without_errors` |

## Attributes are hints, not proof

| Guarantee | Where | Test |
|---|---|---|
| A file edited within the same timestamp step as the last backup, snapshot, mirror run or sync is compared by content instead of being assumed unchanged | `util/timecmp.py::is_racy` in `Repository.backup`, `SnapshotSet.create`, `plan_mirror`, `plan_sync` | `test_backup_reads_again_a_file_edited_in_the_same_timestamp_step`, `test_snapshot_copies_a_file_edited_in_the_same_timestamp_step`, `test_mirror_compares_recent_files_by_content`, `test_sync_compares_by_content_a_file_edited_in_the_same_timestamp_step` |
| An edit that put the old size and modification time back is noticed through the change time (which users cannot set) and the inode, in backups and in sync; `--ignore-ctime` opts out | `Repository.backup`, `sync.plan_sync` (`doubtful`) | `test_backup_notices_an_edit_that_put_the_old_modification_time_back`, `test_a_file_replaced_by_another_with_the_same_size_and_time_is_noticed`, `test_an_edit_that_put_the_old_modification_time_back_is_still_synced`, `test_sync_sees_a_silent_edit_of_a_long_settled_file_through_its_change_time` |
| A change of change time alone (chmod) costs a comparison, never a copy | `plan_sync` | `test_a_permission_change_alone_is_checked_and_costs_no_copy` |
| Both sides edited in place is a conflict, not a coin toss | `plan_sync` | `test_in_place_edits_on_both_sides_are_a_conflict_not_a_coin_toss`, `test_an_edit_on_one_side_and_an_in_place_edit_on_the_other_is_a_conflict` |
| With the change time set aside, the old limit applies and is stated: size and time are the only clues | | `test_ignoring_the_change_time_leaves_size_and_time_as_the_only_clues`, `test_ignoring_the_change_time_restores_the_old_limit` |
| For long-settled files, content checks are available on request (`--rehash`, `--checksum`, `--verify-links`) | | `test_mirror_checksum_is_the_answer_for_old_files`, `test_snapshot_verify_links_catches_edits_that_kept_size_and_time` |
| A modification time in the future never wins a "newer" contest (merge, sync, diff); equal or doubtful times keep both versions | `timecmp.trusted_newer` | `test_a_time_in_the_future_never_wins_a_newer_contest`, `test_racy_and_future_rules` |
| An unknown sync-history format is dropped (first run: nothing is deleted), never misread; the previous one is upgraded in place | `syncstate.py` | `test_an_older_sync_history_is_dropped_not_misread`, `test_history_from_before_change_times_were_recorded_is_kept` |

## Repository garbage collection

`backup prune` decides what is unused. A mistake there loses backups silently, so it is the most guarded operation in the tool.

| Guarantee | Where | Test |
|---|---|---|
| What is in use is read from every snapshot's file list, not from the summaries; a missing or unreadable file list stops the run | `backup/repo_gc.py::prune` | `test_a_deleted_summary_does_not_make_its_data_look_unused`, `test_damage_to_metadata_never_costs_data`, `test_a_flaky_read_of_a_tree_cannot_make_prune_destroy_its_data` |
| A file list with no summary (lost file, interrupted backup, interrupted forget) keeps its data for a week; `backup repair` rebuilds the summary exactly, because every file list carries a copy of it | `repo_gc`, `Repository.adopt_orphan_trees` | `test_a_young_tree_without_a_summary_is_protected`, `test_an_old_tree_without_a_summary_is_set_aside_not_deleted`, `test_a_file_list_made_before_summaries_were_embedded_can_still_be_adopted` |
| Data written in the last day is never touched, whatever the locks say (a backup on another machine may still be running) | `prune(min_age_s=…)` | `test_recent_data_is_never_touched` |
| Unused data goes to `trash/` first and is deleted only by a later run after a grace period (default 1 day); `backup trash restore` undoes a run, and `backup check` points at the trash when chunks are missing | `repo_gc` | `test_prune_sets_data_aside_and_a_later_run_deletes_it`, `test_check_points_at_the_trash_when_chunks_are_missing`, `test_trash_purge_deletes_for_good`, `test_dry_run_changes_nothing` |
| Damaged file lists (truncated, flipped, wrong shape) are reported as repository errors, never as crashes or as empty lists | `Repository._read_tree` | `test_damage_to_metadata_never_costs_data` |

## Locks are leases

| Guarantee | Where | Test |
|---|---|---|
| The owner refreshes its lock every minute; others treat it as abandoned only after 20 minutes without a refresh | `backup/lockfile.py` | `test_the_owner_keeps_refreshing_the_lock`, `test_a_lock_that_is_still_being_refreshed_is_respected`, `test_a_lock_nobody_refreshes_is_taken_over` |
| Staleness is judged by the share's own clock (file age against a probe file written on the same share), never by the time written inside the file or by the local clock | `lockfile.lock_age_s` | `test_staleness_follows_the_file_not_the_clock_written_inside_it`, `test_a_lock_in_the_future_is_not_stale` |
| An owner that lost its lock stops before changing anything more; a machine that slept re-confirms the lock instead of assuming it | `Lease.check` / `verify`, called while working and right before publishing, pruning and removing | `test_a_lock_taken_over_is_noticed_by_its_former_owner`, `test_a_vanished_lock_file_counts_as_lost`, `test_a_process_that_slept_reconfirms_the_lock_instead_of_assuming_it`, `test_a_backup_that_lost_its_lock_publishes_nothing`, `test_prune_stops_before_moving_anything_when_its_lock_was_lost` |
| Breaking a stale lock moves it aside and puts back a live lock if that is what moved; an owner never removes a lock that is no longer its own | `lockfile._break_stale`, `Lease.release` | `test_breaking_a_stale_lock_never_removes_a_live_one` |

## Files that change while they are backed up

| Guarantee | Where | Test |
|---|---|---|
| A file is looked at before and after it is read (size, modification time, change time, inode) and read again if it moved; reads never go past the size first seen | `Repository._store_file` | `test_a_file_that_settles_is_read_again_and_stored_cleanly` |
| One that never holds still is stored as read but flagged: the run exits non-zero and names it, the snapshot counts it as a problem, restore warns, and the next run reads it again | `TreeEntry.unstable`, `SnapshotMeta.unstable` | `test_a_file_that_never_holds_still_is_flagged_everywhere`, `test_the_command_exits_non_zero_and_names_the_files`, `test_an_unstable_file_is_never_trusted_as_unchanged_next_time` |
| Retention does not count such a snapshot as the good one | `retention.apply_policy` | `test_retention_does_not_count_an_unstable_snapshot_as_the_good_one` |
| Hard-link snapshots flag the same way | `SnapshotSet.create` | `test_hard_link_snapshot_flags_a_file_that_changed_while_copied` |

## Interrupted anywhere

`tests/test_crash_sweep.py` runs each operation once to find every durable step it takes (journal records, writes, renames,
removals, state commits: `util/faults.py::crashpoint`) and then once per step in a forked process that is **killed at that
step** (`os._exit`, no cleanup) or **made to fail with an I/O error** there, which also models "it worked but the answer
was lost". After every interruption the invariants are checked on what is on disk, and the program has to recover. This
checks every place a run can stop, not the few somebody thought of (the method of ALICE and CrashMonkey for storage systems).

| Operation | Invariant after any interruption | After a rerun / `undo` | Test |
|---|---|---|---|
| dedupe: quarantine, delete, hard-link | every distinct content is still held somewhere; nothing torn under a final name | each content has one live copy / every file is back | `test_dedupe_never_loses_content_and_a_rerun_finishes`, `test_undo_after_an_interruption_restores_every_file` |
| merge: copy, replace, move | every content from both folders is still held | everything from A is in B / both folders as they were | `test_merge_never_loses_content_and_a_rerun_finishes`, `test_undo_after_an_interrupted_merge_restores_both_folders` |
| sync | nothing lost, nothing torn | A and B identical, and a further run has nothing to do / both as they were | `test_sync_never_loses_content_and_a_rerun_converges`, `test_undo_after_an_interrupted_sync_restores_both_folders` |
| backup | the repository checks out; the earlier snapshot restores exactly; a visible snapshot is complete or says it is not | a new backup works, pruning afterwards harms nothing | `test_backup_interrupted_anywhere_leaves_a_sound_repository` |
| forget + prune | the other snapshots restore exactly | prune completes | `test_forget_and_prune_interrupted_anywhere_keeps_the_other_snapshots` |
| hard-link snapshot | the first snapshot is untouched; no partial snapshot is listed as clean | a new one completes; forgetting one harms no other | `test_hard_link_snapshots_interrupted_anywhere_never_show_a_partial_snapshot` |
| phone import with `--free-source` | every photo is on the phone or in the library | all imported, phone freed | `test_ingest_that_frees_the_source_never_loses_a_photo` |
| mirror | nothing lost, nothing torn | destination equals source | `test_mirror_interrupted_anywhere_loses_nothing_and_a_rerun_converges` |

The default run tries every third step to keep the suite short; **`DUPLIO_SWEEP=full pytest tests/test_crash_sweep.py` tries
every step and is the gate before a release.** Errors in `fsync` are not simulated, and neither is the order in which a file
system persists unsynchronised writes after a power cut (the sweeps kill the process, not the machine).

## Notifications cannot hurt a job

A failed webhook, command or e-mail is a warning, never a failure (`test_failed_delivery_is_reported_not_raised`); messages
reach commands through environment variables, never through the shell command line
(`test_command_target_gets_the_message_in_the_environment`); webhook URLs, which carry tokens, are never printed
(`test_target_parsing_and_masking`).

## Known limits

- **Pictures:** the picture cache for USB, network, phone and cloud drives (the default; `previews settings --keep session` keeps pictures only until Duplio closes) is stored unencrypted in Duplio's cache folder; nothing is kept for the computer's own disk, and Ctrl+Shift+D (disk) and Ctrl+Shift+M (memory) in the window empty the buffers. The built-in decoder reads baseline JPEG thumbnails (what cameras embed); a progressive or unusual image is shown only when Pillow is installed, otherwise the pane says why there is no picture. A drive that Duplio cannot recognise as slow (no sign in its file-system name, volume label or file attributes) is read like a normal disk; `duplio drives PATH` shows the verdict, and `previews settings --mode always` caches everywhere.
- **Network servers:** WebDAV cannot set a modification time and FTP can only where the server has `MFMT`, so copies there carry the time of the upload (content, not time, decides what is identical). FTP has no "rename unless the name exists": the check happens just before the rename, so another program writing the same names at the same instant could be overwritten. FTP and WebDAV assume names are case-insensitive and Windows-style (the cautious choice; `duplio probe` measures the real thing). Implicit FTPS (port 990) is not supported. SFTP password login needs a Unix-like computer (keys and the agent work everywhere). A WebDAV server that quietly stores a file under another name is caught after the write, but its misnamed copy stays on the server.
- **What was and was not exercised:** the network backends are tested against WsgiDAV, pyftpdlib and paramiko's SFTP server (reached through a stand-in for the `ssh` program), not against every vendor's server and not against the real OpenSSH client. The Linux launcher is tested; `Duplio.bat` and `Duplio.command` are reviewed but have not been run on Windows or macOS. The window is tested on Linux under a virtual screen (Xvfb); it has not been run on Windows or macOS, where Tk draws fonts, scaling and the folder chooser a little differently, and it has not been tried on a Linux Wayland desktop (it runs through XWayland there). Whether a Tresorit drive is an SMB or WebDAV share on a given computer is not settled by public documentation: Duplio treats any virtual or network drive the same and `duplio drives` reports what the computer says.

- **Backups store file contents, modification time and permission bits only.** Symbolic links, empty directories, ownership, extended attributes, ACLs, macOS resource forks and NTFS alternate data streams are not stored. Quarantine moves (renames) keep everything, but the copy-based fallback used on MTP does not.
- Detection is exact. Similar-but-not-identical files are only found for images (`--similar-images`); duplicate *folders*, truncated copies and paired files (RAW+JPEG, Live Photos, sidecars) are not recognised.
- Symbolic links are skipped when scanning, so a link is never "deduplicated" and never followed out of a root.
- Hidden temporary files left by a killed process (`.<name>.duplio-tmp-…`, `.duplio-del-…`, `.duplio-link-…`) are ignored by scans. `duplio recover` removes or puts back the ones a journal names; inside backup repositories `backup prune` removes old ones.
- On Windows, paths longer than 260 characters and reserved device names depend on the OS configuration; Duplio reports the resulting error for the affected file and carries on.
- Hard links and clones share or replace storage. After a hard link, editing one name edits the other; reflinks do not have this problem. `duplio undo` turns them back into independent files.
- The system trash cannot be restored programmatically; Duplio tells you where the files went.
- A share mounted locally *and* opened as `smb://` at the same time cannot be recognised as the same storage in advance. The executor's post-action keeper check catches it and rolls the change back.
- **A lost repository password cannot be recovered.** Encryption protects contents and names; it does not hide how many objects there are, their sizes or their timestamps. `duplio-repo.json` itself is not authenticated: a changed configuration is refused when it contradicts the stored data or its own markers, but on a repository that holds no data yet nothing can reveal that its key block was removed. Changing the password re-wraps the master key and does not rotate it, so a saved copy of the old configuration still opens with the old password.
- Incremental backup, the default mirror, snapshot hard-link reuse and sync change detection trust size, modification time (within what the drive can store) and, for snapshots, permission bits, except for files modified around the time they were recorded (the racy-timestamp rule). Backups and sync also compare the change time and inode where the file system keeps them reliably (local POSIX file systems; not SMB, MTP or most network mounts), which catches an edit that restored the old modification time. Where those are unavailable, or with `--ignore-ctime`, a long-settled file edited without changing size or time is not noticed; `--rehash`, `--checksum`, `--verify-links`, `duplio manifest verify` and `backup check --read-data` are the ways to look.
- The first sync with the default `time-size` basis treats same-size, same-time files as identical without reading them; `--basis content` reads them.
- `merge`, `sync` and `snapshot` copy file contents and modification times only, with the same limits as backups (no symbolic links, ownership, extended attributes or empty folders). Hard-link snapshots keep permission bits.
- `sync` does not detect renames: a renamed file is a deletion plus a new file (the content is copied again; nothing is lost). It also cannot pair names that differ only by case or Unicode form on a drive that distinguishes them; those are left alone and reported.
- `merge --move` and `--clean-source` re-read and re-hash both copies before removing a source file, so a move reads each file about three times.
- `watch` polls; a change that is made and reverted within one interval is not seen.
- **A file system or database in use is not a consistent source.** Duplio detects a file that changes while it is read and flags it, but cannot make a set of files that belong together (a database and its log, a virtual-machine disk) consistent. Back those up from a file-system snapshot (LVM, ZFS, btrfs, APFS, VSS) using a `pre` and `post` hook in a profile.
- Backup repositories hold set-aside data for a day (`backup prune --grace`), so space returns after the grace period; `--immediate` frees it at once and removes the safety margin.
- A lock is a lease judged by the share's clock. A machine suspended longer than the lease may find its lock taken over; it then stops and the next run starts cleanly. Servers that do not set file times on writes (rare) make the lease fall back to the local clock.
- Content-defined chunking (`backup init --chunker cdc`) is slower than fixed blocks (tens of MB/s per core) and needs numpy; it is opt-in, recorded in the repository, and restoring never needs numpy.
- The crash sweeps cover the steps Duplio itself takes. They do not model a file system that reorders unsynchronised writes after a power cut, a disk that lies about `fsync`, or bit rot (the last is what `check --read-data` is for).
