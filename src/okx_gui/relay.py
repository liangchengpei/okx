"""Session-only relay orders: confirmed full fills, never list disappearance."""
from dataclasses import dataclass, field, replace
from threading import RLock, Event
import time
import uuid

import httpx
from okx.Trade import TradeAPI
from PySide6.QtCore import QObject, QTimer, Signal

from okx_gui.positions import load_credentials
from okx_gui.trading import (TradingError, SubmissionUncertain, TradingTask, data, number,
                             options, prepare_order, submit_order)

STATE_NAMES = {'waiting':'等待前置单完全成交', 'sending':'正在校验并发送', 'sent':'已发送，等待成交',
               'filled':'已完全成交', 'canceled':'已取消', 'blocked':'前置条件失效',
               'failed':'发送失败', 'uncertain':'发送结果待核对'}
TERMINAL_FAILURES = {'canceled', 'blocked', 'failed', 'uncertain'}


@dataclass(frozen=True)
class RelayParent:
    kind: str
    identifier: str
    instrument: str
    label: str
    credentials: object = field(repr=False)


@dataclass(frozen=True)
class RelayOrder:
    identifier: str
    draft: object
    parent: RelayParent
    credentials: object = field(repr=False)
    state: str = 'waiting'
    order_id: str = ''
    message: str = ''
    created: float = field(default_factory=time.time)


def read_fill_state(credentials, instrument, order_id, trade_factory=TradeAPI):
    with trade_factory(**options(credentials)) as trade:
        trade.timeout = httpx.Timeout(3)
        rows = data(trade.get_order(instId=instrument, ordId=order_id), '读取接力前置单', credentials)
    row = next((row for row in rows if row.get('instId') == instrument and str(row.get('ordId')) == order_id), None)
    if row is None:
        raise TradingError('找不到前置单详情，继续等待，不触发接力。')
    state = row.get('state')
    if state == 'filled':
        size = number(row.get('sz'), '前置委托数量')
        filled = number(row.get('accFillSz'), '前置已成交数量')
        if size <= 0 or filled < size:
            raise TradingError('前置单成交数量尚未确认完整，继续等待。')
        return 'filled'
    if state in ('canceled', 'mmp_canceled'):
        return 'canceled'
    return 'waiting'


def read_algo_fill_state(credentials, instrument, algo_id, trade_factory=TradeAPI):
    with trade_factory(**options(credentials)) as trade:
        trade.timeout = httpx.Timeout(3)
        rows = data(trade.get_algo_order_details(algoId=algo_id), '读取计划前置单', credentials)
        row = next((row for row in rows if row.get('instId') == instrument and str(row.get('algoId')) == algo_id), None)
        if row is None:
            raise TradingError('计划委托详情待确认，不触发接力。')
        if row.get('state') in ('canceled', 'order_failed', 'partially_failed'):
            return 'canceled'
        if row.get('state') != 'effective':
            return 'waiting'
        identifiers = row.get('ordIdList')
        if not identifiers and row.get('ordId'):
            identifiers = [row['ordId']]
        if row.get('subAlgoIdList') or not isinstance(identifiers, list) or not identifiers:
            raise TradingError('计划委托已触发，但实际订单编号尚未确认，不触发接力。')
        total = 0
        for identifier in dict.fromkeys(identifiers):
            children = data(trade.get_order(instId=instrument, ordId=str(identifier)), '读取计划委托实际订单', credentials)
            child = next((c for c in children if c.get('instId') == instrument and str(c.get('ordId')) == str(identifier)), None)
            if child is None:
                raise TradingError('实际订单详情待确认，不触发接力。')
            if child.get('state') in ('canceled', 'mmp_canceled'):
                return 'canceled'
            if child.get('state') != 'filled':
                return 'waiting'
            size = number(child.get('sz'), '实际订单数量')
            filled = number(child.get('accFillSz'), '实际成交数量')
            if size <= 0 or filled < size:
                raise TradingError('实际订单尚未确认完全成交，不触发接力。')
            total += filled
        expected = number(row.get('sz') if row.get('sz') not in (None, '', '-1', '0') else row.get('actualSz'), '计划委托数量')
        if expected <= 0 or total < expected:
            raise TradingError('计划委托累计成交数量不足，不触发接力。')
        return 'filled'


