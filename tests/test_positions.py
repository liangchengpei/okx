import httpx
import pytest

from okx_gui.positions import (
    Credentials, PositionsError, PositionsWorker, fetch_positions, load_credentials,
    parse_positions,
)


@pytest.fixture
def credentials_file(tmp_path):
    path = tmp_path / 'credentials.env'
    path.write_text("export OKX_API_KEY=example-key\nOKX_SECRET_KEY=example-secret\nOKX_PASSPHRASE='phrase # with spaces'\n")
    return path


def test_credentials_aliases_quotes_and_no_secrets_in_repr(credentials_file):
    credentials = load_credentials(credentials_file)
    assert credentials.api_key == 'example-key'
    assert credentials.passphrase == 'phrase # with spaces'
    assert credentials.flag == '0'
    assert 'example' not in repr(credentials)
    with credentials_file.open('a') as stream:
        stream.write('OKX_FLAG=1\n')
    assert load_credentials(credentials_file).flag == '1'


@pytest.mark.parametrize('text,match', [
    ('apiKey=k\nsecretKey=s', 'passphrase'),
    ("apiKey='private-value", '引号'),
    ('apiKey=private value', '空格'),
    ('invalid private-value', '格式'),
    ('apiKey=k\nsecretKey=s\npassphrase=p\nflag=2', 'OKX_FLAG'),
])
def test_invalid_credentials_are_safe(tmp_path, text, match):
    path = tmp_path / 'credentials.env'
    path.write_text(text)
    with pytest.raises(PositionsError, match=match) as error:
        load_credentials(path)
    assert 'private' not in str(error.value)


def test_credentials_does_not_execute_shell(tmp_path):
    marker = tmp_path / 'executed'
    path = tmp_path / 'credentials.env'
    path.write_text(f"apiKey=k\nsecretKey=s\npassphrase='$(touch {marker})'\n")
    assert load_credentials(path).passphrase.startswith('$(touch ')
    assert not marker.exists()


def position(**overrides):
    return dict(instId='BTC-USDT-SWAP', instType='SWAP', pos='-2.50',
                posSide='net', avgPx='82600.05', markPx='82601.02',
                upl='-0.15', ccy='USDT', lever='3', mgnMode='cross', **overrides)


def test_position_precision_units_and_directions():
    rows = parse_positions([position(), {**position(), 'pos': '0'},
                            {**position(), 'posSide': 'short', 'pos': '1'},
                            {**position(), 'instType': 'MARGIN', 'instId': 'BTC-USDT',
                             'posCcy': 'USDT', 'pos': '100'}])
    assert len(rows) == 3
    assert rows[0].cells() == ('BTC-USDT-SWAP', '净空', '2.50 张', '82600.05',
                               '82601.02', '-0.15 USDT', '3×', '全仓')
    assert rows[1].direction == '空'
    assert rows[2].direction == '空'
    assert rows[2].quantity == '100 USDT'


@pytest.mark.parametrize('data', [None, {}, [None], [{}], [{**position(), 'pos': 'NaN'}]])
def test_invalid_positions_never_become_empty(data):
    with pytest.raises(PositionsError):
        parse_positions(data)


class FakeAccount:
    response = {'code': '0', 'data': [position()]}

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.closed = False
        self.calls = 0

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.closed = True

    def get_positions(self):
        self.calls += 1
        return self.response


def test_sdk_read_only_query_mode_and_cleanup():
    accounts = []

    def factory(**kwargs):
        account = FakeAccount(**kwargs)
        accounts.append(account)
        return account

    assert len(fetch_positions(Credentials('key', 'secret', 'phrase'), factory)) == 1
    account = accounts[0]
    assert account.kwargs['flag'] == '0'
    assert account.kwargs['debug'] is False
    assert account.calls == 1 and account.closed
    assert account.timeout.read == 3


