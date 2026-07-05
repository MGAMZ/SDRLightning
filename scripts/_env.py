"""
scripts/_env.py

跨平台 SoapySDR 运行时环境配置：在 `import _env` 时自动把本地构建的
SoapySDRPlay3 模块路径与 libsdrplay_api 库目录注入到对应环境变量，
使后续 `import SoapySDR` / SoapySDRUtil 等能直接找到 sdrplay 模块。

用法::

    import _env        # 自动设置 SOAPY_SDR_PLUGIN_PATH / PATH / LD_LIBRARY_PATH
    import numpy as np

或显式调用::

    from _env import setup
    setup()            # 已 setup 过则 no-op

行为:
  - 探测本仓库 third_party/install/(Linux/WSL 本地构建的 SoapySDRPlay3)
  - 写入 SOAPY_SDR_PLUGIN_PATH
  - 写入 PATH (Windows) / LD_LIBRARY_PATH (Linux)
  - Windows 上同时把 conda env 自带的 bin 放到 PATH 最前 (让 SoapySDR.dll 等优先被找到)
  - 重复调用幂等

平台差异:
  - Windows: SoapySDR 模块路径 = <conda env>\\Library\\lib\\SoapySDR\\modules0.8
             SDRplay API DLL    = <conda env>\\Library\\bin (如果有) 或 C:\\Program Files\\SDRplay\\API\\x64
  - Linux:   SoapySDR 模块路径 = <repo>/third_party/install/lib/SoapySDR/modules0.8
             libsdrplay_api.so  = <repo>/third_party/install/lib
  - 路径分隔符自动用 ; (Win) / : (Linux)
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


# 仓库根目录 (含 third_party/)
REPO_ROOT = Path(__file__).resolve().parent.parent

IS_WINDOWS = sys.platform.startswith("win")
IS_LINUX = sys.platform.startswith("linux")

# 本地构建的 SoapySDRPlay3 / libsdrplay_api 可能位置
LOCAL_PLUGIN_DIR = REPO_ROOT / "third_party" / "install" / "lib" / "SoapySDR" / "modules0.8"
LOCAL_LIB_DIR = REPO_ROOT / "third_party" / "install" / "lib"

# 当前平台的本机库后缀 (.dll / .so)
_NATIVE_LIB_GLOB = "*.dll" if IS_WINDOWS else "*.so*"


def _has_native_lib(d: Path) -> bool:
    """d 是否含当前平台可用的本机库 (.dll on Win / .so on Linux)."""
    if not d.exists() or not d.is_dir():
        return False
    return any(d.glob(_NATIVE_LIB_GLOB))

_setup_done = False


def _append_env(name: str, value: str, sep: str) -> None:
    """把 value 接到已有 env 末尾 (若尚未存在), 用平台分隔符."""
    if not value:
        return
    cur = os.environ.get(name, "")
    parts = [p for p in cur.split(sep) if p]
    if value in parts:
        return
    parts.append(value)
    os.environ[name] = sep.join(parts)


def _prepend_env(name: str, value: str, sep: str) -> None:
    """把 value 放到 env 最前面 (优先级高), 用于覆盖 conda 默认值."""
    if not value:
        return
    cur = os.environ.get(name, "")
    parts = [p for p in cur.split(sep) if p]
    if value in parts:
        parts.remove(value)
    parts.insert(0, value)
    os.environ[name] = sep.join(parts)


def setup(verbose: bool = True) -> None:
    """配环境. 幂等: 重复调用 no-op."""
    global _setup_done
    if _setup_done:
        return

    sep = ";" if IS_WINDOWS else ":"

    # 1) 本地 third_party/install/ 里的 SoapySDRPlay3 模块
    #    仅当目录里有当前平台可用的本机库时才加 (避免 Windows 误用 Linux .so)
    if _has_native_lib(LOCAL_PLUGIN_DIR):
        _prepend_env("SOAPY_SDR_PLUGIN_PATH", str(LOCAL_PLUGIN_DIR), sep)
        if verbose:
            print(f"[env] + SOAPY_SDR_PLUGIN_PATH: {LOCAL_PLUGIN_DIR}")

    # 2) libsdrplay_api (Linux: LD_LIBRARY_PATH; Windows: PATH)
    #    只在当前平台能找到对应后缀的库时才加路径 (避免在 Windows 上误加 .so)
    if _has_native_lib(LOCAL_LIB_DIR):
        if IS_WINDOWS:
            _prepend_env("PATH", str(LOCAL_LIB_DIR), sep)
        else:
            _prepend_env("LD_LIBRARY_PATH", str(LOCAL_LIB_DIR), sep)
        if verbose:
            print(f"[env] + {'PATH' if IS_WINDOWS else 'LD_LIBRARY_PATH'}: {LOCAL_LIB_DIR}")

    # 3) Windows: 把 conda env 自带的 bin 也放到 PATH 最前 (SoapySDR.dll 等)
    #    这样 SoapySDR 的 native lib 总能被找到
    if IS_WINDOWS:
        conda_prefix = os.environ.get("CONDA_PREFIX", "")
        if conda_prefix:
            bin_dir = Path(conda_prefix) / "Library" / "bin"
            if bin_dir.exists():
                _prepend_env("PATH", str(bin_dir), sep)

    # 4) 把 scripts/ 目录加到 sys.path (允许 sibling import _xxx)
    scripts_dir = str(Path(__file__).resolve().parent)
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)

    _setup_done = True


def describe() -> dict[str, str | bool]:
    """返回当前生效的 env 状态, 方便调试."""
    return {
        "platform": "windows" if IS_WINDOWS else ("linux" if IS_LINUX else sys.platform),
        "SOAPY_SDR_PLUGIN_PATH": os.environ.get("SOAPY_SDR_PLUGIN_PATH", ""),
        "LD_LIBRARY_PATH": os.environ.get("LD_LIBRARY_PATH", ""),
        "local_plugin_exists": LOCAL_PLUGIN_DIR.exists(),
        "local_lib_exists": LOCAL_LIB_DIR.exists(),
    }


# 模块 import 时即执行一次
setup()