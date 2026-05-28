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
import base64
import queue
import threading
import collections
from datetime import datetime

import requests
from bs4 import BeautifulSoup
from PIL import Image
from playwright.sync_api import sync_playwright
import ddddocr
from flask import Flask, render_template, request, jsonify, Response

# ─── 常量 ─────────────────────────────────────────────────────────────────────

BASE_URL = "https://m.shcstheatre.com"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/120.0.0.0 Safari/537.36",
    "Referer": f"{BASE_URL}/Program/ProgramListWeChat.aspx?GROUP_ID=351",
}

# ─── 日志系统（SSE 广播 + 文件按需刷写）────────────────────────────────────────

_log_buf = collections.deque(maxlen=200)
_log_file = "ticket_monitor.log"
_log_subscribers: list[queue.Queue] = []
_sub_lock = threading.Lock()


def _broadcast(msg: str):
    """向所有 SSE 订阅者广播日志"""
    with _sub_lock:
        dead = []
        for q in _log_subscribers:
            try:
                q.put_nowait(msg)
            except queue.Full:
                dead.append(q)
        for q in dead:
            _log_subscribers.remove(q)


def log(msg: str, flush: bool = False):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    _log_buf.append(line)
    _broadcast(line)
    if flush:
        _flush_log(msg)


def _flush_log(reason: str):
    with open(_log_file, "a", encoding="utf-8") as f:
        f.write(f"\n{'='*60}\n[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {reason}\n{'='*60}\n")
        while _log_buf:
            f.write(_log_buf.popleft() + "\n")


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
        _log_buf.append(f"[API] get_price_levels HTTP {resp.status_code} len={len(resp.text)}")
        result = resp.json()
        if result.get("code") != 0:
            _log_buf.append(f"[API] get_price_levels 异常: {json.dumps(result, ensure_ascii=False)[:300]}")
            _flush_log("get_price_levels 异常响应")
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
        _log_buf.append(f"[API] buy_ticket HTTP {resp.status_code} event={event_id} price={price_id}")
        result = resp.json()
        _log_buf.append(f"[API] buy_ticket 响应: {json.dumps(result, ensure_ascii=False)[:500]}")
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


# ─── Flask 应用 ────────────────────────────────────────────────────────────────

app = Flask(__name__)

# CORS 支持（允许 GitHub Pages 前端跨域调用）
@app.after_request
def add_cors_headers(response):
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type"
    response.headers["Access-Control-Allow-Methods"] = "GET,POST,OPTIONS"
    return response

# 全局状态
_state = {
    "browser": BrowserManager(),
    "api": SHCSTheatreAPI(),
    "ocr": ddddocr.DdddOcr(show_ad=False),
    "captcha_cookies": {},
    "monitoring": False,
    "monitor_thread": None,
    "programs": [],
    "events": [],
    "prices": [],
    # 用户选择
    "sel_program_id": None,
    "sel_program_name": "",
    "sel_event_id": None,
    "sel_event_dt": "",
    "sel_price_id": None,
    "sel_price_info": None,
    # 推送配置
    "push_method": "wxpusher",
    "push_key": "",
    "push_uid": "",
}


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/captcha", methods=["POST"])
def api_captcha():
    """获取验证码图片（启动浏览器 + 截图 + OCR）"""
    try:
        img_bytes, cookies = _state["browser"].fetch_captcha()
        _state["captcha_cookies"] = cookies
        _state["api"].set_cookies(cookies)
        b64 = base64.b64encode(img_bytes).decode()
        ocr_text = _state["ocr"].classification(img_bytes)
        log(f"验证码获取成功，OCR识别: {ocr_text}")
        return jsonify({"ok": True, "image": b64, "ocr": ocr_text})
    except Exception as e:
        log(f"获取验证码失败: {e}")
        return jsonify({"ok": False, "msg": str(e)})


@app.route("/api/login", methods=["POST"])
def api_login():
    d = request.json
    phone, pwd, captcha = d.get("phone", ""), d.get("password", ""), d.get("captcha", "")
    if not phone or not pwd or not captcha:
        return jsonify({"ok": False, "msg": "请填写完整登录信息"})
    try:
        result = _state["api"].login(phone, pwd, captcha)
        if result.get("code") == 0 and result.get("iRtn") == 0:
            log(f"登录成功 token={_state['api'].token[:12]}...")
            return jsonify({"ok": True})
        msg = result.get("msg", "登录失败")
        log(f"登录失败: {msg}")
        return jsonify({"ok": False, "msg": msg})
    except Exception as e:
        log(f"登录异常: {e}")
        return jsonify({"ok": False, "msg": str(e)})


