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


def test_live_entry_requires_confirmation_before_sending(qtbot, monkeypatch):
    from PySide6.QtWidgets import QPushButton
    from okx_gui.positions import Credentials
    from okx_gui.trading import PreparedOrder
    widget = OrderEntry(trading=True)
    qtbot.addWidget(widget)
    widget.set_contracts(['BTC-USDT-SWAP'])
    widget.quantity.setText('1')
    widget.price.setText('82600')
    calls = []
    def prepare(draft):
        calls.append('prepare')
        return PreparedOrder(draft, Credentials('test', 'secret', 'phrase', '1'), 'net_mode', 'gui123')
    def send(prepared):
        calls.append('send')
        assert prepared.credentials.flag == '1'
        return '123'
    monkeypatch.setattr('okx_gui.trading.prepare_order', prepare)
    monkeypatch.setattr('okx_gui.trading.submit_order', send)
    widget.preview('long')
    qtbot.waitUntil(lambda: not widget.busy)
    assert calls == ['prepare']
    dialog = widget.findChildren(QDialog)[0]
    assert '模拟盘' in dialog.windowTitle()
    buttons = dialog.findChildren(QPushButton)
    confirm = next(b for b in buttons if b.text().startswith('确认发送'))
    with qtbot.waitSignal(widget.submitted):
        confirm.click()
    qtbot.waitUntil(lambda: not widget.busy)
    assert calls == ['prepare', 'send']
    assert widget.quantity.text() == ''
    assert '已受理' in widget.error.text()


def test_cancel_confirmation_never_sends(qtbot, monkeypatch):
    from okx_gui.positions import Credentials
    from okx_gui.trading import PreparedOrder
    widget = OrderEntry(trading=True)
    qtbot.addWidget(widget)
    widget.set_contracts(['BTC-USDT-SWAP'])
    widget.quantity.setText('1')
    widget.price.setText('82600')
    monkeypatch.setattr('okx_gui.trading.prepare_order', lambda draft: PreparedOrder(
        draft, Credentials('test', 'secret', 'phrase', '1'), 'net_mode', 'gui123'))
    monkeypatch.setattr('okx_gui.trading.submit_order', lambda _: pytest.fail('Canceled order must not be sent'))
    widget.preview('long')
    qtbot.waitUntil(lambda: not widget.busy)
    widget.findChildren(QDialog)[0].reject()
    assert widget.quantity.text() == '1'


def test_uncertain_order_blocks_repeat_submission(qtbot, monkeypatch):
    widget = OrderEntry(trading=True)
    qtbot.addWidget(widget)
    widget.set_contracts(['BTC-USDT-SWAP'])
    widget.price.setText('82600')
    widget.quantity.setText('1')
    widget.order_failed('结果未确认', True)
    widget.update_form()
    assert not widget.buy.isEnabled() and not widget.sell.isEnabled()
    monkeypatch.setattr('okx_gui.trading.prepare_order', lambda _: pytest.fail('Must remain blocked'))
    widget.preview('long')
    assert not widget.unlock.isHidden()


def test_leverage_choices_follow_selected_contract_and_ignore_old_response(qtbot):
    widget = entry(qtbot)
    widget.apply_leverage_rules(widget.current_rules_key(), (Decimal('75'), Decimal('20')))
    assert not widget.leverage.isEditable()
    assert widget.leverage.count() == 9
    assert widget.leverage.currentData() == Decimal('20')
    old_key = widget.current_rules_key()
    widget.contract.setCurrentIndex(1)
    widget.apply_leverage_rules(widget.current_rules_key(), (Decimal('10'), Decimal('5')))
    widget.apply_leverage_rules(old_key, (Decimal('125'), Decimal('100')))
    assert widget.leverage.count() == 5
    assert widget.leverage.currentData() == Decimal('5')


def test_leverage_load_failure_blocks_open_but_not_close(qtbot):
    widget = entry(qtbot)
    widget.quantity.setText('1')
    widget.price.setText('82600')
    widget.loaded_rules_key = None
    widget.leverage_failed(widget.current_rules_key(), '连接失败')
    with pytest.raises(ValueError, match='杠杆规则'):
        widget.build_draft('long')
    widget.action.setCurrentIndex(1)
    assert widget.build_draft('long').action == 'close'
    assert not widget.leverage.isEnabled()


def test_btc_leverage_matches_app_presets_without_arbitrary_values(qtbot):
    widget = entry(qtbot)
    expected = [Decimal(i) for i in (1, 2, 3, 5, 10, 20, 30, 50, 75, 100)]
    assert [widget.leverage.itemData(i) for i in range(widget.leverage.count())] == expected
    widget.apply_leverage_rules(widget.current_rules_key(), (Decimal('100'), Decimal('17')))
    assert widget.leverage.currentData() is None
    assert all(widget.leverage.itemData(i) != Decimal('17') for i in range(widget.leverage.count()))


