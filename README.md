# 青年心盾 · 青少年意识形态风险识别平台

浏览器插件 + FastAPI 后端 + MySQL。插件抓取当前网页正文，后端用关键词/向量检索 + LLM 六维精判，输出风险五级五色分级、逐句证据、分龄处置策略与引导话术。

## 换机部署清单

新机器上从零跑通，照这个顺序走。已在 Windows（i9 + RTX 4090 Laptop）与 macOS 上各验证过一次。

### 1. 外部工具

| 工具 | 用途 | macOS | Windows |
| --- | --- | --- | --- |
| ffmpeg | 视频抽音频（含 ffprobe） | `brew install ffmpeg` | `winget install ffmpeg` |
| whisper.cpp | 本地语音转写 | `brew install whisper-cpp` | 下 release 包，见下方说明 |
| ollama | 本地大模型 | `brew install ollama` | `winget install ollama` |
| MySQL 8 | 数据库 | `brew install mysql` | `winget install Oracle.MySQL` |
| tesseract | 图片 OCR（可选） | `brew install tesseract` | `winget install tesseract` |

Windows 上 whisper.cpp 分 CPU 版（约 7 MB）和 cuBLAS GPU 版（约 640 MB）。有独显就装 GPU 版，长视频转写差距明显。

### 2. 本地大模型

```bash
ollama pull qwen2.5:7b
# 启动时必须带并发参数，否则六维研判的多窗口请求会排队
OLLAMA_NUM_PARALLEL=4 ollama serve
```

7B 占约 5.6 GB 显存。显存不足 8 GB 时换量化版 `qwen2.5:7b-instruct-q4_K_M`。
实测 14B 效果反而更差（误报更多、慢 4 倍），不建议。

### 3. 建库导数据

```bash
mysql -u root -p -e "CREATE DATABASE youth_ideology DEFAULT CHARACTER SET utf8mb4;"
mysql -u root -p youth_ideology < db/youth_ideology_dump.sql
mysql -u root -p youth_ideology < db/corpus_sample.sql   # 可选，1000 条语料供 RAG 检索
```

**`db/data/` 下那 8 张表不要单独导。** `youth_ideology_dump.sql` 已经包含它们的全部数据（关键词 255、标签 259、话术 201、案例 60 等），重复导入会撞主键。那个目录只是按表拆开的备份，供单表修补用。

全量语料 `db/data/t_corpus.sql`（83 MB，5.9 万条）不在仓库里，需要时另行索取。不导也能跑，只影响看板聚合和 RAG 召回的丰富度。

### 4. 配置

```bash
cp backend/.env.example backend/.env
```

**`.env` 必须放在 `backend/` 下**（与 `main.py` 同级）。`main.py` 用的是无参 `load_dotenv()`，放项目根目录不会被读取。

至少要填 `DB_PASSWORD`。用本地 ollama 的话，`.env.example` 里默认已指向 `localhost:11434`，不用改。做视频转写还要填 `WHISPER_BIN` 和 `WHISPER_MODEL` 两个绝对路径。

### 5. Python 环境

```bash
python -m venv .venv
.venv/bin/pip install -r requirements.txt      # Windows: .venv\Scripts\pip
.venv/bin/playwright install chromium          # 需要服务端抓网页时才装
```

### 6. whisper 模型

`ggml-large-v3-turbo.bin`（1.6 GB）体积太大不入仓库，需单独下载或拷贝，路径写进 `.env` 的 `WHISPER_MODEL`。

放好后访问 `/api/transcribe/health`，`ffmpeg`、`whisper`、`model` 三项都为 `true` 才算齐。

### 7. 启动与验证

```bash
cd backend
PYTHONUTF8=1 python -m uvicorn main:app --host 0.0.0.0 --port 8000
```

- http://localhost:8000/docs 能打开即启动成功
- 启动日志里确认 LLM 已接入本地 ollama、Chroma 索引已建立
- 插件：`chrome://extensions` 开开发者模式 → 加载 `中青网插件/visual-ai-extension/`（Edge 同理，地址是 `edge://extensions`）。插件里写死 `localhost:8000`，端口没改就不用动

### 常见问题

**视频转写页面打不开、启动日志有一行 transcribe 相关警告** —— 缺 `python-multipart`。转写路由是 try/except 导入的，缺依赖时静默跳过。`pip install python-multipart` 即可（已在 requirements 里）。

**没有管理员权限装不了 MySQL 服务** —— 可以用户态跑：`mysqld --initialize-insecure --datadir=<某目录>` 后 `mysqld --datadir=<某目录> --bind-address=127.0.0.1`。缺点是开机不自启，重启后要手动拉。

**六维研判很慢** —— 检查 ollama 是否带了 `OLLAMA_NUM_PARALLEL=4` 启动。不带的话多窗口请求会串行排队。

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
- Python 3.10+（开发用 3.13，新机器验证过 3.11）
- MySQL 8.0
- Chrome / Edge

## 后端部署

完整步骤见上面的「换机部署清单」。这里只补两点它没覆盖的：

- 首次启动会自动下载 sentence-transformers 模型 `paraphrase-multilingual-MiniLM-L12-v2`（约几百 MB），需联网一次。
- Windows 控制台必须设 `PYTHONUTF8=1`，否则启动时 emoji 日志会报 GBK 编码错。

不配 LLM 也能跑：六维精判和话术会自动退回规则引擎兜底，明显风险仍能识别分级。想用云端模型（如 GPT-5.5）而非本地 ollama，把 `backend/.env` 里那三行按注释切换即可，业务代码不用改。

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