class RelayBook:
    def __init__(self, *, credentials_loader=load_credentials, reader=read_fill_state,
                 preparer=prepare_order, submitter=submit_order, algo_reader=read_algo_fill_state):
        self.credentials_loader, self.reader = credentials_loader, reader
        self.preparer, self.submitter = preparer, submitter
        self.algo_reader = algo_reader
        self.lock = RLock()
        self.stopping = Event()
        self.items = {}

    def snapshot(self):
        with self.lock:
            return list(self.items.values())

    def add(self, draft, parent):
        credentials = self.credentials_loader()
        if credentials != parent.credentials:
            raise TradingError('前置单与当前账户或交易环境不一致，请重新选择。')
        if draft.order_type not in ('limit', 'market', 'trigger'):
            raise TradingError('接力触发后仅支持限价、市价或计划委托。')
        with self.lock:
            if self.stopping.is_set():
                raise TradingError('程序正在退出，不能创建接力。')
            if parent.kind == 'relay':
                source = self.items.get(parent.identifier)
                if source is None or source.state in TERMINAL_FAILURES or source.credentials != credentials:
                    raise TradingError('所选接力前置单已失效，请重新选择。')
            elif parent.kind not in ('order', 'algo') or not parent.identifier:
                raise TradingError('请选择有效的前置委托。')
            identifier = 'relay-' + uuid.uuid4().hex
            item = RelayOrder(identifier, draft, parent, credentials)
            self.items[identifier] = item
            return item

    def update(self, identifier, **changes):
        with self.lock:
            self.items[identifier] = replace(self.items[identifier], **changes)
            return self.items[identifier]

    def cancel(self, identifier):
        with self.lock:
            if self.items[identifier].state != 'waiting':
                raise TradingError('该接力已开始处理或结束，不能作为本地等待单取消。')
            self.update(identifier, state='canceled', message='用户取消本地接力，未向交易所撤单。')
            self.propagate_failures()

    def propagate_failures(self):
        with self.lock:
            for item in list(self.items.values()):
                if item.state == 'waiting' and item.parent.kind == 'relay':
                    if self.items[item.parent.identifier].state in TERMINAL_FAILURES:
                        self.update(item.identifier, state='blocked', message='前置接力未完全成交，后续接力已停止。')

    def stop(self):
        self.stopping.set()
        with self.lock:
            for item in list(self.items.values()):
                if item.state == 'waiting':
                    self.update(item.identifier, state='canceled', message='退出程序，取消未触发接力。')

    def poll(self, accepted=lambda prepared, order_id: None):
        if self.stopping.is_set():
            return
        credentials = self.credentials_loader()
        states = {}
        def read(creds, instrument, order_id, kind='order'):
            key = (creds, instrument, order_id, kind)
            if key not in states:
                states[key] = (self.algo_reader if kind == 'algo' else self.reader)(creds, instrument, order_id)
            return states[key]
        for initial in self.snapshot():
            if self.stopping.is_set():
                return
            with self.lock:
                item = self.items[initial.identifier]
            if item.state not in ('waiting', 'sent'):
                continue
            if credentials != item.credentials:
                self.update(item.identifier, state='blocked', message='账户或交易环境已更改，接力已停止。')
                continue
            try:
                if item.state == 'sent':
                    state = read(item.credentials, item.draft.instrument, item.order_id,
                                 'algo' if item.draft.order_type == 'trigger' else 'order')
                    if state in ('filled', 'canceled'):
                        self.update(item.identifier, state=state, message='')
                    continue
                if item.parent.kind == 'relay':
                    with self.lock:
                        parent = self.items[item.parent.identifier]
                    if parent.state in TERMINAL_FAILURES:
                        self.update(item.identifier, state='blocked', message='前置接力失败或取消，不触发。')
                        continue
                    ready = parent.state == 'filled'
                else:
                    state = read(item.credentials, item.parent.instrument, item.parent.identifier, item.parent.kind)
                    if state == 'canceled':
                        self.update(item.identifier, state='blocked', message='前置单已撤销，不触发接力。')
                        continue
                    ready = state == 'filled'
                if not ready:
                    self.update(item.identifier, message='')
                    continue
            except Exception as exc:
                self.update(item.identifier, message=item.credentials.redact(str(exc)))
                continue  # Retrying a GET is safe; never infer a fill from a failed query.
            with self.lock:
                if self.stopping.is_set() or self.items[item.identifier].state != 'waiting':
                    continue
                self.update(item.identifier, state='sending', message='')
            try:
                prepared = self.preparer(item.draft, credentials=item.credentials)
                if self.stopping.is_set():
                    self.update(item.identifier, state='canceled', message='退出程序，未发送接力订单。')
                    return
                order_id = self.submitter(prepared)
                self.update(item.identifier, state='sent', order_id=order_id)
                accepted(prepared, order_id)
            except SubmissionUncertain as exc:
                self.update(item.identifier, state='uncertain', message=str(exc))
            except Exception as exc:
                self.update(item.identifier, state='failed', message=item.credentials.redact(str(exc)))
        self.propagate_failures()


class RelayController(QObject):
    changed = Signal()
    accepted = Signal(object, str)
    busy_changed = Signal(bool)

    def __init__(self, parent=None, *, book=None, busy=lambda: False):
        super().__init__(parent)
        self.book = book or RelayBook()
        self.busy = busy
        self.task = None
        self.timer = QTimer(self)
        self.timer.setInterval(1000)
        self.timer.timeout.connect(self.tick)
        self.timer.start()

    def tick(self):
        if self.task is not None or self.busy() or self.book.stopping.is_set():
            return
        if not any(item.state in ('waiting', 'sent') for item in self.book.snapshot()):
            return
        before = self.book.snapshot()
        self.task = TradingTask(lambda: self.book.poll(self.accepted.emit), self)
        task = self.task
        self.busy_changed.emit(True)
        def failed(message, uncertain):
            for item in self.book.snapshot():
                if item.state in ('waiting', 'sent'):
                    self.book.update(item.identifier, message=message)
        task.failed.connect(failed)
        def finished():
            self.task = None
            self.busy_changed.emit(False)
            if before != self.book.snapshot():
                self.changed.emit()
            task.deleteLater()
        task.finished.connect(finished)
        task.start()

    def shutdown(self):
        self.timer.stop()
        self.book.stop()
        return self.task is None or not self.task.isRunning()
