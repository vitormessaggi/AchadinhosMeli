from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sqlite3
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote, urlencode, urlparse, urlsplit
from urllib.request import Request, urlopen

from dotenv import load_dotenv
from playwright.async_api import async_playwright
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatType
from telegram.error import TelegramError
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")
DB_PATH = Path(os.getenv("DATABASE_PATH", ROOT / "data" / "meli.sqlite3"))
HOST, PORT = "127.0.0.1", int(os.getenv("PANEL_PORT", "8765"))
LOG = logging.getLogger("meli")
DOMAINS = {"mercadolivre.com.br", "mercadolivre.com", "meli.la"}
URL_RE = re.compile(r"https?://[^\s<>\"']+", re.I)
BROWSER_PROFILE = ROOT / "data" / "chrome-profile"
PLAYWRIGHT_RUNTIME: Any = None
BROWSER_CONTEXT: Any = None
BROWSER_LOCK = asyncio.Lock()
PUBLISH_LOCK = asyncio.Lock()


def timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DB_PATH, timeout=10)
    db.row_factory = sqlite3.Row
    return db


def init_db() -> None:
    with connect() as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("""CREATE TABLE IF NOT EXISTS products (
            id INTEGER PRIMARY KEY AUTOINCREMENT, url TEXT NOT NULL UNIQUE,
            title TEXT NOT NULL, price TEXT NOT NULL DEFAULT '', image_url TEXT NOT NULL DEFAULT '',
            original_price TEXT NOT NULL DEFAULT '', discount_amount TEXT NOT NULL DEFAULT '',
            discount_percent TEXT NOT NULL DEFAULT '',
            description TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'pending'
              CHECK(status IN ('pending','approved','rejected')),
            submitted_by TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""")
        columns = {row["name"] for row in db.execute("PRAGMA table_info(products)").fetchall()}
        migrations = {
            "original_price": "TEXT NOT NULL DEFAULT ''",
            "discount_amount": "TEXT NOT NULL DEFAULT ''",
            "discount_percent": "TEXT NOT NULL DEFAULT ''",
            "published_at": "TEXT NOT NULL DEFAULT ''",
            "published_chat_id": "TEXT NOT NULL DEFAULT ''",
            "published_message_id": "INTEGER",
            "whatsapp_sent_at": "TEXT NOT NULL DEFAULT ''",
            "whatsapp_group_name": "TEXT NOT NULL DEFAULT ''",
        }
        for name, declaration in migrations.items():
            if name not in columns:
                db.execute(f"ALTER TABLE products ADD COLUMN {name} {declaration}")


