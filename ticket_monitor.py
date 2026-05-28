#!/usr/bin/env python3
"""
上海文化广场 余票监测 & 自动下单程序
=====================================
功能：
1. 手机号+密码登录（Playwright 加载验证码 + ddddocr 自动识别）
2. 自动抓取可购票剧目
3. 选择剧目 → 选择场次 → 选择票档
4. 定时监测余票，发现有票自动下单（系统自动分配座位）
5. 下单成功后通过微信推送通知（Server酱 / PushPlus / WxPusher）
"""

import re
import io
import json
import time
import threading
import collections
import tkinter as tk
from tkinter import ttk, messagebox, scrolledtext
from datetime import datetime

import requests
from bs4 import BeautifulSoup
from PIL import Image, ImageTk
from playwright.sync_api import sync_playwright
import ddddocr

BASE_URL = "https://m.shcstheatre.com"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/120.0.0.0 Safari/537.36",
    "Referer": f"{BASE_URL}/Program/ProgramListWeChat.aspx?GROUP_ID=351",
}

# 环形缓冲区 + 按需刷写 — 只在异常/成功时写文件，避免日志膨胀
_log_buf = collections.deque(maxlen=200)
_log_file = "ticket_monitor.log"


def _flush_log(reason: str):
    """将缓冲区内容刷写到文件"""
    with open(_log_file, "a", encoding="utf-8") as f:
        f.write(f"\n{'='*60}\n[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {reason}\n{'='*60}\n")
        while _log_buf:
            f.write(_log_buf.popleft() + "\n")


# ─── 浏览器层（Playwright）───────────────────────────────────────────────────

