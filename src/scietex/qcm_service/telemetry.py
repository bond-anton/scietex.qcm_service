"""Telemetry payloads the QCM worker publishes to Valkey.

The structs mirror the API read side field-for-field so the monitoring endpoint
can decode the payload unchanged, with one addition: this producer carries a
``configured`` flag the API struct omits (the API decoder tolerates unknown
fields, so it is ignored there). The key shape, push interval, and TTL are the
frozen contract: key ``scietex:{service}:{instance_id}:telemetry`` (the
``telemetry`` suffix never matches the heartbeat discovery glob
``scietex:*:*:status``), pushed every 2 s with a 6 s TTL.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING

import msgspec

if TYPE_CHECKING:
    from .qcm_worker import QCMWorker

#: Push cadence and key TTL: 2 s interval, 6 s TTL so a single missed beat does
#: not expire the key.
TELEMETRY_INTERVAL: float = 2.0
TELEMETRY_TTL: int = 6


class QCMTelemetry(msgspec.Struct, frozen=True):
    """Per-device monitoring status and last reading.

    The reading fields mirror `scietex.hal.qcm.base.data.FTMParameters`; the
    optional ones stay ``None`` when the device does not report them.
    """

    label: str
    driver: str
    connected: bool
    monitoring: bool
    frequency: float | None
    frequency_std: float | None
    averaging_window: int | None
    averaging_progress: int | None
    rate: float | None
    thickness: float | None
    thickness_std: float | None
    target: float | None
    material_density: float | None
    material_z_ratio: float | None
    scale: float | None
    running: bool
    last_reading_at: datetime | None
    reading_count: int
    error_count: int
    last_error: str | None


class QCMServiceTelemetry(msgspec.Struct, frozen=True):
    """Service telemetry snapshot pushed by the worker.

    Mirrors the API read struct field-for-field and adds ``configured`` (whether
    the device list is populated). ``forbid_unknown_fields`` is deliberately left
    off so the API side can decode this payload with its own (slightly smaller)
    struct.
    """

    service: str
    instance_id: str
    configured: bool
    gateway_host: str | None
    gateway_port: int | None
    devices: list[QCMTelemetry]
    timestamp: datetime = msgspec.field(default_factory=lambda: datetime.now(timezone.utc))
    schema_version: int = 1


def build_telemetry(worker: QCMWorker) -> QCMServiceTelemetry:
    """Snapshot the worker's device registry into a telemetry payload.

    An unconfigured worker (empty device list) reports ``configured=False`` with
    no devices. A configured worker reads the per-label stats and monitoring
    flags; the gateway host/port come from the effective settings (``None``
    before startup), and devices are sorted by label.
    """
    settings = worker.qcm_settings
    devices = [
        _device_telemetry(worker, label, device_settings.driver)
        for label, device_settings in sorted(worker.device_settings.items())
    ]
    return QCMServiceTelemetry(
        service=worker.service_name,
        instance_id=worker.instance_id,
        configured=bool(worker.device_settings),
        gateway_host=settings.gateway.host if settings is not None else None,
        gateway_port=settings.gateway.port if settings is not None else None,
        devices=devices,
    )


def _device_telemetry(worker: QCMWorker, label: str, driver: str) -> QCMTelemetry:
    """Build one device's telemetry entry from its stats and last reading."""
    stats = worker.stats[label]
    parameters = stats.last_parameters
    return QCMTelemetry(
        label=label,
        driver=driver,
        connected=worker.connected.get(label, False),
        monitoring=worker.monitoring.get(label, False),
        frequency=parameters.frequency if parameters is not None else None,
        frequency_std=parameters.frequency_std if parameters is not None else None,
        averaging_window=parameters.averaging_window if parameters is not None else None,
        averaging_progress=parameters.averaging_progress if parameters is not None else None,
        rate=parameters.rate if parameters is not None else None,
        thickness=parameters.thickness if parameters is not None else None,
        thickness_std=parameters.thickness_std if parameters is not None else None,
        target=parameters.target if parameters is not None else None,
        material_density=parameters.material_density if parameters is not None else None,
        material_z_ratio=parameters.material_z_ratio if parameters is not None else None,
        scale=parameters.scale if parameters is not None else None,
        running=parameters.running if parameters is not None else False,
        last_reading_at=stats.last_reading_at,
        reading_count=stats.reading_count,
        error_count=stats.error_count,
        last_error=stats.last_error,
    )
