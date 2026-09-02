# Cloudflared 内网穿透配置指南

本指南详细说明如何在 Windows 机器上配置 Cloudflared，将本地部署的青年心盾平台暴露到公网。

---

## 方案选择

### 方案一：快速临时隧道（推荐新手测试）

优点：无需注册，立即可用，命令简单
缺点：URL 随机生成，每次重启都会变，有效期 24 小时

### 方案二：持久命名隧道（推荐生产使用）

优点：固定域名，可以绑定自定义域名，可设置开机自启
缺点：需要 Cloudflare 账号，配置稍复杂

---

## 方案一：快速临时隧道

### 1. 安装 Cloudflared

winget install cloudflare.cloudflared

### 2. 启动临时隧道

确保后端已经启动在 8000 端口，然后运行：

cloudflared tunnel --url http://localhost:8000

### 3. 获取公网地址

命令执行后会输出类似：
https://random-name-1234.trycloudflare.com

这就是你的公网地址，访问 https://random-name-1234.trycloudflare.com/docs 测试。

---

## 方案二：持久命名隧道

### 1. 安装并登录

winget install cloudflare.cloudflared
cloudflared tunnel login

会自动打开浏览器，登录你的 Cloudflare 账号并授权。

### 2. 创建隧道

cloudflared tunnel create youth-shield

记住输出的隧道 ID（xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx）

### 3. 配置 DNS（可选，使用自定义域名）

如果你有自己的域名：

cloudflared tunnel route dns youth-shield youth.example.com

### 4. 创建配置文件

在 C:\Users\你的用户名\.cloudflared\config.yml

tunnel: xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
credentials-file: C:\Users\你的用户名\.cloudflared\xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx.json

ingress:
  - hostname: youth.example.com
    service: http://localhost:8000
  - service: http_status:404

如果没有自定义域名，去掉 hostname 行即可。

### 5. 启动隧道

cloudflared tunnel run youth-shield

---

## 从 Mac 迁移现有配置（推荐）

### 在 Mac 上

cd ~/.cloudflared
tar -czf cloudflared-config.tar.gz *.json config.yml

### 在 Windows 上

解压到 C:\Users\你的用户名\.cloudflared\
修改 config.yml 中的路径为 Windows 格式
运行：cloudflared tunnel run youth-shield

Mac 和 Windows 可以共用同一个隧道配置，但不能同时运行。

---

## 设置开机自启动

### 方法一：Windows 服务（推荐）

以管理员身份运行：

cloudflared service install
net start cloudflared

### 方法二：任务计划程序

创建 start-tunnel.bat：

@echo off
cloudflared tunnel run youth-shield

然后在任务计划程序中设置为开机启动。

---

## 故障排查

### 隧道启动后无法访问

检查后端是否运行：netstat -ano | findstr :8000
检查防火墙是否允许 cloudflared.exe

### 端口被占用

netstat -ano | findstr :8000
taskkill /F /PID <PID>

### DNS 解析失败

nslookup youth.example.com
ipconfig /flushdns

---

## 常用命令

cloudflared tunnel list              # 查看所有隧道
cloudflared tunnel info youth-shield # 查看隧道详情
cloudflared tunnel delete youth-shield # 删除隧道

---

现在你的 Mac 可以关机了，Windows 机器通过 Cloudflared 持续提供服务！
