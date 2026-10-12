"""Allowlisted, credential-redacted order diagnostics; never record auth headers."""
import hashlib
import json
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

LOG_PATH = Path.home() / '.local/state/okx-trader/orders.log'


def response_fields(response, credentials):
    if not isinstance(response, dict):
        return {'response': 'invalid response type'}
    result = {key: response.get(key) for key in ('code', 'msg')}
    rows = response.get('data')
    if isinstance(rows, list):
        result['data'] = [{key: row.get(key) for key in ('sCode', 'sMsg')}
                          for row in rows if isinstance(row, dict)]
    return json.loads(credentials.redact(json.dumps(result, ensure_ascii=False, default=str)))


def order_diagnostics(prepared):
    draft = prepared.draft
    buy = (draft.direction == 'long') == (draft.action == 'open')
    estimate = (draft.quantity * draft.price / draft.leverage
                if draft.price is not None and draft.action == 'open' else None)
    return dict(instId=draft.instrument, tdMode=draft.margin,
                posSide=draft.direction if prepared.position_mode == 'long_short_mode' else 'net',
                side='buy' if buy else 'sell', px=str(draft.price) if draft.price is not None else '市价',
                sz=str(prepared.contract_quantity), coinQuantity=str(draft.quantity), targetLeverage=str(draft.leverage),
                estimatedMargin=str(estimate) if estimate is not None else '不适用/市价无法按委托价估算',
                marginCurrency=draft.instrument.split('-')[1], flag=prepared.credentials.flag,
                domain=prepared.credentials.domain,
                credentialFingerprint=hashlib.sha256(prepared.credentials.api_key.encode()).hexdigest()[:12],
                balance=prepared.balance_snapshot, rules=prepared.instrument_rules)


def audit(event, payload, credentials):
    """Return False if logging is unavailable; diagnostics must not imply a failed POST."""
    try:
        logger = logging.getLogger('okx_gui.order_audit')
        if not logger.handlers:
            LOG_PATH.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            handler = RotatingFileHandler(LOG_PATH, maxBytes=2_000_000, backupCount=3, encoding='utf-8')
            LOG_PATH.chmod(0o600)
            handler.setFormatter(logging.Formatter('%(asctime)s %(message)s'))
            logger.addHandler(handler)
            logger.setLevel(logging.INFO)
            logger.propagate = False
        logger.info('%s %s', event, credentials.redact(json.dumps(payload, ensure_ascii=False, default=str)))
        return True
    except Exception:
        return False
