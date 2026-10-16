# Feedy trinasty.sk pre Magrano

Každú noc GitHub Actions spustí `build_feed.py`, ktorý stiahne katalóg e-shopu
https://www.trinasty.sk/ z verejného WooCommerce Store API a zverejní tri XML súbory
podľa https://magrano.sk/specifikacia-xml-feedu/ cez GitHub Pages:

- `trinasty-produkty.xml` – produkty (varianty ako samostatné položky s ITEM_GROUP_ID)
- `trinasty-produkty-kategorie.xml` – kategórie
- `trinasty-produkty-znacky.xml` – značky
- `stats.json` – štatistika posledného behu

Ručné spustenie: záložka Actions → Build feeds → Run workflow.
