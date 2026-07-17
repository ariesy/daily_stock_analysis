# AGENTS.md

本仓库 AI 协作规则的唯一真源。规则与代码现实不一致时，以可执行的脚本 / 工作流 / 代码为准，并顺手修正文档。

- `CLAUDE.md` 必须是软链接到本文件。
- `.github/copilot-instructions.md` 与 `.github/instructions/*.instructions.md` 是镜像 / 分层补充，冲突时以本文件为准。
- 根目录 `SKILL.md` 与 `docs/openclaw-skill-integration.md` 属产品或外部集成说明，不是仓库协作规则真源。
- 修改本文件或相关 AI 协作资产后，必须通过：

```bash
python scripts/check_ai_assets.py
```

## 1. 硬规则

- **目录边界**：后端逻辑在 `src/`、`data_provider/`、`api/`、`bot/`；Web 在 `apps/dsa-web/`；桌面端在 `apps/dsa-desktop/`；部署与流水线在 `scripts/`、`.github/workflows/`、`docker/`。不新增平行目录。
- **Git 操作**：未经明确确认，不执行 `git commit` / `git tag` / `git push`；commit message 用英文，不加 `Co-Authored-By`。
- **配置**：不写死密钥、账号、路径、模型名、端口；新增配置项必须同步更新 `.env.example` 与对应文档；新配置默认 "不配也能跑，配置后增强能力"。
- **复用优先**：优先复用现有模块、配置入口、脚本与测试，不新增平行实现。稳定性优先于 "顺手优化"。
- **CHANGELOG**：`docs/CHANGELOG.md` 的 `[Unreleased]` 段使用**扁平格式** `- [类型] 描述`，类型枚举 `新功能` / `改进` / `修复` / `文档` / `测试` / `chore`；禁止在 `[Unreleased]` 内新增 `### 类目标题`（减少并发 PR 冲突）。发版时由 maintainer 整理成带标题的正式格式。
- **README 边界**：`README.md` 只承载项目定位、核心能力、快速开始、主要入口、赞助 / 合作等首页级内容；模块行为、页面交互、专题配置、排障、字段契约等写入对应 `docs/*.md`。
- **双语文档**：中英两版文档只更新其中之一时，交付说明里要写明未同步原因。
- **截图**：报告格式 / 渲染 / Web UI 改动在 PR 描述必须附受影响页面或报告截图（优先前后对比），无法截图时说明原因与替代可视证据。Issue / PR 临时截图不入库，应放 PR 描述 / 评论 / Actions artifact / 外部链接。
- **PR 标题**（非阻断）：推荐 `<类型>: <修改内容>`，类型优先 `fix` / `feat` / `refactor` / `docs` / `chore` / `test` / `ci`；不加 `[codex]` / `codex` / `autocode` / `copilot` 等工具或 agent 来源前缀。
- **贡献质量底线**：不接受以堆代码 / 扩大 diff / 补丁式响应 review 替代真实设计收敛的 PR；不允许 mock 掉真实风险层只证明局部实现通过；不允许 CI 通过后声称已修复反例；PR body 必须与实际 diff、验证结果、兼容性、风险、回滚方案一致。

## 2. 仓库速览

- 定位：股票智能分析系统，覆盖 A 股、港股、美股、台股、日股、韩股。
- 主流程：抓取数据 → 技术分析 / 新闻检索 → LLM 分析 → 生成报告 → 通知推送。
- 入口与职责：
  - `main.py` — 分析任务主入口（CLI + 调度 + 可选 serve）
  - `server.py` — FastAPI 入口；`uvicorn server:app`
  - `apps/dsa-web/` — Web 前端（Vite + React）
  - `apps/dsa-desktop/` — Electron 桌面端
  - `src/core/` 主流程编排 / `src/services/` 业务服务 / `src/repositories/` 数据访问 / `src/schemas/` Schema / `src/llm/` LLM 后端 / `data_provider/` 多源适配与 fallback / `api/` FastAPI 路由 / `bot/` 机器人接入 / `tests/` pytest 测试 / `docs/` 文档
  - `.github/workflows/` — CI、每日任务、Release、Docker 发布
- 可复用 skill：`.claude/skills/analyze-issue/`、`.claude/skills/analyze-pr/`、`.claude/skills/fix-issue/`；分析产物放 `.claude/reviews/`。

