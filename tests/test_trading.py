from dataclasses import replace
from decimal import Decimal

import httpx
import pytest

from okx_gui.order_entry import OrderDraft
from okx_gui.positions import Credentials
from okx_gui.trading import (PreparedOrder, SubmissionUncertain, TradingError,
                             order_parameters, prepare_order, submit_order)

CREDS = Credentials('test-key', 'test-secret', 'test-phrase', '1')
DRAFT = OrderDraft('BTC-USDT-SWAP', 'open', 'long', 'cross', 10, 'limit', Decimal('1'), Decimal('82600.1'))


def ok(rows):
    return {'code': '0', 'data': rows}


class Account:
    mode = 'long_short_mode'
    positions = []
    calls = []
    def __init__(self, **kwargs):
        assert kwargs['flag'] == '1'
    def __enter__(self):
        return self
    def __exit__(self, *args):
        pass
    def get_account_config(self):
        return ok([{'acctLv': '2', 'posMode': self.mode}])
    def get_instruments(self, **kwargs):
        return ok([{'instId': DRAFT.instrument, 'state': 'live', 'lotSz': '1', 'minSz': '1',
                    'tickSz': '0.1', 'lever': '100', 'maxLmtSz': '100', 'maxMktSz': '50'}])
    def get_positions(self, **kwargs):
        return ok(self.positions)
    def set_leverage(self, **kwargs):
        self.calls.append(kwargs)
        return ok([{}])


class Trade:
    calls = []
    timeout_post = False
    response = ok([{'sCode': '0', 'ordId': '123'}])
    lookup = ok([{'ordId': '123'}])
    def __init__(self, **kwargs):
        assert kwargs['flag'] == '1'
    def __enter__(self):
        return self
    def __exit__(self, *args):
        pass
    def place_order(self, **kwargs):
        self.calls.append(kwargs)
        if self.timeout_post:
            raise httpx.ReadTimeout('timeout')
        return self.response
    def get_order(self, **kwargs):
        return self.lookup


@pytest.fixture(autouse=True)
def reset():
    Account.mode = 'long_short_mode'
    Account.positions = []
    Account.calls = []
    Trade.calls = []
    Trade.timeout_post = False
    Trade.response = ok([{'sCode': '0', 'ordId': '123'}])
    Trade.lookup = ok([{'ordId': '123'}])


def submit(prepared):
    return submit_order(prepared, Account, Trade, lambda: CREDS)


@pytest.mark.parametrize('mode', ['net_mode', 'long_short_mode'])
@pytest.mark.parametrize('action,direction,side', [('open','long','buy'), ('open','short','sell'),
                                                 ('close','long','sell'), ('close','short','buy')])
def test_order_side_mapping_and_reduce_only(mode, action, direction, side):
    draft = replace(DRAFT, action=action, direction=direction)
    params = order_parameters(PreparedOrder(draft, CREDS, mode, 'gui123'))
    assert params['side'] == side
    assert params['posSide'] == ('net' if mode == 'net_mode' else direction)
    assert params.get('reduceOnly', False) == (mode == 'net_mode' and action == 'close')


def test_preparation_is_read_only_then_explicit_submission_sets_leverage():
    prepared = prepare_order(DRAFT, CREDS, Account)
    assert not Trade.calls and not Account.calls
    assert submit(prepared) == '123'
    assert len(Trade.calls) == 1
    assert Account.calls == [{'instId': DRAFT.instrument, 'lever': '10', 'mgnMode': 'cross'}]
    assert Trade.calls[0]['sz'] == '1'
    assert len(prepared.client_id) == 32


@pytest.mark.parametrize('changes', [{'quantity':Decimal('0.5')}, {'quantity':Decimal('101')},
                                    {'price':Decimal('82600.15')}, {'leverage':101}])
def test_invalid_exchange_rules_block_submission(changes):
    with pytest.raises(TradingError):
        prepare_order(replace(DRAFT, **changes), CREDS, Account)
    assert not Trade.calls and not Account.calls


def test_close_checks_available_position_and_does_not_set_leverage():
    draft = replace(DRAFT, action='close', order_type='market', price=None)
    with pytest.raises(TradingError, match='可平数量不足'):
        prepare_order(draft, CREDS, Account)
    Account.positions = [{'instId':DRAFT.instrument, 'mgnMode':'cross', 'posSide':'long', 'pos':'3','availPos':'2'}]
    prepared = prepare_order(draft, CREDS, Account)
    submit(prepared)
    assert not Account.calls
    assert 'px' not in Trade.calls[0]


