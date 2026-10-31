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

## Deployment

The service runs as a rootless Podman quadlet under the user's systemd instance,
alongside the other Scietex services. It reaches the QCM over Modbus/TCP through
the Modbus gateway, so it needs no serial device and publishes no ports.

### 1. Register the device with the gateway

The gateway rejects unknown unit ids (`allow_unknown_devices: false`), so the
TM106B must be registered before the service can read it. Devices live in the
API database, not in `modbus.yml`:

```bash
curl -s -X POST http://localhost:8000/api/v1/equipment/modbus/devices \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"name": "qcm-1", "driver": "cyky_tm106b", "unit_id": 3}'
```

`driver` is the **gateway** descriptor (`cyky_tm106b`), not the HAL driver
(`cyky_tm106b_modbus`). The gateway picks up a new device only on restart:

```bash
systemctl --user restart modbus-service.service
```

### 2. Write the service config

`~/.config/scietex/qcm/qcm.yml` — the `gateway.host` is
`host.containers.internal` because the service runs inside a container and the
gateway listens on the host:

```yaml
gateway:
  host: host.containers.internal
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

`~/.config/scietex-qcm/valkey.yml` — a copy of the host `valkey.yml` with the
node host rewritten to `host.containers.internal`, so the container reaches the
host's Valkey instead of its own loopback:

```bash
mkdir -p ~/.config/scietex/qcm ~/.config/scietex-qcm
sed 's/localhost/host.containers.internal/' \
  ~/.config/scietex/valkey.yml > ~/.config/scietex-qcm/valkey.yml
```

### 3. Install the quadlet

`~/.config/containers/systemd/qcm.container`:

```ini
[Unit]
Description=QCM Service Container
Requires=valkey.service
After=valkey.service
Wants=modbus-service.service
After=modbus-service.service

[Container]
ContainerName=qcm
Image=registry.buro-nts.ru/scietex-qcm-service:0.1.0
UserNS=keep-id
Volume=%h/.config/scietex:/config:z
Volume=%h/.config/scietex-qcm/valkey.yml:/config/valkey.yml:Z
AutoUpdate=registry

[Install]
WantedBy=multi-user.target default.target
```

Two volume details matter:

- `UserNS=keep-id` maps the host user to the container's `appuser` (uid 1000),
  so the mounted config tree stays writable without root.
- The shared config dir is mounted `:z` (shared), not `:Z` (private).
  `modbus-service` mounts the same tree, and `:Z` assigns a per-container MCS
  category — each start would relabel the directory and lock the other
  container out. The single-file `valkey.yml` overlay is `:Z` because only this
  container reads it.

### 4. Start and verify

```bash
systemctl --user daemon-reload
systemctl --user start qcm.service
systemctl --user status qcm.service
journalctl --user -u qcm.service -f
```

A healthy start logs `QCM service started: 1 device(s) via gateway
host.containers.internal:15020`, all handlers, and `Connected to Valkey`.

Polling is **opt-in** — the service starts with `monitoring: False` and reads
nothing until a `monitoring:start` control task arrives. To start it by hand,
publish the task to the worker's directed control stream:

```bash
python - <<'PY'
import msgspec, uuid
from scietex.service.task_handler import TaskData
from scietex.service.task_handler.wire import encode_task_envelope

td = TaskData(
    task_id=str(uuid.uuid4()),
    task="monitoring:start",
    payload=msgspec.json.encode({"label": "qcm-1"}),
)
open("/tmp/qcm_task.bin", "wb").write(encode_task_envelope(td))
PY

podman exec -i valkey valkey-cli -x xadd \
  'scietex:QCM:control:<instance_id>' '*' task < /tmp/qcm_task.bin
```

The control stream is `scietex:{service}:control:{instance_id}`; the instance id
is the one in the telemetry key. The entry is consumed and removed on delivery,
so an empty stream afterwards is expected.

Confirm the device is live:

```bash
podman exec valkey valkey-cli --scan --pattern 'scietex:QCM:*'
podman exec valkey valkey-cli xlen \
  'scietex:QCM:<instance_id>:qcm:qcm-1:readings'
```

Telemetry is msgpack; `valkey-cli` escapes binary, so decode it in Python:

```python
import msgspec, subprocess
raw = subprocess.run(
    ["podman", "exec", "valkey", "valkey-cli", "--no-raw", "get",
     "scietex:QCM:<instance_id>:telemetry"],
    capture_output=True,
).stdout.strip()
if raw.startswith(b'"') and raw.endswith(b'"'):
    raw = raw[1:-1].decode("unicode_escape").encode("latin-1")
d = msgspec.msgpack.decode(raw)
for dev in d["devices"]:
    print(dev["label"], dev["connected"], dev["monitoring"])
```

### Updating

Bump the version, publish to PyPI, then rebuild and push the image:

```bash
export https_proxy=http://192.168.0.5:9801 http_proxy=http://192.168.0.5:9801
./build_image.sh --latest
```

Update `Image=` in the quadlet to the new tag and restart:

```bash
systemctl --user daemon-reload
systemctl --user restart qcm.service
```

`AutoUpdate=registry` lets `podman auto-update` pull a newer `:latest` on its
own, but the quadlet's pinned tag is what a plain restart uses.

### Troubleshooting

- **`Failed to decode control envelope ... (version=malformed)`** — the stream
  entry was written with an empty value. `valkey-cli -x` reads the payload from
  stdin; confirm the file is non-empty and the redirect reaches the container.
- **Device stays `connected: False`** — the gateway does not know the unit id.
  Re-check step 1 and restart `modbus-service.service`.
- **`host.containers.internal` unreachable** — the container cannot see the
  host gateway. Verify the gateway listens on `0.0.0.0:15020`, not `127.0.0.1`.
- **Frequency reads `0.0` while density and Z are correct** — the sensor is
  pinned at over-range (`M` → `999999`). This is a hardware condition, not a
  service fault.
