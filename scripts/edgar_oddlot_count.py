"""Count issuer self-tender filings (SC TO-I) on EDGAR that mention odd-lot priority, per year.
Pre-registered experiment helper: prints one JSON object of metrics. Upper bound only: a full-text
hit does not prove the tender was unconditional and uncapped; that requires reading each filing.
Usage: python edgar_oddlot_count.py [start_year] [end_year]"""
import json, sys, time, urllib.parse, urllib.request

BASE = "https://efts.sec.gov/LATEST/search-index"
UA = "Roundtable research tool (contact: research@roundtable.build)"   # SEC fair-access policy wants a contact


def _get(params: dict) -> dict:
    req = urllib.request.Request(BASE + "?" + urllib.parse.urlencode(params), headers={"User-Agent": UA, "Accept": "application/json"})
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.load(r)
        except Exception:  # noqa: BLE001
            if attempt == 3:
                raise
            time.sleep(2 * (attempt + 1))
    return {}


def count(q: str, forms: str, start: str, end: str) -> int:
    """Distinct filings (accession numbers). EDGAR hits are per document, and one filing has several."""
    accessions: set[str] = set()
    frm = 0
    while True:
        data = _get({"q": q, "forms": forms, "dateRange": "custom", "startdt": start, "enddt": end, "from": frm})
        hits = data.get("hits", {}).get("hits", [])
        for h in hits:
            accessions.add(h["_id"].split(":")[0])
        if len(hits) < 100:
            break
        frm += 100
        time.sleep(0.3)
    return len(accessions)


def main() -> int:
    y0 = int(sys.argv[1]) if len(sys.argv) > 1 else 2022
    y1 = int(sys.argv[2]) if len(sys.argv) > 2 else 2026
    out = {"source": "EDGAR full-text search (efts.sec.gov)", "forms": "SC TO-I", "note": "upper bound; hits are filings, not verified eligible tenders"}
    per_year = {}
    for y in range(y0, y1 + 1):
        start, end = f"{y}-01-01", (f"{y}-12-31" if y < 2026 else "2026-09-26")
        all_toi = count('"tender offer"', "SC TO-I", start, end)
        time.sleep(0.5)
        odd = count('"odd lot"', "SC TO-I", start, end)
        time.sleep(0.5)
        odd_priority = count('"odd lot" priority', "SC TO-I", start, end)
        time.sleep(0.5)
        per_year[str(y)] = {"sc_to_i": all_toi, "odd_lot_mentions": odd, "odd_lot_priority_mentions": odd_priority}
    full_years = [y for y in per_year if y != "2026"]
    out["per_year"] = per_year
    out["odd_lot_priority_per_year_mean"] = round(sum(per_year[y]["odd_lot_priority_mentions"] for y in full_years) / max(1, len(full_years)), 1)
    out["odd_lot_mentions_per_year_mean"] = round(sum(per_year[y]["odd_lot_mentions"] for y in full_years) / max(1, len(full_years)), 1)
    out["sc_to_i_per_year_mean"] = round(sum(per_year[y]["sc_to_i"] for y in full_years) / max(1, len(full_years)), 1)
    flat = {k: v for k, v in out.items() if not isinstance(v, dict)}
    for y, m in per_year.items():
        for k, v in m.items():
            flat[f"{k}_{y}"] = v
    print(json.dumps(flat, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
