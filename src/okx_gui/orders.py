"""Read-only regular and algo pending orders, with complete pagination."""
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
import time
from zoneinfo import ZoneInfo

import httpx
from okx.Account import AccountAPI
from okx.Trade import TradeAPI

from okx_gui.positions import PositionsError, PositionsWorker
from okx_gui.position_widgets import number

ALGO_TYPES = ('trigger', 'conditional,oco', 'move_order_stop', 'iceberg', 'twap', 'smart_iceberg', 'chase')
TYPE_NAMES = {'trigger': '计划委托', 'conditional': '止盈止损', 'oco': '双向止盈止损',
              'move_order_stop': '移动止盈止损', 'iceberg': '冰山委托', 'smart_iceberg': '智能冰山',
              'twap': '时间加权委托', 'chase': '追价委托', 'limit': '限价委托', 'market': '市价委托',
              'post_only': '只挂单', 'fok': '全部成交或取消', 'ioc': '立即成交或取消',
              'optimal_limit_ioc': '市价委托'}


class OrdersError(PositionsError):
    pass


@dataclass(frozen=True)
class Order:
    order_id: str
    instrument: str
    kind: str
    side: str
    margin_mode: str
    leverage: str
    created: str
    timestamp: int
    fields: tuple
    state: str

    @property
    def title(self):
        parts = self.instrument.split('-')
        return ''.join(parts[:2]) + (' 永续' if parts[-1] == 'SWAP' else ' ' + '-'.join(parts[2:])).rstrip()


def price(value):
    return '市价' if str(value) == '-1' else number(value)


def trigger(value, source):
    label = {'last': '最新', 'mark': '标记', 'index': '指数'}.get(source, '')
    return f'{label} {number(value)}'.strip()


def quantity(item, metadata, key='sz'):
    value = item.get(key, '')
    currency = '张' if item.get('instType') in ('SWAP', 'FUTURES', 'OPTION') else (
        item.get('instId', '').split('-')[1 if item.get('tgtCcy', 'quote_ccy' if item.get('ordType') == 'market' and item.get('side') == 'buy' else 'base_ccy') == 'quote_ccy' else 0])
    try:
        amount = Decimal(value)
        if not amount.is_finite() or amount < 0:
            raise ValueError
        if currency == '张' and metadata.get('ctValCcy') == item['instId'].split('-')[0]:
            face = Decimal(metadata['ctVal']) * Decimal(metadata.get('ctMult') or '1')
            if face.is_finite() and face > 0:
                amount *= face
                currency = metadata['ctValCcy']
        return number(str(amount)), currency
    except (InvalidOperation, KeyError, ValueError, TypeError):
        return '--', currency


def parse_orders(data, instruments=None):
    instruments = instruments or {}
    orders = []
    for item in data:
        if not isinstance(item, dict) or not isinstance(item.get('instId'), str) or not item['instId'] or item.get('side') not in ('buy', 'sell'):
            raise OrdersError('委托响应包含无效数据，请等待自动重试。')
        identifier = item.get('algoId') if item.get('_algo') else item.get('ordId')
        if not identifier:
            raise OrdersError('委托响应缺少委托编号。')
        try:
            timestamp = int(item.get('cTime') or 0)
            created = datetime.fromtimestamp(timestamp / 1000, ZoneInfo('Asia/Shanghai')).strftime('%m/%d %H:%M:%S') if timestamp else '--'
        except (ValueError, OverflowError, OSError, TypeError):
            timestamp, created = 0, '--'
        metadata = instruments.get(item['instId'], {})
        size, currency = quantity(item, metadata)
        if item.get('closeFraction') and item.get('sz') in ('', None, '-1'):
            size, currency = number(item['closeFraction'], decimals=2, percentage=True), '仓位'
        kind = item.get('ordType', '')
        fields = []
        if kind == 'trigger':
            fields.extend([('触发价格', trigger(item.get('triggerPx'), item.get('triggerPxType')), str(item.get('triggerPx', ''))),
                           ('委托价格', price(item.get('ordPx')), str(item.get('ordPx', '')))])
        elif kind in ('conditional', 'oco'):
            for prefix, label in (('tp', '止盈'), ('sl', '止损')):
                if item.get(prefix + 'TriggerPx'):
                    fields.extend([(label + '触发', trigger(item[prefix + 'TriggerPx'], item.get(prefix + 'TriggerPxType')), item[prefix + 'TriggerPx']),
                                   (label + '委托价', price(item.get(prefix + 'OrdPx')), str(item.get(prefix + 'OrdPx', '')))])
        elif kind == 'move_order_stop':
            fields.append(('激活价格', price(item.get('activePx')), str(item.get('activePx', ''))))
            if item.get('callbackRatio'):
                fields.append(('回调幅度', number(item['callbackRatio'], percentage=True), item['callbackRatio']))
            elif item.get('callbackSpread'):
                fields.append(('回调价差', number(item['callbackSpread']), item['callbackSpread']))
        else:
            fields.append(('委托价格', ('市价' if kind in ('market', 'optimal_limit_ioc') else price(item.get('px') or item.get('ordPx') or item.get('pxLimit'))), str(item.get('px') or item.get('ordPx') or item.get('pxLimit') or '')))
        fields.append((f'委托数量 ({currency})', size, str(item.get('sz') or item.get('closeFraction') or '')))
        if not item.get('_algo'):
            filled, unit = quantity(item, metadata, 'accFillSz')
            fields.append((f'已成交 ({unit})', filled, str(item.get('accFillSz') or '')))
        for key, label in (('pxLimit', '价格限制'), ('szLimit', '单笔数量（张）'), ('timeInterval', '时间间隔（秒）'), ('chaseVal', '追价幅度')):
            if item.get(key):
                fields.append((label, number(item[key]), str(item[key])))
        orders.append(Order(str(identifier), item['instId'], TYPE_NAMES.get(kind, kind or '委托'),
                            '买入' if item['side'] == 'buy' else '卖出',
                            {'cross': '全仓', 'isolated': '逐仓', 'cash': '现货'}.get(item.get('tdMode'), '--'),
                            number(item.get('lever')) + 'x' if item.get('lever') else '--', created, timestamp, tuple(fields),
                            {'live': '待成交', 'partially_filled': '部分成交', 'pause': '已暂停'}.get(item.get('state'), item.get('state') or '--')))
    return sorted(orders, key=lambda order: (order.timestamp, order.order_id), reverse=True)


