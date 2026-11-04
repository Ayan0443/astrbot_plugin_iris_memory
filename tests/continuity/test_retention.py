"""活人感 P0-3：observed_posts 第三方原文保留期（dry-run / 实删 / 开关）。"""

from __future__ import annotations

import os
import stat
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from astrbot_plugin_iris_memory.iris_memory.continuity.service import ContinuityService
from astrbot_plugin_iris_memory.iris_memory.continuity.store import ContinuityStore

T0 = 1_793_000_000.0
DAY = 86400.0


class Clock:
    def __init__(self, t: float = T0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


def _post(store, msg_id, age_days, kind="other", chat_id=-100123):
    store.add_observed_post(
        chat_id=chat_id,
        msg_id=msg_id,
        posted_at=T0 - age_days * DAY,
        sender_id="" if kind in ("unknown", "channel") else str(1000 + msg_id),
        author_kind=kind,
        text=f"群友原文{msg_id}",
        via="test",
    )


@pytest.fixture
def store(tmp_path: Path):
    s = ContinuityStore(tmp_path / "continuity" / "continuity.db", clock=Clock())
    s.open()
    yield s
    s.close()


def _rows(store):
    with store._lock:
        return {
            (r["msg_id"], r["author_kind"])
            for r in store._conn().execute("SELECT msg_id, author_kind FROM observed_posts").fetchall()
        }


def _seed(store):
    _post(store, 1, 40)  # other 超期 → 删
    _post(store, 2, 31)  # other 超期 → 删
    _post(store, 3, 29)  # other 未超期
    _post(store, 4, 40, kind="owner")
    _post(store, 5, 40, kind="self")
    _post(store, 6, 40, kind="channel")
    _post(store, 7, 40, kind="unknown")


def test_store_count_and_purge_only_other(store):
    _seed(store)
    cutoff = T0 - 30 * DAY
    assert store.count_observed_expired(cutoff) == 2
    assert _rows(store).__len__() == 7  # 计数不删除
    assert store.purge_observed(cutoff, batch=1) == 2  # 分批
    left = _rows(store)
    assert (1, "other") not in left and (2, "other") not in left
    assert {(3, "other"), (4, "owner"), (5, "self"), (6, "channel"), (7, "unknown")} <= left
    assert store.purge_observed(cutoff) == 0


def test_store_invalid_kinds_noop(store):
    _seed(store)
    assert store.count_observed_expired(T0, kinds=("bogus",)) == 0
    assert store.purge_observed(T0, kinds=()) == 0
    assert len(_rows(store)) == 7


class Cfg:
    def __init__(self, **kv):
        self.values = dict(kv)

    def get(self, key, default=None):
        return self.values.get(key, default)


def _svc(tmp_path, cfg, clock):
    return ContinuityService(
        context=SimpleNamespace(get_platform_inst=lambda pid: None),
        config_source=cfg,
        state=SimpleNamespace(get_whitelist=lambda: [], resume_after_owner_message=lambda g: True),
        owner_ids_get=lambda: ["123456789"],
        silent_sessions_get=lambda: [],
        data_dir=tmp_path / "data",
        llm_manager=SimpleNamespace(generate=AsyncMock(return_value='{"ops":[]}')),
        clock=clock,
    )


@pytest.mark.asyncio
async def test_service_default_is_dry_run(tmp_path, caplog):
    clock = Clock()
    s = _svc(tmp_path, Cfg(), clock)
    s.store.open()
    try:
        _seed(s.store)
        res = await s._retention_observed(clock())
        assert res == {"days": 30, "dry_run": True, "count": 2}
        assert len(_rows(s.store)) == 7
        assert s.store.get_counters().get("lifelike_retention_would_delete") == 2
        # 日志不含正文
        assert all("群友原文" not in r.getMessage() for r in caplog.records)
        # 每日一次：同一天再跑跳过
        assert await s._retention_observed(clock() + 3600) is None
        clock.t += DAY
        assert (await s._retention_observed(clock.t))["dry_run"] is True
    finally:
        s.store.close()


@pytest.mark.asyncio
async def test_service_real_delete_when_dry_run_off(tmp_path):
    clock = Clock()
    s = _svc(tmp_path, Cfg(lifelike_retention_dry_run=False), clock)
    s.store.open()
    try:
        _seed(s.store)
        res = await s._retention_observed(clock())
        assert res == {"days": 30, "dry_run": False, "count": 2}
        assert len(_rows(s.store)) == 5
        assert s.store.get_counters().get("lifelike_retention_deleted") == 2
    finally:
        s.store.close()


@pytest.mark.asyncio
async def test_service_days_zero_disables(tmp_path):
    clock = Clock()
    s = _svc(tmp_path, Cfg(lifelike_retention_days_observed=0, lifelike_retention_dry_run=False), clock)
    s.store.open()
    try:
        _seed(s.store)
        assert await s._retention_observed(clock()) is None
        assert len(_rows(s.store)) == 7
    finally:
        s.store.close()


@pytest.mark.asyncio
async def test_service_custom_days_and_dict_source(tmp_path):
    clock = Clock()
    s = _svc(tmp_path, {"lifelike_retention_days_observed": "35", "lifelike_retention_dry_run": "false"}, clock)
    s.store.open()
    try:
        _seed(s.store)
        res = await s._retention_observed(clock())
        assert res["count"] == 1 and res["days"] == 35
    finally:
        s.store.close()


@pytest.mark.asyncio
async def test_service_store_error_is_swallowed(tmp_path):
    clock = Clock()
    s = _svc(tmp_path, Cfg(), clock)
    # store 未打开 → 异常 → None，不抛
    assert await s._retention_observed(clock()) is None


@pytest.mark.asyncio
async def test_maintenance_triggers_retention(tmp_path):
    clock = Clock()
    s = _svc(tmp_path, Cfg(lifelike_retention_dry_run=False), clock)
    await s.start()
    try:
        _seed(s.store)
        s._last_maintenance = 0.0
        await s._maintenance()
        assert len(_rows(s.store)) == 5
    finally:
        await s.stop()


def test_migration_backup_is_owner_only(tmp_path):
    from astrbot_plugin_iris_memory.iris_memory.continuity import store as store_mod

    db = tmp_path / "c" / "continuity.db"
    s = ContinuityStore(db, clock=Clock())
    s.open()
    s.close()
    os.chmod(db, 0o644)
    s = ContinuityStore(db, clock=Clock())
    s.open()
    try:
        s._backup_before_migration(1)
    finally:
        s.close()
    backup = db.with_name("continuity.backup_v1.db")
    assert backup.exists()
    assert stat.S_IMODE(backup.stat().st_mode) == 0o600
    assert stat.S_IMODE(db.stat().st_mode) == 0o644  # 原库权限不动
    assert store_mod.SCHEMA_VERSION >= 1
