#!/usr/bin/env python3
"""
上海文化广场 余票监测 & 自动下单 — Web 版
========================================
Flask + SSE 实现，可在无界面服务器上运行，通过浏览器远程操作。
"""

import re
import io
import json
import time
import os
import uuid
import secrets
import base64
import queue
import threading
import collections
import functools
from datetime import datetime, timedelta

import requests
from bs4 import BeautifulSoup
from PIL import Image
from playwright.sync_api import sync_playwright
import ddddocr
from flask import Flask, render_template, request, jsonify, Response, session, redirect, url_for

# ─── 常量 ─────────────────────────────────────────────────────────────────────

BASE_URL = "https://m.shcstheatre.com"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/120.0.0.0 Safari/537.36",
    "Referer": f"{BASE_URL}/Program/ProgramListWeChat.aspx?GROUP_ID=351",
}

# ─── 浏览器层 ──────────────────────────────────────────────────────────────────

class BrowserManager:
    def __init__(self):
        self._pw = None
        self._browser = None

    def start(self):
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(headless=True)

    def stop(self):
        if self._browser:
            self._browser.close()
        if self._pw:
            self._pw.stop()

    def fetch_captcha(self) -> tuple[bytes, dict]:
        """返回 (png_bytes, cookies_dict)"""
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_page()
            try:
                page.goto(f"{BASE_URL}/PersonalCenter/loginwechat.aspx",
                          wait_until="networkidle", timeout=15000)
                captcha_el = page.locator("#yanzhengma")
                if captcha_el.count() == 0:
                    raise RuntimeError("页面中未找到验证码元素 #yanzhengma")
                img_bytes = captcha_el.screenshot()
                cookies = {c["name"]: c["value"] for c in page.context.cookies()}
                return img_bytes, cookies
            finally:
                page.close()
                browser.close()

    @staticmethod
    def login_with_captcha(username: str, password: str, ocr_engine) -> tuple[bool, str, dict]:
        """一键登录：Playwright 获取验证码 + OCR + HTTP 登录，返回 (成功, 消息, cookies)"""
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_page()
            try:
                page.goto(f"{BASE_URL}/PersonalCenter/loginwechat.aspx",
                          wait_until="networkidle", timeout=15000)
                captcha_el = page.locator("#yanzhengma")
                if captcha_el.count() == 0:
                    return False, "页面中未找到验证码元素", {}
                img_bytes = captcha_el.screenshot()
                cookies = {c["name"]: c["value"] for c in page.context.cookies()}
                captcha_text = ocr_engine.classification(img_bytes)
            finally:
                page.close()
                browser.close()

        # 用 requests 发送登录请求
        import requests as req
        session = req.Session()
        for name, value in cookies.items():
            session.cookies.set(name, value, domain="m.shcstheatre.com")
        login_url = f"{BASE_URL}/WebAPIWeChat.ashx?op=CustomerLoginWeChat"
        data = {"username": username, "newpassword": password,
                "loginsurecode": captcha_text, "sessioncode": captcha_text,
                "cookieOP_ID": "", "OPEND_ID_COOKIE": ""}
        resp = session.post(login_url, data=data)
        result = resp.json()
        if result.get("code") == 0 and result.get("iRtn") == 0:
            return True, result.get("token", ""), cookies
        return False, result.get("msg", "登录失败"), {}

    @staticmethod
    def select_seat_and_buy(program_id, event_id, price_id,
                            cookies, token, qty=1, log_callback=None):
        def _do_select():
            def plog(msg):
                if log_callback:
                    log_callback(msg)

            with sync_playwright() as pw:
                browser = pw.chromium.launch(headless=True)
                context = browser.new_context()
                page = context.new_page()
                try:
                    cookie_list = []
                    for name, value in cookies.items():
                        for domain in ("m.shcstheatre.com", "seatmb2.shcstheatre.com"):
                            cookie_list.append({
                                "name": name, "value": value,
                                "domain": domain, "path": "/"
                            })
                    if cookie_list:
                        context.add_cookies(cookie_list)
                    page.add_init_script(f"window.token='{token}';")

                    seat_url = (f"{BASE_URL}/Program/ProgramDetailsWeChat.aspx"
                                f"?xz_program_id={program_id}&xz_event_id={event_id}")
                    plog(f"正在加载选座页面: {seat_url}")
                    page.goto(seat_url, wait_until="networkidle", timeout=30000)
                    page.wait_for_timeout(3000)

                    # 阶段1：事件选择弹窗
                    event_modal_visible = page.evaluate("""() => {
                        let btn = document.querySelector('[onclick*="EventSelectChange"]');
                        return btn ? (btn.offsetParent !== null) : false;
                    }""")
                    if event_modal_visible:
                        plog("检测到事件选择弹窗，点击确定...")
                        try:
                            page.locator("[onclick*='EventSelectChange']").click()
                        except Exception as e:
                            plog(f"点击确定失败: {e}")
                        for i in range(20):
                            page.wait_for_timeout(500)
                            still = page.evaluate("""() => {
                                let b = document.querySelector('[onclick*="EventSelectChange"]');
                                return b && b.offsetParent !== null;
                            }""")
                            if not still:
                                break
                        page.wait_for_timeout(1000)

                    # 阶段2：等待座位数据
                    plog("等待座位数据加载...")
                    seats_loaded = False
                    for i in range(60):
                        page.wait_for_timeout(500)
                        state = page.evaluate("""() => {
                            let sc = document.querySelectorAll('.s-c').length;
                            let loading = document.getElementById('cart_load_msg');
                            let lh = !loading || loading.style.display === 'none' || loading.offsetParent === null;
                            let pg = typeof pg_seats_data === 'object' ? Object.keys(pg_seats_data).length : 0;
                            return {sc: sc, loading: lh, pg: pg};
                        }""")
                        if state["sc"] > 0 and state["loading"]:
                            plog(f"座位数据已加载: {state['sc']}个元素")
                            seats_loaded = True
                            break
                    if not seats_loaded:
                        page.screenshot(path="seat_map_no_load.png")
                        plog("座位数据加载超时")

                    # 阶段3：找匹配座位
                    price_amount = page.evaluate(f"""(pid) => {{
                        if (window.price_data && window.price_data[pid]) return window.price_data[pid].I_PRICE_AMT;
                        return null;
                    }}""", price_id)
                    plog(f"price_id={price_id} -> 金额={price_amount}")

                    target_seats = page.evaluate(f"""(targetPrice) => {{
                        let seats = [];
                        document.querySelectorAll('.s-c').forEach(function(el) {{
                            let cls = el.className || '';
                            let pa = el.getAttribute('PA') || '';
                            let notSale = el.getAttribute('NOT_SALE') || '0';
                            let is_sold = cls.includes('saled') || cls.includes('mk_saled');
                            let is_stu = cls.includes('stu') || cls.includes('mk_stu');
                            if (!is_sold && !is_stu && notSale !== '1' && String(pa) === String(targetPrice)) {{
                                seats.push({{ed: el.getAttribute('ED')||'', esd: el.getAttribute('ESD')||'',
                                    zone: el.getAttribute('ZN')||'', row: el.getAttribute('RW')||'',
                                    col: el.getAttribute('CL')||'', pa: pa}});
                            }}
                        }});
                        return seats;
                    }}""", price_amount)
                    plog(f"匹配价格¥{price_amount}的可用座位: {len(target_seats)}个")

                    # 阶段4：选座
                    seat_selected = False
                    if target_seats:
                        ts = target_seats[0]
                        ed = ts.get("ed") or str(event_id)
                        esd = ts.get("esd", "")
                        result = page.evaluate(f"""(function() {{
                            try {{
                                if (typeof SeatsSelected === 'function') {{ SeatsSelected('{ed}', '{esd}'); return {{ok:true}}; }}
                                return {{ok:false, err:'SeatsSelected not found'}};
                            }} catch(e) {{ return {{ok:false, err:e.toString()}}; }}
                        }})()""")
                        if result.get("ok"):
                            seat_selected = True
                            page.wait_for_timeout(1500)

                    if not seat_selected and target_seats:
                        esd = target_seats[0]["esd"]
                        page.evaluate(f"""document.querySelector(".s-c[ESD='{esd}']").click()""")
                        page.wait_for_timeout(1500)
                        seat_selected = True

                    page.screenshot(path="seat_map_after_select.png")

                    # 阶段5：验证购物车
                    cart_state = page.evaluate("""() => {
                        let btn = document.querySelector('[onclick*="Buy"]');
                        let text = btn ? btn.textContent.trim() : '';
                        let gray = btn ? btn.className.includes('gray_s') : true;
                        let selected = document.querySelectorAll('.s-c.selected, .s-c.mk_selected');
                        return {cart_text: text, gray: gray, selected_count: selected.length};
                    }""")
                    plog(f"购物车状态: {json.dumps(cart_state, ensure_ascii=False)}")

                    price_match = re.search(r'￥(\d+)', cart_state.get("cart_text", ""))
                    cart_amount = int(price_match.group(1)) if price_match else 0

                    if cart_state.get("selected_count", 0) == 0 and cart_amount == 0:
                        page.evaluate("typeof CartChange === 'function' && CartChange()")
                        page.wait_for_timeout(1000)
                        cart_state = page.evaluate("""() => {
                            return {selected_count: document.querySelectorAll('.s-c.selected, .s-c.mk_selected').length};
                        }""")

                    if cart_state.get("selected_count", 0) == 0:
                        return {"code": -1, "msg": f"未选到座位。目标价格¥{price_amount}，可选{len(target_seats)}个"}

                    # 阶段6：加入购物车
                    if cart_state.get("gray"):
                        page.evaluate("""() => {
                            let btn = document.querySelector('.AddShopCar');
                            if (btn) btn.classList.remove('gray_s');
                            if (typeof bo_gray !== 'undefined') bo_gray = false;
                            if (typeof Buy === 'function') Buy();
                        }""")
                    else:
                        page.evaluate("typeof Buy === 'function' && Buy()")
                    plog("已触发 Buy()，等待页面跳转...")

                    # 阶段7：等待跳转
                    for i in range(30):
                        page.wait_for_timeout(1000)
                        cur = page.url
                        if any(kw in cur for kw in ["OrderStep", "Order", "ShoppingCart", "ShopCar", "Cart", "BuyTicket", "Commit", "PayConfirm"]):
                            plog(f"已跳转到订单页 ({i+1}s): {cur}")
                            break

                    page.wait_for_timeout(3000)
                    page.screenshot(path="seat_map_order_page.png")

                    # 阶段8：结算 → 弹窗 → 提交 → 友情提示
                    def _page_has_exact_text(text):
                        return page.evaluate(f"""(function() {{
                            let target = '{text}';
                            let els = document.querySelectorAll('button, a, input[type="button"], input[type="submit"], [onclick], [class*="btn"], [class*="Btn"], [class*="pop"], [class*="Pop"], [class*="modal"], [class*="Modal"], [class*="dialog"], [class*="Dialog"], [class*="layer"], [class*="Layer"], [class*="tip"], [class*="Tip"], [class*="hint"], [class*="Hint"], [class*="alert"], [class*="Alert"], [class*="confirm"], [class*="Confirm"], [class*="msg"], [class*="Msg"], div, span, p, label, h1, h2, h3, h4');
                            for (let el of els) {{
                                let txt = (el.textContent || el.value || '').trim();
                                if (txt.includes(target)) {{ let r = el.getBoundingClientRect(); if (r.width > 0 && r.height > 0) return true; }}
                            }}
                            return false;
                        }})()""")

                    def _popup_visible():
                        return page.evaluate("""(function() {
                            let sels = '[class*="pop"], [class*="Pop"], [class*="modal"], [class*="Modal"], [class*="dialog"], [class*="Dialog"], [class*="layer"], [class*="Layer"], [class*="tip"], [class*="Tip"], [class*="alert"], [class*="Alert"], [class*="confirm"], [class*="Confirm"], [class*="overlay"], [class*="Overlay"], [class*="mask"], [class*="Mask"]';
                            let els = document.querySelectorAll(sels);
                            for (let el of els) { let r = el.getBoundingClientRect(); if (r.width > 100 && r.height > 100) return true; }
                            return false;
                        })()""")

                    def _find_button_coords(text):
                        coords_json = page.evaluate(f"""(function() {{
                            let target = '{text}';
                            let clickable = 'button, a, input[type="button"], input[type="submit"], [onclick], [class*="btn"], [class*="Btn"]';
                            for (let el of document.querySelectorAll(clickable)) {{
                                let txt = (el.textContent || el.value || '').trim();
                                if (txt.includes(target)) {{ let r = el.getBoundingClientRect(); if (r.width > 0 && r.height > 0) return JSON.stringify({{x:r.x+r.width/2, y:r.y+r.height/2}}); }}
                            }}
                            let popup = '[class*="pop"], [class*="Pop"], [class*="modal"], [class*="Modal"], [class*="dialog"], [class*="Dialog"], [class*="layer"], [class*="Layer"], [class*="tip"], [class*="Tip"], [class*="alert"], [class*="Alert"], [class*="confirm"], [class*="Confirm"], [class*="msg"], [class*="Msg"]';
                            for (let el of document.querySelectorAll(popup)) {{
                                let txt = (el.textContent || '').trim();
                                if (txt.includes(target)) {{ let r = el.getBoundingClientRect(); if (r.width > 0 && r.height > 0) return JSON.stringify({{x:r.x+r.width/2, y:r.y+r.height/2}}); }}
                            }}
                            for (let el of document.querySelectorAll('div, span, p, label, h1, h2, h3, h4')) {{
                                let txt = (el.textContent || '').trim();
                                if (txt.includes(target) && txt.length < 200) {{ let r = el.getBoundingClientRect(); if (r.width > 0 && r.height > 0) return JSON.stringify({{x:r.x+r.width/2, y:r.y+r.height/2}}); }}
                            }}
                            return null;
                        }})()""")
                        return json.loads(coords_json) if coords_json else None

                    def _click_button_exact(text, step_label, expect_disappear=True):
                        plog(f"{step_label}: 查找 '{text}'...")
                        if not _page_has_exact_text(text):
                            plog(f"  页面上未找到 '{text}'，可能已跳过此步骤")
                            return True
                        coords = _find_button_coords(text)
                        if not coords:
                            plog(f"  未能获取 '{text}' 的坐标")
                            return False
                        had_popup = _popup_visible()
                        for attempt in range(5):
                            if expect_disappear:
                                if had_popup and not _popup_visible():
                                    plog(f"  弹窗已关闭，'{text}' 点击成功")
                                    break
                                if not had_popup and not _page_has_exact_text(text):
                                    plog(f"  '{text}' 已消失，点击成功")
                                    break
                            try:
                                if attempt == 0:
                                    btn = page.get_by_text(text, exact=False).first
                                    if btn.count() > 0 and btn.is_visible():
                                        btn.click(timeout=5000)
                                    else:
                                        page.mouse.click(coords["x"], coords["y"])
                                elif attempt == 1:
                                    page.mouse.click(coords["x"], coords["y"])
                                elif attempt == 2:
                                    page.evaluate(f"""(function() {{
                                        let el = document.elementFromPoint({coords['x']}, {coords['y']});
                                        if (el) {{ ['mousedown','mouseup','click'].forEach(function(t) {{ el.dispatchEvent(new MouseEvent(t, {{bubbles:true,cancelable:true}})); }}); let p = el.closest('a,button,[onclick]'); if (p && p !== el) p.click(); }}
                                    }})()""")
                                elif attempt == 3:
                                    page.evaluate(f"""(function() {{
                                        let target = '{text}';
                                        for (let el of document.querySelectorAll('[onclick]')) {{
                                            if ((el.textContent||'').trim().includes(target)) {{ try {{ eval(el.getAttribute('onclick')); }} catch(e) {{}} return; }}
                                        }}
                                    }})()""")
                                else:
                                    page.mouse.move(coords["x"], coords["y"])
                                    page.wait_for_timeout(100)
                                    page.mouse.down()
                                    page.wait_for_timeout(80)
                                    page.mouse.up()
                            except Exception as e:
                                plog(f"  第{attempt+1}轮异常: {e}")
                            page.wait_for_timeout(2000)
                            new_coords = _find_button_coords(text)
                            if new_coords:
                                coords = new_coords
                        if expect_disappear:
                            popup_still = had_popup and _popup_visible()
                            text_still = _page_has_exact_text(text)
                            if popup_still or (not had_popup and text_still):
                                plog(f"  重试5轮后 '{text}' 仍存在，此步骤失败")
                                page.screenshot(path=f"seat_map_{step_label}_failed.png")
                                return False
                        plog(f"  '{text}' 点击完成")
                        page.wait_for_timeout(2000)
                        page.screenshot(path=f"seat_map_{step_label}.png")
                        return True

                    # Step 1: 结算
                    plog("=== Step 1: 点击结算 ===")
                    _click_button_exact("结算", "step1_settle", expect_disappear=False)
                    page.wait_for_timeout(3000)
                    page.screenshot(path="seat_map_after_settle.png")

                    # Step 2: 弹窗
                    plog("=== Step 2: 处理弹窗 ===")
                    popup_appeared = False
                    for i in range(10):
                        if _page_has_exact_text("我知道了"):
                            popup_appeared = True
                            break
                        page.wait_for_timeout(500)
                    if popup_appeared:
                        _click_button_exact("我知道了，继续购买", "step2_popup", expect_disappear=True)
                    else:
                        for alt_text in ["我知道了", "知道了", "继续", "确定"]:
                            if _page_has_exact_text(alt_text):
                                _click_button_exact(alt_text, "step2_alt", expect_disappear=True)
                                break
                    page.wait_for_timeout(2000)

                    # Step 3: 提交订单
                    plog("=== Step 3: 提交订单 ===")
                    _click_button_exact("提交订单", "step3_submit", expect_disappear=False)
                    page.wait_for_timeout(3000)

                    # Step 4: 友情提示
                    plog("=== Step 4: 友情提示弹窗 ===")
                    hint_appeared = False
                    for i in range(30):
                        if _page_has_exact_text("我同意") or _page_has_exact_text("友情提示"):
                            hint_appeared = True
                            break
                        page.wait_for_timeout(500)
                    if hint_appeared:
                        page.evaluate("""(function() {
                            for (let el of document.querySelectorAll('input[type="checkbox"]')) {
                                let parent = el.closest('div, label, li, p, span, td');
                                if (parent && (parent.textContent||'').includes('我同意')) { if (!el.checked) el.click(); return true; }
                            }
                            for (let lb of document.querySelectorAll('label')) {
                                if ((lb.textContent||'').includes('我同意')) {
                                    let cb = (lb.getAttribute('for')) ? document.getElementById(lb.getAttribute('for')) : lb.querySelector('input[type="checkbox"]');
                                    if (cb && !cb.checked) { cb.click(); return true; }
                                    lb.click(); return true;
                                }
                            }
                            return false;
                        })""")
                        page.wait_for_timeout(500)
                        _click_button_exact("确定", "step4_confirm", expect_disappear=True)
                    page.wait_for_timeout(3000)

                    # 最终结果
                    page.screenshot(path="seat_map_final.png")
                    final_url = page.url
                    plog(f"最终URL: {final_url}")
                    if any(ind in final_url for ind in ["OrderStep", "orderdetail", "payresult", "PayResult", "Success", "success", "payconfirm", "PayConfirm"]):
                        return {"code": 0, "msg": f"选座下单成功! URL: {final_url}"}
                    final_text = page.evaluate("document.body.innerText.substring(0, 500)")
                    if any(w in final_text for w in ["下单成功", "订单提交成功", "支付成功"]):
                        return {"code": 0, "msg": "页面内容显示下单成功"}
                    return {"code": -1, "msg": f"订单流程未完成。最终URL: {final_url[:100]}"}
                except Exception as e:
                    import traceback
                    return {"code": -1, "msg": f"选座异常: {e}\n{traceback.format_exc()}"}
                finally:
                    page.close()
                    context.close()
                    browser.close()

        return _do_select()


