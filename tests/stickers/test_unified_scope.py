"""表情包统一启用（continuity.sticker_scope_unified）：私聊与群都可用全部启用条目；频道 / silent 永不；off 不变。"""

from astrbot_plugin_iris_memory.iris_memory.continuity.service import ContinuityConfig
from tests.stickers.test_library import CHANNEL, GROUP, OWNER, OWNER_SCOPE, clock, fake_doc, fake_msg, lib  # noqa: F401


def _labels(rows):
    return sorted(r["label"] for r in rows)


def _seed(lib):
    lib.add_from_message("私密", "", fake_msg(fake_doc(1)), OWNER)
    lib.add_from_message("公开", "", fake_msg(fake_doc(2)), OWNER)
    lib.set_scope("公开", "all")
    lib.add_from_message("停用", "", fake_msg(fake_doc(3)), OWNER)
    lib.set_enabled("停用", False)


def test_config_default_off():
    assert ContinuityConfig().sticker_scope_unified is False
    assert ContinuityConfig.from_config({"continuity": {"sticker_scope_unified": True}}).sticker_scope_unified is True


def test_unified_allows_private_entries_in_groups(lib):
    _seed(lib)
    owners = [OWNER]
    assert _labels(lib.allowed_for(GROUP, owners, True)) == ["公开"]  # off：与原来一致
    assert _labels(lib.allowed_for(GROUP, owners, True, unified=True)) == ["公开", "私密"]
    assert _labels(lib.allowed_for(OWNER_SCOPE, owners, False, unified=True)) == ["公开", "私密"]
    # 群里仍需打开群聊表情包开关；频道 / silent 永不
    assert lib.allowed_for(GROUP, owners, False, unified=True) == []
    assert lib.allowed_for(CHANNEL, owners, True, unified=True, blocked_scopes=[CHANNEL]) == []
    assert lib.allowed_for(GROUP, owners, True, unified=True, blocked_scopes=[GROUP]) == []


def test_add_defaults_to_all_and_scope_hint(lib):
    out = lib.handle_command(["add", "开心"], fake_msg(fake_doc(9)), OWNER, [OWNER], unified=True)
    assert "私聊和群" in out and lib.get("开心")["scope"] == "all"
    out2 = lib.handle_command(["add", "难过"], fake_msg(fake_doc(10)), OWNER, [OWNER])
    assert "仅私聊" in out2 and lib.get("难过")["scope"] == "private"
    hint = lib.handle_command(["scope", "难过", "private"], None, OWNER, [OWNER], unified=True)
    assert "已统一启用" in hint and "无需区分" in hint
    listing = lib.handle_command(["list"], None, OWNER, [OWNER], unified=True)
    assert "仅私聊" not in listing
