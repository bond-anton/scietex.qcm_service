"""Tests for the QCM telemetry payload structs."""

from datetime import timedelta

import pytest
from _helpers import FAKE_DRIVER_NAME, register_fake_driver
from scietex.hal.qcm.base.data import FTMParameters
from scietex.service import ValkeyWorkerConfig

from scietex.qcm_service.config import QCMServiceSettings, QCMSettings
from scietex.qcm_service.qcm_worker import QCMWorker
from scietex.qcm_service.telemetry import QCMServiceTelemetry, QCMTelemetry, build_telemetry


def test_service_telemetry_defaults() -> None:
    """QCMServiceTelemetry defaults schema_version to 1 and timestamp to a tz-aware UTC instant."""
    telemetry = QCMServiceTelemetry(
        service="test",
        instance_id="abc",
        configured=False,
        gateway_host=None,
        gateway_port=None,
        devices=[],
    )

    assert telemetry.schema_version == 1
    assert telemetry.timestamp.tzinfo is not None  # timezone-aware
    assert telemetry.timestamp.utcoffset() == timedelta(0)  # UTC


def test_device_telemetry_accepts_none_optionals() -> None:
    """QCMTelemetry accepts None for every optional reading field."""
    device = QCMTelemetry(
        label="qcm-1",
        driver="cyky_tm106b_modbus",
        connected=False,
        monitoring=False,
        frequency=None,
        frequency_std=None,
        averaging_window=None,
        averaging_progress=None,
        rate=None,
        thickness=None,
        thickness_std=None,
        target=None,
        material_density=None,
        material_z_ratio=None,
        scale=None,
        running=False,
        last_reading_at=None,
        reading_count=0,
        error_count=0,
        last_error=None,
    )

    assert device.frequency is None
    assert device.thickness is None
    assert device.material_z_ratio is None
    assert device.reading_count == 0


@pytest.fixture(autouse=True)
def _fake_driver(monkeypatch):
    """Advertise the shared fake driver for every test in this module."""
    register_fake_driver(monkeypatch)


def _make_worker(tmp_path) -> QCMWorker:
    return QCMWorker(ValkeyWorkerConfig(service_name="test", conf_dir=str(tmp_path), remote_config_enabled=False))


@pytest.mark.asyncio
async def test_telemetry_reports_last_reading(tmp_path) -> None:
    """A device with a reading reports every parameter field and the run state."""
    worker = _make_worker(tmp_path)
    await worker._build_devices(QCMServiceSettings(devices=[QCMSettings(label="a", driver=FAKE_DRIVER_NAME)]))
    worker.stats["a"].last_parameters = FTMParameters(
        connected=True,
        frequency=5_000_000.0,
        frequency_std=1.5,
        averaging_window=10,
        averaging_progress=7,
        rate=1.25,
        thickness=1000.0,
        thickness_std=0.5,
        target=5000.0,
        material_density=7.2,
        material_z_ratio=0.31,
        scale=1.0,
        running=True,
    )
    worker.stats["a"].reading_count = 3

    telemetry = build_telemetry(worker)

    device = telemetry.devices[0]
    assert device.frequency == 5_000_000.0
    assert device.frequency_std == 1.5
    assert device.averaging_window == 10
    assert device.averaging_progress == 7
    assert device.rate == 1.25
    assert device.thickness == 1000.0
    assert device.thickness_std == 0.5
    assert device.target == 5000.0
    assert device.material_density == 7.2
    assert device.material_z_ratio == 0.31
    assert device.scale == 1.0
    assert device.running is True
    assert device.reading_count == 3
    await worker.close_devices()


@pytest.mark.asyncio
async def test_telemetry_without_reading_reports_none(tmp_path) -> None:
    """A device that has never been read reports None for every reading field."""
    worker = _make_worker(tmp_path)
    await worker._build_devices(QCMServiceSettings(devices=[QCMSettings(label="a", driver=FAKE_DRIVER_NAME)]))

    telemetry = build_telemetry(worker)

    device = telemetry.devices[0]
    assert device.frequency is None
    assert device.thickness is None
    assert device.material_density is None
    assert device.running is False
    assert device.last_reading_at is None
    await worker.close_devices()