# ─── API 层 ────────────────────────────────────────────────────────────────────

class SHCSTheatreAPI:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update(HEADERS)
        self.token = ""

    def set_cookies(self, cookies: dict):
        for name, value in cookies.items():
            self.session.cookies.set(name, value, domain="m.shcstheatre.com")

    def login(self, username, password, captcha) -> dict:
        url = f"{BASE_URL}/WebAPIWeChat.ashx?op=CustomerLoginWeChat"
        data = {"username": username, "newpassword": password,
                "loginsurecode": captcha, "sessioncode": captcha,
                "cookieOP_ID": "", "OPEND_ID_COOKIE": ""}
        resp = self.session.post(url, data=data)
        result = resp.json()
        if result.get("code") == 0 and result.get("iRtn") == 0:
            self.token = result.get("token", "")
        return result

    def get_program_list(self) -> list[dict]:
        url = f"{BASE_URL}/Program/ProgramListWeChat.aspx?GROUP_ID=351"
        resp = self.session.get(url)
        soup = BeautifulSoup(resp.text, "lxml")
        programs, seen = [], set()
        for a in soup.find_all("a", href=True):
            m = re.search(r"ProgramDetailsWeChat\.aspx\?id=(\d+)", a["href"])
            if m:
                pid = int(m.group(1))
                name = a.get_text(strip=True)
                if pid not in seen and name and "购票" not in name:
                    seen.add(pid)
                    programs.append({"id": pid, "name": name})
        return programs

    def get_events(self, program_id: int) -> tuple[list[dict], dict]:
        url = f"{BASE_URL}/webapi.ashx?op=Gettblprogram"
        resp = self.session.post(url, data={"token": self.token, "id": program_id})
        result = resp.json()
        events, program_info = [], {}
        if result.get("code") == 0 and result.get("data"):
            data = result["data"]
            tbl = data.get("tblprogram")
            if isinstance(tbl, list) and tbl and isinstance(tbl[0], dict):
                p = tbl[0]
                program_info = {"seat_type": int(p.get("I_SEAT_TYPE", 0)),
                                "scs_type": int(p.get("SCS_TYPE", 0)),
                                "special_type": p.get("SPECIAL_TYPE", "")}
            for e in (data.get("TBLEVENT") or []):
                select_seat = int(float(e.get("B_WEB_SELECTSEAT", 0)))
                events.append({
                    "event_id": e["I_EVENT_ID"], "datetime": e["DT_EVENT_DATETIME"],
                    "week": e.get("VC_WEEK", ""), "if_begin": int(e.get("IF_BEGIN", 0)),
                    "if_begin_pg": int(e.get("IF_BEGIN_PG", 0)),
                    "seat_cnt": float(e.get("I_WHGC_WEB_SEAT_CNT", 0)),
                    "rg_begin": int(e.get("RG_BEGIN", 0)),
                    "event_name": e.get("VC_EVENT_NAME", ""), "select_seat": select_seat})
            if not program_info and events:
                program_info = {"seat_type": 1 if any(e["select_seat"] == 1 for e in events) else 2}
        return events, program_info

    def get_price_levels(self, event_id: int) -> list[dict]:
        url = f"{BASE_URL}/webapi.ashx?op=GettblpricelevelList_ns"
        resp = self.session.post(url, data={"I_EVENT_ID": event_id})
        result = resp.json()
        prices = []
        if result.get("code") == 0 and result.get("data"):
            for p in result["data"]:
                prices.append({"price_id": int(p["I_PRICE_ID"]), "price_amt": float(p["I_PRICE_AMT"]),
                               "desc": p.get("VC_PRICEDESC", ""), "remark": p.get("VC_REMARK", ""),
                               "sold_out": int(p.get("SOLD_OUT", 0)), "seat_cnt": int(p.get("I_WHGC_WEB_SEAT_CNT", 0))})
        return prices

    def check_price_availability(self, event_id, price_id, event_if_begin=1) -> dict:
        for p in self.get_price_levels(event_id):
            if p["price_id"] == price_id:
                can_buy = event_if_begin == 1 and p["sold_out"] == 0 and p["seat_cnt"] > 0
                return {"available": can_buy, "seat_cnt": p["seat_cnt"],
                        "sold_out": p["sold_out"] == 1, "if_begin": event_if_begin}
        return {"available": False, "seat_cnt": 0, "sold_out": True, "if_begin": event_if_begin}

    def buy_ticket(self, event_id, price_id, qty=1) -> dict:
        url = f"{BASE_URL}/SK_WebAPI.ashx?op=NoSeatBuy"
        data = {"I_EVENT_ID": event_id, "I_PRICE_ID": price_id, "iQty": qty, "token": self.token}
        resp = self.session.post(url, data=data)
        result = resp.json()
        return result


