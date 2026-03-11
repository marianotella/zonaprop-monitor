#!/usr/bin/env python3
"""
Zonaprop Server Monitor
- Lee múltiples URLs desde config.json
- Consulta todas en paralelo
- Manda email cuando aparecen listings nuevos
- Guarda estado por búsqueda en data/
"""

import json
import re
import smtplib
import sys
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

try:
    from bs4 import BeautifulSoup
except ImportError:
    import subprocess
    subprocess.run([sys.executable, "-m", "pip", "install", "beautifulsoup4"], check=True)
    from bs4 import BeautifulSoup

try:
    from curl_cffi import requests as cf_requests
    CURL_CFFI_OK = True
except ImportError:
    CURL_CFFI_OK = False

# ─── Paths ────────────────────────────────────────────────────────────────────

BASE_DIR   = Path(__file__).parent
CONFIG     = BASE_DIR / "config.json"
DATA_DIR   = BASE_DIR / "data"
LOG_FILE   = BASE_DIR / "monitor.log"

DATA_DIR.mkdir(exist_ok=True)

# ─── Logging ──────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(sys.stdout),
    ]
)
log = logging.getLogger(__name__)

# ─── Config ───────────────────────────────────────────────────────────────────

def load_config():
    if not CONFIG.exists():
        log.error(f"No se encontró {CONFIG}. Copiá config.json.example y completalo.")
        sys.exit(1)
    with open(CONFIG) as f:
        return json.load(f)

# ─── Estado por búsqueda ──────────────────────────────────────────────────────

def state_file(monitor_name: str) -> Path:
    safe = re.sub(r"[^\w\-]", "_", monitor_name)
    return DATA_DIR / f"{safe}.json"

def load_seen(monitor_name: str) -> dict:
    f = state_file(monitor_name)
    if f.exists():
        with open(f) as fp:
            return json.load(fp)
    return {}

def save_seen(monitor_name: str, data: dict):
    with open(state_file(monitor_name), "w") as fp:
        json.dump(data, fp, ensure_ascii=False, indent=2)

# ─── Scraping ─────────────────────────────────────────────────────────────────

def _parse_html(html: str, url: str) -> list:
    """Extrae listings del HTML de la página."""
    # ── Método 1: __NEXT_DATA__ ───────────────────────────────────────────────
    match = re.search(
        r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
        html, re.DOTALL
    )
    if match:
        try:
            data = json.loads(match.group(1))
            postings = _find_postings(data)
            if postings:
                return postings
        except Exception as e:
            log.warning(f"  __NEXT_DATA__ parse error: {e}")

    # ── Método 2: HTML clásico ────────────────────────────────────────────────
    soup = BeautifulSoup(html, "html.parser")
    cards = (soup.find_all(attrs={"data-id": True}) or
             soup.find_all(attrs={"data-posting-id": True}) or
             soup.find_all("div", class_=re.compile(r"posting|property|listing", re.I)))

    listings = []
    for card in cards:
        lid = card.get("data-id") or card.get("data-posting-id") or card.get("id", "")
        if not lid:
            continue
        title_el = card.find(class_=re.compile(r"title|address|location", re.I))
        price_el = card.find(class_=re.compile(r"price|valor", re.I))
        link_el  = card.find("a", href=True)
        link = link_el["href"] if link_el else ""
        if link and not link.startswith("http"):
            link = "https://www.zonaprop.com.ar" + link
        listings.append({
            "id":    str(lid),
            "title": title_el.get_text(strip=True)[:100] if title_el else "",
            "price": price_el.get_text(strip=True)[:60]  if price_el else "",
            "url":   link,
        })
    return listings


