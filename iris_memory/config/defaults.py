"""
Iris Chat Memory - 默认配置定义

使用 dataclass 提供类型安全的配置定义，支持：
- IDE 自动补全
- 编译时类型检查
- 字段注释即文档
- 扁平化键名访问
"""

from dataclasses import dataclass, field, asdict
from typing import Literal, Optional, Dict, List


@dataclass
class ImageParsingConfig:
    """图片解析配置"""

    enable: bool = False
    provider: str = ""
    mode: Literal["all", "related"] = "related"
    daily_quota: int = 200


@dataclass
class L1BufferConfig:
    """L1 消息上下文缓冲配置（用户可见选项）"""

    enable: bool = True
    summary_provider: str = ""
    inject_queue_length: int = 50
    image_parsing: ImageParsingConfig = field(default_factory=ImageParsingConfig)


@dataclass
class L2MemoryConfig:
    """L2 记忆库配置"""

    enable: bool = True
    embedding_source: Literal["provider", "local"] = "provider"
    embedding_provider: str = ""
    embedding_model: str = "BAAI/bge-small-zh-v1.5"
    top_k: int = 10
    relevance_threshold: float = 0.3
    enable_hybrid_retrieval: bool = True


@dataclass
class L3KGConfig:
    """L3 知识图谱配置"""

    enable: bool = True
    extraction_provider: str = ""


@dataclass
class ProfileConfig:
    """画像系统配置"""

    enable: bool = True
    analysis_provider: str = ""
    enable_auto_injection: bool = True
    favorability_enable: bool = True


@dataclass
class IsolationConfig:
    """隔离配置"""

    enable_group_memory_isolation: bool = False
    enable_group_isolation: bool = False
    enable_persona_isolation: bool = False


@dataclass
class ScheduledTasksConfig:
    """梦境任务配置"""

    provider: str = ""
    # 高成本维护任务默认关闭；完成 Governor 灰度后由管理员显式启用。
    enable_dream: bool = False
    dream_stage_temporal_anchor_enabled: bool = True
    dream_stage_reconciliation_enabled: bool = True
    dream_stage_knowledge_induction_enabled: bool = True
    dream_stage_l2_pruning_enabled: bool = True
    dream_stage_l3_maintenance_enabled: bool = True
    dream_max_llm_calls_per_run: int = 20
    dream_max_runtime_minutes: int = 20
    dream_max_groups_per_stage: int = 5
    dream_min_call_interval_ms: int = 500


@dataclass
class ContextControlConfig:
    """上下文控制配置"""

    enable_conversation_cleanup: bool = True


@dataclass
class FeatureToggleConfig:
    """通用辅助功能开关。"""

    enable: bool = True


@dataclass
class PureAtReplyConfig:
    """纯 @ 回复接管配置。

    默认关闭（与 _conf_schema.json 保持一致）：启用前需先关闭
    AstrBot 的 empty_mention_waiting，属于需要人工前置条件的开关。
    """

    enable: bool = False


@dataclass
class ExtrasConfig:
    """用户可见的低成本辅助功能。"""

    pure_at_reply: PureAtReplyConfig = field(default_factory=PureAtReplyConfig)
    error_friendly: FeatureToggleConfig = field(default_factory=FeatureToggleConfig)
    markdown_stripper: FeatureToggleConfig = field(default_factory=FeatureToggleConfig)