def fetch_orders(credentials, trade_factory=TradeAPI, account_factory=AccountAPI, *, instrument_cache=None, should_stop=lambda: False):
    rows, seen = [], set()
    instruments = instrument_cache if instrument_cache is not None else {}
    next_request = 0.0

    def check_stop():
        if should_stop():
            raise OrdersError('委托查询已停止。')

    def pages(method, id_key, **kwargs):
        nonlocal next_request
        after, cursors = '', set()
        while True:
            check_stop()
            time.sleep(max(0, next_request - time.monotonic()))
            check_stop()
            next_request = time.monotonic() + 0.12
            response = method(limit='100', after=after, **kwargs)
            if not isinstance(response, dict) or str(response.get('code')) != '0':
                message = credentials.redact(f"委托查询失败（{response.get('code', '--')}）：{response.get('msg', '响应异常')}") if isinstance(response, dict) else '委托响应格式异常。'
                raise OrdersError(message[:240])
            data = response.get('data')
            if not isinstance(data, list) or any(not isinstance(row, dict) or not row.get(id_key) for row in data):
                raise OrdersError('委托响应格式异常。')
            for row in data:
                identity = (id_key, str(row[id_key]))
                if identity not in seen:
                    seen.add(identity)
                    rows.append({**row, '_algo': id_key == 'algoId'})
            if len(data) < 100:
                break
            after = str(data[-1][id_key])
            if after in cursors:
                raise OrdersError('委托分页异常，未能确认完整列表。')
            cursors.add(after)

    try:
        with trade_factory(api_key=credentials.api_key, api_secret_key=credentials.secret_key,
                           passphrase=credentials.passphrase, flag=credentials.flag, debug=False) as trade:
            trade.timeout = httpx.Timeout(3)
            pages(trade.get_order_list, 'ordId')
            for kind in ALGO_TYPES:
                pages(trade.order_algos_list, 'algoId', ordType=kind)
        missing = {row['instId']: row.get('instType') for row in rows if row.get('instId') not in instruments and row.get('instType') in ('SWAP', 'FUTURES', 'OPTION')}
        if missing:
            with account_factory(api_key=credentials.api_key, api_secret_key=credentials.secret_key,
                                 passphrase=credentials.passphrase, flag=credentials.flag, debug=False) as account:
                account.timeout = httpx.Timeout(3)
                for instrument, kind in missing.items():
                    check_stop()
                    try:
                        response = account.get_instruments(instType=kind, instId=instrument)
                        if str(response.get('code')) == '0':
                            match = next((row for row in response.get('data', []) if row.get('instId') == instrument), None)
                            if match:
                                instruments[instrument] = match
                    except Exception:
                        pass  # Explicit contract units remain available.
    except OrdersError:
        raise
    except httpx.HTTPError:
        raise OrdersError('委托查询网络异常，正在自动重试。') from None
    except Exception:
        raise OrdersError('委托读取失败，请检查连接和凭据配置。') from None
    return parse_orders(rows, instruments)


class OrdersWorker(PositionsWorker):
    resource_name = '委托'

    def __init__(self, parent=None, **kwargs):
        super().__init__(parent, **kwargs)
        if kwargs.get('fetcher') is None:
            self.fetcher = lambda credentials: fetch_orders(credentials, instrument_cache=self._instruments,
                                                             should_stop=self.isInterruptionRequested)