def fetch_listings(url: str) -> list:
    """
    Obtiene listings usando curl_cffi, que imita el TLS fingerprint
    exacto de Chrome para evitar el bloqueo de Cloudflare.
    """
    if not CURL_CFFI_OK:
        raise RuntimeError("curl_cffi no está instalado. Corré: pip install curl_cffi")

    resp = cf_requests.get(
        url,
        impersonate="chrome120",
        headers={"Accept-Language": "es-AR,es;q=0.9"},
        timeout=30,
    )
    resp.raise_for_status()

    # Guardar HTML para debug
    debug_file = DATA_DIR / "debug_last_page.html"
    with open(debug_file, "w", encoding="utf-8") as f:
        f.write(resp.text)
    log.info(f"  Respuesta: {resp.status_code} ({len(resp.text)} chars)")

    return _parse_html(resp.text, url)


def _find_postings(data, depth=0):
    if depth > 10:
        return []
    if isinstance(data, dict):
        for key in ("listPostings", "postings", "results", "items", "data"):
            if key in data and isinstance(data[key], list):
                p = _parse_list(data[key])
                if p:
                    return p
        for v in data.values():
            r = _find_postings(v, depth + 1)
            if r:
                return r
    elif isinstance(data, list):
        p = _parse_list(data)
        if p:
            return p
        for item in data:
            r = _find_postings(item, depth + 1)
            if r:
                return r
    return []


def _parse_list(items):
    if not items or not isinstance(items[0], dict):
        return []
    first = items[0]
    if not any(k in first for k in ("id", "postingId", "posting_id", "propertyId")):
        return []
    results = []
    for item in items:
        pid = (item.get("id") or item.get("postingId") or
               item.get("posting_id") or item.get("propertyId") or "")
        title = (item.get("title") or item.get("address") or
                 item.get("fullAddress") or item.get("location") or "")
        price_data = item.get("priceOperationTypes") or item.get("price") or {}
        if isinstance(price_data, list) and price_data:
            price_data = price_data[0]
        price = ""
        if isinstance(price_data, dict):
            prices = price_data.get("prices") or []
            if prices:
                p = prices[0]
                price = f"{p.get('currency','')} {p.get('amount','')}".strip()
        elif isinstance(price_data, (str, int, float)):
            price = str(price_data)
        url = item.get("url") or item.get("link") or item.get("permalink") or ""
        if url and not url.startswith("http"):
            url = "https://www.zonaprop.com.ar" + url
        if pid:
            results.append({
                "id":    str(pid),
                "title": title[:100],
                "price": price[:60],
                "url":   url,
            })
    return results

# ─── Email ────────────────────────────────────────────────────────────────────