@dataclass
class HiddenConfig:
    """隐藏配置(内部参数)

    这些配置项不会在 WebUI 中展示，用于控制内部行为。
    支持运行时热修改，并自动持久化到 data/iris_memory/hidden_config.json

    每个字段通过 metadata 声明 description 和 group，
    供 Web 前端自动渲染，增删字段时无需手动同步路由代码。
    """

    # Token 预算控制
    token_budget_max_tokens: int = field(
        default=5000,
        metadata={"description": "Token 预算上限", "group": "Token 预算"},
    )

    # L1 缓冲内部参数
    l1_segment_1_length: int = field(
        default=10,
        metadata={
            "description": "L1-1 最新段消息数（始终注入上下文）",
            "group": "L1 缓冲",
        },
    )
    l1_segment_3_length: int = field(
        default=10,
        metadata={
            "description": "L1-3 缓冲段消息数（辅助总结理解）",
            "group": "L1 缓冲",
        },
    )
    l1_max_queue_tokens: int = field(
        default=4000,
        metadata={"description": "队列最大 Token 数，超限触发总结", "group": "L1 缓冲"},
    )
    l1_max_single_message_tokens: int = field(
        default=500,
        metadata={
            "description": "单条消息最大 Token 数，普通消息超限丢弃，合并转发消息超限截断",
            "group": "L1 缓冲",
        },
    )
    l1_inject_max_content_chars: int = field(
        default=300,
        metadata={
            "description": "注入时单条消息最大字符数，0 不截断",
            "group": "L1 缓冲",
        },
    )
    l1_max_memories_per_summary: int = field(
        default=10,
        metadata={"description": "每次总结写入 L2 的最大记忆条数", "group": "L1 缓冲"},
    )
    l1_summary_queue_limit: int = field(
        default=500,
        metadata={"description": "L1 总结待处理会话上限", "group": "L1 缓冲"},
    )
    l1_summary_worker_count: int = field(
        default=2,
        metadata={"description": "L1 总结固定 Worker 数", "group": "L1 缓冲"},
    )
    l1_outbox_worker_count: int = field(
        default=2,
        metadata={"description": "L1 Outbox 后台写入 Worker 数", "group": "L1 缓冲"},
    )
    l1_outbox_poll_seconds: int = field(
        default=60,
        metadata={"description": "L1 Outbox 崩溃恢复扫描间隔(秒)", "group": "L1 缓冲"},
    )

    # 遗忘权重算法参数
    forgetting_lambda: float = field(
        default=0.1,
        metadata={"description": "近因性衰减系数", "group": "遗忘算法"},
    )
    forgetting_threshold: float = field(
        default=0.3,
        metadata={"description": "遗忘阈值", "group": "遗忘算法"},
    )
    forgetting_immediate_eviction_threshold: float = field(
        default=0.1,
        metadata={"description": "极端低分直接淘汰阈值", "group": "遗忘算法"},
    )

    # L2 记忆遗忘评分权重 S = w_recency·R + w_frequency·F + w_confidence·C + w_isolation·(1-D) + w_importance·I
    forgetting_l2_weight_recency: float = field(
        default=0.4,
        metadata={"description": "L2 遗忘评分: 近因性权重", "group": "遗忘算法"},
    )
    forgetting_l2_weight_frequency: float = field(
        default=0.35,
        metadata={"description": "L2 遗忘评分: 频率性权重", "group": "遗忘算法"},
    )
    forgetting_l2_weight_confidence: float = field(
        default=0.25,
        metadata={"description": "L2 遗忘评分: 置信度权重", "group": "遗忘算法"},
    )
    forgetting_l2_weight_importance: float = field(
        default=0.3,
        metadata={"description": "L2 遗忘评分: 重要度权重", "group": "遗忘算法"},
    )
    forgetting_l2_weight_isolation: float = field(
        default=0.0,
        metadata={
            "description": "L2 遗忘评分: 孤立度权重（设为 0 则禁用孤立度因子）",
            "group": "遗忘算法",
        },
    )
    l2_retention_days: int = field(
        default=30,
        metadata={
            "description": "L2 记忆保留天数（超期且低分才淘汰）",
            "group": "遗忘算法",
        },
    )
    l2_checkpoint_writes: int = field(
        default=50,
        metadata={
            "description": "L2 FAISS 索引每 N 次写入异步落盘一次，收敛崩溃丢失窗口（0=禁用，仅关闭时保存）",
            "group": "持久化",
        },
    )

    # L3 知识图谱遗忘评分权重 S = w_recency·R + w_structure·(1-D) + w_confidence·C + w_verification·V
    forgetting_kg_weight_recency: float = field(
        default=0.15,
        metadata={"description": "KG 遗忘评分: 近因性权重", "group": "遗忘算法"},
    )
    forgetting_kg_weight_structure: float = field(
        default=0.40,
        metadata={"description": "KG 遗忘评分: 结构重要性权重", "group": "遗忘算法"},
    )
    forgetting_kg_weight_confidence: float = field(
        default=0.15,
        metadata={"description": "KG 遗忘评分: 置信度权重", "group": "遗忘算法"},
    )
    forgetting_kg_weight_verification: float = field(
        default=0.30,
        metadata={"description": "KG 遗忘评分: 验证度权重", "group": "遗忘算法"},
    )
    forgetting_lambda_kg: float = field(
        default=0.05,
        metadata={
            "description": "KG 近因性衰减系数（比 L2 更慢）",
            "group": "遗忘算法",
        },
    )

    enable_context_logging: bool = field(
        default=False,
        metadata={"description": "启用 LLM 上下文日志输出", "group": "LLM 调用管理"},
    )

    l2_similarity_threshold: float = field(
        default=0.87,
        metadata={"description": "L2 去重相似度阈值", "group": "L2 记忆"},
    )
    l2_timeout_ms: int = field(
        default=4000,
        metadata={"description": "L2 检索超时(ms)", "group": "L2 记忆"},
    )
    l2_hybrid_rrf_k: int = field(
        default=60,
        metadata={
            "description": "混合检索 RRF 融合常数 k，越大则两路排名差距越平滑",
            "group": "L2 记忆",
        },
    )
    l2_hybrid_keyword_pool_factor: int = field(
        default=3,
        metadata={
            "description": "混合检索关键词路候选池倍数（候选数 = top_k × 倍数）",
            "group": "L2 记忆",
        },
    )
    l2_hybrid_enable_fts_debug: bool = field(
        default=False,
        metadata={
            "description": "混合检索时输出两路命中明细到日志",
            "group": "L2 记忆",
        },
    )
    l2_hybrid_global_scope_factor: float = field(
        default=0.8,
        metadata={
            "description": "全局共享记忆（scope=global）的 RRF 融合分降权系数，1 表示不降权",
            "group": "L2 记忆",
        },
    )
    l2_enable_hit_reinforcement: bool = field(
        default=True,
        metadata={
            "description": "对话检索命中时渐近提升记忆重要度（梦境扫描不触发）",
            "group": "L2 记忆",
        },
    )
    l2_hit_reinforcement_step: float = field(
        default=0.1,
        metadata={
            "description": "命中强化步长：I' = I + step·(1-I)，渐近自限",
            "group": "L2 记忆",
        },
    )
    l2_archive_retention_days: int = field(
        default=30,
        metadata={
            "description": "归档记忆保留天数，超期由梦境自动清除",
            "group": "L2 记忆",
        },
    )

    # L3 知识图谱参数
    l3_timeout_ms: int = field(
        default=1500,
        metadata={"description": "L3 检索超时(ms)", "group": "L3 知识图谱"},
    )
    l3_expansion_depth: int = field(
        default=2,
        metadata={"description": "图谱检索路径扩展深度", "group": "L3 知识图谱"},
    )
    l3_enable_type_whitelist: bool = field(
        default=True,
        metadata={"description": "启用 LLM 实体类型白名单约束", "group": "L3 知识图谱"},
    )
    l3_max_inject_tokens: int = field(
        default=600,
        metadata={
            "description": "知识图谱注入上下文最大 token 数",
            "group": "L3 知识图谱",
        },
    )
    node_confidence_threshold: float = field(
        default=0.3,
        metadata={"description": "节点最低置信度", "group": "L3 知识图谱"},
    )
    forgetting_threshold_kg: float = field(
        default=0.2,
        metadata={"description": "知识图谱遗忘阈值", "group": "L3 知识图谱"},
    )
    kg_retention_days: int = field(
        default=30,
        metadata={"description": "知识图谱保留天数", "group": "L3 知识图谱"},
    )

    # LLM 调用管理参数
    call_log_max_entries: int = field(
        default=1000,
        metadata={"description": "调用日志最大保留条数", "group": "LLM 调用管理"},
    )
    llm_global_concurrency: int = field(
        default=4,
        metadata={"description": "全插件 LLM 最大并发", "group": "LLM 调用管理"},
    )
    llm_provider_concurrency: int = field(
        default=2,
        metadata={"description": "单 Provider 最大并发", "group": "LLM 调用管理"},
    )
    llm_provider_background_concurrency: int = field(
        default=1,
        metadata={"description": "单 Provider 后台最大并发", "group": "LLM 调用管理"},
    )
    llm_provider_rpm: int = field(
        default=30,
        metadata={"description": "单 Provider 每分钟调用上限，0 关闭", "group": "LLM 调用管理"},
    )
    llm_provider_min_interval_ms: int = field(
        default=0,
        metadata={"description": "单 Provider 两次调用的最小间隔(ms)，0 关闭", "group": "LLM 调用管理"},
    )
    llm_queue_limit: int = field(
        default=500,
        metadata={"description": "LLM 等待队列上限", "group": "LLM 调用管理"},
    )
    llm_interactive_reserved_slots: int = field(
        default=1,
        metadata={"description": "为交互请求保留的并发槽位", "group": "LLM 调用管理"},
    )
    llm_circuit_failure_threshold: int = field(
        default=5,
        metadata={"description": "Provider 连续失败熔断阈值，0 关闭", "group": "LLM 调用管理"},
    )
    llm_circuit_open_seconds: int = field(
        default=60,
        metadata={"description": "Provider 熔断持续时间(秒)", "group": "LLM 调用管理"},
    )
    llm_priority_aging_seconds: int = field(
        default=30,
        metadata={"description": "低优先级等待多久提升一级(秒)", "group": "LLM 调用管理"},
    )
    llm_queue_timeout_ms: int = field(
        default=300000,
        metadata={"description": "等待 LLM 调用资格的超时(ms)，0 不限制", "group": "LLM 调用管理"},
    )
    llm_provider_timeout_ms: int = field(
        default=60000,
        metadata={"description": "Provider 实际调用超时(ms)，0 不限制", "group": "LLM 调用管理"},
    )
    llm_total_timeout_ms: int = field(
        default=360000,
        metadata={"description": "排队加 Provider 调用的整体超时(ms)，0 不限制", "group": "LLM 调用管理"},
    )
    llm_call_timeout_ms: int = field(
        default=60000,
        metadata={
            "description": "旧版 Provider 超时兼容项；llm_provider_timeout_ms 未配置时使用",
            "group": "LLM 调用管理",
        },
    )

    # 画像首次/后续分析证据门槛
    profile_first_mid_min_summaries: int = field(
        default=3,
        metadata={"description": "新画像首次中期分析所需总结次数", "group": "画像系统"},
    )
    profile_first_long_min_summaries: int = field(
        default=10,
        metadata={"description": "新画像首次长期分析所需总结次数", "group": "画像系统"},
    )
    profile_long_min_new_summaries: int = field(
        default=3,
        metadata={"description": "后续长期分析所需最小新增总结证据", "group": "画像系统"},
    )
    profile_batch_size: int = field(
        default=8,
        metadata={"description": "单次画像分析最多包含的用户数", "group": "画像系统"},
    )
    profile_batch_user_messages: int = field(
        default=12,
        metadata={"description": "批量画像中每个用户最多保留的近期消息数", "group": "画像系统"},
    )

    # 运行日志参数
    run_log_enabled: bool = field(
        default=True,
        metadata={
            "description": "启用运行日志（LLM 调用 / 上下文注入 / 主动回复）",
            "group": "运行日志",
        },
    )
    run_log_max_entries: int = field(
        default=100,
        metadata={
            "description": "运行日志每类保留的最新条数",
            "group": "运行日志",
        },
    )
    run_log_content_max_chars: int = field(
        default=2000,
        metadata={
            "description": "运行日志单字段最大字符数，超出截断并保留原始长度",
            "group": "运行日志",
        },
    )

    # 梦境任务参数
    dream_task_interval_hours: int = field(
        default=24,
        metadata={"description": "梦境任务间隔(小时)", "group": "梦境任务"},
    )
    dream_consolidation_similarity_threshold: float = field(
        default=0.85,
        metadata={"description": "合并相似度阈值", "group": "梦境任务"},
    )
    dream_consolidation_batch_size: int = field(
        default=5,
        metadata={"description": "合并批处理大小", "group": "梦境任务"},
    )
    dream_consolidation_scan_budget: int = field(
        default=200,
        metadata={"description": "每轮扫描记忆条数上限", "group": "梦境任务"},
    )
    dream_consolidation_query_batch_size: int = field(
        default=50,
        metadata={"description": "向量检索批量查询大小", "group": "梦境任务"},
    )
    dream_consolidation_query_top_k: int = field(
        default=5,
        metadata={
            "description": "合并阶段向量检索每条返回的近邻数",
            "group": "梦境任务",
        },
    )
    dream_consolidation_max_group_size: int = field(
        default=5,
        metadata={"description": "单组合并最大条目数", "group": "梦境任务"},
    )
    dream_temporal_anchor_batch_size: int = field(
        default=50,
        metadata={"description": "时间锚定批处理大小", "group": "梦境任务"},
    )
    dream_contradiction_similarity_floor: float = field(
        default=0.55,
        metadata={"description": "矛盾检测相似度下限", "group": "梦境任务"},
    )
    dream_contradiction_similarity_ceiling: float = field(
        default=0.85,
        metadata={"description": "矛盾检测相似度上限", "group": "梦境任务"},
    )
    dream_contradiction_max_groups: int = field(
        default=20,
        metadata={"description": "矛盾检测最大分组数", "group": "梦境任务"},
    )
    dream_contradiction_scan_budget: int = field(
        default=200,
        metadata={"description": "矛盾检测每轮扫描记忆条数上限", "group": "梦境任务"},
    )
    dream_contradiction_query_batch_size: int = field(
        default=50,
        metadata={"description": "矛盾检测向量检索批量查询大小", "group": "梦境任务"},
    )
    dream_contradiction_llm_batch_size: int = field(
        default=5,
        metadata={"description": "单次 LLM 判断的矛盾候选组数", "group": "梦境任务"},
    )
    dream_pattern_sample_size: int = field(
        default=30,
        metadata={"description": "模式发现采样数", "group": "梦境任务"},
    )
    dream_pattern_min_confidence: str = field(
        default="medium",
        metadata={"description": "模式发现最低置信度", "group": "梦境任务"},
    )
    dream_knowledge_extract_min_unprocessed: int = field(
        default=10,
        metadata={"description": "最小未处理记忆数量阈值", "group": "梦境任务"},
    )
    dream_knowledge_extract_batch_size: int = field(
        default=20,
        metadata={"description": "每批处理记忆数", "group": "梦境任务"},
    )
    eviction_batch_size: int = field(
        default=100,
        metadata={"description": "淘汰批处理大小", "group": "梦境任务"},
    )
    image_cache_cleanup_interval_hours: int = field(
        default=24,
        metadata={"description": "图片缓存清理任务间隔(小时)", "group": "梦境任务"},
    )

    # L3 知识图谱提取 - 相关记忆检索权重
    kg_extraction_semantic_weight: float = field(
        default=0.5,
        metadata={"description": "语义相似记忆权重", "group": "L3 知识图谱"},
    )
    kg_extraction_same_group_weight: float = field(
        default=0.3,
        metadata={"description": "同群聊记忆权重", "group": "L3 知识图谱"},
    )
    kg_extraction_same_user_weight: float = field(
        default=0.2,
        metadata={"description": "同用户记忆权重", "group": "L3 知识图谱"},
    )

    # 画像系统参数
    profile_max_messages_for_analysis: int = field(
        default=50,
        metadata={"description": "群聊画像分析时最大消息数", "group": "画像系统"},
    )
    profile_max_messages_for_user_analysis: int = field(
        default=30,
        metadata={"description": "用户画像分析时最大消息数", "group": "画像系统"},
    )
    profile_mid_update_interval_summaries: int = field(
        default=5,
        metadata={"description": "中期更新: 每隔N次总结触发", "group": "画像系统"},
    )
    profile_mid_update_interval_hours: float = field(
        default=24.0,
        metadata={"description": "中期更新: 最短间隔(小时)", "group": "画像系统"},
    )
    profile_long_update_interval_hours: float = field(
        default=168.0,
        metadata={"description": "长期更新: 最短间隔(小时)", "group": "画像系统"},
    )
    profile_favorability_max_delta: float = field(
        default=20.0,
        metadata={"description": "好感度单次更新最大变化量（夹紧用）", "group": "画像系统"},
    )
    profile_message_max_chars: int = field(
        default=150,
        metadata={"description": "画像分析时单条消息最大字符数（0 不截断）", "group": "画像系统"},
    )

    # 图片解析（从 _conf_schema.json 迁移）
    image_max_parse_per_request: int = field(
        default=3,
        metadata={
            "description": "单次请求最大图片解析数（与并发批次对齐）",
            "group": "图片处理",
        },
    )
    image_max_concurrent_parse: int = field(
        default=3,
        metadata={"description": "最大并发图片解析数", "group": "图片处理"},
    )
    image_queue_limit: int = field(
        default=200,
        metadata={"description": "图片解析后台队列上限", "group": "图片解析"},
    )
    image_cache_retention_days: int = field(
        default=7,
        metadata={"description": "图片解析结果缓存保留天数", "group": "图片处理"},
    )
    image_skip_on_passive_trigger: bool = field(
        default=True,
        metadata={"description": "被动触发时跳过图片解析", "group": "图片处理"},
    )
    image_parse_timeout_ms: int = field(
        default=30000,
        metadata={
            "description": "单次请求图片解析整体超时(ms)，0 表示不限制",
            "group": "图片处理",
        },
    )

    # 图片去重参数
    image_phash_enable: bool = field(
        default=True,
        metadata={"description": "启用 pHash 感知哈希去重", "group": "图片处理"},
    )
    image_phash_threshold: int = field(
        default=10,
        metadata={"description": "pHash 汉明距离阈值", "group": "图片处理"},
    )

    # 无效图过滤参数
    image_filter_enable: bool = field(
        default=True,
        metadata={"description": "启用无效图过滤(纯色/过小)", "group": "图片处理"},
    )
    image_filter_min_size: int = field(
        default=16,
        metadata={"description": "最小图片尺寸(像素)", "group": "图片处理"},
    )
    image_filter_std_threshold: float = field(
        default=5.0,
        metadata={"description": "纯色检测标准差阈值", "group": "图片处理"},
    )

    # 输入清理参数
    input_sanitizer_enable: bool = field(
        default=True,
        metadata={"description": "启用 Prompt 注入过滤", "group": "输入清理"},
    )
    input_sanitizer_max_length: int = field(
        default=10000,
        metadata={"description": "输入最大长度", "group": "输入清理"},
    )

    # 遗忘确认参数
    forgetting_llm_confirm_enable: bool = field(
        default=False,
        metadata={"description": "启用 LLM 最终兜底确认遗忘", "group": "遗忘确认"},
    )
    forgetting_llm_confirm_provider: str = field(
        default="",
        metadata={
            "description": "确认使用的 Provider(空则使用默认)",
            "group": "遗忘确认",
        },
    )
    forgetting_llm_confirm_threshold: float = field(
        default=0.15,
        metadata={"description": "评分低于此值才触发 LLM 确认", "group": "遗忘确认"},
    )

    # L2 查询改写参数
    l2_query_rewrite_enable: bool = field(
        default=False,
        metadata={"description": "启用 L2 检索查询改写", "group": "L2 查询改写"},
    )
    l2_query_rewrite_provider: str = field(
        default="",
        metadata={
            "description": "查询改写使用的 Provider(空则使用默认)",
            "group": "L2 查询改写",
        },
    )
    l2_query_rewrite_timeout_ms: int = field(
        default=3000,
        metadata={"description": "查询改写超时(ms)", "group": "L2 查询改写"},
    )
    l2_query_rewrite_queue_timeout_ms: int = field(
        default=800,
        metadata={"description": "查询改写排队超时(ms)，超时回退原查询", "group": "L2 查询改写"},
    )
    l2_query_rewrite_cache_size: int = field(
        default=256,
        metadata={"description": "查询改写 LRU 缓存条数", "group": "L2 查询改写"},
    )
    l2_query_rewrite_inflight_limit: int = field(
        default=64,
        metadata={"description": "查询改写最大在途 singleflight 数", "group": "L2 查询改写"},
    )
    persona_evolution_retry_task_limit: int = field(
        default=100,
        metadata={"description": "人格迭代进程内退避定时任务上限", "group": "人格自迭代"},
    )

    # 上下文清理参数
    enable_legacy_cleanup: bool = field(
        default=False,
        metadata={
            "description": "启用旧版对话清理模式(完成后删除整个对话)，默认关闭。"
            "关闭时采用请求前清空 contexts 策略，保留对话 ID 以兼容主动回复",
            "group": "上下文控制",
        },
    )

    # 主动回复 - 触发门控
    reply_mute_start_hour: int = field(
        default=1,
        metadata={
            "description": "静音时段开始小时(0-23)，静音时段内不会触发任何主动回复",
            "group": "主动回复·触发门控",
        },
    )
    reply_mute_start_minute: int = field(
        default=0,
        metadata={
            "description": "静音时段开始分钟(0-59)",
            "group": "主动回复·触发门控",
        },
    )
    reply_mute_end_hour: int = field(
        default=7,
        metadata={
            "description": "静音时段结束小时(0-23)",
            "group": "主动回复·触发门控",
        },
    )
    reply_mute_end_minute: int = field(
        default=0,
        metadata={
            "description": "静音时段结束分钟(0-59)",
            "group": "主动回复·触发门控",
        },
    )
    reply_quality_threshold: float = field(
        default=0.2,
        metadata={
            "description": "消息质量阈值(0-1)，低于此值的消息不进入滑动窗口、不参与触发评估",
            "group": "主动回复·触发门控",
        },
    )
    reply_trigger_min_interval: int = field(
        default=30,
        metadata={
            "description": "触发最小间隔(秒)，同一群两次 LLM 决策调用之间的最短时间",
            "group": "主动回复·触发门控",
        },
    )
    reply_passive_watch_min_interval: int = field(
        default=600,
        metadata={
            "description": "同群两次 passive watch LLM 评估的最小间隔(秒)",
            "group": "主动回复·触发门控",
        },
    )
    reply_passive_watch_enabled: bool = field(
        default=False,
        metadata={
            "description": "启用被动回复后的 LLM 跟进评估（高成本，默认关闭）",
            "group": "主动回复·触发门控",
        },
    )
    reply_default_n: int = field(
        default=15,
        metadata={
            "description": "消息计数阈值 N，每收到 N 条有效消息触发一次采样评估",
            "group": "主动回复·触发门控",
        },
    )
    reply_default_t: int = field(
        default=30,
        metadata={
            "description": "时间间隔阈值 T(分钟)，距上次采样超过 T 分钟且有新消息时触发",
            "group": "主动回复·触发门控",
        },
    )

    # 主动回复 - 上下文与窗口
    reply_window_size: int = field(
        default=15,
        metadata={
            "description": "滑动窗口大小(条)，保留最近 N 条有效发言供决策参考",
            "group": "主动回复·上下文",
        },
    )
    reply_max_token: int = field(
        default=3000,
        metadata={
            "description": "上下文 Token 上限，提交给决策 LLM 的上下文最大 Token 数",
            "group": "主动回复·上下文",
        },
    )
    reply_follow_up_ttl: int = field(
        default=10,
        metadata={
            "description": "跟进锚点 TTL(分钟)，关注用户/关键词的存活时长，超时自动失效",
            "group": "主动回复·上下文",
        },
    )
    reply_follow_up_aggregate_window: int = field(
        default=6,
        metadata={
            "description": "跟进聚合窗口(秒)，锚点命中后等待此时间聚合多条消息再触发评估",
            "group": "主动回复·上下文",
        },
    )

    # 主动回复 - 频率提升
    reply_boost_factor: float = field(
        default=0.6,
        metadata={
            "description": "提升系数(<1.0)，回复后触发阈值临时乘以此值，越低越容易再次触发",
            "group": "主动回复·频率提升",
        },
    )
    reply_boost_duration: int = field(
        default=15,
        metadata={
            "description": "提升持续时间(分钟)，回复后 boost 效果的持续时长，之后线性衰减",
            "group": "主动回复·频率提升",
        },
    )
    reply_max_boosted_replies: int = field(
        default=5,
        metadata={
            "description": "最大连续 boost 次数，连续回复不超过此次数时享受完整 boost，超出后逐渐减弱",
            "group": "主动回复·频率提升",
        },
    )

    # 主动回复 - 主动发起
    proactive_check_interval: int = field(
        default=5,
        metadata={
            "description": "候选检查基准(分钟)，各群据此独立随机安排下一次概率评估",
            "group": "主动回复·主动发起",
        },
    )
    proactive_quiet_minutes: int = field(
        default=120,
        metadata={
            "description": "冷场节奏基准(分钟)，用于风险率计算；实际另有30–90分钟自适应硬静默底线",
            "group": "主动回复·主动发起",
        },
    )
    proactive_max_per_day: int = field(
        default=2,
        metadata={
            "description": "滚动24小时最大发起次数，避免零点统一重置形成固定时间槽",
            "group": "主动回复·主动发起",
        },
    )
    proactive_min_interval: int = field(
        default=360,
        metadata={
            "description": "两次发起最小间隔(分钟)，同一群两次主动发起之间的最短时间",
            "group": "主动回复·主动发起",
        },
    )
    proactive_drift_delay: int = field(
        default=15,
        metadata={
            "description": "话题结束发起延迟(分钟)，检测到话题结束后若持续静默此时间可提前发起",
            "group": "主动回复·主动发起",
        },
    )
    proactive_pending_timeout: int = field(
        default=30,
        metadata={
            "description": "发起接话等待(分钟)，发起后等待群友接话的超时时间",
            "group": "主动回复·主动发起",
        },
    )
    proactive_max_streak: int = field(
        default=2,
        metadata={
            "description": "无人接话上限(次)，连续发起无人接话达到此次数后当天不再发起",
            "group": "主动回复·主动发起",
        },
    )
    proactive_instruction: str = field(
        default="",
        metadata={
            "description": "发起话题倾向，自定义偏好说明如「多聊技术话题」，留空由 Iris 自行发挥",
            "group": "主动回复·主动发起",
        },
    )
    proactive_max_message_len: int = field(
        default=300,
        metadata={
            "description": "发起消息最大长度(字符)，超出将被截断",
            "group": "主动回复·主动发起",
        },
    )

    # 学习模块内部参数
    learning_inject_max_tokens: int = field(
        default=600,
        metadata={"description": "学习上下文注入最大 Token 数", "group": "学习模块"},
    )
    learning_inject_max_item_chars: int = field(
        default=200,
        metadata={
            "description": "学习注入单条条目最大字符数",
            "group": "学习模块",
        },
    )
    learning_few_shot_max: int = field(
        default=3,
        metadata={"description": "注入的对话样例最大条数", "group": "学习模块"},
    )
    learning_review_max_batches_per_drain: int = field(
        default=5,
        metadata={"description": "学习审查 Worker 每轮最多处理批次数", "group": "学习模块"},
    )
    learning_persona_review_max_batches_per_run: int = field(
        default=5,
        metadata={"description": "Persona 学习复审单轮最大批次数", "group": "学习模块"},
    )
    learning_persona_review_max_llm_calls_per_run: int = field(
        default=10,
        metadata={"description": "Persona 学习复审单轮 LLM 调用上限", "group": "学习模块"},
    )
    learning_persona_review_max_runtime_minutes: int = field(
        default=5,
        metadata={"description": "Persona 学习复审单轮运行上限(分钟)", "group": "学习模块"},
    )
    learning_pattern_top_n: int = field(
        default=5,
        metadata={"description": "注入的表达模式 Top-N 条数", "group": "学习模块"},
    )
    learning_jargon_window_days: int = field(
        default=14,
        metadata={"description": "暗语候选滚动统计窗口（天）", "group": "学习模块·暗语"},
    )
    learning_jargon_ngram_max: int = field(
        default=6,
        metadata={"description": "中文暗语候选最大字数", "group": "学习模块·暗语"},
    )
    learning_jargon_candidates_per_message: int = field(
        default=64,
        metadata={"description": "单条消息最多采集的暗语候选数", "group": "学习模块·暗语"},
    )
    learning_jargon_user_cooldown_minutes: int = field(
        default=30,
        metadata={"description": "同一用户重复贡献同一候选的冷却时间（分钟）", "group": "学习模块·暗语"},
    )
    learning_jargon_min_messages: int = field(
        default=6,
        metadata={"description": "暗语候选进入审查的最少有效消息数", "group": "学习模块·暗语"},
    )
    learning_jargon_min_users_small: int = field(
        default=2,
        metadata={"description": "小群暗语候选最少不同发送者数", "group": "学习模块·暗语"},
    )
    learning_jargon_min_users_large: int = field(
        default=3,
        metadata={"description": "活跃群暗语候选最少不同发送者数", "group": "学习模块·暗语"},
    )
    learning_jargon_large_group_users: int = field(
        default=10,
        metadata={"description": "启用活跃群发送者门槛的人数", "group": "学习模块·暗语"},
    )
    learning_jargon_max_single_user_ratio: float = field(
        default=0.6,
        metadata={"description": "单一发送者贡献占比上限", "group": "学习模块·暗语"},
    )
    learning_jargon_min_span_hours: float = field(
        default=6.0,
        metadata={"description": "普通候选最小传播时间跨度（小时）", "group": "学习模块·暗语"},
    )
    learning_jargon_fast_track_messages: int = field(
        default=8,
        metadata={"description": "快速传播通道最少消息数", "group": "学习模块·暗语"},
    )
    learning_jargon_fast_track_users: int = field(
        default=5,
        metadata={"description": "快速传播通道最少发送者数", "group": "学习模块·暗语"},
    )
    learning_jargon_substring_support_ratio: float = field(
        default=0.85,
        metadata={"description": "短词被长词支配的证据重合阈值", "group": "学习模块·暗语"},
    )
    learning_jargon_substring_count_ratio: float = field(
        default=0.8,
        metadata={"description": "短词被长词支配的频次比例阈值", "group": "学习模块·暗语"},
    )
    learning_jargon_llm_batch_size: int = field(
        default=12,
        metadata={"description": "单次 LLM 暗语审查最大候选簇数", "group": "学习模块·暗语"},
    )
    learning_jargon_llm_trigger_size: int = field(
        default=8,
        metadata={"description": "立即触发批量暗语审查的候选簇数", "group": "学习模块·暗语"},
    )
    learning_jargon_llm_max_wait_hours: float = field(
        default=6.0,
        metadata={"description": "不足一批时候选最长等待时间（小时）", "group": "学习模块·暗语"},
    )
    learning_jargon_llm_min_interval_hours: float = field(
        default=6.0,
        metadata={"description": "两次暗语 LLM 调用最小间隔（小时）", "group": "学习模块·暗语"},
    )
    learning_jargon_llm_daily_limit: int = field(
        default=4,
        metadata={"description": "暗语审查每日 LLM 调用硬上限", "group": "学习模块·暗语"},
    )
    learning_jargon_max_clusters_per_group: int = field(
        default=4,
        metadata={"description": "单批次每个群最多审查的候选簇数", "group": "学习模块·暗语"},
    )
    learning_jargon_approve_confidence: float = field(
        default=0.85,
        metadata={"description": "暗语自动批准最低置信度", "group": "学习模块·暗语"},
    )
    learning_jargon_reject_confidence: float = field(
        default=0.75,
        metadata={"description": "非暗语自动拒绝最低置信度", "group": "学习模块·暗语"},
    )
    learning_jargon_retry_days: int = field(
        default=7,
        metadata={"description": "不确定候选再次审查的最短等待天数", "group": "学习模块·暗语"},
    )
    learning_jargon_retry_evidence_multiplier: float = field(
        default=2.0,
        metadata={"description": "不确定候选复审所需证据增长倍数", "group": "学习模块·暗语"},
    )
    learning_jargon_max_llm_attempts: int = field(
        default=2,
        metadata={"description": "单个暗语候选最多 LLM 审查次数", "group": "学习模块·暗语"},
    )
    learning_jargon_candidate_expire_days: int = field(
        default=30,
        metadata={"description": "无新证据候选自动过期天数", "group": "学习模块·暗语"},
    )
    learning_jargon_rejected_retention_days: int = field(
        default=60,
        metadata={"description": "被拒候选保留与冷却天数", "group": "学习模块·暗语"},
    )
    learning_jargon_dormant_days: int = field(
        default=60,
        metadata={"description": "正式暗语无使用转休眠的天数", "group": "学习模块·暗语"},
    )
    learning_jargon_max_candidates_per_group: int = field(
        default=3000,
        metadata={"description": "每群暗语候选存储上限", "group": "学习模块·暗语"},
    )

    # 人格自迭代 Job 创建默认值（文档 §15.1）
    persona_evolution_edit_mode: str = field(
        default="managed_block",
        metadata={
            "description": "新建 Job 默认编辑模式（managed_block 受控区块 / full_prompt 完整人格）",
            "group": "人格自迭代",
        },
    )
    persona_evolution_approval_mode: str = field(
        default="auto",
        metadata={
            "description": "新建 Job 默认审批模式（auto 自动发布 / manual 手动审批）",
            "group": "人格自迭代",
        },
    )
    persona_evolution_trigger_sample_count: int = field(
        default=100,
        metadata={
            "description": "自动触发所需新增有效语料数",
            "group": "人格自迭代",
        },
    )
    persona_evolution_min_interval_hours: int = field(
        default=24,
        metadata={"description": "距上次成功迭代的最短间隔(小时)", "group": "人格自迭代"},
    )
    persona_evolution_manual_min_samples: int = field(
        default=20,
        metadata={"description": "手动立即执行所需最低有效语料数", "group": "人格自迭代"},
    )
    persona_evolution_analysis_sample_size: int = field(
        default=60,
        metadata={"description": "单次分析的均衡抽样最大条数", "group": "人格自迭代"},
    )
    persona_evolution_full_prompt_max_change_ratio: float = field(
        default=0.20,
        metadata={
            "description": "完整人格模式单次字符改动率上限（不得超过 0.40）",
            "group": "人格自迭代",
        },
    )

    # 人格自迭代隐藏高级参数（文档 §15.2）
    persona_evolution_sample_store_chars: int = field(
        default=500,
        metadata={"description": "单条语料最大存储字符数", "group": "人格自迭代"},
    )
    persona_evolution_sample_prompt_chars: int = field(
        default=240,
        metadata={"description": "送模时单条语料最大字符数", "group": "人格自迭代"},
    )
    persona_evolution_sample_group_max_ratio: float = field(
        default=0.35,
        metadata={"description": "均衡抽样多群场景单群占比上限", "group": "人格自迭代"},
    )
    persona_evolution_sample_user_max_ratio: float = field(
        default=0.20,
        metadata={"description": "均衡抽样多用户场景单用户占比上限", "group": "人格自迭代"},
    )
    persona_evolution_min_confidence: float = field(
        default=0.65,
        metadata={"description": "阶段 A 风格画像最低置信度（低于则停止本轮）", "group": "人格自迭代"},
    )
    persona_evolution_block_max_chars: int = field(
        default=1500,
        metadata={"description": "受控区块最大字符数", "group": "人格自迭代"},
    )
    persona_evolution_full_max_growth_ratio: float = field(
        default=1.25,
        metadata={"description": "完整人格模式新人格最大增长率（相对原长度）", "group": "人格自迭代"},
    )
    persona_evolution_full_max_length: int = field(
        default=20000,
        metadata={"description": "完整人格模式人格绝对长度上限(字符)", "group": "人格自迭代"},
    )
    persona_evolution_max_reuse_chars: int = field(
        default=16,
        metadata={"description": "候选与语料直接连续复用的最长允许字符数", "group": "人格自迭代"},
    )
    persona_evolution_retry_intervals_minutes: str = field(
        default="30,120,360",
        metadata={
            "description": "Provider/网络失败重试间隔(分钟)，逗号分隔，次数即最大自动重试次数",
            "group": "人格自迭代",
        },
    )
    persona_evolution_circuit_breaker_threshold: int = field(
        default=3,
        metadata={"description": "连续解析/校验失败熔断次数（达到后 Job 转 paused_error）", "group": "人格自迭代"},
    )

    # 活人感 P0-1：收口旁路出站（修复项，不受 lifelike_enable 控制）
    lifelike_strip_bypass_tools: bool = field(
        default=True,
        metadata={
            "description": "从主管线 LLM 请求中剥离绕过 Iris 账本的内置工具"
            "（send_message_to_user / future_task）；false 恢复 AstrBot 默认挂载",
            "group": "活人感·出站",
        },
    )

    # 活人感 P0-8：主动安全底线（修复项，不受 lifelike_enable 控制）
    lifelike_proactive_quiet_hours: str = field(
        default="",
        metadata={
            "description": "主动发起的硬静音时段 HH:MM-HH:MM（可跨午夜）；空 = 沿用现网"
            "（reply_mute_* 静音设置），格式错误同样视为空",
            "group": "活人感·主动底线",
        },
    )
    lifelike_proactive_activity_gate: bool = field(
        default=True,
        metadata={
            "description": "活动窗口判定：当前时段历史上主人几乎不活跃、且最近一段时间没有互动时，"
            "推迟私聊主动发起（不是硬静音，主人醒着聊天时照常）",
            "group": "活人感·主动底线",
        },
    )
    lifelike_activity_recent_hours: float = field(
        default=6.0,
        metadata={
            "description": "活动窗口判定：最近多少小时内有过互动就不推迟",
            "group": "活人感·主动底线",
        },
    )
    lifelike_activity_lookback_days: int = field(
        default=28,
        metadata={"description": "活动窗口判定：统计主人作息的回看天数", "group": "活人感·主动底线"},
    )
    lifelike_activity_min_days: int = field(
        default=7,
        metadata={
            "description": "活动窗口判定：主人至少有多少个活跃日才用历史判定，不足时用先验时段",
            "group": "活人感·主动底线",
        },
    )
    lifelike_activity_inactive_ratio: float = field(
        default=0.1,
        metadata={
            "description": "活动窗口判定：某小时的活跃日占全部活跃日的比例低于此值视为几乎不活跃",
            "group": "活人感·主动底线",
        },
    )
    lifelike_activity_prior_inactive: str = field(
        default="01:00-08:00",
        metadata={
            "description": "活动窗口判定：历史数据不足时视为几乎不活跃的先验时段 HH:MM-HH:MM；"
            "空 = 数据不足时不推迟",
            "group": "活人感·主动底线",
        },
    )
    lifelike_proactive_empty_window: bool = field(
        default=False,
        metadata={
            "description": "窗口为空（如重启且主人 24h 未说话）时，用水位 + 出站账本判断最后互动时间，"
            "继续调度私聊主动；false（默认，首发保守）= 现网（窗口为空即不调度）。"
            "打开后重启当晚可能多出一条无上下文的主动私聊，先看回放再开",
            "group": "活人感·主动底线",
        },
    )
    # 活人感改造 P0-2（独立生效，不受 lifelike_enable 控制）
    lifelike_error_friendly_agent: bool = field(
        default=True,
        metadata={
            "description": "agent 异常时把 core 英文原始报错替换为中文友好文案（还需 extras.error_friendly.enable）",
            "group": "活人感",
        },
    )
    lifelike_adapter_watchdog: bool = field(
        default=True,
        metadata={
            "description": "适配器失联心跳告警：断开超过阈值写 ERROR 日志与 health 文件（不重启、不发消息）",
            "group": "活人感",
        },
    )
    lifelike_adapter_down_threshold_s: int = field(
        default=300,
        metadata={"description": "适配器断开多少秒后判定失联(秒，最少 60)", "group": "活人感"},
    )
    lifelike_adapter_watchdog_period_s: int = field(
        default=60,
        metadata={"description": "失联检查间隔(秒，最少 10)", "group": "活人感"},
    )

    # 活人感 P0-3：隐私（日志去正文、第三方原文保留期）；不受 lifelike_enable 控制
    lifelike_log_redact: bool = field(
        default=True,
        metadata={
            "description": "INFO 及以上日志不记聊天正文（适配器入站、core 收发行改为只记长度）；DEBUG 级不过滤",
            "group": "活人感·隐私",
        },
    )
    lifelike_retention_days_observed: int = field(
        default=30,
        metadata={
            "description": "群/频道第三方原文（observed_posts 中 author_kind=other）保留天数；0 = 不清理",
            "group": "活人感·隐私",
        },
    )
    lifelike_retention_dry_run: bool = field(
        default=True,
        metadata={
            "description": "保留期清理只计数写日志、不删除（主人批准实删前保持 true）",
            "group": "活人感·隐私",
        },
    )

    # 活人感改造（docs/lifelike-20261103，P0 开关各自独立生效，不受 lifelike_enable 控制）
    lifelike_session_extract_mode: str = field(
        default="off",
        metadata={
            "description": "会话批量抽取事项（P0-4）：off 关闭 / shadow 只记计数不写事项 / on 写入事项",
            "group": "活人感",
        },
    )
    lifelike_budget_cheap_calls_daily: int = field(
        default=80,
        metadata={
            "description": "活人感廉价模型每日调用硬上限（所有 lifelike_* 模块合计；0=禁止调用）",
            "group": "活人感",
        },
    )
    lifelike_budget_cheap_tokens_daily: int = field(
        default=150000,
        metadata={
            "description": "活人感廉价模型每日 token 硬上限（输入+输出，所有 lifelike_* 模块合计；0=禁止调用）",
            "group": "活人感",
        },
    )

    # 活人感 P0-5：数值泄漏守卫（修复项，不受 lifelike_enable 控制）
    lifelike_numeric_guard: bool = field(
        default=True,
        metadata={
            "description": "注入段中好感度/分值/百分比等数值化关系表达整行丢弃，并隔离画像好感度残留（false=恢复旧行为）",
            "group": "活人感",
        },
    )
    lifelike_emotion_hint: Literal["auto", "off", "hedged", "raw"] = field(
        default="auto",
        metadata={
            "description": "画像情感推测注入：auto=认知模块(lifelike_memory_mode)为 on 前不注入；off；hedged=带'可能'与未确认标注；raw=旧格式。守卫关闭时恒为 raw",
            "group": "活人感",
        },
    )

    # 活人感·风格审查（docs/lifelike-20261103 ROADMAP P0-6；P0 开关各自独立生效，不受 lifelike_enable 控制）
    lifelike_style_guard_mode: str = field(
        default="log",
        metadata={
            "description": "StyleGuard 风格审查（P0-6）：off 关闭 / log 只记规则 id 到 style_audit，不改写回复（P0 只开放到 log）",
            "group": "活人感",
        },
    )
    learning_cutesy_gate: bool = field(
        default=True,
        metadata={
            "description": "学习采集零 LLM 闸门（做作/过度卖萌/照搬/跟随一轮不入库）+ 对话审查新增 cutesy/catchphrase/copied 标准",
            "group": "活人感",
        },
    )
    learning_persona_review_anime: bool = field(
        default=False,
        metadata={
            "description": "Persona 复审追加动漫口吻/照搬/卖萌超范围两句标准（复审判不兼容会删除条目；只读副本试跑并经主人确认后再打开）",
            "group": "活人感",
        },
    )

    # 口吻风格层（ROADMAP P1-2 / P1-4，口吻附录 §4、§5.3；独立风格层，不受 lifelike_enable 控制）
    style_anime_register: str = field(
        default="medium",
        metadata={
            "description": "<iris:style> 口吻档位上限：off 不注入（改造前行为）/ light 淡 / medium 中 / strong 浓；实际档位 = min(场景档位, 本值)",
            "group": "活人感",
        },
    )
    style_group_register_cap: str = field(
        default="light",
        metadata={
            "description": "群聊口吻档位上限：off 群聊不注入 / light / medium（D5：群聊淡档）",
            "group": "活人感",
        },
    )
    style_cute_cap: str = field(
        default="occasional",
        metadata={
            "description": "卖萌档上限：off 不卖萌 / rare 很少 / occasional 偶尔（最高档）；和场景卖萌档取较低者，淡档恒不卖萌",
            "group": "活人感",
        },
    )
    style_group_unified: bool = field(
        default=False,
        metadata={
            "description": "口吻统一：true = 群聊与私聊用同一套场景档位（群里也可以偶尔俏皮，上限仍取 style_group_register_cap 与 style_cute_cap；"
            "认真求助 / 难过 / 问看法 / 被恶意对待时照样不卖萌；群→私聊关心在生病 / 心情差 / 危机时仍淡档）；false = 群聊用更收敛的一列（3c6e6e6）",
            "group": "活人感",
        },
    )
    style_casual_punctuation: bool = field(
        default=False,
        metadata={
            "description": "随性标点：true = 闲聊类回复（含主动消息）去掉每段末尾的句号，并改用随性语气版示例库；"
            "代码 / 列表 / 步骤 / 技术话题 / 长段说明不处理，问号感叹号省略号不动；false = 3c6e6e6 行为",
            "group": "活人感",
        },
    )
    style_examples_path: str = field(
        default="",
        metadata={
            "description": "风格示例库覆盖路径（JSON，结构同 iris_memory/style/style_examples_v1.json）；空 = 用代码自带种子库；读失败回退种子库",
            "group": "活人感",
        },
    )
    style_downgrade_on_feedback: bool = field(
        default=True,
        metadata={
            "description": "被说做作/装可爱/嗲/翻译腔后，本段对话保持淡档且不卖萌",
            "group": "活人感",
        },
    )
    style_inject_proactive: bool = field(
        default=True,
        metadata={
            "description": "主动消息 system 末尾追加 style 段（固定中档，群聊再取群上限），生成稿命中 StyleGuard 操纵句 MAN-* 时不发送（P1-4；需 style_anime_register 非 off）",
            "group": "活人感",
        },
    )

    # learning 反自我强化（ROADMAP P1-3，口吻附录 §5.5）
    learning_pattern_rotation: bool = field(
        default=True,
        metadata={
            "description": "表达模式注入改为加权随机 + 冷却轮换（仍按注入计数，不改表）；衰减的'未使用'判定窗口相应放宽，避免轮换后被误清空。false = 按命中数取前 N（旧行为）",
            "group": "活人感",
        },
    )
    learning_few_shot_scene_filter: bool = field(
        default=True,
        metadata={
            "description": "few-shot 按本轮场景过滤（技术/难过等淡档场景不注入浓档样例）+ 冷却轮换。false = 取最新 N 条（旧行为）",
            "group": "活人感",
        },
    )
    learning_expression_tail_particle_rule: bool = field(
        default=False,
        metadata={
            "description": "true = 保留旧的表达模式抽取（语气词结尾句专门收、反应词开头照收）；false = 剥掉句尾语气词后不足 4 字不收、纯反应词开头不收",
            "group": "活人感",
        },
    )
    learning_inject_paused: bool = field(
        default=False,
        metadata={
            "description": "只暂停 learning 注入（采集与审查照常）；漂移告警或人工处置时使用",
            "group": "活人感",
        },
    )

    # 活人感 P1-8：主动候选池 + 硬闸门 + 归因与退避（设计 §7）
    lifelike_initiative_mode: str = field(
        default="on",
        metadata={
            "description": "主动候选池（P1-8）：off = 只用事项候选（旧行为）/ shadow = 只计算闸门与排序并记录，不交给决策 / on = 合并候选池",
            "group": "活人感·主动",
        },
    )
    lifelike_pool_empty_window: bool = field(
        default=True,
        metadata={
            "description": "主动候选池（P1-8）：候选池里有通过闸门的可推送候选（如群→私聊关心）时，私聊窗口为空也允许调度（与 lifelike_proactive_empty_window 无关）；false = 空窗一律不调度（P0 行为）",
            "group": "活人感·主动",
        },
    )
    lifelike_push_daily_cap: int = field(
        default=3,
        metadata={"description": "主动推送（候选池）滚动 24 小时上限，另受现有每日主动上限约束", "group": "活人感·主动"},
    )
    lifelike_push_min_gap_hours: float = field(
        default=3.0,
        metadata={"description": "两次主动推送之间的最小间隔(小时)；上一条 6 小时无回复时翻倍", "group": "活人感·主动"},
    )
    lifelike_passive_only_types: str = field(
        default="life_share,group_topic",
        metadata={"description": "只顺口提、不推送的候选类型（逗号分隔）", "group": "活人感·主动"},
    )

    # 活人感 U8：主人私聊自由主动（用户拍板"私聊限制关掉，让她自由决定什么时候私聊我"）
    lifelike_proactive_freedom: bool = field(
        default=False,
        metadata={
            "description": "主人私聊自由主动：true = 条数上限 / 最小间隔 / 未回复暂停 / 静音与活动窗口推迟 / "
            "收尾冷却 / 候选池 push 上限与间隔 / 关心桥每日每周上限对主人私聊全部失效，改为定时评估、由她自己决定；"
            "仍保留同内容/同话题去重、硬底线、熔断、主人叫停、冷静期试探句与操纵/讨要回复句拦截（后者不随口吻开关关闭）；群聊不受影响。受 lifelike_enable 总开关控制",
            "group": "活人感·主动",
        },
    )
    lifelike_freedom_eval_minutes: int = field(
        default=20,
        metadata={"description": "自由主动：多久评估一次要不要主动发（分钟，±20% 抖动，5–120）", "group": "活人感·主动"},
    )
    lifelike_freedom_owner_quiet_minutes: int = field(
        default=10,
        metadata={"description": "自由主动：主人最后一条消息之后至少静默多久才评估（分钟，0–120；避免插话）", "group": "活人感·主动"},
    )
    lifelike_freedom_min_gap_minutes: int = field(
        default=10,
        metadata={"description": "自由主动技术安全网：两次主动之间的硬底线（分钟，5–240；防故障连发）", "group": "活人感·主动"},
    )
    lifelike_freedom_breaker_24h: int = field(
        default=20,
        metadata={"description": "自由主动技术安全网：主人最后一条消息之后、滚动 24 小时内的主动达到此条数即熔断并记 ERROR（主人一回话即归零；3–100）", "group": "活人感·主动"},
    )
    lifelike_freedom_hush_hours: float = field(
        default=12.0,
        metadata={"description": "自由主动：主人说「别发了 / 安静点 / 别烦我」后暂停主动的小时数（1–72；说「可以找我了」立即解除）", "group": "活人感·主动"},
    )

    # 活人感 U9：白名单群自由搭话（用户拍板"让她自主决定跟白名单群里的群友搭话吧，不要加限制了"）
    lifelike_group_freedom: bool = field(
        default=False,
        metadata={
            "description": "白名单群自由搭话：true = 平台 id_whitelist 里的群（不含频道 / silent_sessions / 主人频道）任何人的发言"
            "都在一阵消息停下来后交给她决定要不要搭话，群里安静一段时间后也可以自己开话题；取消冷却闸门 / 采样 / 频率上限 / "
            "仅点名回应 / 跟进时长 / 质量分 / 静音时段；保留去重、硬底线、连续无人说话熔断、群里叫停、私聊内容不进群、频道不发。"
            "受 lifelike_enable 总开关控制",
            "group": "活人感·群聊",
        },
    )
    lifelike_gf_debounce_seconds: int = field(
        default=45,
        metadata={"description": "群聊自由搭话：一阵消息停下来多少秒后合并判断一次（10–300；窗口内只做一次决策）", "group": "活人感·群聊"},
    )
    lifelike_gf_initiate_quiet_minutes: int = field(
        default=45,
        metadata={"description": "群聊自由搭话：群里安静多少分钟后才考虑自己开话题（10–720）", "group": "活人感·群聊"},
    )
    lifelike_gf_initiate_eval_minutes: int = field(
        default=45,
        metadata={"description": "群聊自由搭话：开话题评估节奏（分钟，±20% 抖动；15–360）", "group": "活人感·群聊"},
    )
    lifelike_gf_floor_seconds: int = field(
        default=120,
        metadata={"description": "群聊自由搭话技术安全网：同一群两次主动搭话之间至少间隔多少秒（被 @ / 回复 / 叫名字不受限；60–1800）", "group": "活人感·群聊"},
    )
    lifelike_gf_breaker_consecutive: int = field(
        default=2,
        metadata={"description": "群聊自由搭话熔断：她连续主动发言、中间没有任何人说话达到此条数 → 暂停主动直到有人说话（2–10）", "group": "活人感·群聊"},
    )
    lifelike_gf_hush_owner_hours: float = field(
        default=6.0,
        metadata={"description": "群聊：主人在群里叫她安静 / 别说话后暂停主动搭话的小时数（0.5–72；主人说「可以说话了」解除）", "group": "活人感·群聊"},
    )
    lifelike_gf_hush_member_hours: float = field(
        default=2.0,
        metadata={"description": "群聊：群友在群里对她说安静 / 别说话后暂停主动搭话的小时数（0.5–24）", "group": "活人感·群聊"},
    )
    lifelike_chat_drive: str = field(
        default="off",
        metadata={
            "description": "聊天欲望档位 off / low / medium / high：只在主人私聊自由主动（U8）/ 群聊自由搭话（U9）打开时生效——"
            "决策措辞里开口的倾向（off = 3c6e6e6 原措辞；low 略多一点；medium 看到能接的话题或想到对方就常说一句、仍挑时机；"
            "high 大多数时候倾向开口）、评估节奏（medium：私聊约 15 分钟、群安静约 30 分钟后考虑开话题）、她自己请求冷却的上限；"
            "去重 / 硬底线 / 熔断 / 叫停 / MAN 拦截不变。受 lifelike_enable 总开关控制",
            "group": "活人感·主动",
        },
    )
    lifelike_member_nickname: str = field(
        default="off",
        metadata={
            "description": "群友常用称呼 off / on：on = 从授权群公开发言里学群友的常用叫法（别人怎么叫 TA），学不到用规则从显示名生成简称；"
            "群聊与私聊提到群友时注入称呼表并要求不用完整显示名、不乱起外号（称呼级信息；墓碑 / 别记我的人不学）。受 lifelike_enable 总开关控制",
            "group": "活人感·群聊",
        },
    )
    lifelike_sticker_auto_learn: str = field(
        default="off",
        metadata={
            "description": "表情包自动学习 off / on（U19）：on = 主人在与她的私聊里发的贴纸（复用贴纸理解的描述缓存，不新增视觉调用）"
            "自动按 emoji + 描述推导标签与适用场合加入表情库（推不出时一次廉价模型调用，走活人感预算、模块日上限），"
            "并记下主人用它的场合概括（不存原话）；不学群友 / 群里 / 频道；容量见 lifelike_sticker_learn_max，"
            "超出淘汰主人最久没用的自动条目（手动 /sticker add 的永不淘汰）；/sticker learn on|off 可暂停。受 lifelike_enable 总开关控制",
            "group": "活人感·表情包",
        },
    )
    lifelike_sticker_use: str = field(
        default="off",
        metadata={
            "description": "表情包使用意愿 off / on（U19）：on = 新的表情包说明（标签 + 适用场合 + 主人常用场合，按主人近 7 天"
            "私聊发贴纸的多少给出很少 / 有时 / 经常的文字档位，与 lifelike_chat_drive 联动）；接受 [贴纸:标签] 写法与近似标签；"
            "论坛话题与主动消息也可配贴纸；同会话两张贴纸至少间隔 60 秒（防故障底线，off 时 120 秒）。受 lifelike_enable 总开关控制",
            "group": "活人感·表情包",
        },
    )
    lifelike_sticker_learn_groups: str = field(
        default="off",
        metadata={
            "description": "表情包也学主人在群里的用法 off / on（U20，需同时打开 lifelike_sticker_auto_learn）：on = 主人本人在白名单群"
            "（与群聊自由搭话资格相同：平台 id_whitelist 里的群，排除频道、silent_sessions、主人频道）里发的贴纸也学进表情库；"
            "群友的、转发的、频道里的一律不学；分别统计主人在私聊 / 群里发贴纸的多少与场合短语（只计数与短语，不存原话、不存群友信息），"
            "群聊说明的档位用主人在群里的频率；主人在群里亲自用过的贴纸可视为群里可用（真人 / 骂人 / 色情 / 政治等硬类别除外）。"
            "受 lifelike_enable 总开关控制",
            "group": "活人感·表情包",
        },
    )
    lifelike_sticker_learn_max: int = field(
        default=60,
        metadata={"description": "自动学到的表情包容量上限（10–200；手动添加的不计入、不淘汰）", "group": "活人感·表情包"},
    )
    identity_bot_names: str = field(
        default="",
        metadata={
            "description": "角色身份：她被叫的名字（逗号分隔，如 小白,xiaobai）。用于判断一句话是不是在说她、她是不是在用名字自称等；"
            "留空则这类规则只认\"你\"",
            "group": "角色身份",
        },
    )
    identity_owner_names: str = field(
        default="",
        metadata={
            "description": "角色身份：主人的称呼（逗号分隔，第一个作为默认称呼）。用于回怼时识别冲主人、称呼提示、风格守卫；留空则称\"主人\"",
            "group": "角色身份",
        },
    )
    lifelike_world_interests: str = field(
        default="",
        metadata={"description": "虚拟日常（lifelike_world_mode）里她的喜好，逗号分隔；留空则不提喜好", "group": "角色身份"},
    )
    persona_reference_file: str = field(
        default="",
        metadata={
            "description": "可选：参照角色文件（JSON，相对插件数据目录）。人设以某部作品的角色为性格参照但不许照搬时，"
            "在这里写作品识别正则与禁止照搬的口癖/设定梗；格式见 iris_memory/identity.py",
            "group": "角色身份",
        },
    )
    lifelike_counter_mode: str = field(
        default="off",
        metadata={
            "description": "被恶意对待时反击 off / on：on = 规则初筛出冲她（或冲主人）的难听话后，群里不等去抖立即回应、"
            "由回复模型分辨玩笑与恶意，恶意时简短有力地回击；底线由提示与发送前规则双重兜底（脏话 / 身份攻击 / 威胁 / "
            "隐私 / 性内容 / 煽动围攻一律删掉），同一人纠缠超过轮数上限后不再理会。受 lifelike_enable 总开关控制",
            "group": "活人感·群聊",
        },
    )
    lifelike_counter_owner_names: str = field(
        default="",
        metadata={"description": "反击：识别\"冲主人说难听话\"用的主人称呼（逗号分隔；另外回复主人消息也算）；留空 = 用 identity_owner_names", "group": "活人感·群聊"},
    )
    lifelike_counter_max_rounds: int = field(
        default=3,
        metadata={"description": "反击：同一个人 30 分钟内最多回应几轮恶意（最后一轮一句话收尾，之后不再理会；2–6；主人除外）", "group": "活人感·群聊"},
    )

    # 活人感 P1-12：群→私聊关心桥（设计 §7.9；D14 两个采集群授权、推送开）
    lifelike_care_bridge_mode: str = field(
        default="on",
        metadata={
            "description": "群→私聊关心桥：off 关闭 / shadow 只检测并记录 care_signals、不入队 / on 入候选池（推送还需 lifelike_initiative_mode=on）",
            "group": "活人感·关心桥",
        },
    )
    lifelike_care_bridge_groups: str = field(
        default="*",
        metadata={
            "description": "授权群 chat_id（逗号分隔）；* = continuity.observe_chat_ids 中的全部群。最终还要与 observe_chat_ids、平台 id_whitelist 取交集并去掉主人频道",
            "group": "活人感·关心桥",
        },
    )
    lifelike_care_bridge_categories: str = field(
        default="tired,sick,overtime,mood_low,good_news",
        metadata={"description": "关心类别（逗号分隔）", "group": "活人感·关心桥"},
    )
    lifelike_care_bridge_push: bool = field(
        default=True,
        metadata={"description": "false = 群里看到的近况只在她下次私聊时顺口提，不主动推送", "group": "活人感·关心桥"},
    )
    lifelike_care_include_owner_channel: bool = field(
        default=False,
        metadata={"description": "主人频道帖是否也作为关心来源（默认否：频道不算'群里'）", "group": "活人感·关心桥"},
    )
    lifelike_care_llm: bool = field(
        default=True,
        metadata={"description": "规则拿不准的句子是否送廉价模型复核（只送主人自己的话，计入每日预算）", "group": "活人感·关心桥"},
    )
    lifelike_care_llm_daily_cap: int = field(
        default=8,
        metadata={"description": "关心桥廉价模型每日调用上限（另受 lifelike_budget_cheap_* 总预算约束）", "group": "活人感·关心桥"},
    )

    # 活人感 P1-6 / P1-11：关系事件溯源 + 可恢复反馈 + 关系渐进（docs/lifelike-20261103 设计 §5）
    lifelike_relation_mode: str = field(
        default="shadow",
        metadata={
            "description": "关系事件溯源与可恢复反馈（P1-6）：off 不读写任何行（表由 continuity.db v3 迁移统一建好） / shadow 只记录事件与模式并写日志、不注入不改行为 / on 主人私聊注入<你们之间>，并接入主动候选池（U2：冷静期只影响语气/内容/排序，按 U8 不拦截、不减少主动）",
            "group": "活人感",
        },
    )
    lifelike_relation_levels: str = field(
        default="soften,boundary",
        metadata={
            "description": "可恢复反馈启用的级别（逗号分隔）；P1 只认 soften、boundary（拍板 D8，最高 2 级）",
            "group": "活人感",
        },
    )
    lifelike_relation_detector_daily_cap: int = field(
        default=10,
        metadata={
            "description": "关系检测廉价模型确认每日调用上限（另受 lifelike_budget_cheap_* 全局硬预算约束；0=只用规则，负向/倾诉事件不写）",
            "group": "活人感",
        },
    )
    lifelike_bond_mode: str = field(
        default="shadow",
        metadata={
            "description": "关系渐进（P1-11）：off / shadow 每日折叠只写 proposed 转移 / on 注入阶段行并驱动联动（熟悉的朋友起 life_share 可推送、私聊两格卖萌放宽到偶尔、趋同档位上限；关心桥推送不读阶段，U4）；实际取值不高于 lifelike_relation_mode",
            "group": "活人感",
        },
    )
    lifelike_bond_start_stage: str = field(
        default="s1",
        metadata={
            "description": "关系渐进起点阶段 s1..s4（s1=聊得来的网友，等于现网相处方式）",
            "group": "活人感",
        },
    )
    lifelike_bond_promote_approval: str = field(
        default="notify",
        metadata={
            "description": "阶段升降是否需主人批准：notify 直接生效只在 /iris_rel bond 可见 / manual 先写 proposed，/iris_rel bond approve 后生效",
            "group": "活人感",
        },
    )

    # 活人感 P1-7 用户认知 / forget / dream 归档（docs/lifelike-20261103 §6.2/6.5/6.6）
    lifelike_memory_mode: str = field(
        default="shadow",
        metadata={
            "description": "用户认知 user_beliefs（P1-7）：off 关闭 / shadow 不注入、默认也不改会话抽取 prompt（见 lifelike_memory_shadow_extract），只保留查看/forget 命令与维护 / on 随会话抽取记录并在主人私聊注入<你记得的她>",
            "group": "活人感",
        },
    )
    lifelike_memory_shadow_extract: bool = field(
        default=False,
        metadata={
            "description": "shadow 下是否把 beliefs 并入 P0-4 会话抽取（会改变现网抽取 prompt 与输出结构、增加 token）；false=shadow 时抽取 prompt 与 P0-4 原版一致",
            "group": "活人感",
        },
    )
    lifelike_memory_inference_inject: str = field(
        default="hedged",
        metadata={
            "description": "推测性认知注入：off 一律不注入 / hedged 只注入'较确定'以上并带'没确认过'措辞（仅 lifelike_memory_mode=on 时生效）",
            "group": "活人感",
        },
    )
    lifelike_dream_contradiction_archive: bool = field(
        default=True,
        metadata={
            "description": "梦境冲突消解：被替代的 L2 记忆归档并标 superseded（可恢复），不再硬删（false=旧行为硬删）",
            "group": "活人感",
        },
    )
    # 活人感 P1-13 群友认知（§6.8）与隐私收口 R1/R2（D16）
    lifelike_member_mode: str = field(
        default="shadow",
        metadata={
            "description": "群友认知（P1-13）：off 关闭 / shadow 只从授权群公开发言建档、不注入 / on 主人私聊提起群友时注入印象",
            "group": "活人感",
        },
    )
    lifelike_member_groups: str = field(
        default="",
        metadata={
            "description": "群友认知授权群（逗号分隔群 id；* = 全部采集群；*whitelist = 平台 id_whitelist 里的全部群，动态读取，可与显式 id 混写）；还须在 continuity.observe_chat_ids 中，不含主人频道/私聊；同一 Telegram 账号跨群合并为一份印象；空=不产生数据",
            "group": "活人感",
        },
    )
    lifelike_member_require_owner_present: str = field(
        default="auto",
        metadata={
            "description": "授权群是否还要求主人 60 天内在群里发过言：auto=显式群列表要求、通配（* / *whitelist）不要求 / true / false",
            "group": "活人感",
        },
    )
    lifelike_private_l2_filter: bool = field(
        default=True,
        metadata={
            "description": "私聊注入的 L2 群记忆只取授权群 / global，且过敏感过滤（隐私审查 R1；false=旧行为）",
            "group": "活人感",
        },
    )
    lifelike_private_l2_groups: str = field(
        default="",
        metadata={
            "description": "私聊 L2 允许的群（逗号分隔；支持 * / *whitelist，按群友认知同一授权规则解析）；空=沿用 lifelike_member_groups",
            "group": "活人感",
        },
    )
    lifelike_group_global_memory_guard: bool = field(
        default=True,
        metadata={
            "description": "群事件里禁止 LLM 自选 save_memory(scope=global)，全局作用域只认主人在 WebUI 设置（R2/D16；false=旧行为）",
            "group": "活人感",
        },
    )

    # 活人感 P1-5：生活系统（ROADMAP §2.1：P1/P2 子模块受 lifelike_enable 总开关控制）
    lifelike_enable: bool = field(
        default=True,
        metadata={
            "description": "活人感 P1/P2 总开关：false 时所有 P1/P2 lifelike_* 子模块视为 off（P0 修复项与 style_* 不受影响）",
            "group": "活人感",
        },
    )
    lifelike_world_mode: str = field(
        default="shadow",
        metadata={
            "description": "生活系统（P1-5）：off 关闭 / shadow 生成计划、推进状态、核对自述并记录，不注入 prompt / on 注入<她的今天>并落实核对",
            "group": "活人感",
        },
    )
    lifelike_world_claim_audit: bool = field(
        default=True,
        metadata={
            "description": "生活系统：回复/主动消息发出后核对她的自述（ClaimAuditor，主人私聊；廉价模型计入日预算）",
            "group": "活人感",
        },
    )
    lifelike_world_plan_llm: bool = field(
        default=True,
        metadata={
            "description": "生活系统：DayPlanner 每天用廉价模型提议一次（false=只用模板）",
            "group": "活人感",
        },
    )


