"""Check SDK transports without credentials or network access."""

def test_okx_rest_and_websocket_imports():
    from okx.Trade import TradeAPI
    from okx.websocket.WsPublicAsync import WsPublicAsync
    from okx.websocket.WsPrivateAsync import WsPrivateAsync
    from websockets import connect

    assert callable(TradeAPI)
    assert callable(WsPublicAsync)
    assert callable(WsPrivateAsync)
    assert callable(connect)
