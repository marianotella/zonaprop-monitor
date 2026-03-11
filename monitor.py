#!/usr/bin/env python3
"""
Zonaprop Server Monitor
- Lee múltiples URLs desde config.json
- Consulta secuencialmente (evita 403 por IP)
- Manda email cuando aparecen listings nuevos con foto y ambientes
- Guarda estado por búsqueda en data/
"""

import json
import re
import smtplib
import ssl
import sys
import time
import logging
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
    from curl_cffi import requests as cffi_requests
    CURL_OK = True
except ImportError:
    CURL_OK = False

# ─── Paths ────────────────────────────────────────────────────────────────────

BASE_DIR = Path(__file__).parent
CONFIG   = BASE_DIR / "config.json"
DATA_DIR = BASE_DIR / "data"
LOG_FILE = BASE_DIR / "monitor.log"

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

def state_file(monitor_name):
    safe = re.sub(r"[^\w\-]", "_", monitor_name)
    return DATA_DIR / f"{safe}.json"

def load_seen(monitor_name):
    f = state_file(monitor_name)
    if f.exists():
        with open(f) as fp:
            return json.load(fp)
    return {}

def save_seen(monitor_name, data):
    with open(state_file(monitor_name), "w") as fp:
        json.dump(data, fp, ensure_ascii=False, indent=2)

# ─── Extracción de features ───────────────────────────────────────────────────

def _extract_ambientes_from_features(features):
    """Busca ambientes en mainFeatures. Devuelve string con el número o ''."""
    if not features:
        return ""
    if isinstance(features, list):
        for feat in features:
            if not isinstance(feat, dict):
                continue
            label = str(feat.get("label", "")).lower()
            value = str(feat.get("value", ""))
            if re.search(r"amb|room|ambiente", label):
                m = re.search(r"(\d+)", value)
                if m:
                    return m.group(1)
            if re.search(r"(\d+)\s*amb", value, re.I):
                m = re.search(r"(\d+)", value)
                if m:
                    return m.group(1)
    if isinstance(features, dict):
        for key, val in features.items():
            val_str = str(val).lower()
            if re.search(r"amb|room", val_str):
                m = re.search(r"(\d+)", val_str)
                if m:
                    return m.group(1)
    return ""


def _extract_surface_from_features(features):
    """Busca superficie (m²) en mainFeatures. Devuelve string como '70' o ''."""
    if not features:
        return ""
    if isinstance(features, list):
        for feat in features:
            if not isinstance(feat, dict):
                continue
            label = str(feat.get("label", "")).lower()
            value = str(feat.get("value", ""))
            # Etiquetas típicas: "Superficie total", "Superficie cubierta", "m²"
            if re.search(r"superficie|m²|m2|area|tamaño", label):
                m = re.search(r"(\d+)", value)
                if m:
                    return m.group(1)
            # Valor con "m²" explícito: "70 m²"
            if re.search(r"(\d+)\s*m[²2]", value, re.I):
                m = re.search(r"(\d+)", value)
                if m:
                    return m.group(1)
    if isinstance(features, dict):
        for key, val in features.items():
            val_str = str(val)
            if re.search(r"(\d+)\s*m[²2]", val_str, re.I):
                m = re.search(r"(\d+)", val_str)
                if m:
                    return m.group(1)
    return ""

# ─── Scraping ─────────────────────────────────────────────────────────────────