class BrowserManager:
    """用 Playwright 管理浏览器会话"""

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

    @staticmethod
    def select_seat_and_buy(program_id: int, event_id: int, price_id: int,
                            cookies: dict, token: str, qty: int = 1,
                            log_callback=None) -> dict:
        """在后台线程使用同步 Playwright 选座下单"""

        def _do_select():
            def log(msg):
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
                    log(f"正在加载选座页面: {seat_url}")
                    page.goto(seat_url, wait_until="networkidle", timeout=30000)
                    page.wait_for_timeout(3000)

                    # ── 阶段1：处理事件选择弹窗 ──
                    event_modal_visible = page.evaluate("""() => {
                        let btn = document.querySelector('[onclick*="EventSelectChange"]');
                        return btn ? (btn.offsetParent !== null) : false;
                    }""")
                    if event_modal_visible:
                        log("检测到事件选择弹窗，点击确定...")
                        try:
                            page.locator("[onclick*='EventSelectChange']").click()
                            log("已点击确定")
                        except Exception as e:
                            log(f"点击确定失败: {e}")
                        # 等待弹窗关闭
                        for i in range(20):
                            page.wait_for_timeout(500)
                            still = page.evaluate("""() => {
                                let b = document.querySelector('[onclick*="EventSelectChange"]');
                                return b && b.offsetParent !== null;
                            }""")
                            if not still:
                                break
                        page.wait_for_timeout(1000)

                    # ── 阶段2：等待座位数据 AJAX 加载完成 ──
                    log("等待座位数据加载...")
                    seats_loaded = False
                    for i in range(60):  # 最多等30秒
                        page.wait_for_timeout(500)
                        state = page.evaluate("""() => {
                            let sc_count = document.querySelectorAll('.s-c').length;
                            let loading = document.getElementById('cart_load_msg');
                            let loading_hidden = !loading || loading.style.display === 'none' ||
                                                loading.offsetParent === null;
                            let pg_keys = typeof pg_seats_data === 'object' ?
                                          Object.keys(pg_seats_data).length : 0;
                            return {sc: sc_count, loading: loading_hidden, pg: pg_keys};
                        }""")
                        if state["sc"] > 0 and state["loading"]:
                            log(f"座位数据已加载: {state['sc']}个.s-c元素, pg_keys={state['pg']}")
                            seats_loaded = True
                            break
                        if i % 10 == 9:
                            log(f"仍在等待... sc={state['sc']} loading_hidden={state['loading']} pg_keys={state['pg']}")

                    if not seats_loaded:
                        page.screenshot(path="seat_map_no_load.png")
                        log("座位数据加载超时")

                    # ── 阶段3：获取目标价格金额并找匹配的座位 ──
                    price_amount = page.evaluate(f"""(pid) => {{
                        if (window.price_data && window.price_data[pid]) {{
                            return window.price_data[pid].I_PRICE_AMT;
                        }}
                        return null;
                    }}""", price_id)
                    log(f"price_id={price_id} -> 金额={price_amount}")

                    # 获取所有可售座位（匹配目标价格，非已售/非学生票）
                    target_seats = page.evaluate(f"""(targetPrice) => {{
                        let seats = [];
                        let all = document.querySelectorAll('.s-c');
                        all.forEach(function(el) {{
                            let cls = el.className || '';
                            let pa = el.getAttribute('PA') || '';
                            let notSale = el.getAttribute('NOT_SALE') || '0';
                            // 排除已售(saled)、已选(selected)、学生票(stu)
                            let is_sold = cls.includes('saled') || cls.includes('mk_saled');
                            let is_stu = cls.includes('stu') || cls.includes('mk_stu');
                            let not_for_sale = notSale === '1';
                            if (!is_sold && !is_stu && !not_for_sale && String(pa) === String(targetPrice)) {{
                                seats.push({{
                                    ed: el.getAttribute('ED') || '',
                                    esd: el.getAttribute('ESD') || '',
                                    zone: el.getAttribute('ZN') || '',
                                    row: el.getAttribute('RW') || '',
                                    col: el.getAttribute('CL') || '',
                                    pa: pa,
                                    cls: cls.substring(0, 50)
                                }});
                            }}
                        }});
                        return seats;
                    }}""", price_amount)
                    log(f"匹配价格¥{price_amount}的可用座位: {len(target_seats)}个")
                    if target_seats:
                        log(f"首个: {json.dumps(target_seats[0], ensure_ascii=False)}")
                        for s in target_seats[:5]:
                            log(f"  {s.get('zone')} {s.get('row')}排{s.get('col')}座")

                    # ── 阶段4：通过 JS 函数选座（直接调用 SeatsSelected，避开 jQuery 事件绑定问题） ──
                    seat_selected = False
                    if target_seats:
                        ts = target_seats[0]
                        ed = ts.get("ed") or str(event_id)
                        esd = ts.get("esd", "")
                        log(f"调用 SeatsSelected(ED={ed}, ESD={esd})...")
                        result = page.evaluate(f"""(function() {{
                            try {{
                                if (typeof SeatsSelected === 'function') {{
                                    SeatsSelected('{ed}', '{esd}');
                                    return {{ok: true}};
                                }}
                                return {{ok: false, err: 'SeatsSelected not found'}};
                            }} catch(e) {{
                                return {{ok: false, err: e.toString()}};
                            }}
                        }})()""")
                        log(f"SeatsSelected 结果: {json.dumps(result, ensure_ascii=False)}")
                        if result.get("ok"):
                            seat_selected = True
                            page.wait_for_timeout(1500)

                    if not seat_selected and target_seats:
                        # Fallback: 直接触发 .s-c 元素的原生 click 事件
                        esd = target_seats[0]["esd"]
                        log(f"Fallback: 触发 .s-c[ESD='{esd}'] 点击事件...")
                        page.evaluate(f"""document.querySelector(".s-c[ESD='{esd}']").click()""")
                        page.wait_for_timeout(1500)
                        seat_selected = True

                    if not seat_selected and target_seats:
                        # 最后尝试：鼠标坐标点击
                        pos = page.evaluate(f"""() => {{
                            let el = document.querySelector(".s-c[ESD='{target_seats[0]["esd"]}']");
                            if (el) {{ let r = el.getBoundingClientRect(); return {{x: r.x + r.width/2, y: r.y + r.height/2}}; }}
                            return null;
                        }}""")
                        if pos:
                            page.mouse.click(pos["x"], pos["y"])
                            page.wait_for_timeout(1000)
                            seat_selected = True
                            log(f"鼠标点击 ({pos['x']:.0f}, {pos['y']:.0f})")

                    page.screenshot(path="seat_map_after_select.png")

                    # ── 阶段5：验证购物车状态 ──
                    cart_state = page.evaluate("""() => {
                        let btn = document.querySelector('[onclick*="Buy"]');
                        let text = btn ? btn.textContent.trim() : '';
                        let gray = btn ? btn.className.includes('gray_s') : true;
                        let selected = document.querySelectorAll('.s-c.selected, .s-c.mk_selected');
                        let sel_info = [];
                        selected.forEach(el => {
                            sel_info.push({
                                row: el.getAttribute('RW'),
                                col: el.getAttribute('CL'),
                                price: el.getAttribute('PA'),
                                zone: el.getAttribute('ZN'),
                                ed: el.getAttribute('ED'),
                                esd: el.getAttribute('ESD')
                            });
                        });
                        return {cart_text: text, gray: gray, selected_count: selected.length, selected: sel_info};
                    }""")
                    log(f"购物车状态: {json.dumps(cart_state, ensure_ascii=False)}")

                    price_match = re.search(r'￥(\d+)', cart_state.get("cart_text", ""))
                    cart_amount = int(price_match.group(1)) if price_match else 0

                    if cart_state.get("selected_count", 0) == 0 and cart_amount == 0:
                        log("未选到座位，尝试强制调用 CartChange...")
                        page.evaluate("typeof CartChange === 'function' && CartChange()")
                        page.wait_for_timeout(1000)
                        # 重新检查
                        cart_state = page.evaluate("""() => {
                            let selected = document.querySelectorAll('.s-c.selected, .s-c.mk_selected');
                            return {selected_count: selected.length};
                        }""")
                        log(f"CartChange 后: selected={cart_state['selected_count']}")

                    if cart_state.get("selected_count", 0) == 0:
                        return {"code": -1, "msg": f"未选到座位。目标价格¥{price_amount}，可选{len(target_seats)}个"}

                    # ── 阶段6：点击"加入购物车"（Buy） ──
                    log(f"已选{cart_state['selected_count']}座 ¥{cart_amount}，点击加入购物车...")

                    # 如果按钮 gray_s，先通过 JS 取消
                    if cart_state.get("gray"):
                        log("按钮是 gray_s 状态，通过 JS 调用 Buy()...")
                        page.evaluate("""() => {
                            // 移除 gray_s 状态
                            let btn = document.querySelector('.AddShopCar');
                            if (btn) btn.classList.remove('gray_s');
                            if (typeof bo_gray !== 'undefined') bo_gray = false;
                            if (typeof Buy === 'function') Buy();
                        }""")
                    else:
                        page.evaluate("typeof Buy === 'function' && Buy()")
                    log("已触发 Buy()，等待页面跳转...")

                    # ── 阶段7：等待跳转到订单页 ──
                    for i in range(30):
                        page.wait_for_timeout(1000)
                        cur = page.url
                        if any(kw in cur for kw in ["OrderStep", "/order/", "Order", "ShoppingCart",
                                                       "ShopCar", "/cart/", "Cart", "BuyTicket",
                                                       "Commit", "PayConfirm"]):
                            log(f"已跳转到订单页 ({i+1}s): {cur}")
                            break
                        if i % 5 == 4:
                            log(f"等待跳转... {i+1}s 当前: {cur[:100]}")

                    page.wait_for_timeout(3000)
                    page.screenshot(path="seat_map_order_page.png")

                    # ── 阶段8：依次点击 结算 → [弹窗]我知道了，继续购买 → 提交订单 ──
                    # 流程：购物车页点"结算" → 弹窗出现，点"我知道了，继续购买"关闭弹窗 → 点"提交订单"
                    current_url = page.url
                    log(f"订单页URL: {current_url}")

                    def _page_has_exact_text(text):
                        """检查页面上是否存在包含指定文字的可见元素（精确子串匹配）"""
                        found = page.evaluate(f"""(function() {{
                            let target = '{text}';
                            let els = document.querySelectorAll('button, a, input[type="button"], input[type="submit"], [onclick], [class*="btn"], [class*="Btn"], [class*="pop"], [class*="Pop"], [class*="modal"], [class*="Modal"], [class*="dialog"], [class*="Dialog"], [class*="layer"], [class*="Layer"], [class*="tip"], [class*="Tip"], [class*="hint"], [class*="Hint"], [class*="alert"], [class*="Alert"], [class*="confirm"], [class*="Confirm"], [class*="msg"], [class*="Msg"], div, span, p, label, h1, h2, h3, h4');
                            for (let el of els) {{
                                let txt = (el.textContent || el.value || '').trim();
                                if (txt.includes(target)) {{
                                    let r = el.getBoundingClientRect();
                                    if (r.width > 0 && r.height > 0) return true;
                                }}
                            }}
                            return false;
                        }})()""")
                        return found

                    def _find_button_coords(text):
                        """查找包含指定文字的可见元素的中心坐标（优先可点击元素）"""
                        coords_json = page.evaluate(f"""(function() {{
                            let target = '{text}';
                            // 优先在可点击元素中查找
                            let clickable = 'button, a, input[type="button"], input[type="submit"], [onclick], [class*="btn"], [class*="Btn"]';
                            let els = document.querySelectorAll(clickable);
                            for (let el of els) {{
                                let txt = (el.textContent || el.value || '').trim();
                                if (txt.includes(target)) {{
                                    let r = el.getBoundingClientRect();
                                    if (r.width > 0 && r.height > 0) {{
                                        return JSON.stringify({{x: r.x + r.width/2, y: r.y + r.height/2}});
                                    }}
                                }}
                            }}
                            // 回退：在弹窗常见元素中查找
                            let popup = '[class*="pop"], [class*="Pop"], [class*="modal"], [class*="Modal"], [class*="dialog"], [class*="Dialog"], [class*="layer"], [class*="Layer"], [class*="tip"], [class*="Tip"], [class*="hint"], [class*="Hint"], [class*="alert"], [class*="Alert"], [class*="confirm"], [class*="Confirm"], [class*="msg"], [class*="Msg"]';
                            els = document.querySelectorAll(popup);
                            for (let el of els) {{
                                let txt = (el.textContent || '').trim();
                                if (txt.includes(target)) {{
                                    let r = el.getBoundingClientRect();
                                    if (r.width > 0 && r.height > 0) {{
                                        return JSON.stringify({{x: r.x + r.width/2, y: r.y + r.height/2}});
                                    }}
                                }}
                            }}
                            // 最后：在所有可见元素中按文字查找
                            let all = document.querySelectorAll('div, span, p, label, h1, h2, h3, h4');
                            for (let el of all) {{
                                let txt = (el.textContent || '').trim();
                                if (txt.includes(target) && txt.length < 200) {{
                                    let r = el.getBoundingClientRect();
                                    if (r.width > 0 && r.height > 0) {{
                                        return JSON.stringify({{x: r.x + r.width/2, y: r.y + r.height/2}});
                                    }}
                                }}
                            }}
                            return null;
                        }})()""")
                        return json.loads(coords_json) if coords_json else None

                    def _popup_visible():
                        """检查页面上是否有可见的弹窗/遮罩层"""
                        return page.evaluate("""(function() {
                            let sels = '[class*="pop"], [class*="Pop"], [class*="modal"], [class*="Modal"], [class*="dialog"], [class*="Dialog"], [class*="layer"], [class*="Layer"], [class*="tip"], [class*="Tip"], [class*="alert"], [class*="Alert"], [class*="confirm"], [class*="Confirm"], [class*="overlay"], [class*="Overlay"], [class*="mask"], [class*="Mask"]';
                            let els = document.querySelectorAll(sels);
                            for (let el of els) {
                                let r = el.getBoundingClientRect();
                                if (r.width > 100 && r.height > 100) return true;
                            }
                            return false;
                        })()""")

                    def _click_button_exact(text, step_label, expect_disappear=True):
                        """点击包含指定文字的按钮，支持多种点击方式重试。
                        expect_disappear=True: 点击后弹窗应关闭
                        expect_disappear=False: 点击后页面跳转，按钮自然消失
                        """
                        log(f"{step_label}: 查找 '{text}'...")

                        if not _page_has_exact_text(text):
                            log(f"  页面上未找到 '{text}'，可能已跳过此步骤")
                            return True

                        coords = _find_button_coords(text)
                        if not coords:
                            log(f"  未能获取 '{text}' 的坐标")
                            return False

                        had_popup = _popup_visible()

                        for attempt in range(5):
                            if expect_disappear:
                                if had_popup and not _popup_visible():
                                    log(f"  弹窗已关闭，'{text}' 点击成功")
                                    break
                                if not had_popup and not _page_has_exact_text(text):
                                    log(f"  '{text}' 已消失，点击成功")
                                    break

                            try:
                                if attempt == 0:
                                    # 第1轮：Playwright locator 点击（用 getByText 精确匹配）
                                    btn = page.get_by_text(text, exact=False).first
                                    if btn.count() > 0 and btn.is_visible():
                                        btn.click(timeout=5000)
                                        log(f"  第{attempt+1}轮: getByText.click")
                                    else:
                                        # 回退到坐标点击
                                        page.mouse.click(coords["x"], coords["y"])
                                        log(f"  第{attempt+1}轮: mouse.click (fallback)")
                                elif attempt == 1:
                                    # 第2轮：坐标点击
                                    page.mouse.click(coords["x"], coords["y"])
                                    log(f"  第{attempt+1}轮: mouse.click ({coords['x']:.0f},{coords['y']:.0f})")
                                elif attempt == 2:
                                    # 第3轮：JS dispatchEvent
                                    page.evaluate(f"""(function() {{
                                        let el = document.elementFromPoint({coords['x']}, {coords['y']});
                                        if (el) {{
                                            ['mousedown','mouseup','click'].forEach(function(t) {{
                                                el.dispatchEvent(new MouseEvent(t, {{bubbles:true,cancelable:true}}));
                                            }});
                                            let p = el.closest('a,button,[onclick]');
                                            if (p && p !== el) p.click();
                                        }}
                                    }})()""")
                                    log(f"  第{attempt+1}轮: JS dispatchEvent")
                                elif attempt == 3:
                                    # 第4轮：eval onclick
                                    page.evaluate(f"""(function() {{
                                        let target = '{text}';
                                        let els = document.querySelectorAll('[onclick]');
                                        for (let el of els) {{
                                            let txt = (el.textContent || '').trim();
                                            if (txt.includes(target)) {{
                                                try {{ eval(el.getAttribute('onclick')); }} catch(e) {{}}
                                                return;
                                            }}
                                        }}
                                    }})()""")
                                    log(f"  第{attempt+1}轮: eval(onclick)")
                                else:
                                    # 第5轮：模拟完整鼠标序列
                                    page.mouse.move(coords["x"], coords["y"])
                                    page.wait_for_timeout(100)
                                    page.mouse.down()
                                    page.wait_for_timeout(80)
                                    page.mouse.up()
                                    log(f"  第{attempt+1}轮: mouse.down/up")
                            except Exception as e:
                                log(f"  第{attempt+1}轮异常: {e}")

                            page.wait_for_timeout(2000)
                            # 重新获取坐标（页面可能已变化）
                            new_coords = _find_button_coords(text)
                            if new_coords:
                                coords = new_coords

                        # 最终检查
                        if expect_disappear:
                            popup_still = had_popup and _popup_visible()
                            text_still = _page_has_exact_text(text)
                            if popup_still or (not had_popup and text_still):
                                log(f"  重试5轮后 '{text}' 仍存在，此步骤失败")
                                page.screenshot(path=f"seat_map_{step_label}_failed.png")
                                return False

                        log(f"  '{text}' 点击完成")
                        page.wait_for_timeout(2000)
                        page.screenshot(path=f"seat_map_{step_label}.png")
                        return True

                    # Step 1: 购物车页 → 点击"结算"跳转到订单确认页
                    log("=== Step 1: 点击结算 ===")
                    _click_button_exact("结算", "step1_settle", expect_disappear=False)

                    # 等待页面跳转/加载
                    page.wait_for_timeout(3000)
                    cur_url = page.url
                    log(f"结算后URL: {cur_url}")
                    page.screenshot(path="seat_map_after_settle.png")

                    # Step 2: 处理弹窗 → 点击"我知道了，继续购买"关闭弹窗
                    log("=== Step 2: 处理弹窗 ===")
                    # 先等一下弹窗出现
                    popup_appeared = False
                    for i in range(10):
                        if _page_has_exact_text("我知道了"):
                            popup_appeared = True
                            log(f"  弹窗已出现 ({(i+1)*0.5:.1f}s)")
                            break
                        page.wait_for_timeout(500)

                    if popup_appeared:
                        _click_button_exact("我知道了，继续购买", "step2_popup", expect_disappear=True)
                    else:
                        log("  未检测到弹窗，尝试直接点击...")
                        # 可能弹窗文字不同，尝试其他常见文案
                        for alt_text in ["我知道了", "知道了", "继续", "确定"]:
                            if _page_has_exact_text(alt_text):
                                log(f"  找到替代按钮: '{alt_text}'")
                                _click_button_exact(alt_text, "step2_alt", expect_disappear=True)
                                break

                    page.wait_for_timeout(2000)
                    page.screenshot(path="seat_map_after_popup.png")

                    # Step 3: 点击"提交订单"
                    log("=== Step 3: 提交订单 ===")
                    _click_button_exact("提交订单", "step3_submit", expect_disappear=False)

                    page.wait_for_timeout(3000)
                    page.screenshot(path="seat_map_after_submit.png")

                    # Step 4: 处理"友情提示"弹窗 → 勾选"我同意" → 点击"确定"
                    log("=== Step 4: 友情提示弹窗 ===")
                    hint_appeared = False
                    for i in range(30):  # 最多等15秒，弹窗可能加载较慢
                        if _page_has_exact_text("我同意") or _page_has_exact_text("友情提示"):
                            hint_appeared = True
                            log(f"  友情提示弹窗已出现 ({(i+1)*0.5:.1f}s)")
                            break
                        # 每5秒截一次图，方便排查
                        if i > 0 and i % 10 == 0:
                            page.screenshot(path=f"seat_map_step4_wait_{i}.png")
                            log(f"  等待弹窗中... ({(i+1)*0.5:.1f}s)")
                        page.wait_for_timeout(500)

                    if hint_appeared:
                        # 勾选"我同意"复选框
                        checked = page.evaluate("""(function() {
                            // 方式1：查找包含"我同意"文字附近的 checkbox/input
                            let els = document.querySelectorAll('input[type="checkbox"]');
                            for (let el of els) {
                                let parent = el.closest('div, label, li, p, span, td');
                                if (parent && (parent.textContent || '').includes('我同意')) {
                                    if (!el.checked) { el.click(); }
                                    return true;
                                }
                            }
                            // 方式2：查找 label 中含"我同意"且关联的 checkbox
                            let labels = document.querySelectorAll('label');
                            for (let lb of labels) {
                                if ((lb.textContent || '').includes('我同意')) {
                                    let forId = lb.getAttribute('for');
                                    let cb = forId ? document.getElementById(forId) : lb.querySelector('input[type="checkbox"]');
                                    if (cb && !cb.checked) { cb.click(); return true; }
                                    // 直接点击 label 也可能触发 checkbox
                                    lb.click(); return true;
                                }
                            }
                            // 方式3：查找弹窗/遮罩层内的所有 checkbox
                            let popups = document.querySelectorAll('[class*="pop"], [class*="Pop"], [class*="modal"], [class*="Modal"], [class*="dialog"], [class*="Dialog"], [class*="layer"], [class*="Layer"], [class*="tip"], [class*="Tip"], [class*="alert"], [class*="Alert"], [class*="confirm"], [class*="Confirm"]');
                            for (let pop of popups) {
                                let cbs = pop.querySelectorAll('input[type="checkbox"]');
                                for (let cb of cbs) {
                                    if (!cb.checked) { cb.click(); return true; }
                                }
                            }
                            // 方式4：查找所有可见 checkbox，勾选未勾选的
                            let allCb = document.querySelectorAll('input[type="checkbox"]');
                            for (let cb of allCb) {
                                if (!cb.checked) {
                                    let r = cb.getBoundingClientRect();
                                    if (r.width > 0 && r.height > 0) { cb.click(); return true; }
                                }
                            }
                            return false;
                        })""")
                        log(f"  勾选'我同意': {'成功' if checked else '未找到复选框'}")
                        page.wait_for_timeout(500)
                        page.screenshot(path="seat_map_after_agree.png")

                        # 点击"确定"
                        _click_button_exact("确定", "step4_confirm", expect_disappear=True)
                    else:
                        log("  未检测到友情提示弹窗，跳过")

                    page.wait_for_timeout(3000)
                    page.screenshot(path="seat_map_after_confirm.png")

                    # ── 阶段9：最终结果 ──
                    page.screenshot(path="seat_map_final.png")
                    final_url = page.url
                    log(f"最终URL: {final_url}")

                    url_success = any(ind in final_url for ind in
                                     ["OrderStep", "orderdetail", "payresult", "PayResult",
                                      "Success", "success", "payconfirm", "PayConfirm"])
                    if url_success:
                        return {"code": 0, "msg": f"选座下单成功! URL: {final_url}"}

                    final_text = page.evaluate("document.body.innerText.substring(0, 500)")
                    if any(w in final_text for w in ["下单成功", "订单提交成功", "支付成功"]):
                        return {"code": 0, "msg": "页面内容显示下单成功"}

                    return {"code": -1,
                            "msg": f"订单流程未完成。最终URL: {final_url[:100]}"}
                except Exception as e:
                    import traceback
                    return {"code": -1, "msg": f"选座异常: {e}\n{traceback.format_exc()}"}
                finally:
                    page.close()
                    context.close()
                    browser.close()

        return _do_select()

    def fetch_captcha(self) -> tuple[Image.Image, dict]:
        """打开登录页，截图验证码，返回 (PIL.Image, cookies_dict)"""
        page = self._browser.new_page()
        try:
            page.goto(f"{BASE_URL}/PersonalCenter/loginwechat.aspx",
                      wait_until="networkidle", timeout=15000)
            captcha_el = page.locator("#yanzhengma")
            if captcha_el.count() == 0:
                raise RuntimeError("页面中未找到验证码元素 #yanzhengma")
            img_bytes = captcha_el.screenshot()
            cookies = {c["name"]: c["value"] for c in page.context.cookies()}
            img = Image.open(io.BytesIO(img_bytes))
            return img, cookies
        finally:
            page.close()


