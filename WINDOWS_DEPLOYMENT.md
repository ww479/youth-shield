# Windows 部署教程

本教程适用于从 GitHub 克隆项目后在 Windows 机器上完整部署青年心盾平台。

## 前置要求

- Windows 10/11
- 至少 15 GB 可用磁盘空间
- 管理员权限（安装软件时需要）
- 稳定的网络连接

---

## 第一步：克隆项目

打开 PowerShell 或 CMD：

```cmd
cd D:\
git clone https://github.com/ww479/youth-shield.git
cd youth-shield
```

如果没有安装 Git，先安装：
```cmd
winget install Git.Git
```

---

## 第二步：安装必需工具

### 2.1 安装 Python 3.11+

```cmd
winget install Python.Python.3.11
```

安装后重启终端，验证：
```cmd
python --version
```

### 2.2 安装 MySQL 8.0

```cmd
winget install Oracle.MySQL
```

安装过程中会要求设置 root 密码，**请记住这个密码**。

安装后启动 MySQL 服务：
```cmd
net start MySQL80
```

### 2.3 安装 Ollama（本地大模型）

```cmd
winget install ollama
```

### 2.4 安装 FFmpeg（视频处理）

```cmd
winget install ffmpeg
```

### 2.5 安装 Tesseract（可选，图片 OCR）

```cmd
winget install tesseract
```

### 2.6 下载 whisper.cpp

**GPU 版本（推荐，如果有 NVIDIA 显卡）：**

1. 访问 https://github.com/ggerganov/whisper.cpp/releases
2. 下载 `whisper-cublas-<版本号>-bin-x64.zip`（约 640 MB）
3. 解压到 `D:\whisper-cpp\`

**CPU 版本：**

1. 下载 `whisper-bin-x64.zip`（约 7 MB）
2. 解压到 `D:\whisper-cpp\`

### 2.7 下载 Whisper 模型

1. 下载 `ggml-large-v3-turbo.bin`（1.6 GB）
   - 下载地址：https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-large-v3-turbo.bin
2. 放到 `D:\whisper-models\` 目录

---

## 第三步：配置数据库

### 3.1 创建数据库

```cmd
mysql -u root -p -e "CREATE DATABASE youth_ideology DEFAULT CHARACTER SET utf8mb4;"
```

输入你在安装 MySQL 时设置的 root 密码。

### 3.2 导入数据

```cmd
cd D:\youth-shield

mysql -u root -p youth_ideology < db\youth_ideology_dump.sql

mysql -u root -p youth_ideology < db\corpus_sample.sql
```

**重要提示：** 
- `db\data\` 目录下的 8 张表数据已经包含在 `youth_ideology_dump.sql` 中，不要单独导入
- 全量语料 `t_corpus.sql`（83 MB，5.9 万条）不在仓库中，不导入也能正常运行

---

## 第四步：配置后端

### 4.1 创建虚拟环境

```cmd
cd D:\youth-shield\青少年风险预测前后端\backend

python -m venv .venv
```

### 4.2 安装依赖

```cmd
.venv\Scripts\activate
pip install -r requirements.txt
playwright install chromium
```

### 4.3 配置环境变量

```cmd
copy .env.example .env
```

用文本编辑器打开 `.env` 文件，修改以下内容：

```env
# 必须填写：MySQL 密码
DB_PASSWORD=你的MySQL密码

# 必须填写：Whisper 配置
WHISPER_BIN=D:/whisper-cpp/main.exe
WHISPER_MODEL=D:/whisper-models/ggml-large-v3-turbo.bin

# 其他配置保持默认即可（使用本地 Ollama）
NIHILISM_OPENAI_BASE_URL=http://localhost:11434/v1
NIHILISM_OPENAI_MODEL=qwen2.5:7b
```

**注意：** Windows 路径可以用正斜杠 `/` 或双反斜杠 `\\`

---

## 第五步：下载并启动 Ollama 模型

### 5.1 下载模型

```cmd
ollama pull qwen2.5:7b
```

下载约 4.7 GB，需要一些时间。

### 5.2 启动 Ollama 服务

**重要：必须设置并发参数**

打开一个新的终端窗口（保持运行）：

```cmd
set OLLAMA_NUM_PARALLEL=4
ollama serve
```

保持这个窗口开启，不要关闭。

---

## 第六步：启动后端服务

打开另一个新终端窗口：

```cmd
cd D:\youth-shield\青少年风险预测前后端\backend

.venv\Scripts\activate

set PYTHONUTF8=1

python -m uvicorn main:app --host 0.0.0.0 --port 8000
```

### 验证后端启动

1. 打开浏览器访问：http://localhost:8000/docs
2. 应该能看到 FastAPI 的 Swagger 文档界面
3. 检查视频转写健康状态：http://localhost:8000/api/transcribe/health
   - `ffmpeg`、`whisper`、`model` 三项都应该是 `true`

---

## 第七步：安装浏览器插件

### 7.1 Chrome 或 Edge

1. 打开浏览器，输入：
   - Chrome: `chrome://extensions`
   - Edge: `edge://extensions`