def test_net_mode_blocks_opening_against_existing_position():
    Account.mode = 'net_mode'
    Account.positions = [{'instId':DRAFT.instrument,'mgnMode':'cross','posSide':'net','pos':'-2'}]
    with pytest.raises(TradingError, match='反向持仓'):
        prepare_order(DRAFT, CREDS, Account)


def test_changed_environment_blocks_before_mutations():
    prepared = prepare_order(DRAFT, CREDS, Account)
    with pytest.raises(TradingError, match='交易环境'):
        submit_order(prepared, Account, Trade, lambda: replace(CREDS, flag='0'))
    assert not Trade.calls and not Account.calls


def test_timeout_is_reconciled_without_resending():
    Trade.timeout_post = True
    assert submit(prepare_order(DRAFT, CREDS, Account)) == '123'
    assert len(Trade.calls) == 1


def test_unconfirmed_timeout_requires_manual_reconciliation():
    Trade.timeout_post = True
    Trade.lookup = {'code':'51603', 'msg':'not found'}
    with pytest.raises(SubmissionUncertain, match='请勿重复下单'):
        submit(prepare_order(DRAFT, CREDS, Account))
    assert len(Trade.calls) == 1


def test_per_order_rejection_is_not_reported_as_success():
    Trade.response = {'code':'1','data':[{'sCode':'51008','sMsg':'insufficient balance'}]}
    with pytest.raises(TradingError, match='51008'):
        submit(prepare_order(DRAFT, CREDS, Account))


def test_server_processing_timeout_is_uncertain():
    Trade.response = {'code':'50004', 'msg':'timeout', 'data':[]}
    with pytest.raises(SubmissionUncertain):
        submit(prepare_order(DRAFT, CREDS, Account))
    assert len(Trade.calls) == 1


def test_failed_leverage_never_sends_order(monkeypatch):
    prepared = prepare_order(DRAFT, CREDS, Account)
    monkeypatch.setattr(Account, 'set_leverage', lambda self, **kwargs: {'code':'59102','msg':'invalid leverage'})
    with pytest.raises(TradingError, match='设置杠杆'):
        submit(prepared)
    assert not Trade.calls


def test_expired_confirmation_never_changes_account_or_sends():
    prepared = prepare_order(DRAFT, CREDS, Account)
    with pytest.raises(TradingError, match='60秒'):
        submit(replace(prepared, prepared_at=prepared.prepared_at - 61))
    assert not Account.calls and not Trade.calls


def test_malformed_success_response_is_uncertain():
    Trade.response = {'code':'0', 'data':{'unexpected':'shape'}}
    with pytest.raises(SubmissionUncertain):
        submit(prepare_order(DRAFT, CREDS, Account))
    assert len(Trade.calls) == 1


def test_account_and_trade_use_same_configured_domain():
    from okx_gui.trading import options
    assert options(CREDS)['domain'] == 'https://openapi.okx.com'
    assert options(replace(CREDS, domain='https://eea.okx.com'))['domain'] == 'https://eea.okx.com'


def test_crypto_permission_rejection_explains_environment_and_domain():
    Trade.response = {'code':'50123','msg':'This API Key does not have trading permission for the Crypto.'}
    with pytest.raises(TradingError) as error:
        submit(prepare_order(DRAFT, CREDS, Account))
    assert '50123' in str(error.value)
    assert '模拟盘' in str(error.value)
    assert CREDS.domain in str(error.value)
    assert 'test-secret' not in str(error.value)
    assert len(Trade.calls) == 1


def test_leverage_rules_read_contract_ceiling_and_existing_setting():
    from okx_gui.trading import fetch_leverage_options
    class WithLeverage(Account):
        def get_leverage(self, **kwargs):
            return ok([{'instId':DRAFT.instrument, 'mgnMode':'cross', 'lever':'25'}])
    maximum, current = fetch_leverage_options(DRAFT.instrument, 'cross', CREDS, WithLeverage)
    assert maximum == Decimal('100') and current == Decimal('25')
    assert not Account.calls and not Trade.calls
