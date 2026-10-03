# NAATAL+ — B24/B26 : résilience, concurrence, idempotence, synchronisation

Rapport d'exécution, 2026-10-03. Faits techniques uniquement : aucune décision d'architecture n'est prise ici.

---

## 0. Constat préalable : il n'y a aucun code NAATAL+ à tester

| Élément attendu | Ce que j'ai trouvé |
|---|---|
| Dépôt `williampagny35-ship-it/Claude-code` | **Vide** : 0 commit, aucune branche distante (`git ls-remote` ne renvoie rien) |
| Schéma PostgreSQL / PostGIS | Absent |
| Fonctions (accept, redispatch, éligibilité, expiration, dedup) | Absentes |
| Table `request_dedup` | Absente |
| Mécanisme de retry « prévu » | Absent |
| Documentation d'architecture (ordre de verrouillage, niveau d'isolation, stratégie dedup) | Absente |
| Projet Supabase / base de test distante, identifiants | Aucun (pas de variable d'environnement, pas de config) |
| Autres dépôts accessibles à la session | Aucun (`list_repos` ne renvoie que ce dépôt) |

**Conséquence : pour les 10 tests, le verdict NAATAL+ est `INCONCLUSIF — environnement insuffisant`.**
Il manque le schéma réel, les fonctions serveur réelles et la stratégie d'idempotence/retry réelle. Sans eux,
je ne peux affirmer aucun PASS pour NAATAL+.

### Ce que j'ai fait quand même (et ses limites)

1. J'ai démarré un **vrai PostgreSQL 16.14** local (`read committed` par défaut, `deadlock_timeout=1s`,
   `track_commit_timestamp=on`, `log_lock_waits=on`).
2. J'ai écrit une **maquette minimale** (`harness/fixture.sql`, schéma `fx`, tables `rides`, `offers`,
   `drivers`, `request_dedup`, `transitions`, `side_effects`). Ce **n'est pas** une proposition d'architecture :
   elle sert seulement à exécuter réellement chaque scénario et à observer **le comportement du moteur PostgreSQL**
   selon plusieurs stratégies d'implémentation plausibles (variantes `v_lock`, `v_cond`, `v_naive`,
   `dedup atomique` / `dedup en 3 transactions`, horloges `now()` / `statement_timestamp()` / `clock_timestamp()`).
