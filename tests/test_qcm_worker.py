"""Tests for the QCMWorker registry lifecycle and poll loop."""

import asyncio
import logging
from importlib.metadata import EntryPoint

import msgspec
import pytest
from _helpers import FAKE_DRIVER_NAME, _FakeQCM, register_fake_driver
from pymodbus.exceptions import ConnectionException
from scietex.hal.qcm.base.data import FTMParameters
from scietex.hal.serial import ModbusOperationError
from scietex.service import ValkeyWorker, ValkeyWorkerConfig

from scietex.qcm_service import qcm_worker as worker_module
from scietex.qcm_service.config import (
    QCM_CONFIG_FILE,
    QCM_CONFIG_SUBDIR,
    ModbusGatewaySettings,
    QCMServiceSettings,
    QCMSettings,
)
from scietex.qcm_service.qcm_worker import (
    CONNECTED_FAILURE_THRESHOLD,
    DRIVER_ENTRY_POINT_GROUP,
    STREAM_MAXLEN,
    QCMWorker,
    encode_value,
    resolve_driver,
)
from scietex.qcm_service.telemetry import build_telemetry

#: A module-level non-class, used to exercise the "resolved to a non-class" path.
_NOT_A_CLASS = "just a string, not a class"


@pytest.fixture(autouse=True)
def _fake_driver(monkeypatch):
    """Advertise the shared fake driver for every test in this module."""
    register_fake_driver(monkeypatch)


def _make_worker(tmp_path) -> QCMWorker:
    """Build a worker with a real conf_dir and no remote-config handlers."""
    return QCMWorker(
        ValkeyWorkerConfig(
            service_name="test",
            conf_dir=str(tmp_path),
            remote_config_enabled=False,
        )
    )


def _write_config(tmp_path, devices, gateway=None) -> None:
    """Write a qcm.yml with the given devices and gateway."""
    path = tmp_path / QCM_CONFIG_SUBDIR / QCM_CONFIG_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(
        msgspec.yaml.encode(
            QCMServiceSettings(
                gateway=gateway if gateway is not None else ModbusGatewaySettings(),
                devices=devices,
            )
        )
    )


def _write_raw(tmp_path, text: str) -> None:
    """Write raw text to qcm.yml (for malformed-file tests)."""
    path = tmp_path / QCM_CONFIG_SUBDIR / QCM_CONFIG_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def _device(worker: QCMWorker, label: str) -> _FakeQCM:
    """The live fake device instance for ``label`` (the registry is live)."""
    return worker.devices[label]


def test_resolve_driver_resolves_class() -> None:
    """An advertised entry point name resolves to the driver class."""
    assert resolve_driver(FAKE_DRIVER_NAME) is _FakeQCM


def test_resolve_driver_unknown_name() -> None:
    """A name no entry point advertises raises ValueError."""
    with pytest.raises(ValueError, match="Unknown driver"):
        resolve_driver("no_such_driver")


def test_resolve_driver_non_class(monkeypatch) -> None:
    """An entry point resolving to a non-class raises ValueError."""
    entry_point = EntryPoint(
        name="not_a_class",
        value=f"{__name__}:_NOT_A_CLASS",
        group=DRIVER_ENTRY_POINT_GROUP,
    )
    monkeypatch.setattr(worker_module, "entry_points", lambda group: [entry_point])

    with pytest.raises(ValueError, match="not a class"):
        resolve_driver("not_a_class")


def test_encode_value_roundtrips_and_marks_none() -> None:
    """``None`` encodes as an empty value; a float round-trips exactly."""
    assert encode_value(None) == b""
    assert encode_value(0.0) == b"0.0"
    assert encode_value(1.2345678901234567) == b"1.2345678901234567"


@pytest.mark.asyncio
async def test_initialize_fails_on_unparseable_yaml(tmp_path, caplog) -> None:
    """An unparseable qcm.yml fails startup with an error log."""
    _write_raw(tmp_path, "unterminated: [flow\n")
    worker = _make_worker(tmp_path)

    with caplog.at_level(logging.ERROR):
        assert await worker.initialize() is False

    assert any("Failed to read QCM configuration" in record.message for record in caplog.records)


