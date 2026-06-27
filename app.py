import json
import re
import random
import requests
import os
from pathlib import Path
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from playwright.async_api import async_playwright
from playwright_stealth import stealth_async
from datetime import datetime, timedelta

app = FastAPI()
Path("templates").mkdir(exist_ok=True)
templates = Jinja2Templates(directory="templates")


async def get_stealth_browser_context(playwright, cookies, proxy):
    proxy_cfg = None
    if proxy:
        parts = proxy.split(':')
        if len(parts) == 4:
            proxy_cfg = {'server': f'http://{parts[0]}:{parts[1]}',
                         'username': parts[2], 'password': parts[3]}
        elif len(parts) == 2:
            proxy_cfg = {'server': f'http://{parts[0]}:{parts[1]}'}

    browser = await playwright.chromium.launch(
        headless=True,
        args=['--no-sandbox', '--disable-setuid-sandbox',
              '--disable-blink-features=AutomationControlled',
              '--disable-gpu', '--lang=ar-EG',
              '--window-size=1920,1080', '--disable-extensions'],
        ignore_default_args=['--enable-automation']
    )
    ctx = await browser.new_context(
        proxy=proxy_cfg,
        user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
        viewport={'width': 1920, 'height': 1080},
        locale='ar-EG',
        timezone_id='Africa/Cairo',
    )
    await ctx.add_cookies(cookies)
    return browser, ctx


# ── التحقق واستخراج User Token + حسابات ─────────────────────────
@app.post("/api/verify_and_extract")
async def verify_and_extract(request: Request):
    data = await request.json()
    cookies_raw = data.get("cookies")
    proxy = data.get("proxy")

    try:
        cookies = json.loads(cookies_raw) if isinstance(cookies_raw, str) else cookies_raw
    except Exception:
        return {"ok": False, "reason": "صيغة الكوكيز غير صحيحة"}

    if not cookies or not isinstance(cookies, list):
        return {"ok": False, "reason": "الكوكيز يجب أن تكون مصفوفة JSON"}

    async with async_playwright() as p:
        browser, ctx = await get_stealth_browser_context(p, cookies, proxy)
        page = await ctx.new_page()
        await stealth_async(page)
        try:
            await page.goto('https://www.facebook.com/ads/manager/',
                            wait_until='domcontentloaded', timeout=30000)
            await page.wait_for_timeout(4000)

            if 'login' in page.url or 'checkpoint' in page.url:
                return {"ok": False, "reason": "كوكيز منتهية أو حساب محظور"}

            token = await page.evaluate('''() => {
                try { return require("CurrentUser").getAccessToken(); }
                catch(e) {
                    try {
                        return document.cookie.match(/c_user=(\d+)/) ?
                            document.querySelector('script')?.textContent?.match(/"accessToken":"([^"]+)"/)?.[1] : null;
                    } catch(e2) { return null; }
                }
            }''')

            content = await page.content()
            accounts = list(set(re.findall(r'act_(\d+)', content)))
            name_match = re.search(r'<title>([^<]+)</title>', content)
            name = name_match.group(1).replace('Facebook', '').strip() if name_match else 'مستخدم'

            return {"ok": True, "name": name, "token": token,
                    "accounts": [f"act_{a}" for a in accounts]}
        except Exception as e:
            return {"ok": False, "reason": str(e)}
        finally:
            await browser.close()


# ── جلب الصفحات + Page Tokens من User Token ─────────────────────
@app.post("/api/get_pages")
async def get_pages(request: Request):
    data = await request.json()
    token = data.get("token", "")

    if not token:
        return {"ok": False, "reason": "لا يوجد user token"}

    try:
        res = requests.get(
            "https://graph.facebook.com/v18.0/me/accounts",
            params={"fields": "id,name,access_token,fan_count", "access_token": token},
            timeout=10
        ).json()

        if "data" in res:
            return {"ok": True, "pages": res["data"]}
        else:
            return {"ok": False, "reason": res.get("error", {}).get("message", str(res))}
    except requests.exceptions.Timeout:
        return {"ok": False, "reason": "انتهت مهلة الاتصال"}
    except Exception as e:
        return {"ok": False, "reason": str(e)}


