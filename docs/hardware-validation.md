# Deliberate controller validation

Do not run physical watering as an incidental discovery or installation test.

1. Remove competing integrations and HA timed-watering automations. Check entities, dashboards and scripts for consumers. Retain local source revisions for code rollback. Removing an HA integration must not erase or reset device programs.
2. Install the candidate and perform fresh status, firmware, station-name and full A/B/C reads. Compare every physical station, all eight start slots, durations, calendar/date/phase, water budgets, delays and controller OFF state against MySOLEM. Preserve the locally stored raw snapshots privately.
3. Check phone access after normal reads, timeout and cancellation. Confirm that there are no overlapping BLE sessions. Verify automatic clock updates use the configured local timezone, including daylight-saving changes.
4. Choose an explicitly agreed program edit with no imminent starts. Check stale drafts by changing a setting in MySOLEM after opening the HA editor: HA must reject the old draft. Save a controlled change and compare all programs afterwards, including unused settings and interval dates. Do not deliberately interrupt a live write without an agreed recovery procedure.
5. In a supervised test, set a short onboard start and duration for an agreed safe station. Make HA's integration/Bluetooth path unavailable **before** the scheduled start. Independently observe both the physical start and physical stop while that path remains unavailable. An HA entity changing state is not sufficient evidence. Keep a person and physical stop method available.
6. Reconnect HA after the scheduled window. Confirm readback, normal status and no duplicate or catch-up watering. Restore the agreed normal device program and verify it.
7. Separately test water-budget scaling from a known normal baseline and restore it. Check a one-day delay across its countdown boundary and power/connectivity changes before making claims about elapsed hours. Confirm that every program affected by a controller-wide delay may safely be paused.
8. Enable rainfall only for explicitly selected lawn programs after these checks. Test missing/stale rainfall, phone edits, permanent OFF, longer manual delays and HA going offline with a reduced budget.

Rollback means unloading/removing the candidate and returning to an agreed integration or MySOLEM. It does not automatically restore controller programs or budgets. Restore those only from reviewed device snapshots and verify the resulting configuration. Keep device identifiers, station names, local HA details and private test records out of public issues and PRs.

Program-save diagnostics expose the transaction phase, acknowledged block count, response headers and mismatched block indexes without name payloads. The V5 save path subscribes before writing, waits for each matching `30`/`38` response, and verifies the complete configuration on the same connection. A response timeout stops the sequence without replay. Check these diagnostics alongside fresh readback before retrying an unconfirmed save; a successful BLE acknowledgement alone is not proof that settings were persisted.
