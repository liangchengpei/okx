"""Explicitly confirmed amendments and cancellations of existing orders."""
from dataclasses import replace
from decimal import Decimal
import uuid

import httpx
from okx.Account import AccountAPI
from okx.Trade import TradeAPI

from okx_gui.positions import load_credentials
from okx_gui.trading import TradingError, SubmissionUncertain, data, number, options


def editable_fields(order):
    """(API field, source field, label, allows market price)."""
    kind = order.raw.get('ordType')
    if not order.raw.get('_algo'):
        return [('newPx', 'px', '委托价格', False)] if kind in ('limit', 'post_only') else []
    if kind == 'trigger':
        return [('newTriggerPx', 'triggerPx', '触发价格', False), ('newOrdPx', 'ordPx', '委托价格（-1 为市价）', True)]
    if kind in ('conditional', 'oco'):
        return [(f'new{prefix.title()}{suffix}', prefix + suffix, label + caption, market)
                for prefix, label in [('tp', '止盈'), ('sl', '止损')]
                if order.raw.get(prefix + 'TriggerPx')
                for suffix, caption, market in [('TriggerPx', '触发价格', False), ('OrdPx', '委托价格（-1 为市价）', True)]]
    return []


def quantity_unit(order):
    """Return coins per contract and a clear unit; do not guess missing metadata."""
    if order.raw.get('instType') in ('SWAP', 'FUTURES', 'OPTION'):
        base = order.instrument.split('-')[0]
        if order.metadata.get('ctValCcy') != base:
            return None, '币'
        try:
            face = number(order.metadata.get('ctVal'), '合约面值') * number(order.metadata.get('ctMult') or '1', '合约乘数')
            if face > 0:
                return face, base
        except TradingError:
            pass
        return None, '币'
    if order.raw.get('instType') in ('SPOT', 'MARGIN'):
        return Decimal(1), order.instrument.split('-')[1 if order.raw.get('tgtCcy') == 'quote_ccy' else 0]
    return None, '币'