@pytest.mark.asyncio
async def test_initialize_fails_on_bad_driver(tmp_path, caplog) -> None:
    """A device whose driver name cannot be resolved fails startup."""
    _write_config(tmp_path, [QCMSettings(label="x", driver="nonexistent_driver")])
    worker = _make_worker(tmp_path)

    with caplog.at_level(logging.ERROR):
        assert await worker.initialize() is False

    assert any("Failed to build QCM registry" in record.message for record in caplog.records)


@pytest.mark.asyncio
async def test_initialize_fails_on_duplicate_labels(tmp_path, caplog) -> None:
    """Two devices sharing a label fail startup (labels are the registry key)."""
    _write_config(
        tmp_path,
        [
            QCMSettings(label="dup", driver=FAKE_DRIVER_NAME),
            QCMSettings(label="dup", driver=FAKE_DRIVER_NAME),
        ],
    )
    worker = _make_worker(tmp_path)

    with caplog.at_level(logging.ERROR):
        assert await worker.initialize() is False

    assert any("Duplicate device label" in record.message for record in caplog.records)


@pytest.mark.asyncio
async def test_initialize_succeeds_with_empty_devices(tmp_path, monkeypatch) -> None:
    """An empty device list is a valid state and initializes successfully."""

    async def fake_initialize(self) -> bool:
        return True

    monkeypatch.setattr(ValkeyWorker, "initialize", fake_initialize)
    _write_config(tmp_path, [])
    worker = _make_worker(tmp_path)

    assert await worker.initialize() is True
    assert worker.devices == {}
    assert worker.device_settings == {}


@pytest.mark.asyncio
async def test_initialize_builds_registry_from_yaml(tmp_path, monkeypatch) -> None:
    """With no remote config, the registry is seeded from qcm.yml."""

    async def fake_initialize(self) -> bool:
        return True

    monkeypatch.setattr(ValkeyWorker, "initialize", fake_initialize)
    _write_config(tmp_path, [QCMSettings(label="qcm-1", driver=FAKE_DRIVER_NAME, unit_id=3)])
    worker = _make_worker(tmp_path)

    assert await worker.initialize() is True
    assert set(worker.devices) == {"qcm-1"}
    assert worker.device_settings["qcm-1"].unit_id == 3
    assert worker.qcm_settings is not None
    assert worker.qcm_settings.devices[0].label == "qcm-1"
    await worker.cleanup()


@pytest.mark.asyncio
async def test_initialize_rebuilds_from_merged_settings(tmp_path, monkeypatch) -> None:
    """The merged section replaces the YAML list wholesale, not by label.

    Approach: monkeypatch ``ValkeyWorker.initialize`` to skip the live connect,
    and monkeypatch the worker's ``current_config_settings`` accessor to return
    a struct that differs from the YAML — the authoritative merged config.
    """

    async def fake_initialize(self) -> bool:
        return True

    monkeypatch.setattr(ValkeyWorker, "initialize", fake_initialize)
    _write_config(tmp_path, [QCMSettings(label="yaml", driver=FAKE_DRIVER_NAME)])
    worker = _make_worker(tmp_path)

    merged = QCMServiceSettings(
        gateway=ModbusGatewaySettings(host="remote", port=5020),
        devices=[QCMSettings(label="remote", driver=FAKE_DRIVER_NAME)],
    )
    monkeypatch.setattr(worker, "current_config_settings", lambda name: merged)

    assert await worker.initialize() is True
    assert set(worker.devices) == {"remote"}
    assert "yaml" not in worker.devices
    assert worker.qcm_settings.gateway.host == "remote"
    await worker.cleanup()


@pytest.mark.asyncio
async def test_build_devices_passes_gateway_and_unit_id(tmp_path) -> None:
    """The gateway endpoint and the device unit id reach the driver constructor."""
    worker = _make_worker(tmp_path)
    await worker._build_devices(
        QCMServiceSettings(
            gateway=ModbusGatewaySettings(host="gw", port=5020, timeout=2.5),
            devices=[QCMSettings(label="a", driver=FAKE_DRIVER_NAME, unit_id=3)],
        )
    )

    device = _device(worker, "a")
    assert device.host == "gw"
    assert device.port == 5020
    assert device.unit_id == 3
    assert device.timeout == 2.5
    assert device.label == "a"
    await worker.close_devices()