@app.route("/api/programs")
def api_programs():
    try:
        _state["programs"] = _state["api"].get_program_list()
        log(f"加载到 {len(_state['programs'])} 个剧目")
        return jsonify({"ok": True, "data": _state["programs"]})
    except Exception as e:
        log(f"加载剧目失败: {e}")
        return jsonify({"ok": False, "msg": str(e)})


@app.route("/api/events/<int:pid>")
def api_events(pid):
    try:
        events, prog_info = _state["api"].get_events(pid)
        _state["events"] = events
        _state["sel_program_id"] = pid
        for p in _state["programs"]:
            if p["id"] == pid:
                _state["sel_program_name"] = p["name"]
                break
        log(f"加载到 {len(events)} 个场次")
        return jsonify({"ok": True, "data": events, "program_info": prog_info})
    except Exception as e:
        log(f"加载场次失败: {e}")
        return jsonify({"ok": False, "msg": str(e)})


@app.route("/api/prices/<int:eid>")
def api_prices(eid):
    try:
        prices = _state["api"].get_price_levels(eid)
        _state["prices"] = prices
        _state["sel_event_id"] = eid
        for e in _state["events"]:
            if e["event_id"] == eid:
                _state["sel_event_dt"] = e["datetime"][:16]
                break
        log(f"加载到 {len(prices)} 个票档")
        return jsonify({"ok": True, "data": prices})
    except Exception as e:
        log(f"加载票档失败: {e}")
        return jsonify({"ok": False, "msg": str(e)})


@app.route("/api/select_price", methods=["POST"])
def api_select_price():
    d = request.json
    price_id = d.get("price_id")
    for p in _state["prices"]:
        if p["price_id"] == price_id:
            _state["sel_price_id"] = price_id
            _state["sel_price_info"] = p
            log(f"已选择票档: ¥{p['price_amt']:.0f} {p['desc']}")
            return jsonify({"ok": True})
    return jsonify({"ok": False, "msg": "未找到该票档"})


@app.route("/api/monitor/start", methods=["POST"])
def api_monitor_start():
    if _state["monitoring"]:
        return jsonify({"ok": False, "msg": "监测已在运行"})
    if not _state["sel_event_id"] or not _state["sel_price_id"]:
        return jsonify({"ok": False, "msg": "请先选择场次和票档"})
    if not _state["api"].token:
        return jsonify({"ok": False, "msg": "请先登录"})

    d = request.json or {}
    interval = d.get("interval", 3)
    qty = d.get("qty", 1)

    event_if_begin = 1
    is_select_seat = False
    for ev in _state["events"]:
        if ev["event_id"] == _state["sel_event_id"]:
            event_if_begin = ev["if_begin"]
            is_select_seat = ev.get("select_seat") == 1
            break

    _state["monitoring"] = True
    _state["monitor_thread"] = threading.Thread(
        target=_monitor_loop,
        args=(_state["sel_event_id"], _state["sel_price_id"], qty, interval,
              event_if_begin, is_select_seat), daemon=True)
    _state["monitor_thread"].start()

    seat_str = "选座" if is_select_seat else "无座"
    log(f"开始监测 | 场次:{_state['sel_event_id']} 票档:¥{_state['sel_price_info']['price_amt']:.0f} "
        f"间隔:{interval}s 数量:{qty} 类型:{seat_str}")
    return jsonify({"ok": True})


@app.route("/api/monitor/stop", methods=["POST"])
def api_monitor_stop():
    _state["monitoring"] = False
    log("监测已停止")
    return jsonify({"ok": True})


@app.route("/api/monitor/status")
def api_monitor_status():
    return jsonify({"monitoring": _state["monitoring"]})


@app.route("/api/push/config", methods=["POST"])
def api_push_config():
    d = request.json
    _state["push_method"] = d.get("method", "wxpusher")
    _state["push_key"] = d.get("key", "")
    _state["push_uid"] = d.get("uid", "")
    log(f"推送配置已更新: method={_state['push_method']}")
    return jsonify({"ok": True})


