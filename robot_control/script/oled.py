#!/usr/bin/env python3
"""Display current Wi-Fi SSID and local IP on an I2C OLED (default 0x3C)."""

import argparse
import re
import subprocess
import time
from typing import Optional


def run_cmd(cmd: list[str]) -> str:
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    except Exception:
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


def detect_wifi_interface() -> Optional[str]:
    output = run_cmd(["iw", "dev"])
    if output:
        for line in output.splitlines():
            line = line.strip()
            if line.startswith("Interface "):
                return line.split(maxsplit=1)[1].strip()

    output = run_cmd(["bash", "-lc", "ls /sys/class/net"])
    for iface in output.split():
        wireless_flag = run_cmd(["bash", "-lc", f"test -d /sys/class/net/{iface}/wireless && echo yes"])
        if wireless_flag == "yes":
            return iface

    return None


def get_wifi_ssid(interface: Optional[str]) -> str:
    if interface:
        ssid = run_cmd(["iwgetid", interface, "--raw"])
        if ssid:
            return ssid

    ssid = run_cmd(["iwgetid", "--raw"])
    if ssid:
        return ssid

    nmcli = run_cmd(["nmcli", "-t", "-f", "active,ssid", "dev", "wifi"])
    for line in nmcli.splitlines():
        if line.startswith("yes:"):
            return line.split(":", 1)[1] or "(connected)"

    return "Not connected"


def get_interface_ipv4(interface: Optional[str]) -> str:
    if not interface:
        return "No IP"

    output = run_cmd(["ip", "-4", "-o", "addr", "show", "dev", interface])
    match = re.search(r"inet\s+(\d+\.\d+\.\d+\.\d+)", output)
    if match:
        return match.group(1)

    return "No IP"


def get_wifi_signal_dbm(interface: Optional[str]) -> str:
    if not interface:
        return "--dBm"

    output = run_cmd(["iw", "dev", interface, "link"])
    match = re.search(r"signal:\s*(-?\d+)\s*dBm", output)
    if match:
        return f"{match.group(1)}dBm"

    output = run_cmd(["nmcli", "-t", "-f", "IN-USE,SIGNAL", "dev", "wifi"])
    for line in output.splitlines():
        if line.startswith("*:"):
            parts = line.split(":", 1)
            if len(parts) == 2 and parts[1].isdigit():
                return f"{parts[1]}%"

    return "--dBm"


def create_oled(device_type: str, i2c_port: int, i2c_addr: int, width: int, height: int):
    try:
        from luma.core.interface.serial import i2c
        from luma.oled.device import sh1106, ssd1306
    except ImportError as exc:
        raise RuntimeError(
            "Missing dependency: install with 'pip install luma.oled pillow'"
        ) from exc

    serial = i2c(port=i2c_port, address=i2c_addr)

    if device_type == "ssd1306":
        return ssd1306(serial, width=width, height=height)
    if device_type == "sh1106":
        return sh1106(serial, width=width, height=height)

    last_error = None
    for constructor in (ssd1306, sh1106):
        try:
            return constructor(serial, width=width, height=height)
        except Exception as exc:  # pragma: no cover - hardware dependent
            last_error = exc

    raise RuntimeError(f"Unable to initialize OLED device: {last_error}")


def draw_status(device, wifi_name: str, ip: str, iface_info: str) -> None:
    from PIL import Image, ImageDraw, ImageFont

    image = Image.new("1", (device.width, device.height))
    draw = ImageDraw.Draw(image)

    font = getattr(device, "_status_font", ImageFont.load_default())
    line_h = getattr(device, "_status_line_h", 10)

    draw.text((0, 0), f"WiFi:{wifi_name}", font=font, fill=255)
    draw.text((0, line_h), f"IF:{iface_info}", font=font, fill=255)
    draw.text((0, line_h * 2), f"IP:{ip}", font=font, fill=255)

    device.display(image)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Show Wi-Fi SSID and IP on OLED")
    parser.add_argument("--addr", type=lambda x: int(x, 0), default=0x3C, help="I2C OLED address")
    parser.add_argument("--port", type=int, default=1, help="I2C bus port")
    parser.add_argument(
        "--device",
        choices=["auto", "ssd1306", "sh1106"],
        default="auto",
        help="OLED driver type",
    )
    parser.add_argument("--width", type=int, default=128, help="OLED width in pixels")
    parser.add_argument("--height", type=int, default=32, help="OLED height in pixels")
    parser.add_argument("--interval", type=float, default=2.0, help="Refresh interval seconds")
    parser.add_argument("--font-size", type=int, default=10, help="Font size in pixels")
    parser.add_argument(
        "--font-path",
        type=str,
        default="",
        help="Optional TTF font path (e.g. /usr/share/fonts/truetype/dejavu/DejaVuSans.ttf)",
    )
    return parser.parse_args()


def load_font(font_size: int, font_path: str):
    from PIL import ImageFont

    size = max(8, font_size)
    candidates = [font_path] if font_path else []
    candidates.append("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")

    for candidate in candidates:
        if not candidate:
            continue
        try:
            return ImageFont.truetype(candidate, size=size)
        except Exception:
            continue

    return ImageFont.load_default()


def main() -> None:
    args = parse_args()

    try:
        device = create_oled(args.device, args.port, args.addr, args.width, args.height)
    except RuntimeError as exc:
        print(exc)
        return

    font = load_font(args.font_size, args.font_path)
    try:
        bbox = font.getbbox("Ag")
        line_h = max(10, (bbox[3] - bbox[1]) + 2)
    except Exception:
        line_h = max(10, args.font_size + 2)
    device._status_font = font
    device._status_line_h = line_h

    print(
        f"OLED ready on i2c-{args.port} addr={hex(args.addr)} "
        f"({args.width}x{args.height}, mode={args.device})"
    )

    try:
        while True:
            iface = detect_wifi_interface() or "wlan0"
            ssid = get_wifi_ssid(iface)
            ip = get_interface_ipv4(iface)
            signal = get_wifi_signal_dbm(iface)
            draw_status(device, ssid, ip, f"{iface} {signal}")
            time.sleep(max(0.2, args.interval))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