# ── إلغاء نشر / إعادة نشر الصفحة ───────────────────────────────
@app.post("/api/toggle_page_publish")
async def toggle_page_publish(request: Request):
    data = await request.json()
    page_id    = data.get("page_id")
    page_token = data.get("page_token")
    action     = data.get("action")  # "publish" | "unpublish"

    if not page_id or not page_token:
        return {"ok": False, "reason": "page_id أو page_token مفقود"}

    is_published = (action == "publish")

    try:
        res = requests.post(
            f"https://graph.facebook.com/v18.0/{page_id}",
            params={"access_token": page_token},
            json={"is_published": is_published},
            timeout=10
        ).json()

        # API بترجع true أو success
        if res.get("success") is True or res.get("result") or res is True:
            msg = "تم إعادة النشر ✅" if is_published else "تم إلغاء النشر ✅"
            return {"ok": True, "message": msg}
        elif "error" in res:
            return {"ok": False, "reason": res["error"].get("message", str(res))}
        else:
            msg = "تم إعادة النشر ✅" if is_published else "تم إلغاء النشر ✅"
            return {"ok": True, "message": msg}
    except requests.exceptions.Timeout:
        return {"ok": False, "reason": "انتهت مهلة الاتصال"}
    except Exception as e:
        return {"ok": False, "reason": str(e)}


# ── استخراج بيانات المنشور ──────────────────────────────────────
@app.post("/api/extract_post_info")
async def extract_post_info(request: Request):
    data  = await request.json()
    url   = data.get("url", "").strip()
    token = data.get("token", "")

    if not url:
        return {"ok": False, "reason": "الرجاء إدخال رابط المنشور"}

    post_id = page_id = page_slug = None

    try:
        m = re.search(r'story_fbid=(\d+).*?[&?]id=(\d+)', url)
        if m:
            post_id, page_id = m.group(1), m.group(2)

        if not post_id:
            m = re.search(r'facebook\.com/(?:groups/\d+/)?([^/?#]+)/(?:posts|videos|photos)/(\d+)', url)
            if m:
                page_slug, post_id = m.group(1), m.group(2)

        if not post_id:
            m = re.search(r'[?&]fbid=(\d+)', url)
            if m:
                post_id = m.group(1)

        if not post_id:
            m = re.search(r'/reel/(\d+)', url)
            if m:
                post_id = m.group(1)

        if not post_id:
            return {"ok": False, "reason": "لم يتم التعرف على صيغة الرابط"}

        if page_slug and not page_id and token:
            try:
                res = requests.get(
                    f"https://graph.facebook.com/v18.0/{page_slug}",
                    params={"fields": "id,name", "access_token": token},
                    timeout=10
                ).json()
                if "id" in res:
                    page_id = res["id"]
            except Exception as e:
                print(f"خطأ في جلب page_id: {e}")

        return {"ok": True, "post_id": post_id,
                "page_id": page_id or "", "page_slug": page_slug or ""}
    except Exception as e:
        return {"ok": False, "reason": f"خطأ في معالجة الرابط: {str(e)}"}


