# 56idc 自动登录脚本

自动登录 56idc.net 并保持会话活跃。

## 功能

- 支持多账号
- 自动登录（支持 2FA）
- 自动处理 Cloudflare Turnstile 验证
- 会话持久化
- Telegram 通知

## 安装

```bash
# 安装系统依赖
sudo apt install xvfb  # Debian/Ubuntu
# sudo yum install xorg-x11-server-Xvfb  # CentOS/RHEL

# 安装 uv
curl -LsSf https://astral.sh/uv/install.sh | sh

# 安装项目依赖
uv sync

# 安装 Playwright 浏览器
uv run playwright install chromium
```

## 配置

```bash
cp .env.example .env
vim .env
```

```env
# 账号配置 (格式: 邮箱:密码:2FA密钥)
# 多账号逗号分隔，2FA密钥可选
ACCOUNTS=user@example.com:password:TOTP_SECRET

# 登录后停留时间 (秒)
STAY_DURATION=10

# TOTP API (如果有2FA)
TOTP_API_URL=http://localhost:8000/totp

# Telegram 通知 (可选)
TELEGRAM_BOT_TOKEN=your_bot_token
TELEGRAM_CHAT_ID=your_chat_id
```

## 运行

```bash
xvfb-run uv run python 56idc_login.py
```

## 定时任务

建议每周运行一次保持账号活跃。

```bash
crontab -e

# 每周日上午 10 点运行
0 10 * * 0 cd /path/to/56idc-auto-login && xvfb-run /home/user/.local/bin/uv run python 56idc_login.py >> /tmp/56idc.log 2>&1
```
