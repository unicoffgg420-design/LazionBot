"""
LAZION HUNTER BOT v2.1
Bot 24/7 de deteccion de tokens con overlap de "smart money" (metodo GMGN 7 pasos).
Corregido y endurecido a partir de AREPAPOWER 100x HUNTER BOT.

Cambios clave vs v1 (ver VEREDICTO.md para el detalle completo):
  - Etherscan migrado a API v2 (v1 murio el 15-ago-2025, el bot llevaba
    ~1 año haciendo llamadas a un endpoint muerto).
  - Scoring rediseñado: el maximo alcanzable en v1 era 7/10, así que la
    condicion "score >= 9" NUNCA podia cumplirse. El bot jamas iba a
    mandar una alerta.
  - GMGN ahora es una fuente opcional (bloquea requests sin navegador
    real via Cloudflare); si falla, el bot sigue funcionando solo con
    Dexscreener en vez de morir silenciosamente.
  - Rate limiting real: 1 sola llamada por wallet en vez de 3, con
    backoff automatico ante 429.
  - El ciclo principal ya no puede tumbar el proceso completo: cualquier
    excepcion se loggea y el bot sigue vivo (importante en Railway, que
    solo reintenta 10 veces antes de rendirse).
  - Deduplicado de alertas: no se repite el mismo token en cada ciclo.
  - Dependencias no usadas (solana, web3, python-telegram-bot) eliminadas.

v2.1 -- Helius reemplazo completo de API (agosto 2026):
  - El dominio api.helius.xyz y el endpoint /v0/token/transfers ya NO
    existen para este uso. Helius migro todo a:
      * JSON-RPC en mainnet.helius-rpc.com para historial de
        transacciones (metodo getTransactionsForAddress)
      * REST en api.helius.xyz/v1/wallet/{address}/balances para
        balances de wallet (Wallet API nueva)
  - get_sol_early_buyers ahora usa getTransactionsForAddress sobre la
    direccion del mint, ordenado cronologicamente (mas antiguo primero),
    y detecta compradores comparando preTokenBalances/postTokenBalances
    (patron documentado oficialmente por Helius).
  - get_wallet_tx_stats usa getTransactionsForAddress en modo
    "signatures" (mas barato en creditos) para actividad/deteccion de bots.
  - get_recent_tokens_bought (lado Solana) usa la Wallet API nueva
    (/v1/wallet/{address}/balances).
"""

import os
import json
import time
import statistics
from collections import Counter, defaultdict
from datetime import datetime, timedelta

import requests

# ----------------------------------------------------------------------
# CONFIG
# ----------------------------------------------------------------------

ANCHOR_COINS_SOL = [
    {"symbol": "BONK", "chain": "sol", "address": "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"},
    {"symbol": "WIF", "chain": "sol", "address": "EKpQGSJtjMFqKZ9KQanSqYXRcF8fBopzLHYxdM65zcjm"},
]
ANCHOR_COINS_ETH = [
    {"symbol": "PEPE", "chain": "eth", "address": "0x6982508145454Ce325dDbE47a25d4ec3d2311933"},
]
ANCHOR_COINS = ANCHOR_COINS_SOL + ANCHOR_COINS_ETH

# Tokens "obvios" que casi todas las wallets tienen (stablecoins, SOL
# wrapeado, ETH wrapeado). Si los dejamos en el analisis de overlap,
# el bot los va a marcar como "señal" constantemente solo porque todo
# el mundo los tiene -- no porque varias wallets hayan comprado lo
# mismo recientemente. Se filtran antes de buscar overlap.
EXCLUDED_MINTS = {
    # Solana
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",  # USDC
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",  # USDT
    "So11111111111111111111111111111111111111112",  # SOL wrapeado
    # Ethereum
    "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48",  # USDC
    "0xdac17f958d2ee523a2206206994597c13d831ec7",  # USDT
    "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2",  # WETH
}

