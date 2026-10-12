from dataclasses import replace
from decimal import Decimal
from types import SimpleNamespace

import pytest

from okx_gui.order_entry import OrderDraft
from okx_gui.positions import Credentials
from okx_gui.relay import RelayBook, RelayParent, read_fill_state
from okx_gui.trading import TradingError, SubmissionUncertain

CREDS = Credentials('test-key','test-secret','test-pass','1')
DRAFT = OrderDraft('BTC-USDT-SWAP','open','long','cross',10,'limit',Decimal('0.1'),Decimal('82000'))
PARENT = RelayParent('order','parent-order','BTC-USDT-SWAP','BTC 前置单',CREDS)


def harness():
    states, reads, sends = {}, [], []
    def reader(credentials, instrument, identifier):
        reads.append(identifier)
        value = states.get(identifier, 'waiting')
        if isinstance(value, Exception):
            raise value
        return value
    def prepare(draft, credentials):
        return SimpleNamespace(draft=draft, credentials=credentials)
    def submit(prepared):
        sends.append(prepared)
        return 'child-' + str(len(sends))
    book = RelayBook(credentials_loader=lambda: CREDS, reader=reader, preparer=prepare, submitter=submit)
    return book, states, reads, sends


def test_creation_and_partial_fill_do_not_send_then_full_fill_sends_once():
    book, states, reads, sends = harness()
    item = book.add(DRAFT, PARENT)
    assert not reads and not sends
    book.poll()
    assert book.items[item.identifier].state == 'waiting' and not sends
    states[PARENT.identifier] = 'filled'
    accepted = []
    book.poll(lambda prepared, identifier: accepted.append(identifier))
    book.poll()
    assert len(sends) == 1 and accepted == ['child-1']
    assert book.items[item.identifier].state == 'sent'


def test_three_stage_chain_waits_for_each_full_fill():
    book, states, reads, sends = harness()
    b = book.add(DRAFT, PARENT)
    c = book.add(replace(DRAFT, direction='short'), RelayParent('relay',b.identifier,DRAFT.instrument,'B',CREDS))
    states[PARENT.identifier] = 'filled'
    book.poll()
    assert len(sends) == 1 and book.items[c.identifier].state == 'waiting'
    book.poll()
    assert len(sends) == 1
    states['child-1'] = 'filled'
    book.poll()
    assert len(sends) == 2
    assert sends[-1].draft.direction == 'short'
    book.poll()
    assert len(sends) == 2


@pytest.mark.parametrize('state', ['canceled', TradingError('not found'), TimeoutError('timeout')])
def test_canceled_or_unknown_parent_never_triggers(state):
    book, states, reads, sends = harness()
    b = book.add(DRAFT, PARENT)
    c = book.add(DRAFT, RelayParent('relay',b.identifier,DRAFT.instrument,'B',CREDS))
    states[PARENT.identifier] = state
    book.poll()
    assert not sends
    assert book.items[b.identifier].state == ('blocked' if state == 'canceled' else 'waiting')
    if state == 'canceled':
        assert book.items[c.identifier].state == 'blocked'


def test_local_cancellation_stops_descendants_without_exchange_calls():
    book, states, reads, sends = harness()
    b = book.add(DRAFT, PARENT)
    c = book.add(DRAFT, RelayParent('relay',b.identifier,DRAFT.instrument,'B',CREDS))
    book.cancel(b.identifier)
    states[PARENT.identifier] = 'filled'
    book.poll()
    assert not sends and not reads
    assert book.items[c.identifier].state == 'blocked'


@pytest.mark.parametrize('error', [TradingError('insufficient margin'), SubmissionUncertain('unknown result')])
def test_failed_send_never_retries_and_stops_chain(error):
    book, states, reads, sends = harness()
    b = book.add(DRAFT, PARENT)
    c = book.add(DRAFT, RelayParent('relay',b.identifier,DRAFT.instrument,'B',CREDS))
    def rejected(prepared):
        sends.append(prepared)
        raise error
    book.submitter = rejected
    states[PARENT.identifier] = 'filled'
    book.poll()
    book.poll()
    assert len(sends) == 1
    assert book.items[b.identifier].state == ('uncertain' if isinstance(error, SubmissionUncertain) else 'failed')
    assert book.items[c.identifier].state == 'blocked'