def perform_order_action(order, action, changes=None, *, trade_factory=TradeAPI,
                         account_factory=AccountAPI, credentials_loader=load_credentials):
    credentials = order.credentials
    if credentials is None or credentials_loader() != credentials:
        raise TradingError('账户凭据或交易环境已改变，请等待委托重新读取后再操作。')
    if action not in ('cancel', 'amend'):
        raise TradingError('无效的委托操作。')
    algo = bool(order.raw.get('_algo'))
    id_key = 'algoId' if algo else 'ordId'
    params = {'instId': order.instrument, id_key: order.order_id}
    try:
        with trade_factory(**options(credentials)) as trade:
            trade.timeout = httpx.Timeout(5)
            response = (trade.get_algo_order_details(algoId=order.order_id) if algo else
                        trade.get_order(instId=order.instrument, ordId=order.order_id))
            rows = data(response, '核对委托状态', credentials)
            fresh = next((r for r in rows if str(r.get(id_key)) == order.order_id and r.get('instId') == order.instrument), None)
            if not fresh or fresh.get('state') not in ('live', 'partially_filled', 'pause'):
                raise TradingError('委托已成交、已触发或已撤销，请查看最新委托列表。')
            if action == 'amend':
                fields = editable_fields(order)
                if not fields:
                    raise TradingError('该委托类型暂不支持在此修改，请在 OKX 修改。')
                watched = ['sz', 'ordType', 'side', 'tdMode', 'posSide', 'triggerPxType', 'tpTriggerPxType', 'slTriggerPxType'] + [source for _, source, _, _ in fields]
                if any(str(fresh.get(key, '')) != str(order.raw.get(key, '')) for key in watched):
                    raise TradingError('委托内容已改变，请关闭窗口后重新打开修改。')
                changes = dict(changes or {})
                if not changes or set(changes) - ({key for key, _, _, _ in fields} | {'newSz'}):
                    raise TradingError('请修改价格或数量后再提交。')
                with account_factory(**options(credentials)) as account:
                    account.timeout = httpx.Timeout(5)
                    rules = data(account.get_instruments(instType=fresh.get('instType'), instId=order.instrument), '读取合约规则', credentials)
                rule = next((r for r in rules if r.get('instId') == order.instrument), None)
                if not rule:
                    raise TradingError('无法读取该委托的交易规则。')
                for key, _, label, market in fields:
                    if key not in changes:
                        continue
                    value = number(changes[key], label)
                    tick = number(rule.get('tickSz'), '价格步长')
                    if not (market and value == -1) and (value <= 0 or tick <= 0 or value % tick != 0):
                        raise TradingError(f'{label}必须大于0且为 {tick} 的整数倍。')
                    params[key] = format(value, 'f')
                if 'newSz' in changes:
                    from dataclasses import replace
                    face, unit = quantity_unit(replace(order, metadata=rule))
                    old_face, _ = quantity_unit(order)
                    if face is None or face != old_face or fresh.get('closeFraction') not in (None, '', '0'):
                        raise TradingError('数量换算规则不可用或已改变，请重新读取委托。')
                    coins = number(changes['newSz'], '委托总数量')
                    lot = number(rule.get('lotSz'), '数量步长')
                    minimum = number(rule.get('minSz'), '最小数量')
                    if lot <= 0 or minimum <= 0 or coins < minimum * face or coins % (lot * face) != 0:
                        raise TradingError(f'数量至少为 {minimum * face:f} {unit}，且为 {lot * face:f} {unit} 的整数倍。')
                    size = coins / (lot * face) * lot
                    if size <= number(fresh.get('accFillSz') or '0', '已成交数量'):
                        raise TradingError('委托总数量必须大于已成交数量；如需停止剩余成交，请撤单。')
                    max_key = 'maxTriggerSz' if algo else 'maxLmtSz'
                    if rule.get(max_key) and size > number(rule[max_key], '单笔上限'):
                        raise TradingError('数量超过该委托的单笔上限。')
                    params['newSz'] = format(size, 'f')
                params.update(cxlOnFail=False, reqId='gui' + uuid.uuid4().hex[:29])
            # One POST only. An unknown outcome must never trigger an automatic retry.
            try:
                if action == 'cancel':
                    result = trade.cancel_algo_order([params]) if algo else trade.cancel_order(**params)
                else:
                    result = trade.amend_algo_order(**params) if algo else trade.amend_order(**params)
            except Exception:
                raise SubmissionUncertain('操作结果未确认，请核对 OKX 委托记录；请勿重复提交。') from None
            if not isinstance(result, dict) or 'code' not in result or str(result.get('code')) == '50004':
                raise SubmissionUncertain('操作结果未确认，请核对 OKX 委托记录。')
            if str(result.get('code')) not in ('0', '1'):
                data(result, '委托操作', credentials)
            result_rows = result.get('data')
            if not isinstance(result_rows, list) or not result_rows or not isinstance(result_rows[0], dict) or 'sCode' not in result_rows[0]:
                raise SubmissionUncertain('操作响应异常，请核对 OKX 委托记录。')
            row = result_rows[0]
            if str(row['sCode']) == '50004':
                raise SubmissionUncertain('操作处理超时，请核对 OKX 委托记录。')
            if str(row['sCode']) != '0':
                raise TradingError(credentials.redact(f"操作被拒绝（{row['sCode']}）：{row.get('sMsg', '')}")[:240])
            if str(row.get(id_key)) != order.order_id:
                raise SubmissionUncertain('响应委托编号不匹配，请核对 OKX 委托记录。')
            return '撤单请求已受理，请以刷新后的委托状态为准。' if action == 'cancel' else '修改请求已受理，请以刷新后的委托内容为准。'
    except TradingError:
        raise
    except Exception:
        raise TradingError('核对委托失败，尚未发送操作，请检查连接后重试。') from None


def fetch_order_after_action(order, *, trade_factory=TradeAPI, credentials_loader=load_credentials):
    """Read one affected order without waiting for the full pending-order scan."""
    from okx_gui.orders import parse_orders
    credentials = order.credentials
    if credentials is None or credentials_loader() != credentials:
        raise TradingError('账户已改变，请等待委托列表更新。')
    algo = bool(order.raw.get('_algo'))
    id_key = 'algoId' if algo else 'ordId'
    with trade_factory(**options(credentials)) as trade:
        trade.timeout = httpx.Timeout(3)
        response = (trade.get_algo_order_details(algoId=order.order_id) if algo else
                    trade.get_order(instId=order.instrument, ordId=order.order_id))
        rows = data(response, '查询委托状态', credentials)
    row = next((r for r in rows if str(r.get(id_key)) == order.order_id and r.get('instId') == order.instrument), None)
    if row is None:
        raise TradingError('尚未确认委托最终状态，请等待列表同步。')
    if row.get('state') in ('canceled', 'mmp_canceled', 'filled', 'effective', 'order_failed'):
        return None
    if row.get('state') not in ('live', 'partially_filled', 'pause'):
        raise TradingError('委托状态待确认，请等待列表同步。')
    return replace(parse_orders([{**row, '_algo':algo}], {order.instrument:order.metadata})[0], credentials=credentials)
