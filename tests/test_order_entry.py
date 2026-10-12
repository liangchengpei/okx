from decimal import Decimal

import pytest
from PySide6.QtWidgets import QDialog

from okx_gui.order_entry import OrderEntry


def entry(qtbot):
    widget = OrderEntry()
    qtbot.addWidget(widget)
    widget.set_contracts(['BTC-USDT-SWAP', 'SPCX-USDT-SWAP'])
    return widget


def test_limit_draft_preserves_decimal_precision(qtbot):
    widget = entry(qtbot)
    widget.price.setText('82601.50001')
    widget.quantity.setText('0.01')
    draft = widget.build_draft('long')
    assert draft.instrument == 'BTC-USDT-SWAP'
    assert draft.price == Decimal('82601.50001')
    assert draft.quantity == Decimal('0.01')
    assert draft.action == 'open' and draft.margin == 'cross'


@pytest.mark.parametrize('text', ['', '0', '-1', 'NaN', 'Infinity', 'abc'])
def test_invalid_quantities_are_rejected(qtbot, text):
    widget = entry(qtbot)
    widget.quantity.setText(text)
    with pytest.raises(ValueError, match='数量'):
        widget.build_draft('short')
    widget.preview('short')
    assert not widget.error.isHidden()
    assert not widget.findChildren(QDialog)


def test_market_and_close_order_ignore_old_limit_price(qtbot):
    widget = entry(qtbot)
    widget.quantity.setText('2')
    widget.price.setText('invalid')
    widget.action.setCurrentIndex(1)
    widget.order_type.setCurrentIndex(1)
    draft = widget.build_draft('short')
    assert draft.action == 'close' and draft.direction == 'short'
    assert draft.order_type == 'market' and draft.price is None
    assert not widget.price.isEnabled()
    assert widget.sell.text() == '预览平空'


def test_switching_contract_clears_inputs_but_list_updates_preserve_draft(qtbot):
    widget = entry(qtbot)
    widget.price.setText('82600')
    widget.quantity.setText('1')
    widget.set_contracts(['BTC-USDT-SWAP', 'SPCX-USDT-SWAP', 'ETH-USDT-SWAP'])
    assert widget.price.text() == '82600'
    assert widget.quantity.text() == '1'
    widget.contract.setCurrentIndex(1)
    assert widget.price.text() == widget.quantity.text() == ''
    widget.set_contracts([])
    assert not widget.buy.isEnabled() and not widget.sell.isEnabled()


def test_latest_price_does_not_overwrite_user_limit_and_expires(qtbot, monkeypatch):
    widget = entry(qtbot)
    clock = [100.0]
    monkeypatch.setattr('okx_gui.order_entry.time.monotonic', lambda: clock[0])
    widget.price.setText('80000')
    widget.update_quote('BTC-USDT-SWAP', '82601.5')
    assert widget.price.text() == '80000'
    widget.fill_latest()
    assert widget.price.text() == '82601.5'
    clock[0] += 11
    widget.refresh_quote()
    assert widget.current_quote() is None
    assert not widget.latest.isEnabled()


def test_preview_opens_local_summary_only(qtbot):
    widget = entry(qtbot)
    widget.price.setText('82601.5')
    widget.quantity.setText('1')
    widget.preview('long')
    dialogs = widget.findChildren(QDialog)
    assert len(dialogs) == 1
    assert dialogs[0].windowTitle() == '订单预览'
    dialogs[0].close()