@pytest.mark.asyncio
async def test_start_monitoring_selects_devices(tmp_path) -> None:
    """start_monitoring starts one, all, and rejects unknown labels; no double-starts."""
    worker = _make_worker(tmp_path)
    await worker._build_devices(
        QCMServiceSettings(
            devices=[
                QCMSettings(label="a", driver=FAKE_DRIVER_NAME),
                QCMSettings(label="b", driver=FAKE_DRIVER_NAME),
            ]
        )
    )

    assert worker.start_monitoring("a") == ["a"]
    task_a = worker._poll_tasks["a"]
    assert worker.monitoring["a"] is True
    assert worker.monitoring["b"] is False

    # Already monitoring: not returned again.
    assert worker.start_monitoring("a") == []

    with pytest.raises(KeyError):
        worker.start_monitoring("unknown")

    # Start all: only the not-yet-monitoring labels are affected.
    assert worker.start_monitoring() == ["b"]
    task_b = worker._poll_tasks["b"]

    assert worker.stop_monitoring() == ["a", "b"]
    assert worker._poll_tasks == {}
    assert worker.monitoring == {"a": False, "b": False}
    await asyncio.gather(task_a, task_b, return_exceptions=True)
    await worker.close_devices()


@pytest.mark.asyncio
async def test_poll_loop_publishes_reading(tmp_path, monkeypatch) -> None:
    """One successful poll iteration writes the expected stream entry.

    Approach: monkeypatch ``_sleep`` to raise `asyncio.CancelledError` on the
    first call, so exactly one connect+read+publish cycle runs before the loop
    is stopped.
    """
    worker = _make_worker(tmp_path)
    await worker._build_devices(QCMServiceSettings(devices=[QCMSettings(label="a", driver=FAKE_DRIVER_NAME)]))
    _device(worker, "a").parameters = FTMParameters(
        connected=True,
        frequency=5_000_000.0,
        frequency_std=1.5,
        averaging_window=10,
        averaging_progress=10,
        rate=1.25,
        thickness=1000.0,
        thickness_std=0.5,
        target=5000.0,
        material_density=7.2,
        material_z_ratio=0.31,
        scale=1.0,
        running=True,
    )

    class FakeClient:
        def __init__(self):
            self.calls = []

        async def xadd(self, key, values, options=None):
            self.calls.append((key, values, options))

    fake = FakeClient()
    monkeypatch.setattr(worker, "_client", fake)

    async def cancel_after_one(_delay):
        raise asyncio.CancelledError

    monkeypatch.setattr(worker, "_sleep", cancel_after_one)

    with pytest.raises(asyncio.CancelledError):
        await worker._poll_device("a")

    assert len(fake.calls) == 1
    key, values, options = fake.calls[0]
    assert key == f"scietex:{worker.service_name}:{worker.instance_id}:qcm:a:readings"
    fields = dict(values)
    assert list(fields) == [
        b"ts",
        b"frequency",
        b"frequency_std",
        b"rate",
        b"thickness",
        b"thickness_std",
        b"target",
        b"material_density",
        b"material_z_ratio",
        b"scale",
        b"running",
    ]
    assert fields[b"frequency"] == b"5000000.0"
    assert fields[b"thickness"] == b"1000.0"
    assert fields[b"material_density"] == b"7.2"
    assert fields[b"material_z_ratio"] == b"0.31"
    assert fields[b"running"] == b"1"
    assert options.trim.threshold == STREAM_MAXLEN
    assert options.trim.exact is False

    assert worker.stats["a"].reading_count == 1
    assert worker.stats["a"].last_parameters is not None
    assert worker.stats["a"].last_error is None
    await worker.close_devices()