@dataclass
class LearningConfig:
    """学习模块配置（用户可见选项）

    从群聊对话中学习表达风格、对话样例与圈内暗语，
    注入 LLM 上下文让回复更贴合群氛围。
    """

    enable: bool = False
    jargon_enable: bool = True
    review_provider: str = ""
    review_batch_size: int = 10
    pattern_max_count: int = 300
    pattern_decay_days: int = 15


@dataclass
class PersonaEvolutionConfig:
    """人格自迭代配置（用户可见选项）

    根据群聊真人发言的表达风格迭代 AstrBot Persona，
    采用"风格归纳 → 人格生成 → 确定性校验 → 发布"流水线，
    全程保留版本快照，支持逐字差异与回滚。
    """

    enable: bool = False
    provider: str = ""
    review_provider: str = ""
    sample_retention_days: int = 30
    sample_max_count: int = 20000


@dataclass
class ContinuityConfig:
    """连续性层配置（用户可见选项，与 _conf_schema.json 的 continuity 分组一致）

    运行时由 ``iris_memory.continuity.service.ContinuityConfig.from_config``
    统一做类型收敛与范围钳制；``enable=false`` 为软回滚。
    """

    enable: bool = True
    items_enable: bool = True
    proactive_topics_enable: bool = True
    closure_enable: bool = True
    batching_enable: bool = True
    channel_ingest_enable: bool = False
    digest_enable: bool = False
    observe_chat_ids: List[str] = field(default_factory=list)
    owner_channel_ids: List[str] = field(default_factory=list)
    merge_quiet_ms: int = 1200
    merge_cap_ms: int = 2000
    explicit_wait_timeout_s: int = 180
    closure_cooldown_minutes: int = 120
    inject_max_items: int = 3
    inject_max_chars: int = 600
    extract_debounce_s: int = 6
    extract_timeout_s: int = 30
    extract_queue_max: int = 50
    digest_interval_hours: int = 3
    sticker_understanding_enable: bool = True
    image_understanding_enable: bool = True
    sticker_send_enable: bool = True
    sticker_groups_enable: bool = False
    sticker_scope_unified: bool = False
    sticker_describe_daily_cap: int = 100


