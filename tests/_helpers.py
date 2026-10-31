"""Shared helpers for the QCM service handler tests.

This is not a test module: the filename does not match pytest's default
collection patterns (``test_*.py`` / ``*_test.py``), so pytest will not collect
it.
"""

import logging
from importlib.metadata import EntryPoint
from uuid import uuid4

import msgspec
from scietex.hal.qcm.base.data import (
    FTMParameters,
    GatedFTMCapabilities,
    Material,
    OutCTRLMode,
    PwmCTRLMode,
    QCMError,
)
from scietex.hal.qcm.base.gated_ftm import GatedFTM
from scietex.service.task_handler import TaskCapabilities, TaskData, TaskHandlerContext

#: Entry point name the shared fake driver is advertised under in tests.
FAKE_DRIVER_NAME = "test_helpers_fake_qcm"


def register_fake_driver(monkeypatch) -> None:
    """Advertise `_FakeQCM` as the only driver entry point.

    The worker resolves drivers from installed distribution metadata, which a
    test module cannot contribute to. Patching ``entry_points`` keeps the
    resolution path under test without installing a throwaway distribution.
    """
    from scietex.qcm_service import qcm_worker as worker_module

    entry_point = EntryPoint(
        name=FAKE_DRIVER_NAME,
        value=f"{__name__}:_FakeQCM",
        group=worker_module.DRIVER_ENTRY_POINT_GROUP,
    )
    monkeypatch.setattr(worker_module, "entry_points", lambda group: [entry_point])


def make_context() -> TaskHandlerContext:
    """A narrow handler context with fixed service identity and a test logger."""
    return TaskHandlerContext(service_name="test", instance_id="inst", logger=logging.getLogger("test"))


def make_capabilities() -> TaskCapabilities:
    """Per-call capabilities with a no-op progress writer (mirrors modbus tests)."""

    async def _noop(_task_id, _value) -> None:
        return None

    return TaskCapabilities(task_id=uuid4(), _write_progress=_noop)


def make_task(task_name: str, payload: dict | None) -> TaskData:
    """A ``TaskData`` whose payload is JSON-encoded from ``payload`` (``None`` -> empty)."""
    return TaskData(
        task_id=str(uuid4()),
        task=task_name,
        payload=msgspec.json.encode(payload) if payload is not None else b"",
    )


