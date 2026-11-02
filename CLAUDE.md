# sazed-agent

La mente di sazed, sul computer dell'utente. Riceve i messaggi da `sazed-bot` via WebSocket, li
capisce e li elabora, li ricorda in `coppermind` (modulo e database SQLite di questo
repository) e risponde su Telegram.
Requisiti, decisioni e ordine di costruzione sono in `../architettura.md`; il protocollo con
il Worker è nel README di `../sazed-bot`.

## Stato

Passo 1 in corso (`architettura.md`, capitolo 7). Fatte le prime tre tappe: configurazione,
registro locale, catalogo dei testi, `setup.sh`; connessione con il Worker (registro prima
dell'ack, doppioni, riconnessione, pong mancanti, sospensione del computer); corsie con
reazioni, risposta provvisoria, `/stato`, tentativi ed errori. Dalla quarta tappa il Worker
passa `/stato` all'agente quando è collegato: va risposto e confermato in fretta, perché dopo
15 s senza ack risponde il Worker. Quinta tappa: la prova di contratto (`tests/contract.py`)
fa girare l'agente contro il codice del Worker in `wrangler dev`, e il giro dal telefono con il
Worker pubblicato è riuscito. Prossima: la prova MTProto.

## Struttura

- `sazed_agent/config.py`: `config.toml` e segreti (letti solo quando servono, mai mostrati)
- `sazed_agent/journal.py`: registro locale in `data/coppermind.db` (WAL, synchronous=FULL)
- `sazed_agent/messages.py`: tutti i testi per l'utente, in italiano e inglese
- `sazed_agent/lanes.py`: le due corsie (comandi, acquisizione), stati, tentativi, reazioni
- `sazed_agent/link.py`: connessione WebSocket con il Worker, ack, riconnessioni
- `sazed_agent/telegram.py`: chiamate all'API dei bot ed errori (definitivi, da riprovare, di
  configurazione); il token non compare mai nei messaggi di errore
- `sazed_agent/__main__.py`: `python3 -m sazed_agent check` e `run` (istanza unica per `data/`)
- `tests/`: test con `unittest`, senza rete; `fake_worker.py` e `fake_telegram.py` sono un
  finto sazed-bot e un finto Telegram, `test_flow.py` prova il giro completo. `contract.py`
  (fuori dalla scoperta di unittest) prova l'agente contro il vero codice del Worker
- `Containerfile`, `scripts/`: immagine Podman, build, test, setup

## Comandi

```bash
scripts/setup.sh                   # segreti e config.toml (chiede il token del bot)
scripts/build.sh                   # immagine sazed-agent (Ubuntu 26.04 + apt)
scripts/test.sh                    # tutti i test nel container, senza rete
scripts/test.sh tests.test_journal # un solo modulo di test
scripts/contract-test.sh           # agente contro il Worker in wrangler dev (Node di ../sazed-bot)
scripts/run.sh                     # avvia l'agente nel container (podman stop sazed-agent)
```

## Regole

- Il protocollo WebSocket con `sazed-bot` è un contratto: se lo cambi, aggiorna anche
  `sazed-bot` (codice, test e README) e `architettura.md`, e segnalalo.
- Un solo agente per Worker: sul codice 4000 (un altro agente ha preso il posto di questo)
  l'agente si ferma con il codice di uscita 2, senza ricollegarsi. I frame del Worker che non
  sono oggetti JSON si ignorano.
- Ogni messaggio ricevuto si scrive subito nel registro locale degli ingressi (stesso
  database di coppermind) e solo allora si conferma al Worker con `ack`. Il registro è
  idempotente sull'`update_id`: un messaggio riconsegnato non crea un secondo elemento.
- Ogni elemento attraversa stati persistenti: `ricevuto → letto → salvato → risposto →
  completato`, più `in errore`. Ogni passaggio si scrive su disco prima del successivo, e
  salvataggio e cambio di stato stanno nella stessa transazione: dopo un arresto si riprende
  dallo stato registrato, senza doppioni.
- Una risposta Telegram dall'esito incerto si rimanda: meglio una risposta doppia che
  nessuna. Il `message_id` della risposta si registra appena noto.
