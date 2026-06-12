# Frantumabile?

Web app mobile-first per verificare se un farmaco orale è idoneo alla
frantumazione nel paziente disfagico. Cerca per nome commerciale o principio
attivo e restituisce un verdetto a semaforo con la motivazione clinica e,
per i farmaci non idonei, le alternative della stessa categoria.

Strumento di supporto per uso professionale. Non sostituisce il giudizio
clinico né l'RCP della specialità.

## Principio di funzionamento

L'app applica una logica **fail-safe**: un farmaco è verde solo quando
l'idoneità è certa.

- **IDONEO (verde)** — solo se l'idoneità è documentata: forme
  orodispersibili, liquide, dispersibili/solubili, effervescenti (leggibili
  dalla descrizione della confezione), oppure compresse la cui triturabilità
  è stata verificata sull'RCP e inserita in `override.json`.
- **NON IDONEO (rosso)** — tutto il resto. La motivazione distingue due casi:
  - *controindicato* (gastroresistente, rilascio modificato, sublinguale,
    rischio per l'operatore): non frantumare mai;
  - *non confermato* (compressa o capsula semplice): verificare l'RCP e, se
    conferma la triturabilità, aggiungere a `override.json`.

Per ogni farmaco rosso, l'app suggerisce alternative verdi:

1. **stesso principio attivo** in forma idonea (cambio di formulazione);
2. **stessa classe ATC (3° livello)** con molecola diversa, accompagnata
   dall'avviso che non è una sostituzione automatica.

I dati di partenza vengono dagli Open Data AIFA, che contengono la
descrizione della confezione ma **non** la triturabilità: quest'ultima sta
solo nell'RCP. Di conseguenza, con i soli dati di anagrafica la maggioranza
delle compresse risulta rossa finché non si popola `override.json`.

## Struttura del repository

```
.
├── index.html                  App pubblicata (GitHub Pages serve questo)
├── farmaci.json                Database generato (output dello script)
├── override.json               Eccezioni verificate a mano sull'RCP
├── genera_database.py          Anagrafica AIFA → farmaci.json
├── estrai_override_da_rcp.py   PDF degli RCP → override_proposto.json
└── README.md
```

Solo `index.html` e `farmaci.json` sono necessari alla pubblicazione; gli
script e gli altri file servono alla manutenzione del database.

## Flusso operativo

### 1. Scaricare l'anagrafica AIFA

Dagli Open Data AIFA, scaricare il file dell'anagrafica dei farmaci /
confezioni in formato CSV. Verificare che contenga la colonna del codice ATC
(serve per le alternative di classe).

### 2. Generare il database

```bash
python3 genera_database.py anagrafica_aifa.csv --override override.json
```

Produce `farmaci.json` (usato dall'app) e `report.txt` con le statistiche di
classificazione. Lo script rileva da solo encoding, separatore e nomi delle
colonne, scarta revocati/sospesi ed esclude le forme non orali.

### 3. (Opzionale) Estrarre proposte di override dagli RCP

Per portare a verde le compresse triturabili senza inserirle una a una:

```bash
pip install pdfplumber
# cartella di PDF nominati con l'AIC, es. 032657028.pdf
python3 estrai_override_da_rcp.py ./rcp_pdf/ --anagrafica anagrafica_aifa.csv
```

Produce `override_proposto.json` con, per ogni voce, la frase trovata
nell'RCP come evidenza. **L'analisi è euristica: ogni proposta va revisionata
a mano prima dell'uso.** Le voci valide si spostano in `override.json`.

### 4. Rigenerare e pubblicare

Dopo ogni modifica a `override.json`, rieseguire lo step 2 e aggiornare
`farmaci.json` nel repository.

## Pubblicazione su GitHub Pages

1. Creare il repository (es. `frantumabile`) e caricare i file.
2. Nelle impostazioni del repository: **Settings → Pages**.
3. In *Build and deployment*, sorgente **Deploy from a branch**, branch
   `main`, cartella `/ (root)`, salvare.
4. Dopo qualche minuto l'app è online su
   `https://aurex-cmd.github.io/frantumabile`.

L'app è completamente statica: nessun server, nessun costo, funziona offline
una volta caricata. Se `farmaci.json` non è presente, mostra un dataset
dimostrativo con un banner di avviso.

## Manutenzione di `override.json`

Il file ha due sezioni; gli override per AIC hanno priorità su quelli per
principio attivo. Stati ammessi: `verde`, `giallo`, `rosso`. Il campo
`alternativa` è facoltativo.

```json
{
  "per_principio_attivo": [
    {
      "principio_attivo": "lansoprazolo",
      "stato": "giallo",
      "motivo": "Capsule con microgranuli gastroresistenti",
      "nota": "La capsula può essere aperta ma i granuli NON vanno triturati.",
      "alternativa": "Formulazione orodispersibile dello stesso principio attivo"
    }
  ],
  "per_aic": [
    {
      "aic": "039021015",
      "stato": "verde",
      "motivo": "Frantumazione confermata dall'RCP",
      "nota": "RCP: triturazione e sospensione in acqua descritte, anche per sondino."
    }
  ]
}
```

Ogni voce verde dovrebbe corrispondere a una verifica reale sull'RCP
(Banca Dati Farmaci AIFA, sezioni 4.2 e 6.6).

## Avvertenze cliniche

- Lo strumento è di supporto e non sostituisce il giudizio clinico né il
  parere del farmacista.
- In caso di dubbio fa fede l'RCP della specialità in uso.
- Le alternative di classe non sono sostituzioni automatiche: dose,
  indicazione, interazioni e controindicazioni vanno sempre verificate.
- Anche per i farmaci idonei in forma liquida, adeguare la consistenza al
  grado di disfagia (i liquidi fluidi possono comportare rischio di
  aspirazione).
