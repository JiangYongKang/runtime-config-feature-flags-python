# runtime-config-feature-flags

基于 FastAPI 的**运行时可配置功能开关（Feature Flags）**服务。

- 多来源拼装最终配置，优先级明确；
- 值缺失 / 类型错误 / 取值越界分别给出**可区分的错误原因**，绝不静默回退；
- 配置可在服务运行期间**整批、原子更新**，请求只能看到更新前或更新后的完整配置；
- 按请求上下文（调用方名单、按比例灰度）评估，规则顺序固定、冲突结论确定；
- 同一请求内多次评估同一开关结果稳定一致。

## 目录结构

```
app/
  flags/
    models.py     # 数据模型：开关定义、不可变快照、评估上下文/结果、错误码
    errors.py     # ConfigError（带原因码）/ UnknownFlagError
    sources.py    # 三个来源：内置默认值 / JSON 文件 / 环境变量，含严格校验
    merge.py      # 字段级合并 + 组装不可变快照 + 缺失字段显式报错
    store.py      # 快照存储：锁保护的一次性原子替换
    evaluator.py  # 固定规则顺序评估 + SHA-256 确定性分桶
    manager.py    # 串联加载/更新/评估，请求级 ContextVar 缓存，判定日志
  deps.py         # FastAPI 依赖：manager 单例、按请求头构造上下文
main.py           # HTTP 接口与请求级缓存中间件
tests/            # 合并优先级 / 并发一致性 / 灰度边界 / HTTP 端到端
examples/flags.example.json
```

## 配置来源与优先级

优先级**从低到高**：

| 顺序 | 来源 | 说明 |
| --- | --- | --- |
| 1（最低） | 内置默认值 | 代码 `app/flags/sources.py::default_definitions()`，受版本管理 |
| 2 | JSON 配置文件 | 由环境变量 `FEATURE_FLAGS_CONFIG_FILE` 指定路径 |
| 3（最高） | 环境变量 | `FEATURE_FLAG_*`，无需重启即可……（注意：环境变量在**启动时**读取；运行中调整请走管理接口） |

合并粒度是**字段级**：高优先级来源只需给出关心的字段，未给出的字段沿用低层。
列表字段（`allowed_callers` / `denied_callers`）是**整体替换而非拼接**，避免"名单到底包含谁"产生歧义。

### 每个开关的字段

| 字段 | 类型 | 合法值 | 说明 |
| --- | --- | --- | --- |
| `status` | string | `"on"` / `"off"` | 基线开关；**必填**（可由任意低层提供） |
| `rollout_percentage` | int | `0..100` | 按调用方灰度比例，边界合法（0 全关、100 全开） |
| `allowed_callers` | string[] | — | 白名单（命中直接开启） |
| `denied_callers` | string[] | — | 黑名单（优先级最高，命中直接关闭） |
| `description` | string | — | 说明文字 |

### 环境变量命名

```
FEATURE_FLAG_<NAME>                 = on/off     -> status
FEATURE_FLAG_<NAME>_PERCENTAGE      = 0..100      -> rollout_percentage
FEATURE_FLAG_<NAME>_ALLOW           = a,b         -> allowed_callers（逗号分隔，整体替换）
FEATURE_FLAG_<NAME>_DENY            = a,b         -> denied_callers
FEATURE_FLAG_<NAME>_DESC            = 文本         -> description
```

`<NAME>` 取小写后作为开关名。例如：

```bash
export FEATURE_FLAG_NEW_CHECKOUT=on
export FEATURE_FLAG_NEW_CHECKOUT_PERCENTAGE=20
export FEATURE_FLAG_NEW_CHECKOUT_ALLOW=qa-team,beta-partner
```

### 错误不静默：三类原因码

来源解析与更新校验阶段，只要字段出现就必须合法：

| 错误码 (`ErrorCode`) | 触发场景 |
| --- | --- |
| `missing_value` | 合并完所有来源后，开关仍缺少必填的 `status` |
| `type_error` | 字段类型不对（如比例给了字符串/布尔、名单不是字符串列表、JSON 顶层不是对象、未知字段） |
| `value_out_of_range` | 取值越界（`status` 非 on/off、比例不在 `[0,100]`、env 状态值非法） |
| `invalid_source` | 配置文件不存在或 JSON 非法 |
| `unknown_flag` | 评估了一个未声明的开关（HTTP 404） |

