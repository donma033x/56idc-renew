#!/usr/bin/env python3
"""
56idc 自动登录脚本

功能:
1. 支持多账号
2. 自动通过 Cloudflare Turnstile 验证
3. 自动登录 56idc.net
4. 保存会话供下次使用

使用方法:
    xvfb-run python3 56idc_login.py
"""

import asyncio
import json
import requests
from pathlib import Path
from datetime import datetime
from playwright.async_api import async_playwright

# ==================== 加载配置 ====================
def load_env():
    env_file = Path(__file__).parent / '.env'
    env_vars = {}
    if not env_file.exists():
        print("错误: 未找到 .env 文件")
        exit(1)
    with open(env_file, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                key, value = line.split('=', 1)
                env_vars[key.strip()] = value.strip()
    return env_vars

ENV = load_env()
ACCOUNTS_STR = ENV.get('ACCOUNTS', '')
STAY_DURATION = int(ENV.get('STAY_DURATION', '10'))
TELEGRAM_BOT_TOKEN = ENV.get('TELEGRAM_BOT_TOKEN', '')
TELEGRAM_CHAT_ID = ENV.get('TELEGRAM_CHAT_ID', '')
TOTP_API_URL = ENV.get('TOTP_API_URL', '')

LOGIN_URL = "https://56idc.net/login.php"
DASHBOARD_URL = "https://56idc.net/clientarea.php"
SESSION_DIR = Path(__file__).parent / "sessions"

def parse_accounts(accounts_str: str) -> list:
    """解析账号配置，格式: 邮箱:密码:2FA密钥 (2FA密钥可选)"""
    accounts = []
    if not accounts_str:
        return accounts
    for item in accounts_str.split(','):
        item = item.strip()
        if ':' in item:
            parts = item.split(':')
            if len(parts) >= 2:
                email = parts[0].strip()
                password = parts[1].strip()
                totp_secret = parts[2].strip() if len(parts) >= 3 else ''
                accounts.append({
                    'email': email,
                    'password': password,
                    'totp_secret': totp_secret
                })
    return accounts

def get_session_file(email: str) -> Path:
    SESSION_DIR.mkdir(exist_ok=True)
    safe_name = email.replace('@', '_at_').replace('.', '_')
    return SESSION_DIR / f"{safe_name}.json"


class TelegramNotifier:
    def __init__(self, bot_token: str, chat_id: str):
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.enabled = bool(bot_token and chat_id)
    
    def send(self, message: str) -> bool:
        if not self.enabled:
            return False
        try:
            url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
            payload = {"chat_id": self.chat_id, "text": message, "parse_mode": "HTML"}
            response = requests.post(url, json=payload, timeout=10)
            return response.status_code == 200
        except:
            return False


class Logger:
    @staticmethod
    def log(step: str, msg: str, status: str = "INFO"):
        timestamp = datetime.now().strftime("%H:%M:%S")
        symbols = {"INFO": "ℹ", "OK": "✓", "WARN": "⚠", "ERROR": "✗", "WAIT": "⏳"}
        symbol = symbols.get(status, "•")
        print(f"[{timestamp}] [{step}] {symbol} {msg}")


class IDC56Login:
    def __init__(self, email: str, password: str, totp_secret: str = ''):
        self.email = email
        self.password = password
        self.totp_secret = totp_secret
        self.session_file = get_session_file(email)
        self.browser = None
        self.context = None
        self.page = None
        self.cdp = None
    
    def get_totp_code(self, wait_for_fresh: bool = False) -> str:
        """从TOTP API获取验证码"""
        if not TOTP_API_URL or not self.totp_secret:
            return ''
        try:
            url = f"{TOTP_API_URL}/totp/{self.totp_secret}"
            response = requests.get(url, timeout=10)
            if response.status_code == 200:
                data = response.json()
                code = data.get('code', '')
                remaining = data.get('remaining_seconds', 30)
                
                # 如果需要新鲜的验证码，且剩余时间少于5秒，等待下一个周期
                if wait_for_fresh and remaining < 5:
                    import time
                    Logger.log("2FA", f"验证码即将过期，等待 {remaining+1} 秒...", "WAIT")
                    time.sleep(remaining + 1)
                    # 重新获取
                    response = requests.get(url, timeout=10)
                    data = response.json()
                    code = data.get('code', '')
                    remaining = data.get('remaining_seconds', 30)
                
                Logger.log("2FA", f"获取验证码成功: {code} (剩余 {remaining} 秒)", "OK")
                return code
            else:
                Logger.log("2FA", f"API返回错误: {response.status_code}", "ERROR")
        except Exception as e:
            Logger.log("2FA", f"获取验证码失败: {e}", "ERROR")
        return ''
    
    async def handle_2fa(self) -> bool:
        """处理2FA验证"""
        url = self.page.url
        text = await self.page.evaluate('() => document.body.innerText')
        
        # 检查是否有2FA页面
        is_2fa_page = ('challenge' in url or 
                       '两步验证' in text or 
                       '2FA' in text or 
                       'Two-Factor' in text or
                       '认证器' in text or
                       'Authentication' in text)
        
        if not is_2fa_page:
            return True  # 不需要2FA
        
        Logger.log("2FA", "检测到需要两步验证", "WAIT")
        
        if not self.totp_secret:
            Logger.log("2FA", f"账号 {self.email} 未配置TOTP密钥", "ERROR")
            return False
        
        # 获取验证码 (等待新鲜的验证码，避免即将过期)
        code = self.get_totp_code(wait_for_fresh=True)
        if not code:
            Logger.log("2FA", "无法获取验证码", "ERROR")
            return False
        
        # 查找并填写验证码输入框
        selectors = [
            'input[name="code"]',
            'input[name="2fa_code"]', 
            'input[name="totp"]',
            'input#code',
            'input.form-control[type="text"]',
            'input[type="text"][maxlength="6"]',
            'input[placeholder*="验证码"]',
            'input[placeholder*="code"]',
        ]
        
        filled = False
        for selector in selectors:
            try:
                elem = await self.page.query_selector(selector)
                if elem:
                    await elem.fill(code)
                    Logger.log("2FA", f"已填写验证码: {code} (selector: {selector})", "OK")
                    filled = True
                    break
            except:
                continue
        
        if not filled:
            Logger.log("2FA", "无法找到验证码输入框", "ERROR")
            return False
        
        # 点击提交按钮
        await asyncio.sleep(0.5)
        try:
            submit_btn = await self.page.query_selector('button[type="submit"]') or \
                         await self.page.query_selector('input[type="submit"]') or \
                         await self.page.query_selector('button.btn-primary')
            if submit_btn:
                await submit_btn.click()
                Logger.log("2FA", "已提交验证码", "OK")
        except Exception as e:
            Logger.log("2FA", f"提交按钮点击失败: {e}", "WARN")
        
        await asyncio.sleep(5)
        
        # 检查是否还在验证页面
        new_url = self.page.url
        if 'incorrect' in new_url:
            Logger.log("2FA", "验证码错误", "ERROR")
            return False
        
        return True
    
    async def save_session(self):
        cookies = await self.context.cookies()
        with open(self.session_file, 'w') as f:
            json.dump(cookies, f, indent=2)
        Logger.log("会话", f"会话已保存", "OK")
    
    async def load_session(self) -> bool:
        if self.session_file.exists():
            try:
                with open(self.session_file) as f:
                    cookies = json.load(f)
                await self.context.add_cookies(cookies)
                Logger.log("会话", "已加载保存的会话", "OK")
                return True
            except:
                pass
        return False
    
    async def check_logged_in(self) -> bool:
        url = self.page.url
        if 'login' in url.lower():
            return False
        try:
            text = await self.page.evaluate('() => document.body.innerText')
            if '退出' in text or 'Logout' in text:
                return True
        except:
            pass
        return 'clientarea' in url
    
    async def login(self) -> bool:
        """执行登录"""
        Logger.log("登录", f"开始登录 {self.email}...", "WAIT")
        
        # 导航到登录页
        Logger.log("登录", "导航到登录页面...")
        await self.page.goto(LOGIN_URL)
        await asyncio.sleep(5)
        
        # 处理 CF 挑战
        Logger.log("登录", "处理 Cloudflare 验证...", "WAIT")
        for i in range(30):
            title = await self.page.title()
            if 'Just a moment' not in title:
                Logger.log("登录", "Cloudflare 验证通过!", "OK")
                break
            await self.cdp.send('Input.dispatchMouseEvent', {'type': 'mouseMoved', 'x': 210, 'y': 290})
            await asyncio.sleep(0.1)
            await self.cdp.send('Input.dispatchMouseEvent', {'type': 'mousePressed', 'x': 210, 'y': 290, 'button': 'left', 'clickCount': 1})
            await asyncio.sleep(0.05)
            await self.cdp.send('Input.dispatchMouseEvent', {'type': 'mouseReleased', 'x': 210, 'y': 290, 'button': 'left', 'clickCount': 1})
            await asyncio.sleep(2)
        
        # 等待页面加载
        Logger.log("登录", "等待页面加载...", "WAIT")
        await asyncio.sleep(5)
        
        # 处理表单 Turnstile
        Logger.log("验证", "等待 Turnstile 验证...", "WAIT")
        turnstile = await self.page.evaluate('''() => {
            const el = document.querySelector('.cf-turnstile');
            if (el) { const r = el.getBoundingClientRect(); return {x: r.x, y: r.y}; }
            return null;
        }''')
        
        turnstile_ok = False
        if turnstile:
            x = int(turnstile['x'] + 30)
            y = int(turnstile['y'] + 32)
            Logger.log("验证", f"点击 Turnstile ({x}, {y})", "INFO")
            
            await self.cdp.send('Input.dispatchMouseEvent', {'type': 'mouseMoved', 'x': x, 'y': y})
            await asyncio.sleep(0.1)
            await self.cdp.send('Input.dispatchMouseEvent', {'type': 'mousePressed', 'x': x, 'y': y, 'button': 'left', 'clickCount': 1})
            await asyncio.sleep(0.05)
            await self.cdp.send('Input.dispatchMouseEvent', {'type': 'mouseReleased', 'x': x, 'y': y, 'button': 'left', 'clickCount': 1})
            
            for i in range(15):
                await asyncio.sleep(1)
                response = await self.page.evaluate('() => document.querySelector("input[name=cf-turnstile-response]")?.value || ""')
                if len(response) > 10:
                    Logger.log("验证", "Turnstile 验证已完成", "OK")
                    turnstile_ok = True
                    break
            
            if not turnstile_ok:
                Logger.log("验证", "Turnstile 验证超时", "WARN")
        
        # 填写表单
        Logger.log("登录", "填写登录表单...")
        await self.page.fill('#inputEmail', self.email)
        Logger.log("登录", f"用户名: {self.email}", "OK")
        await self.page.fill('#inputPassword', self.password)
        Logger.log("登录", "密码: ********", "OK")
        
        # 点击登录
        Logger.log("登录", "点击登录按钮...")
        await self.page.click('button[type="submit"]')
        
        # 等待结果
        Logger.log("登录", "等待登录结果...", "WAIT")
        await asyncio.sleep(8)
        
        # 检查是否需要2FA
        if not await self.handle_2fa():
            Logger.log("登录", "2FA验证失败", "ERROR")
            return False
        
        # 检查结果
        url = self.page.url
        text = await self.page.evaluate('() => document.body.innerText')
        
        if 'clientarea' in url or '退出' in text or 'Logout' in text:
            Logger.log("登录", "登录成功!", "OK")
            return True
        
        if '账户或密码错误' in text or '密码错误' in text:
            Logger.log("登录", "账号或密码错误", "ERROR")
        else:
            Logger.log("登录", f"登录失败，当前 URL: {url}", "ERROR")
        return False
    
    async def run(self) -> bool:
        print()
        print("-" * 60)
        Logger.log("账号", f"开始处理: {self.email}", "WAIT")
        print("-" * 60)
        
        async with async_playwright() as p:
            self.browser = await p.chromium.launch(
                headless=False,
                args=['--disable-blink-features=AutomationControlled']
            )
            try:
                self.context = await self.browser.new_context(
                    viewport={'width': 1280, 'height': 900},
                    user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
                )
                self.page = await self.context.new_page()
                self.cdp = await self.context.new_cdp_session(self.page)
                Logger.log("启动", "浏览器已启动", "OK")
                
                # 加载会话
                has_session = await self.load_session()
                
                if has_session:
                    Logger.log("检查", "检查登录状态...", "WAIT")
                    await self.page.goto(DASHBOARD_URL)
                    await asyncio.sleep(5)
                    
                    for i in range(30):
                        title = await self.page.title()
                        if 'Just a moment' not in title:
                            break
                        await self.cdp.send('Input.dispatchMouseEvent', {'type': 'mouseMoved', 'x': 210, 'y': 290})
                        await asyncio.sleep(0.1)
                        await self.cdp.send('Input.dispatchMouseEvent', {'type': 'mousePressed', 'x': 210, 'y': 290, 'button': 'left', 'clickCount': 1})
                        await asyncio.sleep(0.05)
                        await self.cdp.send('Input.dispatchMouseEvent', {'type': 'mouseReleased', 'x': 210, 'y': 290, 'button': 'left', 'clickCount': 1})
                        await asyncio.sleep(2)
                    
                    await asyncio.sleep(2)
                    
                    if await self.check_logged_in():
                        Logger.log("检查", "会话有效，已登录", "OK")
                    else:
                        Logger.log("检查", "会话已过期", "WARN")
                        if not await self.login():
                            return False
                else:
                    Logger.log("检查", "无保存的会话，需要登录", "INFO")
                    if not await self.login():
                        return False
                
                Logger.log("保活", f"停留 {STAY_DURATION} 秒...", "WAIT")
                for i in range(STAY_DURATION, 0, -1):
                    print(f"\r[{datetime.now().strftime('%H:%M:%S')}] [保活] ⏳ 剩余 {i} 秒...", end='', flush=True)
                    await asyncio.sleep(1)
                print()
                Logger.log("保活", "停留完成", "OK")
                
                await self.save_session()
                Logger.log("结果", f"{self.email} 完成!", "OK")
                return True
            finally:
                await self.browser.close()


async def main():
    accounts = parse_accounts(ACCOUNTS_STR)
    if not accounts:
        print("错误: 未配置账号信息")
        exit(1)
    
    telegram = TelegramNotifier(TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID)
    
    print()
    print("=" * 60)
    print("  56idc 自动登录脚本")
    print("=" * 60)
    print(f"  账号数量: {len(accounts)}")
    print(f"  停留时间: {STAY_DURATION} 秒")
    print(f"  开始时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 60)
    
    results = []
    for i, account in enumerate(accounts, 1):
        print(f"\n[进度] 处理账号 {i}/{len(accounts)}")
        login = IDC56Login(account['email'], account['password'], account.get('totp_secret', ''))
        success = await login.run()
        results.append({'email': account['email'], 'success': success})
    
    # 汇总
    print()
    print("=" * 60)
    print("  📊 任务汇总")
    print("=" * 60)
    success_count = sum(1 for r in results if r['success'])
    for r in results:
        status = "✓ 成功" if r['success'] else "✗ 失败"
        print(f"  {status}: {r['email']}")
    print("-" * 60)
    print(f"  总计: {success_count}/{len(results)} 成功")
    print("=" * 60)
    
    # Telegram
    if telegram.enabled:
        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        if success_count == len(results):
            emoji, title = "✅", "56idc 登录成功"
        elif success_count > 0:
            emoji, title = "⚠️", "56idc 登录部分成功"
        else:
            emoji, title = "❌", "56idc 登录失败"
        
        msg_lines = [f"{emoji} <b>{title}</b>", ""]
        for r in results:
            status = "✅" if r['success'] else "❌"
            msg_lines.append(f"{status} {r['email']}")
        msg_lines.extend(["", f"📊 结果: {success_count}/{len(results)} 成功", f"🕒 时间: {now}"])
        telegram.send("\n".join(msg_lines))
        print("✓ 已发送 Telegram 通知")
    
    return success_count == len(results)


if __name__ == '__main__':
    result = asyncio.run(main())
    exit(0 if result else 1)