def test_trigger_order_draft_and_switching_back_to_market(qtbot):
    widget = entry(qtbot)
    widget.quantity.setText('1')
    widget.price.setText('83001')
    widget.order_type.setCurrentIndex(2)
    assert not widget.trigger_price.isHidden()
    with pytest.raises(ValueError, match='触发价格'):
        widget.build_draft('long')
    widget.trigger_price.setText('83000')
    widget.trigger_source.setCurrentIndex(1)
    draft = widget.build_draft('long')
    assert draft.order_type == 'trigger'
    assert draft.trigger_price == Decimal('83000')
    assert draft.trigger_source == 'mark'
    assert draft.price == Decimal('83001')
    widget.execution_type.setCurrentIndex(1)
    widget.price.setText('invalid')
    assert widget.build_draft('long').price is None
    assert not widget.price.isEnabled()
    widget.order_type.setCurrentIndex(1)
    draft = widget.build_draft('long')
    assert draft.order_type == 'market' and draft.trigger_price is None
    assert widget.trigger_price.isHidden()


def test_trigger_confirmation_displays_conditions(qtbot):
    from PySide6.QtWidgets import QLabel
    widget = entry(qtbot)
    widget.order_type.setCurrentIndex(2)
    widget.quantity.setText('1')
    widget.trigger_price.setText('83000')
    widget.execution_type.setCurrentIndex(1)
    widget.preview('long')
    dialog = widget.findChildren(QDialog)[0]
    texts = [label.text() for label in dialog.findChildren(QLabel)]
    assert '计划委托' in texts and '83000' in texts and '最新价' in texts
    assert '触发后委托' in texts and '市价' in texts
    dialog.close()


def test_quantity_currency_tracks_contract_and_confirmation(qtbot):
    from PySide6.QtWidgets import QLabel
    widget = entry(qtbot)
    assert widget.quantity_label.text() == '数量（BTC）'
    widget.quantity.setText('0.02')
    widget.price.setText('82600')
    widget.preview('long')
    dialog = widget.findChild(QDialog)
    assert '0.02 BTC' in [label.text() for label in dialog.findChildren(QLabel)]
    dialog.close()
    widget.contract.setCurrentIndex(1)
    assert widget.quantity_label.text() == '数量（SPCX）'
    assert 'SPCX' in widget.quantity.placeholderText()
    assert widget.quantity.text() == ''


def test_skip_confirmation_persists_and_can_be_restored(qtbot, monkeypatch, tmp_path):
    from PySide6.QtCore import QSettings
    from PySide6.QtWidgets import QCheckBox, QPushButton
    from okx_gui.positions import Credentials
    from okx_gui.trading import PreparedOrder
    settings = QSettings(str(tmp_path / 'confirmation.ini'), QSettings.IniFormat)
    widget = OrderEntry(trading=True, confirmation_settings=settings)
    qtbot.addWidget(widget)
    widget.set_contracts(['BTC-USDT-SWAP'])
    widget.price.setText('82600')
    calls = []
    credentials = [Credentials('test', 'secret', 'phrase', '1')]
    def prepare(draft):
        calls.append('prepare')
        return PreparedOrder(draft, credentials[0], 'net_mode', 'gui123')
    def send(prepared):
        calls.append('send')
        return '123'
    monkeypatch.setattr('okx_gui.trading.prepare_order', prepare)
    monkeypatch.setattr('okx_gui.trading.submit_order', send)
    widget.quantity.setText('1')
    widget.preview('long')
    qtbot.waitUntil(lambda: not widget.busy)
    dialog = next(d for d in widget.findChildren(QDialog) if d.isVisible())
    dialog.findChild(QCheckBox).setChecked(True)
    confirm = next(b for b in dialog.findChildren(QPushButton) if b.text().startswith('确认发送'))
    with qtbot.waitSignal(widget.submitted):
        confirm.click()
    qtbot.waitUntil(lambda: not widget.busy)
    assert calls == ['prepare', 'send']
    widget.confirmation_settings = QSettings(str(tmp_path / 'confirmation.ini'), QSettings.IniFormat)
    widget.quantity.setText('1')
    with qtbot.waitSignal(widget.submitted):
        widget.preview('long')
        widget.preview('long')  # Busy guard prevents a second submission.
    qtbot.waitUntil(lambda: not widget.busy)
    assert calls == ['prepare', 'send', 'prepare', 'send']
    assert not any(d.isVisible() for d in widget.findChildren(QDialog))
    credentials[0] = Credentials('test', 'secret', 'phrase', '0')
    widget.quantity.setText('1')
    widget.preview('long')
    qtbot.waitUntil(lambda: not widget.busy)
    dialog = next(d for d in widget.findChildren(QDialog) if d.isVisible())
    assert '实盘' in dialog.windowTitle()
    dialog.reject()
    widget.restore_confirmation.click()
    assert not settings.allKeys()
    credentials[0] = Credentials('test', 'secret', 'phrase', '1')
    widget.preview('long')
    qtbot.waitUntil(lambda: not widget.busy)
    dialog = next(d for d in widget.findChildren(QDialog) if d.isVisible())
    dialog.reject()
    assert calls.count('send') == 2