def test_sdk_error_redacts_credentials():
    class Rejected(FakeAccount):
        response = {'code': '50113', 'msg': 'invalid example-key example-secret example-phrase'}

    with pytest.raises(PositionsError) as error:
        fetch_positions(Credentials('example-key', 'example-secret', 'example-phrase'), Rejected)
    assert '50113' in str(error.value)
    assert 'example-' not in str(error.value)


def test_network_exception_does_not_expose_request():
    def fail(**kwargs):
        raise httpx.ConnectError('example-secret')

    with pytest.raises(PositionsError, match='网络') as error:
        fetch_positions(Credentials('key', 'example-secret', 'phrase'), fail)
    assert 'example-secret' not in str(error.value)


def test_worker_manual_refresh_reloads_credentials_and_stops(qtbot, credentials_file):
    seen = []
    worker = PositionsWorker(credentials_path=credentials_file, interval=60,
                             fetcher=lambda credentials: seen.append(credentials.flag) or [])
    try:
        with qtbot.waitSignal(worker.updated, timeout=2000):
            worker.start()
        with credentials_file.open('a') as stream:
            stream.write('OKX_FLAG=1\n')
        with qtbot.waitSignal(worker.updated, timeout=2000) as result:
            worker.refresh()
        assert result.args == [[], '1']
        assert seen == ['0', '1']
    finally:
        worker.stop()
        assert worker.wait(1000)


def test_card_fields_coin_conversion_signed_short_and_precision():
    raw = {**position(), 'pos': '-400', 'imr': '3310.24', 'uplRatio': '-0.2182',
           'mgnRatio': '4.6758', 'liqPx': '84486.6'}
    metadata = {'BTC-USDT-SWAP': {'ctVal': '0.01', 'ctMult': '1',
                                'ctValCcy': 'BTC', 'ctType': 'linear', 'tickSz': '0.1'}}
    row = parse_positions([raw], metadata)[0]
    assert row.title == 'BTCUSDT 永续'
    assert row.sell
    assert row.quantity_value == '-4.00'
    assert row.quantity_currency == 'BTC'
    assert row.margin == '3310.24' and row.margin_currency == 'USDT'
    assert row.unrealized_ratio == '-0.2182'
    assert row.maintenance_ratio == '4.6758'
    assert row.liquidation == '84486.6'
    assert row.price_decimals == 1


def test_isolated_margin_and_inverse_contract_quantity():
    raw = {**position(), 'instId': 'BTC-USD-SWAP', 'pos': '2', 'markPx': '50000',
           'mgnMode': 'isolated', 'margin': '0.02', 'imr': '', 'ccy': 'BTC'}
    metadata = {'BTC-USD-SWAP': {'ctVal': '100', 'ctMult': '1',
                               'ctValCcy': 'USD', 'ctType': 'inverse'}}
    row = parse_positions([raw], metadata)[0]
    assert row.quantity_value == '0.004'
    assert row.quantity_currency == 'BTC'
    assert row.margin == '0.02' and row.margin_currency == 'BTC'
    assert row.maintenance_ratio == '--' and row.liquidation == '--'


def test_instrument_metadata_cached_across_refreshes():
    calls = []

    class Account(FakeAccount):
        def get_instruments(self, **kwargs):
            calls.append(kwargs)
            return {'code': '0', 'data': [{'instId': 'BTC-USDT-SWAP', 'ctVal': '0.01',
                                          'ctValCcy': 'BTC', 'ctMult': '1'}]}

    cache = {}
    credentials = Credentials('key', 'secret', 'phrase')
    first = fetch_positions(credentials, Account, instrument_cache=cache)
    second = fetch_positions(credentials, Account, instrument_cache=cache)
    assert len(calls) == 1
    assert first == second
    assert first[0].quantity_value == '-0.0250'
    assert first[0].quantity_currency == 'BTC'


def test_metadata_failure_keeps_explicit_contract_units():
    row = fetch_positions(Credentials('key', 'secret', 'phrase'), FakeAccount)[0]
    assert row.quantity_currency == '张'
    assert row.quantity_value == '-2.50'