HELIUS_API_KEY = os.getenv("HELIUS_API_KEY")
ETHERSCAN_API_KEY = os.getenv("ETHERSCAN_API_KEY")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# Umbral de score. El maximo alcanzable es 10 (ver score_token). Se deja
# configurable por variable de entorno porque GMGN a veces no responde
# (bloqueo Cloudflare) y en ese caso el maximo real baja a 7.
ALERT_THRESHOLD = int(os.getenv("ALERT_THRESHOLD", "7"))
CYCLE_SECONDS = int(os.getenv("CYCLE_SECONDS", "600"))
WALLETS_TO_ANALYZE = int(os.getenv("WALLETS_TO_ANALYZE", "5"))
STATE_FILE = os.getenv("STATE_FILE", "alerted_tokens.json")

# --- Watchlist dinamico de Solana --------------------------------------
# En vez de hardcodear direcciones (riesgo de tipeo = analizar el token
# equivocado sin darse cuenta), el bot le pregunta a Dexscreener cuales
# son los tokens de Solana activos ahora mismo y arma su propia lista.
# WATCHLIST_SIZE: cuantos tokens like maximo mantiene el watchlist.
# SEEDS_PER_CYCLE: cuantos de esos se analizan POR CICLO (rotando). Esto
# existe para no reventar los creditos gratis de Helius -- cada moneda
# analizada cuesta varias llamadas (early buyers + wallets + balances).
# Con el default (5), un watchlist de 50 se recorre completo cada ~10
# ciclos (~100 min con CYCLE_SECONDS=600). Subilo con cuidado.
WATCHLIST_SIZE = int(os.getenv("WATCHLIST_SIZE", "50"))
SEEDS_PER_CYCLE = int(os.getenv("SEEDS_PER_CYCLE", "5"))
WATCHLIST_REFRESH_SECONDS = int(os.getenv("WATCHLIST_REFRESH_SECONDS", str(6 * 3600)))
WATCHLIST_CACHE_FILE = os.getenv("WATCHLIST_CACHE_FILE", "sol_watchlist.json")

# --- Alertas de precio (SOL / BTC / ETH) --------------------------------
# Algo totalmente separado del sistema de "compradores tempranos": estas
# 3 son monedas grandes y ya establecidas, no moonshots nuevos, asi que
# no tiene sentido buscarles "early buyers". En cambio, el bot avisa
# cuando el precio se mueve mas de PRICE_ALERT_PCT% desde la ultima vez
# que aviso (o desde que arranco, para el primer chequeo).
PRICE_WATCH_COINS = {"SOL": "solana", "BTC": "bitcoin", "ETH": "ethereum"}
PRICE_ALERT_PCT = float(os.getenv("PRICE_ALERT_PCT", "5"))
PRICE_STATE_FILE = os.getenv("PRICE_STATE_FILE", "price_baseline.json")

BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
}

session = requests.Session()


# ----------------------------------------------------------------------
# HTTP helper con reintentos ante 429 / errores de red
# ----------------------------------------------------------------------

def http_get(url, timeout=15, headers=None, max_retries=2):
    for attempt in range(max_retries + 1):
        try:
            r = session.get(url, timeout=timeout, headers=headers or BROWSER_HEADERS)
            if r.status_code == 429:
                wait = 2 ** attempt
                print(f"[RATE LIMIT] {url[:60]}... esperando {wait}s")
                time.sleep(wait)
                continue
            r.raise_for_status()
            return r.json()
        except Exception as e:
            if attempt == max_retries:
                print(f"[ERROR] GET {url[:60]}...: {e}")
                return None
            time.sleep(1)
    return None


def http_post(url, payload, timeout=20, max_retries=2):
    for attempt in range(max_retries + 1):
        try:
            r = session.post(url, json=payload, timeout=timeout, headers=BROWSER_HEADERS)
            if r.status_code == 429:
                wait = 2 ** attempt
                print(f"[RATE LIMIT] {url[:60]}... esperando {wait}s")
                time.sleep(wait)
                continue
            r.raise_for_status()
            return r.json()
        except Exception as e:
            if attempt == max_retries:
                print(f"[ERROR] POST {url[:60]}...: {e}")
                return None
            time.sleep(1)
    return None


