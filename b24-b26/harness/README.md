# Harnais B24/B26

Harnais d'exécution **réelle** sur PostgreSQL. La maquette `fixture.sql` (schéma `fx`) n'est **pas** le schéma
NAATAL+ ; elle existe uniquement parce que le dépôt ne contenait aucun schéma au moment des tests.

## Prérequis
- PostgreSQL 16 (testé sur 16.14), `track_commit_timestamp = on` (01-G2), `log_lock_waits = on` (facultatif)
- Python 3.11 + `psycopg[binary]` 3.x
- 01-D2 arrête et redémarre le serveur (`pg_ctlcluster 16 main stop -m immediate`) : **base de test uniquement**

## Lancer
```bash
export B2426_DSN="host=localhost dbname=b2426 user=tester password=tester"
psql "$B2426_DSN" -f fixture.sql
python3 test_01AB.py A      # 01-A
python3 test_01AB.py B      # 01-B
python3 test_01CD.py        # 01-C, 01-D
python3 test_01D2.py        # 01-D2 (crashs réels)
python3 test_01EF.py        # 01-E, 01-F
python3 test_01G2.py        # 01-G2
python3 test_01HI.py H      # 01-H
python3 test_01HI.py I      # 01-I2 A/B
python3 test_01I2_A3.py     # 01-I2 A3 ciblé
```
Les logs sont écrits dans `../logs/`.

## Brancher le vrai schéma NAATAL+
Remplacer, dans les tests, les appels `fx.accept_*`, `fx.redispatch`, `fx.disable_*`, `fx.dedup_*` par les
fonctions réelles, et adapter `seed()`, `snapshot()` et la vue `invariant_violations` aux tables réelles.
