# Runtime Feature Flags（运行时可配置的功能开关服务）

基于 FastAPI 的功能开关（feature flags）服务，支持：

- **多来源配置拼装**：内置默认值 → JSON 配置文件 → 环境变量，字段级覆盖，优先级明确；
- **严格校验、错误可区分**：值缺失 / 类型不对 / 取值越界 / 未知字段 / 规则冲突分别用不同 `reason` 上报，绝不悄悄退回默认值；
- **运行时原子更新**：`PUT /admin/config` 整批校验通过后一次性替换快照，非法内容整批拒绝、继续沿用旧配置；请求处理期间要么看到旧完整配置，要么看到新完整配置；
- **按请求上下文判定**：调用方黑白名单、按稳定哈希的百分比灰度，规则冲突时有确定性结论；同一请求内多次评估同一开关结果恒定；
- **判定可解释**：每次评估返回 `matched` 与逐步 `basis` 判定依据。

## 目录结构

```
app/
  errors.py     # 错误类型：reason 区分缺失/类型/越界/冲突等
  models.py     # FlagRule / FeatureFlagsConfig / Evaluation，严格校验
  sources.py    # 默认值、文件、环境变量三层来源与字段级合并
  manager.py    # ConfigManager：不可变快照 + 原子替换 + 版本号
  evaluator.py  # 确定性判定规则、稳定哈希灰度、请求内缓存
  api.py        # FastAPI 路由与请求级快照绑定
main.py         # 入口：启动时加载三层配置
tests/          # pytest 测试（合并优先级 / 并发一致性 / 灰度边界）
examples/flags.json
```

## 配置来源与优先级

优先级**从低到高**：

| 优先级 | 来源 | 说明 |
|---|---|---|
| 1（最低） | `defaults` | 代码内置默认值，见 `app/sources.py` 的 `DEFAULT_CONFIG`，永远生效 |
| 2 | `file` | JSON 文件，路径由环境变量 `FF_CONFIG_FILE` 指定（或直接传入）；**文件不存在视为该层缺省**；文件存在但不是合法 JSON 会报错 |
| 3（最高） | `env` | 环境变量 |

**合并是字段级而不是整开关级**：高层只覆盖它显式给出的字段，没给出的字段沿用低层。合并完成后还会做一次完整校验（包含跨层的名单冲突检查）。

配置文件格式（顶层必须有 `flags`）：

```json
{
  "flags": {
    "new_checkout": {
      "enabled": true,
      "allowed_callers": ["alice", "bob"],
      "denied_callers": ["mallory"],
      "rollout_percentage": 25
    }
  }
}
```

环境变量命名为 `FF_<开关名>_<字段>`（开关名大写、下划线分隔）：

```bash
export FF_CONFIG_FILE=examples/flags.json
export FF_NEW_CHECKOUT_ENABLED=false                  # 布尔只接受 true/false
export FF_NEW_CHECKOUT_ROLLOUT_PERCENTAGE=50          # 整数 0-100
export FF_NEW_CHECKOUT_ALLOWED_CALLERS=alice,bob      # 逗号分隔
export FF_NEW_CHECKOUT_DENIED_CALLERS=mallory
```

注意：

- 环境变量**赋空字符串**（`FF_X_ENABLED=`）视为非法值而不是“未配置”，防止误关功能；要取消设置请 `unset`；
- 无法识别的 `FF_` 前缀变量（字段后缀拼错）会报 `unknown_env_variable`，不会被静默忽略。

### 校验失败原因（reason）

| reason | 含义 |
|---|---|
| `missing_value` | 必填字段缺失（完整配置中字段不允许缺） |
| `invalid_type` | 类型错误，例如布尔给了字符串、比例给了小数 |
| `out_of_range` | 灰度比例不在 0-100 |
| `invalid_element` | 名单数组里有非字符串或空元素 |
| `unknown_field` | 出现规则字段之外的未知字段 |
| `invalid_flag_name` | 开关名不匹配 `^[a-z][a-z0-9_]*$` |
| `rules_conflict` | 同一调用方同时在白名单和黑名单中（含跨层合并后冲突） |
| `unknown_env_variable` | 无法识别的开关环境变量 |
| `invalid_structure` | 顶层缺少 `flags` 字段 |

## 判定规则与兼容语义

对每个请求，按以下**确定性**顺序判定（先命中先生效，无随机、无时钟依赖）：

1. 调用方在 `denied_callers` 黑名单 → **关闭**（黑名单优先级最高）；
2. 调用方在 `allowed_callers` 白名单 → **开启**（白名单优先于总闸，便于灰度期定向救火）；
3. `enabled = false` → **关闭**；
4. `rollout_percentage = 100` → **开启**（全量）；
5. `rollout_percentage = 0` → **关闭**；
6. 灰度：稳定键优先取 `caller_id`，缺失时退化为 `request_id`，计算
   `sha256("<flag>:<key>")` 前 12 个十六进制字符转整数后 `% 100` 得到桶号 `[0,99]`，
   桶号 `< rollout_percentage` 时开启；
