"""Short BLE sessions adapted from beelzetron/solem-blip-ble 0.3.0.

Reads use bounded retries. Mutations are sent once and their outcome checked.
One controller lock covers transactions and cancellation cleanup. A connection
whose release cannot be confirmed blocks further use until reviewed/reloaded.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any, TypeVar, cast

from bleak import BleakClient, BleakScanner
from bleak.backends.device import BLEDevice
from bleak.exc import BleakError
from bleak_retry_connector import (
    BleakClientWithServiceCache,
    BleakOutOfConnectionSlotsError,
    establish_connection,
)

from . import protocol
from .locking import controller_lock
from .snapshot import InvalidSnapshot, ProgramSnapshot, UncertainWrite
from .station_names import StationNameSnapshot, pack_station_name
from .const import (
    BLE_DEVICE_CACHE_TTL,
    COMMIT_COMMAND,
    DEFAULT_BLUETOOTH_TIMEOUT,
    IRRIGATION_CONFIG_IDLE_TIMEOUT,
    MAX_STATION_NUM,
    NOTIFY_CHAR_UUID,
    NOTIFY_PARTIAL_RETRY_DELAY,
    NOTIFY_SETTLE_DELAY,
    OPERATION_DEADLINE,
    REQUEST_MAX_ATTEMPTS,
    REQUEST_RETRY_DELAY,
    STATION_NAMES_IDLE_TIMEOUT,
    STATUS_NOTIFY_TIMEOUT,
    WRITE_CHAR_UUID,
)
from .exceptions import SolemConnectionError, SolemDeadlineExceeded

_LOGGER = logging.getLogger(__name__)

_T = TypeVar("_T")

DISCONNECT_CLEANUP_TIMEOUT = 3.0
IDENTIFICATION_NAME_TIMEOUT = 2.0

# Backend-level errors. BleakError is the base class of every backend error
# family (BleakDBusError, BleakGATTProtocolError, bleak-esphome wrappers), so
# catching the base covers all current and future backend error types.
_BACKEND_ERRORS = (BleakError, TimeoutError, OSError)


class _ConnectTimedOut(asyncio.TimeoutError):
    """Internal: the whole-operation deadline expired during the connect phase.

    Subclasses ``asyncio.TimeoutError`` so every existing except clause that
    matches the bare timeout keeps matching, while carrying a human-readable
    reason — a bare ``asyncio.TimeoutError`` has an empty ``str()`` and made
    the ``Attempt N failed:`` log line useless for triage.

    Message format: ``Connect phase timed out after Ns (device not
    advertising, out of range, or refusing connections)``.
    """


class _DropDetected(Exception):
    """Internal: the link dropped mid-operation; do not retry."""


async def _await_operation(awaitable: Awaitable[_T]) -> _T:
    return await awaitable


class StatelessSolemClient:
    """Connect-per-operation BLE client for a single Solem BL-IP controller.

    Public API mirrors ``solem_blip_ble.client.SolemClient`` (0.1.x) so the
    integration can migrate by changing the import.
    """

    def __init__(
        self,
        mac_address: str,
        bluetooth_timeout: float = DEFAULT_BLUETOOTH_TIMEOUT,
        *,
        mock: bool = False,
        max_station_num: int = MAX_STATION_NUM,
        ble_device: BLEDevice | None = None,
        ble_device_resolver: Callable[[], BLEDevice | None] | None = None,
    ) -> None:
        self.mac_address = mac_address
        self.bluetooth_timeout = bluetooth_timeout
        self.mock = mock
        self.max_station_num = max_station_num
        self._ble_device_resolver = ble_device_resolver
        self._ble_device: BLEDevice | None = ble_device
        self._ble_device_cached_at: float | None = None
        self._link_dropped = False
        self._drop_event = asyncio.Event()
        self._active_client: BleakClient | None = None
        self._connecting_client: BleakClient | None = None
        self._cleanup_tasks: set[asyncio.Task[Any]] = set()
        self._transaction_lock = controller_lock(mac_address)
        self.last_snapshot: ProgramSnapshot | None = None
        self._mock_snapshot: ProgramSnapshot | None = None
        self._mock_station_names: StationNameSnapshot | None = None
        self.station_name_write_diagnostics: dict[str, Any] = {}
        self.program_write_diagnostics: dict[str, Any] = {}

    # -- device resolution -------------------------------------------------

    async def _resolve_ble_device(self) -> BLEDevice:
        """Resolve a fresh BLEDevice, honouring the cache TTL."""
        if (
            self._ble_device is not None
            and self._ble_device_cached_at is not None
            and time.monotonic() - self._ble_device_cached_at < BLE_DEVICE_CACHE_TTL
        ):
            return self._ble_device
        self._ble_device = None

        if self._ble_device_resolver is not None:
            ble_device = self._ble_device_resolver()
            if ble_device is not None:
                self._ble_device = ble_device
                self._ble_device_cached_at = time.monotonic()
                return ble_device
            raise SolemConnectionError("Device not found! Failed connecting!")

        last_round = 2
        for round_idx in range(3):
            ble_device = await BleakScanner.find_device_by_address(
                self.mac_address, timeout=10.0
            )
            if ble_device is not None:
                self._ble_device = ble_device
                self._ble_device_cached_at = time.monotonic()
                return ble_device

            devices = await BleakScanner.discover(timeout=10.0)
            for device in devices:
                if (device.address or "").lower() == self.mac_address.lower():
                    self._ble_device = device
                    self._ble_device_cached_at = time.monotonic()
                    return device

            if round_idx < last_round:
                await asyncio.sleep(1.0)

        raise SolemConnectionError("Device not found! Failed connecting!")

    def _ble_device_callback(self) -> BLEDevice:
        if self._ble_device_resolver is not None:
            ble_device = self._ble_device_resolver()
            if ble_device is not None:
                self._ble_device = ble_device
                self._ble_device_cached_at = time.monotonic()
                return ble_device
        if self._ble_device is None:
            raise SolemConnectionError("Device not found! Failed connecting!")
        return self._ble_device

    # -- connection core ---------------------------------------------------

    def _on_disconnected(self, _client: BleakClient) -> None:
        """Backend disconnect callback: raise a drop *hint*.

        The callback alone must not fail the operation: backends deliver it
        through wrapper objects (service cache, ESPHome backend clients),
        its client argument is not reliably connection-unique, and
        ``establish_connection`` reuses one callback across its internal
        attempts — so a hint can be stale or concern a superseded
        connection. It is only consulted by :meth:`_check_drop`, which
        requires the active client to confirm it is actually disconnected
        before raising; a hint with a still-connected client is stale and
        cleared.
        """
        self._link_dropped = True
        self._drop_event.set()

    async def _connect(self) -> BleakClient:
        """Connect once, with the disconnect-callback hint armed.

        ``establish_connection`` is called with ``max_attempts=1``: the
        controller is a single-connection device that stops advertising
        during and after a connect attempt, so an immediate internal retry
        would be guaranteed to fail. Any retries happen at the
        operation level, spaced by ``REQUEST_RETRY_DELAY``.

        The hint is cleared before returning so the operation starts from
        a clean signal state and any *new* hint that arrives is meaningful.
        """
        ble_device = await self._resolve_ble_device()
        connect_kwargs: dict[str, Any] = {}
        if self._ble_device_resolver is not None:
            connect_kwargs["ble_device_callback"] = self._ble_device_callback
        owner = self

        class OwnedClient(BleakClientWithServiceCache):
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                super().__init__(*args, **kwargs)
                owner._connecting_client = self

        try:
            client = await establish_connection(
                OwnedClient,
                ble_device,
                name=f"Solem - {self.mac_address}",
                timeout=self.bluetooth_timeout,
                # Single-connection device: the controller stops advertising during
                # and for tens of seconds after any connection attempt, so an
                # immediate internal retry (bleak-retry-connector's default
                # double-tap) is guaranteed to fail and burns the operation
                # deadline. One attempt per establish_connection call; retries are
                # provided by the operation-level flat loop, spaced by
                # REQUEST_RETRY_DELAY (see REQUEST_MAX_ATTEMPTS).
                max_attempts=1,
                disconnected_callback=self._on_disconnected,
                **connect_kwargs,
            )
        except asyncio.CancelledError:
            await self._cleanup(self._connecting_client, [])
            raise
        except BleakOutOfConnectionSlotsError as exc:
            raise SolemConnectionError(
                "Bluetooth adapter/proxy out of connection slots or device busy"
            ) from exc
        except (BleakError, TimeoutError, OSError) as exc:
            raise SolemConnectionError("Failed connecting to device") from exc
        except Exception as exc:
            raise SolemConnectionError("Unexpected BLE connection error") from exc
        self._link_dropped = False
        self._drop_event.clear()
        return client

    async def _connect_within(self, remaining: float) -> BleakClient:
        """Wait for :meth:`_connect` within the remaining operation budget.

        ``asyncio.wait_for`` raises a *bare* TimeoutError whose ``str()``
        is empty, which made the ``Attempt N failed:`` log line carry no
        reason and cost live triage time. The timeout is re-raised as
        :class:`_ConnectTimedOut` — a subclass of ``asyncio.TimeoutError``
        with the connect-phase context baked in — so every existing except
        clause that matches the bare timeout keeps matching.
        """
        try:
            return await asyncio.wait_for(self._connect(), timeout=remaining)
        except asyncio.TimeoutError as exc:
            if isinstance(exc, _ConnectTimedOut):
                raise
            raise _ConnectTimedOut(
                f"Connect phase timed out after {remaining:.0f}s "
                "(device not advertising, out of range, or refusing "
                "connections)"
            ) from exc

    # -- the stateless executor --------------------------------------------

    async def _watch_drop(self, client: BleakClient) -> None:
        """Wait for a drop *confirmed* by the client itself.

        Races the hint event against a periodic liveness poll of the active
        client; the watcher only completes when ``is_connected`` is False.
        A bare hint (callback without confirmation — stale, or delivered by
        a superseded connection) never satisfies it.
        """
        while True:
            if not client.is_connected:
                return
            if self._drop_event.is_set():
                self._link_dropped = False
                self._drop_event.clear()
            await asyncio.sleep(0.25)

    def _check_drop(self) -> None:
        """Raise if the link dropped, confirmed by the active client.

        The disconnect callback is only a hint (see :meth:`_on_disconnected`).
        The hint is acted on only when the active client confirms it is no
        longer connected; a hint with a still-connected client is stale
        noise and is cleared so it cannot accumulate into a later failure.
        """
        if not self._link_dropped:
            return
        active = self._active_client
        if active is not None and not active.is_connected:
            raise _DropDetected()
        self._link_dropped = False
        self._drop_event.clear()

    def transaction(self) -> Any:
        """Hold controller ownership across several short BLE connections."""
        return self._transaction_lock.hold()

    def _retain_cleanup_task(self, task: asyncio.Task[Any]) -> None:
        self._cleanup_tasks.add(task)
        def finished(done: asyncio.Task[Any]) -> None:
            self._cleanup_tasks.discard(done)
            if not done.cancelled():
                done.exception()
        task.add_done_callback(finished)

    async def _disconnect_quietly(self, client: BleakClient) -> None:
        for _ in range(2):
            task = asyncio.create_task(client.disconnect())
            self._retain_cleanup_task(task)
            done, _ = await asyncio.wait({task}, timeout=3.0)
            if not done:
                task.cancel()
                self._transaction_lock.quarantined = True
                break
            try:
                task.result()
                if not client.is_connected:
                    return
            except Exception:
                _LOGGER.debug("BLE disconnect attempt failed", exc_info=True)
        self._transaction_lock.quarantined = True
        _LOGGER.error("BLE disconnect could not be confirmed; further operations blocked")

    async def _cleanup(
        self, client: BleakClient | None, tasks: list[asyncio.Task[Any]]
    ) -> None:
        """Bound cancellation cleanup and block reuse if tasks or links remain."""
        async def finish() -> None:
            for task in tasks:
                task.cancel()
                self._retain_cleanup_task(task)
            pending: set[asyncio.Task[Any]] = set()
            if tasks:
                _, pending = await asyncio.wait(tasks, timeout=2.0)
            if client is not None:
                await self._disconnect_quietly(client)
            if pending:
                _, pending = await asyncio.wait(pending, timeout=2.0)
                if pending:
                    self._transaction_lock.quarantined = True

        cleanup = asyncio.create_task(finish())
        cancelled = False
        while not cleanup.done():
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                cancelled = True
        cleanup.result()
        if cancelled:
            raise asyncio.CancelledError

    async def _run_operation(
        self, operation: Callable[[BleakClient], Awaitable[_T]], *,
        deadline: float | None = None, retry_safe: bool = True,
    ) -> _T:
        async with self.transaction():
            return await self._run_operation_locked(operation, deadline=deadline, retry_safe=retry_safe)

    async def _run_operation_locked(
        self,
        operation: Callable[[BleakClient], Awaitable[_T]],
        *,
        deadline: float | None = None,
        retry_safe: bool = True,
    ) -> _T:
        """Resolve, connect, run, close. Bounded, flat-retried, stateless.

        The deadline covers reads and their retries. Mutations get one attempt.
        Every attempt opens a fresh session; cleanup completes before retrying.
        Cancellation cleanup has its own bounded allowance after the deadline.
        """
        if self.mock:
            raise SolemConnectionError("mock client has no BLE operations")

        if deadline is None:
            # Read at call time so monkeypatching the module constant works.
            deadline = OPERATION_DEADLINE
        deadline_at = time.monotonic() + deadline
        last_error: Exception | None = None

        max_attempts = REQUEST_MAX_ATTEMPTS if retry_safe else 1
        for attempt in range(1, max_attempts + 1):
            if self._transaction_lock.quarantined:
                raise SolemConnectionError("Previous Bluetooth cleanup failed; connection reuse blocked")
            remaining = deadline_at - time.monotonic()
            if remaining <= 0:
                raise SolemDeadlineExceeded(
                    f"Operation deadline exceeded after {attempt - 1} attempt(s)"
                ) from last_error

            client: BleakClient | None = None
            op_task: asyncio.Task[_T] | None = None
            drop_task: asyncio.Task[Any] | None = None
            try:
                client = await self._connect_within(remaining)
                self._active_client = client
                remaining = deadline_at - time.monotonic()
                if remaining <= 0:
                    raise SolemDeadlineExceeded(
                        "Operation deadline exhausted during connect"
                    )
                op_task = asyncio.create_task(
                    _await_operation(operation(client))
                )
                drop_task = asyncio.create_task(self._watch_drop(client))
                done, _ = await asyncio.wait(
                    {op_task, drop_task},
                    timeout=remaining,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if drop_task in done:
                    # Confirmed drop (client says it is disconnected): abort
                    # the attempt immediately instead of hanging on the dead
                    # link; the flat retry reconnects fresh.
                    raise SolemConnectionError(
                        "BLE link dropped during operation"
                    )
                if op_task not in done:
                    raise asyncio.TimeoutError(
                        f"Operation deadline exceeded during attempt {attempt}"
                    )
                return op_task.result()
            except asyncio.CancelledError:
                raise
            except SolemDeadlineExceeded:
                raise
            except (
                asyncio.TimeoutError,
                BleakError,
                OSError,
                SolemConnectionError,
                _DropDetected,
            ) as exc:
                last_error = exc
                _LOGGER.debug(
                    "%s - Attempt %d failed: %s",
                    self.mac_address,
                    attempt,
                    exc,
                )
            finally:
                try:
                    await self._cleanup(client or self._connecting_client, [task for task in (op_task, drop_task) if task is not None])
                finally:
                    self._active_client = None
                    self._connecting_client = None
                    self._link_dropped = False
                    self._drop_event.clear()

            if (
                time.monotonic() < deadline_at
                and attempt < max_attempts
            ):
                await asyncio.sleep(REQUEST_RETRY_DELAY)

        if not retry_safe:
            raise UncertainWrite("Command outcome uncertain; refresh status before retrying") from last_error
        raise SolemDeadlineExceeded(
            f"Operation deadline exceeded after {max_attempts} attempt(s)"
        ) from last_error

    # -- shared operation helpers ------------------------------------------

    def _ensure_client(self, client: BleakClient, phase: str) -> None:
        if not client.is_connected:
            raise SolemConnectionError(f"Client disconnected before {phase}")
        self._check_drop()

    async def _start_notify(
        self,
        client: BleakClient,
        handler: Callable[[Any, bytearray], None],
    ) -> None:
        """Subscribe to status notifications with settle time and retries."""
        last_exc: Exception | None = None
        for attempt in range(3):
            self._check_drop()
            try:
                if attempt == 0:
                    await asyncio.sleep(NOTIFY_SETTLE_DELAY)
                else:
                    await asyncio.sleep(NOTIFY_PARTIAL_RETRY_DELAY)
                self._check_drop()
                await client.start_notify(NOTIFY_CHAR_UUID, handler)
                return
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                _LOGGER.debug(
                    "%s - start_notify attempt %s failed: %s",
                    self.mac_address,
                    attempt + 1,
                    exc,
                )
                try:
                    await client.stop_notify(NOTIFY_CHAR_UUID)
                except Exception:  # noqa: BLE001
                    pass
        raise SolemConnectionError(
            "Failed to subscribe to status notifications"
        ) from last_exc

    async def _write(self, client: BleakClient, payload: bytes) -> None:
        self._check_drop()
        if not client.is_connected:
            raise SolemConnectionError("Client disconnected before write")
        try:
            await client.write_gatt_char(WRITE_CHAR_UUID, payload, response=False)
        except BleakError as exc:
            raise SolemConnectionError("BLE write failed") from exc

    async def _stop_notify(self, client: BleakClient) -> None:
        """Subscriptions end with the mandatory disconnect in the executor."""

    async def _wait_for_event(
        self,
        event: asyncio.Event,
        timeout: float,
        what: str,
    ) -> None:
        """Wait on an event, aborting early if the link dropped."""
        wait_task = asyncio.create_task(event.wait())
        stage_deadline = time.monotonic() + timeout
        try:
            while not event.is_set():
                self._check_drop()
                remaining = stage_deadline - time.monotonic()
                if remaining <= 0:
                    raise SolemConnectionError(f"Timeout waiting for {what}")
                done, _ = await asyncio.wait(
                    {wait_task}, timeout=min(remaining, 0.5)
                )
                if wait_task in done:
                    return
                self._check_drop()
        finally:
            wait_task.cancel()

    # -- public API (mirrors 0.1.x SolemClient) ----------------------------

    async def connect(self) -> None:
        """Verify the device is reachable and exposes the write characteristic."""

        async def _op(client: BleakClient) -> None:
            services = getattr(client, "services", None)
            if services is None:
                raise SolemConnectionError("Services not available on BLE client")
            for service in services:
                for char in service.characteristics:
                    if str(char.uuid).lower() == WRITE_CHAR_UUID.lower():
                        if (
                            "write" in char.properties
                            or "write-without-response" in char.properties
                        ):
                            return
            raise SolemConnectionError("Device isn't suitable!")

        await self._run_operation(_op)

    async def get_status(self, *, include_raw: bool = False) -> dict[str, Any]:
        """Poll status via commit (triggers seq 0x02 notification)."""
        if self.mock:
            return protocol.mock_status()

        async def _op(client: BleakClient) -> dict[str, Any]:
            status_result: dict[str, Any] = {}
            status_event = asyncio.Event()

            def notification_handler(_sender: int, data: bytearray) -> None:
                parsed = protocol.parse_status_notification(
                    data, max_station_num=self.max_station_num
                )
                if parsed is not None:
                    status_result.update(parsed)
                    if include_raw:
                        status_result["raw_notification_hex"] = bytes(data).hex()
                    _LOGGER.debug(
                        "%s - Status notification (seq=2): %s",
                        self.mac_address,
                        status_result,
                    )
                    status_event.set()
                    return

                station_num = status_result.get("station_num")
                if (
                    len(data) >= 3
                    and data[2] == 0x01
                    and status_result.get("is_watering")
                    and station_num is not None
                    and status_result.get("remaining_seconds") is None
                    and (
                        remaining := protocol.parse_intermediate_remaining(
                            data,
                            station_num,
                            max_station_num=self.max_station_num,
                        )
                    )
                    is not None
                ):
                    status_result["remaining_seconds"] = remaining
                    _LOGGER.debug(
                        "%s - Remaining time from seq=1 notification: %ss (station %s)",
                        self.mac_address,
                        remaining,
                        station_num,
                    )
                    status_event.set()

            await self._start_notify(client, notification_handler)
            await asyncio.sleep(NOTIFY_SETTLE_DELAY)
            self._ensure_client(client, phase="status poll")
            try:
                await self._write(client, COMMIT_COMMAND)
                await self._wait_for_event(
                    status_event, STATUS_NOTIFY_TIMEOUT, "status notification"
                )
                if not status_result:
                    raise SolemConnectionError("Empty status notification")
                if (
                    status_result.get("is_watering")
                    and status_result.get("remaining_seconds") is None
                    and (status_result.get("station_num") or 0) >= 3
                ):
                    await asyncio.sleep(NOTIFY_SETTLE_DELAY)
                return status_result
            finally:
                await self._stop_notify(client)

        return await self._run_operation(_op)

    async def get_firmware_version(self) -> protocol.FirmwareVersion:
        """Read the firmware version stored on the V5 controller."""
        request = protocol.pack_get_firmware_version()
        if self.mock:
            return {"major": 5, "minor": 0, "patch": 0, "raw_hex": "5.0.0"}

        async def _op(client: BleakClient) -> protocol.FirmwareVersion:
            firmware_version: protocol.FirmwareVersion | None = None
            firmware_event = asyncio.Event()
            name_event = asyncio.Event()
            controller_name: str | None = None

            def notification_handler(_sender: int, data: bytearray) -> None:
                nonlocal firmware_version, controller_name
                name = protocol.parse_controller_name_response(data)
                if name is not None:
                    controller_name = name
                    name_event.set()
                _LOGGER.debug(
                    "%s - Identification notification: %s",
                    self.mac_address, bytes(data).hex(),
                )
                parsed = protocol.parse_firmware_version_response(data)
                if parsed is None:
                    return
                firmware_version = parsed
                _LOGGER.debug(
                    "%s - Firmware version notification: %s",
                    self.mac_address,
                    bytes(data).hex(),
                )
                firmware_event.set()

            await self._start_notify(client, notification_handler)
            await asyncio.sleep(NOTIFY_SETTLE_DELAY)
            self._ensure_client(client, phase="firmware version read")
            try:
                await self._write(client, request)
                await self._wait_for_event(
                    firmware_event, STATUS_NOTIFY_TIMEOUT, "firmware version"
                )
                if firmware_version is None:
                    raise SolemConnectionError("Empty firmware version response")
                if not name_event.is_set():
                    try:
                        await asyncio.wait_for(
                            name_event.wait(), IDENTIFICATION_NAME_TIMEOUT
                        )
                    except TimeoutError:
                        pass  # Name metadata is optional; firmware is already valid.
                if controller_name is not None:
                    firmware_version["controller_name"] = controller_name
                return firmware_version
            finally:
                await self._stop_notify(client)

        return await self._run_operation(_op)

    async def get_station_name(self, station: int) -> str:
        """Read a station name stored on the V5 controller."""
        if not 1 <= station <= self.max_station_num:
            raise ValueError(f"station must be between 1 and {self.max_station_num}")
        return (await self.get_station_names())[station]

    async def get_station_names(self) -> dict[int, str]:
        """Read names for each configured station from the V5 controller."""
        request = protocol.pack_get_station_names()
        if self.mock:
            snapshot = await self.get_station_name_snapshot()
            return {station: name for station, name in snapshot.names.items() if station <= self.max_station_num}

        async def _op(client: BleakClient) -> dict[int, str]:
            fragments: dict[int, dict[int, bytes]] = {}
            station_names: dict[int, str] = {}
            last_fragment_at: float | None = None

            def notification_handler(_sender: int, data: bytearray) -> None:
                nonlocal last_fragment_at
                parsed = protocol.parse_station_name_fragment(data)
                if parsed is None:
                    return
                last_fragment_at = time.monotonic()
                if not 1 <= parsed["station"] <= self.max_station_num:
                    return
                station = parsed["station"]
                fragments.setdefault(station, {})[parsed["sequence"]] = parsed[
                    "name_bytes"
                ]
                _LOGGER.debug(
                    "%s - Station %s name fragment (seq=%s): %s",
                    self.mac_address,
                    station,
                    parsed["sequence"],
                    bytes(data).hex(),
                )
                if fragments[station].keys() >= {0, 1}:
                    station_fragments = fragments[station]
                    station_names[station] = (
                        station_fragments[1] + station_fragments[0]
                    ).decode("utf-8", errors="replace")

            async def _wait_for_station_names() -> dict[int, str]:
                stage_deadline = time.monotonic() + STATUS_NOTIFY_TIMEOUT
                while True:
                    now = time.monotonic()
                    if (
                        station_names
                        and last_fragment_at is not None
                        and now - last_fragment_at >= STATION_NAMES_IDLE_TIMEOUT
                    ):
                        return station_names
                    if now >= stage_deadline:
                        if station_names:
                            return station_names
                        raise SolemConnectionError(
                            "Timeout waiting for station names"
                        )
                    self._check_drop()
                    await asyncio.sleep(0.05)

            await self._start_notify(client, notification_handler)
            await asyncio.sleep(NOTIFY_SETTLE_DELAY)
            self._ensure_client(client, phase="station names read")
            try:
                await self._write(client, request)
                return await _wait_for_station_names()
            finally:
                await self._stop_notify(client)

        return await self._run_operation(_op)

    async def get_station_name_snapshot(self) -> StationNameSnapshot:
        """Read all name fragments, rejecting partial or conflicting results."""
        if self.mock:
            if self._mock_station_names is None:
                self._mock_station_names = StationNameSnapshot({
                    i: f"Station {i}".encode().ljust(32, b"\0") for i in range(1, 13)
                })
            return self._mock_station_names

        async def _op(client: BleakClient) -> StationNameSnapshot:
            frames: list[bytes] = []

            def notification_handler(_sender: int, data: bytearray) -> None:
                if data[:2] in (b"\x36\x12", b"\x35\x12"):
                    frames.append(bytes(data))

            await self._start_notify(client, notification_handler)
            try:
                await asyncio.sleep(NOTIFY_SETTLE_DELAY)
                return await self._read_station_names_on_connection(client, frames)
            finally:
                await self._stop_notify(client)

        return await self._run_operation(_op)

    async def _read_station_names_on_connection(
        self, client: BleakClient, frames: list[bytes],
    ) -> StationNameSnapshot:
        """Collect a complete response through the session's active subscriber."""
        frames.clear()
        await self._write(client, protocol.pack_get_station_names())
        deadline = time.monotonic() + STATUS_NOTIFY_TIMEOUT
        last_received = time.monotonic()
        count = len(frames)
        while time.monotonic() < deadline:
            self._check_drop()
            if len(frames) != count:
                count = len(frames)
                last_received = time.monotonic()
            if time.monotonic() - last_received >= STATION_NAMES_IDLE_TIMEOUT:
                try:
                    return StationNameSnapshot.from_frames(frames, self.max_station_num)
                except InvalidSnapshot:
                    pass
            await asyncio.sleep(0.05)
        raise InvalidSnapshot("Timeout waiting for complete station names")

    async def write_station_name(
        self, station: int, name: str, expected: StationNameSnapshot,
        *, before: StationNameSnapshot,
    ) -> StationNameSnapshot:
        """Check, write once and verify names with one short, subscribed session."""
        frames = pack_station_name(station, name, self.max_station_num)
        if self.mock:
            self._mock_station_names = expected
            return expected

        async def _op(client: BleakClient) -> StationNameSnapshot:
            read_frames: list[bytes] = []
            acknowledged = asyncio.Event()
            rejected = False
            expected_header: bytes | None = None
            self.station_name_write_diagnostics = {"phase": "subscribe", "acknowledged_parts": 0}

            def notification_handler(_sender: int, data: bytearray) -> None:
                nonlocal rejected
                if self.station_name_write_diagnostics["phase"] in ("preflight", "readback"):
                    if data[:2] in (b"\x36\x12", b"\x35\x12"):
                        read_frames.append(bytes(data))
                    return
                self.station_name_write_diagnostics["last_reply_header"] = bytes(data[:4]).hex()
                self.station_name_write_diagnostics["last_reply_length"] = len(data)
                if bytes(data) == b"\x34\x00":
                    acknowledged.set()
                    return
                if len(data) < 3 or data[0] != 0x34:
                    return
                # Name-write replies echo the part index and output index.
                # Unlike name READ replies, byte 2 is not a countdown.
                # F0 signals a rejected/unsupported command, not success.
                if data[2] == 0xF0 or (len(data) > 3 and data[3] == 0xF0):
                    rejected = True
                    acknowledged.set()
                elif len(data) == 20 and bytes(data[:4]) == expected_header:
                    acknowledged.set()

            await self._start_notify(client, notification_handler)
            try:
                await asyncio.sleep(NOTIFY_SETTLE_DELAY)
                self.station_name_write_diagnostics["phase"] = "preflight"
                current = await self._read_station_names_on_connection(client, read_frames)
                if current.revision != before.revision:
                    raise UncertainWrite("Station names changed before writing; refresh the editor")
                for part, frame in enumerate(frames):
                    expected_header = b"\x34" + frame[1:4]
                    acknowledged.clear()
                    self.station_name_write_diagnostics.update(phase="await_ack", part=part)
                    await self._write(client, frame)
                    await self._wait_for_event(
                        acknowledged, STATUS_NOTIFY_TIMEOUT,
                        f"station-name part {part} acknowledgement",
                    )
                    if rejected:
                        raise UncertainWrite("Controller rejected the station-name command")
                    self.station_name_write_diagnostics["acknowledged_parts"] = part + 1
                self.station_name_write_diagnostics["phase"] = "readback"
                actual = await self._read_station_names_on_connection(client, read_frames)
                if actual.revision != expected.revision:
                    self.station_name_write_diagnostics["phase"] = "readback_mismatch"
                    raise UncertainWrite("Station-name verification failed; reopen the editor to review")
                self.station_name_write_diagnostics["phase"] = "verified"
                return actual
            finally:
                await self._stop_notify(client)

        return await self._run_operation(_op, retry_safe=False)

    async def get_irrigation_config(
        self,
    ) -> dict[int, protocol.IrrigationProgram]:
        """Read persisted irrigation programs (A/B/C) from the V5 controller."""
        if self.mock:
            if self._mock_snapshot is None:
                frames = []
                for index in range(3):
                    program: protocol.IrrigationProgram = {
                        "name": f"Program {chr(65 + index)}", "inter_station_delay": 0,
                        "water_budget": 100, "cycle": 0, "week_days": 127,
                        "period_length": 1, "synchro_day": 0, "period_start_date": None,
                        "start_times": [None] * 8, "station_durations": [0] * 12,
                    }
                    for chunk, frame in enumerate(protocol.pack_set_irrigation_program(index, program, max_stations=12)):
                        frames.append(frame[:2] + bytes([6-chunk]) + frame[3:])
                self._mock_snapshot = ProgramSnapshot.from_frames(tuple(frames))
            self.last_snapshot = self._mock_snapshot
            return self.last_snapshot.programs

        async def _op(client: BleakClient) -> dict[int, protocol.IrrigationProgram]:
            payloads: list[bytes] = []

            def notification_handler(_sender: int, data: bytearray) -> None:
                if protocol.normalize_config_notification(data) is not None:
                    payloads.append(bytes(data))

            await self._start_notify(client, notification_handler)
            try:
                await asyncio.sleep(NOTIFY_SETTLE_DELAY)
                snapshot = await self._read_programs_on_connection(client, payloads)
                self.last_snapshot = snapshot
                return snapshot.programs
            finally:
                await self._stop_notify(client)

        return await self._run_operation(_op)

    async def _read_programs_on_connection(
        self, client: BleakClient, payloads: list[bytes],
    ) -> ProgramSnapshot:
        """Read all fragments using an already subscribed, owned connection."""
        payloads.clear()
        await self._write(client, protocol.pack_get_irrigation_config())
        deadline = time.monotonic() + STATUS_NOTIFY_TIMEOUT
        previous_count = -1
        last_fragment_at = time.monotonic()
        while True:
            now = time.monotonic()
            if len(payloads) != previous_count:
                previous_count = len(payloads)
                last_fragment_at = now
            complete = protocol.irrigation_config_complete(payloads)
            if complete and (now - last_fragment_at >= IRRIGATION_CONFIG_IDLE_TIMEOUT or now >= deadline):
                return ProgramSnapshot.from_frames(tuple(payloads))
            if now >= deadline:
                raise SolemConnectionError("Timeout waiting for irrigation config")
            self._check_drop()
            await asyncio.sleep(0.05)

    async def get_program_snapshot(self) -> ProgramSnapshot:
        await self.get_irrigation_config()
        assert self.last_snapshot is not None
        return self.last_snapshot

    async def write_program_frames(self, frames: list[bytes], expected: ProgramSnapshot) -> ProgramSnapshot:
        """Acknowledge each block and verify all settings on one connection.

        The manager owns the fresh snapshot and durable journal. Never replay a
        write, send a manual-command commit, or weaken full readback verification.
        """
        if self.mock:
            self._mock_snapshot = self.last_snapshot = expected
            return expected
        before = self.last_snapshot
        if before is None:
            raise InvalidSnapshot("Read programs before writing")

        async def _op(client: BleakClient) -> ProgramSnapshot:
            payloads: list[bytes] = []
            acknowledged = asyncio.Event()
            expected_header = b""
            expected_length = 0
            rejected = False
            self.program_write_diagnostics = {"phase": "subscribe", "acknowledged_blocks": 0}
            diagnostic = self.program_write_diagnostics

            def notification_handler(_sender: int, data: bytearray) -> None:
                nonlocal rejected
                if diagnostic["phase"] in ("preflight", "readback"):
                    if protocol.normalize_config_notification(data) is not None:
                        payloads.append(bytes(data))
                    return
                # Retain only headers/counts: never include user names/payloads.
                diagnostic["last_reply_header"] = bytes(data[:4]).hex()
                diagnostic["last_reply_length"] = len(data)
                if not data or not expected_header or data[0] != expected_header[0]:
                    return
                if len(data) >= 3 and (data[2] == 0xF0 or (len(data) > 3 and data[3] == 0xF0)):
                    rejected = True
                    acknowledged.set()
                elif bytes(data) == expected_header[:1] + b"\x00":
                    acknowledged.set()
                elif len(data) == expected_length and bytes(data[:4]) == expected_header:
                    acknowledged.set()

            await self._start_notify(client, notification_handler)
            try:
                await asyncio.sleep(NOTIFY_SETTLE_DELAY)
                diagnostic["phase"] = "preflight"
                current = await self._read_programs_on_connection(client, payloads)
                if current.revision != before.revision:
                    raise UncertainWrite("Programs changed before writing; refresh the editor")
                for block, frame in enumerate(frames):
                    expected_header = bytes([frame[0] + 1]) + frame[1:4]
                    expected_length = len(frame)
                    acknowledged.clear()
                    diagnostic.update(phase="await_ack", block=block)
                    await self._write(client, frame)
                    await self._wait_for_event(acknowledged, STATUS_NOTIFY_TIMEOUT,
                                               f"program block {block} acknowledgement")
                    if rejected:
                        raise UncertainWrite("Controller rejected the program block")
                    diagnostic["acknowledged_blocks"] = block + 1
                diagnostic["phase"] = "readback"
                actual = await self._read_programs_on_connection(client, payloads)
                self.last_snapshot = actual
                if actual.revision != expected.revision:
                    diagnostic["phase"] = "readback_mismatch"
                    diagnostic["mismatched_blocks"] = {
                        str(index): [chunk for chunk, (left, right) in enumerate(zip(actual.blocks[index], expected.blocks[index]))
                                     if left != right]
                        for index in expected.blocks if actual.blocks[index] != expected.blocks[index]
                    }
                    diagnostic["extra_frames_changed"] = actual.extras != expected.extras
                    raise UncertainWrite("Program verification failed; refresh and review the controller")
                diagnostic["phase"] = "verified"
                return actual
            finally:
                await self._stop_notify(client)

        try:
            return await self._run_operation(_op, retry_safe=False)
        except (Exception, asyncio.CancelledError):
            _LOGGER.warning("Program save unconfirmed (%s)", self.program_write_diagnostics)
            raise

    async def set_time(self, when: datetime | None = None) -> None:
        """Set local time once; wait for its reply and verify the clock alarm clears."""
        if self.mock:
            return

        async def _op(client: BleakClient) -> None:
            received = asyncio.Event()
            status: dict[str, Any] = {}
            reply: bytes | None = None
            phase = "status"

            def notification_handler(_sender: int, data: bytearray) -> None:
                nonlocal reply
                if phase == "time":
                    if len(data) >= 2 and data[0] == 0x04:
                        reply = bytes(data)
                        received.set()
                elif data[:2] in (b"\x32\x10", b"\x3c\x10"):
                    parsed = protocol.parse_status_notification(data, max_station_num=self.max_station_num)
                    if parsed is not None:
                        status.update(parsed)
                        received.set()

            async def read_status() -> None:
                received.clear()
                status.clear()
                await self._write(client, COMMIT_COMMAND)
                await self._wait_for_event(received, STATUS_NOTIFY_TIMEOUT, "clock status")

            await self._start_notify(client, notification_handler)
            try:
                await asyncio.sleep(NOTIFY_SETTLE_DELAY)
                await read_status()
                if status["is_watering"] or status.get("active_program") is not None:
                    raise SolemConnectionError("Clock update deferred while watering")
                phase = "time"
                received.clear()
                await self._write(client, protocol.pack_set_time(when))
                await self._wait_for_event(received, STATUS_NOTIFY_TIMEOUT, "time-setting reply")
                assert reply is not None
                if len(reply) >= 3 and (reply[2] == 0xF0 or (len(reply) > 3 and reply[3] == 0xF0)):
                    raise SolemConnectionError(f"Controller rejected time update ({reply[:4].hex()})")
                phase = "status"
                # 3b00 here is a read-only status request, not a time-command commit.
                await read_status()
                if status["time_alarm"]:
                    raise SolemConnectionError(f"Clock alarm remains set after time update ({reply[:4].hex()})")
            finally:
                await self._stop_notify(client)

        try:
            await self._run_operation(_op, retry_safe=False)
        except UncertainWrite as err:
            raise UncertainWrite(f"Clock sync not verified: {err.__cause__ or err}") from err

    async def _execute_command(
        self,
        command: bytes,
    ) -> protocol.SolemStatus | None:
        """Send command + commit and wait for device notification ack."""
        if self.mock:
            return None

        async def _op(client: BleakClient) -> protocol.SolemStatus | None:
            response_event = asyncio.Event()
            last_status: protocol.SolemStatus | None = None

            def notification_handler(_sender: int, data: bytearray) -> None:
                nonlocal last_status
                if not protocol.is_command_notification(data):
                    return
                parsed = protocol.parse_status_notification(
                    data, max_station_num=self.max_station_num
                )
                if parsed is not None:
                    last_status = parsed
                    response_event.set()
                _LOGGER.debug(
                    "%s - Command notification (seq=%s): %s",
                    self.mac_address,
                    data[2],
                    bytes(data).hex(),
                )
                if data[2] == 0x00:
                    response_event.set()

            await self._start_notify(client, notification_handler)
            await asyncio.sleep(NOTIFY_SETTLE_DELAY)
            self._ensure_client(client, phase="command")
            try:
                await self._write(client, command)
                await self._write(client, protocol.pack_commit())
                await self._wait_for_event(
                    response_event, STATUS_NOTIFY_TIMEOUT, "command response"
                )
                return last_status
            finally:
                await self._stop_notify(client)

        async with self.transaction():
            status = await self._run_operation(_op, retry_safe=False)
            if status is None or not self._command_matches(command, status):
                try:
                    status = cast(protocol.SolemStatus, await self.get_status())
                except Exception as err:
                    raise UncertainWrite("Command sent but status verification failed") from err
            if not self._command_matches(command, status):
                raise UncertainWrite("Controller status does not confirm the requested command")
            return status

    @staticmethod
    def _command_matches(command: bytes, status: protocol.SolemStatus) -> bool:
        opcode = command[2]
        if opcode == 0xA0:
            return status["controller_state"] == "On"
        if opcode == 0xC0:
            return (status["controller_state"] == "Off"
                    and status["controller_off_days_remaining"] == command[4])
        if opcode == 0x15:
            return not status["is_watering"]
        if opcode == 0x12:
            return status["is_watering"] and status["station_num"] == command[3]
        if opcode == 0x14:
            return status["is_watering"] and status["active_program"] == command[4]
        return opcode == 0x11 and status["is_watering"]

    async def turn_on(self) -> None:
        if self.mock:
            return
        await self._execute_command(protocol.pack_turn_on())

    async def turn_off_permanent(self) -> None:
        if self.mock:
            return
        await self._execute_command(protocol.pack_turn_off_permanent())

    async def turn_off_x_days(self, days: int) -> None:
        if self.mock:
            return
        await self._execute_command(protocol.pack_turn_off_x_days(days))

    async def sprinkle_station_x_for_y_minutes(
        self, station: int, minutes: int
    ) -> protocol.SolemStatus | None:
        if self.mock:
            return None
        return await self._execute_command(
            protocol.pack_sprinkle_station(station, minutes)
        )

    async def sprinkle_all_stations_for_y_minutes(
        self, minutes: int
    ) -> protocol.SolemStatus | None:
        if self.mock:
            return None
        return await self._execute_command(protocol.pack_sprinkle_all_stations(minutes))

    async def run_program_x(self, program: int) -> None:
        if self.mock:
            return
        await self._execute_command(protocol.pack_run_program(program))

    async def stop_manual_sprinkle(self) -> None:
        if self.mock:
            return
        await self._execute_command(protocol.pack_stop_manual_sprinkle())
