import pytest

from okx_gui.orders import ALGO_TYPES, OrdersError, OrdersWorker, fetch_orders, parse_orders
from okx_gui.positions import Credentials


def raw_order(identifier='1', *, algo=True, **changes):
    row = dict(instId='BTC-USDT-SWAP', instType='SWAP', side='buy', tdMode='cross',
               lever='100', sz='400', cTime='1791568435000', state='live',
               triggerPx='84400', triggerPxType='last', ordPx='84403',
               ordType='trigger' if algo else 'limit', _algo=algo)
    row['algoId' if algo else 'ordId'] = identifier
    row.update(changes)
    return row


METADATA = {'BTC-USDT-SWAP': dict(ctVal='0.01', ctMult='1', ctValCcy='BTC')}


def test_reference_trigger_order_layout_fields_and_coin_quantity():
    row = parse_orders([raw_order()], METADATA)[0]
    assert row.title == 'BTCUSDT 永续'
    assert row.kind == '计划委托' and row.side == '买入'
    assert row.margin_mode == '全仓' and row.leverage == '100x'
    assert row.created == '10/10 01:53:55'
    assert [(caption, text) for caption, text, _ in row.fields] == [
        ('触发价格', '最新 84,400'), ('委托价格', '84,403'), ('委托数量 (BTC)', '4')]


def test_ordinary_partial_order_and_strategy_market_prices():
    rows = parse_orders([
        raw_order('2', algo=False, px='82500.05', accFillSz='100', state='partially_filled'),
        raw_order('3', ordType='oco', tpTriggerPx='85000', tpTriggerPxType='mark',
                  tpOrdPx='-1', slTriggerPx='80000', slTriggerPxType='index', slOrdPx='79999'),
    ], METADATA)
    regular = next(row for row in rows if row.order_id == '2')
    assert regular.state == '部分成交'
    assert ('已成交 (BTC)', '1', '100') in regular.fields
    oco = next(row for row in rows if row.order_id == '3')
    assert ('止盈委托价', '市价', '-1') in oco.fields
    assert ('止损触发', '指数 80,000', '80000') in oco.fields


def test_unknown_contract_metadata_uses_contracts_and_spot_quote_unit():
    rows = parse_orders([raw_order(), raw_order('2', instId='BTC-USDT', instType='SPOT', tgtCcy='quote_ccy')])
    assert any(field[0] == '委托数量 (张)' for field in rows[1].fields)
    assert any(field[0] == '委托数量 (USDT)' for field in rows[0].fields)


@pytest.fixture
def no_throttle(monkeypatch):
    monkeypatch.setattr('okx_gui.orders.time.sleep', lambda seconds: None)


class FakeTrade:
    instances = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.calls = []
        self.closed = False
        self.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.closed = True

    def get_order_list(self, **kwargs):
        self.calls.append(('ordinary', kwargs))
        return {'code': '0', 'data': []}

    def order_algos_list(self, **kwargs):
        self.calls.append(('algo', kwargs))
        data = [raw_order()] if kwargs['ordType'] == 'trigger' else []
        return {'code': '0', 'data': data}


def test_fetch_all_types_and_preserve_read_only_settings(no_throttle):
    rows = fetch_orders(Credentials('key', 'secret', 'phrase'), FakeTrade, instrument_cache=dict(METADATA))
    instance = FakeTrade.instances[-1]
    assert len(rows) == 1
    assert [kwargs['ordType'] for name, kwargs in instance.calls if name == 'algo'] == list(ALGO_TYPES)
    assert instance.kwargs['flag'] == '0'
    assert instance.kwargs['debug'] is False
    assert instance.closed


def test_fetch_all_pages_and_deduplicate(no_throttle):
    class Paged(FakeTrade):
        def get_order_list(self, **kwargs):
            self.calls.append(('ordinary', kwargs))
            data = [raw_order(str(i), algo=False) for i in range(100, 0, -1)] if not kwargs['after'] else [raw_order('0', algo=False)]
            return {'code': '0', 'data': data}

    rows = fetch_orders(Credentials('key', 'secret', 'phrase'), Paged, instrument_cache=dict(METADATA))
    assert len(rows) == 102
    pages = [kwargs for name, kwargs in Paged.instances[-1].calls if name == 'ordinary']
    assert pages[1]['after'] == '1'


def test_partial_endpoint_failure_does_not_report_complete_list(no_throttle):
    class Rejected(FakeTrade):
        def order_algos_list(self, **kwargs):
            if kwargs['ordType'] == 'conditional,oco':
                return {'code': '50113', 'msg': 'example-secret example-phrase'}
            return super().order_algos_list(**kwargs)

    with pytest.raises(OrdersError) as error:
        fetch_orders(Credentials('key', 'example-secret', 'example-phrase'), Rejected, instrument_cache=dict(METADATA))
    assert '50113' in str(error.value)
    assert 'example-' not in str(error.value)
    assert Rejected.instances[-1].closed


def test_repeated_pagination_cursor_is_an_error(no_throttle):
    class Repeating(FakeTrade):
        def get_order_list(self, **kwargs):
            return {'code': '0', 'data': [raw_order(str(i), algo=False) for i in range(100)]}

    with pytest.raises(OrdersError, match='分页'):
        fetch_orders(Credentials('key', 'secret', 'phrase'), Repeating)


def test_stop_prevents_any_request(no_throttle):
    with pytest.raises(OrdersError, match='停止'):
        fetch_orders(Credentials('key', 'secret', 'phrase'), FakeTrade, should_stop=lambda: True)
    assert not FakeTrade.instances[-1].calls


def test_worker_reports_safe_errors_and_can_retry(qtbot, tmp_path):
    path = tmp_path / 'credentials.env'
    path.write_text('apiKey=key\nsecretKey=secret\npassphrase=phrase\n')
    calls = []

    def fetcher(credentials):
        calls.append(True)
        if len(calls) == 1:
            raise OrdersError('网络异常')
        return []

    worker = OrdersWorker(credentials_path=path, interval=60, fetcher=fetcher)
    try:
        with qtbot.waitSignal(worker.error):
            worker.start()
        with qtbot.waitSignal(worker.updated) as result:
            worker.refresh()
        assert result.args == [[], '0']
    finally:
        worker.stop()
        assert worker.wait(1000)
