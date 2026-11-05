"""LAN performance: explicit, bounded iperf3 tests against a trusted private host.

Experimental integration derived from the design of BEACN's MIT-licensed
LAN diagnostics (Copyright (c) 2026 iamevl). No tests run during widget
refresh, connection checks, or demo mode.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import math
import shutil
import time
from typing import Any

from .base import Action, Adapter, AdapterError, Context, Field, WidgetData, WidgetType

PORT = 5201
DURATION = 10
TIMEOUT = 20
MAX_CAPTURE = 1024 * 1024

# One process for the entire application. Network benchmarks compete for bandwidth.
_TEST_LOCK = asyncio.Lock()


def _target(value: Any) -> str:
    """Require a literal RFC1918 IPv4 destination, not a DNS name or URL."""
    try:
        address = ipaddress.IPv4Address(str(value or "").strip())
    except ipaddress.AddressValueError as error:
        raise AdapterError("Enter a private IPv4 address, not a hostname or URL.", code="bad_param") from error
    if not address.is_private or not any(
        address in ipaddress.IPv4Network(block)
        for block in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
    ):
        raise AdapterError("Only RFC1918 private IPv4 targets are permitted.", code="bad_param")
    return str(address)


def _decode(stdout: bytes, *, reverse: bool) -> dict[str, Any]:
    """Treat missing or malformed iperf data as a failure, never as 0 Mbps."""
    try:
        data = json.loads(stdout)
        if not isinstance(data, dict) or data.get("error"):
            raise ValueError("iperf3 reported an error")
        end = data["end"]
        # iperf3 JSON reports sender and receiver in its local client view.
        receiver = end.get("sum_received") or {}
        sender = end.get("sum_sent") or {}
        bps = float(receiver["bits_per_second"])
        if not math.isfinite(bps) or bps < 0:
            raise ValueError("invalid throughput")
        retransmits = sender.get("retransmits")
        if retransmits is not None:
            retransmits = int(retransmits)
            if retransmits < 0:
                raise ValueError("invalid retransmit count")
        return {
            "direction": "reverse" if reverse else "forward",
            "mbps": round(bps / 1_000_000, 2),
            "retransmits": retransmits,
            "at": time.time(),
        }
    except (ValueError, KeyError, TypeError, OverflowError, json.JSONDecodeError) as error:
        raise AdapterError("iperf3 returned no valid TCP measurement.", code="bad_response") from error


async def _measure(host: str, reverse: bool) -> dict[str, Any]:
    """An allowlisted subprocess, with bounded duration and bounded output."""
    if _TEST_LOCK.locked():
        raise AdapterError("A LAN performance test is already running.", code="busy")
    if shutil.which("iperf3") is None:
        raise AdapterError("iperf3 is not installed on the NexDeck host.", code="unavailable")

    async with _TEST_LOCK:
        command = ["iperf3", "-c", host, "-p", str(PORT), "-J", "-t", str(DURATION)]
        if reverse:
            command.append("-R")
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as error:
            raise AdapterError("Unable to launch iperf3.", code="unavailable") from error
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=TIMEOUT)
        except TimeoutError as error:
            process.kill()
            await process.communicate()
            raise AdapterError("The LAN test timed out.", code="timeout") from error
        if len(stdout) > MAX_CAPTURE or len(stderr) > MAX_CAPTURE:
            raise AdapterError("The iperf3 response was too large.", code="bad_response")
        if process.returncode:
            raise AdapterError("The LAN test failed. Check that an iperf3 server is listening on TCP 5201.", code="unreachable")
        return _decode(stdout, reverse=reverse)


class LanPerformanceAdapter(Adapter):
    kind = "lanperf"
    label = "LAN performance (iperf3)"
    category = "network"
    description = "Manually benchmark throughput to one private IPv4 iperf3 server."
    icon = "speedtest-tracker"
    beta = True
    docs_url = "https://iperf.fr/iperf-doc.php"
    fields = (
        Field("host", "Destination IPv4", required=True, placeholder="192.168.1.10",
              help="Private IPv4 address of a trusted host running iperf3 -s on TCP port 5201."),
    )
    widgets = (
        WidgetType(
            kind="latest", label="LAN throughput", renderer="value",
            description="Last manually requested measurement; refresh never runs a test.",
            default_size=(3, 2), refresh_seconds=60, metrics=("forward", "reverse"),
        ),
    )

    def __init__(self) -> None:
        # Initial proof of concept: memory only; no database migration or automatic testing.
        self._last: dict[str, dict[str, dict[str, Any]]] = {}

    async def test(self, config: dict[str, Any], ctx: Context) -> str:
        host = _target(config.get("host"))
        return f"LAN test target {host} is configured. Tests run only when requested."

    async def fetch(
        self, widget_kind: str, config: dict[str, Any], options: dict[str, Any], ctx: Context
    ) -> WidgetData:
        host = _target(config.get("host"))
        readings = self._last.get(host, {})
        forward = readings.get("forward")
        reverse = readings.get("reverse")
        latest = max(readings.values(), key=lambda row: row["at"]) if readings else None
        return WidgetData(
            status="ok" if readings else "unknown",
            primary={
                "label": "Last measurement",
                "value": latest["mbps"] if latest else None,
                "unit": "Mbps",
            },
            secondary=[
                {"label": "Forward", "value": forward["mbps"] if forward else "Not tested", "unit": "Mbps" if forward else ""},
                {"label": "Reverse", "value": reverse["mbps"] if reverse else "Not tested", "unit": "Mbps" if reverse else ""},
                {"label": "Retransmits", "value": latest["retransmits"] if latest and latest["retransmits"] is not None else "?"},
                {"label": "Destination", "value": host},
            ],
            metrics={
                key: row["mbps"] for key, row in (("forward", forward), ("reverse", reverse)) if row
            },
            actions=[
                Action(id="forward", label="Test forward throughput", icon="play", confirm=True),
                Action(id="reverse", label="Test reverse throughput", icon="play", confirm=True),
            ],
        )

    async def action(
        self, widget_kind: str, action_id: str, params: dict[str, Any],
        config: dict[str, Any], options: dict[str, Any], ctx: Context,
    ) -> str:
        if widget_kind != "latest" or action_id not in ("forward", "reverse"):
            raise AdapterError("No such LAN test action.", code="no_such_action")
        host = _target(config.get("host"))
        result = await _measure(host, reverse=(action_id == "reverse"))
        self._last.setdefault(host, {})[result["direction"]] = result
        ctx.forget_answers()
        return f"{result['direction'].capitalize()} LAN test: {result['mbps']} Mbps."

    def demo(self, widget_kind: str, options: dict[str, Any], tick: int) -> WidgetData:
        return WidgetData(
            status="unknown",
            primary={"label": "Last measurement", "value": None, "unit": "Mbps"},
            secondary=[{"label": "Destination", "value": "192.168.1.10"},
                       {"label": "State", "value": "No test performed"}],
            meta={"empty": "Start a test on a real connection to measure LAN throughput."},
        )


ADAPTER = LanPerformanceAdapter()
