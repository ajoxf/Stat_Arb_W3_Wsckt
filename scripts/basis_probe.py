#!/usr/bin/env python3
"""
basis_probe.py — is the BTC dated-future vs perp basis actually tradeable?

Your worry is the right one: the basis is a textbook trade, so it's arbitraged
tight — few dislocations, and fast players eat them first. This measures exactly
that, from OKX public candles (no API key), so the decision is data, not vibes:

  FREQUENCY  — how many distinct |z|>=entry dislocations per day/week
  PERSISTENCE— how long each lasts (your window to actually get in)
  MAGNITUDE  — how big (bps of price) vs your ~1.6 bps round-trip fee hurdle
  REVERSION  — Hurst + half-life (does it come back, and how fast)

Run on the box with OKX access:
    python scripts/basis_probe.py                          # nearest BTC-USDT quarterly vs perp
    python scripts/basis_probe.py --future BTC-USDT-260926 # pin a contract
    python scripts/basis_probe.py --bar 5m --bars 3000     # finer / more history

Read-only market data. Nothing is traded.
"""
import argparse
import json
import math
import sys
import time
import urllib.parse
import urllib.request

try:
    import numpy as np
except ImportError:
    sys.exit("needs numpy (pip install numpy)")

OKX = "https://www.okx.com"
BAR_MIN = {"1m": 1, "3m": 3, "5m": 5, "15m": 15, "30m": 30, "1H": 60, "2H": 120, "4H": 240}


def _get(path, params):
    url = OKX + path + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": "basis-probe"})
    with urllib.request.urlopen(req, timeout=25) as r:
        d = json.loads(r.read())
    if d.get("code") != "0":
        raise RuntimeError(f"OKX {path}: {d.get('code')} {d.get('msg')}")
    return d["data"]


def nearest_futures(uly="BTC-USDT"):
    insts = _get("/api/v5/public/instruments", {"instType": "FUTURES", "uly": uly})
    now = time.time() * 1000
    return sorted([i for i in insts if i.get("state") == "live"
                   and float(i.get("expTime", 0)) > now],
                  key=lambda i: float(i["expTime"]))


def fetch_closes(inst, bar, want):
    """Paginate history-candles backward; return {ts_ms: close}."""
    out, after, calls = {}, None, 0
    while len(out) < want and calls < 60:
        p = {"instId": inst, "bar": bar, "limit": "100"}
        if after:
            p["after"] = after
        data = _get("/api/v5/market/history-candles", p)
        calls += 1
        if not data:
            break
        for row in data:                       # [ts,o,h,l,c,vol,...] newest-first
            out[int(row[0])] = float(row[4])
        after = data[-1][0]
        time.sleep(0.12)
    return out


def _signalgen():
    """A SignalGenerator with __init__ bypassed, so we can reuse the ENGINE's
    exact Hurst (Anis-Lloyd corrected) and half-life — the same numbers the bot
    trades on. Returns None if the module can't be imported (then we fall back)."""
    try:
        from core.signals import SignalGenerator
        return SignalGenerator.__new__(SignalGenerator)
    except Exception:
        return None


_SG = _signalgen()


def half_life(series):
    """OU half-life in bars. Reuses core/signals.py when available."""
    series = np.asarray(series, dtype=float)
    if _SG is not None:
        try:
            return _SG._calculate_half_life(series)   # bars; inf if not reverting
        except Exception:
            pass
    n = len(series)
    if n < 10:
        return float("inf")
    lag, diff = series[:-1], series[1:] - series[:-1]
    x, y = np.mean(lag) - lag, diff
    denom = np.dot(x, x)
    if denom == 0:
        return float("inf")
    theta = np.dot(x, y) / denom
    return math.log(2) / theta if theta > 0 else float("inf")


