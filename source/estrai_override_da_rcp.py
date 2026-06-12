#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
estrai_override_da_rcp.py
=========================
Analizza i PDF dei Riassunti delle Caratteristiche del Prodotto (RCP) scaricati
dalla Banca Dati Farmaci AIFA e propone voci di override per la frantumabilità,
cercando nel testo le formule esplicite (sezioni 4.2 e 6.6).

>>> IL RISULTATO È UNA PROPOSTA DA REVISIONARE A MANO. <<<
L'analisi è euristica: i falsi positivi/negativi sono possibili e pericolosi.
Nessuna voce va pubblicata senza la verifica di un professionista sull'RCP.

USO:
    pip install pdfplumber
    # opzione A) una cartella di PDF nominati con l'AIC, es. 032657028.pdf
    python3 estrai_override_da_rcp.py ./rcp_pdf/
    # opzione B) anche l'anagrafica, per riempire il principio attivo nelle proposte
    python3 estrai_override_da_rcp.py ./rcp_pdf/ --anagrafica anagrafica_aifa.csv

OUTPUT:
    override_proposto.json   -> voci proposte (verde / rosso) con la frase trovata
    rcp_report.txt           -> riepilogo e file senza esito chiaro

FLUSSO CONSIGLIATO:
    1. genera farmaci.json con genera_database.py (la maggior parte sarà rossa)
    2. scarica gli RCP dei farmaci che ti interessano (per AIC)
    3. esegui questo script -> ottieni override_proposto.json
    4. REVISIONA ogni voce leggendo la frase di contesto
    5. sposta le voci valide in override.json e rigenera farmaci.json