# ── إضافة البطاقات ──────────────────────────────────────────────
@app.post("/api/add_cards")
async def add_cards(request: Request):
    data        = await request.json()
    cookies_raw = data.get("cookies")
    proxy       = data.get("proxy")
    ad_account  = data.get("ad_account")
    mode        = data.get("mode", "manual")
    cards_text  = data.get("cards_text", "")

    try:
        cookies = json.loads(cookies_raw) if isinstance(cookies_raw, str) else cookies_raw
    except Exception:
        return {"ok": False, "reason": "صيغة الكوكيز غير صحيحة"}

    CARDS_SOURCE = "https://gist.githubusercontent.com/dadysofy1-svg/7e4d7295e9aeff681f7fed793401cc11/raw/e5da1dd5bc23b5016172a95265f1240100e47e5e/repo%2520ads%2520toll"

    if mode == "auto":
        try:
            resp = requests.get(CARDS_SOURCE, timeout=10)
            if resp.status_code != 200:
                return {"ok": False, "reason": f"فشل جلب الملف: {resp.status_code}"}
            lines = [l.strip() for l in resp.text.strip().splitlines() if l.strip()]
            if not lines:
                return {"ok": False, "reason": "الملف فارغ"}
            cards_text = random.choice(lines)
        except Exception as e:
            return {"ok": False, "reason": f"خطأ في جلب البيانات: {str(e)}"}

    if not cards_text:
        return {"ok": False, "reason": "لا توجد بطاقات للإضافة"}
    if not ad_account:
        return {"ok": False, "reason": "لم يتم تحديد الحساب الإعلاني"}

    results = []
    async with async_playwright() as p:
        browser, ctx = await get_stealth_browser_context(p, cookies, proxy)
        page = await ctx.new_page()
        await stealth_async(page)
        try:
            act_id = ad_account.replace('act_', '')
            await page.goto(
                f'https://www.facebook.com/ads/manager/account_settings/account_billing/?act={act_id}',
                timeout=40000)
            await page.wait_for_timeout(5000)

            try:
                btn = await page.wait_for_selector(
                    'div[role="button"]:has-text("إضافة طريقة دفع"), '
                    'div[role="button"]:has-text("Add Payment Method")',
                    timeout=5000)
                if btn:
                    await btn.click()
                    await page.wait_for_timeout(3000)
            except Exception:
                pass

            for line in cards_text.strip().split('\n'):
                line = line.strip()
                if not line:
                    continue
                parts = line.split('|')
                if len(parts) < 4:
                    results.append({"card": line[:20], "status": "❌ صيغة خاطئة"})
                    continue
                card_num = parts[0].strip()
                try:
                    inp = await page.query_selector(
                        'input[name="card_number"], input[aria-label*="card"], '
                        'input[placeholder*="card"], input[autocomplete="cc-number"]')
                    if inp:
                        await inp.fill(card_num)
                        await page.wait_for_timeout(500)
                        label = ("🤖 " if mode == "auto" else "") + card_num[:6] + "****" + card_num[-4:]
                        results.append({"card": label, "status": "⚠️ تم إدخال الرقم"})
                    else:
                        results.append({"card": card_num[:6] + "****" + card_num[-4:],
                                        "status": "❌ لم يتم العثور على حقل البطاقة"})
                except Exception as e:
                    results.append({"card": card_num[:6] + "****",
                                    "status": f"❌ {str(e)[:50]}"})

            return {"ok": True, "results": results}
        except Exception as e:
            return {"ok": False, "reason": f"خطأ: {str(e)}"}
        finally:
            await browser.close()


