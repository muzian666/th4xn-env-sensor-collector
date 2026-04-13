# TH4xN Environment Sensor Data Collector

[中文](#背景) | English

---

## Background

While inspecting internal network traffic, I noticed a device continuously sending UDP packets to public IP `120.79.239.247` on **port 6666**. Tracing it down, it turned out to be a YiWeiLian (易维联) TH4xN temperature/humidity sensor.

This meant the sensor was quietly uploading environmental data to an unknown cloud server. Concerned about data security and privacy, I decided to intercept and store the data locally instead of letting it phone home to the vendor's cloud platform.

## Interception

The approach uses a FortiGate firewall DNAT (VIP) to redirect the sensor's UDP traffic to a local collector service — no sensor reconfiguration needed.

```
Sensor (UDP:6666)
    │
    ▼
FortiGate DNAT (VIP)
    │
    ▼
K8s LoadBalancer
    │
    ├── UDP :6666  → collector (data capture + protocol response)
    └── TCP :8080  → FastAPI  (Web Dashboard + API)
```

### FortiGate DNAT Configuration

Create a VIP to redirect the sensor's default target IP to the collector:

```
config firewall vip
    edit "sensor-collector"
        set type static-nat
        set extip 120.79.239.247       # the vendor's cloud server the sensor reports to
        set mappedip <collector-lb-ip>  # your collector LoadBalancer IP
        set extintf "any"
    next
end
```

Create a firewall policy to allow the traffic:

```
config firewall policy
    edit <policy_id>
        set srcintf "<sensor-port>"
        set dstintf "any"
        set srcaddr "<sensor-ip>"          # sensor IP or Address Group
        set dstaddr "sensor-collector"     # the VIP created above
        set action accept
        set schedule "always"
        set service "ALL"
        set nat enable
    next
end
```

For multiple sensors, use a FortiGate Address Group to cover them all in a single policy.

## Protocol Analysis

Using `test_listen.py` to capture raw packets on UDP 6666, I reverse-engineered the sensor protocol:

```
Frame:  0x7e <payload> <checksum:2> 0x0d
Payload: <device_type:1> <device_id:5> <cmd:1> <seq:1> <data_length:1> <data:N>

Body (sensor data):
  [0]    = flags/channel
  [1]    = temperature (0.1 degC, 1 byte unsigned)
  [2:4]  = humidity (0.1 %RH, 2 bytes big-endian)
```

The sensor sends two types of commands:
- `cmd=0x01`: Data report (temperature and humidity)
- `cmd=0x02`: Heartbeat

Key finding: the sensor expects a response after each packet. Without one, it may retransmit or behave unexpectedly. The collector therefore mimics the original cloud server's response to keep the sensor happy.

### Known Issue: Humidity Sensor Lockup

During testing, a critical bug was discovered: **sharing response templates between different sensors causes sensor malfunction.**

Symptoms:
1. A second sensor's humidity reading suddenly locked at 80.1% and stopped changing
2. The body data showed a `04` status byte (normal is `00`), indicating the sensor entered an error state
3. Root cause: the CMD=01 response packet contains device-specific parameters at bytes 24-25. Sending wrong parameters to a different sensor causes its humidity probe to enter an error mode

**Solution: each sensor must use its own response template.**

### CMD=01 Response Structure (43 bytes)

```
Offset  Content               Description
0       0x7E                  Frame start
1       0xC0                  Device type
2-6     <device_id>           Device ID (5 bytes, patched at runtime)
7       0x01                  Command number
8-9     fixed                 Sequence + data length
10-15   fixed                 Protocol parameters
16      <month>               Month (dynamically updated)
17      <day>                 Day (dynamically updated)
18      <hour>                Hour (dynamically updated)
19      <minute>              Minute (dynamically updated)
20      <second>              Second (dynamically updated)
21-23   fixed                 Protocol parameters
24-25   <device params>       **Device-specific**, affects sensor behavior
26-38   fixed                 Protocol parameters
39-40   <checksum>            Checksum
42      0x0D                  Frame end
```

The collector automatically patches bytes 2-6 (device ID) and 16-20 (timestamp) at runtime. Bytes 24-25 **must** come from the actual device's captured response.

## Quick Start

To use this project with your own TH4xN sensors, you need to obtain per-device response templates.

### Step 1: Capture Response Packets

Before setting up DNAT, let the sensor communicate with `120.79.239.247` normally. Capture the bidirectional traffic:

**Option A — FortiGate sniffer** (recommended, sees both directions):
```bash
diagnose sniffer packet any 'host <sensor_ip> and udp port 6666' 6 0 l
```

**Option B — Port mirror + test_listen.py**:
Mirror the sensor's switch port to a machine running:
```bash
python test_listen.py
```

### Step 2: Extract Response Hex

From the capture, identify the **server → sensor** packets (responses from `120.79.239.247`). You need two per device:
- CMD=01 response (data report ACK)
- CMD=02 response (heartbeat ACK)

### Step 3: Configure Templates

Edit `response_templates.json` and add an entry for each device:

```json
{
    "DEVICE_ID_UPPERCASE": {
        "cmd_01": "<full hex of CMD=01 response>",
        "cmd_02": "<full hex of CMD=02 response>"
    }
}
```

The device ID portion (bytes 2-6) and timestamp (bytes 16-20) are automatically updated at runtime. Only bytes 24-25 and other fixed fields must match the captured packet exactly.

### Step 4: Deploy

Configure DNAT, build the image, and deploy.

## Implementation

### Components

- **collector.py** — UDP listener + protocol parser + FastAPI web server
- **dashboard.html** — Real-time temperature/humidity dashboard frontend
- **response_templates.json** — Per-device response templates (edit this for your sensors)
- **Dockerfile** — Container image
- **k8s.yaml** — Kubernetes deployment manifest
- **test_listen.py** — Simple UDP listener for debugging/packet capture

### Deployment

```bash
# Build and push image
podman build -t <your-registry>:5000/sensor-collector:latest .
podman push <your-registry>:5000/sensor-collector:latest --tls-verify=false

# Deploy to K8s
kubectl apply -f k8s.yaml

# Update (rebuild and rollout)
podman build -t <your-registry>:5000/sensor-collector:latest .
podman push <your-registry>:5000/sensor-collector:latest --tls-verify=false
kubectl rollout restart deployment sensor-collector
```

### Data Persistence

The SQLite database is stored via hostPath at `/data/sensor-collector/` on the K8s node. Data survives pod restarts, but won't migrate if the pod is rescheduled to a different node.

### Web Dashboard

Access the dashboard on the collector's **TCP 8080** port.

#### Features

- Real-time temperature and humidity display (auto-refresh every 10 seconds)
- Historical trend charts (6H / 24H / 3D / 7D)
- Statistics (avg/min/max temperature and humidity)
- Device nicknames (click the edit button on a card to rename, e.g. "IT Room")
- Alert configuration (set temperature/humidity thresholds; card turns red on threshold breach)

#### API Endpoints

| Endpoint | Description |
|----------|-------------|
| `GET /api/devices` | Device list (with nicknames) |
| `GET /api/latest` | Latest readings for all devices |
| `GET /api/history?device_id=X&hours=24` | Historical data |
| `GET /api/stats?device_id=X&hours=24` | Statistics (avg/min/max) |
| `GET /api/alerts` | Alert configuration |
| `POST /api/alerts` | Set alert thresholds |
| `POST /api/device-name` | Set device nickname |

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `LISTEN_HOST` | `0.0.0.0` | UDP listen address |
| `LISTEN_PORT` | `6666` | UDP listen port |
| `HTTP_PORT` | `8080` | Web server port |
| `DB_PATH` | `/data/sensor_data.db` | SQLite database path |
| `TEMPLATES_PATH` | `response_templates.json` | Per-device response templates file |
| `LOG_LEVEL` | `INFO` | Log level |
| `TZ` | `Asia/Shanghai` | Timezone |

---

## 背景

在排查内网流量时，我发现有一台设备在持续向公网 IP `120.79.239.247` 的 **UDP 6666 端口**发送数据包。经过追踪，发现这是一台 YiWeiLian (易维联) TH4xN 温湿度传感器。

这意味着这台传感器采集的温湿度数据，正在被发送到一个我不了解的云端服务器。出于对数据安全和隐私的考虑，我决定将数据截留在本地，而不是让它上传到厂商的云平台。

## 拦截方案

通过 FortiGate 防火墙的 DNAT（VIP），将传感器的 UDP 流量重定向到本地 collector 服务，从而在不修改传感器配置的前提下截获所有数据。

架构图见 [英文部分](#interception)。

### FortiGate DNAT 配置

创建 VIP，将传感器默认上报的目标 IP 重定向到 collector：

```
config firewall vip
    edit "sensor-collector"
        set type static-nat
        set extip 120.79.239.247       # 传感器默认上报的服务器 IP
        set mappedip <collector-lb-ip>  # collector LoadBalancer IP
        set extintf "any"
    next
end
```

创建防火墙策略允许流量通过：

```
config firewall policy
    edit <policy_id>
        set srcintf "<sensor-port>"
        set dstintf "any"
        set srcaddr "<sensor-ip>"          # 传感器 IP 或 Address Group
        set dstaddr "sensor-collector"     # 上面创建的 VIP
        set action accept
        set schedule "always"
        set service "ALL"
        set nat enable
    next
end
```

多传感器可以用 Address Group 合并到一条策略中。

## 协议分析

用 `test_listen.py` 在 UDP 6666 上抓包，分析传感器发出的原始数据：

```
Frame:  0x7e <payload> <checksum:2> 0x0d
Payload: <device_type:1> <device_id:5> <cmd:1> <seq:1> <data_length:1> <data:N>

Body (sensor data):
  [0]    = flags/channel
  [1]    = temperature (0.1 degC, 1 byte unsigned)
  [2:4]  = humidity (0.1 %RH, 2 bytes big-endian)
```

传感器主要发送两种命令：
- `cmd=0x01`: 数据上报（包含温湿度）
- `cmd=0x02`: 心跳包

关键发现：传感器发送数据后期望收到服务器回复，如果不回复，传感器可能会反复重传或行为异常。因此 collector 需要模拟原始云服务器的响应包来"安抚"传感器。

### 已知问题：湿度传感器锁死

测试中发现了一个严重 bug：**不同传感器之间共用响应模板会导致传感器故障。**

症状：
1. 第二台传感器的湿度读数突然锁定在 80.1% 不再变化
2. body 数据出现 `04` 状态字节（正常应为 `00`），传感器进入异常状态
3. 根本原因：CMD=01 回复包的 24-25 字节包含设备相关参数，不同传感器参数不同。发送错误参数导致湿度探头进入错误模式

**解决方案：每台传感器必须使用独立的回复模板。**

### CMD=01 回复包结构（43 字节）

```
位置  内容                     说明
0     0x7E                    帧起始
1     0xC0                    设备类型
2-6   <device_id>             设备 ID（5 字节，运行时自动替换）
7     0x01                    命令号
8-9   固定                    序列号 + 数据长度
10-15 固定                    协议参数
16    <month>                 月份（运行时动态更新）
17    <day>                   日期（运行时动态更新）
18    <hour>                  小时（运行时动态更新）
19    <minute>                分钟（运行时动态更新）
20    <second>                秒（运行时动态更新）
21-23 固定                    协议参数
24-25 <设备参数>              **因设备而异**，影响传感器行为
26-38 固定                    协议参数
39-40 <checksum>              校验和
42    0x0D                    帧结束
```

collector 会自动更新 2-6（设备 ID）和 16-20（时间戳），但 24-25 **必须**来自该设备的实际抓包数据。

## 实现

### 快速上手

要让这个项目适配你自己的 TH4xN 传感器，你需要为每台设备获取独立的响应模板。

#### 第 1 步：抓取响应包

在配置 DNAT 之前，让传感器正常与 `120.79.239.247` 通信。抓取双向流量：

**方式 A — FortiGate sniffer**（推荐，能看到双向数据）：
```bash
diagnose sniffer packet any 'host <sensor_ip> and udp port 6666' 6 0 l
```

**方式 B — 端口镜像 + test_listen.py**：
将传感器所在的交换机端口镜像到一台机器上运行：
```bash
python test_listen.py
```

#### 第 2 步：提取响应 Hex

从抓包数据中找出**服务器→传感器**方向的数据包（即 `120.79.239.247` 的回复）。每台设备需要两个：
- CMD=01 响应（数据上报 ACK）
- CMD=02 响应（心跳 ACK）

#### 第 3 步：配置模板

编辑 `response_templates.json`，为每台设备添加条目：

```json
{
    "设备ID大写": {
        "cmd_01": "<CMD=01 回复的完整 hex>",
        "cmd_02": "<CMD=02 回复的完整 hex>"
    }
}
```

设备 ID（bytes 2-6）和时间戳（bytes 16-20）会在运行时自动更新，只有 24-25 等固定字段必须与抓包完全一致。

#### 第 4 步：部署

配置 DNAT（参见上文），构建镜像并部署。

### 核心组件

- **collector.py** — UDP 监听 + 协议解析 + FastAPI Web 服务
- **dashboard.html** — 实时温湿度仪表盘前端
- **response_templates.json** — 每台传感器的独立响应模板（需要编辑此文件）
- **Dockerfile** — 容器化部署
- **k8s.yaml** — Kubernetes 部署配置
- **test_listen.py** — 简单的 UDP 抓包工具，用于调试

### 部署

```bash
# 构建并推送镜像
podman build -t <your-registry>:5000/sensor-collector:latest .
podman push <your-registry>:5000/sensor-collector:latest --tls-verify=false

# 部署到 K8s
kubectl apply -f k8s.yaml

# 更新
podman build -t <your-registry>:5000/sensor-collector:latest .
podman push <your-registry>:5000/sensor-collector:latest --tls-verify=false
kubectl rollout restart deployment sensor-collector
```

### 数据持久化

SQLite 数据库通过 hostPath 挂载在 K8s 节点的 `/data/sensor-collector/` 目录下，pod 重启数据不丢失（但如果 pod 被调度到其他节点，数据不会迁移）。

### Web Dashboard

访问 collector 的 **TCP 8080** 端口即可打开仪表盘。

#### 功能

- 实时温湿度显示（每 10 秒自动刷新）
- 历史趋势图表（6H / 24H / 3D / 7D）
- 统计数据（平均/最高/最低温度和湿度）
- 设备昵称（点击卡片上的编辑按钮可自定义名称，如 "IT Room"）
- 告警配置（设置温湿度阈值，超限时卡片变红）

#### API 端点

| 端点 | 说明 |
|------|------|
| `GET /api/devices` | 设备列表（含昵称） |
| `GET /api/latest` | 各设备最新读数 |
| `GET /api/history?device_id=X&hours=24` | 历史数据 |
| `GET /api/stats?device_id=X&hours=24` | 统计数据（avg/min/max） |
| `GET /api/alerts` | 告警配置 |
| `POST /api/alerts` | 设置告警阈值 |
| `POST /api/device-name` | 设置设备昵称 |

## 环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `LISTEN_HOST` | `0.0.0.0` | UDP 监听地址 |
| `LISTEN_PORT` | `6666` | UDP 监听端口 |
| `HTTP_PORT` | `8080` | Web 服务端口 |
| `DB_PATH` | `/data/sensor_data.db` | SQLite 数据库路径 |
| `TEMPLATES_PATH` | `response_templates.json` | 每台设备的响应模板文件 |
| `LOG_LEVEL` | `INFO` | 日志级别 |
| `TZ` | `Asia/Shanghai` | 时区 |
