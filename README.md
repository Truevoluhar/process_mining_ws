# Pot zadeve · process mining z agentom

Samostojen webservice z API in GUI. **Vsa logika process mininga teče pri obstoječem agentu.** Webservice ne razčlenjuje dnevnikov, ne povezuje tabel, ne računa poti, statistike, anomalij ali pragov. Pošlje izvirne datoteke, spremlja opravilo, preveri obliko JSON in veljavnost povezav ter prikaže agentov rezultat. Izvorna koda agenta ostaja nespremenjena.

## Zagon

```bash
cd process_mining_ws
cp .env.example .env  # neobvezno; privzete vrednosti že delujejo za lokalni Docker
# Po potrebi nastavite naslova obstoječih agent-api in workspace-api.
docker compose up -d --build
```

GUI: http://localhost:8087 · API: http://localhost:8087/docs

Storitev ima svoj Docker projekt in podatkovni volumen. V produkciji jo lahko prestavite samostojno; potrebuje le omrežni dostop do obeh obstoječih API-jev. Za HTTPS nastavite `COOKIE_SECURE=true` in `PUBLIC_ORIGIN` na javni izvor. Spremenljivka `PORT` spremeni zunanji port. Uporablja en Uvicorn proces zaradi obnovitve opravila in njihovega spremljanja v ozadju.

## Uporaba

Prijava z obstoječim uporabniškim imenom/geslom agenta ali veljavnim GUID. Naložite en ali več CSV-jev oziroma drugih datotek; dodajte pojasnila, šifrante in znana pravila. Največ 20 datotek, 40 MB na datoteko in 100 MB skupaj. Storitev vsebine ne spreminja in lokalno ne preverja berljivosti formatov. Agent sam določi kodiranje, stolpce, povezovalne ključe in način branja; neberljive ali delno pregledane vire mora izrecno opisati.

Agent išče odstopanja v časih, vrstnem redu, ponovitvah, povratnih zankah, predajah, redkih poteh/prehodih ter izrecnih poslovnih pravilih. Sam izbere in pojasni merila ter primerljive procesne skupine. Brez šifranta kodam ne pripisuje izmišljenih pomenov; brez poslovnih pravil ali dokazljivega zaključka ne razglaša manjkajočih obveznih korakov. Anomalija ni dokaz goljufije.

GUI prikazuje agentove metrike, interaktivni zemljevid, različice poti, ugotovitve z izvornimi sklici in časovnico izbranih zadev. Filtri spreminjajo samo prikaz vrnjenega rezultata. Poročilo zahteva jasno opredelitev obsega analize, za vsak vir prebrane/skupne vrstice (ali `null`) ter omejitve. Prikaz ima zaradi velikosti odgovora izbor največ 20 zadev in 160 dogodkov, 20 različic, 30 vozlišč, 60 povezav ter 25 ugotovitev; agent je pozvan, naj izračune opravi nad celotnimi berljivimi podatki. Te omejitve niso lokalno vzorčenje vhodnih datotek. Pokritost in pravilnost agentovih izračunov zahtevata vsebinsko preverjanje.

## API

Uporabite `Authorization: Bearer <GUID>`. Isti uporabnik ima lahko eno aktivno analizo v tej storitvi. Tuje datoteke in analize niso dostopne.

```bash
# 1. Izvirne datoteke -> dataset_id v odgovoru
curl -H "Authorization: Bearer $AGENT_GUID" \
  -F 'files=@events.csv' -F 'files=@activities.csv' \
  http://localhost:8087/v1/datasets

# 2. Zaženite agenta -> HTTP 202, id in Location
curl -H "Authorization: Bearer $AGENT_GUID" \
  -H 'Content-Type: application/json' \
  -d '{"dataset_id":"ID_IZ_PRVEGA_KLICA","instructions":"activities.csv je šifrant aktivnosti. Preveri dolga čakanja."}' \
  http://localhost:8087/v1/analyses

# 3. Spremljajte status; ob completed je v analysis agentov celoten JSON
curl -H "Authorization: Bearer $AGENT_GUID" \
  http://localhost:8087/v1/analyses/ID_OPRAVILA
```

- `POST /v1/datasets`: multipart `files`, odgovor `id`, seznam datotek.
- `GET /v1/datasets/{id}`: metapodatki lastnega paketa.
- `POST /v1/analyses`: `dataset_id`, neobvezen `instructions`.
- `GET /v1/analyses`: zadnjih 30 analiz uporabnika.
- `GET /v1/analyses/{id}`: `queued/running/completed/failed`, faza, napaka in `analysis` (do konca `null`).
- `GET /v1/schema`: shema poročila; `summary`, `coverage`, `sources`, `metrics`, `nodes`, `edges`, `variants`, `cases`, `findings`, `recommendations`, `limitations`.
- `/api/login`, `/api/me`, `/api/logout`: GUI prijava z lokalnim HttpOnly piškotkom.
- `GET /health`: zdravje tega webservisa; ni preverjanje agenta ali Bifrosta.

Opravilo ni vezano na trajanje HTTP zahteve ali odprt brskalnik. GUI preverja stanje vsake 3 sekunde in ob izgubljeni povezavi ohrani zadnji rezultat ter opozori, da trenutno stanje ni potrjeno. SQLite ohrani datoteke, sejo in ID-je opravila/threada/runa; ponovni zagon tega servisa nadaljuje spremljanje. Izgubljen odgovor ob zagonu se preverja prek aktivnega runa/transkripta brez slepega ponovnega zagona. Če agent ponovni zagon potrjeno izgubi, je opravilo označeno kot neuspešno. Nejasen sprejem ostane v preverjanju; ne prikazuje lažnega uspeha ali samodejno pošilja iste analize ponovno.

Agent shrani celotno JSON poročilo v svoj workspace pod `/exports/process_mining/<job-id>/report.json`, v kratkem končnem odgovoru vrne njegovo pot. Tako obstoječi korak za strukturiranje končnega odgovora ne skrajša velikega poročila. Webservice prebere samo vnaprej določeno pot in strukturno preveri poročilo; referenc ne obravnava kot dokazanih vsebinskih ugotovitev. Napačna shema, neznani viri in prekinjene grafične povezave niso objavljeni kot uspešna analiza.

Datoteke/poročila ostanejo v lokalnem Docker volumnu in agentovem uporabniškem workspace; ni samodejnega roka brisanja. Izvorni primer `data_for_analysis.csv` se ne vgradi v Docker sliko. Bifrost uporablja obstoječi agent; ta servis ga ne konfigurira.

## Preverjanje

```bash
docker compose run --rm -T -v "$PWD/tests:/tests:ro" web \
  python -m unittest discover -s /tests -v
```

Testi z nadomestnim agentovim HTTP preverjajo nespremenjene izvirne datoteke, pogodbo API, avtorizacijo, veljavnost rezultatov, aktivno opravilo ob prekinitvi povezave in obnovitev izgubljenega odgovora brez dvojnega zagona. Ne merijo natančnosti agentove poslovne analize.