2. 开启右上角的"开发者模式"

3. 点击"加载已解压的扩展程序"

4. 选择目录：`D:\youth-shield\中青网插件\visual-ai-extension\`

5. 插件加载成功后，会在工具栏显示图标

### 7.2 测试插件

1. 打开任意网页
2. 点击插件图标或页面上的悬浮球
3. 查看风险分析结果

---

## 第八步：配置 Cloudflared 内网穿透

### 8.1 安装 Cloudflared

```cmd
winget install cloudflare.cloudflared
```

### 8.2 配置隧道

**方式一：快速临时隧道（推荐测试用）**

```cmd
cloudflared tunnel --url http://localhost:8000
```

会输出一个临时的公网 URL，有效期 24 小时。

**方式二：持久隧道（推荐生产用）**

如果你在 Mac 上已经配置过隧道，需要复制配置文件：

1. 从 Mac 复制 `~/.cloudflared/` 目录到 Windows
2. 放到 `C:\Users\你的用户名\.cloudflared\`
3. 运行：

```cmd
cloudflared tunnel run <你的隧道名称>
```

**或者创建新隧道：**

```cmd
cloudflared tunnel login
cloudflared tunnel create youth-shield
cloudflared tunnel route dns youth-shield your-domain.com
```

然后创建配置文件 `C:\Users\你的用户名\.cloudflared\config.yml`：

```yaml
tunnel: <隧道ID>
credentials-file: C:\Users\你的用户名\.cloudflared\<隧道ID>.json

ingress:
  - hostname: your-domain.com
    service: http://localhost:8000
  - service: http_status:404
```

启动隧道：
```cmd
cloudflared tunnel run youth-shield
```

---

## 第九步：设置开机自启动（可选）

### 9.1 创建启动脚本

创建 `D:\youth-shield\start.bat`：

```batch
@echo off
echo Starting Ollama...
start "Ollama" cmd /k "set OLLAMA_NUM_PARALLEL=4 && ollama serve"

timeout /t 5

echo Starting Youth Shield Backend...
start "Backend" cmd /k "cd D:\youth-shield\青少年风险预测前后端\backend && .venv\Scripts\activate && set PYTHONUTF8=1 && python -m uvicorn main:app --host 0.0.0.0 --port 8000"

timeout /t 5

echo Starting Cloudflared Tunnel...
start "Cloudflared" cmd /k "cloudflared tunnel run youth-shield"

echo All services started!
```

### 9.2 添加到任务计划程序

1. 按 `Win + R`，输入 `taskschd.msc`
2. 创建基本任务
3. 触发器：计算机启动时
4. 操作：启动程序，选择 `D:\youth-shield\start.bat`

---

## 常见问题排查

### Q1: 视频转写功能不可用

检查：
- `WHISPER_BIN` 路径是否正确
- `WHISPER_MODEL` 文件是否存在
- 访问 http://localhost:8000/api/transcribe/health 查看详细状态

### Q2: 六维研判很慢

确保 Ollama 启动时设置了并发：
```cmd
set OLLAMA_NUM_PARALLEL=4
ollama serve
```

### Q3: 启动时报 GBK 编码错误

确保启动命令包含：
```cmd
set PYTHONUTF8=1
```

### Q4: MySQL 连接失败

检查：
- MySQL 服务是否运行：`net start MySQL80`
- `.env` 中的 `DB_PASSWORD` 是否正确
- 数据库 `youth_ideology` 是否已创建

### Q5: 插件无法连接后端

检查：
- 后端是否在 8000 端口运行
- 防火墙是否放行 8000 端口
- 如果修改了端口，需要同步修改插件代码中的地址

---

## 性能优化建议

### GPU 加速

如果有 NVIDIA 显卡：
- 使用 cuBLAS 版本的 whisper.cpp
- 长视频转写速度提升显著

### 显存优化

如果显存不足 8 GB：
- 使用量化模型：`ollama pull qwen2.5:7b-instruct-q4_K_M`
- 修改 `.env` 中的 `NIHILISM_OPENAI_MODEL`

### 并发调优

根据机器性能调整并发数：
- `.env` 中的 `TRANSCRIPT_WINDOW_WORKERS`
- Ollama 的 `OLLAMA_NUM_PARALLEL`

---

## 验证部署成功

1. 后端 API 文档：http://localhost:8000/docs
2. 视频转写健康检查：http://localhost:8000/api/transcribe/health
3. 浏览器插件能正常分析网页
4. Cloudflared 隧道提供公网访问

---

## 在 Windows 的 Claude 上继续工作

现在你可以：

1. 在 Windows 机器上安装 Claude Desktop
2. 在 Claude 中运行：`cd D:\youth-shield`
3. 让 Claude 帮你：
   - 检查配置是否正确
   - 自动化部署流程
   - 排查问题
   - 修改代码

---

## 需要帮助？

如果遇到问题，在 Windows 的 Claude 中：

1. 告诉 Claude 具体的错误信息
2. 提供启动日志
3. Claude 可以直接读取你的配置文件并帮你修复

祝部署顺利！