@pytest.mark.asyncio
async def test_poll_loop_encodes_missing_values_as_empty(tmp_path, monkeypatch) -> None:
    """A device that does not report a field encodes an empty value, not zero."""
    worker = _make_worker(tmp_path)
    await worker._build_devices(QCMServiceSettings(devices=[QCMSettings(label="a", driver=FAKE_DRIVER_NAME)]))
    _device(worker, "a").parameters = FTMParameters(frequency=5_000_000.0, running=False)

    class FakeClient:
        def __init__(self):
            self.calls = []

        async def xadd(self, key, values, options=None):
            self.calls.append((key, values, options))

    fake = FakeClient()
    monkeypatch.setattr(worker, "_client", fake)

    async def cancel_after_one(_delay):
        raise asyncio.CancelledError

    monkeypatch.setattr(worker, "_sleep", cancel_after_one)

    with pytest.raises(asyncio.CancelledError):
        await worker._poll_device("a")

    fields = dict(fake.calls[0][1])
    assert fields[b"thickness"] == b""
    assert fields[b"rate"] == b""
    assert fields[b"running"] == b"0"
    await worker.close_devices()


@pytest.mark.asyncio
async def test_poll_loop_uses_configured_stream_maxlen(tmp_path, monkeypatch) -> None:
    """The readings stream retention comes from settings, not the module default."""
    worker = _make_worker(tmp_path)
    await worker._build_devices(
        QCMServiceSettings(
            devices=[QCMSettings(label="a", driver=FAKE_DRIVER_NAME)],
            stream_maxlen=250,
        )
    )

    class FakeClient:
        def __init__(self):
            self.calls = []

        async def xadd(self, key, values, options=None):
            self.calls.append((key, values, options))

    fake = FakeClient()
    monkeypatch.setattr(worker, "_client", fake)

    async def cancel_after_one(_delay):
        raise asyncio.CancelledError

    monkeypatch.setattr(worker, "_sleep", cancel_after_one)

    with pytest.raises(asyncio.CancelledError):
        await worker._poll_device("a")

    assert fake.calls[0][2].trim.threshold == 250
    await worker.close_devices()


@pytest.mark.asyncio
async def test_poll_loop_survives_read_error(tmp_path, monkeypatch) -> None:
    """A read_parameters() exception increments error_count without killing the loop.

    Approach: monkeypatch ``_sleep`` to raise `asyncio.CancelledError` on the
    second call, so two read attempts run (each failing) before the loop is
    stopped — proving it survived the first failure.
    """
    worker = _make_worker(tmp_path)
    await worker._build_devices(QCMServiceSettings(devices=[QCMSettings(label="a", driver=FAKE_DRIVER_NAME)]))
    _device(worker, "a").read_exc = RuntimeError("boom")

    calls = {"n": 0}

    async def cancel_after_two(_delay):
        calls["n"] += 1
        if calls["n"] >= 2:
            raise asyncio.CancelledError

    monkeypatch.setattr(worker, "_sleep", cancel_after_two)

    with pytest.raises(asyncio.CancelledError):
        await worker._poll_device("a")

    assert worker.stats["a"].error_count == 2
    assert worker.stats["a"].last_error is not None
    assert worker.stats["a"].reading_count == 0
    await worker.close_devices()


@pytest.mark.asyncio
async def test_cleanup_cancels_tasks_and_closes_devices(tmp_path) -> None:
    """cleanup() cancels poll tasks and closes every device."""
    worker = _make_worker(tmp_path)
    await worker._build_devices(QCMServiceSettings(devices=[QCMSettings(label="a", driver=FAKE_DRIVER_NAME)]))
    device = _device(worker, "a")
    worker.start_monitoring("a")
    task = worker._poll_tasks["a"]
    assert worker.monitoring["a"] is True

    await worker.cleanup()

    assert worker._poll_tasks == {}
    assert worker.monitoring["a"] is False
    assert device.close_calls == 1
    await asyncio.gather(task, return_exceptions=True)


def test_build_telemetry_unconfigured(tmp_path) -> None:
    """An unconfigured worker reports configured=False with no devices and no gateway."""
    worker = _make_worker(tmp_path)

    telemetry = build_telemetry(worker)

    assert telemetry.service == "test"
    assert telemetry.instance_id == worker.instance_id
    assert telemetry.configured is False
    assert telemetry.gateway_host is None
    assert telemetry.gateway_port is None
    assert telemetry.devices == []


