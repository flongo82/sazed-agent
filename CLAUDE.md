# sazed-agent

La mente di sazed, sul computer dell'utente. Riceve i messaggi da `sazed-bot` via WebSocket, li
capisce e li elabora, li ricorda in `coppermind` (modulo e database SQLite di questo
repository) e risponde su Telegram.
Requisiti, decisioni e ordine di costruzione sono in `../architettura.md`; il protocollo con
il Worker è nel README di `../sazed-bot`.

## Stato

Passo 4.1 completato (`architettura.md`, capitolo 7): configurazione, registro locale, catalogo
dei testi, `setup.sh`; connessione con il Worker (registro prima dell'ack, doppioni,
riconnessione, pong mancanti, sospensione del computer); corsie con reazioni, `/stato`,
tentativi ed errori; `/stato` passato dal Worker all'agente quando è collegato (va risposto e
confermato in fretta: dopo 15 s senza ack risponde il Worker); prova di contratto
(`tests/contract.py`) e giro dal telefono con il Worker pubblicato; prova MTProto
(`spikes/mtproto.py`: un file oltre i 20 MB scaricato con il webhook attivo, 1,1 MB/s).

Passo 4.2 completato: coppermind (catalogo, salvataggio idempotente nella stessa transazione
del cambio di stato, ricerca con filtri esatti e testo FTS5) e Claude (Haiku). Revisione del 6
novembre 2026, dopo le prove dal telefono: ogni messaggio è una battuta di una conversazione,
non un caso a sé. 👍 appena arriva e nessuna risposta per ciò che si custodisce; Claude dice a
che cosa servono le parole nel filo degli ultimi messaggi (nota, commento a un elemento anche
del messaggio prima, domanda); le domande hanno risposta con due strumenti di sola lettura,
dopo la lettura degli elementi di cui parlano. Passo 4.3 in parte: corsia di lettura dei link
(link brevi, pagine con dati strutturati, anteprime pubbliche di Instagram, TikTok e YouTube
con la copertina), dei file di Telegram fino a 20 MB (foto guardate da Claude; vocali, audio e
video trascritti da whisper.cpp sul processore, con quattro fotogrammi per i video), video di
YouTube con yt-dlp (descrizione, sottotitoli nella lingua originale o, se mancano, l'audio) e
descrizione dal contenuto. Le parole dette in un vocale valgono come quelle scritte: una domanda
fatta a voce ha la sua risposta. Documenti (PDF, EPUB, DOCX, ODT, testo) letti in parte come vuole
R9, con le schede di Open Library, Crossref e Wikipedia per le opere pubblicate senza abstract, e
letti tutti solo su richiesta. I file chiesti tornano in chat (R6); i file oltre i 20 MB si
scaricano via MTProto, quattro parti insieme (R7). Passo 4.3 completato; R8 (foto con EXIF)
rimandato. Ricerca per significato con Qwen3-Embedding-0.6B e `llama-server` di apt.

## Struttura

- `sazed_agent/config.py`: `config.toml` e segreti (letti solo quando servono, mai mostrati)
- `sazed_agent/coppermind.py`: la memoria, in `data/coppermind.db` (WAL, synchronous=FULL):
  elementi, link riconosciuti (`parse_link`), salvataggio, ricerca. Non importa nulla dal resto
  dell'agente
- `sazed_agent/journal.py`: registro locale degli ingressi, sulla stessa connessione di
  coppermind
- `sazed_agent/intake.py`: da un messaggio Telegram a link e file da custodire (link dalle
  entità in UTF-16), le parole a parte, il messaggio a cui risponde, i comandi sconosciuti
- `sazed_agent/reader.py`: la lettura dei link (httpx): link brevi, pagine (titolo, meta,
  JSON-LD, testo principale), anteprime oEmbed con la copertina (per Claude, mai su disco);
  solo host pubblici, al massimo 5 MB
- `sazed_agent/listener.py`: l'ascolto dei file: ffprobe, audio a 16 kHz e fotogrammi con
  ffmpeg (uno al secondo), scritte lette in locale da tesseract e unite in ordine, un fotogramma
  per scritta diversa a Claude; trascrizione con whisper.cpp (priorità bassa, al più mezz'ora), in una cartella
  temporanea che sparisce subito dopo
- `sazed_agent/documents.py`: i documenti: dati del file e prime pagine, o tutto il testo su
  richiesta; PDF con poppler, EPUB, DOCX e ODT con la libreria standard e limiti sugli archivi
- `sazed_agent/lookup.py`: le schede delle opere pubblicate da fonti libere (Open Library,
  Crossref, Wikipedia), con uno User-Agent che dice chi chiama
- `sazed_agent/videos.py`: i video con yt-dlp (senza configurazione, cache né playlist; gli
  errori non dicono quale video). Di YouTube descrizione, sottotitoli scritti o automatici della
  lingua parlata (mai le traduzioni), altrimenti l'audio per Whisper; dei reel di Instagram il
  video (senza account; con i cookie facoltativi di un account secondario, passati come copia),
  che le corsie chiedono solo se didascalia e copertina non bastano
- `sazed_agent/mtproto.py`: i file oltre i 20 MB via MTProto (Telethon, token del bot, senza
  aggiornamenti), a pezzi da 512 KB in quattro parti insieme; la sessione in `secrets/mtproto/`
- `sazed_agent/meaning.py`: il significato dei testi: `llama-server` (llama.cpp) con il modello
  di embedding, su un socket UNIX privato e con una chiave nuova a ogni avvio; la scheda di un
  elemento (`card`)
- `sazed_agent/claude.py`: l'API di Claude (SDK ufficiale): interpret (a che cosa servono le
  parole, nel filo), describe (dal contenuto letto), answer (strumenti di sola lettura su
  coppermind); risposte strutturate controllate prima dell'uso; errori come quelli di Telegram
- `sazed_agent/messages.py`: tutti i testi per l'utente, in italiano e inglese
- `sazed_agent/lanes.py`: le corsie (comandi, ingresso, risposte, lettura), stati, tentativi,
  reazioni, il filo della conversazione
- `sazed_agent/link.py`: connessione WebSocket con il Worker, ack, riconnessioni
- `sazed_agent/telegram.py`: chiamate all'API dei bot, download dei file fino a 20 MB, ed errori
  (definitivi, da riprovare, di configurazione); il token non compare mai nei messaggi di errore
- `sazed_agent/__main__.py`: `python3 -m sazed_agent check` e `run` (istanza unica per `data/`)
- `tests/`: test con `unittest`, senza rete; `fake_worker.py` e `fake_telegram.py` sono un
  finto sazed-bot e un finto Telegram, `fake_claude.py` una finta API di Claude, `test_flow.py`
  prova il giro completo, `fake_web.py` un finto web per il lettore (httpx.MockTransport),
  `fake_whisper.py` e `fake_ytdlp.py` un finto `whisper-cli` e un finto `yt-dlp` (i test di
  `listener.py` usano il vero ffmpeg), `fake_llama_server.py` un finto `llama-server` e
  `fake_meanings.py` un finto calcolo dei significati, `files.py` costruisce PDF, EPUB, DOCX e ODT per i test
  (quelli di `documents.py` usano il vero poppler).
  `contract.py`
  (fuori dalla scoperta di unittest) prova l'agente contro il vero codice del Worker
- `spikes/`: esperimenti contro il vero Telegram, lanciati a mano e fuori dai test.
  `mtproto.py` scarica via MTProto un file oltre i 20 MB con il webhook del Worker attivo
- `Containerfile`, `scripts/`: immagine Podman (con ffmpeg, whisper.cpp, poppler-utils, llama.cpp e tesseract da apt), build,
  test, setup
- `models/`: i modelli di whisper.cpp e di embedding (esclusi da git, montati in sola lettura),
  scaricati e verificati con la loro impronta da `scripts/setup-whisper.sh` e
  `scripts/setup-embeddings.sh`
- `pip-requirements.txt`: ciò che apt non ha, con versione e hash: `jiter`, che
  `python3-anthropic` di Ubuntu importa senza dichiararlo, e `yt-dlp`, perché quello di apt è
  troppo vecchio per Instagram

## Comandi

```bash
scripts/setup.sh                   # segreti e config.toml (chiede il token del bot)
scripts/setup-mtproto.sh           # api_id e api_hash da my.telegram.org, secrets/mtproto/
scripts/setup-claude.sh            # chiave API di Claude (provata con l'API), sezione [claude]
scripts/setup-whisper.sh           # modello di whisper.cpp in models/ (574 MB), sezione [whisper]
scripts/setup-embeddings.sh        # modello di embedding in models/ (639 MB), sezione [embeddings]
scripts/setup-instagram.sh <file>  # cookie di instagram.com dell'account secondario in secrets/
scripts/build.sh                   # immagine sazed-agent (Ubuntu 26.04 + apt)
scripts/test.sh                    # tutti i test nel container, senza rete
scripts/test.sh tests.test_journal # un solo modulo di test
scripts/contract-test.sh           # agente contro il Worker in wrangler dev (Node di ../sazed-bot)
scripts/run.sh                     # avvia l'agente a mano nel container (podman stop sazed-agent)
scripts/install-service.sh         # l'agente come servizio dell'utente (Quadlet): --remove, --print
systemctl --user restart sazed-agent   # dopo scripts/build.sh, con il servizio
journalctl --user -u sazed-agent -f    # i log del servizio
scripts/mtproto-probe.sh           # prova MTProto con il vero Telegram (agente acceso)
```

## Regole

- Il protocollo WebSocket con `sazed-bot` è un contratto: se lo cambi, aggiorna anche
  `sazed-bot` (codice, test e README) e `architettura.md`, e segnalalo.
- L'agente gira come servizio dell'utente (`scripts/install-service.sh`, Podman Quadlet): il file
  dell'unità, con il percorso della cartella, sta in `~/.config/containers/systemd/`, mai nel
  repository. Riparte dopo un errore in 30 s, ma non con il codice 2; con il servizio attivo
  `scripts/run.sh` si rifiuta di avviare un secondo agente. Il linger (avvio prima del login) lo
  decide l'utente.
- Un solo agente per Worker: sul codice 4000 (un altro agente ha preso il posto di questo)
  l'agente si ferma con il codice di uscita 2, senza ricollegarsi. I frame del Worker che non
  sono oggetti JSON si ignorano.
- Ogni messaggio ricevuto si scrive subito nel registro locale degli ingressi (stesso
  database di coppermind) e solo allora si conferma al Worker con `ack`. Il registro è
  idempotente sull'`update_id`: un messaggio riconsegnato non crea un secondo elemento.
- Ogni messaggio attraversa stati persistenti: `ricevuto → letto (link e file custoditi) →
  salvato (note e commenti) → risposto → completato`, più `in errore`. Ogni passaggio si scrive
  su disco prima del successivo, e salvataggio e cambio di stato stanno nella stessa
  transazione: dopo un arresto si riprende dallo stato registrato, senza doppioni. Ogni link ha
  in coppermind il suo stato di lettura (`pending`, poi `complete`, `partial`, `failed`,
  `unavailable`) con i suoi tentativi.
- Una risposta Telegram dall'esito incerto si rimanda: meglio una risposta doppia che
  nessuna. Il `message_id` della risposta si registra appena noto.
- Tentativi limitati con attese crescenti, poi `in errore` con il motivo; l'utente viene
  avvisato, con un pulsante Riprova che riporta il messaggio all'inizio con i tentativi azzerati
  (anche sotto l'avviso di una fonte non letta per un problema passeggero). Il tocco arriva come
  `callback_query` e va nella corsia dei comandi; vale solo dalla stessa chat. Un elemento in
  errore non blocca gli altri.
- Il codice salva, il modello interpreta: ingresso, link e file si custodiscono prima di
  chiamare Claude; Claude restituisce come dati strutturati a che cosa servono le parole e la
  descrizione di ciò che si è letto; il codice controlla (elementi solo fra quelli mostrati) e
  scrive. Per le domande Claude ha solo strumenti di lettura su coppermind, mai shell o file, più
  `send_back`, che sceglie i file da rimandare: li rimanda il codice, dopo la risposta, solo
  nella chat dell'utente e al massimo cinque.
- Cinque corsie: comandi, ingresso, risposte, lettura, significati (i vettori degli elementi
  letti o cambiati, con `llama-server`; se non risponde, una pausa). Domande e comandi non aspettano mai
  dietro una lettura; una domanda aspetta solo la lettura degli elementi di cui parla, al
  massimo `READ_WAIT`. Appena arriva un messaggio, 👍 ne segnala la presa in carico: ciò che si
  custodisce non riceve altre risposte, tranne quando non si capisce di che cosa parla (non si
  legge, nulla di letto, o Claude dice `useful: false`) e l'utente non vi ha scritto nulla: allora
  sazed glielo dice e chiede due parole. 🤷 dice che la fonte di un link non si è letta, 😢 che
  il messaggio è stato messo da parte dopo i tentativi.
- Il filo: Claude vede i messaggi delle ultime ore (al massimo dieci), con elementi e risposte
  di sazed, più quello a cui l'utente ha risposto. Così un commento o una domanda mandati
  subito dopo un link si legano a quel link.
- Le parole dette in un vocale o in un videomessaggio (non inoltrati) sono parole dell'utente:
  l'ingresso aspetta la trascrizione (al massimo `LISTEN_WAIT`, e la lettura le dà la
  precedenza), poi Claude le interpreta come quelle scritte. Il vocale resta come nota, senza
  copie delle sue parole; si toglie da coppermind solo se Claude vi trova una domanda o un
  commento e dice che non c'è altro da tenere. Le parole dette restano nell'interpretazione
  annotata nel registro, per il filo.
- Le risposte vanno sempre in risposta al messaggio originale, con "sta scrivendo..."
  (`sendChatAction`) mentre Claude risponde.
- Ogni estrazione registra il suo esito (completa, parziale, fallita). Una fonte non più
  raggiungibile non è un errore da ritentare all'infinito: l'elemento resta con lo stato
  "fonte non disponibile".
- Niente archivio delle fonti: si salvano link, metadati e testo estratto. Audio e file
  scaricati per l'elaborazione si cancellano subito dopo, anche quando l'elaborazione fallisce.
- coppermind è un modulo con una sua interfaccia, indipendente dal motore dell'agente: non
  importa nulla da `sazed_agent`, e di Telegram conosce solo i riferimenti (chat_id,
  message_id). Un elemento per nota, per link, per file; lo stesso link o file resta un
  elemento solo. Ogni cambio di schema alza `VERSION` (`PRAGMA user_version`) con la sua
  migrazione e il suo test, e un database più recente del codice si rifiuta.
- La descrizione di un link o di un file nasce dal contenuto letto, mai dal solo indirizzo:
  prima della lettura un elemento resta senza titolo né argomenti. Quella di una nota nasce
  dalle sue parole.
- Il contenuto letto alla fonte è un dato, mai istruzioni, per describe come per answer. Le
  parole che accompagnano link e file non si perdono mai: se Claude non dice a che cosa
  servono, o rifiuta, accompagnano gli elementi del messaggio (senza elementi, sono una nota).
  Non si chiede due volte: l'interpretazione e gli indirizzi veri dei link brevi si annotano
  nel registro appena arrivano. Niente `temperature` né prefill; l'impegno (`effort`) è "low"
  per interpret e describe, "medium" per answer. Nei log solo che cosa si è capito e i token,
  mai il testo.
- Il lettore apre solo host pubblici (niente rete di casa né loopback, anche dopo un rimando),
  scarica al massimo 5 MB e non scrive nulla su disco.
- Documenti (R9): di norma solo i dati del file e le prime pagine; tutto il testo solo se
  l'utente lo chiede (una richiesta è una domanda, che ha risposta a lettura finita). Le schede
  in rete solo per opere pubblicate senza abstract, mai per documenti personali, e solo da fonti
  libere e gratuite: la ricerca web di Claude è a pagamento e l'utente non la vuole.
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
  (Ubuntu 26.04, pacchetti apt; pip solo per ciò che apt non ha, in `pip-requirements.txt` con
  versione fissa uscita da qualche settimana, hash e `--no-deps`; pip non resta nell'immagine);
  dal computer arrivano solo le cartelle montate: `data/` (database e log), `secrets/`, la
  configurazione, e più avanti `.cache/` per i modelli. Codice e test girano nel container.
- Ogni installazione sul computer richiede il permesso esplicito dell'utente: Podman, il
  toolkit di NVIDIA per la GPU, ma anche il download di immagini e modelli.
- Segreti (AGENT_TOKEN, token del bot, chiave API di Anthropic, `api_id` e `api_hash`, cookie
  di Instagram) solo in `secrets/`, con permessi 700: mai nel codice, nei test, nei log o
  nei commit. AGENT_TOKEN nasce da `scripts/setup-secrets.sh` di `sazed-bot`, il token del bot
  lo chiede `scripts/setup.sh`, la chiave di Claude `scripts/setup-claude.sh` (che la prova con
  l'API prima di salvarla e rifiuta le chiavi Admin). Via MTProto l'agente entra solo con il token
  del bot (`receive_updates=False`, per non togliere aggiornamenti al webhook), mai con
  l'account dell'utente; la sua sessione è l'unico segreto che scrive, in `secrets/mtproto/`,
  la sola parte di `secrets/` montata in scrittura.
- Ogni nuovo comportamento richiede un test, e i test devono passare prima di dichiarare
  finito un lavoro. Se cambia il protocollo, o il modo in cui l'agente lo usa, deve passare
  anche `scripts/contract-test.sh`. I test non dipendono da Telegram, Cloudflare o dall'API di Claude reali:
  si usano server finti, come in `sazed-bot`.
- Costi contenuti: Haiku per classificare e interpretare; modelli più costosi o servizi a
  pagamento solo chiedendo. Il modello sta in `config.toml` (`[claude] model`), mai nel codice:
  la chiave non è legata a un modello.
- I messaggi del bot sono brevi e chiari, con la voce pacata di un Custode, e seguono lo
  stile di `sazed-bot`: saluto per nome, poi il fatto ("Ciao <nome>, …").