@dataclass
class Defaults:
    """所有默认配置的统一入口

    提供扁平化键名访问方法，支持 "l1_buffer.enable" 格式的键名。
    """

    l1_buffer: L1BufferConfig = field(default_factory=L1BufferConfig)
    l2_memory: L2MemoryConfig = field(default_factory=L2MemoryConfig)
    l3_kg: L3KGConfig = field(default_factory=L3KGConfig)
    profile: ProfileConfig = field(default_factory=ProfileConfig)
    isolation_config: IsolationConfig = field(default_factory=IsolationConfig)
    scheduled_tasks: ScheduledTasksConfig = field(default_factory=ScheduledTasksConfig)
    context_control: ContextControlConfig = field(default_factory=ContextControlConfig)
    extras: ExtrasConfig = field(default_factory=ExtrasConfig)
    learning: LearningConfig = field(default_factory=LearningConfig)
    persona_evolution: PersonaEvolutionConfig = field(
        default_factory=PersonaEvolutionConfig
    )
    continuity: ContinuityConfig = field(default_factory=ContinuityConfig)
    hidden: HiddenConfig = field(default_factory=HiddenConfig)

    def get_by_flat_key(self, flat_key: str) -> Optional[object]:
        """通过扁平化键名获取默认值

        Args:
            flat_key: 扁平化键名，支持两种格式：
                - "l1_buffer.enable" (用户配置)
                - "debug_mode" (隐藏配置)

        Returns:
            默认值，找不到返回 None

        Examples:
            >>> defaults = Defaults()
            >>> defaults.get_by_flat_key("l1_buffer.enable")
            True
            >>> defaults.get_by_flat_key("debug_mode")
            False
        """
        parts = flat_key.split(".")

        if len(parts) == 1:
            # 隐藏配置项(单层键名)
            return getattr(self.hidden, parts[0], None)
        elif len(parts) == 2:
            # 用户配置项(双层键名：section.key)
            section, key = parts
            section_config = getattr(self, section, None)
            if section_config is not None:
                return getattr(section_config, key, None)
        elif len(parts) == 3:
            # 嵌套配置项(三层键名：section.subsection.key)
            section, subsection, key = parts
            section_config = getattr(self, section, None)
            if section_config is not None:
                subsection_config = getattr(section_config, subsection, None)
                if subsection_config is not None:
                    return getattr(subsection_config, key, None)

        return None

    def get_section_defaults(self, section: str) -> Dict[str, object]:
        """获取指定配置分组的所有默认值

        Args:
            section: 配置分组名，如 "l1_buffer"

        Returns:
            配置字典

        Examples:
            >>> defaults = Defaults()
            >>> l1_defaults = defaults.get_section_defaults("l1_buffer")
            >>> print(l1_defaults["enable"])
            True
        """
        section_config = getattr(self, section, None)
        if section_config is None:
            return {}
        return asdict(section_config)
