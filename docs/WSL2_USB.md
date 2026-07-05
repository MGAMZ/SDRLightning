# WSL2 透传 SDRplay RSP1 步骤

RSP1 物理插在 Windows 主机 USB 口上。WSL2 默认**看不见**任何 USB 设备。需要用 `usbipd-win` 把设备桥接到 WSL2。

## 一次性设置（Windows 侧）

### 1. 安装 usbipd-win

打开 PowerShell（**管理员**）：

```powershell
winget install usbipd
```

或从 GitHub releases 下载 .msi：<https://github.com/dorssel/usbipd-win/releases>

### 2. 确认服务在运行

```powershell
usbipd server
```

第一次可能需要重启。如果报"service not running"就重启。

### 3. 插上 RSP1，列出 USB 设备

```powershell
usbipd list
```

输出示例（找到 SDRplay 那行，记下 BUSID，比如 `2-3`）：

```
BUSID  VID:PID    DEVICE                                              STATE
1-1    046d:c52b  Logitech USB Receiver, USB Input Device              Not attached
1-2    1b1c:0a16  USB Mass Storage Device                              Not attached
2-1    8087:0026  Intel(R) Wireless Bluetooth(R)                        Not attached
2-2    1d6b:0003  Linux Foundation 3.0 root hub                         Not attached
2-3    1df7:2500  SDRplay RSP1                                          Not attached
```

注意 SDRplay 的 VID:PID 是 `1df7:2500`（RSP1）或 `1df7:3010`（RSP1A/RSP1B）等。

### 4. 透传到 WSL2

`usbipd-win 5.0+` 改了语法：`wsl` 不再是子命令，而是 `--wsl` flag，且**必须先 bind 再 attach**。

**一次性（重启 WSL2 后失效）**：

```powershell
# 先标记为可共享（管理员，需做一次）
usbipd bind --busid 2-3

# 再 attach 到 WSL（不需要管理员）
usbipd attach --wsl --busid 2-3
```

**永久（重启/插拔都自动恢复）**：

```powershell
usbipd bind --busid 2-3
usbipd attach --wsl --busid 2-3 --auto-attach
```

> **注意**：老语法 `usbipd wsl attach --busid X` 在 5.0+ 已经移除，会报错
> `The 'wsl' subcommand has been removed`。务必用上面新写法。

### 5. WSL2 内验证

切到 WSL2 Ubuntu 终端：

```bash
lsusb | grep -i SDRplay
# 应该看到：Bus 002 Device 002: ID 1df7:2500 SDRplay RSP1

SoapySDRUtil --find
# 应该看到一行 driver=sdrplay
```

## 故障排查

| 症状 | 解决 |
|---|---|
| `usbipd list` 看不到 RSP1 | 拔掉 USB 再插一次；确认 Windows 设备管理器里识别正常 |
| WSL2 里看不到设备 | `usbipd wsl detach --busid 2-3` 然后重新 attach |
| `usbipd wsl attach` 报 `WSL not found` | `wsl --status` 确认 WSL 已装；`wsl --update` |
| `SoapySDRUtil --find` 没有 sdrplay | 装 `SoapySDRPlay3` 没成功，重新跑 install 脚本 |
| 权限不够 / 看不到 `/dev/bus/usb` | `sudo` 一下，或者在 WSL2 里加 udev 规则 |

## udev 规则（可选，省得每次 sudo）

新建 `/etc/udev/rules.d/99-sdrplay.rules`（WSL2 里通常不需要，但写一下方便真机 Linux 用）：

```
SUBSYSTEM=="usb", ATTRS{idVendor}=="1df7", MODE="0666"
```

然后 `sudo udevadm control --reload-rules && sudo udevadm trigger`。

## 拔掉 / 临时释放

把设备还回 Windows：

```powershell
usbipd detach --busid 2-3
```

## 关闭永久透传

```powershell
usbipd attach --wsl --busid 2-3 --no-auto-attach
```

## 参考

- <https://github.com/dorssel/usbipd-win>
- <https://learn.microsoft.com/en-us/windows/wsl/connect-usb>