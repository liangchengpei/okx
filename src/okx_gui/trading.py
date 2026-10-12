"""Explicitly confirmed order submission through python-okx; never auto-retry POSTs."""
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
import uuid
import time

import httpx
from okx.Account import AccountAPI
from okx.Trade import TradeAPI
from PySide6.QtCore import QThread, Signal

from okx_gui.positions import Credentials, load_credentials


class TradingError(ValueError):
    pass


class SubmissionUncertain(TradingError):
    pass


@dataclass(frozen=True)
class PreparedOrder:
    draft: object
    credentials: Credentials = field(repr=False)
    position_mode: str
    client_id: str
    prepared_at: float = field(default_factory=time.monotonic)

    @property
    def environment(self):
        return '模拟盘' if self.credentials.flag == '1' else '实盘'


def data(response, label, credentials):
    if not isinstance(response, dict):
        raise TradingError(f'{label}响应异常。')
    if str(response.get('code')) == '50123':
        raise TradingError(f'OKX 拒绝了该 Key 的加密资产交易权限（50123）。当前为'
                           f'{"模拟盘" if credentials.flag == "1" else "实盘"}，接口 {credentials.domain}。'
                           '账户显示 trade，但服务端仍拒绝此次交易；请核对所属站点，仍失败请联系 OKX 核查产品权限。')
    if str(response.get('code')) != '0':
        raise TradingError(credentials.redact(f"{label}失败（{response.get('code')}）：{response.get('msg', '')}")[:240])
    rows = response.get('data')
    if not isinstance(rows, list):
        raise TradingError(f'{label}响应异常。')
    return rows


def options(credentials):
    return dict(api_key=credentials.api_key, api_secret_key=credentials.secret_key,
                passphrase=credentials.passphrase, flag=credentials.flag, domain=credentials.domain, debug=False)


def number(value, label):
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise TradingError(f'{label}无效。') from None
    if not result.is_finite():
        raise TradingError(f'{label}无效。')
    return result


def order_parameters(prepared):
    draft = prepared.draft
    buy = (draft.direction == 'long') == (draft.action == 'open')
    params = dict(instId=draft.instrument, tdMode=draft.margin,
                  side='buy' if buy else 'sell', ordType=draft.order_type,
                  sz=format(draft.quantity, 'f'), clOrdId=prepared.client_id,
                  posSide=draft.direction if prepared.position_mode == 'long_short_mode' else 'net')
    if draft.order_type == 'trigger':
        params['algoClOrdId'] = params.pop('clOrdId')
        params['triggerPx'] = format(draft.trigger_price, 'f')
        params['triggerPxType'] = draft.trigger_source
        params['orderPx'] = format(draft.price, 'f') if draft.price is not None else '-1'
    elif draft.order_type == 'limit':
        params['px'] = format(draft.price, 'f')
    if prepared.position_mode == 'net_mode' and draft.action == 'close':
        params['reduceOnly'] = True
    return params