# ----------------------------------------------------------------------
# Helius JSON-RPC (getTransactionsForAddress) -- reemplaza los endpoints
# REST viejos de api.helius.xyz que ya no existen (ago-2026).
# ----------------------------------------------------------------------

def helius_rpc(method, params):
    if not HELIUS_API_KEY:
        return None
    url = f"https://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}"
    data = http_post(url, {"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
    if not data or "result" not in data:
        if data and data.get("error"):
            print(f"[ERROR] Helius RPC {method}: {data['error']}")
        return None
    return data["result"]


# ----------------------------------------------------------------------
# PASO 2: early buyers
# ----------------------------------------------------------------------

def get_sol_early_buyers(token_address, limit=20):
    """
    Usa getTransactionsForAddress sobre la direccion del MINT, ordenado
    cronologicamente (las primeras transacciones son las mas antiguas).
    Un "comprador" es cualquier owner cuyo balance de este mint sube
    entre preTokenBalances y postTokenBalances de una transaccion --
    este es el patron que la propia documentacion de Helius recomienda
    para detectar cambios de balance sin llamadas extra.
    """
    result = helius_rpc("getTransactionsForAddress", [
        token_address,
        {
            "transactionDetails": "full",
            "sortOrder": "asc",
            "limit": 100,
            "encoding": "jsonParsed",
            "maxSupportedTransactionVersion": 0,
            "filters": {"status": "succeeded"},
        },
    ])
    if not result:
        return []

    buyers, seen = [], set()
    for entry in result.get("data", []):
        meta = entry.get("meta") or {}
        pre = meta.get("preTokenBalances") or []
        post = meta.get("postTokenBalances") or []
        pre_by_idx = {
            b.get("accountIndex"): float((b.get("uiTokenAmount") or {}).get("uiAmount") or 0)
            for b in pre if b.get("mint") == token_address
        }
        for b in post:
            if b.get("mint") != token_address:
                continue
            owner = b.get("owner")
            if not owner or owner in seen:
                continue
            post_amt = float((b.get("uiTokenAmount") or {}).get("uiAmount") or 0)
            pre_amt = pre_by_idx.get(b.get("accountIndex"), 0)
            if post_amt > pre_amt:  # el balance subio = compro/recibio
                seen.add(owner)
                buyers.append(owner)
        if len(buyers) >= limit:
            break
    return buyers[:limit]


def get_eth_early_buyers(token_address, limit=20):
    if not ETHERSCAN_API_KEY:
        return []
    url = (
        "https://api.etherscan.io/v2/api?chainid=1&module=account&action=tokentx"
        f"&contractaddress={token_address}&startblock=0&endblock=99999999"
        f"&sort=asc&apikey={ETHERSCAN_API_KEY}"
    )
    data = http_get(url)
    if not data or data.get("status") not in ("1", 1):
        return []
    buyers, seen = [], set()
    for tx in data.get("result", [])[:500]:
        buyer = tx.get("to")
        if buyer and buyer not in seen:
            seen.add(buyer)
            buyers.append(buyer)
        if len(buyers) >= limit:
            break
    return buyers


# ----------------------------------------------------------------------
# PASOS 3+4: actividad + deteccion de bots (1 sola llamada por wallet)
# ----------------------------------------------------------------------

def get_wallet_tx_stats(wallet, chain="sol"):
    """Devuelve (is_active, is_bot) usando una unica llamada a la API."""
    try:
        if chain == "sol":
            # Modo "signatures": mas barato en creditos que "full" y es
            # todo lo que necesitamos (solo miramos blockTime).
            result = helius_rpc("getTransactionsForAddress", [
                wallet,
                {"transactionDetails": "signatures", "sortOrder": "desc", "limit": 20},
            ])
            entries = (result or {}).get("data", [])
            if not entries:
                return False, False
            timestamps = [e.get("blockTime", 0) for e in entries if e.get("blockTime")]
        else:
            url = (
                "https://api.etherscan.io/v2/api?chainid=1&module=account&action=txlist"
                f"&address={wallet}&startblock=0&endblock=99999999&sort=desc&apikey={ETHERSCAN_API_KEY}"
            )
            r = http_get(url)
            if not r or r.get("status") not in ("1", 1):
                return False, False
            timestamps = [int(t["timeStamp"]) for t in r.get("result", [])[:20]]

        if not timestamps:
            return False, False

        last_date = datetime.fromtimestamp(timestamps[0])
        is_active = (datetime.now() - last_date) < timedelta(days=30)

        is_bot = False
        if len(timestamps) >= 10:
            diffs = [abs(timestamps[i] - timestamps[i + 1]) for i in range(len(timestamps) - 1)]
            diffs = [d for d in diffs if d > 0]
            if diffs:
                is_bot = statistics.mean(diffs) < 15

        return is_active, is_bot
    except Exception as e:
        print(f"[ERROR] wallet_tx_stats {wallet[:8]}: {e}")
        return False, False


# ----------------------------------------------------------------------
# PASO 1 (extendido): watchlist dinamico de Solana via Dexscreener
# ----------------------------------------------------------------------

def fetch_solana_watchlist(limit=50):
    """
    Arma una lista de hasta `limit` tokens de Solana activos ahora mismo,
    usando las listas publicas de Dexscreener (sin API key). Devuelve
    solo direcciones REALES tal como las entrega Dexscreener -- nunca
    inventadas a mano.

    OJO: los "boosts" de Dexscreener son un producto PAGO (un proyecto
    paga para aparecer ahi). Eso NO los hace señal de calidad por si
    solos -- por eso igual pasan por score_token() como cualquier otro
    candidato antes de que el bot considere alertar.
    """
    seen = set()
    addresses = []
    for url in (
        "https://api.dexscreener.com/token-boosts/top/v1",
        "https://api.dexscreener.com/token-boosts/latest/v1",
        "https://api.dexscreener.com/token-profiles/latest/v1",
    ):
        data = http_get(url)
        if not isinstance(data, list):
            continue
        for item in data:
            if item.get("chainId") != "solana":
                continue
            addr = item.get("tokenAddress")
            if addr and addr not in seen and addr not in EXCLUDED_MINTS:
                seen.add(addr)
                addresses.append(addr)
            if len(addresses) >= limit:
                break
        if len(addresses) >= limit:
            break
    return addresses[:limit]


def load_watchlist():
    """Usa el watchlist cacheado si es reciente; si no, lo refresca."""
    try:
        with open(WATCHLIST_CACHE_FILE, "r") as f:
            cache = json.load(f)
        age = time.time() - cache.get("fetched_at", 0)
        if age < WATCHLIST_REFRESH_SECONDS and cache.get("addresses"):
            return cache["addresses"]
    except Exception:
        pass

    fresh = fetch_solana_watchlist(WATCHLIST_SIZE)
    if fresh:
        try:
            with open(WATCHLIST_CACHE_FILE, "w") as f:
                json.dump({"fetched_at": time.time(), "addresses": fresh}, f)
        except Exception as e:
            print(f"[ERROR] guardando watchlist: {e}")
        return fresh

    # Si Dexscreener falla, seguimos con los anchors fijos (BONK/WIF/PEPE)
    # en vez de dejar al bot sin nada que analizar.
    print("[AVISO] No se pudo refrescar el watchlist de Solana; sigo solo con los anchors.")
    return []


# ----------------------------------------------------------------------
# PASO 5: tokens comprados recientemente
# ----------------------------------------------------------------------

def get_recent_tokens_bought(wallet, chain="sol"):
    tokens = []
    try:
        if chain == "sol":
            # Wallet API nueva de Helius (reemplaza /v0/addresses/.../balances,
            # que ya no existe). Devuelve balance actual, igual que hacia v1.
            url = (
                f"https://api.helius.xyz/v1/wallet/{wallet}/balances"
                f"?api-key={HELIUS_API_KEY}&showZeroBalance=false&showNative=false&limit=100"
            )
            r = http_get(url)
            if isinstance(r, dict):
                for token in r.get("balances", [])[:50]:
                    mint = token.get("mint")
                    if mint and mint not in EXCLUDED_MINTS:
                        tokens.append(mint)
        else:
            url = (
                "https://api.etherscan.io/v2/api?chainid=1&module=account&action=tokentx"
                f"&address={wallet}&sort=desc&apikey={ETHERSCAN_API_KEY}"
            )
            r = http_get(url)
            if r and r.get("status") in ("1", 1):
                cutoff = datetime.now() - timedelta(days=30)
                for tx in r.get("result", [])[:100]:
                    ts = datetime.fromtimestamp(int(tx["timeStamp"]))
                    contract = (tx.get("contractAddress") or "").lower()
                    if ts > cutoff and contract and contract not in EXCLUDED_MINTS:
                        tokens.append(tx["contractAddress"])
    except Exception as e:
        print(f"[ERROR] recent_tokens {wallet[:8]}: {e}")
    return list(set(tokens))


# ----------------------------------------------------------------------
# PASO 6: overlap
# ----------------------------------------------------------------------

def find_overlaps(wallets_recent_map):
    counter = Counter()
    token_to_wallets = defaultdict(list)
    for wallet, tokens in wallets_recent_map.items():
        for t in tokens:
            counter[t] += 1
            token_to_wallets[t].append(wallet)
    return {
        token: {"count": c, "wallets": token_to_wallets[token]}
        for token, c in counter.items() if c >= 3
    }


# ----------------------------------------------------------------------
# PASO 7: scoring -- REDISEÑADO para que el maximo real sea 10
# ----------------------------------------------------------------------
#
# v1 sumaba como maximo 7 puntos (liquidez, no_mint, lp_lock, liq_dex,
# microcap, socials, overlap_3plus) pero exigia score >= 9. Era
# matematicamente imposible que saltara una alerta.
#
# v2: Dexscreener (siempre disponible) da hasta 7 puntos por si solo.
# GMGN (a veces bloqueado por Cloudflare) suma hasta 3 puntos extra.
# ALERT_THRESHOLD=7 por defecto es alcanzable solo con Dexscreener;
# subilo a 9-10 en Railway si queres exigir tambien confirmacion GMGN.

def score_token(token_address, overlap_count, chain="sol"):
    score = 0
    checks = {}

    # Overlap (hasta 3 pts) -- señal central del metodo
    if overlap_count >= 3:
        score += 2
        checks["overlap_3plus"] = True
    if overlap_count >= 5:
        score += 1
        checks["overlap_5plus"] = True

    # Dexscreener (hasta 4 pts) -- fuente primaria, sin bloqueo anti-bot
    try:
        dex = http_get(f"https://api.dexscreener.com/latest/dex/tokens/{token_address}")
        pairs = dex.get("pairs") if dex else None
        pair = pairs[0] if pairs else {}
        liq = (pair.get("liquidity") or {}).get("usd", 0) or 0
        fdv = pair.get("fdv", 0) or 0
        if liq > 20000:
            score += 2
            checks["liquidez_dex"] = True
        if 0 < fdv < 2_000_000:
            score += 1
            checks["microcap"] = True
        if (pair.get("info") or {}).get("socials"):
            score += 1
            checks["socials"] = True
    except Exception as e:
        print(f"[ERROR] dexscreener {token_address[:8]}: {e}")

    # GMGN (hasta 3 pts) -- opcional, puede fallar por Cloudflare
    if chain == "sol":
        try:
            url = f"https://gmgn.ai/defi/quotation/v1/tokens/security/sol/{token_address}"
            r = http_get(url)
            data = (r or {}).get("data", {})
            if data:
                if float(data.get("liquidity", 0) or 0) > 30000:
                    score += 1
                    checks["liquidez_gmgn"] = True
                if data.get("is_mintable") is False:
                    score += 1
                    checks["no_mint"] = True
  
