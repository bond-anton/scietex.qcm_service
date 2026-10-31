"""``qcm:*`` data-plane tasks: read and control a single quartz crystal microbalance.

Each task targets one device by label. Every control operation is gated on the
device's declared capabilities before dispatch, so an unsupported command is
rejected as a structured error rather than silently ignored by the driver.
"""

from typing import TYPE_CHECKING, ClassVar, cast

import msgspec
from scietex.hal.qcm.base.data import Material, PwmCTRLMode, QCMError
from scietex.service.task_handler import (
    TaskCapabilities,
    TaskData,
    TaskHandler,
    TaskHandlerContext,
    TaskResult,
)

if TYPE_CHECKING:
    from ..qcm_worker import QCMWorker

#: Task name that reads the device's operating parameters and error code.
QCM_READ_TASK_NAME = "qcm:read"

#: Task name that starts the measurement.
QCM_START_TASK_NAME = "qcm:start"

#: Task name that stops the measurement.
QCM_STOP_TASK_NAME = "qcm:stop"

#: Task name that resets the accumulated film thickness.
QCM_RESET_THICKNESS_TASK_NAME = "qcm:reset-thickness"

#: Task name that writes the PWM control value (and optionally its mode).
QCM_SET_PWM_TASK_NAME = "qcm:set-pwm"

#: Task name that writes the gate time.
QCM_SET_GATE_TIME_TASK_NAME = "qcm:set-gate-time"

#: Task name that writes the material density and Z-ratio.
QCM_SET_MATERIAL_TASK_NAME = "qcm:set-material"

#: Task name that writes the material Z-ratio only.
QCM_SET_Z_RATIO_TASK_NAME = "qcm:set-z-ratio"

#: Task name that writes the FTM (tooling) scale factor.
QCM_SET_FTM_SCALE_TASK_NAME = "qcm:set-ftm-scale"

#: Task name that writes the PWM scale.
QCM_SET_PWM_SCALE_TASK_NAME = "qcm:set-pwm-scale"

#: Task name that clears the latched fault.
QCM_CLEAR_ERROR_TASK_NAME = "qcm:clear-error"

#: Maps a control task name to the capability field that gates it. A field that
#: is ``False`` rejects the task. ``qcm:read`` is absent — reading is always
#: supported.
_CAPABILITY_BY_TASK: dict[str, str] = {
    QCM_START_TASK_NAME: "start",
    QCM_STOP_TASK_NAME: "stop",
    QCM_RESET_THICKNESS_TASK_NAME: "thickness_reset",
    QCM_SET_PWM_TASK_NAME: "pwm",
    QCM_SET_GATE_TIME_TASK_NAME: "gate_time",
    QCM_SET_MATERIAL_TASK_NAME: "material",
    QCM_SET_Z_RATIO_TASK_NAME: "z_ratio",
    QCM_SET_FTM_SCALE_TASK_NAME: "ftm_scale",
    QCM_SET_PWM_SCALE_TASK_NAME: "pwm_scale",
    QCM_CLEAR_ERROR_TASK_NAME: "fault_reset",
}


