# Solem BL-IP for Home Assistant

Manage the original Solem BL-IP controller's onboard A/B/C watering programs from Home Assistant. The controller owns scheduled starts and stops. Home Assistant does not run a parallel watering schedule or issue catch-up starts after reconnecting.

Based on [beelzetron/solem-blip-ha](https://github.com/beelzetron/solem-blip-ha). The MIT-licensed BLE implementation is bundled inside this integration with its [attribution](custom_components/solem_blip/ble/NOTICE.md). No separate Solem Toolkit, Solem BLE package or custom dashboard card is needed. Generic Bluetooth packages are supplied by Home Assistant's Bluetooth integration.

Requires Home Assistant 2026.3 or newer. Program editing supports original BL-IP firmware 5.x; firmware 6.x is not supported.

## Installation

Copy `custom_components/solem_blip` into Home Assistant's `custom_components` directory and restart HA. Add **Solem BL-IP** through Settings → Devices & services, select the discovered controller, and specify the physical station count. Use only one integration for the controller's Bluetooth connection.

Setup reads existing settings. It never uploads default or cached programs. Automatic clock synchronization remains enabled, using HA's configured local timezone after a successful idle status read. BLE connections are short, serialized per controller, and disconnected after each operation.

HA reads the controller display name from the optional identification response during its firmware read. A confirmed name is cached for offline starts and used for the default device label; custom HA names and entity IDs are preserved. Missing or malformed name responses leave the existing name intact. It does not use or correct Bluetooth advertisement names. The read keeps the same short connection open for at most two additional seconds if the optional name is absent; no rename command is sent. Reload the integration to read a changed onboard controller name.

## Programs and controls

Use **Configure → Edit program** for the onboard program editor. It supports three programs, eight start slots per program, calendar modes, station durations, inter-station delay and water budget. Station labels come from the controller. Editing does not turn the controller ON or start watering.

Opening the editor reads all programs. Saving reads them again and refuses a stale draft if any settings have changed. Only edited fields are patched; other programs, unused station slots and uninterpreted bytes are preserved. Unsupported block layouts and interval programs with unknown date/phase are rejected instead of filled with guessed values.

Writes use the inferred upstream seven-frame `2f`/`37` sequence, without the manual-command `3b00` commit. Every write is followed by a complete readback, including the interval date. The protocol does not establish atomic interrupted-write behaviour. A private HA storage journal records the before/expected configurations before transmitting. A failed or cancelled write is not replayed automatically; further edits are blocked until a fresh read reconciles it.

The controller status sensor exposes `program_revision`, `programs_last_read`, `programs_last_write`, `program_write_uncertain`, `program_error` and `rainfall_status`. A failed read leaves the last complete snapshot visible with its timestamp/error. If a fresh read differs from both the before and expected snapshots, inspect the current programs and use **Accept current programs** before making another edit.

Available actions:

- `solem_blip.refresh_programs`: await a complete fresh read.
- `solem_blip.set_program`: select a device and program (1–3), provide the revision from when the draft was opened, and supply only fields to change. `station_durations` maps physical station numbers to seconds; omitted stations are preserved. `start_times`, when supplied, replaces all eight slots; omitted trailing slots are disabled.
- `solem_blip.accept_current_programs`: explicitly accept fresh settings after an uncertain write; this action itself does not write to the device.
- `solem_blip.apply_rainfall`: apply the configured rainfall discount once.

Manual station/program start, stop, permanent ON/OFF and temporary rain-delay controls remain available. They act immediately when deliberately invoked. Program schedule sensors are estimates for display, particularly interval schedules; they do not trigger irrigation. Native HA tiles can show these sensors, with a navigation button to the integration's Configure menu for editing.

## Onboard station names

Use **Configure → Rename onboard stations**, select a physical station, and save its new name. This changes the name stored on the controller and displayed in MySOLEM. Home Assistant entity IDs and custom display-name overrides are preserved; entities without an override follow the onboard name.

Names must be non-empty and fit 32 UTF-8 bytes. Accents and emoji can occupy multiple bytes; long names are rejected rather than truncated. The editor requires a complete fresh read and an idle V5 controller. Saving the unchanged name sends no name-write frames.

Saving compares every output name with the opened draft. The write session first reads and checks the complete names again, then writes only the selected station and verifies all output names on that same subscribed connection. This checks notification delivery before mutation and avoids reconnecting between the write and its verification. The connection is released after the transaction, including on cancellation. The V5 name command is two `33 12` frames with part indices 0 and 1, a zero-based output index, and 16 bytes of zero-padded name data each. Each name frame waits for its `34` acknowledgement before the next frame or disconnect. Full name-write acknowledgements echo the part index (0 or 1) and zero-based output index; this is distinct from the countdown used in name-read responses. Only the matching part/output acknowledgement advances the write; unrelated notifications do not count as acknowledgement. It does not use the manual-command `3b00` commit or program writes. Permanent OFF and watering programs are preserved.

A private journal records an in-progress save before transmission. Interrupted writes are never replayed automatically. Reopen the editor to read the controller again; if the result matches neither the old nor intended names, review and explicitly accept the current names before another edit.

## Rainfall adjustment

Configure **Rainfall adjustment** with an existing rolling 24-hour rainfall total in millimetres. Do not integrate an already accumulated total again. Explicitly select the lawn programs and confirm their normal water-budget baselines. Leave other programs unselected.

`fraction = clamp(1 - rainfall_24h / target_mm, 0, 1)`

For an illustrative 4 mm target, totals of 0, 1, 2, 3 and 4+ mm give 100%, 75%, 50%, 25% and a complete skip. The target is configurable; it is not a daily watering recommendation or a soil-moisture model. The existing onboard watering cadence stays unchanged.

Partial discounts always use the confirmed baseline, not the previous reduced budget. Automatic application is opt-in and checks hourly while HA is running. Otherwise use the action manually. Missing, unavailable, negative, non-finite, restored or stale rainfall is rejected. After integration startup, a new report is required before using the sensor. External program or budget changes pause adjustment until the normal baseline is reviewed.

Automatic adjustment never writes a persistent 0% budget. A complete skip requires separate permission for a whole-controller, one-day rain delay, and is refused if an unselected active program would also be paused. Permanent OFF and existing delays are preserved. The delay is requested at most once per observed wet episode; an uncertain request is not replayed. A one-day setting follows the controller's countdown rules and is not claimed to mean exactly 24 hours.

The last saved settings persist when HA, Bluetooth or the network is unavailable. A reduced budget can therefore persist indefinitely offline; automatic offline restoration to 100% is not provided. Disable automatic adjustment before manually restoring/reconfiguring a baseline. Disabling the feature itself sends no device commands.

Validate budget scaling, delay countdown and interval behaviour on the target firmware before enabling automatic rainfall adjustment. Follow the [hardware validation procedure](docs/hardware-validation.md) for a deliberate watering test.

## Development

```sh
uv sync --frozen --extra dev
uv run pytest
uv run mypy custom_components/solem_blip
python -m compileall -q custom_components
```

Tests use synthetic BLE responses and software fixtures. Hardware acceptance and release procedures are separate. See [AGENTS.md](AGENTS.md) and [branching and release](docs/branching_and_release.md).

Controllers that omit service identifiers from their advertisements can be added by entering their Bluetooth MAC address in setup. The controller must still be visible to a connectable Home Assistant Bluetooth adapter or proxy.

The V5 twelve-slot response is preserved in full, including the nine additional storage slots. Only A/B/C are exposed for editing; whole-controller rainfall delays also check the additional slots for active schedules.
