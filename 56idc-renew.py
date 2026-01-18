#!/usr/bin/env python3
"""
56idc 自动登录续期脚本

cron: 0 8 * * 1
new Env('56idc-renew')

环境变量:
    ACCOUNTS_56IDC: 账号配置，格式: 邮箱:密码:2FA密钥,邮箱:密码 (2FA密钥可选)
    STAY_DURATION: 停留时间(秒)，默认10
    TOTP_API_URL: TOTP API地址
"""

import os
import asyncio
import json
import sys
import requests
from pathlib import Path
from datetime import datetime

# 青龙通知
try:
    from notify import send as notify_send
except ImportError:
    def notify_send(title, content): print(f"[通知] {title}: {content}")
from playwright.async_api import async_playwright

# 常量
LOGIN_URL = "https://56idc.net/login"
DASHBOARD_URL = "https://56idc.net/clientarea.php"
SESSION_DIR = Path(__file__).parent / "sessions"


def get_config():
    """获取配置"""
    return {
        'accounts_str': os.environ.get('ACCOUNTS_56IDC', ''),
        'stay_duration': int(os.environ.get('STAY_DURATION', '10')),
        'totp_api_url': os.environ.get('TOTP_API_URL', ''),
    }


def parse_accounts(accounts_str: str) -> list:
    accounts = []
    if not accounts_str:
        return accounts
    for item in accounts_str.split(','):
        item = item.strip()
        if ':' in item:
            parts = item.split(':')
            if len(parts) >= 2:
                accounts.append({
                    'email': parts[0].strip(),
                    'password': parts[1].strip(),
                    'totp_secret': parts[2].strip() if len(parts) >= 3 else ''
                })
    return accounts


def get_session_file(email: str) -> Path:
    SESSION_DIR.mkdir(exist_ok=True)
    safe_name = email.replace('@', '_at_').replace('.', '_')
    return SESSION_DIR / f"{safe_name}.json"




class Logger:
    @staticmethod
    def log(step: str, msg: str, status: str = "INFO"):
        timestamp = datetime.now().strftime("%H:%M:%S")
        symbols = {"INFO": "ℹ", "OK": "✓", "WARN": "⚠", "ERROR": "✗", "WAIT": "⏳"}
        symbol = symbols.get(status, "•")
        print(f"[{timestamp}] [{step}] {symbol} {msg}", flush=True)


def get_totp_code(secret: str, totp_api_url: str) -> str:
    if not totp_api_url or not secret:
        return ''
    try:
        response = requests.get(f"{totp_api_url}/totp/{secret}", timeout=10)
        if response.status_code == 200:
            return response.json().get('code', '')
    except Exception as e:
        Logger.log("TOTP", f"获取TOTP失败: {e}", "ERROR")
    return ''


async def cdp_click(cdp, x, y):
    """使用 CDP 模拟鼠标点击"""
    await cdp.send('Input.dispatchMouseEvent', {'type': 'mouseMoved', 'x': x, 'y': y})
    await asyncio.sleep(0.1)
    await cdp.send('Input.dispatchMouseEvent', {'type': 'mousePressed', 'x': x, 'y': y, 'button': 'left', 'clickCount': 1})
    await asyncio.sleep(0.05)
    await cdp.send('Input.dispatchMouseEvent', {'type': 'mouseReleased', 'x': x, 'y': y, 'button': 'left', 'clickCount': 1})


async def handle_cloudflare(page, cdp, max_attempts=30):
    """处理 Cloudflare 挑战页面"""
    Logger.log("CF", "处理 Cloudflare 验证...", "WAIT")
    for i in range(max_attempts):
        title = await page.title()
        if 'Just a moment' not in title:
            Logger.log("CF", "Cloudflare 验证通过!", "OK")
            return True
        # CDP 点击
        await cdp_click(cdp, 210, 290)
        await asyncio.sleep(2)
    Logger.log("CF", "Cloudflare 验证超时", "ERROR")
    return False


async def handle_turnstile(page, cdp):
    """处理表单中的 Turnstile 验证"""
    Logger.log("Turnstile", "等待 Turnstile 验证...", "WAIT")
    
    turnstile = await page.evaluate('''() => {
        const el = document.querySelector('.cf-turnstile');
        if (el) { const r = el.getBoundingClientRect(); return {x: r.x, y: r.y}; }
        return null;
    }''')
    
    if turnstile:
        x = int(turnstile['x'] + 30)
        y = int(turnstile['y'] + 32)
        Logger.log("Turnstile", f"点击 Turnstile ({x}, {y})", "INFO")
        await cdp_click(cdp, x, y)
        
        for i in range(15):
            await asyncio.sleep(1)
            response = await page.evaluate('() => document.querySelector("input[name=cf-turnstile-response]")?.value || ""')
            if len(response) > 10:
                Logger.log("Turnstile", "Turnstile 验证完成", "OK")
                return True
        
        Logger.log("Turnstile", "Turnstile 验证超时", "WARN")
        return False
    
    Logger.log("Turnstile", "未找到 Turnstile 元素", "INFO")
    return True