- Tentativi limitati con attese crescenti, poi `in errore` con il motivo; l'utente viene
  avvisato e può chiedere di riprovare. Un elemento in errore non blocca gli altri.
- Il codice salva, il modello interpreta: ingresso e riferimenti Telegram si registrano prima
  di chiamare Claude; Claude restituisce intenzione e classificazione come dati strutturati;
  il codice scrive, verifica e solo allora dice "salvato". Per le domande Claude ha solo
  strumenti di lettura, mai shell o file.
- Due corsie: domande e comandi non aspettano dietro un'acquisizione lunga. Appena arriva un
  messaggio, una reazione ne segnala la ricezione.
- Le risposte vanno sempre in risposta al messaggio originale, con "sta scrivendo..."
  (`sendChatAction`) durante l'elaborazione.
- Ogni estrazione registra il suo esito (completa, parziale, fallita). Una fonte non più
  raggiungibile non è un errore da ritentare all'infinito: l'elemento resta con lo stato
  "fonte non disponibile".
- Niente archivio delle fonti: si salvano link, metadati e testo estratto. Audio e file
  scaricati per l'elaborazione si cancellano subito dopo, anche quando l'elaborazione fallisce.
- coppermind è un modulo con una sua interfaccia, indipendente dal motore dell'agente.
- Backup del database a caldo con l'API di backup di SQLite, mai copiando il file in uso.
- Nominatim: al massimo una richiesta al secondo, con un User-Agent esplicito.
- Il repository è pubblico: niente che permetta di raggiungere un'istanza (indirizzo del
  Worker, username del bot, ID utente, account, percorsi della macchina) nei file tracciati,
  nei test o nei commit. I valori dell'istanza stanno in file esclusi da git. Il nome
  dell'autore in licenza e commit invece è voluto.
- Niente valori fissi nel codice: ciò che può cambiare da un'istanza all'altra viene dalla
  configurazione, dai segreti o da Telegram.
- README in inglese, perché chiunque possa riprodurre il progetto.
- Testi rivolti all'utente in un catalogo per lingua (almeno italiano e inglese), mai scritti
  direttamente nel codice; la lingua si sceglie come in `sazed-bot` (`LANGUAGE`: "auto", "it",
  "en"). Le risposte del modello seguono la stessa lingua. Il nome dell'utente arriva da
  Telegram, mai scritto nel codice o nei test.
- L'agente gira in un container Podman senza root. Le dipendenze stanno nell'immagine
  (Ubuntu 26.04, pacchetti apt; pip solo per ciò che apt non ha); dal computer arrivano solo
  le cartelle montate: `data/` (database e log), `secrets/`, la configurazione, e più avanti
  `.cache/` per i modelli. Codice e test girano nel container.
- Ogni installazione sul computer richiede il permesso esplicito dell'utente: Podman, il
  toolkit di NVIDIA per la GPU, ma anche il download di immagini e modelli.
- Segreti (AGENT_TOKEN, token del bot, chiave API di Anthropic, `api_id` e `api_hash`, cookie
  di Instagram) solo in `secrets/`, con permessi 700: mai nel codice, nei test, nei log o
  nei commit. AGENT_TOKEN nasce da `scripts/setup-secrets.sh` di `sazed-bot`; come arriva qui il
  token del bot è ancora da decidere (quello script non lo salva su disco).
- Ogni nuovo comportamento richiede un test, e i test devono passare prima di dichiarare
  finito un lavoro. Se cambia il protocollo, o il modo in cui l'agente lo usa, deve passare
  anche `scripts/contract-test.sh`. I test non dipendono da Telegram, Cloudflare o dall'API di Claude reali:
  si usano server finti, come in `sazed-bot`.
- Costi contenuti: Haiku per classificare e interpretare; modelli più costosi o servizi a
  pagamento solo chiedendo.
- I messaggi del bot sono brevi e chiari, con la voce pacata di un Custode, e seguono lo
  stile di `sazed-bot`: saluto per nome, poi il fatto ("Ciao <nome>, …").
