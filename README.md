# 56idc 自动续期脚本

自动登录 56idc.net 并保持会话活跃。

## ⚠️ 免责声明

本项目仅供学习网页自动化技术使用。使用本脚本可能违反相关网站的服务条款，包括但不限于：
- 禁止使用自动化工具访问
- 禁止绕过安全验证措施

**使用本项目的风险由用户自行承担**，包括但不限于账号被封禁、服务被终止等后果。请在使用前仔细阅读相关网站的服务条款。

## 功能

- 支持多账号
- 自动登录（支持 2FA）
- 自动处理 Cloudflare Turnstile 验证
- 会话持久化
- Telegram 通知

## 安装

```bash
# 安装系统依赖
sudo apt install -y xvfb  # Debian/Ubuntu

# 安装 uv
curl -LsSf https://astral.sh/uv/install.sh | sh

# 克隆项目
git clone https://github.com/donma033x/56idc-renew.git
cd 56idc-renew

# 安装项目依赖
uv sync

# 安装 Playwright 浏览器
uv run playwright install chromium
```

## 配置

```bash
cp .env.example .env
nano .env
```

配置说明：
- `ACCOUNTS`: 账号配置，格式 `邮箱:密码:2FA密钥`，多账号用逗号分隔，2FA密钥可选
- `STAY_DURATION`: 登录后停留时间 (秒)
- `TOTP_API_URL`: TOTP API 地址 (如果有2FA)
- `TELEGRAM_BOT_TOKEN`: Telegram Bot Token (可选)
- `TELEGRAM_CHAT_ID`: Telegram Chat ID (可选)

## 运行

```bash
cd 56idc-renew
xvfb-run uv run python 56idc-renew.py
```

## 定时任务

建议每周运行一次保持账号活跃。

```bash
# 使用 crontab
crontab -e

# 每周日上午 10 点运行
0 10 * * 0 cd /path/to/56idc-renew && xvfb-run ~/.local/bin/uv run python 56idc-renew.py >> /tmp/56idc-renew.log 2>&1
```

## 文件说明

- `56idc-renew.py` - 主脚本
- `pyproject.toml` - 项目配置和依赖
- `.env.example` - 配置文件示例
- `sessions/` - 会话保存目录

## 许可证

MIT
