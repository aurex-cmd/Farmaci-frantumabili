#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
genera_database.py
==================
Converte l'anagrafica AIFA dei farmaci autorizzati in un database JSON
classificato per frantumabilità (uso nel paziente disfagico).

USO:
    python3 genera_database.py anagrafica_aifa.csv
    python3 genera_database.py anagrafica_aifa.csv --override override.json --out farmaci.json

INPUT:
    CSV scaricato dagli Open Data AIFA (anagrafica farmaci / confezioni).
    Lo script rileva automaticamente separatore, encoding e colonne
    (denominazione, principio attivo, AIC, descrizione confezione/forma).

OUTPUT:
    farmaci.json  -> usato dall'app web
    report.txt    -> statistiche di classificazione e casi dubbi

LOGICA FAIL-SAFE (verde solo se l'idoneità è CERTA):
    Sono VERDE esclusivamente le forme la cui idoneità nel disfagico è
    leggibile con certezza dalla descrizione della confezione AIFA:
      - orodispersibili / orosolubili
      - dispersibili / solubili / effervescenti
      - forme liquide (sciroppo, gocce, soluzione/sospensione orale,
        granulato o bustine per soluzione/sospensione orale)
    TUTTO IL RESTO è ROSSO (comprese le compresse semplici): l'anagrafica
    non contiene la frantumabilità, che sta solo nell'RCP.

    Il ROSSO porta sempre una motivazione che distingue due casi:
      - controindicato in assoluto (gastroresistente, rilascio modificato,
        sublinguale, rischio operatore) -> "non frantumare mai";
      - non confermato (compressa/capsula semplice) -> "verificare l'RCP;
        se conferma la triturabilità, aggiungere a override".

    Gli override (per principio attivo o AIC) hanno priorità assoluta:
    sono il SOLO modo per portare a verde una compressa triturabile, dopo
    aver letto l'RCP. Possono assegnare qualsiasi stato (verde/giallo/rosso).
"""

import csv
import io
import json
import re
import sys
import unicodedata
from collections import Counter
from pathlib import Path

# ----------------------------------------------------------------------------
# 1. PRINCIPI ATTIVI A RISCHIO OPERATORE (citotossici, teratogeni, ormonali)
#    Lista di partenza, da estendere secondo il prontuario locale.
# ----------------------------------------------------------------------------
PRINCIPI_RISCHIO_OPERATORE = [
    "finasteride", "dutasteride", "metotrexato", "methotrexate",
    "talidomide", "lenalidomide", "pomalidomide",
    "micofenolato", "micofenolico",
    "ciclofosfamide", "clorambucile", "melfalan", "idrossiurea", "idrossicarbamide",
    "capecitabina", "mercaptopurina", "tioguanina", "azatioprina",
    "tamoxifene", "anastrozolo", "letrozolo", "exemestane", "bicalutamide",
    "abiraterone", "enzalutamide",
    "isotretinoina", "acitretina", "alitretinoina",
    "valganciclovir", "ganciclovir",
    "colchicina",  # margine terapeutico stretto: la perdita di polvere altera la dose
]

# ----------------------------------------------------------------------------
# 2. PATTERN SULLE FORME FARMACEUTICHE
#    I testi AIFA sono in maiuscolo e usano molte abbreviazioni:
#    CPR=compresse, CPS=capsule, RIV=rivestite, GASTRORES=gastroresistenti,
#    RP=rilascio prolungato, RM=rilascio modificato, ORODISPERS, EFFERV, ...
# ----------------------------------------------------------------------------

# Forme non orali -> escluse dal database (non pertinenti per la frantumazione)
NON_ORALI = re.compile(
    r"\b(FIALE?|FL?\b|EV\b|IM\b|SC\b|INIETT|INFUS|SIRINGA|PENNA|"
    r"CREMA|POMATA|UNGUENTO|GEL\b|SCHIUMA|LOZIONE|"
    r"SUPPOSTE?|OVULI?|CANDELETTE|RETT|VAG|"
    r"COLLIRIO|OFT|GTT\s*OCUL|AURIC|OTOL|"
    r"CEROTT[OI]|TRANSDERM|TTS\b|"
    r"SPRAY\s*NAS|NEBUL|INALAT|AEROSOL|POLV\s*INAL|"
    r"COLLUT|SCIACQUI|GARZE?|MEDICAZ)\b", re.I)

# Gastroresistenti
GASTRO = re.compile(r"GASTRORES|GASTRO[\s\-]?RES|GASTROPROT|ENTERIC", re.I)

# Rilascio modificato/prolungato: parole intere e sigle come token isolati
RILASCIO_ESTESO = re.compile(
    r"RILASCIO\s+(PROLUNGATO|MODIFICATO|CONTROLLATO|RITARDATO)|"
    r"\b(RP|RM|SR|XR|XL|ER|LP|LA)\b|"
    r"\bRETARD\b|\bCRONO\b|\bCHRONO\b|AZIONE\s+PROLUNGATA", re.I)

# Sublinguali / buccali
SUBLINGUALE = re.compile(r"SUBLING|SUBL\b|BUCCAL|OROMUCOS", re.I)

# --- FORME VERDI: idoneità certa dalla descrizione della confezione ---
# Si dissolvono in bocca (idonee al disfagico per definizione)
VERDE_ORODISP = re.compile(r"ORODISPERS|ORODISP|OROSOLUB|VELOTAB", re.I)
# Forme liquide o che diventano liquide/disperse in acqua
VERDE_LIQUIDE = re.compile(
    r"\bDISPERS|\bSOLUB|EFFERV|SCIROPPO|\bSCIR\b|"
    r"SOSP(ENSIONE)?\b|SOLUZ|\bGOCCE\b|\bGTT\b|"
    r"BUST(INE)?\b|GRANUL|\bGRAT\b|POLV(ERE)?\s+(OS|ORALE|PER\s+SOL)", re.I)

# Compresse rivestite (a rilascio immediato)
CPR_RIVESTITE = re.compile(r"(CPR|COMPRESSE?)\s+(RIV|FILM[\s\-]?RIV|RIVESTITE)", re.I)

# Compresse semplici
COMPRESSE = re.compile(r"\bCPR\b|COMPRESSE?", re.I)

# Capsule molli
CPS_MOLLI = re.compile(r"(CPS|CAPSULE?)\s+MOLL[EI]", re.I)

# Capsule rigide / capsule generiche
CAPSULE = re.compile(r"\bCPS\b|CAPSULE?", re.I)


def normalizza(testo: str) -> str:
    """Minuscole, senza accenti: per confronti robusti sui principi attivi."""
    t = unicodedata.normalize("NFD", testo or "")
    return "".join(c for c in t if unicodedata.category(c) != "Mn").lower().strip()


def classifica(forma: str, principio: str) -> dict | None:
    """Logica fail-safe: VERDE solo se l'idoneità è certa dalla confezione.
    Tutto il resto è ROSSO, con motivazione che distingue
    'controindicato' da 'non confermato'. None se la forma non è orale."""
    f = forma or ""
    p = normalizza(principio)

    if NON_ORALI.search(f):
        return None

    # ---------- VERDE: idoneità certa dalla descrizione confezione ----------
    if VERDE_ORODISP.search(f):
        return {
            "stato": "verde",
            "motivo": "Forma orodispersibile",
            "nota": "Si dissolve in bocca: idonea al paziente disfagico senza "
                    "frantumazione. Verificare la consistenza adatta al grado di disfagia.",
        }

    if VERDE_LIQUIDE.search(f):
        return {
            "stato": "verde",
            "motivo": "Forma liquida/dispersibile",
            "nota": "Somministrabile senza frantumare. ATTENZIONE: i liquidi fluidi "
                    "possono richiedere addensante nel disfagico (rischio di aspirazione). "
                    "Adeguare la consistenza al grado di disfagia.",
        }

    # ---------- ROSSO controindicato in assoluto ----------
    for attivo in PRINCIPI_RISCHIO_OPERATORE:
        if attivo in p:
            return {
                "stato": "rosso",
                "motivo": "Principio attivo a rischio per l'operatore",
                "nota": "Non frantumare mai: rischio di esposizione per chi prepara "
                        "(citotossico/teratogeno) o di alterazione di una dose critica.",
            }

    if GASTRO.search(f):
        return {
            "stato": "rosso",
            "motivo": "Forma gastroresistente",
            "nota": "Non frantumare mai: la triturazione distrugge il rivestimento "
                    "enterico (inattivazione del farmaco o lesione gastrica).",
        }

    if RILASCIO_ESTESO.search(f):
        return {
            "stato": "rosso",
            "motivo": "Rilascio prolungato/modificato",
            "nota": "Non frantumare mai: rischio di dose dumping (rilascio immediato "
                    "dell'intera dose) e tossicità.",
        }

    if SUBLINGUALE.search(f):
        return {
            "stato": "rosso",
            "motivo": "Forma sublinguale/buccale",
            "nota": "Non frantumare: la via sublinguale è spesso già praticabile nel "
                    "disfagico. Verificare l'RCP.",
        }

    # ---------- ROSSO non confermato (compresse/capsule semplici) ----------
    if CPR_RIVESTITE.search(f):
        return {
            "stato": "rosso",
            "motivo": "Compressa rivestita — frantumabilità non confermata",
            "nota": "Il film può essere funzionale o solo estetico. Verificare l'RCP "
                    "(sez. 4.2 e 6.6): se conferma la triturabilità, aggiungere a override.",
        }

    if CPS_MOLLI.search(f):
        return {
            "stato": "rosso",
            "motivo": "Capsula molle — non frantumabile",
            "nota": "L'estrazione del contenuto liquido è possibile solo per alcuni "
                    "farmaci. Verificare l'RCP; in alternativa cercare un'altra formulazione.",
        }

    if CAPSULE.search(f):
        return {
            "stato": "rosso",
            "motivo": "Capsula — contenuto non confermato",
            "nota": "Il contenuto può essere in pellet gastroprotetti o a rilascio "
                    "modificato. Verificare l'RCP: se l'apertura è consentita, aggiungere a override.",
        }

    if COMPRESSE.search(f):
        return {
            "stato": "rosso",
            "motivo": "Compressa — frantumabilità non confermata dall'RCP",
            "nota": "Non confermata dai dati di anagrafica (che non riportano la "
                    "triturabilità). Verificare l'RCP (sez. 4.2 e 6.6): se conferma la "
                    "triturabilità, aggiungere a override per portarla a verde.",
        }

    return {
        "stato": "rosso",
        "motivo": "Forma non classificata",
        "nota": "Idoneità non determinabile dai dati di anagrafica. Verificare l'RCP "
                "sulla Banca Dati Farmaci AIFA.",
    }


# ----------------------------------------------------------------------------
# 3. LETTURA ROBUSTA DEL CSV AIFA
# ----------------------------------------------------------------------------
def apri_csv(percorso: Path):
    """Prova encoding e separatori comuni nei dataset AIFA."""
    raw = percorso.read_bytes()
    for enc in ("utf-8-sig", "utf-8", "latin-1"):
        try:
            testo = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    try:
        dialetto = csv.Sniffer().sniff(testo[:4096], delimiters=";,\t|")
        sep = dialetto.delimiter
    except csv.Error:
        sep = ";"
    return csv.DictReader(io.StringIO(testo), delimiter=sep)


def trova_colonna(intestazioni, candidati):
    """Trova la colonna giusta confrontando i nomi normalizzati."""
    norm = {normalizza(h): h for h in intestazioni}
    for cand in candidati:
        for chiave, originale in norm.items():
            if cand in chiave:
                return originale
    return None


COLONNE = {
    "nome":      ["denominazione", "farmaco", "medicinale", "nome commerciale"],
    "principio": ["principio attivo", "principio_attivo", "principi attivi"],
    "aic":       ["codice aic", "aic", "codice_aic", "cod aic"],
    "forma":     ["descrizione confezione", "confezione", "forma farmaceutica",
                  "forma_farmaceutica", "descrizione", "desc confezione"],
    "stato_amm": ["stato amministrativo", "stato_amministrativo", "stato"],
    "atc":       ["codice atc", "cod atc", "atc"],
}

# Un codice ATC valido inizia con lettera + 2 cifre + lettera (almeno 3° livello)
ATC_RE = re.compile(r"^[A-Z]\d{2}[A-Z]", re.I)


# ----------------------------------------------------------------------------
# 4. MAIN
# ----------------------------------------------------------------------------
def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    csv_path = Path(sys.argv[1])
    out_path = Path("farmaci.json")
    override_path = None
    args = sys.argv[2:]
    for i, a in enumerate(args):
        if a == "--out" and i + 1 < len(args):
            out_path = Path(args[i + 1])
        if a == "--override" and i + 1 < len(args):
            override_path = Path(args[i + 1])

    # Override curati a mano (priorità assoluta)
    override_pa, override_aic = {}, {}
    if override_path and override_path.exists():
        dati = json.loads(override_path.read_text(encoding="utf-8"))
        for voce in dati.get("per_principio_attivo", []):
            override_pa[normalizza(voce["principio_attivo"])] = voce
        for voce in dati.get("per_aic", []):
            override_aic[str(voce["aic"]).strip()] = voce
        print(f"Override caricati: {len(override_pa)} per principio attivo, "
              f"{len(override_aic)} per AIC")

    reader = apri_csv(csv_path)
    intestazioni = reader.fieldnames or []
    col = {k: trova_colonna(intestazioni, v) for k, v in COLONNE.items()}
    print("Colonne rilevate:", {k: v for k, v in col.items() if v})
    if not col["nome"] or not col["forma"]:
        print("ERRORE: non trovo le colonne con nome del farmaco e/o "
              "descrizione confezione. Intestazioni presenti:", intestazioni)
        sys.exit(2)

    farmaci, conteggio, esclusi, fonti_override = [], Counter(), 0, 0
    for riga in reader:
        nome = (riga.get(col["nome"]) or "").strip()
        if not nome:
            continue
        # Salta revocati/sospesi se la colonna esiste
        if col["stato_amm"]:
            stato_amm = normalizza(riga.get(col["stato_amm"]) or "")
            if any(s in stato_amm for s in ("revocat", "sospes", "decadut")):
                continue

        forma = (riga.get(col["forma"]) or "").strip()
        principio = (riga.get(col["principio"]) or "").strip() if col["principio"] else ""
        aic = (riga.get(col["aic"]) or "").strip() if col["aic"] else ""
        atc = (riga.get(col["atc"]) or "").strip().upper() if col["atc"] else ""
        if atc and not ATC_RE.match(atc):
            atc = ""  # scarta valori che non sono codici ATC (es. descrizioni)

        esito = classifica(forma, principio)
        if esito is None:
            esclusi += 1
            continue

        # Override: prima per AIC (più specifico), poi per principio attivo
        ov = override_aic.get(aic) or override_pa.get(normalizza(principio))
        if ov:
            esito = {"stato": ov["stato"], "motivo": ov["motivo"],
                     "nota": ov.get("nota", "")}
            if ov.get("alternativa"):
                esito["alternativa"] = ov["alternativa"]
            esito["verificato"] = True
            fonti_override += 1

        conteggio[esito["stato"]] += 1
        farmaci.append({
            "n": nome, "p": principio, "a": aic, "f": forma,
            "s": esito["stato"], "m": esito["motivo"], "x": esito["nota"],
            **({"atc": atc} if atc else {}),
            **({"alt": esito["alternativa"]} if esito.get("alternativa") else {}),
            **({"v": 1} if esito.get("verificato") else {}),
        })

    out_path.write_text(json.dumps(
        {"generato": True, "voci": farmaci}, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8")

    report = (
        f"Voci orali classificate: {len(farmaci)}\n"
        f"  VERDE:  {conteggio['verde']}\n"
        f"  GIALLO: {conteggio['giallo']}\n"
        f"  ROSSO:  {conteggio['rosso']}\n"
        f"Forme non orali escluse: {esclusi}\n"
        f"Voci coperte da override verificati: {fonti_override}\n"
    )
    Path("report.txt").write_text(report, encoding="utf-8")
    print(report)
    print(f"Database scritto in: {out_path}")


if __name__ == "__main__":
    main()
