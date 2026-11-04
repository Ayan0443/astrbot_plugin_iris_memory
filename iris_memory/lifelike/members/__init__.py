"""活人感 · 群友认知（ROADMAP P1-13，设计 §6.8）。

- ``store``：member_profiles / member_aliases / member_notes（``ensure_members_schema``）；
- ``ledger``：MemberImpressionLedger——从授权群公开发言（observed_posts）按规则生成轻量印象；
- ``lens``：MemberResolver（称呼匹配，MG-pull / MG-alias）与 MemberLens（注入，MG-scope）。
"""
