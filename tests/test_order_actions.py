from dataclasses import replace
from decimal import Decimal

import httpx
import pytest

from okx_gui.orders import parse_orders
from okx_gui.positions import Credentials
from okx_gui.order_actions import perform_order_action
from okx_gui.order_action_dialog import OrderActionDialog
from okx_gui.order_widgets import OrderCard
from okx_gui.trading import TradingError, SubmissionUncertain

CREDS = Credentials('test-key', 'test-secret', 'test-pass', '1')
RULE = dict(instId='BTC-USDT-SWAP', ctVal='0.01', ctMult='1', ctValCcy='BTC',
            tickSz='0.1', lotSz='1', minSz='1', maxLmtSz='10000', maxTriggerSz='10000')


def order(algo=False, **changes):
    raw = dict(instId=RULE['instId'], instType='SWAP', side='buy', tdMode='cross', sz='100',
               accFillSz='20', px='82600', ordPx='82700', triggerPx='82701', triggerPxType='last',
               state='live', ordType='trigger' if algo else 'limit', _algo=algo)
    raw['algoId' if algo else 'ordId'] = '123'
    raw.update(changes)
    return replace(parse_orders([raw], {RULE['instId']:RULE})[0], credentials=CREDS)


class Account:
    def __init__(self, **kwargs):
        pass
    def __enter__(self):
        return self
    def __exit__(self, *args):
        pass
    def get_instruments(self, **kwargs):
        return {'code':'0', 'data':[RULE]}


class Trade(Account):
    fresh = None
    calls = []
    response = None
    timeout_post = False
    def get_order(self, **kwargs):
        return {'code':'0', 'data':[self.fresh]}
    get_algo_order_details = get_order
    def send(self, name, payload):
        self.calls.append((name, payload))
        if self.timeout_post:
            raise httpx.ReadTimeout('timeout')
        return self.response
    def cancel_order(self, **kwargs):
        return self.send('cancel', kwargs)
    def cancel_algo_order(self, payload):
        return self.send('cancel_algo', payload)
    def amend_order(self, **kwargs):
        return self.send('amend', kwargs)
    def amend_algo_order(self, **kwargs):
        return self.send('amend_algo', kwargs)


@pytest.fixture(autouse=True)
def reset():
    Trade.calls = []
    Trade.timeout_post = False


def setup_order(algo=False, **changes):
    item = order(algo, **changes)
    Trade.fresh = dict(item.raw)
    Trade.response = {'code':'0', 'data':[{'sCode':'0', 'algoId' if algo else 'ordId':'123'}]}
    return item


def run(item, action, changes=None, **kwargs):
    return perform_order_action(item, action, changes, trade_factory=Trade,
                                account_factory=Account, credentials_loader=lambda: CREDS, **kwargs)


@pytest.mark.parametrize('algo', [False, True])
def test_cancel_routes_to_correct_order_api(algo):
    item = setup_order(algo)
    assert '已受理' in run(item, 'cancel')
    payload = {'instId':item.instrument, 'algoId' if algo else 'ordId':'123'}
    assert Trade.calls == [('cancel_algo' if algo else 'cancel', [payload] if algo else payload)]


@pytest.mark.parametrize('algo', [False, True])
def test_amend_converts_coins_and_never_cancels_on_failure(algo):
    item = setup_order(algo)
    changes = {'newSz':'0.5', 'newTriggerPx':'83000', 'newOrdPx':'-1'} if algo else {'newSz':'0.5','newPx':'83000'}
    assert '已受理' in run(item, 'amend', changes)
    name, payload = Trade.calls[0]
    assert name == ('amend_algo' if algo else 'amend')
    assert payload['newSz'] == '50'
    assert payload['cxlOnFail'] is False
    assert payload['algoId' if algo else 'ordId'] == '123'
    if algo:
        assert payload['newOrdPx'] == '-1'


@pytest.mark.parametrize('changes', [{'newSz':'0.005'}, {'newSz':'0.2'}, {'newSz':'NaN'},
                                     {'newPx':'83000.05'}, {'newPx':'-1'}, {'side':'sell'}, {}])
def test_invalid_amendment_never_sends(changes):
    with pytest.raises(TradingError):
        run(setup_order(), 'amend', changes)
    assert not Trade.calls