"""

import csv
import io
import json
import re
import sys
import unicodedata
from pathlib import Path

# ----------------------------------------------------------------------------
# Frasi che indicano TRITURABILITÀ POSITIVA (compressa) o APERTURA (capsula)
# ----------------------------------------------------------------------------
POSITIVE = [
    r"pu[òo]\s+essere\s+(frantumat|tritat|schiacciat|divis|spezzat|dispers|sciolt|disciolt)",
    r"(frantumat|tritat|schiacciat|dispers|sciolt|disciolt)\w*\s+in\s+(acqua|un\s+bicchier)",
    r"pu[òo]\s+essere\s+aperta",                       # capsule
    r"il\s+contenuto\s+(della\s+capsula\s+)?pu[òo]\s+essere",
    r"somministrat\w*\s+(tramite|attraverso|mediante|con)\s+(sondino|sng|peg|catetere)",
    r"compress\w*\s+dispersibil",
    r"si\s+(dissolve|scioglie|disperde)\s+in\s+acqua",
    r"pu[òo]\s+essere\s+sospes\w+\s+in\s+acqua",
]

# ----------------------------------------------------------------------------
# Frasi che CONTROINDICANO la frantumazione (hanno priorità sui positivi)
# ----------------------------------------------------------------------------
NEGATIVE = [
    r"non\s+(deve\s+essere\s+|va\s+|devono\s+essere\s+)?(frantumat|tritat|schiacciat|divis|spezzat|mastic|apert|rott)",
    r"deglutit\w*\s+inter",
    r"deve\s+essere\s+deglutit\w*\s+inter",
    r"senza\s+(essere\s+)?(frantumat|mastic|schiacciat|apert)",
    r"non\s+(frantumare|triturare|masticare|aprire|dividere|rompere)",
    r"deglutire\s+\w*\s*inter",
    r"deve\s+essere\s+ingerit\w*\s+inter",
    r"compress\w*\s+(gastroresistent|a\s+rilascio\s+(prolungat|modificat|controllat))",
]

POS_RE = [re.compile(p, re.I) for p in POSITIVE]
NEG_RE = [re.compile(p, re.I) for p in NEGATIVE]


def normalizza(testo: str) -> str:
    t = unicodedata.normalize("NFD", testo or "")
    return "".join(c for c in t if unicodedata.category(c) != "Mn").lower().strip()


def estrai_testo(pdf_path: Path) -> str:
    """Estrae il testo dal PDF. Richiede pdfplumber."""
    try:
        import pdfplumber
    except ImportError:
        print("ERRORE: installa pdfplumber con  pip install pdfplumber")
        sys.exit(1)
    parti = []
    with pdfplumber.open(str(pdf_path)) as pdf:
        for pagina in pdf.pages:
            parti.append(pagina.extract_text() or "")
    return "\n".join(parti)


def sezioni_rilevanti(testo: str) -> str:
    """Isola, se possibile, le sezioni 4.2 (Posologia) e 6.6 (Precauzioni
    per lo smaltimento e la manipolazione), dove di solito sta l'informazione.
    Se non le trova, restituisce tutto il testo."""
    blocchi = []
    for inizio, fine in ((r"4\.?2", r"4\.?3"), (r"6\.?6", r"7\.?\s")):
        m = re.search(inizio, testo)
        if m:
            coda = testo[m.start():]
            stop = re.search(fine, coda[3:])
            blocchi.append(coda[: (stop.start() + 3) if stop else 4000])
    return "\n".join(blocchi) if blocchi else testo


def frasi(testo: str):
    """Spezza in frasi grossolane su . ; e a-capo."""
    for f in re.split(r"(?<=[.;:])\s+|\n", testo):
        f = f.strip()
        if 8 <= len(f) <= 320:
            yield f


def analizza(testo: str) -> dict:
    """Ritorna {'esito': 'verde'|'rosso'|None, 'frase': str, 'tipo': str}.
    La controindicazione esplicita vince sempre sul positivo."""
    area = sezioni_rilevanti(testo)
    frase_pos = frase_neg = None
    for f in frasi(area):
        if frase_neg is None and any(r.search(f) for r in NEG_RE):
            frase_neg = f
        if frase_pos is None and any(r.search(f) for r in POS_RE):
            # un positivo non vale se la stessa frase contiene una negazione
            if not any(r.search(f) for r in NEG_RE):
                frase_pos = f
    if frase_neg:
        return {"esito": "rosso", "frase": frase_neg[:200], "tipo": "controindicazione esplicita"}
    if frase_pos:
        return {"esito": "verde", "frase": frase_pos[:200], "tipo": "triturabilità esplicita"}
    return {"esito": None, "frase": "", "tipo": "nessuna formula trovata"}


def carica_anagrafica(percorso: Path) -> dict:
    """Mappa AIC -> (denominazione, principio attivo) per arricchire le proposte."""
    raw = percorso.read_bytes()
    for enc in ("utf-8-sig", "utf-8", "latin-1"):
        try:
            testo = raw.decode(enc); break
        except UnicodeDecodeError:
            continue
    try:
        sep = csv.Sniffer().sniff(testo[:4096], delimiters=";,\t|").delimiter
    except csv.Error:
        sep = ";"
    reader = csv.DictReader(io.StringIO(testo), delimiter=sep)
    norm_h = {normalizza(h): h for h in (reader.fieldnames or [])}
    def trova(cands):
        for c in cands:
            for k, o in norm_h.items():
                if c in k:
                    return o
        return None
    c_aic = trova(["codice aic", "aic"])
    c_nome = trova(["denominazione", "medicinale", "farmaco"])
    c_pa = trova(["principio attivo", "principio_attivo"])
    mappa = {}
    if c_aic:
        for r in reader:
            aic = (r.get(c_aic) or "").strip()
            if aic:
                mappa[aic] = ((r.get(c_nome) or "").strip() if c_nome else "",
                              (r.get(c_pa) or "").strip() if c_pa else "")
    return mappa


def main():
    if len(sys.argv) < 2:
        print(__doc__); sys.exit(1)

    cartella = Path(sys.argv[1])
    anagrafica = {}
    args = sys.argv[2:]
    for i, a in enumerate(args):
        if a == "--anagrafica" and i + 1 < len(args):
            anagrafica = carica_anagrafica(Path(args[i + 1]))

    pdfs = sorted(cartella.glob("*.pdf"))
    if not pdfs:
        print(f"Nessun PDF in {cartella}"); sys.exit(2)

    proposte_pa, proposte_aic, senza_esito = [], [], []
    for pdf in pdfs:
        aic = re.sub(r"\D", "", pdf.stem)  # AIC dal nome file
        try:
            testo = estrai_testo(pdf)
        except Exception as e:
            senza_esito.append(f"{pdf.name}: errore lettura ({e})")
            continue
        r = analizza(testo)
        nome, pa = anagrafica.get(aic, ("", ""))
        if r["esito"] is None:
            senza_esito.append(f"{pdf.name}: {r['tipo']}")
            continue
        voce = {
            "aic": aic,
            "_farmaco": nome,
            "_principio_attivo": pa,
            "stato": r["esito"],
            "motivo": ("Triturabilità confermata dall'RCP"
                       if r["esito"] == "verde"
                       else "Frantumazione controindicata dall'RCP"),
            "nota": "PROPOSTA AUTOMATICA DA REVISIONARE. Frase trovata nell'RCP: "
                    f"«{r['frase']}»",
            "_da_verificare": True,
        }
        proposte_aic.append(voce)

    out = {
        "_avviso": "PROPOSTE AUTOMATICHE NON VERIFICATE. Revisionare ogni voce "
                   "leggendo l'RCP prima di spostarla in override.json.",
        "per_principio_attivo": proposte_pa,
        "per_aic": proposte_aic,
    }
    Path("override_proposto.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")

    verdi = sum(1 for v in proposte_aic if v["stato"] == "verde")
    rossi = sum(1 for v in proposte_aic if v["stato"] == "rosso")
    report = (
        f"PDF analizzati: {len(pdfs)}\n"
        f"Proposte verde (triturabilità esplicita): {verdi}\n"
        f"Proposte rosso (controindicazione esplicita): {rossi}\n"
        f"Senza esito chiaro (da controllare a mano): {len(senza_esito)}\n\n"
        + "\n".join(senza_esito)
    )
    Path("rcp_report.txt").write_text(report, encoding="utf-8")
    print(report)
    print("\nScritto: override_proposto.json  (DA REVISIONARE)")


if __name__ == "__main__":
    main()