## 3. 常用命令

```bash
# 安装
pip install -r requirements.txt
pip install flake8 pytest

# 运行
python main.py                      # 分析任务
python main.py --debug --dry-run    # 调试 / 仅取数不分析
python main.py --stocks 600519,hk00700,AAPL
python main.py --market-review
python main.py --schedule           # 调度模式
python main.py --serve              # API + 分析
python main.py --serve-only         # 仅 API
uvicorn server:app --reload --host 0.0.0.0 --port 8000

# 后端验证（CI 等价）
./scripts/ci_gate.sh                # 全量；可细分 syntax | flake8 | deterministic | offline-tests
python -m pytest -m "not network"
python -m py_compile <changed_python_files>

# Web / Desktop
cd apps/dsa-web && npm ci && npm run lint && npm run build
cd apps/dsa-desktop && npm install && npm run build

# PR / CI 证据
gh pr view <pr_number>
gh pr checks <pr_number>
gh run view <run_id> --log-failed
```

`scripts/ci_gate.sh` 实际执行：py_compile 关键模块 → `flake8 --select=E9,F63,F7,F82` → `scripts/test.sh code` + `scripts/test.sh yfinance` → `pytest -m "not network"`。
`scripts/test.sh` 子场景：`market` / `a-stock` / `hk-stock` / `us-stock` / `etf` / `mixed` / `single` / `dry-run` / `quick` / `full` / `all` / `code` / `yfinance`。

## 4. 默认工作流

1. 判定任务类型：`fix | feat | refactor | docs | chore | test | review`。
2. 先读现有实现、配置、测试、脚本、工作流与文档再动手。
3. 识别改动面：后端 / API / Web / Desktop / Workflow / Docs / AI 协作资产。
4. 判断是否命中高风险区域：配置语义、API / Schema、数据源 fallback、报告结构、认证、调度、发布流程、桌面端启动链路。
5. 只做与当前任务直接相关的最小改动。
6. 文档与脚本 / 工作流不一致时优先信任实际代码，必要时顺手修正文档。
7. 按 §5 验证矩阵执行检查。
8. 交付默认输出：改了什么 / 为什么改 / 验证情况 / 未验证项 / 风险点 / 回滚方式。docs 任务可写 `Docs only, tests not run`，但仍需说明是否核对了命令与文件名。

## 5. 验证矩阵

CI 在 `.github/workflows/ci.yml`，主表：

| 检查 | 触发条件 | 是否阻断 |
| --- | --- | --- |
| `ai-governance` | `scripts/check_ai_assets.py` | 是 |
| `backend-gate` | `./scripts/ci_gate.sh` | 是 |
| `docker-build` | Docker 构建 + 关键模块导入 smoke | 是 |
| `web-gate` | `apps/dsa-web/**` 改动 → `npm run lint && npm run build` | 是（触发时） |
| `network-smoke` | `pytest -m network` + `scripts/test.sh quick`（`.github/workflows/network-smoke.yml`） | 否 |
| `pr-review` | PR 静态检查 + AI 审查 + 自动 label（`.github/workflows/pr-review.yml`） | 否 |

按改动面：

- **Python 后端** (`main.py` / `src/` / `data_provider/` / `api/` / `bot/` / `tests/`)：跑 `./scripts/ci_gate.sh`；最低 `python -m py_compile <changed_files>`；影响 API、任务编排、报告、通知、fallback、认证、调度的，交付中说明是否覆盖对应路径。
- **Web 前端** (`apps/dsa-web/`)：`cd apps/dsa-web && npm ci && npm run lint && npm run build`；若涉及 API 联调、路由、状态、Markdown / 图表、认证，交付中说明联动面与未覆盖风险。
- **桌面端** (`apps/dsa-desktop/`、`scripts/{run,build}*.{ps1,sh}`、`docs/desktop-package.md`)：先构建 Web 再构建桌面端；平台受限需明确说明 Web 构建产物、Electron 构建、Release 工作流影响。
- **API / Schema / 认证联动**：`api/`、`src/schemas/`、`src/services/`、`apps/dsa-web/`、`apps/dsa-desktop/`；含登录、Cookie、会话、轮询、字段增删、枚举的，必须明确兼容性影响。
- **文档与治理** (`README.md` / `docs/` / `AGENTS.md` / `.github/copilot-instructions.md` / `.github/instructions/` / `.claude/skills/`)：不强制代码测试；核对命令、配置项、文件名、工作流名称与仓库一致；改 AI 协作资产先跑 `python scripts/check_ai_assets.py`。
- **工作流 / 脚本 / Docker**：跑最接近改动面的本地验证；交付中说明影响了哪条流水线 / 发布 / 部署路径。
- **网络或三方依赖**：先离线 / 确定性检查；确认 timeout、retry、fallback、异常文案、降级路径仍成立；未在线验证需明确原因。