def test_oco_modification_uses_algo_fields():
    item = setup_order(True, ordType='oco', tpTriggerPx='85000', tpOrdPx='-1', slTriggerPx='80000', slOrdPx='-1')
    run(item, 'amend', {'newSlTriggerPx':'81000'})
    assert Trade.calls[0][1]['newSlTriggerPx'] == '81000'
    assert 'newTpTriggerPx' not in Trade.calls[0][1]


@pytest.mark.parametrize('action', ['cancel','amend'])
def test_completed_order_rejected_before_send(action):
    item = setup_order()
    Trade.fresh['state'] = 'filled'
    with pytest.raises(TradingError, match='已成交'):
        run(item, action, {'newPx':'83000'})
    assert not Trade.calls


def test_external_amendment_requires_reopening_editor():
    item = setup_order()
    Trade.fresh['px'] = '82500'
    with pytest.raises(TradingError, match='内容已改变'):
        run(item, 'amend', {'newPx':'83000'})
    assert not Trade.calls


def test_changed_account_cannot_act_on_old_order():
    item = replace(setup_order(), credentials=replace(CREDS, flag='0'))
    with pytest.raises(TradingError, match='交易环境'):
        run(item, 'cancel')
    assert not Trade.calls


def test_timeout_never_retries():
    item = setup_order()
    Trade.timeout_post = True
    with pytest.raises(SubmissionUncertain):
        run(item, 'cancel')
    assert len(Trade.calls) == 1


def test_exchange_rejection_redacts_secret():
    item = setup_order()
    Trade.response = {'code':'1', 'data':[{'sCode':'51000','sMsg':'test-secret test-pass'}]}
    with pytest.raises(TradingError) as exc:
        run(item, 'cancel')
    assert 'test-secret' not in str(exc.value) and 'test-pass' not in str(exc.value)


def test_dialog_and_card_do_not_submit_until_confirmed(qtbot, monkeypatch):
    item = setup_order()
    calls = []
    monkeypatch.setattr('okx_gui.order_action_dialog.perform_order_action', lambda *args: calls.append(args) or '已受理')
    card = OrderCard(item)
    qtbot.addWidget(card)
    with qtbot.waitSignal(card.action_requested) as signal:
        card.modify.click()
    assert signal.args == [item, 'amend']
    dialog = OrderActionDialog(item, 'amend', card)
    qtbot.addWidget(dialog)
    dialog.show()
    assert dialog.inputs['newSz'][0].text() == '1.00'
    assert not calls
    dialog.inputs['newPx'][0].setText('83000')
    with qtbot.waitSignal(dialog.accepted):
        dialog.confirm.click()
    assert len(calls) == 1 and calls[0][2] == {'newPx':'83000'}


def test_uncertain_dialog_requires_explicit_acknowledgement(qtbot):
    dialog = OrderActionDialog(setup_order(), 'cancel')
    qtbot.addWidget(dialog)
    dialog.show()
    dialog.failed('请核对', True)
    dialog.reject()
    assert dialog.isVisible()
    dialog.acknowledge.click()
    assert not dialog.isVisible()


@pytest.mark.parametrize('response', [{}, {'code':'0', 'data':[]},
                                      {'code':'0', 'data':[{'sCode':'50004'}]},
                                      {'code':'0', 'data':[{'sCode':'0', 'ordId':'wrong'}]}])
def test_ambiguous_response_is_not_safe_to_retry(response):
    item = setup_order()
    Trade.response = response
    with pytest.raises(SubmissionUncertain):
        run(item, 'cancel')
    assert len(Trade.calls) == 1


def test_failed_amendment_keeps_editor_open(qtbot, monkeypatch):
    def fail(*args):
        raise TradingError('修改被拒绝')
    monkeypatch.setattr('okx_gui.order_action_dialog.perform_order_action', fail)
    card = OrderCard(setup_order())
    qtbot.addWidget(card)
    dialog = OrderActionDialog(card.order, 'amend', card)
    qtbot.addWidget(dialog)
    dialog.show()
    dialog.inputs['newPx'][0].setText('83000')
    with qtbot.waitSignal(dialog.updated):
        dialog.confirm.click()
    qtbot.waitUntil(lambda: not dialog.running)
    assert dialog.isVisible() and dialog.confirm.isEnabled()
    assert dialog.message.text() == '修改被拒绝'
    assert dialog.inputs['newPx'][0].text() == '83000'
    dialog.reject()
