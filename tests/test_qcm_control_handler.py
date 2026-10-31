"""Tests for the ``qcm:*`` data-plane task handler."""

import msgspec
import pytest
from _helpers import (
    FAKE_DRIVER_NAME,
    FULL_CAPABILITIES,
    _FakeQCM,
    make_capabilities,
    make_context,
    make_task,
    register_fake_driver,
)
from scietex.hal.qcm.base.data import FTMParameters, GatedFTMCapabilities, Material, PwmCTRLMode
from scietex.service import ValkeyWorkerConfig

from scietex.qcm_service.config import QCMServiceSettings, QCMSettings
from scietex.qcm_service.qcm_worker import QCMWorker
from scietex.qcm_service.task_handlers import (
    QCM_CLEAR_ERROR_TASK_NAME,
    QCM_READ_TASK_NAME,
    QCM_RESET_THICKNESS_TASK_NAME,
    QCM_SET_FTM_SCALE_TASK_NAME,
    QCM_SET_GATE_TIME_TASK_NAME,
    QCM_SET_MATERIAL_TASK_NAME,
    QCM_SET_PWM_SCALE_TASK_NAME,
    QCM_SET_PWM_TASK_NAME,
    QCM_SET_Z_RATIO_TASK_NAME,
    QCM_START_TASK_NAME,
    QCM_STOP_TASK_NAME,
    QCMCommandResponse,
    QCMControlHandler,
)

_ALL_TASK_NAMES = [
    QCM_READ_TASK_NAME,
    QCM_START_TASK_NAME,
    QCM_STOP_TASK_NAME,
    QCM_RESET_THICKNESS_TASK_NAME,
    QCM_SET_PWM_TASK_NAME,
    QCM_SET_GATE_TIME_TASK_NAME,
    QCM_SET_MATERIAL_TASK_NAME,
    QCM_SET_Z_RATIO_TASK_NAME,
    QCM_SET_FTM_SCALE_TASK_NAME,
    QCM_SET_PWM_SCALE_TASK_NAME,
    QCM_CLEAR_ERROR_TASK_NAME,
]


def _make_worker(tmp_path) -> QCMWorker:
    """A worker with a real conf_dir and no remote-config handlers."""
    return QCMWorker(ValkeyWorkerConfig(service_name="test", conf_dir=str(tmp_path), remote_config_enabled=False))


async def _build(worker: QCMWorker, labels: list[str]) -> None:
    """Seed the registry with one fake device per label."""
    await worker._build_devices(
        QCMServiceSettings(devices=[QCMSettings(label=label, driver=FAKE_DRIVER_NAME) for label in labels])
    )


def _handler(worker: QCMWorker) -> QCMControlHandler:
    return QCMControlHandler("qcm", make_context(), worker=worker)


def _device(worker: QCMWorker, label: str = "a") -> _FakeQCM:
    return worker.devices[label]


@pytest.fixture(autouse=True)
def _fake_driver(monkeypatch):
    """Advertise the shared fake driver for every test in this module."""
    register_fake_driver(monkeypatch)


def test_handler_is_data_lane() -> None:
    """The QCM control handler is dispatched on the data lane."""
    assert QCMControlHandler.control is False


def test_supported_tasks_cover_every_task_name() -> None:
    """The handler declares exactly the eleven ``qcm:*`` task names."""
    handler = QCMControlHandler("qcm", make_context(), worker=None)  # type: ignore[arg-type]
    assert handler.supported_tasks == _ALL_TASK_NAMES


