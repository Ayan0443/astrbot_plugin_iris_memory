"""口吻风格层（ROADMAP P1-2 / P1-4，口吻附录 §4、§5.2～5.3）。

- ``register``：零 LLM 场景档位判定（新函数，不改 ``learning.expression.classify_scene``）；
- ``examples``：原创风格示例库（``style_examples_v1.json``，版本化，跟代码一起评审）；
- ``selector``：``<iris:style>`` 段组装、冷却轮换、卖萌上限、被说做作后降档。

开关全部是 HiddenConfig 平铺键 ``style_*``，不受 ``lifelike_enable`` 控制
（ROADMAP §2.1）；``style_anime_register=off`` 即恢复改造前行为。
档位、卖萌档在 prompt 里只以文字出现（淡 / 中 / 浓，不卖萌 / 很少 / 偶尔），
阈值与词表只在代码里，不进 prompt。
"""
