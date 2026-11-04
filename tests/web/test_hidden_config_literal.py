"""审查 L2：Literal 类型的 HiddenConfig 字段（lifelike_emotion_hint）能在 WebUI 保存。"""

from types import SimpleNamespace

import pytest

from astrbot_plugin_iris_memory.iris_memory.web.routes import hidden_config_routes as hr


@pytest.fixture
def route(monkeypatch):
    saved = []
    box = {}

    async def get_json():
        return box["data"]

    monkeypatch.setattr(hr, "request", SimpleNamespace(get_json=get_json))
    monkeypatch.setattr(hr, "jsonify", lambda d: d)
    monkeypatch.setattr(hr, "get_config", lambda: SimpleNamespace(update_hidden=saved.append))

    async def call(updates):
        box["data"] = {"updates": updates}
        return await hr.update_hidden_config()

    return call, saved


@pytest.mark.asyncio
async def test_literal_field_accepted_with_others(route):
    call, saved = route
    out = await call({"lifelike_emotion_hint": "hedged", "lifelike_numeric_guard": True})
    assert out["success"] is True
    assert saved == [{"lifelike_emotion_hint": "hedged", "lifelike_numeric_guard": True}]


@pytest.mark.asyncio
@pytest.mark.parametrize("value", ["nope", 1, True])
async def test_literal_field_rejects_bad_values(route, value):
    call, saved = route
    body, code = await call({"lifelike_emotion_hint": value})
    assert code == 400 and body["success"] is False and saved == []


@pytest.mark.asyncio
async def test_unknown_key_still_rejected(route):
    call, saved = route
    body, code = await call({"no_such_key": 1})
    assert code == 400 and saved == []