@app.route("/api/push/test", methods=["POST"])
def api_push_test():
    method, key, uid = _state["push_method"], _state["push_key"], _state["push_uid"]
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
    q = queue.Queue(maxsize=500)
    with _sub_lock:
        _log_subscribers.append(q)

    def generate():
        # 先发送最近的历史日志
        for line in list(_log_buf):
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
            with _sub_lock:
                if q in _log_subscribers:
                    _log_subscribers.remove(q)

    return Response(generate(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ─── 监控循环 ──────────────────────────────────────────────────────────────────

def _send_notification(event_id, price_id, qty, success):
    method, key, uid = _state["push_method"], _state["push_key"], _state["push_uid"]
    if not key:
        log("未配置推送Key，跳过微信通知")
        return
    title = "购票成功通知"
    price_str = f"¥{_state['sel_price_info']['price_amt']:.0f}" if _state["sel_price_info"] else f"ID:{price_id}"
    content = (f"剧目: {_state['sel_program_name'] or '上海文化广场'}\n"
               f"场次: {_state['sel_event_dt'] or event_id}\n"
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
        log(f"推送{'成功' if ok else '失败'}: {detail}")
    except Exception as e:
        log(f"推送异常: {e}")


def _monitor_loop(event_id, price_id, qty, interval, event_if_begin, is_select_seat):
    count = 0
    refresh_counter = 0
    err_streak = 0
    start_time = time.time()
    while _state["monitoring"]:
        count += 1
        refresh_counter += 1
        try:
            if refresh_counter >= 10 and _state["sel_program_id"]:
                refresh_counter = 0
                try:
                    events, _ = _state["api"].get_events(_state["sel_program_id"])
                    for ev in events:
                        if ev["event_id"] == event_id:
                            if ev["if_begin"] != event_if_begin:
                                log(f"场次状态变更: IF_BEGIN {event_if_begin} -> {ev['if_begin']}")
                                event_if_begin = ev["if_begin"]
                            break
                except Exception as e:
                    log(f"刷新场次状态失败: {e}", flush=True)

            if count % 100 == 0:
                elapsed = time.time() - start_time
                log(f"--- 运行摘要 | 已查询{count}次 | 运行{elapsed/60:.1f}分钟 | 间隔{interval}s | 连续错误{err_streak}次 ---")

            info = _state["api"].check_price_availability(event_id, price_id, event_if_begin)
            err_streak = 0
            ts = datetime.now().strftime("%H:%M:%S")

            if info["available"]:
                log(f"[{ts}] #{count} 发现有票（余{info['seat_cnt']}张）！尝试下单...")
                buy_result = _state["api"].buy_ticket(event_id, price_id, qty)
                if buy_result.get("code") == 0:
                    log(f"[{ts}] 下单成功! 请前往购物车完成支付", flush=True)
                    _send_notification(event_id, price_id, qty, True)
                    _state["monitoring"] = False
                    return
                msg = buy_result.get("msg", "未知错误")
                code = buy_result.get("code", "?")
                log(f"[{ts}] #{count} 下单失败 code={code} - {msg}")

                if code == 3567 and is_select_seat:
                    log(f"[{ts}] 该场次为选座事件，尝试 Playwright 自动选座...")
                    try:
                        seat_result = BrowserManager.select_seat_and_buy(
                            _state["sel_program_id"], event_id, price_id,
                            _state["captcha_cookies"], _state["api"].token, qty,
                            log_callback=log)
                        if seat_result.get("code") == 0:
                            log(f"[{ts}] 选座下单成功!", flush=True)
                            _send_notification(event_id, price_id, qty, True)
                            _state["monitoring"] = False
                            return
                        else:
                            log(f"[{ts}] 选座失败: {seat_result.get('msg')}")
                    except Exception as se:
                        log(f"[{ts}] 选座异常: {se}")

                if code == 10001:
                    log("登录已过期，请重新登录后再监测", flush=True)
                    _state["monitoring"] = False
                    return
            else:
                ib = info.get("if_begin", 0)
                if ib != 1:
                    status_map = {0: "未开票", 2: "已结束"}
                    log(f"[{ts}] #{count} {status_map.get(ib, '未开票/已暂停')}（IF_BEGIN={ib}）")
                else:
                    log(f"[{ts}] #{count} 暂无余票（余{info['seat_cnt']}张）")

        except Exception as e:
            err_streak += 1
            log(f"[{datetime.now().strftime('%H:%M:%S')}] #{count} 查询异常 (连续第{err_streak}次) - {e}")
            if err_streak >= 10:
                log(f"警告：连续{err_streak}次查询异常，可能存在网络问题或IP被限流", flush=True)

        time.sleep(interval)


# ─── 启动 ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    log("Web 服务启动")
    app.run(host="0.0.0.0", port=5000, debug=False, threaded=True)