# ─── API 层 ───────────────────────────────────────────────────────────────────

class SHCSTheatreAPI:
    """封装上海文化广场网站的所有 HTTP 接口"""

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update(HEADERS)
        self.token = ""

    def set_cookies(self, cookies: dict):
        for name, value in cookies.items():
            self.session.cookies.set(name, value, domain="m.shcstheatre.com")

    def login(self, username: str, password: str, captcha: str) -> dict:
        """POST /WebAPIWeChat.ashx?op=CustomerLoginWeChat"""
        url = f"{BASE_URL}/WebAPIWeChat.ashx?op=CustomerLoginWeChat"
        data = {
            "username": username,
            "newpassword": password,
            "loginsurecode": captcha,
            "sessioncode": captcha,
            "cookieOP_ID": "",
            "OPEND_ID_COOKIE": "",
        }
        resp = self.session.post(url, data=data)
        result = resp.json()
        if result.get("code") == 0 and result.get("iRtn") == 0:
            self.token = result.get("token", "")
        return result

    def get_program_list(self) -> list[dict]:
        """抓取剧目列表页面"""
        url = f"{BASE_URL}/Program/ProgramListWeChat.aspx?GROUP_ID=351"
        resp = self.session.get(url)
        soup = BeautifulSoup(resp.text, "lxml")
        programs = []
        seen = set()
        for a in soup.find_all("a", href=True):
            href = a["href"]
            m = re.search(r"ProgramDetailsWeChat\.aspx\?id=(\d+)", href)
            if m:
                pid = int(m.group(1))
                name = a.get_text(strip=True)
                if pid not in seen and name and "购票" not in name:
                    seen.add(pid)
                    programs.append({"id": pid, "name": name})
        return programs

    def get_events(self, program_id: int) -> tuple[list[dict], dict]:
        """POST /webapi.ashx?op=Gettblprogram，返回 (events, program_info)"""
        url = f"{BASE_URL}/webapi.ashx?op=Gettblprogram"
        resp = self.session.post(url, data={"token": self.token, "id": program_id})
        result = resp.json()
        events = []
        program_info = {}
        if result.get("code") == 0 and result.get("data"):
            data = result["data"]
            # 提取剧目级别信息（含 I_SEAT_TYPE）
            tbl = data.get("tblprogram")
            if isinstance(tbl, list) and tbl:
                p = tbl[0]
                if isinstance(p, dict):
                    program_info = {
                        "seat_type": int(p.get("I_SEAT_TYPE", 0)),
                        "scs_type": int(p.get("SCS_TYPE", 0)),
                        "special_type": p.get("SPECIAL_TYPE", ""),
                    }
            tb = data.get("TBLEVENT") or []
            for e in tb:
                # B_WEB_SELECTSEAT=1 表示需要选座，否则为无座(自动分配)
                select_seat = int(float(e.get("B_WEB_SELECTSEAT", 0)))
                events.append({
                    "event_id": e["I_EVENT_ID"],
                    "datetime": e["DT_EVENT_DATETIME"],
                    "week": e.get("VC_WEEK", ""),
                    "if_begin": int(e.get("IF_BEGIN", 0)),
                    "if_begin_pg": int(e.get("IF_BEGIN_PG", 0)),
                    "seat_cnt": float(e.get("I_WHGC_WEB_SEAT_CNT", 0)),
                    "rg_begin": int(e.get("RG_BEGIN", 0)),
                    "event_name": e.get("VC_EVENT_NAME", ""),
                    "select_seat": select_seat,
                })
            # 从事件级别推断 seat_type（如果 tblprogram 为空）
            if not program_info and events:
                any_select = any(e["select_seat"] == 1 for e in events)
                program_info = {"seat_type": 1 if any_select else 2}
        return events, program_info

    def get_price_levels(self, event_id: int) -> list[dict]:
        """POST /webapi.ashx?op=GettblpricelevelList_ns"""
        url = f"{BASE_URL}/webapi.ashx?op=GettblpricelevelList_ns"
        resp = self.session.post(url, data={"I_EVENT_ID": event_id})
        _log_buf.append(f"[API] get_price_levels HTTP {resp.status_code} len={len(resp.text)}")
        result = resp.json()
        if result.get("code") != 0:
            _log_buf.append(f"[API] get_price_levels 异常响应: {json.dumps(result, ensure_ascii=False)[:300]}")
            _flush_log("get_price_levels 异常响应")
        prices = []
        if result.get("code") == 0 and result.get("data"):
            for p in result["data"]:
                prices.append({
                    "price_id": int(p["I_PRICE_ID"]),
                    "price_amt": float(p["I_PRICE_AMT"]),
                    "desc": p.get("VC_PRICEDESC", ""),
                    "remark": p.get("VC_REMARK", ""),
                    "sold_out": int(p.get("SOLD_OUT", 0)),
                    "seat_cnt": int(p.get("I_WHGC_WEB_SEAT_CNT", 0)),
                })
        return prices

    def check_price_availability(self, event_id: int, price_id: int, event_if_begin: int = 1) -> dict:
        """查询指定票档是否有余票（必须场次 IF_BEGIN==1 才可购买）"""
        prices = self.get_price_levels(event_id)
        for p in prices:
            if p["price_id"] == price_id:
                can_buy = event_if_begin == 1 and p["sold_out"] == 0 and p["seat_cnt"] > 0
                return {
                    "available": can_buy,
                    "seat_cnt": p["seat_cnt"],
                    "sold_out": p["sold_out"] == 1,
                    "if_begin": event_if_begin,
                }
        return {"available": False, "seat_cnt": 0, "sold_out": True, "if_begin": event_if_begin}

    def buy_ticket(self, event_id: int, price_id: int, qty: int = 1) -> dict:
        """POST /SK_WebAPI.ashx?op=NoSeatBuy — 系统自动分配座位"""
        url = f"{BASE_URL}/SK_WebAPI.ashx?op=NoSeatBuy"
        data = {
            "I_EVENT_ID": event_id,
            "I_PRICE_ID": price_id,
            "iQty": qty,
            "token": self.token,
        }
        resp = self.session.post(url, data=data)
        _log_buf.append(f"[API] buy_ticket HTTP {resp.status_code} event={event_id} price={price_id} qty={qty}")
        result = resp.json()
        _log_buf.append(f"[API] buy_ticket 响应: {json.dumps(result, ensure_ascii=False)[:500]}")
        result["_debug"] = {
            "event_id": event_id,
            "price_id": price_id,
            "qty": qty,
            "token_prefix": self.token[:8] + "..." if len(self.token) > 8 else self.token,
            "http_status": resp.status_code,
        }
        return result