@pytest.mark.asyncio
async def test_build_telemetry_configured_sorted(tmp_path) -> None:
    """A configured worker reports devices sorted by label and the gateway endpoint."""
    worker = _make_worker(tmp_path)
    settings = QCMServiceSettings(
        gateway=ModbusGatewaySettings(host="gw", port=5020),
        devices=[
            QCMSettings(label="z", driver=FAKE_DRIVER_NAME),
            QCMSettings(label="a", driver=FAKE_DRIVER_NAME),
        ],
    )
    await worker._build_devices(settings)

    telemetry = build_telemetry(worker)

    assert telemetry.configured is True
    assert telemetry.gateway_host == "gw"
    assert telemetry.gateway_port == 5020
    assert [d.label for d in telemetry.devices] == ["a", "z"]
    await worker.close_devices()


def test_restart_required_transitions(tmp_path) -> None:
    """restart_required is False at startup and True after a post-startup apply."""
    worker = _make_worker(tmp_path)
    assert worker.restart_required is False

    # Pre-startup apply only stores; it does not flag a restart.
    worker._apply_qcm_settings(QCMServiceSettings())
    assert worker.restart_required is False

    worker._startup_complete = True
    worker._apply_qcm_settings(QCMServiceSettings())
    assert worker.restart_required is True


def test_handlers_registered_on_declared_lanes(tmp_path) -> None:
    """Both handlers register at construction, on their declared lanes.

    ``_handler_registry`` returns ``(registration_map, active_map)``; the first
    element is the registered-but-not-started map. ``MonitoringHandler`` declares
    ``control = True``, so it shares the control registry with the built-in
    ``CancelTaskHandler``; ``QCMControlHandler`` declares ``control = False``
    and lands in the data registry.
    """
    worker = _make_worker(tmp_path)

    monitoring_map, monitoring_active = worker._handler_registry("MonitoringHandler")
    assert "MonitoringHandler" in monitoring_map
    assert not monitoring_active

    qcm_map, qcm_active = worker._handler_registry("QCMControlHandler")
    assert "QCMControlHandler" in qcm_map
    assert not qcm_active

    control_map, _ = worker._handler_registry("CancelTaskHandler")
    assert monitoring_map is control_map
    assert qcm_map is not control_map


@pytest.mark.asyncio
async def test_poll_loop_clears_connected_after_failure_threshold(tmp_path, monkeypatch) -> None:
    """A device that connects but cannot be read is reported as disconnected.

    ``connect()`` only opens the TCP socket to the gateway, so it succeeds for
    a unit id that no device answers. ``connected`` therefore tracks whether
    the device answers, not the socket: setting it on connect would report such
    a device as connected for the whole poll interval, between the successful
    connect and the failing read.
    """
    worker = _make_worker(tmp_path)
    await worker._build_devices(QCMServiceSettings(devices=[QCMSettings(label="a", driver=FAKE_DRIVER_NAME)]))
    device = _device(worker, "a")

    # One good read puts the device in the connected state, then every read
    # fails. Record the flag as the loop sees it after each failed read, so the
    # threshold boundary is asserted rather than only the settled end state.
    observed: list[bool] = []
    reads = {"n": 0}

    async def read_parameters():
        reads["n"] += 1
        if reads["n"] == 1:
            return FTMParameters(frequency=5_000_000.0)
        raise RuntimeError("Read of register 0 failed: exception code 11")

    device.read_parameters = read_parameters

    async def record_and_cancel(_delay):
        # The loop sleeps after a success too; only failed reads are of interest.
        if reads["n"] > 1:
            observed.append(worker.connected["a"])
        if len(observed) > CONNECTED_FAILURE_THRESHOLD:
            raise asyncio.CancelledError

    monkeypatch.setattr(worker, "_sleep", record_and_cancel)

    with pytest.raises(asyncio.CancelledError):
        await worker._poll_device("a")

    # Below the threshold the device stays connected; at the threshold it clears.
    assert observed[: CONNECTED_FAILURE_THRESHOLD - 1] == [True] * (CONNECTED_FAILURE_THRESHOLD - 1)
    assert observed[CONNECTED_FAILURE_THRESHOLD - 1] is False
    # One extra failed read runs before the cancel takes effect.
    assert worker.stats["a"].error_count == CONNECTED_FAILURE_THRESHOLD + 1
    assert worker.stats["a"].reading_count == 1
    # An exception response means the gateway answered, so the socket is still
    # open and the loop does not reconnect.
    assert device.connect_calls == 1
    await worker.close_devices()


