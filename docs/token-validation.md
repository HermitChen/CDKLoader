# Token 验证迁移说明

本次以本地 codex-manager 的“Token 验证”入口为准，追踪其接口、批量调度、账号验证、HTTP 会话、RT 刷新和 Token 字段补全。注册、重新登录、Session Token 换取 AT 和 Sentinel 获取属于独立功能，不在该入口的调用链中。

## 原提示的来源

`Token 验证暂不可用: 上游验证处于冷却期，请约 10 秒后重试` 是 CDK Loader 原验证器生成的提示，并非上游返回的原文。

旧实现把风控/限流响应计入进程共享计数，默认在 60 秒内累计 5 次后开启 60 秒冷却；剩余时间向上取整后显示在提示中。冷却期间新的账号检查会直接返回，不发起上游请求。因此“约 10 秒”表示本地冷却剩余时间，无法单凭这条提示判断原始上游错误、账号有效性或是否需要 Sentinel。

codex-manager 的 Token 验证没有这套全局冷却。迁移后已删除共享计数、冷却判断及对应运行参数；旧数据库中的这些参数不再参与配置加载。

## 实际调用链

参考项目的单账号 `POST /{account_id}/validate` 和批量 `POST /batch-validate` 位于 `src/web/routes/accounts.py`：

```text
页面 Token 验证
  → 单账号接口 / 批量接口
  → select_proxy_for_request（每次操作选一个出口）
  → validate_account_token_detailed
      → TokenRefreshManager.validate_token（当前 AT）
          → GET auth.openai.com/api/accounts/oauth/userinfo
          → POST chatgpt.com/backend-api/conversation/init
          → GET chatgpt.com/backend-api/accounts/check/v4-2023-04-27
      → 明确失效或缺少 AT 且有 RT：
          → refresh_by_oauth_token
          → POST auth.openai.com/oauth/token
          → normalize_token_artifacts
          → validate_token（新 AT，完整三阶段）
          → 补齐账号字段、保存轮换凭据、更新状态
  → _normalize_validation_response
  → 批量汇总 valid / invalid / inconclusive / recoverable
```

临时无法确认当前 AT 时立即保留账号原状态，不尝试消耗 RT。仅存在 Session Token 的账号在这个入口中不会自动重新登录或通过 Session Token 恢复。

## 请求与指纹

| 项目 | manager 行为及迁移结果 |
| --- | --- |
| 传输库 | `curl_cffi 0.17.0`，依赖由 `uv.lock` 固定 |
| 浏览器 | 解析当前库的 `chrome` 别名；本次为 `chrome152` |
| UA | Windows Chrome，主版本与 impersonate 一致 |
| 默认浏览器头 | 保持 curl_cffi 默认生成行为，不覆盖 JA3、Akamai 或 Client Hints |
| ChatGPT 版本头 | `oai-client-version: 26.1022.91109`，移除旧版额外 build-number 头 |
| 身份头 | Bearer AT、设备和会话 UUID；同一次 AT 检测的重试共用 UUID |
| Cookie | 每个新会话在 `chatgpt.com`、`.chatgpt.com` 设置 `oai-did` |
| 会话初始化 | 三个空字段 `gizmo_id`、`requested_default_model`、`conversation_id`，时区 `-480` |
| 账号检查 | 固定携带 `timezone_offset_min=-480` |
| 超时 | 每次请求 30 秒 |
| 临时失败 | 最多三次，间隔 0.25、0.75 秒；每次新建会话，从 userinfo 重跑完整链路 |
| 出口 | 当前 AT、RT 刷新、新 AT 复检共用本轮选定的出口 |

userinfo 只携带 manager 的身份验证头；两个 ChatGPT 接口使用同一套 origin、referer、语言、设备、会话、版本、目标路由和 fetch-metadata 头。请求流程不增加 Sentinel 预检、浏览器预热或自定义挑战解算。

