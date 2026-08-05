# CDK Loader

CDK Loader 是一个面向账号库存管理和按额度交付的 Web 服务。管理员可以批量导入账号、验证凭据、生成 CDK 并查看兑换记录；使用者通过公开提取页提交 CDK，系统在交付前实时验活账号，并生成一次性下载文件。

## 主要功能

- **账号导入**：支持 JSON、CSV、TXT 和 ZIP，提供导入预览、重复账号处理和可选的导入后预验活。
- **凭据验活**：远端验活会校验 OAuth 身份及实际 ChatGPT 接口，支持 Refresh Token 刷新；兑换时会再次实时验证候选账号并自动补位。
- **CDK 管理**：按数量和账号额度生成 CDK，支持有效期、状态、额度筛选、复制和批量删除。
- **兑换交付**：支持一次提交多个 CDK、幂等任务、进度查询和一次性下载；已兑换 CDK 可在限时窗口内补发其首次关联账号。
- **双格式 JSON 包**：JSON 交付会生成带时间戳的 ZIP，同时包含 `cpa/` 和 `sub2api/` 两种目录格式，不生成 `manifest.json`。
- **运营后台**：提供账号池、CDK、兑换记录的服务端筛选、分页、当前页全选和批量操作，账号池可按有无 Refresh Token 筛选，并由管理员勾选账号后导出。
- **凭据保护**：账号敏感字段使用 AES-GCM 加密后写入数据库，CDK 使用带密钥摘要匹配。

## 技术栈

- FastAPI、SQLAlchemy、SQLite
- Vue 3、Nanocat UI、Vite 7
- Docker、Docker Compose
- Python 3.12、Node.js 22

## 快速部署

### 环境要求

- Docker Engine 或 Docker Desktop
- Docker Compose v2

### 启动服务

```bash
cp .env.example .env
```

启动前至少修改 `.env` 中的以下配置：

```dotenv
ADMIN_PASSWORD=替换为强密码
ADMIN_TOKEN=替换为随机长字符串
CREDENTIAL_SECRET=替换为随机长字符串
CDK_PEPPER=替换为另一段随机长字符串
PUBLIC_BASE_URL=https://cdk.example.com
```

可以使用 OpenSSL 生成随机值：

```bash
openssl rand -hex 32
```

拉取并启动已发布镜像：

```bash
docker compose pull
docker compose up -d
```

检查运行状态：

```bash
docker compose ps
docker compose logs -f cdkloader
curl http://127.0.0.1:1456/health
```

默认访问地址：

- 公开提取页：`http://127.0.0.1:1456/`
- 管理后台：`http://127.0.0.1:1456/admin`
- API 文档：`http://127.0.0.1:1456/docs`

SQLite 数据保存在宿主机的 `./data` 目录中。停止服务不会删除数据：

```bash
docker compose down
```

## 镜像版本

默认镜像仓库为 `ghcr.io/hermitchen/cdkloader`。可以在 `.env` 中使用固定日期版本或最新版：

```dotenv
CDK_LOADER_IMAGE=ghcr.io/hermitchen/cdkloader:20260801
# 或
CDK_LOADER_IMAGE=ghcr.io/hermitchen/cdkloader:latest
```

固定日期标签适合生产环境回滚，`latest` 适合跟随最新发布版本。

## 配置说明

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `CDK_LOADER_IMAGE` | `ghcr.io/hermitchen/cdkloader:latest` | Docker Compose 使用的镜像 |
| `CDK_LOADER_PORT` | `1456` | 映射到宿主机的端口 |
| `DATABASE_URL` | `sqlite:///./data/cdkloader.db` | SQLite 数据库连接地址 |
| `ADMIN_PASSWORD` | `change-me` | 管理后台登录密码，生产环境必须修改 |
| `ADMIN_TOKEN` | `change-me-admin-token` | 管理 API Bearer Token，生产环境必须修改 |
| `CREDENTIAL_SECRET` | 开发占位值 | 账号凭据加密密钥，修改后无法解密已有数据 |
| `CDK_PEPPER` | 开发占位值 | CDK 和任务凭证摘要密钥，应与凭据密钥不同 |
| `VALIDATION_MODE` | `remote` | `remote` 为远端验活，`structural` 仅检查凭据结构 |
| `VALIDATION_TIMEOUT_SECONDS` | `30` | 单次远端请求超时秒数 |
| `VALIDATION_ATTEMPTS` | `3` | 单个账号的最大验活尝试次数 |
| `VALIDATION_EGRESS_MODE` | `direct` | 验活出口模式：`direct` 直连、`account` 使用账号出口、`pool` 使用代理池 |
| `VALIDATION_PROBE_MODE` | `fast` | 验活探测模式：`fast` 检查 userinfo 和 conversation/init，`strict` 额外检查 accounts/check |
| `VALIDATION_IMPERSONATE` | `chrome146` | curl_cffi 使用的浏览器指纹目标；需使用当前依赖支持的 impersonate 名称 |
| `VALIDATION_PROXY` | 空 | 验活统一出口代理，留空时直连 |
| `VALIDATION_PROXY_POOL` | 空 | 账号未配置代理且统一代理为空时使用的代理池，支持逗号、分号或换行分隔 |
| `VALIDATION_RETRY_BASE_SECONDS` | `2` | 临时错误重试的初始等待时间，单位为秒 |
| `VALIDATION_RETRY_MAX_SECONDS` | `30` | 本地指数退避的最大时间，单位为秒；上游 `Retry-After` 优先 |
| `VALIDATION_RETRY_JITTER_SECONDS` | `0.5` | 重试等待随机抖动范围，单位为秒 |
| `VALIDATION_CONCURRENCY` | `2` | 同一进程可同时执行的账号验活任务数量 |
| `VALIDATION_GATE_THRESHOLD` | `5` | 60 秒内累计达到该数量的风控、Challenge 或限流响应后暂停新的验活请求 |
| `VALIDATION_COOLDOWN_SECONDS` | `60` | 触发上游冷却后的暂停时间，单位为秒 |
| `REDELIVERY_WINDOW_SECONDS` | `1800` | 已兑换 CDK 的公开补发窗口（秒），设为 `0` 可关闭补发 |
| `PUBLIC_BASE_URL` | `http://localhost:1456` | 用户访问服务的外部地址 |

