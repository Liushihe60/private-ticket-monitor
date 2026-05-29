# 余票监测系统 — 管理员手册

## 简介

开发者后台用于管理所有注册用户、查看监测活动、执行管理操作。

---

## 1. 访问管理后台

1. 打开管理后台地址：`https://ticket-60.site/admin`
2. 输入 **开发者账号** 和 **开发者密码**
3. 点击 **进入管理后台**

> 开发者账号密码通过环境变量配置，见下方"服务器配置"章节。

---

## 2. 管理后台功能

### 2.1 概览统计

页面顶部显示三个统计卡片：

| 指标 | 说明 |
|------|------|
| 注册用户 | 所有已注册的用户名数 |
| 当前在线 | 正在使用系统的用户数 |
| 监测中 | 正在运行自动监测的用户数 |

### 2.2 注册用户列表

表格显示所有注册用户的信息：

| 字段 | 说明 |
|------|------|
| 用户名 | 用户注册时设置的名称 |
| 注册时间 | 首次注册的时间 |
| 最后登录 | 最近一次登录的时间 |
| 登录次数 | 累计登录次数 |
| 状态 | 在线/离线 |
| 操作 | 管理操作按钮 |

**管理操作：**

- **停止监测**：停止该用户的自动监测进程（仅在线用户可用）
- **踢出**：强制断开该用户的会话，停止其所有操作（仅在线用户可用）

> 离线用户无可用操作。

### 2.3 实时监测活动

显示所有正在进行监测的用户：

| 字段 | 说明 |
|------|------|
| 用户 | 执行监测的用户名 |
| 剧目 | 正在监测的演出名称 |
| 场次 | 监测的演出日期时间 |
| 票档 | 监测的价格区间 |
| 最后活跃 | 该用户最后一次操作的时间 |

可以点击 **停止监测** 按钮远程停止任意用户的监测。

### 2.4 站点设置

管理后台底部的 **站点设置** 区域可以修改系统配置，**无需重启服务器**：

| 设置项 | 说明 |
|--------|------|
| 开发者账号 | 管理后台登录的用户名 |
| 开发者密码 | 管理后台登录的密码 |

- 留空表示不修改该项
- 修改后立即生效
- 配置保存在 `configs/_site.json`，服务器重启后依然有效

### 2.5 自动刷新

管理后台每 10 秒自动刷新数据。也可以点击右上角 **刷新** 按钮手动刷新。

---

## 3. 服务器配置

### 3.1 环境变量（可选）

> 这些配置也可以在管理后台的 **站点设置** 中修改，无需设置环境变量。

| 变量名 | 默认值 | 说明 |
|--------|--------|------|
| `DEV_USERNAME` | `admin` | 开发者后台登录账号 |
| `DEV_PASSWORD` | `admin123` | 开发者后台登录密码 |
| `FLASK_SECRET` | 随机生成 | Flask session 加密密钥 |

如果通过管理后台修改了配置，会保存在 `configs/_site.json`，优先级高于环境变量。

### 3.2 启动命令

```bash
cd ~/ticket
source ~/ticket-venv/bin/activate

export DEV_USERNAME=admin
export DEV_PASSWORD=你的管理员密码

python ticket_web.py
```

### 3.3 后台运行（推荐使用 systemd 或 nohup）

```bash
nohup python ticket_web.py > flask.log 2>&1 &
```

### 3.4 使用 systemd 管理服务

创建 `/etc/systemd/system/ticket.service`：

```ini
[Unit]
Description=Ticket Monitor Web App
After=network.target

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/home/ubuntu/ticket
Environment=DEV_USERNAME=admin
Environment=DEV_PASSWORD=你的管理员密码
ExecStart=/home/ubuntu/ticket-venv/bin/python ticket_web.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

启用并启动：

```bash
sudo systemctl daemon-reload
sudo systemctl enable ticket
sudo systemctl start ticket

# 查看状态
sudo systemctl status ticket

# 查看日志
sudo journalctl -u ticket -f
```

---

## 4. 数据存储

### 4.1 文件结构

```
configs/
  _registry.json    ← 用户注册信息（用户名、注册时间、登录次数）
  alice.json        ← alice 的推送配置
  bob.json          ← bob 的推送配置
```

### 4.2 用户注册信息 (`_registry.json`)

```json
{
  "alice": {
    "created_at": "2026-05-29 10:30:00",
    "last_login": "2026-05-29 15:20:00",
    "login_count": 5
  },
  "bob": {
    "created_at": "2026-05-29 11:00:00",
    "last_login": "2026-05-29 14:00:00",
    "login_count": 2
  }
}
```

### 4.3 用户配置文件 (`alice.json`)

```json
{
  "push_method": "wxpusher",
  "push_key": "AT_xxx",
  "push_uid": "UID_xxx"
}
```

> `configs/` 目录已在 `.gitignore` 中排除，不会被提交到 GitHub。

---

## 5. 安全建议

1. **修改默认密码**：部署时务必修改 `DEV_PASSWORD` 的默认值
2. **使用 HTTPS**：通过 Nginx 反向代理 + Let's Encrypt 证书启用 HTTPS
3. **限制访问**：如果不需要公开使用，可以通过 Nginx 限制 IP 访问
4. **定期检查**：通过管理后台定期查看用户注册情况，清理异常账号

---

## 6. API 接口一览

### 用户接口

| 接口 | 方法 | 说明 |
|------|------|------|
| `/api/auth/register` | POST | 注册新用户 |
| `/api/auth/login` | POST | 用户登录 |
| `/api/auth/logout` | POST | 用户登出 |
| `/api/auth/check` | GET | 检查登录状态 |

### 管理员接口

| 接口 | 方法 | 说明 |
|------|------|------|
| `/api/admin/login` | POST | 开发者登录 |
| `/api/admin/users` | GET | 获取所有用户列表 |
| `/api/admin/sessions` | GET | 获取活跃会话 |
| `/api/admin/kick` | POST | 踢出用户 |
| `/api/admin/stop` | POST | 停止用户监测 |

### 业务接口

| 接口 | 方法 | 说明 |
|------|------|------|
| `/api/captcha` | POST | 获取验证码 |
| `/api/login` | POST | 登录剧场账号 |
| `/api/programs` | GET | 获取剧目列表 |
| `/api/events/{pid}` | GET | 获取场次列表 |
| `/api/prices/{eid}` | GET | 获取票档列表 |
| `/api/select_price` | POST | 选择票档 |
| `/api/monitor/start` | POST | 启动监测 |
| `/api/monitor/stop` | POST | 停止监测 |
| `/api/monitor/status` | GET | 查询监测状态 |
| `/api/push/config` | POST | 保存推送配置 |
| `/api/push/test` | POST | 测试推送 |
| `/api/logs/stream` | GET | SSE 实时日志流 |