def test_credential_change_stops_automatic_submission():
    book, states, reads, sends = harness()
    b = book.add(DRAFT, PARENT)
    book.credentials_loader = lambda: replace(CREDS, flag='0')
    states[PARENT.identifier] = 'filled'
    book.poll()
    assert book.items[b.identifier].state == 'blocked'
    assert not sends and not reads
    with pytest.raises(TradingError):
        book.add(DRAFT, PARENT)


def test_stop_cancels_waiting_only_and_does_not_cancel_sent_order():
    book, states, reads, sends = harness()
    b = book.add(DRAFT, PARENT)
    c = book.add(DRAFT, RelayParent('relay',b.identifier,DRAFT.instrument,'B',CREDS))
    states[PARENT.identifier] = 'filled'
    book.poll()
    book.stop()
    book.poll()
    assert len(sends) == 1
    assert book.items[b.identifier].state == 'sent'
    assert book.items[c.identifier].state == 'canceled'
    assert RelayBook().snapshot() == []  # A new session has no saved relays.


def test_stop_during_validation_prevents_post():
    book, states, reads, sends = harness()
    b = book.add(DRAFT, PARENT)
    def prepare(draft, credentials):
        book.stop()
        return SimpleNamespace(draft=draft, credentials=credentials)
    book.preparer = prepare
    states[PARENT.identifier] = 'filled'
    book.poll()
    assert not sends and book.items[b.identifier].state == 'canceled'


def test_missing_or_invalid_relay_parent_rejected():
    book, _, _, _ = harness()
    with pytest.raises(TradingError):
        book.add(DRAFT, RelayParent('relay','unknown',DRAFT.instrument,'unknown',CREDS))


@pytest.mark.parametrize('state,filled,expected', [('live','0','waiting'), ('partially_filled','9','waiting'),
                                                ('filled','10','filled'), ('canceled','9','canceled')])
def test_fill_reader_requires_detail_state(state, filled, expected):
    class Trade:
        def __init__(self, **kwargs):
            assert kwargs['flag'] == '1'
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def get_order(self, **kwargs):
            assert kwargs == {'instId':DRAFT.instrument,'ordId':'parent-order'}
            return {'code':'0','data':[{'instId':DRAFT.instrument,'ordId':'parent-order','state':state,'sz':'10','accFillSz':filled}]}
    assert read_fill_state(CREDS, DRAFT.instrument, 'parent-order', Trade) == expected