class _FakeQCM(GatedFTM):
    """A `GatedFTM` fake recording every control call.

    ``capabilities`` defaults to the empty tree (nothing supported) and may be
    replaced per instance. Each control method records its call; ``read_exc``
    makes :meth:`read_parameters` raise, ``control_exc`` makes every control
    method raise.
    """

    capabilities = GatedFTMCapabilities()
    display_name = "Fake QCM"

    def __init__(self, host, port=502, unit_id=1, label="QCM", timeout=1.0, logger=None, **kwargs):
        self.label = label
        self.host = host
        self.port = port
        self.unit_id = unit_id
        self.timeout = timeout
        self.logger = logger
        self.extra_kwargs = kwargs
        self.connect_calls = 0
        self.close_calls = 0
        self.read_calls = 0
        self.parameters = FTMParameters(
            connected=True,
            frequency=5_000_000.0,
            frequency_std=1.0,
            averaging_window=10,
            averaging_progress=10,
            rate=1.5,
            thickness=1000.0,
            thickness_std=0.5,
            target=0.0,
            material_density=7.2,
            material_z_ratio=0.31,
            running=True,
            scale=1.0,
        )
        self.error_code = 0
        self.read_exc = None
        self.control_exc = None
        self.start_calls: list[bool] = []
        self.stop_calls = 0
        self.reset_thickness_calls = 0
        self.set_pwm_value_calls: list[float] = []
        self.set_pwm_mode_calls: list[PwmCTRLMode] = []
        self.set_gate_time_calls: list[float] = []
        self.set_material_calls: list[Material] = []
        self.set_z_ratio_calls: list[float] = []
        self.set_ftm_scale_calls: list[float] = []
        self.set_pwm_scale_calls: list[int] = []
        self.clear_error_calls: list[bool] = []
        self.pwm_mode = PwmCTRLMode.MANUAL
        self.gate_time_ms = 1000.0
        self.pwm_value = 0.02
        self.pwm_scale = 5000

    async def connect(self):
        self.connect_calls += 1

    async def close(self):
        self.close_calls += 1

    async def read_parameters(self):
        self.read_calls += 1
        if self.read_exc is not None:
            raise self.read_exc
        return self.parameters

    async def get_error_code(self, comm: bool = False):
        return self.error_code

    def parse_error_code(self, code: int, comm: bool = False) -> QCMError:
        return QCMError(code=code, message="No error" if code == 0 else "Fault")

    async def clear_error(self, comm: bool = False):
        self.clear_error_calls.append(comm)
        if self.control_exc is not None:
            raise self.control_exc
        self.error_code = 0
        return 0

    async def start_measurement(self, reset: bool = True):
        self.start_calls.append(reset)
        if self.control_exc is not None:
            raise self.control_exc
        return True

    async def stop_measurement(self):
        self.stop_calls += 1
        if self.control_exc is not None:
            raise self.control_exc
        return False

    async def reset_thickness(self):
        self.reset_thickness_calls += 1
        if self.control_exc is not None:
            raise self.control_exc

    async def set_ctrl_pwm_value(self, value: float):
        self.set_pwm_value_calls.append(value)
        if self.control_exc is not None:
            raise self.control_exc
        self.pwm_value = value
        return value

    async def get_ctrl_pwm_mode(self):
        return self.pwm_mode

    async def set_ctrl_pwm_mode(self, mode: PwmCTRLMode):
        self.set_pwm_mode_calls.append(mode)
        if self.control_exc is not None:
            raise self.control_exc
        self.pwm_mode = mode
        return mode

    async def set_gate_time(self, gate_time_ms: float):
        self.set_gate_time_calls.append(gate_time_ms)
        if self.control_exc is not None:
            raise self.control_exc
        self.gate_time_ms = gate_time_ms
        return gate_time_ms

    async def set_material(self, material: Material):
        self.set_material_calls.append(material)
        if self.control_exc is not None:
            raise self.control_exc
        return material

    async def set_material_z_ratio(self, z_ratio: float):
        self.set_z_ratio_calls.append(z_ratio)
        if self.control_exc is not None:
            raise self.control_exc
        return z_ratio

    async def set_ftm_scale(self, scale: float):
        self.set_ftm_scale_calls.append(scale)
        if self.control_exc is not None:
            raise self.control_exc
        return scale

    async def set_ctrl_pwm_scale(self, scale: int):
        self.set_pwm_scale_calls.append(scale)
        if self.control_exc is not None:
            raise self.control_exc
        self.pwm_scale = scale
        return scale

    async def get_ctrl_out_mode(self):
        return OutCTRLMode.DISABLED

    async def set_ctrl_out_mode(self, mode):
        return await self.get_ctrl_out_mode()

    async def get_ctrl_out_value(self) -> bool:
        return False

    async def set_ctrl_out_value(self, value: bool) -> bool:
        return False

    # The remaining abstract surface is not exercised by the service: the
    # handler only calls the methods above. They return fixed values so the
    # fake stays instantiable.

    async def get_vendor(self) -> str:
        return "Fake"

    async def get_product_name(self) -> str:
        return "Fake QCM"

    async def get_serial_number(self) -> str:
        return "0000"

    async def get_version(self) -> str:
        return "1.00"

    async def get_mcu_frequency(self) -> int:
        return 48_000_000

    async def get_gate_time(self) -> float:
        return self.gate_time_ms

    async def get_averaging(self) -> int:
        return 10

    async def set_averaging(self, averaging_window: int) -> int:
        return averaging_window

    async def get_counter(self) -> int:
        return 0

    async def get_frequency_instant(self) -> float:
        return 5_000_000.0

    async def get_averaging_progress(self) -> int:
        return 10

    async def get_frequency(self) -> float:
        return 5_000_000.0

    async def get_frequency_std(self) -> float:
        return 1.0

    async def get_thickness(self) -> float:
        return 1000.0

    async def get_thickness_std(self) -> float:
        return 0.5

    async def get_rate(self) -> float:
        return 1.5

    async def get_material_density(self) -> float:
        return 7.2

    async def set_material_density(self, density: float) -> float:
        return density

    async def get_material_z_ratio(self) -> float:
        return 0.31

    async def get_material(self) -> Material:
        return Material(density=7.2, z_ratio=0.31)

    async def get_ftm_scale(self) -> float:
        return 1.0

    async def get_target_thickness(self) -> float:
        return 0.0

    async def set_target_thickness(self, target: float) -> float:
        return target

    async def get_ctrl_pwm_value(self) -> float:
        return self.pwm_value

    async def get_ctrl_pwm_scale(self) -> int:
        return self.pwm_scale

    async def get_running_state(self) -> bool:
        return True

    async def set_running_state(self, running: bool) -> bool:
        return running


#: A capability tree with every optional operation enabled.
FULL_CAPABILITIES = GatedFTMCapabilities(
    start=True,
    stop=True,
    thickness_reset=True,
    material=True,
    z_ratio=True,
    ftm_scale=True,
    pwm=True,
    pwm_scale=True,
    gate_time=True,
    fault_reset=True,
    target_thickness=True,
)