@pytest.mark.asyncio
async def test_read_returns_parameters_and_fault(tmp_path) -> None:
    """``qcm:read`` returns every parameter field plus the decoded fault."""
    worker = _make_worker(tmp_path)
    await _build(worker, ["a"])
    device = _device(worker)
    device.parameters = FTMParameters(
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
    device.error_code = 7

    result = await _handler(worker).handle(
        make_task(QCM_READ_TASK_NAME, {"label": "a"}),
        capabilities=make_capabilities(),
    )

    assert result.status == "success"
    decoded = msgspec.json.decode(result.payload, type=QCMCommandResponse)
    assert decoded.label == "a"
    assert decoded.task == QCM_READ_TASK_NAME
    assert decoded.frequency == 5_000_000.0
    assert decoded.frequency_std == 1.5
    assert decoded.averaging_window == 10
    assert decoded.averaging_progress == 10
    assert decoded.rate == 1.25
    assert decoded.thickness == 1000.0
    assert decoded.thickness_std == 0.5
    assert decoded.target == 5000.0
    assert decoded.material_density == 7.2
    assert decoded.material_z_ratio == 0.31
    assert decoded.scale == 1.0
    assert decoded.running is True
    assert decoded.error_code == 7
    assert decoded.error_message == "Fault"
    await worker.close_devices()


@pytest.mark.asyncio
async def test_read_works_without_any_capability(tmp_path) -> None:
    """Reading is always supported, even for a device declaring no capabilities."""
    worker = _make_worker(tmp_path)
    await _build(worker, ["a"])

    result = await _handler(worker).handle(
        make_task(QCM_READ_TASK_NAME, {"label": "a"}),
        capabilities=make_capabilities(),
    )

    assert result.status == "success"
    await worker.close_devices()


@pytest.mark.asyncio
async def test_start_passes_reset_flag(tmp_path) -> None:
    """``qcm:start`` forwards the reset flag to the driver."""
    worker = _make_worker(tmp_path)
    await _build(worker, ["a"])
    _device(worker).capabilities = FULL_CAPABILITIES

    result = await _handler(worker).handle(
        make_task(QCM_START_TASK_NAME, {"label": "a", "reset": False}),
        capabilities=make_capabilities(),
    )

    assert result.status == "success"
    assert _device(worker).start_calls == [False]
    await worker.close_devices()


@pytest.mark.asyncio
async def test_start_defaults_to_reset(tmp_path) -> None:
    """An omitted reset flag starts the measurement with a thickness reset."""
    worker = _make_worker(tmp_path)
    await _build(worker, ["a"])
    _device(worker).capabilities = FULL_CAPABILITIES

    await _handler(worker).handle(
        make_task(QCM_START_TASK_NAME, {"label": "a"}),
        capabilities=make_capabilities(),
    )

    assert _device(worker).start_calls == [True]
    await worker.close_devices()


@pytest.mark.asyncio
async def test_stop_reports_running_state(tmp_path) -> None:
    """``qcm:stop`` reports the running state read back from the driver."""
    worker = _make_worker(tmp_path)
    await _build(worker, ["a"])
    _device(worker).capabilities = FULL_CAPABILITIES

    result = await _handler(worker).handle(
        make_task(QCM_STOP_TASK_NAME, {"label": "a"}),
        capabilities=make_capabilities(),
    )

    assert result.status == "success"
    decoded = msgspec.json.decode(result.payload, type=QCMCommandResponse)
    assert decoded.running is False
    assert _device(worker).stop_calls == 1
    await worker.close_devices()


@pytest.mark.asyncio
async def test_reset_thickness_calls_driver(tmp_path) -> None:
    """``qcm:reset-thickness`` resets the accumulated thickness."""
    worker = _make_worker(tmp_path)
    await _build(worker, ["a"])
    _device(worker).capabilities = FULL_CAPABILITIES

    result = await _handler(worker).handle(
        make_task(QCM_RESET_THICKNESS_TASK_NAME, {"label": "a"}),
        capabilities=make_capabilities(),
    )

    assert result.status == "success"
    assert _device(worker).reset_thickness_calls == 1
    await worker.close_devices()


@pytest.mark.asyncio
async def test_set_pwm_writes_value_and_mode(tmp_path) -> None:
    """``qcm:set-pwm`` writes the value and, when given, the mode."""
    worker = _make_worker(tmp_path)
    await _build(worker, ["a"])
    device = _device(worker)
    device.capabilities = FULL_CAPABILITIES

    result = await _handler(worker).handle(
        make_task(QCM_SET_PWM_TASK_NAME, {"label": "a", "value": 42.5, "mode": "ABSOLUTE"}),
        capabilities=make_capabilities(),
    )

    assert result.status == "success"
    decoded = msgspec.json.decode(result.payload, type=QCMCommandResponse)
    assert decoded.pwm_value == 42.5
    assert decoded.pwm_mode == "ABSOLUTE"
    assert device.set_pwm_value_calls == [42.5]
    assert device.set_pwm_mode_calls == [PwmCTRLMode.ABSOLUTE]
    await worker.close_devices()


@pytest.mark.asyncio
async def test_set_pwm_without_mode_keeps_current_mode(tmp_path) -> None:
    """An omitted mode leaves the device's PWM mode untouched."""
    worker = _make_worker(tmp_path)
    await _build(worker, ["a"])
    device = _device(worker)
    device.capabilities = FULL_CAPABILITIES
    device.pwm_mode = PwmCTRLMode.MANUAL

    result = await _handler(worker).handle(
        make_task(QCM_SET_PWM_TASK_NAME, {"label": "a", "value": 10.0}),
        capabilities=make_capabilities(),
    )

    assert result.status == "success"
    decoded = msgspec.json.decode(result.payload, type=QCMCommandResponse)
    assert decoded.pwm_mode == "MANUAL"
    assert device.set_pwm_mode_calls == []
    await worker.close_devices()


@pytest.mark.asyncio
async def test_set_pwm_rejects_unknown_mode(tmp_path) -> None:
    """An unknown PWM mode name is rejected before the driver is called."""
    worker = _make_worker(tmp_path)
    await _build(worker, ["a"])
    device = _device(worker)
    device.capabilities = FULL_CAPABILITIES

    result = await _handler(worker).handle(
        make_task(QCM_SET_PWM_TASK_NAME, {"label": "a", "value": 10.0, "mode": "BOGUS"}),
        capabilities=make_capabilities(),
    )

    assert result.status == "error"
    assert result.error_code == "INVALID_PARAMETERS"
    assert result.retryable is False
    assert device.set_pwm_value_calls == []
    await worker.close_devices()


@pytest.mark.asyncio
async def test_set_gate_time_passes_ms(tmp_path) -> None:
    """``qcm:set-gate-time`` forwards the gate time in milliseconds."""
    worker = _make_worker(tmp_path)
    await _build(worker, ["a"])
    device = _device(worker)
    device.capabilities = FULL_CAPABILITIES

    result = await _handler(worker).handle(
        make_task(QCM_SET_GATE_TIME_TASK_NAME, {"label": "a", "gate_time_ms": 250.0}),
        capabilities=make_capabilities(),
    )

    assert result.status == "success"
    decoded = msgspec.json.decode(result.payload, type=QCMCommandResponse)
    assert decoded.gate_time_ms == 250.0
    assert device.set_gate_time_calls == [250.0]
    await worker.close_devices()


@pytest.mark.asyncio
async def test_set_material_writes_density_and_z_ratio(tmp_path) -> None:
    """``qcm:set-material`` writes both material fields in one call."""
    worker = _make_worker(tmp_path)
    await _build(worker, ["a"])
    device = _device(worker)
    device.capabilities = FULL_CAPABILITIES

    result = await _handler(worker).handle(
        make_task(QCM_SET_MATERIAL_TASK_NAME, {"label": "a", "density": 7.2, "z_ratio": 0.31}),
        capabilities=make_capabilities(),
    )

    assert result.status == "success"
    decoded = msgspec.json.decode(result.payload, type=QCMCommandResponse)
    assert decoded.material_density == 7.2
    assert decoded.material_z_ratio == 0.31
    assert device.set_material_calls == [Material(density=7.2, z_ratio=0.31)]
    await worker.close_devices()


@pytest.mark.asyncio
async def test_set_material_requires_z_ratio(tmp_path) -> None:
    """``qcm:set-material`` without a Z-ratio is rejected before dispatch."""
    worker = _make_worker(tmp_path)
    await _build(worker, ["a"])
    device = _device(worker)
    device.capabilities = FULL_CAPABILITIES

    result = await _handler(worker).handle(
        make_task(QCM_SET_MATERIAL_TASK_NAME, {"label": "a", "density": 7.2}),
        capabilities=make_capabilities(),
    )

    assert result.status == "error"
    assert result.error_code == "INVALID_PARAMETERS"
    assert device.set_material_calls == []
    await worker.close_devices()


@pytest.mark.asyncio
async def test_set_z_ratio_writes_only_z_ratio(tmp_path) -> None:
    """``qcm:set-z-ratio`` writes the Z-ratio without touching the density."""
    worker = _make_worker(tmp_path)
    await _build(worker, ["a"])
    device = _device(worker)
    device.capabilities = FULL_CAPABILITIES

    result = await _handler(worker).handle(
        make_task(QCM_SET_Z_RATIO_TASK_NAME, {"label": "a", "z_ratio": 0.5}),
        capabilities=make_capabilities(),
    )

    assert result.status == "success"
    decoded = msgspec.json.decode(result.payload, type=QCMCommandResponse)
    assert decoded.material_z_ratio == 0.5
    assert device.set_z_ratio_calls == [0.5]
    assert device.set_material_calls == []
    await worker.close_devices()


@pytest.mark.asyncio
async def test_set_ftm_scale_writes_scale(tmp_path) -> None:
    """``qcm:set-ftm-scale`` writes the tooling scale factor."""
    worker = _make_worker(tmp_path)
    await _build(worker, ["a"])
    device = _device(worker)
    device.capabilities = FULL_CAPABILITIES

    result = await _handler(worker).handle(
        make_task(QCM_SET_FTM_SCALE_TASK_NAME, {"label": "a", "scale": 1.05}),
        capabilities=make_capabilities(),
    )

    assert result.status == "success"
    decoded = msgspec.json.decode(result.payload, type=QCMCommandResponse)
    assert decoded.scale == 1.05
    assert device.set_ftm_scale_calls == [1.05]
    await worker.close_devices()


@pytest.mark.asyncio
async def test_set_pwm_scale_writes_scale(tmp_path) -> None:
    """``qcm:set-pwm-scale`` writes the PWM scale."""
    worker = _make_worker(tmp_path)
    await _build(worker, ["a"])
    device = _device(worker)
    device.capabilities = FULL_CAPABILITIES

    result = await _handler(worker).handle(
        make_task(QCM_SET_PWM_SCALE_TASK_NAME, {"label": "a", "pwm_scale": 4000}),
        capabilities=make_capabilities(),
    )

    assert result.status == "success"
    decoded = msgspec.json.decode(result.payload, type=QCMCommandResponse)
    assert decoded.pwm_scale == 4000
    assert device.set_pwm_scale_calls == [4000]
    await worker.close_devices()


@pytest.mark.asyncio
async def test_clear_error_returns_the_code_after_reset(tmp_path) -> None:
    """``qcm:clear-error`` resets the fault and reports the code read back."""
    worker = _make_worker(tmp_path)
    await _build(worker, ["a"])
    device = _device(worker)
    device.capabilities = FULL_CAPABILITIES
    device.error_code = 3

    result = await _handler(worker).handle(
        make_task(QCM_CLEAR_ERROR_TASK_NAME, {"label": "a"}),
        capabilities=make_capabilities(),
    )

    assert result.status == "success"
    decoded = msgspec.json.decode(result.payload, type=QCMCommandResponse)
    assert decoded.error_code == 0
    assert decoded.error_message == "No error"
    assert device.clear_error_calls == [False]
    await worker.close_devices()


@pytest.mark.asyncio
async def test_clear_error_forwards_comm_flag(tmp_path) -> None:
    """The ``comm`` flag selects the communication fault table."""
    worker = _make_worker(tmp_path)
    await _build(worker, ["a"])
    device = _device(worker)
    device.capabilities = FULL_CAPABILITIES

    await _handler(worker).handle(
        make_task(QCM_CLEAR_ERROR_TASK_NAME, {"label": "a", "comm": True}),
        capabilities=make_capabilities(),
    )

    assert device.clear_error_calls == [True]
    await worker.close_devices()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("task", "payload", "capability"),
    [
        (QCM_START_TASK_NAME, {"label": "a"}, "start"),
        (QCM_STOP_TASK_NAME, {"label": "a"}, "stop"),
        (QCM_RESET_THICKNESS_TASK_NAME, {"label": "a"}, "thickness_reset"),
        (QCM_SET_PWM_TASK_NAME, {"label": "a", "value": 10.0}, "pwm"),
        (QCM_SET_GATE_TIME_TASK_NAME, {"label": "a", "gate_time_ms": 100.0}, "gate_time"),
        (QCM_SET_MATERIAL_TASK_NAME, {"label": "a", "density": 7.2, "z_ratio": 0.31}, "material"),
        (QCM_SET_Z_RATIO_TASK_NAME, {"label": "a", "z_ratio": 0.31}, "z_ratio"),
        (QCM_SET_FTM_SCALE_TASK_NAME, {"label": "a", "scale": 1.0}, "ftm_scale"),
        (QCM_SET_PWM_SCALE_TASK_NAME, {"label": "a", "pwm_scale": 5000}, "pwm_scale"),
        (QCM_CLEAR_ERROR_TASK_NAME, {"label": "a"}, "fault_reset"),
    ],
)
async def test_unsupported_capability_is_rejected(tmp_path, task, payload, capability) -> None:
    """A device missing the task's capability rejects it without reaching the driver.

    The capability is cleared on an otherwise fully capable device, so the
    rejection is attributable to that one field rather than to the empty tree.
    """
    worker = _make_worker(tmp_path)
    await _build(worker, ["a"])
    device = _device(worker)
    device.capabilities = GatedFTMCapabilities(
        **{field: (field != capability) for field in GatedFTMCapabilities.__struct_fields__}
    )

    result = await _handler(worker).handle(make_task(task, payload), capabilities=make_capabilities())

    assert result.status == "error"
    assert result.error_code == "UNSUPPORTED"
    assert result.retryable is False
    assert device.start_calls == []
    assert device.stop_calls == 0
    assert device.reset_thickness_calls == 0
    assert device.set_pwm_value_calls == []
    assert device.set_gate_time_calls == []
    assert device.set_material_calls == []
    assert device.set_z_ratio_calls == []
    assert device.set_ftm_scale_calls == []
    assert device.set_pwm_scale_calls == []
    assert device.clear_error_calls == []
    await worker.close_devices()