async def login_account(playwright, account: dict, config: dict) -> bool:
    email = account['email']
    password = account['password']
    totp_secret = account.get('totp_secret', '')
    
    Logger.log("Login", f"开始登录: {email}", "INFO")
    
    browser = None
    try:
        browser = await playwright.chromium.launch(
            headless=False,
            args=['--no-sandbox', '--disable-setuid-sandbox', '--disable-dev-shm-usage', '--disable-blink-features=AutomationControlled']
        )
        context = await browser.new_context(
            viewport={'width': 1280, 'height': 900},
            user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        )
        page = await context.new_page()
        cdp = await context.new_cdp_session(page)
        
        # 加载会话
        session_file = get_session_file(email)
        if session_file.exists():
            try:
                with open(session_file, 'r') as f:
                    cookies = json.load(f)
                await context.add_cookies(cookies)
                Logger.log("Session", "加载已保存的会话", "OK")
            except:
                pass
        
        # 访问登录页
        Logger.log("Navigate", f"访问 {LOGIN_URL}", "INFO")
        await page.goto(LOGIN_URL, wait_until='domcontentloaded', timeout=60000)
        
        # 处理 Cloudflare 挑战
        await handle_cloudflare(page, cdp)
        await asyncio.sleep(3)
        
        # 检查是否已登录
        if 'clientarea' in page.url:
            Logger.log("Login", "已登录，无需重新登录", "OK")
            cookies = await context.cookies()
            with open(session_file, 'w') as f:
                json.dump(cookies, f)
            return True
        
        # 处理 Turnstile
        await handle_turnstile(page, cdp)
        
        # 填写登录表单
        Logger.log("Form", "填写登录表单", "INFO")
        await page.fill('#inputEmail', email)
        await page.fill('#inputPassword', password)
        
        # 点击登录按钮
        Logger.log("Login", "点击登录按钮", "INFO")
        await page.click('button[type="submit"]')
        await asyncio.sleep(5)
        
        # 处理 2FA
        if totp_secret:
            try:
                totp_input = await page.query_selector('input[name="code"], input[name="twoFactorCode"]')
                if totp_input:
                    Logger.log("2FA", "需要2FA验证", "INFO")
                    totp_code = get_totp_code(totp_secret, config['totp_api_url'])
                    if totp_code:
                        await totp_input.fill(totp_code)
                        await page.click('button[type="submit"]')
                        await asyncio.sleep(3)
                        Logger.log("2FA", "已提交2FA验证码", "OK")
            except:
                pass
        
        # 再次处理可能的 Cloudflare
        await handle_cloudflare(page, cdp, 10)
        await asyncio.sleep(3)
        
        # 检查登录结果
        url = page.url
        text = await page.evaluate('() => document.body.innerText')
        
        if 'clientarea' in url or '退出' in text or 'Logout' in text:
            Logger.log("Login", f"登录成功: {email}", "OK")
            
            # 保存会话
            cookies = await context.cookies()
            with open(session_file, 'w') as f:
                json.dump(cookies, f)
            
            # 停留
            Logger.log("Stay", f"停留 {config['stay_duration']} 秒", "WAIT")
            await asyncio.sleep(config['stay_duration'])
            
            return True
        else:
            Logger.log("Login", f"登录失败: {email}", "ERROR")
            return False
            
    except Exception as e:
        Logger.log("Error", f"登录异常: {e}", "ERROR")
        return False
    finally:
        if browser:
            await browser.close()


async def main():
    Logger.log("Start", "56idc 自动登录脚本启动", "INFO")
    
    config = get_config()
    
    if not config['accounts_str']:
        Logger.log("Config", "错误: 未设置 ACCOUNTS_56IDC 环境变量", "ERROR")
        sys.exit(1)
    
    accounts = parse_accounts(config['accounts_str'])
    if not accounts:
        Logger.log("Config", "错误: 无有效账号配置", "ERROR")
        sys.exit(1)
    
    Logger.log("Config", f"共 {len(accounts)} 个账号", "INFO")
    
    
    success_count = 0
    fail_count = 0
    
    async with async_playwright() as playwright:
        for i, account in enumerate(accounts, 1):
            Logger.log("Progress", f"处理第 {i}/{len(accounts)} 个账号", "INFO")
            
            if await login_account(playwright, account, config):
                success_count += 1
            else:
                fail_count += 1
            
            if i < len(accounts):
                Logger.log("Wait", "等待 5 秒后处理下一个账号", "WAIT")
                await asyncio.sleep(5)
    
    Logger.log("Summary", f"完成: 成功 {success_count}, 失败 {fail_count}", "INFO")
    
    # 发送汇总通知
    if success_count == len(accounts):
        title = "56idc 登录成功"
        msg = f"✅ 全部 {success_count} 个账号登录成功"
    elif success_count > 0:
        title = "56idc 登录部分成功"
        msg = f"⚠️ 成功 {success_count} 个，失败 {fail_count} 个"
    else:
        title = "56idc 登录失败"
        msg = f"❌ 全部 {fail_count} 个账号登录失败"
    
    notify_send(title, msg)


if __name__ == '__main__':
    asyncio.run(main())