旧实现固定 chrome146、手写 UA/版本头，可单独修改 impersonate，存在头部与浏览器画像不一致的可能；还采用阶段内重试、提前按本地过期时间刷新以及 RT 强制直连。上述差异均已移除。指纹的配置和生成方式已对齐；随机 UUID 和 TLS 随机字段本来就不会逐字节相同。

## 判定、恢复和保存

| 分支 | 行为 |
| --- | --- |
| 三阶段均成功 | 账号有效；manager 的 active 映射为本项目 available |
| HTTP 401 | 当前 AT 明确失效，有 RT 则尝试刷新 |
| HTTP 403 | 按 manager 的响应详情关键词区分封禁、Token 失效和临时无法确认 |
| 429、服务端错误、网络错误 | 沿用 manager 的消息分类与临时重试规则，不触发跨账号冷却 |
| 本地 expires_at 已过期 | 仍先实际检测当前 AT |
| RT 客户端 | `extra_data.refresh_token_client_id → extra_data.oauth_client_id → account.client_id → 系统默认` |
| RT 回调地址 | 账号元数据优先，否则系统默认；Platform 客户端不发送 redirect_uri |
| RT 刷新成功 | 完整验证新 AT；无论复检成功、失败或暂无法确认，都保存已轮换的凭据 |
| 暂无法确认 | 保留原账号状态，记录检测明细 |
| 明确失败 | 映射 expired / banned；新 AT 的明确失败也沿用该规则 |
| 字段补全 | 移植原版 JWT 解码、账号/工作区与 Token 元数据补全、兼容 ID Token 生成逻辑 |
| 检测明细 | 当前 AT 与刷新后 AT 的有效性、错误、错误类型，是否刷新、是否 RT 恢复，以及最终状态 |
| 批量执行 | 每批默认 10 并发，上限 50；请求可覆盖并发数，分别统计有效、无效、暂无法确认和 RT 恢复 |

错误分类与状态映射是两个字段：例如 manager 对封禁账号的错误类型可以是 access_token，账号最终状态仍为 banned。迁移保留这一行为。

本项目的适配保留 AES 加密存储、同账号操作锁、后台进度任务和兑换预约。manager 规范化结果中的 Token、Session Token 等凭据写入加密列，附加元数据递归移除明文凭据；日志在截断上游文本前脱敏。兼容 ID Token 为未签名的字段兼容载体，仅供下游解析，不能作为认证依据。账号历史 proxy_used 的清理沿用原验证逻辑。

公开兑换仅交付本轮确实通过验证的账号。临时失败释放预约并恢复预约前状态，在同一兑换任务中排除已检测候选，继续补位或以库存不足结束，避免重复请求同一账号。

## 实现位置与核对

| manager 源码 | 本项目实现 |
| --- | --- |
| `core/openai/token_refresh.py` HTTP 检查、错误分类、RT 刷新 | `app/services/openai_validation.py` |
| `core/http_profiles.py` 验证用 Chrome / UA 选择 | `app/services/openai_validation.py` |
| `core/openai/token_artifacts.py` | `app/services/token_artifacts.py` |
| `validate_account_token_detailed` 账号流程及落库 | `app/services/validator.py` |
| `web/routes/accounts.py` 批量结果与并发 | `app/api/admin.py`、`app/services/operations.py` |
| 本项目兑换适配 | `app/services/redemption.py` |

已直接提取并执行本地 manager 原始验证函数，与移植实现并排运行 17 组模拟上游场景；请求顺序、显式请求参数与头、重试间隔、判断及完整验证明细一致，状态名称按本项目映射。pytest 另覆盖加密落库、元数据脱敏、旧参数失效、批量统计、SQLite 兼容迁移和兑换补位。

这些核对使用模拟上游响应，没有用真实账号访问上游，不能据此认定相同流程在任意出口都不会遇到 Cloudflare 或 429。发生问题时应查看本轮实际失败阶段、HTTP 状态码、cf-mitigated、cf-ray 和 Retry-After，而非把所有暂不可用结果当作 Token 失效。

移植代码的 MIT 版权声明见根目录 `THIRD_PARTY_NOTICES.md`，并随容器镜像分发。