def validate_with_account(account, draft, credentials):
    if (draft.action not in ('open', 'close') or draft.direction not in ('long', 'short') or
            draft.margin not in ('cross', 'isolated') or draft.order_type not in ('limit', 'market', 'trigger')):
        raise TradingError('订单参数无效。')
    config = data(account.get_account_config(), '读取账户模式', credentials)
    if not config or config[0].get('acctLv') not in ('2', '3'):
        raise TradingError('当前仅支持合约模式或跨币种保证金模式，请在 OKX 检查账户模式。')
    mode = config[0].get('posMode')
    if mode not in ('net_mode', 'long_short_mode'):
        raise TradingError('无法确认账户持仓模式。')
    kind = 'SWAP' if draft.instrument.endswith('-SWAP') else 'FUTURES'
    rows = data(account.get_instruments(instType=kind, instId=draft.instrument), '读取合约规则', credentials)
    instrument = next((r for r in rows if r.get('instId') == draft.instrument), None)
    if not instrument or instrument.get('state') != 'live':
        raise TradingError('合约不存在或当前不可交易。')
    lot = number(instrument.get('lotSz'), '数量步长')
    minimum = number(instrument.get('minSz'), '最小数量')
    quantity = number(draft.quantity, '数量')
    if lot <= 0 or minimum <= 0:
        raise TradingError('无法确认合约数量规则。')
    if quantity < minimum or quantity % lot != 0:
        raise TradingError(f'数量至少为 {minimum} 张，且必须是 {lot} 张的整数倍。')
    maximum = instrument.get('maxLmtSz' if draft.price is not None else 'maxMktSz')
    if maximum and quantity > number(maximum, '最大委托数量'):
        raise TradingError(f'数量超过单笔委托上限 {maximum} 张。')
    if draft.order_type == 'limit' or (draft.order_type == 'trigger' and draft.price is not None):
        tick = number(instrument.get('tickSz'), '价格步长')
        price = number(draft.price, '价格')
        if tick <= 0 or price <= 0 or price % tick != 0:
            raise TradingError(f'价格必须大于0，且必须是 {tick} 的整数倍。')
    if draft.order_type == 'trigger':
        if draft.trigger_source not in ('last', 'mark', 'index'):
            raise TradingError('计划委托触发依据无效。')
        trigger = number(draft.trigger_price, '触发价格')
        tick = number(instrument.get('tickSz'), '价格步长')
        if tick <= 0 or trigger <= 0 or trigger % tick != 0:
            raise TradingError(f'触发价格必须大于0，且必须是 {tick} 的整数倍。')
        maximum_trigger = instrument.get('maxTriggerSz')
        if maximum_trigger and quantity > number(maximum_trigger, '计划委托数量上限'):
            raise TradingError(f'数量超过计划委托上限 {maximum_trigger} 张。')
    if draft.action == 'open':
        leverage = number(draft.leverage, '杠杆')
        if leverage < 1 or leverage > number(instrument.get('lever'), '合约杠杆上限'):
            raise TradingError(f"杠杆超出合约允许范围（最高 {instrument.get('lever')} ×）。")
    positions = data(account.get_positions(instId=draft.instrument), '读取持仓', credentials)
    matching = [p for p in positions if p.get('instId') == draft.instrument and p.get('mgnMode') == draft.margin]
    if draft.action == 'close':
        available = Decimal(0)
        for p in matching:
            pos = number(p.get('pos', '0'), '持仓数量')
            belongs = p.get('posSide') == draft.direction if mode == 'long_short_mode' else (
                pos > 0 if draft.direction == 'long' else pos < 0)
            if belongs:
                available += abs(number(p.get('availPos') or p.get('pos', '0'), '可平数量'))
        if quantity > available:
            raise TradingError(f'所选方向和保证金模式的可平数量不足，当前最多 {available} 张。')
    elif mode == 'net_mode':
        for p in matching:
            pos = number(p.get('pos', '0'), '持仓数量')
            if (draft.direction == 'long' and pos < 0) or (draft.direction == 'short' and pos > 0):
                raise TradingError('单向持仓模式已有反向持仓，请先平仓再开仓。')
    return mode


def prepare_order(draft, credentials=None, account_factory=AccountAPI):
    credentials = credentials or load_credentials()
    try:
        with account_factory(**options(credentials)) as account:
            account.timeout = httpx.Timeout(5)
            mode = validate_with_account(account, draft, credentials)
    except TradingError:
        raise
    except httpx.HTTPError:
        raise TradingError('订单校验网络异常，请稍后重试。') from None
    except Exception:
        raise TradingError('订单校验失败，请检查账户与连接。') from None
    return PreparedOrder(draft, credentials, mode, 'gui' + uuid.uuid4().hex[:29])