@pytest.mark.asyncio
async def test_unknown_label_is_rejected(tmp_path) -> None:
    """An unknown label yields UNKNOWN_DEVICE and is not retried."""
    worker = _make_worker(tmp_path)
    await _build(worker, ["a"])

    result = await _handler(worker).handle(
        make_task(QCM_READ_TASK_NAME, {"label": "nope"}),
        capabilities=make_capabilities(),
    )

    assert result.status == "error"
    assert result.error_code == "UNKNOWN_DEVICE"
    assert result.retryable is False
    await worker.close_devices()


@pytest.mark.asyncio
async def test_invalid_payload_is_rejected(tmp_path) -> None:
    """A payload that is not a QCMCommandRequest is rejected before dispatch."""
    worker = _make_worker(tmp_path)
    await _build(worker, ["a"])

    result = await _handler(worker).handle(
        make_task(QCM_READ_TASK_NAME, {"bogus": 1}),
        capabilities=make_capabilities(),
    )

    assert result.status == "error"
    assert result.error_code == "INVALID_PAYLOAD"
    assert result.retryable is False
    await worker.close_devices()


@pytest.mark.asyncio
async def test_driver_failure_is_retryable(tmp_path) -> None:
    """A driver exception is reported as a retryable QCM_COMMAND_FAILED."""
    worker = _make_worker(tmp_path)
    await _build(worker, ["a"])
    device = _device(worker)
    device.capabilities = FULL_CAPABILITIES
    device.control_exc = RuntimeError("gateway timeout")

    result = await _handler(worker).handle(
        make_task(QCM_START_TASK_NAME, {"label": "a"}),
        capabilities=make_capabilities(),
    )

    assert result.status == "error"
    assert result.error_code == "QCM_COMMAND_FAILED"
    assert result.retryable is True
    assert "gateway timeout" in result.error
    await worker.close_devices()
