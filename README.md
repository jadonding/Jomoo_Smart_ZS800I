# 九牧 ZS800I — Home Assistant BLE 集成

本地蓝牙、无需云端或MQTT。当前版本为**只读状态快照＋按需查询**，支持广播名 `JOMOO_SMART_237371` 的 ZS800I；`279029` 是另一套协议，不在此版本支持列表中。

## 已完成的验证

- 实机确认 FFE0 / FFE1；FFE1 使用 notify、read、write-without-response。
- Windows 上实际发送状态查询，收到并校验 FA / E9 / EA。
- 独立实物测试确认氛围灯状态位在单字段关/开操作后变化并恢复。集成暂不提供灯或其他动作控制。
- Ubuntu 24.04 / BlueZ 5.72 主机已实际发现同一马桶，信号约 -44dBm。
- 24项纯Python/mock单元测试通过。
- HA Container **2026.10.4 / Python 3.14** 上配置检查及配置流、协调器、全部实体模块的真实导入通过；Web服务返回HTTP 200。
- HA真实配置流已创建配置项，并注册15个实体；YAML导入及查询按钮触发已验证。
- **远端端到端读取仍未通过**：Ubuntu主机可发现目标，但容器及HA内3次建连均超时；同一时段Windows仍能读取FA/E9/EA。当前定位在远端蓝牙连接层，尚非完整可用部署，不把扫描成功或实体创建当作读数成功。

## 安装

建议使用近期稳定版Home Assistant。`hacs.json`声明2025.1为最低版本，但未进行完整版本矩阵测试。

### HACS自定义仓库

仓库：`https://github.com/jadonding/Jomoo_Smart_ZS800I`，类别选择 **Integration**。安装后重启HA。

### 手动安装

从仓库或 `dist/` 安装包取出 `custom_components/jomoo_zs800i`，复制到HA配置目录下同名位置，重启HA。

### 添加设备

设置 → 设备与服务 → 添加集成 → **JOMOO ZS800I**，输入设备蓝牙MAC。也可确认HA自动发现的精确名称设备。

添加后按 **刷新状态**。启动与添加配置不自动连接，没有周期轮询。初次未刷新前动态状态为unknown。

也可用可选YAML导入同一配置项（不会自动查询）：

```yaml
jomoo_zs800i:
  address: "AA:BB:CC:DD:EE:FF"
```

重启后自动创建/复用该地址的集成项；已有配置项不会重复创建。YAML只负责首次导入，删除YAML不自动删除已有集成。

## 蓝牙部署

- HA OS：使用正常工作的本机/USB蓝牙适配器。
- HA Container：宿主机运行BlueZ，映射 `/run/dbus:/run/dbus:ro`，使用host网络；见 `compose.yaml` 示例。Ubuntu上实测docker-default AppArmor会阻止DBus AddMatch；示例仅对此容器设置 `apparmor:unconfined`，未更改主机全局AppArmor策略。其他系统可按实际情况省略该设置。
- 主机防火墙需允许用户局域网访问8123/TCP；不要为了本项目停用防火墙或开放所有来源。
- ESPHome：也可通过支持主动连接的蓝牙代理提供连接通道；本项目未做代理实物验证。
- 设备应在HA蓝牙覆盖范围内；手机小程序占用连接时先断开手机。

`compose.yaml` 和 `configuration.yaml` 用于全新Container部署示例。已有HA请只安装自定义组件，勿覆盖原配置。

## 实体

| 实体 | 说明 |
| --- | --- |
| 刷新状态 | 连接、发送固定查询、读取通知、断开 |
| 最后状态时间/最后查询结果 | 区分新观察、旧快照与失败 |
| 原始FA/E9/EA | 大写HEX，不含空格，最多42字符；长帧显示截断 |
| 型号/设备地址 | 配置信息 |
| 水温/座温/风温档位 | 源码语义推导，不是摄氏度 |
| 着座/盖打开/圈打开 | 源码语义推导，仍需逐项实物操作对照 |
| 氛围灯状态 | 状态位已通过单字段控制回读验证 |

所有动态值是**最后观察快照**，并非断开后的连续实时状态。实体附快照标记、查询结果、观察时间。

## 连接与故障行为

使用HA蓝牙栈提供的可连接BLEDevice，不直接调用BleakScanner，不向Bleak传裸地址触发隐式扫描。
每次刷新独占锁，通过HA蓝牙依赖中的 `bleak_retry_connector.establish_connection` 建连，单次连接超时20秒、最多3次尝试（含库的等待/退避，整体可能超过60秒）；FFE1属性存在 `write` 时带响应写，否则按 `write-without-response` 写。
仅发送固定查询 `F3 F4 08 92 00 E2 00 00 00 00 7C FC`。
从发送起8秒预算内等待本次有效FA/E9/EA，首包后额外收集0.4秒突发通知，随后释放连接。
收到有效通知只证明本次连接的状态接收有效，不独立证明所有通知都由查询触发。

查询失败保留旧快照和原观察时间，并更新失败结果；本次没有新通知时不使用旧缓存冒充成功。
并发刷新报告忙；卸载等待查询释放锁，取消也清理连接。当前无冲水、加热、清洗、温度或任意原始写入接口。

## 协议与解析边界

帧头F3F4，`总长度=frame[2]+4`，`sum(frame[2:-2])&255==frame[-2]`，帧尾FC。
数据仅从 `frame[6:-2]` 取；校验和与帧尾绝不当作状态字段。
支持碎片、粘包、噪声、坏校验/坏长度重同步；单次输入上限4096字节，未完成帧缓冲有界。

- FA：水温 `(payload[0]>>4)&7`；座温 `payload[1]&7`；风温 `payload[2]&7`；氛围灯 `payload[5] bit4`。
- E9：着座 `payload[0] bit7`。
- EA：圈打开 `payload[0] bit1`，盖打开 `payload[0] bit3`。237371的型号配置关闭E9/E2盖圈来源，因此仅使用EA。

参考项目的19字节全量设置会重新构造多个字段，未保留未知位；本集成没有照搬该写入接口。

## 开发与打包

```sh
python -m unittest discover -s tests -v
python build.py
```

单元测试使用HA/bleak stub，不连接硬件。覆盖严格校验、分片/粘包/重同步、短载荷、两种写入模式、超时、取消清理、缓存、锁及打包元数据。
`build.py` 输出可手动安装的ZIP和SHA256；包中不含设备地址、账户、HA存储或扫描报告。

## 协议来源与许可

独立Python实现，MIT许可。未收录第三方反编译小程序源码。

- [wilton999/JomooToilet](https://github.com/wilton999/JomooToilet)
- [公开小程序型号表及控制器参考](https://github.com/sxz799/jomoo-sq9650-homeassistant)
- [Home Assistant Bluetooth API](https://developers.home-assistant.io/docs/core/bluetooth/api/)
