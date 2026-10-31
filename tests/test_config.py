"""Tests for the QCM service configuration models and loader."""

from pathlib import Path

import msgspec
import pytest

from scietex.qcm_service.config import (
    QCM_CONFIG_FILE,
    QCM_CONFIG_SUBDIR,
    QCM_SETTINGS_DEFAULTS,
    ModbusGatewaySettings,
    QCMServiceSettings,
    QCMSettings,
    read_qcm_config,
)


def _config_path(conf_dir: Path) -> Path:
    """The namespaced path the loader reads and writes."""
    return conf_dir / QCM_CONFIG_SUBDIR / QCM_CONFIG_FILE


def test_yaml_roundtrip_preserves_device_list() -> None:
    """Device entries survive a YAML round-trip preserving every field."""
    settings = QCMServiceSettings(
        devices=[
            QCMSettings(
                label="qcm-1",
                driver="cyky_tm106b_modbus",
                unit_id=3,
                poll_interval=0.5,
            )
        ]
    )

    decoded = msgspec.yaml.decode(msgspec.yaml.encode(settings), type=QCMServiceSettings)

    assert decoded.devices[0].label == "qcm-1"
    assert decoded.devices[0].driver == "cyky_tm106b_modbus"
    assert decoded.devices[0].unit_id == 3
    assert decoded.devices[0].poll_interval == 0.5


def test_defaults_constant_is_l0_base() -> None:
    """The exported L0 base is a concrete default struct instance."""
    assert isinstance(QCM_SETTINGS_DEFAULTS, QCMServiceSettings)
    assert QCM_SETTINGS_DEFAULTS == QCMServiceSettings()


def test_gateway_defaults() -> None:
    """ModbusGatewaySettings defaults to the local gateway endpoint."""
    gateway = ModbusGatewaySettings()
    assert gateway.host == "127.0.0.1"
    assert gateway.port == 502
    assert gateway.timeout == 1.0


def test_device_defaults() -> None:
    """A device entry defaults to unit id 1 and a 1 s poll interval."""
    device = QCMSettings(label="qcm-1", driver="cyky_tm106b_modbus")
    assert device.unit_id == 1
    assert device.poll_interval == 1.0


def test_service_defaults() -> None:
    """The service defaults to no devices, a 3-failure threshold and 10000 entries."""
    settings = QCMServiceSettings()
    assert settings.devices == []
    assert settings.failure_threshold == 3
    assert settings.stream_maxlen == 10000


def test_unknown_field_rejected() -> None:
    """``forbid_unknown_fields`` rejects a typo instead of silently ignoring it."""
    with pytest.raises(msgspec.ValidationError):
        msgspec.convert({"devices": [{"label": "a", "driver": "d", "bogus": 1}]}, QCMServiceSettings)


def test_loader_creates_default_file(tmp_path: Path) -> None:
    """A missing file is created with the default settings and returned as a patch."""
    patch = read_qcm_config(tmp_path)

    assert _config_path(tmp_path).exists()
    assert patch == msgspec.to_builtins(QCMServiceSettings())


def test_loader_reads_existing_file(tmp_path: Path) -> None:
    """An existing file is parsed and returned as a raw mapping."""
    path = _config_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text("devices:\n  - label: qcm-1\n    driver: cyky_tm106b_modbus\n")

    patch = read_qcm_config(tmp_path)

    assert patch["devices"] == [{"label": "qcm-1", "driver": "cyky_tm106b_modbus"}]


def test_loader_missing_file_without_create_raises(tmp_path: Path) -> None:
    """``create_default=False`` on a missing file raises instead of writing one."""
    with pytest.raises(RuntimeError, match="does not exist"):
        read_qcm_config(tmp_path, create_default=False)


def test_loader_unparseable_file_raises(tmp_path: Path) -> None:
    """An unparseable file raises RuntimeError and is left untouched."""
    path = _config_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text("unterminated: [flow\n")

    with pytest.raises(RuntimeError, match="Failed to parse"):
        read_qcm_config(tmp_path)

    assert path.read_text() == "unterminated: [flow\n"


def test_loader_none_conf_dir_raises() -> None:
    """A ``None`` config dir raises rather than resolving to the cwd."""
    with pytest.raises(RuntimeError, match="was not set"):
        read_qcm_config(None)


def test_loader_non_directory_raises(tmp_path: Path) -> None:
    """A service subdir that is a file, not a directory, raises."""
    conf_dir = tmp_path / "conf"
    conf_dir.mkdir()
    (conf_dir / QCM_CONFIG_SUBDIR).write_text("not a directory")

    with pytest.raises(RuntimeError, match="not a directory"):
        read_qcm_config(conf_dir)