启动时任一来源非法都会**直接启动失败**；管理接口更新时任一项非法会**整批拒绝**（HTTP 400，响应含 `error/path/source`），继续沿用旧配置。

## 评估规则与兼容/冲突约定

规则顺序**写死**在 `app/flags/evaluator.py`，不随配置变化，因此结论确定、可解释：

1. `denied_callers` 命中 → **关闭**，`reason=deny_list`（即使同时在白名单里，冲突时以关闭为准）
2. `allowed_callers` 命中 → **开启**，`reason=allow_list`（不受灰度影响，`bucket=-1`）
3. `status=off` 且不在名单中 → **关闭**，`reason=baseline_off`（灰度不生效）
4. `status=on`：对 `(开关名, 调用方)` 做 SHA-256 稳定哈希取桶 `bucket∈[0,100)`
   - `bucket < rollout_percentage` → 开启，`reason=rollout_in_bucket`
   - 否则关闭，`reason=rollout_out_bucket`

兼容性/确定性约定：

- 分桶不使用随机种子或线程局部状态，同一 `(开关, 调用方)` 在任意进程/时间结果相同；
  不同开关之间分桶相互独立。
- 比例 `0` 与 `100` 不做特判，由同一个 `bucket < pct` 比较自然覆盖边界。
- **同一请求内**的评估结果缓存在 `contextvars.ContextVar` 中（见请求中间件）：
  即使请求处理途中配置被更新，本请求内多次评估同一开关也始终返回首次结论；
  请求结束后新请求自然看到新版本。

## 运行时更新（原子、整批）

`PATCH /admin/flags`，请求体：

```json
{
  "partial": true,
  "flags": {
    "new_checkout": { "rollout_percentage": 50 }
  }
}
```

- `partial=true`（默认）：字段级合并进当前配置；
- `partial=false`：`flags` 必须是完整配置，整体替换。

实现方式：先在锁外完成**全部**校验并构造好新的不可变 `FlagSnapshot`，
全部合法后才在锁内做一次引用替换。任何读请求拿到的都是一整份冻结快照，
不存在"更新一半"的中间态；任一项非法则整批拒绝、版本号不前进、旧配置继续生效。

## HTTP 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/` | 健康检查 |
| GET | `/flags` | 当前生效配置（含 `version`） |
| GET | `/flags/{name}` | 评估开关，返回 `enabled/reason/bucket/version` |
| PATCH | `/admin/flags` | 整批原子更新配置 |

请求头：`X-Caller`（调用方标识，用于名单与灰度分桶）、`X-Request-Id`（可选）。

```bash
curl -s localhost:8000/flags/new_checkout -H 'X-Caller: qa-team'
# {"flag":"new_checkout","enabled":true,"reason":"allow_list","bucket":-1,...}
```

## 本地验证

```bash
# 1. 安装依赖（需要 Python >= 3.14）
uv sync --dev

# 2. 跑全部单测（32 个）；判定日志会打印"输入 + 判定依据"
uv run pytest -q
# 想看详细判定日志：
uv run pytest -q -o log_cli=true -o log_cli_level=DEBUG

# 3. 用示例配置启动
FEATURE_FLAGS_CONFIG_FILE=examples/flags.example.json uv run uvicorn main:app --reload --port 8000

# 4. 手工验证
curl -s localhost:8000/flags | jq .
curl -s localhost:8000/flags/new_checkout -H 'X-Caller: qa-team' | jq .
curl -s -X PATCH localhost:8000/admin/flags \
  -H 'Content-Type: application/json' \
  -d '{"flags":{"new_checkout":{"rollout_percentage":101}}}'   # 越界 -> 400 整批拒绝
curl -s -X PATCH localhost:8000/admin/flags \
  -H 'Content-Type: application/json' \
  -d '{"flags":{"new_checkout":{"rollout_percentage":80}}}'    # 合法 -> version +1
```

测试覆盖对应需求：

- `tests/test_merge_priority.py`：来源优先级、字段级合并、三类错误原因可区分、判定日志；
- `tests/test_concurrent_update.py`：并发读写只见完整快照、版本严格连续、非法整批拒绝、请求内稳定；
- `tests/test_rollout_bounds.py`：比例 0/100/1/99 边界、分桶确定性、名单冲突结论；
- `tests/test_api.py`：HTTP 端到端。