## 6. 稳定性护栏

- **配置与运行入口**：改 `.env` 语义、默认值、CLI 参数、启动方式、调度语义时同步评估本地、Docker、GitHub Actions、API、Web、Desktop。
- **数据源 fallback**（`data_provider/`）：关注优先级、失败降级、字段标准化、缓存与超时；单一数据源失败不应拖垮整次分析，除非需求明确 fail-fast。
- **API / Web / Desktop 兼容**：优先追加字段、保留旧字段或提供兼容层，不无提示破坏现有客户端。
- **报告 / Prompt / 通知**：检查上下游兼容性；单一通知渠道失败不拖垮主流程。修改 `src/services/image_stock_extractor.py` 中 `EXTRACT_PROMPT` 时必须在 PR 描述中附完整最新 prompt。
- **自动 tag**：默认 opt-in — 仅 commit title 含 `#patch` / `#minor` / `#major` 才触发版本号更新；手动 tag 必须用 annotated tag。

## 7. Issue / PR / Skill 工作流

- 任务明确是 issue 分析、PR 审查或 issue 修复时，优先按对应 skill 执行（`.claude/skills/analyze-issue/`、`.claude/skills/analyze-pr/`、`.claude/skills/fix-issue/`），产物保存到 `.claude/reviews/`。
- skill 中的命令、模板、验证顺序、交付结构必须与本文件一致。skill 默认优先读取 CI / 工作流证据，再决定是否补本地验证。
- **基线同步**：PR 创建 / 更新、PR 审查、issue 分析前必须 `git fetch --all --prune`；工作区干净且当前分支可 fast-forward 才执行 `git pull --ff-only`。否则不得强切 / stash / reset / 覆盖本地；PR 审查 / issue 分析可改用已 fetch 的远端 refs / PR head，并在分析文档中记录当前 HEAD 与远端基线。
- 除非上述 §7 的安全 fast-forward 同步，skill 不得默认执行 `git pull` / `git push` / `git tag` / `gh pr create` 等改变远端或当前分支状态的操作 —— 必须要求用户确认。

### 7.1 PR 审查顺序

必要性 → 关联性 → 标题建议（`<类型>: <修改内容>`，非阻断）→ 描述完整性（对照 `.github/PULL_REQUEST_TEMPLATE.md`）→ 验证证据 → 实现正确性 → 合入判定。`fix` 类 PR 必须说明：原问题 / 根因 / 修复点 / 回归风险。

合入阻断：安全性或正确性问题；阻断型 CI 未通过；PR 描述与实际改动实质性矛盾；缺回滚方案；反复出现未收敛的契约漂移 / 补丁堆叠 / 验证证据失真。

### 7.2 review 反馈处理（补丁堆叠禁止）

处理 review 反馈时禁止只在被指名的位置追加 patch 后声称 "已全部修复"。必须：

1. 逐条列出 reviewer 指出的原问题。
2. 说明根因，不只描述 "改了哪几行"。
3. 找出同一语义涉及的所有路径（runtime、API / Web、CLI、diagnostics、workflow、docs、tests）。
4. 修复完整契约，不只修当前失败测试 / 当前评论行。
5. 补能覆盖 reviewer 反例的回归测试或最终入口验证，未验证则说明原因。
6. 同步 PR body：scope、验证结果、兼容性、风险、回滚方案与 head 一致。

无法收敛时主动说明需拆分 / 关闭重做 / 请求维持者确认新最小范围，不继续堆补丁。

低质量 PR 信号：用 broad fallback / 静默降级 / `return False/None/[]` 掩盖不清晰契约；测试 mock 掉真实风险层；CI 通过后没覆盖 reviewer 反例；PR body 与 diff 不一致；review 后继续追加零散 patch；同一业务语义在 runtime / Web / API / docs / workflow / tests 中表现不一致。
