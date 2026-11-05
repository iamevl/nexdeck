"""Initial guardrails for an experimental, manually triggered LAN benchmark."""

import pytest

from app.adapters import lanperf
from app.adapters.base import AdapterError


def test_private_targets_only():
    assert lanperf._target("192.168.1.10") == "192.168.1.10"
    for target in ("8.8.8.8", "127.0.0.1", "169.254.1.1", "example.com", "172.32.0.1"):
        with pytest.raises(AdapterError):
            lanperf._target(target)


def test_parse_iperf_json():
    data = b'{"end":{"sum_received":{"bits_per_second":923400000},"sum_sent":{"retransmits":7}}}'
    reading = lanperf._decode(data, reverse=False)
    assert reading["mbps"] == 923.4
    assert reading["retransmits"] == 7
    with pytest.raises(AdapterError):
        lanperf._decode(b'{"error":"failed"}', reverse=False)


@pytest.mark.asyncio
async def test_connection_check_and_refresh_never_run_iperf(monkeypatch):
    async def forbidden(*args, **kwargs):
        raise AssertionError("Passive check launched a benchmark")

    monkeypatch.setattr(lanperf, "_measure", forbidden)
    adapter = lanperf.LanPerformanceAdapter()
    config = {"host": "192.168.1.10"}
    assert "configured" in await adapter.test(config, None)
    card = await adapter.fetch("latest", config, {}, None)
    assert card.status == "unknown"
    assert len(card.actions) == 2
    assert all(action.confirm for action in card.actions)