# ─── 微信推送 ─────────────────────────────────────────────────────────────────

class WeChatNotifier:
    """支持 Server酱 / PushPlus / WxPusher 微信推送"""

    @staticmethod
    def send_serverchan(send_key: str, title: str, content: str) -> tuple[bool, str]:
        url = f"https://sctapi.ftqq.com/{send_key}.send"
        try:
            resp = requests.post(url, data={"title": title, "desp": content}, timeout=10)
            data = resp.json()
            if data.get("code") == 0:
                return True, "发送成功"
            return False, f"API返回: code={data.get('code')}, msg={data.get('message', '')}"
        except Exception as e:
            return False, f"请求异常: {e}"

    @staticmethod
    def send_pushplus(token: str, title: str, content: str) -> tuple[bool, str]:
        url = "https://www.pushplus.plus/send"
        try:
            resp = requests.post(url, json={"token": token, "title": title, "content": content}, timeout=10)
            data = resp.json()
            if data.get("code") == 200:
                return True, "发送成功"
            return False, f"API返回: code={data.get('code')}, msg={data.get('msg', '')}"
        except Exception as e:
            return False, f"请求异常: {e}"

    @staticmethod
    def send_wxpusher(app_token: str, uids: list[str], title: str, content: str) -> tuple[bool, str]:
        """WxPusher 推送 — 会触发手机通知，返回 (是否成功, 详情信息)"""
        url = "https://wxpusher.zjiecode.com/api/send/message"
        payload = {
            "appToken": app_token,
            "content": content,
            "summary": title,
            "contentType": 1,
            "uids": uids,
        }
        try:
            resp = requests.post(url, json=payload, timeout=10)
            data = resp.json()
            if data.get("code") != 1000:
                return False, f"API返回: code={data.get('code')}, msg={data.get('msg', '')}"
            # 检查每条消息的发送状态
            details = data.get("data") or []
            failed = []
            for d in details:
                scode = d.get("code", 0)
                if scode != 1000:
                    uid = d.get("uid", "?")
                    status = d.get("status", "")
                    failed.append(f"UID={uid}: code={scode} {status}")
            if failed:
                return False, "; ".join(failed)
            return True, "发送成功"
        except Exception as e:
            return False, f"请求异常: {e}"