def save_product(p: dict[str, str], submitted_by: str) -> tuple[int, bool]:
    with connect() as db:
        old = db.execute("SELECT id FROM products WHERE url=?", (p["url"],)).fetchone()
        if old:
            db.execute("""UPDATE products SET title=?,price=?,image_url=?,original_price=?,
                discount_amount=?,discount_percent=?,description=?,
                status=CASE WHEN published_at='' THEN 'pending' ELSE status END,
                submitted_by=?,updated_at=? WHERE id=?""",
                (p["title"], p["price"], p["image_url"], p["original_price"], p["discount_amount"],
                 p["discount_percent"], p["description"], submitted_by, timestamp(), old["id"]))
            return int(old["id"]), False
        cur = db.execute("""INSERT INTO products(url,title,price,image_url,original_price,discount_amount,
            discount_percent,description,submitted_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (p["url"], p["title"], p["price"], p["image_url"], p["original_price"], p["discount_amount"],
             p["discount_percent"], p["description"], submitted_by, timestamp(), timestamp()))
        return int(cur.lastrowid), True


def get_products(status: str | None = None) -> list[dict[str, Any]]:
    with connect() as db:
        if status in {"pending", "approved", "rejected"}:
            rows = db.execute("SELECT * FROM products WHERE status=? ORDER BY created_at DESC", (status,)).fetchall()
        else:
            rows = db.execute("SELECT * FROM products ORDER BY created_at DESC LIMIT 200").fetchall()
        return [dict(row) for row in rows]


def get_product(product_id: int) -> dict[str, Any] | None:
    with connect() as db:
        row = db.execute("SELECT * FROM products WHERE id=?", (product_id,)).fetchone()
        return dict(row) if row else None


def mark_whatsapp_published(product_id: int, group_name: str) -> bool:
    with connect() as db:
        cur = db.execute("""UPDATE products SET whatsapp_sent_at=?,whatsapp_group_name=?,updated_at=?
            WHERE id=? AND status='pending' AND whatsapp_sent_at=''""",
            (timestamp(), group_name, timestamp(), product_id))
        return cur.rowcount == 1


def mark_telegram_published(product_id: int, chat_id: int, message_id: int) -> bool:
    with connect() as db:
        cur = db.execute("""UPDATE products SET published_at=?,published_chat_id=?,published_message_id=?,updated_at=?
            WHERE id=? AND status='pending' AND published_at=''""",
            (timestamp(), str(chat_id), message_id, timestamp(), product_id))
        return cur.rowcount == 1


def finalize_approval(product_id: int, require_telegram: bool, require_whatsapp: bool) -> bool:
    with connect() as db:
        cur = db.execute("""UPDATE products SET status='approved',updated_at=? WHERE id=? AND status='pending'
            AND (published_at<>'' OR whatsapp_sent_at<>'')
            AND (?=0 OR published_at<>'') AND (?=0 OR whatsapp_sent_at<>'')""",
            (timestamp(), product_id, int(require_telegram), int(require_whatsapp)))
        return cur.rowcount == 1


def update_status(product_id: int, status: str) -> bool:
    if status not in {"pending", "approved", "rejected"}:
        return False
    with connect() as db:
        return db.execute("UPDATE products SET status=?,updated_at=? WHERE id=?", (status, timestamp(), product_id)).rowcount == 1


def allowed_url(url: str) -> bool:
    try:
        p = urlparse(url)
        host = (p.hostname or "").lower().rstrip(".")
        return p.scheme == "https" and any(host == d or host.endswith("." + d) for d in DOMAINS)
    except ValueError:
        return False


async def get_browser_context() -> Any:
    global PLAYWRIGHT_RUNTIME, BROWSER_CONTEXT
    if BROWSER_CONTEXT is None:
        BROWSER_PROFILE.mkdir(parents=True, exist_ok=True)
        PLAYWRIGHT_RUNTIME = await async_playwright().start()
        try:
            BROWSER_CONTEXT = await PLAYWRIGHT_RUNTIME.chromium.launch_persistent_context(
                user_data_dir=str(BROWSER_PROFILE), channel="chrome", headless=False,
                locale="pt-BR", args=["--start-maximized"],
            )
        except Exception as exc:
            await PLAYWRIGHT_RUNTIME.stop()
            PLAYWRIGHT_RUNTIME = None
            raise ValueError("Não consegui abrir o Google Chrome. Verifique se ele está instalado e tente novamente.") from exc
    return BROWSER_CONTEXT


async def read_product_data(page: Any) -> dict[str, str]:
    data = await page.evaluate("""() => {
      const meta = ss => { for (const s of ss) { const e=document.querySelector(s); if(e?.content) return e.content.trim(); } return ''; };
      const text = ss => { for (const s of ss) { const e=document.querySelector(s); if(e?.innerText) return e.innerText.trim(); } return ''; };
      let product=null;
      const walk=x=>{ if(Array.isArray(x)){for(const y of x){const r=walk(y);if(r)return r;}}
        if(x&&typeof x==='object'){if(x['@graph']){const r=walk(x['@graph']);if(r)return r;}
          const t=Array.isArray(x['@type'])?x['@type']:[x['@type']];if(t.some(v=>String(v).toLowerCase()==='product'))return x;} return null;};
      for(const s of document.querySelectorAll('script[type="application/ld+json"]')){try{product=walk(JSON.parse(s.textContent));if(product)break;}catch(_){}}
      const offer=Array.isArray(product?.offers)?product.offers[0]:(product?.offers||{}), img=Array.isArray(product?.image)?product.image[0]:product?.image;
      // Prefer the amounts actually shown beside the product title. Mercado Livre
      // changes these classes often, so inspect the title's nearby containers too.
      const visible=e=>!!(e && (e.getClientRects().length || getComputedStyle(e).display!=='none'));
      const titleNode=document.querySelector('h1')||document.querySelector('[data-testid*="title"]');
      const amountSelectors='.andes-money-amount, .price-tag-amount, [class*="money-amount"]';
      const amountText=e=>{
        const fraction=e.querySelector('.andes-money-amount__fraction');
        const cents=e.querySelector('.andes-money-amount__cents');
        if(fraction) return `R$ ${fraction.textContent.trim()}${cents?.textContent.trim()?','+cents.textContent.trim():''}`;
        return (e.innerText||e.textContent||'').replace(/\\s+/g,' ').trim();
      };
      let scope=titleNode, uiAmounts=[];
      for(let depth=0;scope&&depth<7;depth++,scope=scope.parentElement){
        const found=[...scope.querySelectorAll(amountSelectors)].filter(visible);
        if(found.length){uiAmounts=found;break;}
      }
      let uiCurrent='', uiOriginal='';
      // In product cards the title often is not an h1. Use the explicit price
      // component from the card markup as a fallback (and prefer its labels).
      const priceCard=[...document.querySelectorAll('.poly-component__price')].find(visible);
      if(priceCard){
        const previous=priceCard.querySelector('.poly-price__previous');
        const current=priceCard.querySelector('.poly-price__amount, .poly-price__current .andes-money-amount');
        if(previous) uiOriginal=amountText(previous);
        if(current) uiCurrent=amountText(current);
      }
      const isOld=e=>/previous|original|old-price|original-price/i.test(`${e.className||''} ${e.parentElement?.className||''} ${e.getAttribute('aria-label')||''}`)
        || !!e.closest('.ui-pdp-price__original-value, .andes-money-amount--previous, [class*="original-price"], [class*="old-price"]');
      const oldAmount=uiAmounts.find(isOld);
      if(oldAmount&&!uiOriginal) uiOriginal=amountText(oldAmount);
      const normalAmounts=uiAmounts.filter(e=>!isOld(e));
      if(normalAmounts.length&&!uiCurrent) uiCurrent=amountText(normalAmounts[0]);
      const uiDiscount=text(['.ui-pdp-price__discount','.andes-money-amount__discount','.poly-price__discount','[class*="price__discount"]']);
      if(!uiOriginal && uiAmounts.length>=2 && /\\d{1,2}(?:[,.]\\d+)?\\s*%\\s*OFF/i.test(document.body.innerText||'')){
        uiOriginal=amountText(uiAmounts[0]); uiCurrent=amountText(uiAmounts[uiAmounts.length-1]);
      }
      return {title:product?.name||meta(['meta[property="og:title"]','meta[name="title"]'])||document.querySelector('h1')?.innerText?.trim()||'',
        price:offer?.price||meta(['meta[property="product:price:amount"]']), currency:offer?.priceCurrency||meta(['meta[property="product:price:currency"]']),
        uiPrice:uiCurrent, uiOriginalPrice:uiOriginal,
        originalPrice:meta(['meta[property="product:original_price:amount"]'])||text(['.ui-pdp-price__original-value','.andes-money-amount--previous','.price-tag-amount__old-price']),
        discount:uiDiscount||text(['.price-tag-amount__discount']),
        image:(typeof img==='string'?img:img?.url)||meta(['meta[property="og:image"]']),
        description:product?.description||meta(['meta[property="og:description"]','meta[name="description"]'])};
    }""")
    title = re.sub(r"\s+", " ", str(data.get("title", ""))).strip()
    lowered = title.casefold()
    if not title or any(marker in lowered for marker in ("access denied", "captcha", "não foi possível", "nao foi possivel", "página não encontrada", "pagina nao encontrada")):
        return {}
    price = str(data.get("uiPrice") or data.get("price", "")).strip()
    currency = str(data.get("currency", "")).upper()
    current_value = parse_amount(price)
    if current_value is not None:
        price = format_brl(current_value) if currency in {"", "BRL"} else f"{currency} {current_value:.2f}"
    original_value = parse_amount(str(data.get("uiOriginalPrice") or data.get("originalPrice", "")))
    discount_text = str(data.get("discount", ""))
    percent_match = re.search(r"(\d{1,2}(?:[,.]\d+)?)\s*%", discount_text)
    discount_percent = float(percent_match.group(1).replace(",", ".")) if percent_match else None
    if current_value is not None:
        if original_value is not None and original_value > current_value:
            discount_value = original_value - current_value
            discount_percent = discount_value / original_value * 100
        elif discount_percent and 0 < discount_percent < 100:
            discount_value = current_value * discount_percent / (100 - discount_percent)
            original_value = current_value + discount_value
        else:
            discount_value = 0.0
            original_value = 0.0
    else:
        discount_value = 0.0
        original_value = 0.0
    description = re.sub(r"\s+", " ", str(data.get("description", ""))).strip()
    return {"title": title[:300], "price": price[:80], "image_url": str(data.get("image", ""))[:1000],
            "original_price": format_brl(original_value) if original_value else "",
            "discount_amount": format_brl(discount_value) if discount_value else "",
            "discount_percent": f"{discount_percent:.0f}%" if discount_value and discount_percent else "",
            "description": description[:1000]}


def parse_amount(value: str) -> float | None:
    text = re.sub(r"[^\d,.]", "", str(value))
    if not text:
        return None
    if "," in text and "." in text:
        if text.rfind(",") > text.rfind("."):
            text = text.replace(".", "").replace(",", ".")
        else:
            text = text.replace(",", "")
    elif "," in text:
        text = text.replace(".", "").replace(",", ".")
    elif text.count(".") > 1:
        head, tail = text.rsplit(".", 1)
        text = head.replace(".", "") + "." + tail
    try:
        return float(text)
    except ValueError:
        return None


def format_brl(amount: float) -> str:
    return "R$ " + f"{amount:,.2f}".replace(",", "#").replace(".", ",").replace("#", ".")


async def extract_product(url: str, notify: Any = None) -> dict[str, str]:
    if not allowed_url(url):
        raise ValueError("Envie um link HTTPS do Mercado Livre (mercadolivre.com.br ou meli.la).")
    async with BROWSER_LOCK:
        context = await get_browser_context()
        page = await context.new_page()
        page.set_default_navigation_timeout(45000)
        try:
            response = await page.goto(url, wait_until="domcontentloaded", timeout=45000)
            if not allowed_url(page.url):
                raise ValueError("O link redirecionou para fora do Mercado Livre.")
            data = await read_product_data(page)
            status = response.status if response else None
            if not data or (status is not None and status >= 400):
                if notify:
                    await notify("O anúncio abriu no Chrome. Se aparecer login ou verificação do Mercado Livre, conclua na janela; vou aguardar até 2 minutos.")
                deadline = asyncio.get_running_loop().time() + 120
                while asyncio.get_running_loop().time() < deadline:
                    await asyncio.sleep(2)
                    if not allowed_url(page.url):
                        raise ValueError("O navegador saiu do domínio do Mercado Livre.")
                    data = await read_product_data(page)
                    if data:
                        break
                if not data:
                    suffix = f" (HTTP {status})" if status else ""
                    raise ValueError(f"Não consegui ler o anúncio{suffix}. O Chrome ficou aberto para você conferir; tente enviar o link novamente após resolver a verificação.")
            data["url"] = url
            return data
        except Exception:
            # A janela e a aba ficam abertas para permitir a conferência manual.
            raise


async def close_browser(application: Application) -> None:
    global PLAYWRIGHT_RUNTIME, BROWSER_CONTEXT
    if BROWSER_CONTEXT is not None:
        await BROWSER_CONTEXT.close()
        BROWSER_CONTEXT = None
    if PLAYWRIGHT_RUNTIME is not None:
        await PLAYWRIGHT_RUNTIME.stop()
        PLAYWRIGHT_RUNTIME = None


def _waha_request(method: str, path: str, payload: dict[str, Any] | None = None) -> Any:
    base_url = os.getenv("WAHA_BASE_URL", "http://127.0.0.1:3000").strip().rstrip("/")
    api_key = os.getenv("WAHA_API_KEY", "").strip()
    if not api_key:
        raise ValueError("WAHA_API_KEY não está configurada no .env.")
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = Request(f"{base_url}{path}", data=body, method=method,
                      headers={"X-Api-Key": api_key, "Accept": "application/json",
                               "Content-Type": "application/json"})
    try:
        with urlopen(request, timeout=25) as response:
            raw = response.read().decode("utf-8", errors="replace")
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:300]
        raise ValueError(f"WAHA respondeu HTTP {exc.code}: {detail}") from exc
    except URLError as exc:
        raise ValueError("Não consegui conectar ao WAHA. Confirme se o container está ligado e se WAHA_BASE_URL está correto.") from exc
    if not raw.strip():
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {"response": raw[:300]}


def _normalize_group_name(value: str) -> str:
    return " ".join(value.casefold().split())


def _find_waha_group(group_name: str, session: str) -> tuple[str, str]:
    target = _normalize_group_name(group_name)
    if not target:
        raise ValueError("Configure WHATSAPP_GROUP_ID ou WHATSAPP_GROUP_NAME no .env.")
    matches: list[tuple[str, str]] = []
    partials: list[tuple[str, str]] = []
    base = f"/api/{quote(session, safe='')}/groups"
    for offset in range(0, 10000, 100):
        query = urlencode({"limit": 100, "offset": offset, "exclude": "participants"})
        groups = _waha_request("GET", f"{base}?{query}")
        if isinstance(groups, dict):
            groups = groups.get("groups", groups.get("data", groups.get("items", [])))
        if not isinstance(groups, list):
            raise ValueError("WAHA retornou uma lista de grupos em formato inesperado.")
        for group in groups:
            if not isinstance(group, dict):
                continue
            name = str(group.get("subject") or group.get("name") or group.get("title") or "").strip()
            group_id = str(group.get("id") or group.get("chatId") or "").strip()
            if not name or not group_id:
                continue
            normalized = _normalize_group_name(name)
            if normalized == target:
                matches.append((group_id, name))
            elif target in normalized or normalized in target:
                partials.append((group_id, name))
        if len(groups) < 100:
            break
    found = matches if matches else partials
    unique = {group_id: name for group_id, name in found}
    if len(unique) == 1:
        group_id, name = next(iter(unique.items()))
        return group_id, name
    if not unique:
        raise ValueError(f"WAHA não encontrou o grupo '{group_name}'. Confira o nome em WHATSAPP_GROUP_NAME ou copie o ID do grupo no painel do WAHA.")
    names = ", ".join(repr(name) for name in unique.values())
    raise ValueError(f"Há mais de um grupo correspondente a '{group_name}': {names}. Configure WHATSAPP_GROUP_ID para selecionar o destino sem ambiguidade.")


async def send_whatsapp_group_message(product: dict[str, Any], group_name: str = "",
                                      group_id: str = "", notify: Any = None) -> str:
    session = os.getenv("WAHA_SESSION", "default").strip() or "default"
    if notify:
        await notify("Enviando ao WhatsApp pela API local…")
    if not group_id:
        group_id, group_name = await asyncio.to_thread(_find_waha_group, group_name, session)
    payload = {"session": session, "chatId": group_id, "text": product_approval_text(product)}
    await asyncio.to_thread(_waha_request, "POST", "/api/sendText", payload)
    return group_name or group_id


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat and update.effective_chat.type == ChatType.PRIVATE:
        await update.effective_message.reply_text("Olá! Envie um link de produto do Mercado Livre. Vou abrir no Chrome e retornar os dados com botões de aprovação. Ao aprovar, publico a mensagem no grupo configurado. Use /ajuda para instruções.")


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat and update.effective_chat.type == ChatType.PRIVATE:
        await update.effective_message.reply_text("Envie um link HTTPS de produto do Mercado Livre. O Chrome abre visivelmente; faça login ou resolva uma verificação se aparecer.\n/pendentes — quantidade aguardando aprovação\n/idgrupo — mostrar o ID deste grupo\n/ajuda — instruções\n\nAprovar publica a mensagem no grupo configurado; rejeitar não envia nada.")


async def pending(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat and update.effective_chat.type == ChatType.PRIVATE:
        await update.effective_message.reply_text(f"{len(get_products('pending'))} produto(s) aguardando aprovação no Telegram.")


async def group_id_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    if chat and chat.type in {ChatType.GROUP, ChatType.SUPERGROUP}:
        await update.effective_message.reply_text(f"ID deste grupo: {chat.id}")


def product_approval_text(product: dict[str, str]) -> str:
    discount = product.get("discount_amount") or "Não identificado"
    if product.get("discount_percent"):
        discount += f" ({product['discount_percent']})"
    return (f"🛍️ {product['title']}\n\n"
            f"💰 Valor atual: {product.get('price') or 'Não informado'}\n"
            f"🏷️ Valor de desconto: {discount}\n\n"
            f"🔗 {product['url']}")


def approval_keyboard(product_id: int, allow_reject: bool = True) -> InlineKeyboardMarkup:
    buttons = [InlineKeyboardButton("✅ Aprovar", callback_data=f"approve:{product_id}")]
    if allow_reject:
        buttons.append(InlineKeyboardButton("❌ Rejeitar", callback_data=f"reject:{product_id}"))
    return InlineKeyboardMarkup([buttons])


async def handle_approval(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.data or not query.message:
        return
    if not update.effective_chat or update.effective_chat.type != ChatType.PRIVATE:
        await query.answer("A aprovação só funciona na conversa privada com o bot.", show_alert=True)
        return
    match = re.fullmatch(r"(approve|reject):(\d+)", query.data)
    if not match:
        await query.answer("Ação inválida.", show_alert=True)
        return
    action, raw_id = match.groups()
    product_id = int(raw_id)
    if action == "reject":
        product = get_product(product_id)
        if not product or product["status"] != "pending" or not update_status(product_id, "rejected"):
            await query.answer("Esse produto não está mais disponível.", show_alert=True)
            return
        await query.answer("Produto rejeitado.")
        await query.edit_message_text(f"{query.message.text or ''}\n\n❌ Rejeitado.", reply_markup=None, disable_web_page_preview=True)
        return

    raw_chat_id = os.getenv("TELEGRAM_GROUP_CHAT_ID", "").strip()
    whatsapp_group = os.getenv("WHATSAPP_GROUP_NAME", "").strip()
    whatsapp_group_id = os.getenv("WHATSAPP_GROUP_ID", "").strip()
    whatsapp_configured = bool(whatsapp_group_id or whatsapp_group)
    if not raw_chat_id and not whatsapp_configured:
        await query.answer("Configure ao menos TELEGRAM_GROUP_CHAT_ID ou o destino do WhatsApp no .env.", show_alert=True)
        return
    group_id: int | None = None
    if raw_chat_id:
        try:
            group_id = int(raw_chat_id)
        except ValueError:
            await query.answer("TELEGRAM_GROUP_CHAT_ID precisa ser um número de grupo válido.", show_alert=True)
            return

    async with PUBLISH_LOCK:
        product = get_product(product_id)
        if not product or product["status"] != "pending":
            await query.answer("Esse anúncio já foi decidido ou publicado.", show_alert=True)
            return
        await query.answer("Aprovando. Vou tentar cada canal separadamente.")
        failures: list[str] = []

        # Send to Telegram first so WhatsApp connection/search problems cannot
        # prevent the Telegram publication.
        if group_id is not None and not product.get("published_at"):
            try:
                posted = await context.bot.send_message(
                    chat_id=group_id,
                    text=product_approval_text(product),
                    disable_web_page_preview=False,
                )
                if not mark_telegram_published(product_id, group_id, posted.message_id):
                    failures.append("Telegram enviou, mas o banco não confirmou o registro; confira o grupo antes de repetir.")
            except TelegramError as exc:
                LOG.warning("Não foi possível publicar o produto %s no Telegram (%s).", product_id, type(exc).__name__)
                failures.append(f"Telegram: {str(exc)[:180]}")
            product = get_product(product_id) or product

        # WhatsApp is attempted even if Telegram failed, and vice versa.
        if whatsapp_configured and not product.get("whatsapp_sent_at"):
            async def whatsapp_progress(text: str) -> None:
                await query.message.reply_text(text)
            try:
                sent_group = await send_whatsapp_group_message(
                    product, group_name=whatsapp_group, group_id=whatsapp_group_id, notify=whatsapp_progress)
                if not mark_whatsapp_published(product_id, sent_group):
                    failures.append("WhatsApp enviou, mas o banco não confirmou o registro; confira o grupo antes de repetir.")
            except Exception as exc:
                LOG.warning("Não foi possível publicar o produto %s no WhatsApp (%s).", product_id, type(exc).__name__)
                failures.append(f"WhatsApp: {str(exc)[:220]}")
            product = get_product(product_id) or product

        complete = finalize_approval(product_id, require_telegram=group_id is not None,
                                     require_whatsapp=whatsapp_configured)
        product = get_product(product_id) or product
        if not complete:
            details = "\n".join(f"• {failure}" for failure in failures)
            if not details:
                details = "• Ainda falta concluir um dos canais configurados."
            if product.get("published_at") or product.get("whatsapp_sent_at"):
                await query.edit_message_reply_markup(reply_markup=approval_keyboard(product_id, allow_reject=False))
            await query.message.reply_text(
                f"Envio parcial. O que já foi enviado ficará registrado e não será repetido.\n{details}\n\n"
                "Corrija o canal com falha e toque em Aprovar novamente para tentar somente o que falta."
            )
            return

        channels = []
        if product.get("published_at"):
            channels.append("Telegram")
        if product.get("whatsapp_sent_at"):
            channels.append("WhatsApp")
        skipped = " (WhatsApp não configurado)" if not whatsapp_configured else ""
        await query.edit_message_text(
            f"{product_approval_text(product)}\n\n✅ Publicado em: {', '.join(channels)}.{skipped}",
            reply_markup=None, disable_web_page_preview=True)


async def handle_link(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg, chat = update.effective_message, update.effective_chat
    if not msg or not chat or chat.type != ChatType.PRIVATE or not msg.text:
        return
    found = URL_RE.search(msg.text)
    if not found:
        await msg.reply_text("Envie um link HTTPS de um produto do Mercado Livre.")
        return
    url = found.group(0).rstrip(".,);]}")
    if not allowed_url(url):
        await msg.reply_text("Esse link não parece ser do Mercado Livre. Envie um link HTTPS mercadolivre.com.br ou meli.la.")
        return
    await msg.reply_text("Abrindo o anúncio no Chrome para consultar os dados…")
    try:
        async def notify_browser(text: str) -> None:
            await msg.reply_text(text)
        product = await extract_product(url, notify_browser)
        user = update.effective_user
        who = f"{user.full_name} (@{user.username})" if user and user.username else (user.full_name if user else "Telegram")
        item_id, _ = save_product(product, who)
        saved = get_product(item_id)
        if saved and saved.get("published_at"):
            await msg.reply_text(f"{product_approval_text(product)}\n\n✅ Este anúncio já foi publicado no grupo.", disable_web_page_preview=True)
        else:
            await msg.reply_text(product_approval_text(product), reply_markup=approval_keyboard(item_id), disable_web_page_preview=True)
    except Exception as exc:
        LOG.warning("Falha na extração do anúncio: %s", exc)
        await msg.reply_text(f"Não consegui extrair esse anúncio: {str(exc)[:250]}")


PANEL = r'''<!doctype html><html lang="pt-BR"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Meli — Aprovação</title><style>
:root{font:16px/1.5 system-ui,sans-serif;color:#18202b;background:#f4f6f8}*{box-sizing:border-box}body{max-width:1100px;margin:36px auto;padding:0 18px}h1{margin-bottom:4px}.note{color:#536273;margin-top:0}.toolbar{display:flex;gap:8px;margin:20px 0;flex-wrap:wrap}.toolbar button,.actions button,.copybtn{border:0;border-radius:8px;padding:9px 14px;background:#dce5f2;color:#172337;cursor:pointer}.toolbar button.active,.actions .approve{background:#1464d2;color:white}.card{background:white;border:1px solid #dce2e9;border-radius:12px;padding:18px;margin:12px 0;display:grid;grid-template-columns:120px 1fr;gap:18px}.card img{width:120px;height:120px;object-fit:contain;background:#f7f8fa;border-radius:8px}.card h2{font-size:18px;margin:0 0 6px}.price{font-weight:700}.muted{color:#657487;font-size:14px}.actions{display:none!important;gap:8px;margin:12px 0}.actions .reject{background:#f6e2e2;color:#8b2424}.copy{white-space:pre-wrap;background:#f5f7fa;padding:10px;border-radius:8px;margin:10px 0;font-size:14px}.empty{padding:28px;color:#596879;background:white;border-radius:12px}@media(max-width:600px){.card{grid-template-columns:1fr}.card img{width:100%;height:160px}}
</style></head><body><h1>Produtos recebidos</h1><p class="note">A aprovação e a rejeição são feitas nos botões do Telegram. Este painel é apenas para consulta.</p><div class="toolbar"><button data-filter="pending" class="active">Pendentes</button><button data-filter="approved">Aprovados</button><button data-filter="rejected">Rejeitados</button><button data-filter="all">Todos</button></div><main id="items"></main><script>
let current='pending';const esc=s=>String(s||'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
async function load(){const rows=await fetch('/api/products?status='+current).then(r=>r.json()),root=document.querySelector('#items');if(!rows.length){root.innerHTML='<div class="empty">Nenhum produto nesta lista.</div>';return}root.innerHTML=rows.map(p=>`<article class="card"><div>${p.image_url?`<img src="${esc(p.image_url)}" alt="">`:''}</div><div><h2>${esc(p.title)}</h2>${p.price?`<div class="price">${esc(p.price)}</div>`:''}<p class="muted">Recebido ${esc(new Date(p.created_at).toLocaleString('pt-BR'))} · ${esc(p.submitted_by)}</p><p>${esc(p.description)}</p><p><a href="${esc(p.url)}" target="_blank" rel="noopener">Abrir anúncio</a></p>${p.status==='pending'?`<div class="actions"><button class="approve" onclick="act(${p.id},'approved')">Aprovar</button><button class="reject" onclick="act(${p.id},'rejected')">Rejeitar</button></div>`:`<p class="muted">Status: ${esc(p.status)}</p><div class="actions"><button onclick="act(${p.id},'pending')">Voltar para pendentes</button></div>`}<div class="copy"><strong>Texto para copiar:</strong><br>${esc(p.title)}${p.price?' — '+esc(p.price):''}<br>${esc(p.url)}</div><button class="copybtn" onclick="copyText(this)" data-text="${esc(p.title+(p.price?' — '+p.price:'')+'\n'+p.url)}">Copiar texto</button></div></article>`).join('')}
async function act(id,status){const r=await fetch('/api/products/'+id,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({status})});if(!r.ok){alert('Não foi possível atualizar o status.');return}load()}async function copyText(b){await navigator.clipboard.writeText(b.dataset.text);b.textContent='Copiado'}document.querySelectorAll('[data-filter]').forEach(b=>b.onclick=()=>{document.querySelector('.active')?.classList.remove('active');b.classList.add('active');current=b.dataset.filter;load()});load();
</script></body></html>'''


class Handler(BaseHTTPRequestHandler):
    def local(self) -> bool:
        host = self.headers.get("Host", "").split(":", 1)[0].lower()
        return host in {"127.0.0.1", "localhost", "[::1]"} and self.client_address[0] in {"127.0.0.1", "::1"}

    def send_data(self, status: int, body: bytes, kind: str = "application/json; charset=utf-8") -> None:
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy", "default-src 'self' https:; img-src 'self' https: data:; style-src 'unsafe-inline'; script-src 'unsafe-inline'; object-src 'none'; base-uri 'none'")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if not self.local():
            self.send_data(403, b'{"error":"forbidden"}'); return
        if self.path == "/":
            self.send_data(200, PANEL.encode(), "text/html; charset=utf-8")
        elif self.path.startswith("/api/products"):
            status = parse_qs(urlsplit(self.path).query).get("status", [None])[0]
            self.send_data(200, json.dumps(get_products(status), ensure_ascii=False).encode())
        else:
            self.send_data(404, b'{"error":"not found"}')

    def do_POST(self) -> None:
        if not self.local():
            self.send_data(403, b'{"error":"forbidden"}'); return
        origin = self.headers.get("Origin")
        if origin and urlparse(origin).hostname not in {"127.0.0.1", "localhost", "::1"}:
            self.send_data(403, b'{"error":"invalid origin"}'); return
        m = re.fullmatch(r"/api/products/(\d+)", self.path)
        if not m:
            self.send_data(404, b'{"error":"not found"}'); return
        try:
            n = min(int(self.headers.get("Content-Length", "0")), 2000)
            status = json.loads(self.rfile.read(n) or b"{}").get("status")
            ok = update_status(int(m.group(1)), status)
            self.send_data(200 if ok else 404, b'{"ok":true}' if ok else b'{"error":"not found"}')
        except (ValueError, TypeError, json.JSONDecodeError):
            self.send_data(400, b'{"error":"invalid request"}')

    def log_message(self, fmt: str, *args: Any) -> None:
        LOG.info("painel: " + fmt, *args)


def main() -> None:
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    load_dotenv(ROOT / ".env")
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise SystemExit("Configure TELEGRAM_BOT_TOKEN no arquivo .env antes de iniciar.")
    init_db()
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    threading.Thread(target=server.serve_forever, name="meli-panel", daemon=True).start()
    app = Application.builder().token(token).post_shutdown(close_browser).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("ajuda", help_cmd))
    app.add_handler(CommandHandler("pendentes", pending))
    app.add_handler(CommandHandler("idgrupo", group_id_command))
    app.add_handler(CallbackQueryHandler(handle_approval, pattern=r"^(approve|reject):\d+$"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_link))
    LOG.info("Chrome visível sob demanda | painel em http://%s:%s | divulgação automática desativada", HOST, PORT)
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
