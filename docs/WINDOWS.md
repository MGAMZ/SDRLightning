# Windows 平台安装与使用

RSP1 在 Windows 上**直接插 USB** 就能用 (不需要 WSL2 透传)。SDRplay 官方提供了 Windows 版的 API + 驱动 + GUI, 也可以用 SoapySDR + 本仓库的脚本。

## 一次性安装

### 1. SDRplay API + 驱动

到 <https://www.sdrplay.com/downloads/> 下载最新的 **SDRplay RSP API for Windows** MSI, 双击装.

它会:
- 安装 `sdrplay_api.dll` 到 `C:\Program Files\SDRplay\API\x64\`
- 注册一个 Windows service `SDRplay API` (自动启动)
- 装好 RSP1 的 USB 驱动

### 2. SoapySDR + SoapySDRPlay3

两条路, 选一条:

#### 2a. 装 PothosSDR (推荐, 含 GUI 工具)

下载 PothosSDR: <https://github.com/pothosware/PothosSDR/releases>

里面已经包含:
- SoapySDR runtime + utilities (`SoapySDRUtil.exe`)
- 一堆 SoapySDR 模块 (RTL-SDR, HackRF, Airspy, ...)

装好后把 `C:\Program Files\PothosSDR\bin` 加到 PATH, 跑 `SoapySDRUtil.exe --info` 验证.

⚠ PothosSDR 的某些版本**没有**预编译 `sdrPlaySupport` 模块. 如果 `SoapySDRUtil --probe="driver=sdrplay"` 找不到设备, 走 2b.

#### 2b. 自己编译 SoapySDRPlay3

如果 PothosSDR 里没 `sdrPlaySupport.dll`, 自己从源码编一份:

```powershell
# 在 Visual Studio Developer PowerShell 里跑 (有 cmake + cl)
cd C:\work
git clone https://github.com/pothosware/SoapySDRPlay3.git
cd SoapySDRPlay3
mkdir build; cd build
cmake .. -DCMAKE_INSTALL_PREFIX="C:\Program Files\PothosSDR"
cmake --build . --config Release -j
cmake --install . --config Release
```

或者用本仓库的 `third_party\SoapySDRPlay3\` (如果有), `cmake --install` 时把 prefix 改成本仓库的 `third_party/install`, `scripts\_env.py` 会自动找到它.

### 3. Python 环境

```powershell
conda create -n sdr python=3.12 -y
conda activate sdr
conda install -c conda-forge --solver=classic `
    soapysdr numpy scipy matplotlib flask flask-socketio plotext -y
```

> ⚠ 当前 conda 的 libmamba solver 在某些 Windows 上有 DLL 加载问题. 加 `--solver=classic` 绕开.

验证:

```powershell
python -c "import SoapySDR; print(SoapySDR.getLibVersion())"
# 应该打印 0.8.1-x

SoapySDRUtil.exe --probe="driver=sdrplay"
# 应该看到 RSP1 的硬件信息
```

### 4. 启动 Web UI

```powershell
conda activate sdr
cd C:\path\to\SDR
python scripts\web_monitor.py --port 5000
```

浏览器打开 <http://localhost:5000>.

## 调试助手

```powershell
. scripts\env.ps1                # 打印环境状态
. scripts\env.ps1 -CheckService  # 检查 SDRplay API service 是否在跑
. scripts\env.ps1 -StartService  # 以管理员身份启动 service
```

## 与 WSL2 安装的差异

| 项 | Linux/WSL2 | Windows |
|---|---|---|
| USB 透传 | 需要 `usbipd-win` | 直接插 USB |
| SDRplay API | 共享内存 IPC daemon (`sdrplay_apiService`, 需 root) | Windows service (自动启动) |
| SoapySDR 模块位置 | `third_party/install/lib/SoapySDR/modules0.8/` 或 `/usr/lib/.../SoapySDR/modules0.8/` | `C:\Program Files\PothosSDR\SoapySDR\lib\SoapySDR\modules0.8\` |
| 路径分隔符 | `:` | `;` |
| 库后缀 | `.so` | `.dll` |
| 启停 service | `sudo bash scripts/start_sdrplay_service.sh` | Windows 服务管理器 或 `Start-Service 'SDRplay API'` |
| Python env | `conda activate sdr` | `conda activate sdr` (相同) |
| 启动 web UI | `python scripts/web_monitor.py` | `python scripts\web_monitor.py` (相同) |

## 常见问题

### `SoapySDRUtil --probe="driver=sdrplay"` 看不到设备

- 检查 RSP1 USB 是否被 Windows 识别: 设备管理器 → SDRplay RSP1
- 检查 service 在跑: `Get-Service 'SDRplay API'` (PowerShell)
- 检查 sdrplay_api.dll 路径: `C:\Program Files\SDRplay\API\x64\sdrplay_api.dll` 应存在且在 PATH
- 卸载重装 SDRplay API (90% 时候有效)

### conda install 报 libmamba DLL 错

```powershell
conda install -n sdr -c conda-forge --solver=classic <pkg> -y
```

### `ImportError: DLL load failed while importing _SoapySDR`

`SoapySDR.dll` 没找到. 解决:

1. 确认 `conda install -n sdr soapysdr` 真装了 (不是 base env)
2. 跑 `python -c "import os; print(os.environ['CONDA_PREFIX'])"` 确认指向 `D:\Miniforge\envs\sdr`
3. `scripts\_env.py` 会在 import 时把 `<env>\Library\bin` 加到 PATH

### USB 驱动冲突

如果之前装过 HDSDR / SDRuno / SDR# 等其他 SDRplay 应用, 它们可能装了自己的驱动覆盖了 SDRplay API 需要的那个. 重装 SDRplay API 通常能恢复.

## 参考

- SDRplay API: <https://www.sdrplay.com/downloads/>
- SoapySDR: <https://github.com/pothosware/SoapySDR>
- SoapySDRPlay3: <https://github.com/pothosware/SoapySDRPlay3>
- PothosSDR: <https://github.com/pothosware/PothosSDR>