# 青年心盾 · 青少年意识形态风险识别平台

浏览器插件 + FastAPI 后端 + MySQL。插件抓取当前网页正文，后端用关键词/向量检索 + LLM 六维精判，输出风险五级五色分级、逐句证据、分龄处置策略与引导话术。

## 目录结构

```
中青网项目/
├── 青少年风险预测前后端/        # 后端
│   ├── backend/                # FastAPI 服务（main.py 入口）
│   │   └── .env.example        # 环境变量模板（复制为 .env 填写）
│   ├── db/                     # 数据库 dump 与种子脚本
│   │   ├── youth_ideology_dump.sql   # 核心数据（建库用这个）
│   │   └── corpus_sample.sql         # 语料采样（可选，供 rag 检索演示）
│   ├── frontend/               # 后端自带 Web 大屏原型（挂载在 /ui）
│   └── requirements.txt
└── 中青网插件/visual-ai-extension/   # Chrome MV3 扩展
```

## 环境要求
- Python 3.10+（开发用 3.13）
- MySQL 8.0
- Chrome / Edge

## 后端部署

### 1. 安装依赖
```bash
cd 青少年风险预测前后端
pip install -r requirements.txt
playwright install        # 若需要服务端抓取网页
```
> 首次启动会自动下载 sentence-transformers 模型 `paraphrase-multilingual-MiniLM-L12-v2`（约几百 MB），需联网一次。

### 2. 建库并导入数据
```bash
# 新建数据库（utf8mb4）
mysql -u root -p -e "CREATE DATABASE youth_ideology DEFAULT CHARACTER SET utf8mb4;"
# 导入核心数据（标签/分类/关键词/句式/话术/案例，后端启动与判定必需）
mysql -u root -p youth_ideology < db/youth_ideology_dump.sql
# （可选）导入语料采样，供 rag 相似检索演示
mysql -u root -p youth_ideology < db/corpus_sample.sql
```

### 3. 配置环境变量
```bash
cp backend/.env.example backend/.env
```
编辑 `backend/.env`：
- **`DB_PASSWORD`**：你的 MySQL 口令（必填）。
- **AI 能力（六维精判、处置策略/引导话术生成）需要 LLM key**：
  - 走 OpenAI 兼容接口：填 `NIHILISM_OPENAI_API_KEY` + `NIHILISM_OPENAI_BASE_URL`（你自己的中转或官方），模型默认 `gpt-5.5`；
  - 或把 key 放 `~/.codex/auth.json` 的 `OPENAI_API_KEY` 字段；
  - `ANTHROPIC_API_KEY` / `ANTHROPIC_BASE_URL` 供部分叙事分析用，可选。
  - **不配 key 也能跑**：AI 精判/话术会自动退回规则引擎兜底。

### 4. 启动
```bash
cd backend
PYTHONUTF8=1 NIHILISM_LLM_TIMEOUT=40 python -m uvicorn main:app --host 0.0.0.0 --port 8000 --reload
```
- Windows 控制台必须 `PYTHONUTF8=1`，否则启动时 emoji 日志会报 GBK 编码错。
- 打开 http://localhost:8000/docs 能看到接口文档即启动成功。

## 插件加载
1. 打开 `chrome://extensions`，开启「开发者模式」。
2. 「加载已解压的扩展程序」→ 选 `中青网插件/visual-ai-extension/`。
3. 打开任意网页，点插件图标或页面里的悬浮球分析。
4. 若后端不在 `localhost:8000`，需改插件里的地址（`background.js`、`popup.js`、`content.js` 及 `manifest.json` 的 `host_permissions`）。

## AI 能力说明
- **配了 LLM key**：六维逐维精判 + 分龄处置策略/引导话术由模型动态生成。
- **没配 key**：走规则引擎兜底，明显风险仍能识别分级，六维为规则估算。

## 注意事项
- `db/seed_*.py` 种子脚本依赖开发者本地的 Excel 原始数据（未随仓库分发），**别人无法直接运行**；建库用 `youth_ideology_dump.sql` 即可，种子脚本仅供了解数据加工逻辑。这些脚本里的本地绝对路径需按自己环境调整。
- 完整语料库（t_corpus 约 5.8 万条爬取数据）因体量与数据合规未随仓库分发，仓库内只含 `corpus_sample.sql`（1000 条采样）。
- 所有密钥都从环境变量/`.env` 读取，`.env` 已被 `.gitignore` 排除，**切勿提交任何真实密钥**。
