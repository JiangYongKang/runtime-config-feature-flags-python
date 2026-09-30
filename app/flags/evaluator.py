"""按请求上下文评估开关。

规则顺序固定（写死，不可由配置打乱），冲突时结论确定：

1. ``denied_callers`` 命中      -> 关闭，原因 ``deny_list``
2. ``allowed_callers`` 命中     -> 开启，原因 ``allow_list``
3. 按 (开关名, 调用方) 稳定哈希分桶，``bucket < rollout_percentage``
                                 -> 开启，原因 ``rollout_in_bucket``
   否则                          -> 关闭，原因 ``rollout_out_bucket``
4. 灰度比例为 0/100 的边界仍由同一规则统一处理：
   0 -> 无人入桶；100 -> 全部入桶。
5. 基线 status=off 且以上规则未开启 -> 关闭，原因 ``baseline_off``；
   status=on 但灰度把调用方挡在桶外时，以灰度结论为准（灰度是 on 的细化）。

为避免歧义，规则 3 只在 ``status=on`` 时参与放量：status=off 表示该开关
整体关闭，名单外一律关闭（原因 ``baseline_off``）。
"""

from __future__ import annotations

import hashlib

from .errors import UnknownFlagError
from .models import Evaluation, EvaluationContext, FlagSnapshot


def stable_bucket(flag: str, caller: str) -> int:
    """把 (开关名, 调用方) 确定性地哈希到 [0, 100) 桶。

    使用 SHA-256 前 8 字节取模，不使用随机种子、不使用线程局部状态，
    因此同一 (开关, 调用方) 在任意进程、任意时刻结果一致。
    """
    digest = hashlib.sha256(f"{flag}\x1f{caller}".encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") % 100


def evaluate(snapshot: FlagSnapshot, flag: str, ctx: EvaluationContext) -> Evaluation:
    """按固定规则顺序评估，返回带原因的结论。未声明的开关抛 UnknownFlagError。"""
    defn = snapshot.flags.get(flag)
    if defn is None:
        raise UnknownFlagError(
            f"开关 {flag!r} 未在当前配置(version={snapshot.version})中声明"
        )

    caller = ctx.caller

    # 1) 黑名单优先：命中一律关闭，即使同时在白名单中（冲突结论确定=关闭）
    if caller and caller in defn.denied_callers:
        return Evaluation(
            flag=flag, enabled=False, reason="deny_list", bucket=-1
        )

    # 2) 白名单：直接开启（灰度与基线都不能把白名单调用方关掉）
    if caller and caller in defn.allowed_callers:
        return Evaluation(
            flag=flag, enabled=True, reason="allow_list", bucket=-1
        )

    # 3) 开关整体关闭：名单外一律关闭
    if defn.status.value == "off":
        return Evaluation(
            flag=flag, enabled=False, reason="baseline_off", bucket=-1
        )

    # 4) 按比例灰度（status=on 时的放量细化），边界 0/100 由同一比较自然覆盖
    bucket = stable_bucket(flag, caller)
    if bucket < defn.rollout_percentage:
        return Evaluation(
            flag=flag, enabled=True, reason="rollout_in_bucket", bucket=bucket
        )
    return Evaluation(
        flag=flag, enabled=False, reason="rollout_out_bucket", bucket=bucket
    )