# ─── 微信推送 ──────────────────────────────────────────────────────────────────

class WeChatNotifier:
    @staticmethod
    def send_serverchan(send_key, title, content):
        try:
            resp = requests.post(f"https://sctapi.ftqq.com/{send_key}.send",
                                 data={"title": title, "desp": content}, timeout=10)
            data = resp.json()
            if data.get("code") == 0:
                return True, "发送成功"
            return False, f"code={data.get('code')}, msg={data.get('message', '')}"
        except Exception as e:
            return False, str(e)

    @staticmethod
    def send_pushplus(token, title, content):
        try:
            resp = requests.post("https://www.pushplus.plus/send",
                                 json={"token": token, "title": title, "content": content}, timeout=10)
            data = resp.json()
            if data.get("code") == 200:
                return True, "发送成功"
            return False, f"code={data.get('code')}, msg={data.get('msg', '')}"
        except Exception as e:
            return False, str(e)

    @staticmethod
    def send_wxpusher(app_token, uids, title, content):
        try:
            resp = requests.post("https://wxpusher.zjiecode.com/api/send/message",
                                 json={"appToken": app_token, "content": content, "summary": title,
                                       "contentType": 1, "uids": uids}, timeout=10)
            data = resp.json()
            if data.get("code") != 1000:
                return False, f"API code={data.get('code')}, msg={data.get('msg', '')}"
            failed = []
            for d in (data.get("data") or []):
                if d.get("code") != 1000:
                    failed.append(f"UID={d.get('uid','?')}: code={d.get('code')} {d.get('status','')}")
            if failed:
                return False, "; ".join(failed)
            return True, "发送成功"
        except Exception as e:
            return False, str(e)