HEADERS = {
    "Accept-Language": "es-AR,es;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

def _parse_html(html, url):
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

        # Foto
        img_el = card.find("img")
        photo = img_el.get("src") or img_el.get("data-src", "") if img_el else ""

        # Ambientes y m²: buscar spans de postingMainFeatures
        rooms = ""
        surface = ""
        features_spans = card.find_all(
            "span", class_=re.compile(r"postingMainFeatures.*span", re.I)
        )
        for span in features_spans:
            text = span.get_text(strip=True)
            if not rooms and re.search(r"amb", text, re.I):
                m = re.search(r"(\d+)", text)
                if m:
                    rooms = m.group(1)
            if not surface and re.search(r"m[²2]", text, re.I):
                m = re.search(r"(\d+)", text)
                if m:
                    surface = m.group(1)

        listings.append({
            "id":      str(lid),
            "title":   title_el.get_text(strip=True)[:100] if title_el else "",
            "price":   price_el.get_text(strip=True)[:60]  if price_el else "",
            "url":     link,
            "photo":   photo,
            "rooms":   rooms,
            "surface": surface,
        })
    return listings


def fetch_listings(url, retries=3):
    """Obtiene listings usando curl_cffi (impersona Chrome, evita Cloudflare)."""
    if not CURL_OK:
        raise RuntimeError(
            "curl_cffi no está instalado. Corré: pip install curl_cffi"
        )

    last_err = None
    for attempt in range(retries):
        if attempt > 0:
            delay = attempt * 5
            log.info(f"  Reintento {attempt}/{retries-1} en {delay}s...")
            time.sleep(delay)
        try:
            resp = cffi_requests.get(
                url,
                impersonate="chrome120",
                headers=HEADERS,
                timeout=30,
            )
            if resp.status_code == 403:
                log.warning(f"  403 Forbidden en intento {attempt+1}")
                last_err = Exception(f"HTTP 403")
                continue
            resp.raise_for_status()
            log.info(f"  HTTP {resp.status_code} OK")
            return _parse_html(resp.text, url)
        except Exception as e:
            last_err = e
            log.warning(f"  Error en intento {attempt+1}: {e}")

    raise last_err or Exception("fetch_listings: sin respuesta")


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

        # ── Precio ────────────────────────────────────────────────────────────
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

        # ── URL ───────────────────────────────────────────────────────────────
        url = item.get("url") or item.get("link") or item.get("permalink") or ""
        if url and not url.startswith("http"):
            url = "https://www.zonaprop.com.ar" + url

        # ── Foto ──────────────────────────────────────────────────────────────
        photo = ""
        # Zonaprop suele tener photos/pictures como lista o dict
        photos = item.get("photos") or item.get("pictures") or item.get("images") or []
        if isinstance(photos, list) and photos:
            first_photo = photos[0]
            if isinstance(first_photo, dict):
                photo = (first_photo.get("url") or first_photo.get("src") or
                         first_photo.get("image") or "")
            elif isinstance(first_photo, str):
                photo = first_photo
        elif isinstance(photos, dict):
            photo = photos.get("url") or photos.get("src") or ""
        # Alternativa: campo directo
        if not photo:
            photo = item.get("thumbnail") or item.get("mainPhoto") or item.get("coverPhoto") or ""
            if isinstance(photo, dict):
                photo = photo.get("url") or photo.get("src") or ""

        # ── Ambientes y superficie ────────────────────────────────────────────
        main_features = item.get("mainFeatures") or item.get("features") or []

        rooms = _extract_ambientes_from_features(main_features)
        if not rooms:
            for key in ("totalRooms", "roomsAmount", "suites", "rooms"):
                val = item.get(key)
                if val is not None:
                    m = re.search(r"(\d+)", str(val))
                    if m:
                        rooms = m.group(1)
                        break

        surface = _extract_surface_from_features(main_features)
        if not surface:
            for key in ("surface", "totalArea", "coveredArea", "squareMeters", "area"):
                val = item.get(key)
                if val is not None:
                    m = re.search(r"(\d+)", str(val))
                    if m:
                        surface = m.group(1)
                        break

        if pid:
            results.append({
                "id":      str(pid),
                "title":   title[:100],
                "price":   price[:60],
                "url":     url,
                "photo":   photo,
                "rooms":   rooms,
                "surface": surface,
            })
    return results

# ─── Email ────────────────────────────────────────────────────────────────────

def send_email(smtp_cfg, to, monitor_name, new_listings, search_url):
    count = len(new_listings)
    subject = f"🏠 {count} nuevo{'s' if count > 1 else ''} depto{'s' if count > 1 else ''} — {monitor_name}"

    rows = ""
    for l in new_listings:
        title = l.get("title") or f"Propiedad {l['id']}"
        price = l.get("price") or "Precio no disponible"
        url   = l.get("url")   or search_url
        photo   = l.get("photo")   or ""
        rooms   = l.get("rooms")   or ""
        surface = l.get("surface") or ""

        badges = ""
        if rooms:
            badges += (
                f'<span style="display:inline-block; background:#e8f4fd; color:#1a73e8; '
                f'font-size:12px; font-weight:600; padding:2px 8px; border-radius:12px; margin-left:6px;">'
                f'{rooms} amb.</span>'
            )
        if surface:
            badges += (
                f'<span style="display:inline-block; background:#f0fdf4; color:#16a34a; '
                f'font-size:12px; font-weight:600; padding:2px 8px; border-radius:12px; margin-left:4px;">'
                f'{surface} m²</span>'
            )

        photo_cell = (
            f'<td style="width:100px; padding:8px; border-bottom:1px solid #eee; vertical-align:top;">'
            f'<a href="{url}"><img src="{photo}" width="90" height="65" '
            f'style="object-fit:cover; border-radius:6px; display:block;" '
            f'alt="foto"></a></td>'
        ) if photo else (
            f'<td style="width:100px; padding:8px; border-bottom:1px solid #eee; vertical-align:top;">'
            f'<div style="width:90px; height:65px; background:#f0f0f0; border-radius:6px; '
            f'display:flex; align-items:center; justify-content:center; '
            f'font-size:24px;">🏠</div></td>'
        )

        rows += f"""
        <tr>
          {photo_cell}
          <td style="padding:12px 8px; border-bottom:1px solid #eee; vertical-align:top;">
            <a href="{url}" style="font-weight:600; color:#1a1a2e; text-decoration:none; font-size:15px;">
              {title}
            </a>{badges}<br>
            <span style="color:#e63946; font-weight:700; font-size:14px;">{price}</span>
          </td>
          <td style="padding:12px 8px; border-bottom:1px solid #eee; text-align:right;
                     white-space:nowrap; vertical-align:middle;">
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
  <div style="max-width:640px; margin:32px auto; background:#fff; border-radius:12px;
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

    # Intentar primero con STARTTLS (587), luego SSL (465)
    sent = False
    errors = []
    try:
        with smtplib.SMTP(smtp_cfg["host"], smtp_cfg["port"], timeout=15) as server:
            server.starttls()
            server.login(smtp_cfg["username"], smtp_cfg["password"])
            server.sendmail(smtp_cfg["username"], to, msg.as_string())
        sent = True
    except Exception as e:
        errors.append(f"STARTTLS:{e}")

    if not sent:
        try:
            ctx = ssl.create_default_context()
            with smtplib.SMTP_SSL(smtp_cfg["host"], 465, context=ctx, timeout=15) as server:
                server.login(smtp_cfg["username"], smtp_cfg["password"])
                server.sendmail(smtp_cfg["username"], to, msg.as_string())
            sent = True
        except Exception as e:
            errors.append(f"SSL:{e}")

    if sent:
        log.info(f"  📧 Email enviado a {to}")
    else:
        raise Exception(f"No se pudo enviar email: {'; '.join(errors)}")

# ─── Chequeo de una búsqueda ──────────────────────────────────────────────────

def check_monitor(monitor, smtp_cfg):
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
            meta = []
            if l.get("rooms"):   meta.append(f"{l['rooms']} amb.")
            if l.get("surface"): meta.append(f"{l['surface']} m²")
            meta_str = f" | {', '.join(meta)}" if meta else ""
            log.info(f"[{name}]  🆕 {l['title']}{meta_str} — {l['price']} — {l['url']}")
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
            "rooms":      l.get("rooms", ""),
            "surface":    l.get("surface", ""),
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

    log.info(f"Monitoreando {len(monitors)} búsqueda(s) secuencialmente")

    for i, monitor in enumerate(monitors):
        if i > 0:
            log.info("  Esperando 10s antes de la siguiente búsqueda...")
            time.sleep(10)
        try:
            check_monitor(monitor, smtp_cfg)
        except Exception as e:
            log.error(f"[{monitor['name']}] Error inesperado: {e}")

    log.info("Listo.")


if __name__ == "__main__":
    main()