完整示例及逐项注释见 [.env.example](.env.example)。

## 账号导入

支持以下文件类型：

- `JSON`：单个对象、对象数组或包含账号数组的常见导出结构。
- `CSV`：使用表头识别账号与凭据字段。
- `TXT`：每行使用 `email----password----refresh_token` 格式。
- `ZIP`：批量读取包内的 JSON、CSV 和 TXT 文件；不允许嵌套 ZIP。

每条账号至少需要 `email` 或 `account_id`，并至少包含 `access_token` 或 `refresh_token`。重复账号可以选择跳过、补充空字段或替换已有凭据。

## JSON 交付结构

使用 JSON 格式的 CDK 兑换成功后，下载文件名类似：

```text
accounts_20260801_153045.zip
```

压缩包结构：

```text
accounts_20260801_153045.zip
├── cpa/
│   └── user@example.com.json
└── sub2api/
    └── user@example.com_sub2api.json
```

原始下载链接只能成功使用一次。若 CDK 已成功兑换，在 `REDELIVERY_WINDOW_SECONDS` 指定的窗口内再次提交同一 CDK，系统会直接补发该 CDK 首次关联的账号，不会重新验活、分配账号或扣减额度；每次补发仍使用新的单次下载链接。窗口结束后，公开页面不再支持补发，管理员可在账号池中按关联关系二次导出。已交付文件包含敏感凭据，请在受控环境中保存和传输。

## 验活行为

系统当前不执行定时全量验活：

- 导入账号时，可以选择立即预验活。
- 用户兑换时，系统会对候选账号实时验活；失效账号不会交付，并会继续从库存补位。
- `remote` 模式先验证 OAuth `userinfo`，再验证 ChatGPT 的会话初始化和账户检查接口。实际接口 `401` 会强制使用 Refresh Token 恢复并复检；网络、限流、风控和服务端错误会标记为暂无法确认，不会直接判定失效。
- `VALIDATION_MODE=structural` 只适合本地开发和自动化测试，不应作为生产验活模式。

## 从源码开发

### 环境要求

- Python 3.11 或更高版本
- [uv](https://docs.astral.sh/uv/)
- Node.js `^20.19.0` 或 `>=22.12.0`
- npm

项目默认将 Python 环境放在仓库外的 `../venv`，也可以通过环境变量覆盖：

```bash
UV_PROJECT_ENVIRONMENT=/path/to/venv make sync
make frontend-install
make test
make check
make frontend-build
```

应用运行统一使用 Docker Compose。修改代码后，本地重建镜像并重启服务：

```bash
make docker-rebuild
```

等价命令：

```bash
bash build.sh
docker compose up -d --force-recreate --remove-orphans
```

`build.sh` 默认生成当天的 `YYYYMMDD` 标签和 `latest` 标签并加载到本地 Docker，不会推送镜像。

维护者发布多架构镜像时使用：

```bash
bash build.sh -push
```

该命令构建并推送 `linux/amd64` 与 `linux/arm64`。补发历史版本且不更新 `latest` 时使用：

```bash
bash build.sh -tag YYYYMMDD -push -nolatest
```

## 项目结构

```text
app/                 FastAPI 路由、模型和业务服务
frontend/            Vue 3 管理后台与公开提取页
tests/               后端自动化测试
Dockerfile           前端与后端多阶段镜像构建
docker-compose.yml   容器运行配置
build.sh             本地与多架构镜像构建脚本
```

## 安全与备份

- 不要提交 `.env`、`data/`、账号导入文件或兑换下载包。
- 首次生产部署必须替换所有开发占位密码和密钥。
- `CREDENTIAL_SECRET` 和 `CDK_PEPPER` 应分别生成并长期保存；随意修改会导致已有凭据或 CDK 无法继续使用。
- 建议通过反向代理启用 HTTPS，并限制管理后台和 API 文档的访问范围。
- `./data` 包含业务数据库。升级前应停止服务并备份整个目录。
- 为兼容 Linux 绑定挂载目录，容器默认以镜像默认用户运行；对隔离要求较高的环境建议配合 rootless Docker 或额外的容器运行策略。

## 许可证

本项目使用 [MIT License](LICENSE) 开源协议。