7. 进入灰度但 `caller_id` 与 `request_id` 都缺失 → **关闭**（无法保证稳定，绝不随机放流量）。

兼容/边界规则：

- 比例语义是 **`bucket < percentage`**，因此 `0` 全关、`100` 全开，`1` 只有桶 0 命中；
- 同一调用方在同一开关上的桶号跨请求、跨进程恒定；
- 同一开关在不同开关名下独立哈希；
- 配置中不允许名单重叠，因此“白名单与黑名单冲突”在写入时就会被拒绝（`rules_conflict`），不存在运行时随机取舍；
- 评估不存在的开关返回 `404 unknown_flag`，而不是默认关闭，避免调用方把拼写错误当成“功能没开”。

## 一致性保证

- 配置快照（`FeatureFlagsConfig` / `FlagRule`）是 **frozen dataclass**；
- `ConfigManager.update()` 先在锁外完成解析与校验，通过后才在锁内做一次引用替换并递增版本号；
- 每个请求进入时由中间件绑定当时的快照到 `RequestFlagCache`，**请求处理期间不受后续更新影响**，同一请求内重复评估同一开关直接命中缓存；
- 运行时更新是**整份替换**语义：请求体必须包含每个开关的全部字段，缺字段按 `missing_value` 整批拒绝（不会与旧配置做隐式合并，避免“我明明没传这个字段为什么变了”）。

## HTTP 接口

| 方法/路径 | 说明 |
|---|---|
| `GET /healthz` | 存活探针，返回当前配置版本 |
| `GET /flags` | 列出全部开关当前规则 |
| `GET /flags/{name}` | 评估开关；上下文用查询参数 `caller_id` / `request_id` 或请求头 `X-Caller-Id` / `X-Request-Id`（查询参数优先） |
| `GET /admin/config` | 查看当前整份配置与版本号 |
| `PUT /admin/config` | 整份替换配置；成功返回新版本，非法返回 422 与逐条错误明细 |

评估响应示例：

```json
{
  "version": 2,
  "flag": "new_checkout",
  "on": true,
  "matched": "rollout_in",
  "caller_id": "carol",
  "request_id": "req-1",
  "basis": [
    "规则: enabled=True, rollout_percentage=50, allowed=['alice'], denied=['mallory']",
    "上下文: caller_id='carol', request_id='req-1'",
    "灰度判定: 稳定键来源=caller_id 值='carol' -> 桶号 34; 34 < 50 为 true"
  ]
}
```

## 本地验证

需要 Python 3.14+（或调整 `pyproject.toml` 中的 `requires-python`）与 [uv](https://docs.ast.astral.sh/uv/)。

```bash
# 1. 安装依赖
uv sync

# 2. 跑全部测试（-s 已在 pyproject 中配置，日志会打印每个用例的输入与判定依据）
uv run pytest

# 3. 启动服务（带示例配置与环境变量覆盖）
FF_CONFIG_FILE=examples/flags.json \
FF_NEW_CHECKOUT_ROLLOUT_PERCENTAGE=50 \
uv run uvicorn main:app --reload

# 4. 手动验证
curl -s localhost:8000/healthz
curl -s 'localhost:8000/flags/new_checkout' -H 'X-Caller-Id: carol'
curl -s -X PUT localhost:8000/admin/config \
  -H 'content-type: application/json' \
  -d '{"flags":{"new_checkout":{"enabled":true,"allowed_callers":[],"denied_callers":[],"rollout_percentage":100}}}'
# 非法更新（会整批拒绝并返回逐条原因，版本号不变）
curl -s -X PUT localhost:8000/admin/config \
  -H 'content-type: application/json' \
  -d '{"flags":{"new_checkout":{"enabled":"yes","rollout_percentage":200}}}'
```

测试覆盖：

- `tests/test_sources.py`：三层合并优先级、字段级覆盖、来源标注、各类非法输入的 reason 区分、跨层名单冲突；
- `tests/test_manager.py`：版本递增、非法整批拒绝沿用旧配置、8 读线程 × 更新风暴下的无撕裂读取、请求处理期间更新保持旧快照；
- `tests/test_evaluator.py`：判定优先级、桶号稳定性与范围、1%/99% 灰度边界、请求内缓存一致、稳定键退化与缺失；
- `tests/test_api.py`：HTTP 接口、上下文取值、404/422/400 错误形态、HTTP 层并发读更新一致性。
