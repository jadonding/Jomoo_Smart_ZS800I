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

HA 的 BLEDevice 历史缓存不等于当前可连接设备。0.1.2 在手动刷新时，通过 HA 的共享扫描接口临时请求该地址的新鲜可连接广播，然后使用 `establish_connection`，不会创建独立 BleakScanner，也不会启动周期查询。

HA、容器内独立查询、实体注册是三个不同验证层；应分别记录结果，不能把实体注册当作实际读数成功。

## 与源码的关系

[BlueZ 5.72 adapter.c](https://github.com/bluez/bluez/blob/5.72/src/adapter.c) 会在非零广播 Flags 缺少 BR/EDR Not Supported 位时，同时标记经典蓝牙支持。[device.c](https://github.com/bluez/bluez/blob/5.72/src/device.c) 再据 bearer 能力、配对状态及最近发现时间选择连接方式。本次抓到的是**经典建连被实际发出**；没有把未经采集的具体 Flags 值当成已验证事实。

历史 [BlueZ #445](https://github.com/bluez/bluez/issues/445) 的 scan-off 删除 Telink 设备问题已在5.65修复，并非本次5.72问题的直接证明。