def test_incomplete_fill_details_never_trigger():
    class Trade:
        def __init__(self, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def get_order(self, **kwargs):
            return {'code':'0','data':[{'instId':DRAFT.instrument,'ordId':'parent-order','state':'filled','sz':'10','accFillSz':'9'}]}
    with pytest.raises(TradingError):
        read_fill_state(CREDS, DRAFT.instrument, 'parent-order', Trade)


def test_trigger_revalidates_target_close_order_instead_of_at_registration():
    book, states, _, sends = harness()
    checks = []
    def prepare(draft, credentials):
        checks.append(draft)
        raise TradingError('可平数量不足')
    book.preparer = prepare
    b = book.add(replace(DRAFT, action='close'), PARENT)
    assert not checks
    states[PARENT.identifier] = 'filled'
    book.poll()
    assert len(checks) == 1 and not sends
    assert book.items[b.identifier].state == 'failed'


def test_controller_delivers_accepted_order_and_prevents_overlapping_polls(qtbot):
    from okx_gui.relay import RelayController
    book, states, reads, sends = harness()
    item = book.add(DRAFT, PARENT)
    states[PARENT.identifier] = 'filled'
    controller = RelayController(book=book)
    controller.timer.stop()
    accepted = []
    controller.accepted.connect(lambda prepared, identifier: accepted.append(identifier))
    with qtbot.waitSignal(controller.changed):
        controller.tick()
        controller.tick()
    qtbot.waitUntil(lambda: controller.task is None)
    assert accepted == ['child-1'] and len(sends) == 1
    assert book.items[item.identifier].state == 'sent'
    assert controller.shutdown()
    controller.deleteLater()


@pytest.mark.parametrize('algo_state,child_state,expected', [
    ('live', 'filled', 'waiting'),
    ('effective', 'live', 'waiting'),
    ('effective', 'partially_filled', 'waiting'),
    ('effective', 'filled', 'filled'),
    ('effective', 'canceled', 'canceled'),
    ('canceled', 'filled', 'canceled'),
])
def test_plan_parent_waits_for_actual_order_full_fill(algo_state, child_state, expected):
    from okx_gui.relay import read_algo_fill_state
    class Trade:
        def __init__(self, **kwargs): assert kwargs['flag'] == '1'
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def get_algo_order_details(self, **kwargs):
            assert kwargs == {'algoId': 'plan'}
            return {'code': '0', 'data': [dict(instId=DRAFT.instrument, algoId='plan',
                    state=algo_state, ordIdList=['actual'], sz='10')]}
        def get_order(self, **kwargs):
            assert algo_state == 'effective'
            assert kwargs == dict(instId=DRAFT.instrument, ordId='actual')
            return {'code': '0', 'data': [dict(instId=DRAFT.instrument, ordId='actual',
                    state=child_state, sz='10', accFillSz='10' if child_state == 'filled' else '2')]}
    assert read_algo_fill_state(CREDS, DRAFT.instrument, 'plan', Trade) == expected


@pytest.mark.parametrize('identifiers,expected_size,fill,error', [
    ([], '10', '5', True),
    (['one', 'two'], '10', '5', False),
    (['one', 'two'], '11', '5', True),
    (['one', 'two'], '10', '4', True),
])
def test_plan_split_orders_require_complete_total(identifiers, expected_size, fill, error):
    from okx_gui.relay import read_algo_fill_state
    class Trade:
        def __init__(self, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def get_algo_order_details(self, **kwargs):
            return {'code': '0', 'data': [dict(instId=DRAFT.instrument, algoId='plan',
                    state='effective', ordIdList=identifiers, sz=expected_size)]}
        def get_order(self, **kwargs):
            return {'code': '0', 'data': [dict(instId=DRAFT.instrument, ordId=kwargs['ordId'],
                    state='filled', sz='5', accFillSz=fill)]}
    if error:
        with pytest.raises(TradingError):
            read_algo_fill_state(CREDS, DRAFT.instrument, 'plan', Trade)
    else:
        assert read_algo_fill_state(CREDS, DRAFT.instrument, 'plan', Trade) == 'filled'


def test_plan_parent_uses_algo_reader_and_sends_once_after_full_fill():
    book, states, reads, sends = harness()
    algo_states = ['waiting', 'filled']
    calls = []
    def read_algo(*args):
        calls.append(args)
        return algo_states.pop(0)
    book.algo_reader = read_algo
    item = book.add(DRAFT, replace(PARENT, kind='algo'))
    book.poll()
    assert not sends and not reads
    book.poll()
    book.poll()
    assert len(sends) == 1 and len(calls) == 2
    assert book.items[item.identifier].state == 'sent'


def test_plan_relay_submits_after_parent_fill_and_chain_waits_for_plan_fill():
    book, states, reads, sends = harness()
    draft = replace(DRAFT, order_type='trigger', trigger_price=Decimal('82100'), trigger_source='mark')
    plan = book.add(draft, PARENT)
    child = book.add(DRAFT, RelayParent('relay', plan.identifier, DRAFT.instrument, '计划接力', CREDS))
    algo_state = ['waiting']
    algo_reads = []
    def read_algo(credentials, instrument, identifier):
        algo_reads.append(identifier)
        return algo_state[0]
    book.algo_reader = read_algo
    book.poll()
    assert not sends
    states[PARENT.identifier] = 'filled'
    book.poll()
    assert len(sends) == 1 and sends[0].draft == draft
    book.poll()
    assert algo_reads == ['child-1']
    assert len(sends) == 1 and book.items[child.identifier].state == 'waiting'
    algo_state[0] = 'filled'
    book.poll()
    book.poll()
    assert len(sends) == 2
    assert book.items[child.identifier].state == 'sent'
