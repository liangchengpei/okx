import json
from decimal import Decimal

from okx_gui.market import MarketWorker, parse_ticker


def test_ticker_precision_change_and_invalid_data():
    assert parse_ticker({"instId": "DOGE-USDT-SWAP", "last": "0.123450", "open24h": "0.100000"}) == (
        "DOGE-USDT-SWAP", "0.123450", Decimal("23.4500")
    )
    assert parse_ticker({"instId": "X", "last": "1", "open24h": "0"}) == ("X", "1", None)
    for last in ("NaN", "Infinity", "bad", "-1", "0"):
        assert parse_ticker({"instId": "X", "last": last, "open24h": "1"}) is None
    assert parse_ticker({}) is None


def test_worker_routes_only_tickers_and_subscription_errors(qtbot):
    worker = MarketWorker(["BTC-USDT-SWAP"])
    with qtbot.waitSignal(worker.ticker) as signal:
        worker._message(json.dumps({"data": [{"instId": "BTC-USDT-SWAP", "last": "99", "open24h": "100"}]}))
    assert signal.args == ["BTC-USDT-SWAP", "99", Decimal("-1.00")]
    with qtbot.waitSignal(worker.subscription_error) as signal:
        assert worker._message(json.dumps({"event": "error", "arg": {"instId": "BAD-USDT-SWAP"}, "msg": "不存在"})) == "BAD-USDT-SWAP"
    assert signal.args == ["BAD-USDT-SWAP", "不存在"]
    for message in ("pong", "bad json", "[]", '{"event":"subscribe"}'):
        assert worker._message(message) is None