def test_cancel_does_not_save_skip_preference(qtbot, monkeypatch, tmp_path):
    from PySide6.QtCore import QSettings
    from PySide6.QtWidgets import QCheckBox
    from okx_gui.positions import Credentials
    from okx_gui.trading import PreparedOrder
    settings = QSettings(str(tmp_path / 'confirmation.ini'), QSettings.IniFormat)
    widget = OrderEntry(trading=True, confirmation_settings=settings)
    qtbot.addWidget(widget)
    widget.set_contracts(['BTC-USDT-SWAP'])
    widget.quantity.setText('1')
    widget.price.setText('82600')
    monkeypatch.setattr('okx_gui.trading.prepare_order', lambda draft: PreparedOrder(
        draft, Credentials('test', 'secret', 'phrase', '1'), 'net_mode', 'gui123'))
    monkeypatch.setattr('okx_gui.trading.submit_order', lambda _: pytest.fail('Must not send'))
    widget.preview('long')
    qtbot.waitUntil(lambda: not widget.busy)
    dialog = next(d for d in widget.findChildren(QDialog) if d.isVisible())
    dialog.findChild(QCheckBox).setChecked(True)
    dialog.reject()
    assert not settings.allKeys()


def test_preflight_parameters_visible_before_sending(qtbot, monkeypatch, tmp_path):
    from PySide6.QtCore import QSettings
    from okx_gui.positions import Credentials
    from okx_gui.trading import PreparedOrder
    widget = OrderEntry(trading=True, confirmation_settings=QSettings(str(tmp_path/'audit.ini'), QSettings.IniFormat))
    qtbot.addWidget(widget)
    widget.set_contracts(['BTC-USDT-SWAP'])
    widget.quantity.setText('0.1')
    widget.price.setText('82000')
    draft = widget.build_draft('long')
    from dataclasses import replace
    draft = replace(draft, leverage=100)
    prepared = PreparedOrder(draft, Credentials('key','secret','phrase','1'), 'net_mode','gui123',
                             contract_quantity=Decimal('10'), balance_snapshot={'USDT':{'availEq':'0','frozenBal':'2583.89'}})
    monkeypatch.setattr('okx_gui.trading.submit_order', lambda _: pytest.fail('No order may be sent during review'))
    widget.order_prepared(prepared)
    text = widget.diagnostics.text()
    assert 'sz=10 张' in text and '82.0 USDT' in text
    assert 'posSide=net' in text and 'side=buy' in text and 'px=82000' in text
    assert 'USDT 可用权益 0' in text
    widget.findChild(QDialog).reject()


def test_relay_form_selects_parent_and_emits_limit_or_market_draft(qtbot):
    from okx_gui.relay import RelayParent
    from okx_gui.positions import Credentials
    widget = entry(qtbot)
    parent = RelayParent('relay','relay-test','BTC-USDT-SWAP','接力测试',Credentials('k','s','p','1'))
    widget.set_relay_parents([parent])
    widget.order_type.setCurrentText('接力委托')
    widget.relay_parent.setCurrentIndex(1)
    widget.quantity.setText('0.1')
    widget.price.setText('82000')
    with qtbot.waitSignal(widget.relay_requested) as result:
        widget.preview('short')
    assert result.args[0].order_type == 'limit'
    assert result.args[0].direction == 'short'
    assert result.args[1] == parent
    widget.execution_type.setCurrentText('市价')
    assert widget.build_draft('long').price is None
    assert widget.build_draft('long').order_type == 'market'
    widget.set_relay_parents([parent])
    assert widget.relay_parent.currentData() == parent
    widget.set_relay_parents([])
    widget.preview('long')
    assert '请选择有效的前置委托' in widget.error.text()


@pytest.mark.parametrize('execution,price', [('限价', Decimal('82000')), ('市价', None)])
def test_relay_can_create_plan_order(qtbot, execution, price):
    from okx_gui.relay import RelayParent
    from okx_gui.positions import Credentials
    widget = entry(qtbot)
    parent = RelayParent('order', 'parent', 'BTC-USDT-SWAP', '前置单', Credentials('k','s','p','1'))
    widget.set_relay_parents([parent])
    widget.order_type.setCurrentText('接力委托')
    widget.relay_parent.setCurrentIndex(1)
    widget.relay_type.setCurrentText('计划委托')
    assert not widget.trigger_price.isHidden()
    widget.quantity.setText('0.1')
    widget.price.setText('82000')
    widget.trigger_price.setText('82100')
    widget.trigger_source.setCurrentIndex(1)
    widget.execution_type.setCurrentText(execution)
    with qtbot.waitSignal(widget.relay_requested) as result:
        widget.preview('long')
    draft = result.args[0]
    assert draft.order_type == 'trigger'
    assert draft.trigger_price == Decimal('82100')
    assert draft.trigger_source == 'mark'
    assert draft.price == price
    widget.trigger_price.clear()
    widget.preview('long')
    assert not widget.error.isHidden()