def submit_order(prepared, account_factory=AccountAPI, trade_factory=TradeAPI, credentials_loader=load_credentials):
    credentials = prepared.credentials
    if time.monotonic() - prepared.prepared_at > 60:
        raise TradingError('订单确认已超过60秒，请重新确认最新规则和订单。')
    if credentials_loader() != credentials:
        raise TradingError('凭据或交易环境已更改，请重新确认订单。')
    draft = prepared.draft
    try:
        with account_factory(**options(credentials)) as account:
            account.timeout = httpx.Timeout(5)
            mode = validate_with_account(account, draft, credentials)
            if mode != prepared.position_mode:
                raise TradingError('持仓模式已更改，请重新确认订单。')
            if draft.action == 'open':
                params = dict(instId=draft.instrument, lever=str(draft.leverage), mgnMode=draft.margin)
                if draft.margin == 'isolated' and mode == 'long_short_mode':
                    params['posSide'] = draft.direction
                data(account.set_leverage(**params), '设置杠杆', credentials)
    except TradingError:
        raise
    except Exception:
        raise TradingError('校验或设置杠杆失败，尚未发送订单；请检查账户杠杆和连接后重试。') from None
    # Only this call sends the order, exactly once. A lost response is not a rejection.
    try:
        with trade_factory(**options(credentials)) as trade:
            trade.timeout = httpx.Timeout(5)
            is_trigger = draft.order_type == 'trigger'
            id_key = 'algoId' if is_trigger else 'ordId'
            try:
                send = trade.place_algo_order if is_trigger else trade.place_order
                response = send(**order_parameters(prepared))
            except Exception:
                try:
                    lookup = (trade.get_algo_order_details(algoClOrdId=prepared.client_id) if is_trigger else
                              trade.get_order(instId=draft.instrument, clOrdId=prepared.client_id))
                    rows = data(lookup, '查询订单结果', credentials)
                    if rows and rows[0].get(id_key):
                        return str(rows[0][id_key])
                except Exception:
                    pass
                raise SubmissionUncertain(f'订单结果未确认，请在 OKX 检查委托/成交记录。客户订单号：{prepared.client_id}。请勿重复下单。') from None
            if not isinstance(response, dict) or 'code' not in response:
                raise SubmissionUncertain(f'订单响应异常，请核对客户订单号 {prepared.client_id}，请勿重复下单。')
            if str(response.get('code')) == '50004':
                raise SubmissionUncertain(f'OKX 处理超时，订单结果未确认，请核对客户订单号 {prepared.client_id}，请勿重复下单。')
            if str(response.get('code')) not in ('0', '1'):
                data(response, '发送订单', credentials)
            rows = response.get('data')
            if not isinstance(rows, list) or not rows or not isinstance(rows[0], dict) or 'sCode' not in rows[0]:
                raise SubmissionUncertain(f'订单结果未确认，请核对客户订单号 {prepared.client_id}。')
            row = rows[0]
            if str(row['sCode']) != '0':
                raise TradingError(credentials.redact(f"订单被拒绝（{row['sCode']}）：{row.get('sMsg', '')}")[:240])
            if not row.get(id_key):
                raise SubmissionUncertain(f'未返回订单号，请核对客户订单号 {prepared.client_id}。')
            return str(row[id_key])
    except (TradingError, SubmissionUncertain):
        raise
    except Exception:
        raise TradingError('交易连接失败，未能发送订单。') from None


class TradingTask(QThread):
    completed = Signal(object)
    failed = Signal(str, bool)

    def __init__(self, operation, parent=None):
        super().__init__(parent)
        self.operation = operation

    def run(self):
        try:
            self.completed.emit(self.operation())
        except SubmissionUncertain as exc:
            self.failed.emit(str(exc), True)
        except (TradingError, ValueError) as exc:
            self.failed.emit(str(exc), False)
        except Exception:
            self.failed.emit('交易请求失败，请检查配置和连接。', False)


def fetch_leverage_options(instrument, margin, credentials=None, account_factory=AccountAPI, should_stop=lambda: False):
    """Read the contract ceiling and account setting; never change leverage."""
    credentials = credentials or load_credentials()
    try:
        with account_factory(**options(credentials)) as account:
            account.timeout = httpx.Timeout(5)
            kind = 'SWAP' if instrument.endswith('-SWAP') else 'FUTURES'
            rows = data(account.get_instruments(instType=kind, instId=instrument), '读取杠杆规则', credentials)
            row = next((r for r in rows if r.get('instId') == instrument), None)
            if not row or row.get('state') != 'live':
                raise TradingError('合约当前不可交易。')
            maximum = number(row.get('lever'), '合约杠杆上限')
            if maximum < 1 or maximum > 10000:
                raise TradingError('无法确认合约杠杆上限。')
            current = None
            if should_stop():
                return maximum, current
            try:
                settings = data(account.get_leverage(instId=instrument, mgnMode=margin), '读取当前杠杆', credentials)
                values = [number(r.get('lever'), '当前杠杆') for r in settings
                          if r.get('instId') == instrument and r.get('mgnMode') == margin]
                values = [v for v in values if 1 <= v <= maximum]
                if values:
                    current = min(values)
            except Exception:
                pass  # The contract ceiling is still usable; default to 1x.
            return maximum, current
    except TradingError:
        raise
    except Exception:
        raise TradingError('杠杆规则读取失败，请检查连接后重试。') from None


class LeverageTask(TradingTask):
    """Read-only task, kept independent from order submission."""