@pytest.mark.asyncio
async def test_poll_loop_reconnects_after_transport_failure(tmp_path, monkeypatch) -> None:
    """A read that fails because the socket died makes the loop reconnect.

    pymodbus closes the transport when a request times out, so the socket is
    unusable afterwards and every later read fails the same way. Without
    clearing the transport flag the loop retried a dead socket forever and only
    a service restart recovered it.
    """
    worker = _make_worker(tmp_path)
    await worker._build_devices(QCMServiceSettings(devices=[QCMSettings(label="a", driver=FAKE_DRIVER_NAME)]))
    device = _device(worker, "a")

    reads = {"n": 0}

    async def read_parameters():
        reads["n"] += 1
        if reads["n"] == 1:
            raise ConnectionException("Not connected")
        return FTMParameters(frequency=5_000_000.0)

    device.read_parameters = read_parameters

    sleeps = {"n": 0}

    async def cancel_after_bound(_delay):
        sleeps["n"] += 1
        if worker.stats["a"].reading_count or sleeps["n"] > 5:
            raise asyncio.CancelledError

    monkeypatch.setattr(worker, "_sleep", cancel_after_bound)

    with pytest.raises(asyncio.CancelledError):
        await worker._poll_device("a")

    # The failed read cleared the flag, so the next iteration reconnected.
    assert device.connect_calls == 2
    assert worker.stats["a"].reading_count == 1
    assert worker.connected["a"] is True
    await worker.close_devices()


@pytest.mark.asyncio
async def test_poll_loop_keeps_transport_on_exception_response(tmp_path, monkeypatch) -> None:
    """A Modbus exception response does not trigger a reconnect.

    The gateway answered, so the socket is healthy; reconnecting on every
    exception response would tear down a working connection.
    """
    worker = _make_worker(tmp_path)
    await worker._build_devices(QCMServiceSettings(devices=[QCMSettings(label="a", driver=FAKE_DRIVER_NAME)]))
    device = _device(worker, "a")
    device.read_exc = ModbusOperationError("Read of register 0 failed: exception code 11")

    sleeps = {"n": 0}

    async def cancel_after_bound(_delay):
        sleeps["n"] += 1
        if sleeps["n"] > 3:
            raise asyncio.CancelledError

    monkeypatch.setattr(worker, "_sleep", cancel_after_bound)

    with pytest.raises(asyncio.CancelledError):
        await worker._poll_device("a")

    assert device.connect_calls == 1
    assert worker.stats["a"].error_count == 4
    await worker.close_devices()


@pytest.mark.asyncio
async def test_poll_loop_keeps_connected_below_failure_threshold(tmp_path, monkeypatch) -> None:
    """A single failed read does not flip the device to disconnected.

    An RS485 segment drops the occasional frame, so one failure must not make
    the status icon flicker. The device stays connected until the failures are
    consecutive and reach the threshold.
    """
    worker = _make_worker(tmp_path)
    await worker._build_devices(QCMServiceSettings(devices=[QCMSettings(label="a", driver=FAKE_DRIVER_NAME)]))
    device = _device(worker, "a")

    calls = {"n": 0}

    async def read_parameters():
        calls["n"] += 1
        # One good read, then a single failure, then good reads again.
        if calls["n"] == 2:
            raise RuntimeError("transient bus error")
        return FTMParameters(frequency=5_000_000.0)

    device.read_parameters = read_parameters

    sleeps = {"n": 0}

    async def cancel_after_bound(_delay):
        sleeps["n"] += 1
        if sleeps["n"] > 3:
            raise asyncio.CancelledError

    monkeypatch.setattr(worker, "_sleep", cancel_after_bound)

    with pytest.raises(asyncio.CancelledError):
        await worker._poll_device("a")

    assert worker.connected["a"] is True
    await worker.close_devices()