def send_email(smtp_cfg: dict, to: str, monitor_name: str, new_listings: list, search_url: str):
    count = len(new_listings)
    subject = f"🏠 {count} nuevo{'s' if count > 1 else ''} depto{'s' if count > 1 else ''} — {monitor_name}"

    # ── HTML body ─────────────────────────────────────────────────────────────
    rows = ""
    for l in new_listings:
        title = l["title"] or f"Propiedad {l['id']}"
        price = l["price"] or "Precio no disponible"
        url   = l["url"] or search_url
        rows += f"""
        <tr>
          <td style="padding:12px 8px; border-bottom:1px solid #eee;">
            <a href="{url}" style="font-weight:600; color:#1a1a2e; text-decoration:none; font-size:15px;">
              {title}
            </a><br>
            <span style="color:#e63946; font-weight:700; font-size:14px;">{price}</span>
          </td>
          <td style="padding:12px 8px; border-bottom:1px solid #eee; text-align:right; white-space:nowrap;">
            <a href="{url}"
               style="background:#e63946; color:#fff; padding:6px 14px; border-radius:6px;
                      text-decoration:none; font-size:13px; font-weight:600;">
              Ver depto →
            </a>
          </td>
        </tr>"""

    html = f"""
<!DOCTYPE html>
<html>
<body style="margin:0; padding:0; background:#f5f5f5; font-family:Arial,sans-serif;">
  <div style="max-width:600px; margin:32px auto; background:#fff; border-radius:12px;
              box-shadow:0 2px 8px rgba(0,0,0,0.08); overflow:hidden;">

    <div style="background:#1a1a2e; padding:24px 32px;">
      <h1 style="margin:0; color:#fff; font-size:20px;">🏠 Zonaprop Monitor</h1>
      <p style="margin:6px 0 0; color:#aaa; font-size:14px;">{monitor_name}</p>
    </div>

    <div style="padding:24px 32px;">
      <p style="margin:0 0 16px; color:#333; font-size:15px;">
        Aparecieron <strong>{count} {'nuevos departamentos' if count > 1 else 'nuevo departamento'}</strong>
        en tu búsqueda:
      </p>

      <table width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;">
        {rows}
      </table>

      <div style="margin-top:24px; text-align:center;">
        <a href="{search_url}"
           style="display:inline-block; background:#1a1a2e; color:#fff; padding:12px 28px;
                  border-radius:8px; text-decoration:none; font-size:14px; font-weight:600;">
          Ver búsqueda completa
        </a>
      </div>
    </div>

    <div style="background:#f9f9f9; padding:16px 32px; text-align:center;
                border-top:1px solid #eee;">
      <p style="margin:0; color:#999; font-size:12px;">
        Zonaprop Monitor · {datetime.now().strftime('%d/%m/%Y %H:%M')}
      </p>
    </div>
  </div>
</body>
</html>"""

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = smtp_cfg["username"]
    msg["To"]      = to
    msg.attach(MIMEText(html, "html"))

    with smtplib.SMTP(smtp_cfg["host"], smtp_cfg["port"]) as server:
        server.starttls()
        server.login(smtp_cfg["username"], smtp_cfg["password"])
        server.sendmail(smtp_cfg["username"], to, msg.as_string())

    log.info(f"  📧 Email enviado a {to}")

# ─── Chequeo de una búsqueda ──────────────────────────────────────────────────

def check_monitor(monitor: dict, smtp_cfg: dict):
    name  = monitor["name"]
    url   = monitor["url"]
    email = monitor["notify_email"]

    log.info(f"[{name}] Consultando...")

    try:
        current = fetch_listings(url)
    except Exception as e:
        log.error(f"[{name}] Error al consultar: {e}")
        return

    if not current:
        log.warning(f"[{name}] Sin resultados — el sitio puede haber cambiado.")
        return

    seen = load_seen(name)
    new_listings = [l for l in current if l["id"] not in seen]

    log.info(f"[{name}] Total: {len(current)} | Nuevos: {len(new_listings)}")

    if new_listings:
        for l in new_listings:
            log.info(f"[{name}]  🆕 {l['title']} — {l['price']} — {l['url']}")
        try:
            send_email(smtp_cfg, email, name, new_listings, url)
        except Exception as e:
            log.error(f"[{name}] Error enviando email: {e}")

    # Actualizar estado
    now = datetime.now().isoformat()
    for l in current:
        seen[l["id"]] = {
            "title":      l["title"],
            "price":      l["price"],
            "url":        l["url"],
            "first_seen": seen.get(l["id"], {}).get("first_seen", now),
        }
    save_seen(name, seen)

# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    log.info("=" * 60)
    log.info(f"Zonaprop Monitor iniciado — {datetime.now().strftime('%Y-%m-%d %H:%M')}")

    cfg      = load_config()
    smtp_cfg = cfg["smtp"]
    monitors = cfg["monitors"]

    log.info(f"Monitoreando {len(monitors)} búsqueda(s) en paralelo")

    with ThreadPoolExecutor(max_workers=len(monitors)) as executor:
        futures = {
            executor.submit(check_monitor, m, smtp_cfg): m["name"]
            for m in monitors
        }
        for future in as_completed(futures):
            name = futures[future]
            try:
                future.result()
            except Exception as e:
                log.error(f"[{name}] Error inesperado: {e}")

    log.info("Listo.")


if __name__ == "__main__":
    main()
