# scietex.qcm_service

Scietex microservice daemon for quartz crystal microbalance (QCM) monitoring and
control.

The service drives one or more QCMs through the Scietex Modbus gateway. Each
device is handled by a driver from
[`scietex.hal.qcm`](https://github.com/bond-anton/scietex.hal.qcm), selected by
entry point name in the service configuration. The service polls each device on
its own long-lived task, publishes readings to a per-device Valkey stream, and
exposes control tasks (`qcm:read`, `qcm:start`, `qcm:stop`, `qcm:set-pwm`, …) on
the data lane.

## Supported devices

| Driver entry point | Device | Transport |
| --- | --- | --- |
| `cyky_tm106b_modbus` | CYKY TM106B | Modbus/TCP through the gateway |

The gateway descriptor (`cyky_tm106b`) lives under the `scietex.modbus.drivers`
group and is registered with the gateway, not with this service. The `_modbus`
suffix distinguishes the HAL driver entry point from the gateway descriptor.

## Configuration

The service reads `qcm/qcm.yml` under the framework config directory
(`SCIETEX_CONFIG_DIR`, default `/config` in the container). The file is created
with defaults on first start.

```yaml
gateway:
  host: 127.0.0.1
  port: 15020
  timeout: 1.0
devices:
  - label: qcm-1
    driver: cyky_tm106b_modbus
    unit_id: 3
    poll_interval: 1.0
failure_threshold: 3
stream_maxlen: 10000
```

- `gateway` — Modbus/TCP endpoint of the Scietex Modbus gateway.
- `devices[].label` — unique label; the registry key and the stream key segment.
- `devices[].driver` — HAL driver entry point name (see the table above).
- `devices[].unit_id` — Modbus unit id the gateway routes to the device. This is
  the device's own configured RS485 address (register 14 on the TM106B).
- `devices[].poll_interval` — seconds between reads.
- `failure_threshold` — consecutive failed reads before a device is reported as
  disconnected. One dropped frame on an RS485 segment must not flip the status.
- `stream_maxlen` — entries retained per device's readings stream.

A configuration change at runtime is **restart-required**: the device registry
is not live-rebuilt.

## Tasks

Control lane:

- `monitoring:start` / `monitoring:stop` — payload `{"label": "qcm-1"}` or empty
  for every device.

Data lane:

- `qcm:read` — payload `{"label": "qcm-1"}`. Returns every parameter field plus
  the decoded fault (`error_code`, `error_message`).
- `qcm:start` — `{"label": "qcm-1", "reset": true}`.
- `qcm:stop` — `{"label": "qcm-1"}`.
- `qcm:reset-thickness` — `{"label": "qcm-1"}`.
- `qcm:set-pwm` — `{"label": "qcm-1", "value": 42.5, "mode": "ABSOLUTE"}`.
  `mode` is optional; one of `ABSOLUTE`, `RELATIVE`, `MANUAL`, `DISABLED`.
- `qcm:set-gate-time` — `{"label": "qcm-1", "gate_time_ms": 250.0}`.
- `qcm:set-material` — `{"label": "qcm-1", "density": 7.2, "z_ratio": 0.31}`.
- `qcm:set-z-ratio` — `{"label": "qcm-1", "z_ratio": 0.31}`.
- `qcm:set-ftm-scale` — `{"label": "qcm-1", "scale": 1.05}`.
- `qcm:set-pwm-scale` — `{"label": "qcm-1", "pwm_scale": 4000}`.
- `qcm:clear-error` — `{"label": "qcm-1", "comm": false}`.

Every control task is gated on the device's declared capabilities, so an
unsupported command is rejected as `UNSUPPORTED` before the driver is called.
`qcm:read` is ungated.

## Valkey keys

- Readings stream: `scietex:{service}:{instance_id}:qcm:{label}:readings`
  with fields `ts`, `frequency`, `frequency_std`, `rate`, `thickness`,
  `thickness_std`, `target`, `material_density`, `material_z_ratio`, `scale`,
  `running`. A field the device does not report is written as an empty value,
  which is distinguishable from a reported `0.0`.
- Telemetry: `scietex:{service}:{instance_id}:telemetry`, pushed every 2 s with
  a 6 s TTL.

## Running

```bash
start-qcm-service --conf-dir /etc/scietex
```

Flags: `--conf-dir` (auto-resolved when omitted), `--service-name` (default
`$SCIETEX_SERVICE_NAME` or `QCM`), `--logging-level` (default
`$SCIETEX_LOGGING_LEVEL` or `INFO`). It runs in the foreground until `SIGINT` or
`SIGTERM`.

## Development

```bash
uv venv --python 3.14 .venv
uv pip install -e ".[dev,test,lint]" --python .venv/bin/python
tox -e format,lint,type,test
```

## Container

```bash
./build_image.sh [--latest]
```

Builds and pushes `registry.buro-nts.ru/scietex-qcm-service:<version>` for
`linux/amd64` and `linux/arm64`. The image installs the released wheel from
PyPI, so publish the version first.

## Deployment prerequisites

The gateway speaks 8-N-1 on its serial port. The TM106B must be configured to
match before the gateway can talk to it:

- **CYKY TM106B** — register 15 (baudrate code) → `4` (19200), register 14
  (RS485 address) → the unit id used in `qcm.yml`. The device answers at most
  five registers per read, which the driver already accounts for.

See the HAL protocol docs for the full register map.
