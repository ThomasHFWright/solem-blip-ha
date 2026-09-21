# Watering activity

The controller-status sensor exposes `watering_activity.current` and a newest-first
`watering_activity.history` of up to 30 observations. Entries contain one `source`:

- **Manual Home Assistant**: a matching HA start command was acknowledged and watering was observed. The originating HA user name and context are retained when available; HA automation starts use this same source.
- **Scheduled**: an observed idle-to-active transition matches a saved onboard start, using HA's local timezone and a 30-second clock tolerance.
- **Manual Bluetooth**: an observed manual station run, or a program start outside its saved schedule, without a matching HA command.
- **Unknown**: activity already underway at startup/reconnection, an uncertain HA command, missing/stale/changed schedule data, a clock alarm, or an interval program whose phase cannot be reliably verified.

Scheduled and Manual Bluetooth are inferences, not controller-provided identities.
The controller does not report a phone or person. A phone start coinciding with a
scheduled time is indistinguishable. Brief runs entirely between successful polls,
and stop/restart sequences that look identical across polls, may not be detected.

`first_detected` and `finished_detected` are observation times, not exact physical
start/stop times. Program station changes and inter-station delays stay within one
run. Entries collect observed station numbers. A lost observation window or HA
restart archives the current entry as `Observation interrupted`, with no invented
finish time. A newly detected run after reconnection starts as Unknown.

Starts and finishes also create HA logbook entries. Recent activity is stored
privately with the integration across restarts. Tracking is passive: it adds no
Bluetooth queries, watering commands, scheduled starts or catch-up actions.
