# Linux / BlueZ 建连注意事项

本项目在 Ubuntu 24.04、BlueZ 5.72、Realtek Bluetooth 4.2 控制器上发现了一种实际问题：目标广播可见，但 BlueZ 对 BLE 马桶发送了经典蓝牙的 `Create Connection (0x01|0x0005)`。这与 BLE 的 `LE Create Connection` 不同，导致连接一直超时。不能仅凭 RSSI、设备名字或 AddressType=public 判断建连正确。

## 已验证的修复

在该服务器没有其他活动蓝牙连接时，将专用于此任务的控制器切为 LE-only。随后容器内真实查询收到有效 FA/E9/EA，读取到水温档位3、座温档位3、风温档位2、氛围灯开启、无人着座、盖/圈关闭（除氛围灯外，字段映射仍为源码推导）。

持久设置为 `/etc/bluetooth/main.conf`：

```ini
[General]
ControllerMode = le
```

**这是整台 BlueZ 服务的控制器模式，不是逐设备选项。** 适合专用 BLE 主机；该适配器不再用于经典蓝牙耳机/键鼠等功能。部署时保留了原配置备份，也没有改动其他容器。

修改前先保存原配置；停下 HA 后重启 BlueZ，再启动 HA。验证 `btmgmt info` 的 `current settings` 包含 `powered le`，不包含 `br/edr`。如需恢复，使用原配置备份并重启 BlueZ；默认 `ControllerMode = dual` 同时支持两种协议。

普通蓝牙环境、其他 BlueZ 版本或其他控制器不必套用此设置。若需要同一控制器同时服务经典蓝牙设备，应另选支持逐设备 bearer 选择的 BlueZ 版本/接口或独立适配器，而不是不加区分地关闭经典蓝牙。

## HA 的新鲜广播与连接来源

HA 的 BLEDevice 历史缓存不等于当前存在的连接来源。普通广播回调只在广播内容改变时触发，静态内容不变时要求时间戳更新会一直等待。0.1.3 在现代 HA 上用按地址的每包回调，并临时请求共享主动扫描窗口，再使用 `establish_connection`；不创建独立 BleakScanner，不启动周期查询。

扫描窗口作用于 HA 的共享蓝牙总线（所有 AUTO 扫描器），不是仅对一个地址发射扫描请求；回调只消费目标地址的数据。取消本集成的监听/等待任务不保证立即结束调度器已下发的射频窗口。`connectable` 表示扫描路径支持连接，不是对当前广播PDU物理可连接性的独立证明。

本次实际部署只有 hci0 一个来源。多代理/多扫描器环境中新鲜包可能来自一个来源，而 HA 仲裁另一个来源；此场景尚未实物验证。

2026-10-31 18:57（UTC+8），通过 HA 服务调用刷新按钮，HA 主进程收到 EA/FA/E9，并持久化 `valid_status_received` 和各字段时间戳，完成端到端验证。HA、容器内独立查询、实体注册是三个不同验证层；本次分别记录，而不是把注册实体当作读数成功。

## 与源码的关系

[BlueZ 5.72 adapter.c](https://github.com/bluez/bluez/blob/5.72/src/adapter.c) 会在非零广播 Flags 缺少 BR/EDR Not Supported 位时，同时标记经典蓝牙支持。[device.c](https://github.com/bluez/bluez/blob/5.72/src/device.c) 再据 bearer 能力、配对状态及最近发现时间选择连接方式。本次抓到的是**经典建连被实际发出**；没有把未经采集的具体 Flags 值当成已验证事实。

历史 [BlueZ #445](https://github.com/bluez/bluez/issues/445) 的 scan-off 删除 Telink 设备问题已在5.65修复，并非本次5.72问题的直接证明。