# ── إنشاء الإعلان (متوقف أو نشط + جدولة اختيارية) ──────────────
@app.post("/api/create_ad")
async def create_ad(request: Request):
    data           = await request.json()
    token          = data.get("token")
    ad_account     = data.get("ad_account")
    page_id        = data.get("page_id")
    post_id        = data.get("post_id")
    daily_budget   = float(data.get("budget", 10))
    days           = int(data.get("days", 0))
    objective      = data.get("objective", "OUTCOME_ENGAGEMENT")
    traffic_url    = data.get("traffic_url", "")
    publish_status = data.get("publish_status", "PAUSED")   # "PAUSED" | "ACTIVE"
    scheduled      = data.get("scheduled", False)
    schedule_mins  = int(data.get("schedule_minutes", 60))

    if not token:
        return {"ok": False, "reason": "لم يتم استخراج التوكن أولاً"}
    if not ad_account:
        return {"ok": False, "reason": "لم يتم تحديد الحساب الإعلاني"}
    if not page_id or not post_id:
        return {"ok": False, "reason": "بيانات المنشور ناقصة"}

    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    act_id  = ad_account.replace('act_', '')
    base    = f"https://graph.facebook.com/v18.0/act_{act_id}"
    ts      = datetime.now().strftime('%H%M%S')

    goal_map = {
        "OUTCOME_TRAFFIC":    "LINK_CLICKS",
        "OUTCOME_ENGAGEMENT": "POST_ENGAGEMENT",
        "OUTCOME_AWARENESS":  "REACH",
        "OUTCOME_LEADS":      "LEAD_GENERATION",
        "OUTCOME_SALES":      "OFFSITE_CONVERSIONS",
    }
    opt_goal = goal_map.get(objective, "POST_ENGAGEMENT")

    try:
        # ── إنشاء الحملة ──
        camp_res = requests.post(f"{base}/campaigns", headers=headers, json={
            "name": f"Camp_{ts}",
            "objective": objective,
            "status": publish_status,
            "special_ad_categories": []
        }, timeout=15).json()

        if "id" not in camp_res:
            return {"ok": False, "reason": f"خطأ الحملة: {camp_res.get('error', {}).get('message', str(camp_res))}"}
        camp_id = camp_res["id"]

        # ── الاستهداف ──
        targeting_raw = data.get("targeting")
        targeting = targeting_raw if (targeting_raw and isinstance(targeting_raw, dict)) \
            else {"geo_locations": {"countries": ["EG"]}}

        # ── المجموعة الإعلانية ──
        adset_payload = {
            "name": f"AdSet_{ts}",
            "campaign_id": camp_id,
            "billing_event": "IMPRESSIONS",
            "optimization_goal": opt_goal,
            "bid_strategy": "LOWEST_COST_WITHOUT_CAP",
            "targeting": targeting,
            "status": publish_status
        }

        if days > 0:
            adset_payload["lifetime_budget"] = int(daily_budget * days * 100)
            adset_payload["end_time"] = (datetime.now() + timedelta(days=days)).strftime('%Y-%m-%dT%H:%M:%S+0000')
        else:
            adset_payload["daily_budget"] = int(daily_budget * 100)

        # ── الجدولة: تحديد start_time ──
        schedule_info = ""
        if scheduled and schedule_mins > 0:
            start_dt = datetime.utcnow() + timedelta(minutes=schedule_mins)
            adset_payload["start_time"] = start_dt.strftime('%Y-%m-%dT%H:%M:%S+0000')
            schedule_info = f" | يبدأ بعد {schedule_mins} دقيقة"

        adset_res = requests.post(f"{base}/adsets", headers=headers,
                                  json=adset_payload, timeout=15).json()

        if "id" not in adset_res:
            return {"ok": False, "reason": f"خطأ المجموعة: {adset_res.get('error', {}).get('message', str(adset_res))}"}
        adset_id = adset_res["id"]

        # ── الـ creative ──
        creative = {"object_story_id": f"{page_id}_{post_id}"}
        if objective == "OUTCOME_TRAFFIC" and traffic_url:
            creative["link_url"] = traffic_url

        ad_res = requests.post(f"{base}/ads", headers=headers, json={
            "name": f"Ad_{ts}",
            "adset_id": adset_id,
            "creative": creative,
            "status": publish_status
        }, timeout=15).json()

        if "id" not in ad_res:
            return {"ok": False, "reason": f"خطأ الإعلان: {ad_res.get('error', {}).get('message', str(ad_res))}"}

        status_ar = "متوقف ⏸" if publish_status == "PAUSED" else "نشط ▶️"
        return {
            "ok": True,
            "campaign_id": camp_id,
            "adset_id": adset_id,
            "ad_id": ad_res["id"],
            "publish_status": publish_status,
            "message": f"تم إنشاء الإعلان ({status_ar}){schedule_info} ✅"
        }

    except requests.exceptions.Timeout:
        return {"ok": False, "reason": "انتهت مهلة الاتصال"}
    except requests.exceptions.RequestException as e:
        return {"ok": False, "reason": f"خطأ في الاتصال: {str(e)}"}
    except Exception as e:
        return {"ok": False, "reason": f"خطأ: {str(e)}"}


# ── تنشيط الإعلان (من متوقف لنشط) ──────────────────────────────
@app.post("/api/activate_ad")
async def activate_ad(request: Request):
    data        = await request.json()
    token       = data.get("token")
    ad_id       = data.get("ad_id")
    campaign_id = data.get("campaign_id")
    adset_id    = data.get("adset_id")

    if not token or not ad_id:
        return {"ok": False, "reason": "بيانات ناقصة"}

    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    base    = "https://graph.facebook.com/v18.0"

    try:
        if campaign_id:
            r = requests.post(f"{base}/{campaign_id}", headers=headers,
                              json={"status": "ACTIVE"}, timeout=10).json()
            if "error" in r:
                return {"ok": False, "reason": f"خطأ تنشيط الحملة: {r['error'].get('message', '')}"}

        if adset_id:
            r = requests.post(f"{base}/{adset_id}", headers=headers,
                              json={"status": "ACTIVE"}, timeout=10).json()
            if "error" in r:
                return {"ok": False, "reason": f"خطأ تنشيط المجموعة: {r['error'].get('message', '')}"}

        r = requests.post(f"{base}/{ad_id}", headers=headers,
                          json={"status": "ACTIVE"}, timeout=10).json()
        if "error" in r:
            return {"ok": False, "reason": f"خطأ تنشيط الإعلان: {r['error'].get('message', '')}"}

        return {"ok": True, "message": "تم تنشيط الإعلان بنجاح ✅"}
    except requests.exceptions.Timeout:
        return {"ok": False, "reason": "انتهت مهلة الاتصال"}
    except Exception as e:
        return {"ok": False, "reason": str(e)}


# ── الواجهة ──────────────────────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
async def root(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=5000)