class QCMCommandRequest(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    """Request payload for a ``qcm:*`` task.

    Args:
        label: Device label to target (required).
        reset: Reset the accumulated thickness when starting (``qcm:start``).
        value: PWM control value in percent (``qcm:set-pwm``).
        mode: PWM control mode name (``qcm:set-pwm``); one of ``ABSOLUTE``,
            ``RELATIVE``, ``MANUAL``, ``DISABLED``.
        gate_time_ms: Gate time in milliseconds (``qcm:set-gate-time``).
        density: Material density in g/cm^3 (``qcm:set-material``).
        z_ratio: Material Z-ratio (``qcm:set-material``, ``qcm:set-z-ratio``).
        scale: FTM (tooling) scale factor (``qcm:set-ftm-scale``).
        pwm_scale: PWM scale (``qcm:set-pwm-scale``).
        comm: Reset the communication-protocol fault (``qcm:clear-error``).
    """

    label: str
    reset: bool = True
    value: float | None = None
    mode: str | None = None
    gate_time_ms: float | None = None
    density: float | None = None
    z_ratio: float | None = None
    scale: float | None = None
    pwm_scale: int | None = None
    comm: bool = False


class QCMCommandResponse(msgspec.Struct, frozen=True):
    """Result payload of a ``qcm:*`` task.

    Args:
        label: Device label the command targeted.
        task: Task name that was executed.
        frequency: Sensor frequency in Hz (``qcm:read`` only).
        frequency_std: Sensor frequency standard deviation in Hz.
        averaging_window: Averaging window size in samples.
        averaging_progress: Averaging accumulation progress.
        rate: Deposition rate in A/s.
        thickness: Film thickness in A.
        thickness_std: Film thickness standard deviation in A.
        target: Target film thickness in A.
        material_density: Material density in g/cm^3.
        material_z_ratio: Material Z-ratio.
        scale: FTM (tooling) scale factor.
        running: Whether the measurement is running.
        gate_time_ms: Gate time in milliseconds.
        pwm_value: PWM control value in percent.
        pwm_mode: PWM control mode name.
        pwm_scale: PWM scale.
        error_code: Latched fault code (``qcm:read`` and ``qcm:clear-error``).
        error_message: Human-readable fault description.
    """

    label: str
    task: str
    frequency: float | None = None
    frequency_std: float | None = None
    averaging_window: int | None = None
    averaging_progress: int | None = None
    rate: float | None = None
    thickness: float | None = None
    thickness_std: float | None = None
    target: float | None = None
    material_density: float | None = None
    material_z_ratio: float | None = None
    scale: float | None = None
    running: bool | None = None
    gate_time_ms: float | None = None
    pwm_value: float | None = None
    pwm_mode: str | None = None
    pwm_scale: int | None = None
    error_code: int | None = None
    error_message: str | None = None


class QCMControlHandler(TaskHandler):
    """Handle ``qcm:*`` tasks on the data lane.

    The worker is injected as a constructor keyword argument (``worker=``) via
    ``add_task_handler`` — the handler context carries no worker reference.
    """

    control: ClassVar[bool] = False

    def __init__(self, name: str, context: TaskHandlerContext, *, worker: "QCMWorker") -> None:
        """Initialize the handler.

        Args:
            name: Human-readable name for this handler instance.
            context: Narrow context provided by the owning processor.
            worker: The QCM worker whose device registry is controlled.
        """
        super().__init__(name, context)
        self._worker: QCMWorker = worker

    @property
    def supported_tasks(self) -> list[str]:
        """Task types this handler serves."""
        return [
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

    async def handle(self, task_data: TaskData, *, capabilities: TaskCapabilities) -> TaskResult:
        """Execute one ``qcm:*`` command against the labelled device.

        Checks, in order: payload decode, device lookup, capability gate,
        parameter validation, then dispatch. A transport failure from the driver
        is transient and marked retryable; every other failure is terminal.
        """
        task = task_data.task
        try:
            request = msgspec.json.decode(task_data.payload, type=QCMCommandRequest)
        except msgspec.DecodeError as exc:
            return TaskResult(status="error", error=str(exc), error_code="INVALID_PAYLOAD", retryable=False)

        label = request.label
        device = self._worker.devices.get(label)
        if device is None:
            return TaskResult(
                status="error",
                error=f"Unknown device label {label!r}",
                error_code="UNKNOWN_DEVICE",
                retryable=False,
            )

        capability_path = _CAPABILITY_BY_TASK.get(task)
        if capability_path is not None and not _supports(device, capability_path):
            return TaskResult(
                status="error",
                error=f"Device {label!r} does not support {task}",
                error_code="UNSUPPORTED",
                retryable=False,
            )

        invalid = self._validate(task, request)
        if invalid is not None:
            return invalid

        try:
            response = await self._dispatch(task, label, request, device)
        except Exception as exc:
            return TaskResult(status="error", error=str(exc), error_code="QCM_COMMAND_FAILED", retryable=True)

        return TaskResult(status="success", payload=msgspec.json.encode(response))

    @staticmethod
    def _validate(task: str, request: QCMCommandRequest) -> TaskResult | None:
        """Reject a command whose required parameter is missing or malformed."""
        required: dict[str, tuple[str, str]] = {
            QCM_SET_PWM_TASK_NAME: ("value", "value"),
            QCM_SET_GATE_TIME_TASK_NAME: ("gate_time_ms", "gate_time_ms"),
            QCM_SET_MATERIAL_TASK_NAME: ("density", "density"),
            QCM_SET_Z_RATIO_TASK_NAME: ("z_ratio", "z_ratio"),
            QCM_SET_FTM_SCALE_TASK_NAME: ("scale", "scale"),
            QCM_SET_PWM_SCALE_TASK_NAME: ("pwm_scale", "pwm_scale"),
        }
        field = required.get(task)
        if field is not None and getattr(request, field[0]) is None:
            return QCMControlHandler._invalid_parameters(f"{task} requires {field[1]}")
        if task == QCM_SET_MATERIAL_TASK_NAME and request.z_ratio is None:
            return QCMControlHandler._invalid_parameters(f"{task} requires z_ratio")
        if task == QCM_SET_PWM_TASK_NAME and request.mode is not None:
            try:
                PwmCTRLMode[request.mode]
            except KeyError:
                return QCMControlHandler._invalid_parameters(
                    f"Unknown PWM mode {request.mode!r}; expected one of {', '.join(mode.name for mode in PwmCTRLMode)}"
                )
        return None

    @staticmethod
    async def _dispatch(task: str, label: str, request: QCMCommandRequest, device: object) -> QCMCommandResponse:
        """Run the command against the device and build its response."""
        from scietex.hal.qcm.base.gated_ftm import GatedFTM

        ftm = cast(GatedFTM, device)

        if task == QCM_READ_TASK_NAME:
            parameters = await ftm.read_parameters()
            code = await ftm.get_error_code()
            error = _parse_error(ftm, code)
            return QCMCommandResponse(
                label=label,
                task=task,
                frequency=parameters.frequency,
                frequency_std=parameters.frequency_std,
                averaging_window=parameters.averaging_window,
                averaging_progress=parameters.averaging_progress,
                rate=parameters.rate,
                thickness=parameters.thickness,
                thickness_std=parameters.thickness_std,
                target=parameters.target,
                material_density=parameters.material_density,
                material_z_ratio=parameters.material_z_ratio,
                scale=parameters.scale,
                running=parameters.running,
                error_code=code,
                error_message=error.message if error is not None else None,
            )
        if task == QCM_START_TASK_NAME:
            running = await ftm.start_measurement(reset=request.reset)
            return QCMCommandResponse(label=label, task=task, running=running)
        if task == QCM_STOP_TASK_NAME:
            running = await ftm.stop_measurement()
            return QCMCommandResponse(label=label, task=task, running=running)
        if task == QCM_RESET_THICKNESS_TASK_NAME:
            await ftm.reset_thickness()
            return QCMCommandResponse(label=label, task=task)
        if task == QCM_SET_PWM_TASK_NAME:
            # Validated above: the task is rejected when the value is absent.
            value = await ftm.set_ctrl_pwm_value(cast(float, request.value))
            mode = await ftm.get_ctrl_pwm_mode()
            if request.mode is not None:
                mode = await ftm.set_ctrl_pwm_mode(PwmCTRLMode[request.mode])
            return QCMCommandResponse(label=label, task=task, pwm_value=value, pwm_mode=mode.name)
        if task == QCM_SET_GATE_TIME_TASK_NAME:
            gate_time = await ftm.set_gate_time(cast(float, request.gate_time_ms))
            return QCMCommandResponse(label=label, task=task, gate_time_ms=gate_time)
        if task == QCM_SET_MATERIAL_TASK_NAME:
            material = await ftm.set_material(
                Material(density=cast(float, request.density), z_ratio=cast(float, request.z_ratio))
            )
            return QCMCommandResponse(
                label=label,
                task=task,
                material_density=material.density,
                material_z_ratio=material.z_ratio,
            )
        if task == QCM_SET_Z_RATIO_TASK_NAME:
            z_ratio = await ftm.set_material_z_ratio(cast(float, request.z_ratio))
            return QCMCommandResponse(label=label, task=task, material_z_ratio=z_ratio)
        if task == QCM_SET_FTM_SCALE_TASK_NAME:
            scale = await ftm.set_ftm_scale(cast(float, request.scale))
            return QCMCommandResponse(label=label, task=task, scale=scale)
        if task == QCM_SET_PWM_SCALE_TASK_NAME:
            pwm_scale = await ftm.set_ctrl_pwm_scale(cast(int, request.pwm_scale))
            return QCMCommandResponse(label=label, task=task, pwm_scale=pwm_scale)
        # QCM_CLEAR_ERROR_TASK_NAME (last enumerated task)
        code = await ftm.clear_error(comm=request.comm)
        error = _parse_error(ftm, code, comm=request.comm)
        return QCMCommandResponse(
            label=label,
            task=task,
            error_code=code,
            error_message=error.message if error is not None else None,
        )

    @staticmethod
    def _invalid_parameters(message: str) -> TaskResult:
        """Build the non-retryable result for a parameter-validation failure."""
        return TaskResult(status="error", error=message, error_code="INVALID_PARAMETERS", retryable=False)


def _parse_error(device: object, code: int, *, comm: bool = False) -> QCMError | None:
    """Decode a fault code through the driver's own table, when it has one.

    ``parse_error_code`` is not part of `GatedFTM` — it is a driver-level
    convenience — so the call is guarded with ``hasattr``. A driver without it
    yields ``None`` and the caller reports the bare code.

    Args:
        device: The device driver instance.
        code: The fault code to decode.
        comm: Look the code up in the communication fault table.

    Returns:
        The decoded `QCMError`, or ``None`` when the driver cannot decode codes.
    """
    parse = getattr(device, "parse_error_code", None)
    if not callable(parse):
        return None
    return cast(QCMError, parse(code, comm=comm))


def _supports(device: object, capability_path: str) -> bool:
    """Resolve a dotted capability path against a device's capability tree.

    Args:
        device: The device driver instance.
        capability_path: Dotted path such as ``"start"`` or ``"pwm_scale"``.

    Returns:
        bool: Whether the device declares that capability. A missing capability
        tree or a missing field counts as unsupported.
    """
    node: object = getattr(device, "capabilities", None)
    for part in capability_path.split("."):
        if node is None:
            return False
        node = getattr(node, part, None)
    return bool(node)
