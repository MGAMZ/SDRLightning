#!/usr/bin/env python3
"""minimal probe: just get any samples flowing"""
import _env  # noqa: F401
import sys

try:
    import numpy as np
    import SoapySDR
    from SoapySDR import SOAPY_SDR_CF32, SOAPY_SDR_RX
except ImportError:
    print("找不到 SoapySDR Python 绑定。请先：")
    print("    conda activate sdr")
    sys.exit(1)


def main():
    devs = SoapySDR.Device.enumerate({"driver": "sdrplay"})
    print("Devices found:", devs)
    if not devs:
        print("No sdrplay devices")
        sys.exit(1)
    sdr = SoapySDR.Device(devs[0])
    print("Opened:", sdr.getHardwareInfo())

    sdr.setSampleRate(SOAPY_SDR_RX, 0, 2e6)
    sdr.setFrequency(SOAPY_SDR_RX, 0, 24e3)
    print("Gain mode:", sdr.getGainMode(SOAPY_SDR_RX, 0))
    sdr.setGainMode(SOAPY_SDR_RX, 0, False)
    print("Gain mode after set:", sdr.getGainMode(SOAPY_SDR_RX, 0))

    gains = sdr.listGains(SOAPY_SDR_RX, 0)
    print("Gains:", gains)
    for g in gains:
        rng = sdr.getGainRange(SOAPY_SDR_RX, 0, g)
        mid = (rng.minimum() + rng.maximum()) / 2
        print(f"  setting {g}={mid}")
        sdr.setGain(SOAPY_SDR_RX, 0, g, mid)
        print(f"    -> actual {g}={sdr.getGain(SOAPY_SDR_RX, 0, g)}")
    print("Sample rate set:", sdr.getSampleRate(SOAPY_SDR_RX, 0))
    print("Center freq set:", sdr.getFrequency(SOAPY_SDR_RX, 0))

    stream = sdr.setupStream(SOAPY_SDR_RX, SOAPY_SDR_CF32, [0])
    print("Stream MTU:", sdr.getStreamMTU(stream))

    buf = np.empty(1024, dtype=np.complex64)
    sdr.activateStream(stream)
    print("Stream activated, trying reads...")
    for i in range(5):
        sr = sdr.readStream(stream, [buf], len(buf), timeoutUs=500000)
        print(f"  try {i}: ret={sr.ret} timeUs={getattr(sr, 'timeNs', 'n/a')}")
        if sr.ret > 0:
            x = buf[:sr.ret]
            print(f"    peak={np.max(np.abs(x)):.4f} rms={np.sqrt(np.mean(np.abs(x)**2)):.4f}")
            break

    sdr.deactivateStream(stream)
    sdr.closeStream(stream)
    print("Done")


if __name__ == "__main__":
    main()