def hurst(series):
    """Hurst exponent. Reuses the engine's Anis-Lloyd-corrected estimator so the
    number matches what the bot's filters see; rough R/S fallback if unavailable."""
    series = np.asarray(series, dtype=float)
    if _SG is not None:
        try:
            return _SG._calculate_hurst(series)
        except Exception:
            pass
    n = len(series)
    if n < 64:
        return float("nan")
    ks = np.unique(np.logspace(math.log10(8), math.log10(n // 2), 12).astype(int))
    pts = []
    for k in ks:
        vals = []
        for s in range(n // k):
            seg = series[s * k:(s + 1) * k]
            dev = np.cumsum(seg - seg.mean())
            sd = seg.std()
            if sd > 0:
                vals.append((dev.max() - dev.min()) / sd)
        if vals:
            pts.append((k, np.mean(vals)))
    if len(pts) < 3:
        return float("nan")
    return float(np.polyfit(np.log([p[0] for p in pts]),
                            np.log([p[1] for p in pts]), 1)[0])


def analyze(ts, basis, perp_px, bar_min, window, entry_z):
    """Pure analysis — separated from the fetch so it's testable offline."""
    span_days = len(ts) * bar_min / 1440.0
    z = np.full(len(basis), np.nan)
    for i in range(window, len(basis)):
        win = basis[i - window:i]
        sd = win.std()
        if sd > 0:
            z[i] = (basis[i] - win.mean()) / sd
    valid = ~np.isnan(z)
    hot = valid & (np.abs(z) >= entry_z)

    # distinct dislocation events = rising edges of `hot`
    events = int(np.sum(hot[1:] & ~hot[:-1])) + (1 if hot.size and hot[0] else 0)
    # persistence: run lengths of hot bars
    runs, c = [], 0
    for h in hot:
        if h:
            c += 1
        elif c:
            runs.append(c); c = 0
    if c:
        runs.append(c)
    avg_run = float(np.mean(runs)) if runs else 0.0
    # magnitude at hot bars: |basis - rolling mean|, in bps of perp price
    devs_bps = []
    for i in range(window, len(basis)):
        if valid[i] and abs(z[i]) >= entry_z and perp_px[i] > 0:
            devs_bps.append(abs(basis[i] - basis[i - window:i].mean()) / perp_px[i] * 1e4)

    hl = half_life(basis - basis.mean())
    H = hurst(basis)

    print(f"Aligned bars: {len(ts)} over ~{span_days:.1f} days")
    print(f"Basis (dated − perp): mean ${basis.mean():+.2f}  std ${basis.std():.2f}  "
          f"range ${basis.min():+.1f}..${basis.max():+.1f}")
    print(f"\nREVERSION:")
    print(f"  half-life= {hl:.0f} bars (~{hl*bar_min:.0f} min)   <- THE reliable metric: "
          f"short = it snaps back" if hl != float("inf")
          else "  half-life= inf  <- NOT mean-reverting")
    print(f"  Hurst    = {H:.2f}   (engine's estimator runs high on everything — your")
    print(f"             CURRENT ETH/BTC pair sits ~0.90; read this RELATIVE, lower=better)")
    print(f"\nOPPORTUNITY (|z| ≥ {entry_z}):")
    if span_days > 0:
        print(f"  distinct dislocations : {events}   →  ~{events/span_days:.1f}/day · "
              f"~{events/span_days*7:.0f}/week")
    print(f"  avg time to enter     : ~{avg_run*bar_min:.0f} min per event ({avg_run:.1f} bars) "
          f"— shorter = you need to be fast")
    if devs_bps:
        md = float(np.median(devs_bps))
        print(f"  median dislocation    : {md:.1f} bps of price   "
              f"(vs ~1.6 bps round-trip fee → {'clears' if md > 3 else 'THIN'} the fee hurdle)")
    # heuristic verdict — reversion judged on HALF-LIFE (Hurst is biased), the
    # rest on the raw opportunity numbers. These are guides; read the numbers.
    print(f"\nVERDICT (heuristic — trust the numbers above over these labels):")
    revert = hl != float("inf") and hl <= window            # snaps back within the z-window
    per_day = events / span_days if span_days else 0.0
    freq_ok = per_day >= 0.3                                 # ~2+/week
    big_ok = bool(devs_bps) and float(np.median(devs_bps)) > 3   # clears ~1.6bp fee w/ margin
    print(f"  reverts?     {'YES' if revert else 'NO/weak'}   "
          f"(half-life {'∞' if hl==float('inf') else f'{hl*bar_min:.0f}m'} — reverts "
          f"{'within' if revert else 'beyond'} the z-window)")
    print(f"  enough ops?  {'SOME' if freq_ok else 'FEW'}   (~{per_day:.1f}/day)")
    print(f"  worth fees?  {'YES' if big_ok else 'MARGINAL'}   "
          f"(median {float(np.median(devs_bps)) if devs_bps else 0:.1f} bps vs ~1.6 bps)")
    if revert and freq_ok and big_ok:
        print("  → Tradeable on these numbers. Worth building the roll/funding logic.")
    elif revert and not freq_ok:
        print("  → Reverts cleanly but RARE — your 'few opportunities' worry is confirmed;")
        print("    only worth it if each op is big enough to matter at size.")
    elif not revert:
        print("  → Doesn't snap back fast enough — no better than the current pair. Skip.")
    else:
        print("  → Marginal. Run a longer --bars window before deciding.")


def main():
    ap = argparse.ArgumentParser(description="Probe the BTC future-vs-perp basis for tradeability.")
    ap.add_argument("--future", help="dated future instId (default: nearest live BTC-USDT quarterly)")
    ap.add_argument("--perp", default="BTC-USDT-SWAP")
    ap.add_argument("--bar", default="15m", choices=list(BAR_MIN))
    ap.add_argument("--bars", type=int, default=1500, help="how many bars of history to pull")
    ap.add_argument("--window", type=int, default=96, help="rolling z-score window in bars")
    ap.add_argument("--entry-z", type=float, default=3.5)
    args = ap.parse_args()

    if not args.future:
        futs = nearest_futures()
        if not futs:
            sys.exit("No live BTC-USDT dated futures found.")
        print("Live BTC-USDT futures: " + ", ".join(
            f"{f['instId']} (exp {time.strftime('%Y-%m-%d', time.gmtime(float(f['expTime'])/1000))})"
            for f in futs[:4]))
        args.future = futs[0]["instId"]

    print(f"\nProbing basis: {args.future} (dated) vs {args.perp} (perp) · {args.bar} · "
          f"~{args.bars} bars\n" + "=" * 64)
    fut = fetch_closes(args.future, args.bar, args.bars)
    perp = fetch_closes(args.perp, args.bar, args.bars)
    ts = sorted(set(fut) & set(perp))
    if len(ts) < args.window + 50:
        sys.exit(f"Only {len(ts)} aligned bars — need > {args.window+50}. Try a larger --bars.")
    basis = np.array([fut[t] - perp[t] for t in ts])
    perp_px = np.array([perp[t] for t in ts])
    analyze(ts, basis, perp_px, BAR_MIN[args.bar], args.window, args.entry_z)


if __name__ == "__main__":
    main()