3. Tous les chiffres ci-dessous sont **mesurés** (comptages réels sur N exécutions), aucun n'est théorique.
   Les fréquences dépendent du matériel (4 vCPU), du réseau local et des temporisations de la maquette
   (certaines fonctions de maquette contiennent un `pg_sleep` qui élargit volontairement la fenêtre de course ;
   c'est indiqué). **Elles ne prédisent pas les fréquences en production.**
4. Le harnais est réutilisable : dès que le schéma réel existe, on peut remplacer les appels `fx.*` par les
   vraies fonctions et relancer les mêmes scénarios (voir `harness/README.md`).

Lecture des verdicts ci-dessous :
- **Verdict NAATAL+** : toujours INCONCLUSIF (voir § 0).
- **Verdict maquette** : PASS/FAIL d'une *variante d'implémentation* donnée sur le vrai moteur. Ça dit quelles
  stratégies tiennent et lesquelles cassent. Ça ne dit pas ce que fait le code NAATAL+.

Tous les logs bruts sont dans `logs/` ; les extraits du journal serveur (deadlocks, crash/recovery) sont dans
`logs/postgresql-server-excerpt.log`.

---

## Synthèse

| Test | NAATAL+ | Maquette : ce qui a été observé |
|---|---|---|
| 01-A | INCONCLUSIF | `v_lock` et `v_cond` : 1 gagnant / 200 courses, 0 violation. Témoin `v_naive` (lecture puis écriture sans verrou) : **2 gagnants sur 200/200** → le harnais détecte bien les doubles affectations |
| 01-B | INCONCLUSIF | 50 chauffeurs × 20 courses : `v_lock`, `v_cond` → 1 gagnant par course. `v_naive` RC → 50 gagnants par course. `v_naive` SERIALIZABLE → 1 gagnant + 980 × `40001` |
| 01-C | INCONCLUSIF | 2 request_id différents : la dedup **ne peut pas** les reconnaître (par construction). Seule la garde métier (verrou + statut) a empêché la double exécution (100/100) |
| 01-D | INCONCLUSIF | Dedup atomique : 10 envois parallèles → 1 exécution + 9 `REPLAY` (50/50) ; sans `ON CONFLICT` → `23505` après attente du verrou. **Un échec métier n'est pas mémorisé** : le rejeu ré-exécute |
| 01-D2 | INCONCLUSIF | Crash **réel** (SIGKILL client, arrêt serveur `immediate`, SIGKILL backend). Dedup en 3 transactions → ligne **bloquée en `pending` pour toujours** ; le rejeu répond une erreur alors que l'opération a réussi. Dedup atomique → cohérent dans les 2 crashs testés |
| 01-E | INCONCLUSIF | Accept vs redispatch : 2 issues seulement, toutes deux cohérentes (`v_lock` 123/77, `v_cond` 128/72), 0 violation |
| 01-F | INCONCLUSIF | **Offre acceptée après son expiration réelle** avec `now()` (transaction ouverte avant l'expiration) et avec `now()` **et** `statement_timestamp()` (appel RPC unique bloqué sur un verrou). Seul `clock_timestamp()` lu après le verrou a refusé |
| 01-G2 | INCONCLUSIF | Aucune issue unique : blocage, sérialisation, `40001` ou succès des deux selon la variante. **Write skew observé** sous READ COMMITTED et REPEATABLE READ, y compris avec `FOR SHARE` côté acceptation. SERIALIZABLE : `40001`, 0 write skew |
| 01-H | INCONCLUSIF | Charge mixte 2 × 20 s (accept, redispatch, désactivation, sweeper) : 0 violation des invariants gagnant/perdant |
| 01-I2 | INCONCLUSIF | A (ordre course → offres) : 0 × `40P01` observé (ce n'est pas une preuve d'impossibilité). B (ordres opposés) : `40P01` réel, détecté en ~1 000 ms ; accept vs redispatch « offres d'abord » : **84/100 deadlocks** ; avec retry : 7 deadlocks, 7 rejeux réussis, 0 abandon |

---

## TEST 01-A — deux chauffeurs acceptent simultanément la même course

**Objectif :** une seule acceptation doit gagner.

**Préconditions :** pour chaque itération : course `dispatching`, `version=0`, `dispatch_generation=1`,
`driver_id=NULL` ; 2 offres `pending` (chauffeurs 1 et 2), expiration à +30 s ; `request_dedup` vide.

**Exécution réelle :** 200 itérations par variante ; 2 connexions distinctes, libérées ensemble par une barrière
(`threading.Barrier`), chacune appelle la fonction d'acceptation pour son offre.

**Résultat observé :**

| Variante | Isolation | Réponses (400 appels) | Gagnants par course | Violations |
|---|---|---|---|---|
| `v_lock` (`SELECT … FOR UPDATE` sur la course) | RC | 200 ACCEPTED, 200 `P0001 RIDE_NOT_DISPATCHING:assigned` | 1 (200/200) | 0 |
| `v_cond` (UPDATE conditionnel sur status+version) | RC | 200 ACCEPTED, 200 `P0001 CAS_LOST` | 1 (200/200) | 0 |
| `v_naive` (témoin : lecture puis écriture, sans verrou) | RC | **400 ACCEPTED** | **2 (200/200)** | 600 |
| `v_naive` | SERIALIZABLE | 200 ACCEPTED, 200 `40001` | 1 (200/200) | 200* |

\* Ces 200 violations `pending_offer_on_assigned_ride` viennent du fait que la fonction témoin ne passe pas les offres
perdantes à `lost` (défaut volontaire de la maquette). Ce n'est pas un effet de concurrence.

**État final :** exemple pour `v_lock` : course `assigned`, `version=1`, `driver_id=` le gagnant, 1 offre `accepted`,
1 offre `lost`, 1 transition `dispatching→assigned`, 1 effet de bord.

**Erreurs / SQLSTATE :** `P0001` (refus métier), `40001` (SERIALIZABLE uniquement).

**Invariant vérifié :** au plus une affectation par course, une seule offre acceptée, une seule transition.

**Verdict :** NAATAL+ **INCONCLUSIF**. Maquette : `v_lock` PASS, `v_cond` PASS, `v_naive` RC **FAIL** (attendu,
c'est le témoin), `v_naive` SERIALIZABLE PASS pour l'unicité.

**Explication :** l'unicité dépend entièrement de la stratégie d'implémentation. PostgreSQL ne protège pas une
lecture suivie d'une écriture en READ COMMITTED (le témoin produit 2 affectations à chaque fois). Sous
SERIALIZABLE, le moteur l'a empêché par `40001`, ce qui impose que l'appelant gère un retry.

**Preuve :** `logs/01-A.log`.

---

## TEST 01-B — forte concurrence (plusieurs chauffeurs)

**Objectif :** une seule acceptation gagnante même avec beaucoup de concurrents.

**Préconditions :** 20 courses, 50 chauffeurs, 50 offres `pending` par course.

**Exécution réelle :** pour chaque course, 50 connexions simultanées (barrière), soit 1 000 appels par variante.

**Résultat observé :**

| Variante | Réponses (1 000) | Gagnants par course | Violations |
|---|---|---|---|
| `v_lock` RC | 20 ACCEPTED, 980 `RIDE_NOT_DISPATCHING` | 1 (20/20) | 0 |
| `v_cond` RC | 20 ACCEPTED, 786 `CAS_LOST`, 194 `PRECHECK_FAILED` | 1 (20/20) | 0 |
| `v_naive` RC (témoin) | **1 000 ACCEPTED** | **50 (20/20)** | 1 020 |
| `v_naive` SERIALIZABLE | 20 ACCEPTED, 980 `40001` | 1 (20/20) | 980* |

\* même artefact de maquette qu'en 01-A.

**État final :** `v_lock`/`v_cond` : 20 courses `assigned`, 1 offre `accepted` et 49 `lost` chacune.

**Erreurs / SQLSTATE :** `P0001` ; `40001` × 980 en SERIALIZABLE (98 % des appels finissent en échec de sérialisation
et devraient être rejoués ou rejetés côté appelant).

**Invariant vérifié :** unicité de l'affectation sous 50 concurrents.

**Verdict :** NAATAL+ **INCONCLUSIF**. Maquette : `v_lock` PASS, `v_cond` PASS, témoin FAIL, `v_naive` SERIALIZABLE
PASS pour l'unicité.

**Explication :** mêmes conclusions qu'en 01-A, à plus grande échelle. Mesuré au passage : en SERIALIZABLE sans
verrou explicite, le coût est de 49 erreurs `40001` par course.

**Preuve :** `logs/01-B.log`.

---

## TEST 01-C — deux request_id différents pour la même opération

**Objectif :** un double tap qui régénère le request_id ne doit pas produire deux acceptations.

**Préconditions :** course `dispatching`, 3 offres ; même chauffeur (1), même offre, request_id `C-n-a` et `C-n-b`.

**Exécution réelle :** 100 itérations, 2 appels simultanés à `accept_dedup_atomic` (dedup dans la même transaction
que le métier, puis `accept_v_lock`).

**Résultat observé :** 100 ACCEPTED, 100 `P0001 RIDE_NOT_DISPATCHING:assigned`. Pour 100/100 courses : 1 transition,
1 effet de bord, 1 offre acceptée. `request_dedup` : 100 lignes `done`, une par request_id gagnant. Le perdant a été
annulé (rollback), sa ligne de dedup aussi.

**État final :** course `assigned` (chauffeur 1), `version=1`, offres `accepted:1, lost:2`.

**Erreurs / SQLSTATE :** `P0001`.

**Invariant vérifié :** une seule exécution métier et un seul effet de bord.

**Verdict :** NAATAL+ **INCONCLUSIF**. Maquette PASS, mais **grâce à la garde métier, pas à la dedup**.

**Explication :** fait observé : avec deux request_id différents, `request_dedup` n'a aucun moyen de reconnaître le
doublon. Ce qui protège, c'est le verrou sur la course et le contrôle de statut. Une opération sans garde d'état
équivalente (par exemple un ajout de pourboire ou un paiement) n'aurait aucune protection contre ce cas. Je ne l'ai
pas testé, faute de telles fonctions.

**Preuve :** `logs/01-C.log`.

---

## TEST 01-D — même request_id envoyé plusieurs fois

**Objectif :** un même request_id rejoué ne doit produire qu'une seule exécution, et le client doit recevoir le même
résultat.

**Exécution réelle et résultats :**

- **D1 — 10 envois parallèles du même request_id, 50 itérations (dedup atomique).** 50 ACCEPTED et
  450 `REPLAY:done:ACCEPTED`. Pour 50/50 courses : 1 transition, 1 effet de bord. Les 9 concurrents **attendent**
  sur l'index unique de `request_dedup` jusqu'au COMMIT du premier, puis voient la ligne. Durée maximale d'un appel :
  26 ms.
- **D2-seq — 5 rejeux séquentiels.** `ACCEPTED`, puis 4 × `REPLAY:done:ACCEPTED`.
- **D3 — `INSERT` sans `ON CONFLICT`.** La 2ᵉ transaction est restée bloquée environ 790 ms, le temps que la 1ʳᵉ
  fasse COMMIT, puis a reçu **`23505` unique_violation**. Ce n'est pas un résultat silencieux : l'appelant doit
  gérer ce code.
- **D4 — 1ʳᵉ tentative refusée par le métier (`OFFER_EXPIRED`).** Toute la transaction est annulée, **y compris la
  ligne `request_dedup`**. Au rejeu du même request_id, après remise de l'offre à l'état valide, le métier a été
  **ré-exécuté** et a renvoyé `ACCEPTED`. Le même request_id a donc reçu deux réponses différentes (refus, puis
  succès).

**État final :** 1 transition et 1 effet de bord par course, `request_dedup` `done` avec le résultat `ACCEPTED`.

**Erreurs / SQLSTATE :** `23505` (D3), `P0001` (D4).

**Invariant vérifié :** une seule exécution par request_id réussi. Les résultats d'échec ne sont pas mémorisés.

**Verdict :** NAATAL+ **INCONCLUSIF**. Maquette : D1, D2-seq PASS. D3 : comportement moteur relevé. D4 : **point à
décider** (ce n'est pas un défaut moteur).

**Explication :** si la sémantique attendue est « même request_id ⇒ même réponse », la stratégie atomique ne la
garantit pas pour les échecs, puisque le rollback efface la trace. Rien dans le projet ne précise la sémantique voulue.

**Preuve :** `logs/01-D.log`.

---

## TEST 01-D2 — crash après le métier, avant la finalisation de request_dedup

**Objectif :** vérifier le comportement quand un crash survient entre la validation du métier et la finalisation de
`request_dedup`.

**Ce qui a été réellement reproduit (crashs réels, pas simulés) :**

| Cas | Stratégie dedup | Crash provoqué | Moment |
|---|---|---|---|
| D2-a | 3 transactions (`pending` → métier → `done`) | **SIGKILL du processus client** (équivalent à une Edge Function tuée) | après le COMMIT métier, avant `dedup_finish` |
| D2-b | 3 transactions | **arrêt serveur `pg_ctl stop -m immediate`** (crash, recovery WAL au redémarrage) | après le COMMIT métier, avant `dedup_finish` |
| D2-c | atomique | **SIGKILL du backend PostgreSQL** (le postmaster réinitialise toutes les sessions) | métier exécuté, avant COMMIT |
| D2-d | atomique | **arrêt serveur `immediate`** | juste après le retour du COMMIT (réponse considérée comme perdue) |

**Résultats observés :**

- **D2-a et D2-b (3 transactions) :**
  - après crash : course `assigned` (1 transition, 1 effet de bord, offres `accepted:1, lost:1`) et
    **`request_dedup` = `pending`, `result` NULL** ;
  - retry → `dedup_begin` renvoie `EXISTS:pending` ;
  - **politique P1** (`pending` = « en cours » → ne rien faire) : la requête ne se termine jamais. La ligne reste en
    `pending` indéfiniment. Aucun mécanisme de récupération n'existe dans le projet (aucun code) ;
  - **politique P2** (`pending` → ré-exécuter le métier) : `P0001 RIDE_NOT_DISPATCHING:assigned`. Pas de double
    transition ni de double effet de bord (la garde métier a bloqué), mais **le client reçoit une erreur alors que
    son opération a réussi**. `request_dedup` reste `pending` ;
  - recovery serveur (D2-b) : redémarrage en 2,6 s, le COMMIT métier a été conservé (WAL `redo done`).
- **D2-c (atomique, backend tué avant COMMIT) :** rien n'est persisté (course `dispatching`, aucune ligne dedup, aucune
  transition). Le retry exécute une seule fois (`ACCEPTED`), le 2ᵉ retry reçoit `REPLAY:done:ACCEPTED`.
- **D2-d (atomique, crash juste après COMMIT) :** le retry après recovery reçoit `REPLAY:done:ACCEPTED`. 1 seule
  transition.

**État final :** aucune double transition ni double modification d'offre dans les 4 cas. D2-a/b laissent une ligne
`request_dedup` orpheline en `pending`.

**Erreurs / SQLSTATE :** côté client : `OperationalError` (connexion coupée : « SSL error: unexpected eof »,
« server closed the connection unexpectedly ») ; `P0001` au retry P2.

**Invariant vérifié :** pas de double exécution métier : vérifié dans les 4 cas. Réponse correcte au retry : **non**
en D2-a/b.

**Verdict :** NAATAL+ **INCONCLUSIF**. Maquette : stratégie 3 transactions **FAIL** sur l'issue de la requête (ligne
`pending` orpheline, réponse erronée au retry), PASS sur la non-duplication grâce à la garde métier. Stratégie
atomique : PASS dans les 2 crashs testés.

**Explication et limites :**
- Le brief parle de « finalisation de request_dedup » après la transaction métier validée, ce qui suggère une
  stratégie en plusieurs transactions. C'est précisément la stratégie qui a laissé un état orphelin. Je ne peux pas
  confirmer que c'est le design NAATAL+ : aucun document ne le dit.
- **Effets de bord externes** (notification push, SMS, appel paiement émis par une Edge Function après le COMMIT) :
  **non testables**, il n'y a ni Edge Function ni intégration. Si un tel effet est envoyé entre le COMMIT métier et
  la finalisation, et que le retry P2 le renvoie, il serait dupliqué. Non observé, non exclu.
- Crash « entre deux transactions » : reproduit proprement ici côté client (SIGKILL) et côté serveur
  (`immediate`). À refaire sur Supabase réel : tuer l'Edge Function (timeout forcé, `Deno.exit`) entre ses deux
  appels SQL, et redémarrer la base sur une branche de preview.

**Preuve :** `logs/01-D2.log`, `logs/postgresql-server-excerpt.log` (« received immediate shutdown request »,
« terminated by signal 9 », « redo starts / redo done »).

---

## TEST 01-E — acceptation concurrente et redispatch

**Objectif :** une acceptation et un redispatch simultanés ne doivent produire qu'une seule issue cohérente.

**Préconditions :** course `dispatching`, `generation=1`, offres chauffeurs 1 et 2 ; le redispatch passe les offres
`pending` à `superseded`, incrémente la génération et crée des offres pour les chauffeurs 3 et 4.

**Exécution réelle :** 200 itérations par variante, départ par barrière + décalage aléatoire symétrique de 0 à 4 ms.
Plus un cas forcé : le redispatch garde le verrou 1 s et l'acceptation arrive pendant ce temps.

**Résultat observé :**

| Variante | Issue | Nb |
|---|---|---|
| `v_lock` | accept `OFFER_NOT_PENDING:superseded` / redispatch OK → `dispatching`, gen=2, `pending:2, superseded:2` | 123 |
| `v_lock` | accept OK / redispatch `RIDE_NOT_DISPATCHING:assigned` → `assigned`, gen=1 | 77 |
| `v_cond` | accept `CAS_LOST` / redispatch OK | 120 |
| `v_cond` | accept `PRECHECK_FAILED` / redispatch OK | 8 |
| `v_cond` | accept OK / redispatch refusé | 72 |

Cas forcé : l'acceptation a **attendu** environ 795 ms le verrou de la course, puis a reçu
`OFFER_NOT_PENDING:superseded`. État : `dispatching`, gen=2.

**Erreurs / SQLSTATE :** `P0001` uniquement, aucun `40P01` ni `40001`.

**Invariant vérifié :** jamais d'offre acceptée d'une génération périmée, jamais affecté ET redispatché à la fois.
0 violation.

**Verdict :** NAATAL+ **INCONCLUSIF**. Maquette `v_lock` PASS, `v_cond` PASS.

**Explication :** remarque : lors d'un premier essai, le décalage n'était appliqué qu'au redispatch et
l'acceptation gagnait 200 fois sur 200. Avec un décalage symétrique, les deux ordres apparaissent. Un test de
concurrence biaisé peut donc masquer un ordre d'exécution entier. J'ai corrigé le harnais et relancé, et seule la
version corrigée est rapportée.

**Preuve :** `logs/01-E.log`.

---

## TEST 01-F — expiration réelle d'une offre

**Objectif :** une offre peut-elle être acceptée après son heure d'expiration réelle ?

**Préconditions :** offre avec `expires_at = clock_timestamp() + 2 s` (F1) ou `+ 1 s` (F2).

**Exécution réelle :**

- **F1 — transaction explicite ouverte avant l'expiration.** `BEGIN`, lecture des horloges, attente de 3 s dans la
  transaction, puis appel de l'acceptation (contrôle `expires_at <= horloge`).
- **F2 — appel unique en autocommit (comme un RPC PostgREST).** Lancé avant l'expiration. Une autre transaction
  tient le verrou de la course pendant 3 s puis fait ROLLBACK. L'acceptation attend le verrou et ne fait son
  contrôle qu'après l'expiration.

**Résultat observé (horloges relevées dans la transaction F1 avec `now`) :**

```
début tx : now=14:11:18.688  statement=14:11:18.689  clock=14:11:18.689  expires_at=14:11:20.674
après 3 s : now=14:11:18.688  statement=14:11:21.691  clock=14:11:21.691
→ now() est FIGÉ au début de la transaction ; statement_timestamp() et clock_timestamp() avancent.
```

| Scénario | Horloge du contrôle | Résultat | `decided_at > expires_at` |
|---|---|---|---|
| F1 (tx ouverte 3 s) | `now()` | **ACCEPTED** | **oui** (décidée 1,02 s après l'expiration) |
| F1 | `statement_timestamp()` | `OFFER_EXPIRED` | — |
| F1 | `clock_timestamp()` | `OFFER_EXPIRED` | — |
| F2 (RPC bloqué 2,9 s sur un verrou) | `now()` | **ACCEPTED** | **oui** (≈ 2,0 s après) |
| F2 | `statement_timestamp()` | **ACCEPTED** | **oui** (≈ 2,0 s après) |
| F2 | `clock_timestamp()` lu **après** le verrou | `OFFER_EXPIRED` | — |

**État final :** dans les cas ACCEPTED : course `assigned`, offre `accepted` avec `decided_at` postérieur à
`expires_at`.

**Erreurs / SQLSTATE :** `P0001 OFFER_EXPIRED` dans les cas refusés.

**Invariant vérifié :** « aucune offre acceptée après son expiration réelle » : **violé** avec `now()` (F1, F2) et
avec `statement_timestamp()` (F2).

**Verdict :** NAATAL+ **INCONCLUSIF** (on ne sait pas quelle horloge le code réel utilise). Maquette : `now()`
**FAIL**, `statement_timestamp()` **FAIL** (F2), `clock_timestamp()` après verrou PASS.

**Explication :** fait moteur vérifié : `now()` (= `transaction_timestamp()`, `CURRENT_TIMESTAMP`) renvoie l'heure
de **début de transaction**. `statement_timestamp()` renvoie l'heure de **début de l'instruction**. Dans un appel RPC
(une seule instruction), les deux horloges sont figées **avant** l'attente de verrou. Une attente sur la course
suffit donc à faire passer une offre expirée, sans transaction longue. La même question se pose pour tout
`DEFAULT now()` ou toute contrainte/politique RLS qui compare à `now()`. Point connexe non testé, faute de code : un
éventuel job d'expiration (cron/sweeper) — sa cadence ne protège pas l'acceptation elle-même.

**Preuve :** `logs/01-F.log`.

---

## TEST 01-G2 — acceptation concurrente d'une modification de l'éligibilité

**Objectif :** observer ce que fait réellement PostgreSQL quand une acceptation et une désactivation du chauffeur se
croisent.

**Préconditions :** chauffeur 1 éligible, course `dispatching`, offre chauffeur 1 valide.
Deux opérations de maquette :
- `disable_flag` : `UPDATE drivers SET eligible=false` ;
- `disable_guard` : refuse si le chauffeur a une course `assigned`, sinon `eligible=false`. Elle contient un
  `pg_sleep(0.02)` qui élargit la fenêtre de course.

Deux contrôles d'éligibilité côté acceptation : `read` (SELECT simple) ou `share` (`SELECT … FOR SHARE`). Les ordres
de COMMIT sont relevés via `track_commit_timestamp`.

**Exécution réelle :**
1. **12 entrelacements forcés** : l'acceptation exécute sa fonction sans faire COMMIT. La désactivation est lancée,
   on observe `pg_stat_activity` à +1 s, puis l'acceptation fait COMMIT.
2. **8 campagnes de 150 itérations** à départ simultané (décalage aléatoire de 0 à 4 ms).

**Résultat observé — entrelacements forcés :**

| disable | contrôle | isolation | Désactivation pendant que l'acceptation est ouverte | Acceptation | Final (eligible, course) |
|---|---|---|---|---|---|
| flag | read | RC / RR / SER | **ne bloque pas** (≈ 3 ms), COMMIT | ACCEPTED | false, assigned |
| flag | share | RC / RR / SER | **bloque** (`Lock/transactionid`), environ 1 006 ms, puis OK | ACCEPTED | false, assigned |
| guard | read | RC | ne bloque pas, OK | ACCEPTED | **false, assigned (write skew)** |
| guard | read | RR | ne bloque pas, OK | ACCEPTED | **false, assigned (write skew)** |
| guard | read | SER | ne bloque pas, OK | **`40001`** | false, dispatching |
| guard | share | RC | bloque environ 1 007 ms, puis **OK** | ACCEPTED | **false, assigned (write skew)** |
| guard | share | RR | bloque environ 1 009 ms, puis **OK** | ACCEPTED | **false, assigned (write skew)** |
| guard | share | SER | bloque environ 1 006 ms, puis **`40001`** | ACCEPTED | true, assigned |

**Résultat observé — campagnes (150 itérations chacune) :**

| disable | contrôle | isolation | Issues observées | Anomalies |
|---|---|---|---|---|
| flag | read | RC | 69 OK/OK avec désactivation validée **avant** l'acceptation ; 57 refus `DRIVER_NOT_ELIGIBLE` ; 24 OK/OK acceptation avant | **69 « temps réel »** |
| flag | read | SER | 123 OK/OK désactivation avant ; 24 acceptation avant ; 3 refus | **123 « temps réel »** |
| flag | share | RC | 110 refus ; 40 OK/OK acceptation avant | 0 |
| flag | share | SER | 104 `40001` (accept) ; 42 OK/OK ; 4 refus | 0 |
| guard | read | RC | 148 OK/OK → inéligible ET affecté ; 2 refus `DRIVER_HAS_ACTIVE_RIDE` | **148 write skew** |
| guard | read | SER | 150 `40001` (disable) | 0 |
| guard | share | RC | 147 OK/OK → inéligible ET affecté ; 3 refus | **147 write skew** |
| guard | share | SER | 150 `40001` (disable) | 0 |

Définitions :
- **write skew** : les deux opérations sont validées, et l'état final (chauffeur inéligible avec course active)
  ne peut être produit par aucun ordre série ;
- **« temps réel »** : la désactivation a fait COMMIT *avant* l'acceptation, et l'acceptation a quand même réussi
  parce qu'elle avait lu `eligible=true` plus tôt. Ce n'est pas une violation de sérialisabilité (l'ordre série
  équivalent « acceptation puis désactivation » existe), et **SERIALIZABLE ne l'empêche pas** (123/150). En revanche,
  l'administrateur a vu « désactivé » avant qu'une course soit affectée.

**Erreurs / SQLSTATE :** `40001` (« could not serialize access due to read/write dependencies among transactions »),
`P0001`. **Aucun `40P01`.** Aucune autre erreur.

**Invariant vérifié :** dépend de la règle métier, qui n'est pas définie dans le projet.

**Verdict :** NAATAL+ **INCONCLUSIF**. Maquette : RC et RR avec `disable_guard` **FAIL** (write skew, y compris avec
`FOR SHARE`). SERIALIZABLE PASS sur le write skew, au prix de `40001` à gérer. `disable_flag` + `read` : anomalie
« temps réel » sous toutes les isolations. `disable_flag` + `share` RC : 0 anomalie.

**Explication :** PostgreSQL n'a pas eu un comportement unique. Il a, selon la variante, bloqué, sérialisé, laissé
passer les deux opérations, ou renvoyé `40001`. `FOR SHARE` côté acceptation ne suffit pas contre le write skew si
l'autre opération fait son contrôle (`EXISTS`) **avant** de prendre son verrou de ligne : elle attend, puis valide
sur la base d'une lecture périmée. Les fréquences dépendent du `pg_sleep(0.02)` de la maquette.

**Preuve :** `logs/01-G2.log`.

---

## TEST 01-H — cohérence des offres gagnantes et perdantes

**Objectif :** les états des offres doivent rester cohérents avec l'état de la course, sous charge concurrente.

**Préconditions :** 30 courses, 6 chauffeurs, offres expirant entre 0,2 et 3 s.

**Exécution réelle :** 2 runs de 20 s (horloge `clock_timestamp()` puis `now()`). 12 workers d'acceptation, 3 de
redispatch, 1 de désactivation/réactivation, 2 sweepers d'expiration (`UPDATE offers … 'expired'`). Retry
`40P01`/`40001` activé.

**Résultat observé :** run `clock` : 30 ACCEPTED, 43 REDISPATCHED, 96 DISABLED, 1 935 refus `RIDE_NOT_DISPATCHING`, etc.
Run `now` : 30 ACCEPTED, 29 REDISPATCHED. Retries : 0. (Les « None » des logs sont des passages du sweeper qui n'ont
modifié aucune ligne.)

**État final :** 30/30 courses `assigned`. Offres (run `clock`) : `accepted:30, lost:78, superseded:116, expired:42`.

Contrôles, tous à **0** dans les deux runs : vue `invariant_violations` (affectée sans exactement 1 offre acceptée,
conducteur divergent, offre acceptée sur course non affectée, offre `pending` sur course affectée, plus d'une
transition, offre acceptée d'une génération périmée), `lost` sur course non affectée, offre acceptée après
expiration, offre `pending` d'une ancienne génération.

**Erreurs / SQLSTATE :** `P0001` uniquement.

**Invariant vérifié :** cohérence gagnant/perdant : oui pour la maquette.

**Verdict :** NAATAL+ **INCONCLUSIF**. Maquette `v_lock` PASS.

**Explication et limite :** la charge converge vite (toutes les courses sont affectées en quelques secondes), donc la
majorité des 20 s est passée en refus triviaux. La contention réelle est concentrée au début. Le run `now()` n'a pas
produit d'acceptation après expiration ici, parce que les transactions sont courtes et sans attente longue. Ça ne
contredit pas 01-F : le défaut n'apparaît que sous attente.

**Preuve :** `logs/01-H.log`.

---

## TEST 01-I2 — concurrence des verrous et deadlock

**Objectif :** relever le comportement réel de PostgreSQL face aux conflits de verrous, et vérifier la récupération.

### A. Chemin normal (ordre course → offre → autres offres → chauffeur `FOR SHARE` ; redispatch course → offres)

| Run | Durée | Résultat | `40P01` | `40001` | Violations |
|---|---|---|---|---|---|
| A1 sans sweeper, sans retry | 20 s, 16 workers | 20 ACCEPTED, 35 REDISPATCHED, 93 DISABLED, ~2 000 refus | **0** | 0 | 0 |
| A2 + sweeper (offres mises à jour sans verrou course), sans retry | 20 s | 20 ACCEPTED, 46 REDISPATCHED | **0** | 0 | 0 |
| A3 ciblé : accept vs sweeper multi-lignes sur la même course (7 offres concurrentes expirant pendant l'acceptation) | 300 itérations | 300 ACCEPTED (251 avec sweeper actif, 49 sweeper à 0 ligne) | **0** | 0 | 0 |

**Ce n'est pas une preuve qu'un deadlock est impossible sur le chemin normal.** C'est 0 occurrence observée dans
ces charges, sur cette maquette. A3 n'a pas produit de deadlock probablement parce que les deux UPDATE multi-lignes
parcourent les offres dans le même ordre physique. Rien ne garantit cet ordre en général (plan différent, index,
`ORDER BY` absent).

### B. Scénarios volontairement conflictuels

- **B1 — ordres opposés explicites** (T1 : course puis chauffeur ; T2 : chauffeur puis course) :
  T1 reçoit **`40P01` deadlock detected** après **1 105 ms** (≈ `deadlock_timeout` = 1 s + 100 ms de pause du
  scénario). Détail serveur : « Process 14043 waits for ShareLock on transaction 37393; blocked by process 14044.
  Process 14044 waits for ShareLock on transaction 37392; blocked by process 14043. » T2 fait COMMIT. La version de
  la course est incrémentée une seule fois.
- **B2 — accept (course → offres) vs redispatch « offres d'abord » (offres → course), 100 itérations :**
  **84 × `40P01`** côté acceptation (le redispatch passe) et 16 × acceptation gagnante avec redispatch refusé.
  0 violation : la victime est entièrement annulée.
- **B3 — récupération** : même charge conflictuelle pendant 20 s (redispatch offres d'abord + sweeper), avec un
  retry de maquette (5 tentatives max, backoff exponentiel 5–30 ms × 2ⁿ sur `40P01`/`40001`) : **7 deadlocks, 7
  rejeux, 0 abandon**, 0 violation, 20/20 courses cohérentes.

**Erreurs / SQLSTATE :** `40P01` (B1 : 1, B2 : 84, B3 : 7). Compteur `pg_stat_database.deadlocks` = 92 à la fin,
ce qui correspond exactement à 1 + 84 + 7. Chaque deadlock est journalisé côté serveur
(`logs/postgresql-server-excerpt.log`).

**Invariant vérifié :** après un deadlock, la transaction victime est entièrement annulée, sans état partiel :
**vérifié**. Récupération par retry : **vérifiée pour le retry de maquette uniquement**.

**Verdict :** NAATAL+ **INCONCLUSIF**. Il n'existe aucun ordre de verrouillage documenté, aucune fonction et aucun
mécanisme de retry dans le projet. Maquette : A 0 deadlock observé ; B `40P01` reproduit ; récupération par retry
PASS.

**Explication :** PostgreSQL détecte les deadlocks et en annule une victime, mais **seulement après
`deadlock_timeout`** (1 s ici, réglable). Chaque deadlock coûte donc au moins environ 1 s de latence à une requête,
plus le retry. Une seule opération qui ne respecte pas l'ordre (B2 : un redispatch qui touche les offres avant la
course) a suffi à produire des deadlocks dans 84 % des collisions.

**Preuve :** `logs/01-I2.log`, `logs/01-I2-A3.log`, `logs/postgresql-server-excerpt.log`.

---

## Incohérences et points à signaler (non corrigés)

1. **Le projet ne contient rien.** Le brief décrit une base PostgreSQL + PostGIS, des fonctions serveur,
   `request_dedup`, un ordre de verrouillage et un mécanisme de retry « prévus ». Rien de tout cela n'existe dans le
   dépôt. Tant que ces éléments ne sont pas fournis, aucun résultat B24/B26 ne peut être attribué à NAATAL+.
2. **Horloge d'expiration (01-F)** : si le code réel compare `expires_at` à `now()` ou à `statement_timestamp()`,
   une offre expirée peut être acceptée dès qu'il y a une attente de verrou. Observé sur le moteur.
3. **Idempotence (01-D, 01-D2)** : la sémantique attendue n'est spécifiée nulle part : faut-il mémoriser les échecs ?
   que faire d'une ligne `pending` ? existe-t-il un délai de reprise ? Une stratégie en plusieurs transactions laisse
   des lignes `pending` orphelines après un crash réel.
4. **request_dedup ne couvre pas les request_id différents (01-C)** : la protection repose sur les gardes métier.
5. **Éligibilité (01-G2)** : la règle métier n'est pas définie (bascule simple ou garde « pas de course active »). Le
   write skew a été observé sous RC et RR, et l'anomalie « temps réel » existe même sous SERIALIZABLE.
6. **Retry** : `40001` (SERIALIZABLE) et `40P01` sont des issues réelles. Tout appelant (Edge Function, client
   mobile via RPC) qui ne les rejoue pas renverra une erreur à l'utilisateur. Aucun code de retry n'existe pour
   l'instant.
7. **Biais du harnais (01-E)** : un premier jeu de test biaisé masquait un ordre d'exécution. Corrigé avant les
   résultats rapportés. À garder en tête pour l'audit.

## Ce qu'il faut pour transformer ces INCONCLUSIF en PASS/FAIL NAATAL+

- Le schéma SQL réel (migrations Supabase), les fonctions d'acceptation, de redispatch, d'éligibilité et
  d'expiration, et la stratégie `request_dedup`.
- Le niveau d'isolation utilisé par les RPC et Edge Functions, et l'ordre de verrouillage documenté.
- Le mécanisme de retry (où, sur quels SQLSTATE, combien de fois).
- Idéalement un projet Supabase de test (ou `supabase start` en local avec Docker) pour rejouer 01-D2 avec de
  vraies Edge Functions.

Le harnais (`harness/`) est prêt à être pointé sur ces éléments.