# ─── 访问密码 ──────────────────────────────────────────────────────────────────

ACCESS_PASSWORD = os.environ.get("TICKET_PASSWORD", "changeme")
DEV_USERNAME = os.environ.get("DEV_USERNAME", "admin")
DEV_PASSWORD = os.environ.get("DEV_PASSWORD", "admin123")

# ─── Flask 应用 ────────────────────────────────────────────────────────────────

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET", secrets.token_hex(32))
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(hours=4)


# ─── 用户会话管理 ─────────────────────────────────────────────────────────────

CONFIG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "configs")


def _load_user_config(username: str) -> dict:
    path = os.path.join(CONFIG_DIR, f"{username}.json")
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def _save_user_config(username: str, us: "UserSession"):
    os.makedirs(CONFIG_DIR, exist_ok=True)
    path = os.path.join(CONFIG_DIR, f"{username}.json")
    data = {
        "push_method": us.push_method,
        "push_key": us.push_key,
        "push_uid": us.push_uid,
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


REGISTRY_PATH = os.path.join(CONFIG_DIR, "_registry.json")


def _load_registry() -> dict:
    if os.path.exists(REGISTRY_PATH):
        try:
            with open(REGISTRY_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def _save_registry(registry: dict):
    os.makedirs(CONFIG_DIR, exist_ok=True)
    with open(REGISTRY_PATH, "w", encoding="utf-8") as f:
        json.dump(registry, f, ensure_ascii=False, indent=2)


def _register_user(username: str) -> bool:
    """注册新用户，返回 True 表示成功，False 表示已存在"""
    registry = _load_registry()
    if username in registry:
        return False
    registry[username] = {
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "last_login": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "login_count": 1,
    }
    _save_registry(registry)
    _save_user_config(username, UserSession(username=username))
    return True


def _update_registry_login(username: str):
    """更新用户登录记录"""
    registry = _load_registry()
    if username in registry:
        registry[username]["last_login"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        registry[username]["login_count"] = registry[username].get("login_count", 0) + 1
        _save_registry(registry)


class UserSession:
    """每个登录用户独立的状态"""
    def __init__(self, username: str = ""):
        self.username = username
        self.api = SHCSTheatreAPI()
        self.captcha_cookies = {}
        self.monitoring = False
        self.monitor_thread = None
        self.programs = []
        self.events = []
        self.prices = []
        self.sel_program_id = None
        self.sel_program_name = ""
        self.sel_event_id = None
        self.sel_event_dt = ""
        self.sel_price_id = None
        self.sel_price_info = None
        self.push_method = "wxpusher"
        self.push_key = ""
        self.push_uid = ""
        self.created_at = time.time()
        self.last_active = time.time()
        self.log_buf = collections.deque(maxlen=200)
        self.log_subscribers: list[queue.Queue] = []
        self._log_lock = threading.Lock()


class UserManager:
    """管理所有用户会话，线程安全"""
    def __init__(self, ttl_seconds=3600):
        self._sessions: dict[str, UserSession] = {}
        self._lock = threading.Lock()
        self._ttl = ttl_seconds

    def get_or_create(self, username: str) -> UserSession:
        with self._lock:
            if username in self._sessions:
                self._sessions[username].last_active = time.time()
                return self._sessions[username]
            sess = UserSession(username=username)
            cfg = _load_user_config(username)
            if cfg:
                sess.push_method = cfg.get("push_method", "wxpusher")
                sess.push_key = cfg.get("push_key", "")
                sess.push_uid = cfg.get("push_uid", "")
            self._sessions[username] = sess
            return sess

    def get(self, user_id: str):
        with self._lock:
            sess = self._sessions.get(user_id)
            if sess:
                sess.last_active = time.time()
            return sess

    def remove(self, user_id: str):
        with self._lock:
            sess = self._sessions.pop(user_id, None)
            if sess and sess.monitoring:
                sess.monitoring = False

    def cleanup_stale(self):
        now = time.time()
        with self._lock:
            expired = [uid for uid, s in self._sessions.items()
                       if now - s.last_active > self._ttl]
            for uid in expired:
                sess = self._sessions.pop(uid)
                if sess.monitoring:
                    sess.monitoring = False

    def get_all_sessions(self) -> list[dict]:
        with self._lock:
            result = []
            for username, sess in self._sessions.items():
                info = {
                    "username": username,
                    "online": True,
                    "monitoring": sess.monitoring,
                    "sel_program_name": sess.sel_program_name,
                    "sel_event_dt": sess.sel_event_dt,
                    "sel_price_info": sess.sel_price_info,
                    "last_active": datetime.fromtimestamp(sess.last_active).strftime("%Y-%m-%d %H:%M:%S"),
                }
                result.append(info)
            return result


# ─── 共享单例 ─────────────────────────────────────────────────────────────────

ocr_engine = ddddocr.DdddOcr(show_ad=False)
browser_manager = BrowserManager()
user_manager = UserManager(ttl_seconds=3600)


def _cleanup_loop():
    while True:
        time.sleep(300)
        user_manager.cleanup_stale()

threading.Thread(target=_cleanup_loop, daemon=True).start()


# ─── 日志系统（SSE 广播 + 文件按需刷写）────────────────────────────────────────

_log_file = "ticket_monitor.log"


def _flush_log(reason: str):
    with open(_log_file, "a", encoding="utf-8") as f:
        f.write(f"\n{'='*60}\n[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {reason}\n{'='*60}\n")


def user_log(us: UserSession, msg: str, flush: bool = False):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    us.log_buf.append(line)
    with us._log_lock:
        dead = []
        for q in us.log_subscribers:
            try:
                q.put_nowait(line)
            except queue.Full:
                dead.append(q)
        for q in dead:
            us.log_subscribers.remove(q)
    if flush:
        _flush_log(msg)


def get_user_session() -> UserSession:
    username = session.get("username", "")
    return user_manager.get_or_create(username)


# ─── CORS ─────────────────────────────────────────────────────────────────────

ALLOWED_ORIGINS = {"http://49.235.110.106:5000", "https://ticket-60.site",
                   "http://ticket-60.site", "https://liushihe60.github.io"}


@app.after_request
def add_cors_headers(response):
    origin = request.headers.get("Origin")
    if origin in ALLOWED_ORIGINS:
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Access-Control-Allow-Headers"] = "Content-Type"
        response.headers["Access-Control-Allow-Methods"] = "GET,POST,OPTIONS"
        response.headers["Access-Control-Allow-Credentials"] = "true"
    return response


# ─── 认证中间件 ───────────────────────────────────────────────────────────────

PUBLIC_ROUTES = {"/", "/admin", "/api/auth/login", "/api/auth/register",
                 "/api/auth/check", "/api/admin/login"}


@app.before_request
def check_auth():
    if request.path in PUBLIC_ROUTES or request.path.startswith("/static"):
        return None
    # 管理员路由需要管理员登录
    if request.path.startswith("/api/admin/") or request.path == "/admin":
        if not session.get("is_admin"):
            return jsonify({"ok": False, "msg": "未登录", "code": 401}), 401
        return None
    # 普通路由需要用户登录
    if not session.get("authenticated"):
        return jsonify({"ok": False, "msg": "未登录", "code": 401}), 401
    return None


# ─── 认证路由 ─────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    if session.get("authenticated"):
        return redirect("/app")
    return render_template("login.html")


@app.route("/app")
def app_page():
    if not session.get("authenticated"):
        return redirect("/")
    return render_template("index.html", username=session.get("username", ""))


@app.route("/api/auth/login", methods=["POST"])
def auth_login():
    d = request.json
    if not d:
        return jsonify({"ok": False, "msg": "请求格式错误"})
    username = d.get("username", "").strip()
    if not username:
        return jsonify({"ok": False, "msg": "请输入用户名"})
    if len(username) > 32:
        return jsonify({"ok": False, "msg": "用户名最长32个字符"})
    if not re.match(r'^[a-zA-Z0-9_一-鿿]+$', username):
        return jsonify({"ok": False, "msg": "用户名只能包含字母、数字、下划线和中文"})
    if d.get("password") != ACCESS_PASSWORD:
        return jsonify({"ok": False, "msg": "密码错误"})
    registry = _load_registry()
    if username not in registry:
        return jsonify({"ok": False, "msg": "用户未注册，请先注册"})
    _update_registry_login(username)
    session["authenticated"] = True
    session["username"] = username
    session.permanent = True
    user_manager.get_or_create(username)
    return jsonify({"ok": True})


@app.route("/api/auth/register", methods=["POST"])
def auth_register():
    d = request.json
    if not d:
        return jsonify({"ok": False, "msg": "请求格式错误"})
    username = d.get("username", "").strip()
    if not username:
        return jsonify({"ok": False, "msg": "请输入用户名"})
    if len(username) > 32:
        return jsonify({"ok": False, "msg": "用户名最长32个字符"})
    if not re.match(r'^[a-zA-Z0-9_一-鿿]+$', username):
        return jsonify({"ok": False, "msg": "用户名只能包含字母、数字、下划线和中文"})
    if d.get("password") != ACCESS_PASSWORD:
        return jsonify({"ok": False, "msg": "密码错误"})
    if not _register_user(username):
        return jsonify({"ok": False, "msg": "用户名已存在，请直接登录"})
    session["authenticated"] = True
    session["username"] = username
    session.permanent = True
    user_manager.get_or_create(username)
    return jsonify({"ok": True})


@app.route("/api/auth/check")
def auth_check():
    return jsonify({"authenticated": session.get("authenticated", False),
                    "username": session.get("username", "")})


@app.route("/api/auth/logout", methods=["POST"])
def auth_logout():
    username = session.get("username", "")
    if username:
        user_manager.remove(username)
    session.clear()
    return jsonify({"ok": True})


# ─── 管理员路由 ───────────────────────────────────────────────────────────────

@app.route("/admin")
def admin_page():
    if not session.get("is_admin"):
        return render_template("login.html", is_admin=True)
    return render_template("admin.html")


@app.route("/api/admin/login", methods=["POST"])
def admin_login():
    d = request.json
    if not d:
        return jsonify({"ok": False, "msg": "请求格式错误"})
    if d.get("username") != DEV_USERNAME or d.get("password") != DEV_PASSWORD:
        return jsonify({"ok": False, "msg": "开发者账号或密码错误"})
    session["is_admin"] = True
    session.permanent = True
    return jsonify({"ok": True})


@app.route("/api/admin/users")
def admin_users():
    registry = _load_registry()
    active_sessions = user_manager.get_all_sessions()
    active_usernames = {s["username"] for s in active_sessions}
    users = []
    for username, meta in registry.items():
        users.append({
            "username": username,
            "created_at": meta.get("created_at", ""),
            "last_login": meta.get("last_login", ""),
            "login_count": meta.get("login_count", 0),
            "online": username in active_usernames,
        })
    users.sort(key=lambda u: u["last_login"], reverse=True)
    return jsonify({"ok": True, "data": users})


@app.route("/api/admin/sessions")
def admin_sessions():
    sessions = user_manager.get_all_sessions()
    return jsonify({"ok": True, "data": sessions})


@app.route("/api/admin/kick", methods=["POST"])
def admin_kick():
    d = request.json
    username = d.get("username", "")
    if not username:
        return jsonify({"ok": False, "msg": "未指定用户"})
    user_manager.remove(username)
    return jsonify({"ok": True, "msg": f"已踢出用户 {username}"})


@app.route("/api/admin/stop", methods=["POST"])
def admin_stop():
    d = request.json
    username = d.get("username", "")
    if not username:
        return jsonify({"ok": False, "msg": "未指定用户"})
    us = user_manager.get(username)
    if not us:
        return jsonify({"ok": False, "msg": "用户不在线"})
    us.monitoring = False
    return jsonify({"ok": True, "msg": f"已停止 {username} 的监测"})


# ─── 业务路由 ─────────────────────────────────────────────────────────────────

@app.route("/api/captcha", methods=["POST"])
def api_captcha():
    """获取验证码图片（启动浏览器 + 截图 + OCR）"""
    us = get_user_session()
    try:
        img_bytes, cookies = browser_manager.fetch_captcha()
        us.captcha_cookies = cookies
        us.api.set_cookies(cookies)
        b64 = base64.b64encode(img_bytes).decode()
        ocr_text = ocr_engine.classification(img_bytes)
        user_log(us, f"验证码获取成功，OCR识别: {ocr_text}")
        return jsonify({"ok": True, "image": b64, "ocr": ocr_text})
    except Exception as e:
        user_log(us, f"获取验证码失败: {e}")
        return jsonify({"ok": False, "msg": str(e)})


@app.route("/api/login", methods=["POST"])
def api_login():
    us = get_user_session()
    d = request.json
    phone, pwd, captcha = d.get("phone", ""), d.get("password", ""), d.get("captcha", "")
    if not phone or not pwd or not captcha:
        return jsonify({"ok": False, "msg": "请填写完整登录信息"})
    try:
        result = us.api.login(phone, pwd, captcha)
        if result.get("code") == 0 and result.get("iRtn") == 0:
            user_log(us, f"登录成功 token={us.api.token[:12]}...")
            return jsonify({"ok": True})
        msg = result.get("msg", "登录失败")
        user_log(us, f"登录失败: {msg}")
        return jsonify({"ok": False, "msg": msg})
    except Exception as e:
        user_log(us, f"登录异常: {e}")
        return jsonify({"ok": False, "msg": str(e)})


@app.route("/api/programs")
def api_programs():
    us = get_user_session()
    try:
        us.programs = us.api.get_program_list()
        user_log(us, f"加载到 {len(us.programs)} 个剧目")
        return jsonify({"ok": True, "data": us.programs})
    except Exception as e:
        user_log(us, f"加载剧目失败: {e}")
        return jsonify({"ok": False, "msg": str(e)})


@app.route("/api/events/<int:pid>")
def api_events(pid):
    us = get_user_session()
    try:
        events, prog_info = us.api.get_events(pid)
        us.events = events
        us.sel_program_id = pid
        for p in us.programs:
            if p["id"] == pid:
                us.sel_program_name = p["name"]
                break
        user_log(us, f"加载到 {len(events)} 个场次")
        return jsonify({"ok": True, "data": events, "program_info": prog_info})
    except Exception as e:
        user_log(us, f"加载场次失败: {e}")
        return jsonify({"ok": False, "msg": str(e)})


@app.route("/api/prices/<int:eid>")
def api_prices(eid):
    us = get_user_session()
    try:
        prices = us.api.get_price_levels(eid)
        us.prices = prices
        us.sel_event_id = eid
        for e in us.events:
            if e["event_id"] == eid:
                us.sel_event_dt = e["datetime"][:16]
                break
        user_log(us, f"加载到 {len(prices)} 个票档")
        return jsonify({"ok": True, "data": prices})
    except Exception as e:
        user_log(us, f"加载票档失败: {e}")
        return jsonify({"ok": False, "msg": str(e)})


@app.route("/api/select_price", methods=["POST"])
def api_select_price():
    us = get_user_session()
    d = request.json
    price_id = d.get("price_id")
    for p in us.prices:
        if p["price_id"] == price_id:
            us.sel_price_id = price_id
            us.sel_price_info = p
            user_log(us, f"已选择票档: ¥{p['price_amt']:.0f} {p['desc']}")
            return jsonify({"ok": True})
    return jsonify({"ok": False, "msg": "未找到该票档"})


@app.route("/api/monitor/start", methods=["POST"])
def api_monitor_start():
    us = get_user_session()
    if us.monitoring:
        return jsonify({"ok": False, "msg": "监测已在运行"})
    if not us.sel_event_id or not us.sel_price_id:
        return jsonify({"ok": False, "msg": "请先选择场次和票档"})
    if not us.api.token:
        return jsonify({"ok": False, "msg": "请先登录"})

    d = request.json or {}
    interval = d.get("interval", 3)
    qty = d.get("qty", 1)

    event_if_begin = 1
    is_select_seat = False
    for ev in us.events:
        if ev["event_id"] == us.sel_event_id:
            event_if_begin = ev["if_begin"]
            is_select_seat = ev.get("select_seat") == 1
            break

    us.monitoring = True
    us.monitor_thread = threading.Thread(
        target=_monitor_loop,
        args=(us, us.sel_event_id, us.sel_price_id, qty, interval,
              event_if_begin, is_select_seat), daemon=True)
    us.monitor_thread.start()

    seat_str = "选座" if is_select_seat else "无座"
    user_log(us, f"开始监测 | 场次:{us.sel_event_id} 票档:¥{us.sel_price_info['price_amt']:.0f} "
        f"间隔:{interval}s 数量:{qty} 类型:{seat_str}")
    return jsonify({"ok": True})


@app.route("/api/monitor/stop", methods=["POST"])
def api_monitor_stop():
    us = get_user_session()
    us.monitoring = False
    user_log(us, "监测已停止")
    return jsonify({"ok": True})


@app.route("/api/monitor/status")
def api_monitor_status():
    us = get_user_session()
    return jsonify({"monitoring": us.monitoring})


@app.route("/api/push/config", methods=["POST"])
def api_push_config():
    us = get_user_session()
    d = request.json
    us.push_method = d.get("method", "wxpusher")
    us.push_key = d.get("key", "")
    us.push_uid = d.get("uid", "")
    if us.username:
        _save_user_config(us.username, us)
    user_log(us, f"推送配置已更新: method={us.push_method}")
    return jsonify({"ok": True})


@app.route("/api/push/test", methods=["POST"])
def api_push_test():
    us = get_user_session()
    method, key, uid = us.push_method, us.push_key, us.push_uid
    if not key:
        return jsonify({"ok": False, "msg": "未配置推送Key"})
    try:
        if method == "serverchan":
            ok, detail = WeChatNotifier.send_serverchan(key, "测试推送", "ticket_monitor 测试消息")
        elif method == "pushplus":
            ok, detail = WeChatNotifier.send_pushplus(key, "测试推送", "ticket_monitor 测试消息")
        elif method == "wxpusher":
            if not uid:
                return jsonify({"ok": False, "msg": "未配置WxPusher UID"})
            ok, detail = WeChatNotifier.send_wxpusher(key, [uid], "测试推送", "ticket_monitor 测试消息")
        else:
            return jsonify({"ok": False, "msg": f"未知推送方式: {method}"})
        return jsonify({"ok": ok, "msg": detail})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)})