# ─── GUI ──────────────────────────────────────────────────────────────────────

class TicketMonitorGUI:
    """主界面"""

    def __init__(self):
        self.browser = BrowserManager()
        self.api = SHCSTheatreAPI()
        self.ocr = ddddocr.DdddOcr(show_ad=False)
        self.programs: list[dict] = []
        self.events: list[dict] = []
        self.prices: list[dict] = []
        self.monitoring = False
        self.monitor_thread: threading.Thread | None = None
        self._captcha_cookies: dict = {}

        # 持久化选择（修复切换 Tab 后丢失的问题）
        self._sel_event_id: int | None = None
        self._sel_price_id: int | None = None
        self._sel_price_info: dict | None = None
        self._sel_program_name: str = ""
        self._sel_event_dt: str = ""
        self._sel_program_id: int | None = None

        self._build_ui()

    # ──────── 构建界面 ────────
    def _build_ui(self):
        self.root = tk.Tk()
        self.root.title("上海文化广场 · 余票监测")
        self.root.geometry("920x740")
        self.root.resizable(False, False)

        notebook = ttk.Notebook(self.root)
        notebook.pack(fill="both", expand=True, padx=6, pady=6)

        tab_login = ttk.Frame(notebook)
        notebook.add(tab_login, text="  登录  ")
        self._build_login_tab(tab_login)

        tab_select = ttk.Frame(notebook)
        notebook.add(tab_select, text="  选择演出  ")
        self._build_select_tab(tab_select)

        tab_monitor = ttk.Frame(notebook)
        notebook.add(tab_monitor, text="  余票监测  ")
        self._build_monitor_tab(tab_monitor)

        tab_push = ttk.Frame(notebook)
        notebook.add(tab_push, text="  微信推送  ")
        self._build_push_tab(tab_push)

        self.notebook = notebook
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ---- 登录页 ----
    def _build_login_tab(self, parent):
        frame = ttk.LabelFrame(parent, text="手机号 + 密码 登录", padding=15)
        frame.pack(padx=20, pady=20, fill="x")

        ttk.Label(frame, text="手机号：").grid(row=0, column=0, sticky="e", pady=4)
        self.entry_phone = ttk.Entry(frame, width=30)
        self.entry_phone.grid(row=0, column=1, padx=5, pady=4)

        ttk.Label(frame, text="密  码：").grid(row=1, column=0, sticky="e", pady=4)
        self.entry_pwd = ttk.Entry(frame, width=30, show="*")
        self.entry_pwd.grid(row=1, column=1, padx=5, pady=4)

        ttk.Label(frame, text="验证码：").grid(row=2, column=0, sticky="e", pady=4)
        captcha_row = ttk.Frame(frame)
        captcha_row.grid(row=2, column=1, sticky="w", padx=5, pady=4)
        self.entry_captcha = ttk.Entry(captcha_row, width=12)
        self.entry_captcha.pack(side="left")

        self.btn_refresh_captcha = ttk.Button(captcha_row, text="刷新验证码", command=self._refresh_captcha)
        self.btn_refresh_captcha.pack(side="left", padx=8)

        self.auto_ocr_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(captcha_row, text="自动识别", variable=self.auto_ocr_var).pack(side="left", padx=4)

        self.captcha_label = ttk.Label(frame)
        self.captcha_label.grid(row=3, column=1, sticky="w", padx=5, pady=4)

        btn_row = ttk.Frame(frame)
        btn_row.grid(row=4, column=1, sticky="w", padx=5, pady=10)
        self.btn_login = ttk.Button(btn_row, text="登  录", command=self._do_login)
        self.btn_login.pack(side="left")
        self.btn_start_browser = ttk.Button(btn_row, text="启动浏览器", command=self._start_browser_async)
        self.btn_start_browser.pack(side="left", padx=12)

        self.login_status = ttk.Label(frame, text='未登录（请先点击"启动浏览器"）', foreground="gray")
        self.login_status.grid(row=5, column=0, columnspan=2, pady=4)

    # ---- 选择页 ----
    def _build_select_tab(self, parent):
        f1 = ttk.LabelFrame(parent, text="① 选择剧目", padding=5)
        f1.pack(side="left", fill="both", expand=True, padx=(10, 3), pady=10)

        self.btn_load_programs = ttk.Button(f1, text="加载可购票剧目", command=self._load_programs)
        self.btn_load_programs.pack(pady=4)

        self.prog_listbox = tk.Listbox(f1, height=18, font=("Microsoft YaHei", 9))
        self.prog_listbox.pack(fill="both", expand=True, padx=2)
        self.prog_listbox.bind("<<ListboxSelect>>", self._on_program_select)

        f2 = ttk.LabelFrame(parent, text="② 选择场次", padding=5)
        f2.pack(side="left", fill="both", expand=True, padx=3, pady=10)

        self.event_listbox = tk.Listbox(f2, height=18, font=("Microsoft YaHei", 9))
        self.event_listbox.pack(fill="both", expand=True, padx=2)
        self.event_listbox.bind("<<ListboxSelect>>", self._on_event_select)

        f3 = ttk.LabelFrame(parent, text="③ 选择票档", padding=5)
        f3.pack(side="left", fill="both", expand=True, padx=(3, 10), pady=10)

        self.price_listbox = tk.Listbox(f3, height=18, font=("Microsoft YaHei", 9))
        self.price_listbox.pack(fill="both", expand=True, padx=2)
        self.price_listbox.bind("<<ListboxSelect>>", self._on_price_select)

        self.lbl_selection = ttk.Label(parent, text="当前未选择", anchor="center",
                                       font=("Microsoft YaHei", 10, "bold"))
        self.lbl_selection.pack(side="bottom", fill="x", pady=6)

    # ---- 监测页 ----
    def _build_monitor_tab(self, parent):
        ctrl = ttk.LabelFrame(parent, text="监测控制", padding=10)
        ctrl.pack(fill="x", padx=10, pady=(10, 5))

        ttk.Label(ctrl, text="刷新间隔(秒)：").pack(side="left")
        self.spin_interval = ttk.Spinbox(ctrl, from_=1, to=60, width=5)
        self.spin_interval.set(3)
        self.spin_interval.pack(side="left", padx=5)

        ttk.Label(ctrl, text="购买数量：").pack(side="left", padx=(20, 0))
        self.spin_qty = ttk.Spinbox(ctrl, from_=1, to=4, width=3)
        self.spin_qty.set(1)
        self.spin_qty.pack(side="left", padx=5)

        self.btn_start = ttk.Button(ctrl, text="▶ 开始监测", command=self._start_monitor)
        self.btn_start.pack(side="left", padx=15)

        self.btn_stop = ttk.Button(ctrl, text="■ 停止监测", command=self._stop_monitor, state="disabled")
        self.btn_stop.pack(side="left", padx=5)

        self.monitor_status = ttk.Label(ctrl, text="未运行", foreground="gray")
        self.monitor_status.pack(side="right", padx=10)

        # 显示当前已选
        self.lbl_monitor_sel = ttk.Label(parent, text="尚未选择演出", anchor="center",
                                          font=("Microsoft YaHei", 9), foreground="gray")
        self.lbl_monitor_sel.pack(fill="x", padx=10, pady=(2, 0))

        ttk.Label(parent, text="注：下单后由系统自动分配座位，无法手动选座",
                  foreground="gray", font=("Microsoft YaHei", 8)).pack(fill="x", padx=10)

        log_frame = ttk.LabelFrame(parent, text="运行日志", padding=5)
        log_frame.pack(fill="both", expand=True, padx=10, pady=5)

        self.log_text = scrolledtext.ScrolledText(log_frame, height=18, font=("Consolas", 9), state="disabled")
        self.log_text.pack(fill="both", expand=True)

    # ---- 推送设置页 ----
    def _build_push_tab(self, parent):
        frame = ttk.LabelFrame(parent, text="微信推送配置", padding=15)
        frame.pack(padx=20, pady=20, fill="x")

        ttk.Label(frame, text="推送方式：").grid(row=0, column=0, sticky="e", pady=6)
        self.push_method = tk.StringVar(value="wxpusher")
        ttk.Radiobutton(frame, text="WxPusher（推荐，手机通知）", variable=self.push_method,
                         value="wxpusher").grid(row=0, column=1, sticky="w")
        ttk.Radiobutton(frame, text="Server酱", variable=self.push_method,
                         value="serverchan").grid(row=1, column=1, sticky="w")
        ttk.Radiobutton(frame, text="PushPlus", variable=self.push_method,
                         value="pushplus").grid(row=1, column=2, sticky="w")

        ttk.Label(frame, text="Token / Key：").grid(row=2, column=0, sticky="e", pady=6)
        self.entry_push_key = ttk.Entry(frame, width=50)
        self.entry_push_key.grid(row=2, column=1, columnspan=2, padx=5, pady=6)

        ttk.Label(frame, text="WxPusher UID：").grid(row=3, column=0, sticky="e", pady=6)
        self.entry_wxpusher_uid = ttk.Entry(frame, width=50)
        self.entry_wxpusher_uid.grid(row=3, column=1, columnspan=2, padx=5, pady=6)

        ttk.Button(frame, text="发送测试", command=self._test_push).grid(row=4, column=1, sticky="w", pady=10)

        tip = ttk.Label(frame, text=(
            "WxPusher（推荐）：关注微信公众号「WxPusher」获取UID，手机能收到通知\n"
            "Server酱：sct.ftqq.com 获取 SendKey（免费版为服务号消息，不一定弹通知）\n"
            "PushPlus：pushplus.plus 获取 Token"
        ), foreground="gray", justify="left")
        tip.grid(row=5, column=0, columnspan=3, sticky="w", pady=(10, 0))

    # ──────── 浏览器 & 验证码 ────────

    def _start_browser_async(self):
        self.btn_start_browser.configure(state="disabled", text="启动中...")
        self.login_status.configure(text="正在启动浏览器...", foreground="blue")
        threading.Thread(target=self._start_browser_worker, daemon=True).start()

    def _start_browser_worker(self):
        try:
            self.browser.start()
            self.root.after(0, lambda: self.login_status.configure(
                text="浏览器已启动，正在加载验证码...", foreground="blue"))
            # Playwright 操作必须在同一线程内完成，不能嵌套调用 _load_captcha_worker
            img, cookies = self.browser.fetch_captcha()
            self._captcha_cookies = cookies
            self.api.set_cookies(cookies)
            img_bytes = io.BytesIO()
            img.save(img_bytes, format="PNG")
            raw_bytes = img_bytes.getvalue()
            ocr_text = ""
            if self.auto_ocr_var.get():
                ocr_text = self.ocr.classification(raw_bytes)
            self.root.after(0, lambda b=raw_bytes: self._update_captcha_display(b))
            if ocr_text:
                self.root.after(100, lambda t=ocr_text: self._auto_fill_captcha(t))
            self.root.after(0, lambda: self.btn_start_browser.configure(text="浏览器已启动"))
            self.root.after(0, lambda: self.login_status.configure(
                text="浏览器已启动，可输入密码登录", foreground="green"))
        except Exception as e:
            err_msg = str(e)
            self.root.after(0, lambda m=err_msg: self.login_status.configure(text=f"启动失败: {m}", foreground="red"))
            self.root.after(0, lambda: self.btn_start_browser.configure(state="normal", text="启动浏览器"))

    def _refresh_captcha(self):
        if not self.browser._browser:
            messagebox.showwarning("提示", '请先点击"启动浏览器"')
            return
        self.captcha_label.configure(text="加载中...")
        threading.Thread(target=self._refresh_captcha_worker, daemon=True).start()

    def _refresh_captcha_worker(self):
        """刷新验证码（独立线程，所有 Playwright 操作在此线程完成）"""
        try:
            img, cookies = self.browser.fetch_captcha()
            self._captcha_cookies = cookies
            self.api.set_cookies(cookies)
            img_bytes = io.BytesIO()
            img.save(img_bytes, format="PNG")
            raw_bytes = img_bytes.getvalue()
            ocr_text = ""
            if self.auto_ocr_var.get():
                ocr_text = self.ocr.classification(raw_bytes)
            self.root.after(0, lambda b=raw_bytes: self._update_captcha_display(b))
            if ocr_text:
                self.root.after(100, lambda t=ocr_text: self._auto_fill_captcha(t))
        except Exception as e:
            err_msg = str(e)
            self.root.after(0, lambda m=err_msg: self.captcha_label.configure(text=f"加载失败: {m}"))

    def _update_captcha_display(self, img_bytes: bytes):
        """在主线程中更新验证码图片（PhotoImage 必须在主线程创建）"""
        try:
            img = Image.open(io.BytesIO(img_bytes))
            img_resized = img.resize((150, 50))
            photo = ImageTk.PhotoImage(img_resized)
            self._captcha_photo = photo
            self.captcha_label.configure(image=photo, text="")
        except Exception as e:
            self.captcha_label.configure(text=f"显示失败: {e}")

    def _auto_fill_captcha(self, text):
        self.entry_captcha.delete(0, tk.END)
        self.entry_captcha.insert(0, text)
        self._log(f"验证码自动识别: {text}")

    def _do_login(self):
        if not self.browser._browser:
            messagebox.showwarning("提示", '请先点击"启动浏览器"')
            return
        phone = self.entry_phone.get().strip()
        pwd = self.entry_pwd.get().strip()
        captcha = self.entry_captcha.get().strip()
        if not phone or not pwd or not captcha:
            messagebox.showwarning("提示", "请填写完整登录信息")
            return
        self.btn_login.configure(state="disabled")
        threading.Thread(target=self._login_worker, args=(phone, pwd, captcha), daemon=True).start()

    def _login_worker(self, phone, pwd, captcha):
        try:
            result = self.api.login(phone, pwd, captcha)
            if result.get("code") == 0 and result.get("iRtn") == 0:
                self.root.after(0, lambda: self.login_status.configure(text="登录成功", foreground="green"))
                self._log(f"登录成功 token={self.api.token[:12]}..." if self.api.token else "登录成功（token为空！）")
            else:
                msg = result.get("msg", "登录失败")
                self.root.after(0, lambda m=msg: self.login_status.configure(text=f"失败: {m}", foreground="red"))
                self._log(f"登录失败: {msg}")
                self._refresh_captcha_worker()
        except Exception as e:
            err_msg = str(e)
            self.root.after(0, lambda m=err_msg: self.login_status.configure(text=f"异常: {m}", foreground="red"))
            self._log(f"登录异常: {err_msg}")
        finally:
            self.root.after(0, lambda: self.btn_login.configure(state="normal"))

    # ──────── 选择回调 ────────

    def _load_programs(self):
        self._log("正在加载剧目列表...")
        try:
            self.programs = self.api.get_program_list()
            self.prog_listbox.delete(0, tk.END)
            for p in self.programs:
                self.prog_listbox.insert(tk.END, f"[{p['id']}] {p['name']}")
            self._log(f"加载到 {len(self.programs)} 个剧目")
        except Exception as e:
            self._log(f"加载剧目失败: {e}")

    def _on_program_select(self, _event):
        sel = self.prog_listbox.curselection()
        if not sel:
            return
        prog = self.programs[sel[0]]
        self._sel_program_name = prog["name"]
        self._sel_program_id = prog["id"]
        self._log(f"正在加载 [{prog['name']}] 的场次...")
        # 清空后续选择
        self._sel_event_id = None
        self._sel_price_id = None
        self._sel_price_info = None
        self._sel_event_dt = ""
        try:
            self.events, prog_info = self.api.get_events(prog["id"])
            select_cnt = sum(1 for e in self.events if e.get("select_seat") == 1)
            noselect_cnt = len(self.events) - select_cnt
            self._log(f"加载到 {len(self.events)} 个场次（选座:{select_cnt} 无座:{noselect_cnt}）")
            self.event_listbox.delete(0, tk.END)
            self.price_listbox.delete(0, tk.END)
            for e in self.events:
                dt = e["datetime"][:16]
                week = e["week"] or ""
                ib = e["if_begin"]
                if ib == 1:
                    status = "可购"
                elif ib == 2:
                    status = "已结束"
                else:
                    status = "未开售"
                st = "选座" if e.get("select_seat") == 1 else "无座"
                self.event_listbox.insert(tk.END, f"{dt} {week} [{status}] {st} 余:{e['seat_cnt']:.0f}")
            self._update_selection_label()
        except Exception as ex:
            self._log(f"加载场次失败: {ex}")

    def _on_event_select(self, _event):
        sel = self.event_listbox.curselection()
        if not sel:
            return
        ev = self.events[sel[0]]
        self._sel_event_id = ev["event_id"]
        self._sel_event_dt = ev["datetime"][:16]
        self._sel_price_id = None
        self._sel_price_info = None
        self._log(f"正在加载票档...")
        try:
            self.prices = self.api.get_price_levels(ev["event_id"])
            self.price_listbox.delete(0, tk.END)
            for p in self.prices:
                sold = "已售罄" if p["sold_out"] else "有票"
                self.price_listbox.insert(tk.END,
                    f"¥{p['price_amt']:.0f} {p['desc']} {p['remark']} [{sold}]")
            self._log(f"加载到 {len(self.prices)} 个票档")
            self._update_selection_label()
        except Exception as ex:
            self._log(f"加载票档失败: {ex}")

    def _on_price_select(self, _event):
        sel = self.price_listbox.curselection()
        if not sel:
            return
        pr = self.prices[sel[0]]
        self._sel_price_id = pr["price_id"]
        self._sel_price_info = pr
        self._update_selection_label()

    def _update_selection_label(self):
        parts = []
        ps = self.prog_listbox.curselection()
        if ps:
            parts.append(self.programs[ps[0]]["name"])
        if self._sel_event_id:
            for e in self.events:
                if e["event_id"] == self._sel_event_id:
                    parts.append(e["datetime"][:16])
                    break
        if self._sel_price_info:
            parts.append(f"¥{self._sel_price_info['price_amt']:.0f}")
        text = " → ".join(parts) if parts else "当前未选择"
        self.lbl_selection.configure(text=text)
        self.lbl_monitor_sel.configure(text=text)

    # ──────── 监测逻辑 ────────

    def _start_monitor(self):
        if not self._sel_event_id or not self._sel_price_id:
            messagebox.showwarning("提示", '请先在"选择演出"页选择场次和票档')
            return
        if not self.api.token:
            messagebox.showwarning("提示", "请先登录")
            return

        self.monitoring = True
        self.btn_start.configure(state="disabled")
        self.btn_stop.configure(state="normal")
        self.monitor_status.configure(text="监测中...", foreground="blue")

        event_id = self._sel_event_id
        price_id = self._sel_price_id
        price_info = self._sel_price_info
        interval = int(self.spin_interval.get())
        qty = int(self.spin_qty.get())

        # 从已加载的场次中获取 if_begin 和选座状态
        event_if_begin = 1
        is_select_seat = False
        for ev in self.events:
            if ev["event_id"] == event_id:
                event_if_begin = ev["if_begin"]
                is_select_seat = ev.get("select_seat") == 1
                break

        seat_str = "选座" if is_select_seat else "无座(NoSeatBuy)"
        self._log(f"开始监测 | 场次:{event_id} 票档:¥{price_info['price_amt']:.0f} 间隔:{interval}s 数量:{qty} IF_BEGIN:{event_if_begin} 类型:{seat_str}")

        if is_select_seat:
            self._log("注意：该场次为选座事件，NoSeatBuy 不适用时将自动用 Playwright 选座")

        self.monitor_thread = threading.Thread(
            target=self._monitor_loop,
            args=(event_id, price_id, qty, interval, event_if_begin, is_select_seat),
            daemon=True
        )
        self.monitor_thread.start()

    def _stop_monitor(self):
        self.monitoring = False
        self.btn_start.configure(state="normal")
        self.btn_stop.configure(state="disabled")
        self.monitor_status.configure(text="已停止", foreground="gray")
        self._log("监测已停止")

    def _monitor_loop(self, event_id: int, price_id: int, qty: int, interval: int, event_if_begin: int = 1, is_select_seat: bool = False):
        count = 0
        refresh_counter = 0
        err_streak = 0
        start_time = time.time()
        while self.monitoring:
            count += 1
            refresh_counter += 1
            try:
                # 每 10 次刷新一次场次状态（检测是否开售）
                if refresh_counter >= 10 and self._sel_program_id:
                    refresh_counter = 0
                    try:
                        events, _ = self.api.get_events(self._sel_program_id)
                        for ev in events:
                            if ev["event_id"] == event_id:
                                if ev["if_begin"] != event_if_begin:
                                    self._log(f"场次状态变更: IF_BEGIN {event_if_begin} -> {ev['if_begin']}")
                                    event_if_begin = ev["if_begin"]
                                break
                    except Exception as e:
                        self._log(f"[{datetime.now().strftime('%H:%M:%S')}] 刷新场次状态失败: {e}", flush=True)

                # 每 100 次输出运行摘要
                if count % 100 == 0:
                    elapsed = time.time() - start_time
                    self._log(f"--- 运行摘要 | 已查询{count}次 | 运行{elapsed/60:.1f}分钟 | 间隔{interval}s | 连续错误{err_streak}次 ---")

                info = self.api.check_price_availability(event_id, price_id, event_if_begin)
                err_streak = 0
                ts = datetime.now().strftime("%H:%M:%S")
                if info["available"]:
                    self._log(f"[{ts}] #{count} 发现有票（余{info['seat_cnt']}张）！尝试下单...")
                    buy_result = self.api.buy_ticket(event_id, price_id, qty)
                    _log_buf.append(f"[API] buy_ticket 响应: {json.dumps(buy_result, ensure_ascii=False)}")
                    if buy_result.get("code") == 0:
                        self._log(f"[{ts}] 下单成功! 请前往购物车完成支付", flush=True)
                        self._send_notification(event_id, price_id, qty, True)
                        self.monitoring = False
                        self.root.after(0, self._stop_monitor)
                        self.root.after(0, lambda: messagebox.showinfo("下单成功",
                            "已成功下单！请尽快前往购物车完成支付。"))
                        return
                    else:
                        msg = buy_result.get("msg", "未知错误")
                        code = buy_result.get("code", "?")
                        self._log(f"[{ts}] #{count} 下单失败 code={code} - {msg}")

                        if code == 3567 and is_select_seat:
                            self._log(f"[{ts}] 该场次为选座事件，尝试 Playwright 自动选座...")
                            try:
                                seat_result = BrowserManager.select_seat_and_buy(
                                    self._sel_program_id, event_id, price_id,
                                    self._captcha_cookies, self.api.token, qty,
                                    log_callback=self._log)
                                if seat_result.get("code") == 0:
                                    self._log(f"[{ts}] 选座下单成功!", flush=True)
                                    self._send_notification(event_id, price_id, qty, True)
                                    self.monitoring = False
                                    self.root.after(0, self._stop_monitor)
                                    self.root.after(0, lambda: messagebox.showinfo("下单成功",
                                        "选座下单成功！请尽快前往购物车完成支付。"))
                                    return
                                else:
                                    self._log(f"[{ts}] 选座失败: {seat_result.get('msg')}")
                            except Exception as se:
                                self._log(f"[{ts}] 选座异常: {se}")

                        if code == 10001:
                            self._log("登录已过期，请重新登录后再监测", flush=True)
                            self.monitoring = False
                            self.root.after(0, self._stop_monitor)
                            return
                else:
                    ib = info.get("if_begin", 0)
                    if ib != 1:
                        status_map = {0: "未开票", 2: "已结束"}
                        self._log(f"[{ts}] #{count} {status_map.get(ib, '未开票/已暂停')}（IF_BEGIN={ib}）")
                    else:
                        self._log(f"[{ts}] #{count} 暂无余票（余{info['seat_cnt']}张）")
            except Exception as e:
                err_streak += 1
                self._log(f"[{datetime.now().strftime('%H:%M:%S')}] #{count} 查询异常 (连续第{err_streak}次) - {e}")
                if err_streak >= 10:
                    self._log(f"警告：连续{err_streak}次查询异常，可能存在网络问题或IP被限流", flush=True)

            time.sleep(interval)

    def _send_notification(self, event_id, price_id, qty, success):
        push_key = self.entry_push_key.get().strip()
        if not push_key:
            self._log("未配置推送Key，跳过微信通知")
            return
        method = self.push_method.get()
        title = "购票成功通知" if success else "余票监测通知"
        price_str = f"¥{self._sel_price_info['price_amt']:.0f}" if self._sel_price_info else f"ID:{price_id}"
        content = (
            f"剧目: {self._sel_program_name or '上海文化广场'}\n"
            f"场次: {self._sel_event_dt or event_id}\n"
            f"票档: {price_str}\n"
            f"数量: {qty}\n"
            f"状态: {'下单成功' if success else '发现余票'}\n"
            f"时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"\n请尽快前往购物车完成支付！"
        )
        try:
            ok, detail = False, ""
            if method == "serverchan":
                ok, detail = WeChatNotifier.send_serverchan(push_key, title, content)
            elif method == "pushplus":
                ok, detail = WeChatNotifier.send_pushplus(push_key, title, content)
            elif method == "wxpusher":
                uid = self.entry_wxpusher_uid.get().strip()
                if uid:
                    ok, detail = WeChatNotifier.send_wxpusher(push_key, [uid], title, content)
                else:
                    detail = "未配置WxPusher UID"
            if ok:
                self._log(f"微信推送已发送: {detail}")
            else:
                self._log(f"推送失败: {detail}")
        except Exception as e:
            self._log(f"推送异常: {e}")

    def _test_push(self):
        push_key = self.entry_push_key.get().strip()
        if not push_key:
            messagebox.showwarning("提示", "请输入推送Key/Token")
            return
        method = self.push_method.get()
        ok, detail = False, ""
        if method == "serverchan":
            ok, detail = WeChatNotifier.send_serverchan(push_key, "测试推送", "余票监测程序测试消息")
        elif method == "pushplus":
            ok, detail = WeChatNotifier.send_pushplus(push_key, "测试推送", "余票监测程序测试消息")
        elif method == "wxpusher":
            uid = self.entry_wxpusher_uid.get().strip()
            if not uid:
                messagebox.showwarning("提示", "请输入WxPusher UID")
                return
            # 显示参数方便排查
            param_info = f"appToken={push_key[:8]}...{push_key[-4:]}\nuid={uid}"
            ok, detail = WeChatNotifier.send_wxpusher(push_key, [uid], "测试推送", "余票监测程序测试消息")
            detail = f"{param_info}\n\n{detail}"
        if ok:
            messagebox.showinfo("成功", f"测试推送发送成功，请检查微信\n\n{detail}")
        else:
            messagebox.showerror("失败", f"推送发送失败\n\n{detail}")

    # ──────── 日志 ────────

    def _log(self, msg: str, flush: bool = False):
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        _log_buf.append(f"[{ts}] {msg}")
        if flush:
            _flush_log(msg)
        def _append():
            self.log_text.configure(state="normal")
            self.log_text.insert(tk.END, msg + "\n")
            self.log_text.see(tk.END)
            self.log_text.configure(state="disabled")
        self.root.after(0, _append)

    # ──────── 退出清理 ────────

    def _on_close(self):
        self.monitoring = False
        try:
            self.browser.stop()
        except Exception:
            pass
        self.root.destroy()

    def run(self):
        self.root.mainloop()


if __name__ == "__main__":
    app = TicketMonitorGUI()
    app.run()
