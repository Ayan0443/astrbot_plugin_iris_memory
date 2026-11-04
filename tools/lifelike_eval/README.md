# lifelike_eval：活人感改造 P0-0 离线评测脚手架

离线工具，**不进生产进程**（`main.py` / `iris_memory` 不导入本目录），所以没有运行时开关；回退 = 删除本目录。

## 组成

| 文件 | 作用 |
|---|---|
| `rules.py` | 规则词表与正则：反应词 / 句尾语气词 / 切句（口吻附录 §5.5）、黑名单 `BL-*`（§2.3、§5.4）、禁止照搬 `COPY-H*`/`COPY-S*`（§12.4 原样）、`AIW-*`（设计 §8.1 可正则部分）、`MAN-*`、`PRIV-*`、NumericLeak `NL-*`（ROADMAP P0-5 口径）、卖萌初筛 `CUTE-*`。**只在代码里，不进 prompt** |
| `metrics.py` | 纯函数指标：问号结尾率、语气词密度、"～"率、"！！"数、反应词开头率、同标记重复率（N=20 窗口 + 告警）、首尾 4 字口头禅、黑名单 / 照搬硬软命中率、NumericLeak 命中、卖萌初筛、淡档越档、`scan(text, ctx)`、`aggregate_replies` |
| `snapshot.py` | 只读快照导出 + 基线采集（见下文安全约束） |
| `evalset.py` + `data/evalset_v1.json` | 原创评测集：S01–S30、P1–P12、C1–C7（口吻附录 §7.1），E1/E2/E3/E4/E6/E7/E8/E9/E10/E11 原创样例子集（设计 §12.3）。`requires` 字段标出依赖的后续模块 |
| `runner.py` | 离线运行器：注入式 `generate` 逐轮生成（不触网，`RecordingSender` 假发送）→ `score_case` / `score_run` 规则打分 → `export_blind` 盲评 HTML（随机左右、答案键分文件、权限 600） |
| `../lifelike_snapshot.py` | ROADMAP §7.1 约定的入口名，转调 `snapshot.main` |

E9（开关 off 时 prompt 逐字节一致）在 `tests/lifelike_eval/test_e9_prompt_snapshot.py`，基准 `tests/lifelike_eval/snapshots/e9_prompt_snapshot.json` 生成于引入活人感改造之前的基线提交。

## 只读快照的安全约束（代码断言）

- 只用 `file:<path>?mode=ro` 打开，并 `PRAGMA query_only=ON`；路径必须在数据根目录白名单内且文件已存在。
- 正文只在内存里参与规则计算；输出只有聚合数字、规则 id、单字标记名；聊天 id 不输出（群按帖子数编号 `chat_1..`）。
- 落盘前 `assert_no_text`：任何字符串（含键）出现连续 ≥8 个汉字或 ≥7 位数字串即中止。
- 输出文件权限 600；拒绝写进 `<data_root>` 下除 `dev/` 以外的位置。

## 用法（容器内、插件根目录）

```bash
# 基线（聚合 JSON 到 stdout，重定向到你自己的目录，权限 600）
python -m tools.lifelike_eval.snapshot --data-root /AstrBot/data --stdout --label baseline-YYYYMMDD
# 附带逐条脱敏行（day/len/类别/规则 id），供 E5 主动回放
python -m tools.lifelike_eval.snapshot --data-root /AstrBot/data --stdout --rows
# 对已生成的输出打分：outputs.json = {case_id: [reply, ...]}
python -m tools.lifelike_eval.runner score --outputs outputs.json --out report.json
```

测试（只在容器里、串行）：

```bash
docker exec -w /AstrBot/data/dev/<worktree> astrbot \
  nice -n 19 python -m pytest -q -p no:cacheprovider tests/lifelike_eval
# 验收①：-k smoke
```

## 已知限制

- `rules.py` 是离线度量副本；P0-6 `StyleGuard` 上线后应以其词表为准，二者合并时统一到一处（ROADMAP P0-0 ③"复用 StyleGuard.scan"）。
- `AIW-01/06/07/10`（复述、端水、近 50 条重复、四段式）没有可靠的单条正则，未实现；`AIW-07` 由 `head_tail_repeats` / `marker_repeat` 覆盖。`MAN-*`/`PRIV-*`/`CUTE-*` 是词表近似，最终判定靠人工 / LLM 是否题。
- 颜文字识别是启发式。
- runner 不内置 provider 调用：真实生成要由调用方在开发 worktree 里组装 off/on 两套 prompt 后注入 `generate`（避免误连生产、误发消息）。
- E 系列是原创样例子集（E3 30 条齐全；E6 30/100；E1/E2/E4/E7/E8/E10/E11 为代表性子集），E5 由快照 `--rows` 驱动，不在评测集里。
- E9 只覆盖 L1、画像、连续性、收尾提示、群聊说明这几段；L2/L3/learning/图片/表情在快照里按配置关闭（它们依赖外部存储），由各自模块的单测覆盖。
- 基线样本很小（私聊 bot 回复 16 条、1 天），只作方向对照；按 ROADMAP §7.2 需要连续 7 天每日采集。