@app.route("/api/logs/stream")
def api_logs_stream():
    """SSE 实时日志流"""
    us = get_user_session()
    q = queue.Queue(maxsize=500)
    with us._log_lock:
        us.log_subscribers.append(q)

    def generate():
        for line in list(us.log_buf):
            yield f"data: {json.dumps({'msg': line}, ensure_ascii=False)}\n\n"
        try:
            while True:
                try:
                    msg = q.get(timeout=30)
                    yield f"data: {json.dumps({'msg': msg}, ensure_ascii=False)}\n\n"
                except queue.Empty:
                    yield ": keepalive\n\n"
        except GeneratorExit:
            pass
        finally:
            with us._log_lock:
                if q in us.log_subscribers:
                    us.log_subscribers.remove(q)

    return Response(generate(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ─── 监控循环 ──────────────────────────────────────────────────────────────────

def _send_notification(us: UserSession, event_id, price_id, qty, success):
    method, key, uid = us.push_method, us.push_key, us.push_uid
    if not key:
        user_log(us, "未配置推送Key，跳过微信通知")
        return
    title = "购票成功通知"
    price_str = f"¥{us.sel_price_info['price_amt']:.0f}" if us.sel_price_info else f"ID:{price_id}"
    content = (f"剧目: {us.sel_program_name or '上海文化广场'}\n"
               f"场次: {us.sel_event_dt or event_id}\n"
               f"票档: {price_str}\n数量: {qty}\n"
               f"状态: {'下单成功' if success else '发现余票'}\n"
               f"时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n请尽快前往购物车完成支付！")
    try:
        if method == "serverchan":
            ok, detail = WeChatNotifier.send_serverchan(key, title, content)
        elif method == "pushplus":
            ok, detail = WeChatNotifier.send_pushplus(key, title, content)
        elif method == "wxpusher":
            ok, detail = WeChatNotifier.send_wxpusher(key, [uid], title, content) if uid else (False, "未配置UID")
        else:
            ok, detail = False, "未知推送方式"
        user_log(us, f"推送{'成功' if ok else '失败'}: {detail}")
    except Exception as e:
        user_log(us, f"推送异常: {e}")


def _monitor_loop(us: UserSession, event_id, price_id, qty, interval, event_if_begin, is_select_seat):
    count = 0
    refresh_counter = 0
    err_streak = 0
    start_time = time.time()
    while us.monitoring:
        count += 1
        refresh_counter += 1
        try:
            if refresh_counter >= 10 and us.sel_program_id:
                refresh_counter = 0
                try:
                    events, _ = us.api.get_events(us.sel_program_id)
                    for ev in events:
                        if ev["event_id"] == event_id:
                            if ev["if_begin"] != event_if_begin:
                                user_log(us, f"场次状态变更: IF_BEGIN {event_if_begin} -> {ev['if_begin']}")
                                event_if_begin = ev["if_begin"]
                            break
                except Exception as e:
                    user_log(us, f"刷新场次状态失败: {e}", flush=True)

            if count % 100 == 0:
                elapsed = time.time() - start_time
                user_log(us, f"--- 运行摘要 | 已查询{count}次 | 运行{elapsed/60:.1f}分钟 | 间隔{interval}s | 连续错误{err_streak}次 ---")

            info = us.api.check_price_availability(event_id, price_id, event_if_begin)
            err_streak = 0
            ts = datetime.now().strftime("%H:%M:%S")

            if info["available"]:
                user_log(us, f"[{ts}] #{count} 发现有票（余{info['seat_cnt']}张）！尝试下单...")
                buy_result = us.api.buy_ticket(event_id, price_id, qty)
                if buy_result.get("code") == 0:
                    user_log(us, f"[{ts}] 下单成功! 请前往购物车完成支付", flush=True)
                    _send_notification(us, event_id, price_id, qty, True)
                    us.monitoring = False
                    return
                msg = buy_result.get("msg", "未知错误")
                code = buy_result.get("code", "?")
                user_log(us, f"[{ts}] #{count} 下单失败 code={code} - {msg}")

                if code == 3567 and is_select_seat:
                    user_log(us, f"[{ts}] 该场次为选座事件，尝试 Playwright 自动选座...")
                    try:
                        seat_result = BrowserManager.select_seat_and_buy(
                            us.sel_program_id, event_id, price_id,
                            us.captcha_cookies, us.api.token, qty,
                            log_callback=lambda msg: user_log(us, msg))
                        if seat_result.get("code") == 0:
                            user_log(us, f"[{ts}] 选座下单成功!", flush=True)
                            _send_notification(us, event_id, price_id, qty, True)
                            us.monitoring = False
                            return
                        else:
                            user_log(us, f"[{ts}] 选座失败: {seat_result.get('msg')}")
                    except Exception as se:
                        user_log(us, f"[{ts}] 选座异常: {se}")

                if code == 10001:
                    user_log(us, "登录已过期，请重新登录后再监测", flush=True)
                    us.monitoring = False
                    return
            else:
                ib = info.get("if_begin", 0)
                if ib != 1:
                    status_map = {0: "未开票", 2: "已结束"}
                    user_log(us, f"[{ts}] #{count} {status_map.get(ib, '未开票/已暂停')}（IF_BEGIN={ib}）")
                else:
                    user_log(us, f"[{ts}] #{count} 暂无余票（余{info['seat_cnt']}张）")

        except Exception as e:
            err_streak += 1
            user_log(us, f"[{datetime.now().strftime('%H:%M:%S')}] #{count} 查询异常 (连续第{err_streak}次) - {e}")
            if err_streak >= 10:
                user_log(us, f"警告：连续{err_streak}次查询异常，可能存在网络问题或IP被限流", flush=True)

        time.sleep(interval)


# ─── 启动 ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Web 服务启动，访问密码: {ACCESS_PASSWORD}")
    app.run(host="0.0.0.0", port=5000, debug=False, threaded=True)
