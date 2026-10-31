"""Configuration models and YAML loader for the QCM service.

`QCMServiceSettings` is the concrete constructor (L0) base and the validation
target for the service's remote configuration. The device list is a field-level
map merged onto L0 and overlaid by the remote `qcm` section (L3).

This module is deliberately **not** registered as an L1 bootstrap provider. The
worker reads `qcm.yml` directly at startup (before `super().initialize()`) and
uses it only to seed the initial in-memory device list; the config section is
registered with `bootstrap=None`, so L1 never enters the layer stack and the
remote L3 config cleanly replaces it. Trade-off: `config:show` will not display
it as a layer.

The structs are kept deliberately thin: the device ``driver`` is an entry point
name resolved at runtime (e.g. ``cyky_tm106b_modbus``), so the YAML schema only
constrains types and unknown fields.
"""

from pathlib import Path

import msgspec

#: Remote-config section name the settings are registered under.
QCM_SECTION: str = "qcm"

#: Subdirectory under the shared config dir that namespaces this service's
#: files. The service-owned ``qcm.yml`` lives here, so services sharing one
#: config dir cannot collide.
QCM_CONFIG_SUBDIR: str = "qcm"

#: Filename of the service-owned YAML config file in the config subdirectory.
QCM_CONFIG_FILE: str = "qcm.yml"


class ModbusGatewaySettings(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    """Modbus/TCP gateway connection settings.

    The service drives QCMs through the Scietex Modbus gateway, so only the TCP
    endpoint and the transaction timeout are configurable here.
    """

    host: str = "127.0.0.1"
    port: int = 502
    timeout: float = 1.0


class QCMSettings(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    """Settings for a single quartz crystal microbalance driven by the service.

    ``label`` and ``driver`` are required. ``driver`` is the entry point name of
    a `scietex.hal.qcm` driver, resolved at runtime, e.g.
    ``cyky_tm106b_modbus``.
    """

    label: str
    driver: str
    unit_id: int = 1
    poll_interval: float = 1.0


class QCMServiceSettings(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    """Top-level QCM service settings.

    ``failure_threshold`` and ``stream_maxlen`` are service-wide rather than
    per-device: every device on the bus shares the same RS485 segment, so the
    noise tolerance is a property of the segment, and the readings stream is a
    retention concern that does not vary by device.
    """

    gateway: ModbusGatewaySettings = ModbusGatewaySettings()
    devices: list[QCMSettings] = msgspec.field(default_factory=list)
    #: Consecutive failed reads before a device is reported as disconnected.
    #: One failed read is common on an RS485 segment, so a single glitch must
    #: not flip the status icon.
    failure_threshold: int = 3
    #: Entries retained per device's readings stream.
    stream_maxlen: int = 10000


#: Concrete L0 base for the service's configuration merge. A field absent from
#: every layer, or explicitly cleared with ``null``, resolves to the value here.
QCM_SETTINGS_DEFAULTS: QCMServiceSettings = QCMServiceSettings()


def read_qcm_config(conf_dir: Path | None, *, create_default: bool = True) -> dict[str, object]:
    """Read the service config from ``qcm/qcm.yml`` under the config directory.

    The service's files are namespaced in a ``qcm/`` subdirectory so they do not
    collide with other services sharing the framework's single config dir. The
    file (and, when missing, its directory) is only created when
    ``create_default=True`` (the bootstrap path). A ``None`` or non-directory
    ``conf_dir``, a missing file/directory with ``create_default=False``, or an
    unparseable file each raise `RuntimeError`. An existing-but-invalid file is
    left untouched regardless of ``create_default``.

    The return value is the raw ``qcm.yml`` mapping (an L1-style patch). When
    the file is just created, the builtins of the default struct are returned so
    the patch is equivalent to an empty override.

    Args:
        conf_dir: Path to the configuration directory.
        create_default: Whether to create the directory and write a default
            ``qcm.yml`` when missing. Default ``True``.

    Returns:
        The parsed ``qcm.yml`` mapping as a dict, or the builtins of the default
        `QCMServiceSettings` when the file was just created.

    Raises:
        RuntimeError: If ``conf_dir`` is ``None`` or not a directory, the file
            is missing with ``create_default=False``, or the file cannot be
            parsed.
    """
    if not isinstance(conf_dir, Path):
        raise RuntimeError("Configuration dir was not set!")
    service_dir = conf_dir / QCM_CONFIG_SUBDIR
    if not service_dir.exists():
        if create_default:
            try:
                service_dir.mkdir(parents=True, exist_ok=True)
            except Exception as exc:
                raise RuntimeError(f"Failed to create configuration directory {service_dir}!") from exc
        else:
            raise RuntimeError(
                f"Configuration directory {service_dir} does not exist and create_default=False (no default generated)."
            )
    elif not service_dir.is_dir():
        raise RuntimeError(f"Provided configuration directory path {service_dir} is not a directory!")
    qcm_yml = service_dir.joinpath(QCM_CONFIG_FILE)
    if not qcm_yml.exists():
        if create_default:
            settings = QCMServiceSettings()
            with open(qcm_yml, "wb") as f:
                f.write(msgspec.yaml.encode(settings))
            return msgspec.to_builtins(settings)
        raise RuntimeError(
            f"QCM configuration file {qcm_yml} does not exist and create_default=False "
            "(pass create_default=True to generate defaults)."
        )
    try:
        with open(qcm_yml, "rb") as f:
            patch = msgspec.yaml.decode(f.read(), type=dict)
    except Exception as exc:
        raise RuntimeError(
            f"Failed to parse QCM configuration file {qcm_yml}. Fix the file or remove it to regenerate defaults."
        ) from exc
    return patch
