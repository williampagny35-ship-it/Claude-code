# B24/B26 — GO DÉFINITIF : observations brutes

Date : 2026-10-03. Mode : observation. Aucun code testé n'a été écrit ni modifié.

## Étape 1 : inspection (prérequis à tout test)

### 1.a Définitions réelles demandées

| Objet demandé | Recherche effectuée | Résultat |
|---|---|---|
| RPC d'acceptation | `pg_proc` de toutes les bases du serveur (`postgres`, `b2426`) ; `git ls-remote` ; `list_repos` ; `grep -r` sur le système de fichiers | **Introuvable** |
| `is_driver_eligible()` | idem, motif `eligib` | **Introuvable** |
| Contrôle d'expiration | idem | **Introuvable** |
| `request_dedup` (table réelle) | `information_schema.tables` | **Introuvable** (seul `fx.request_dedup` existe : c'est ma maquette de la session précédente, pas l'objet NAATAL+) |
| Mise à jour de `dispatch_offers` | idem, motif `dispatch_offers` | **Introuvable** |
| Écriture dans `ride_transitions` | idem, motif `ride_transitions` | **Introuvable** |

État du dépôt `williampagny35-ship-it/Claude-code` après `git fetch --all` : une seule branche,
`claude/naatal-b24-b26-tests-gpsnpn`, avec un seul commit (`df173a8`, mon harnais de la session précédente).
La session n'a accès à aucun autre dépôt (`list_repos` n'en renvoie qu'un). Aucune variable d'environnement
Supabase/PostgreSQL et aucun identifiant vers une base distante ne sont présents. La seule occurrence des noms
`is_driver_eligible` / `dispatch_offers` / `ride_transitions` sur la machine est le transcript de cette conversation.

### 1.b Frontières de transaction

| Point | Constat |
|---|---|
| Edge Function ou RPC | Aucun code d'Edge Function ni de RPC : **indéterminable** |
| BEGIN/COMMIT explicites | **Indéterminable** |
| Niveau d'isolation effectif | Seule valeur mesurable : le défaut du serveur local, `read committed`. Le niveau effectif de NAATAL+ est **indéterminable** (pas d'accès au projet Supabase) |
| Ordre réel des verrous | **Indéterminable** (aucune fonction à lire) |

### 1.c Résultats attendus énoncés avant exécution

Sans les définitions réelles, je ne peux pas déduire de l'implémentation un résultat attendu ni un nombre de
transitions attendu. Les seules attentes que je peux poser découlent de l'**intitulé de chaque test**. Je les
enregistre ici, avant toute exécution, pour qu'elles servent de référence quand le code sera fourni. Elles devront
être confrontées aux définitions réelles.

| Test | Résultat attendu (d'après l'intitulé) | Transitions `ride_transitions` attendues | Invariant |
|---|---|---|---|
| 01-A | 1 acceptation réussie, 1 refusée | 1 | une course a au plus un chauffeur affecté par génération |
| 01-B | 1 réussie, N−1 refusées | 1 | idem |
| 01-C | 1 réussie, l'autre refusée ou sans effet | 1 | un seul effet métier pour une même intention |
| 01-D | 1 exécution, les autres renvoient le résultat mémorisé | 1 | même request_id ⇒ au plus une exécution |
| 01-D2 | le retry ne ré-exécute pas | 1 | idem, à travers un crash |
| 01-E | soit acceptée (redispatch refusé), soit redispatchée (acceptation refusée) | 1 ou 0 selon l'issue, jamais les deux | pas d'acceptation sur une génération périmée |
| 01-F | acceptation refusée si le contrôle a lieu après `expires_at` | 0 | aucune offre acceptée après son expiration |
| 01-G2 | aucun chauffeur inéligible affecté (définition exacte à confirmer d'après `is_driver_eligible()`) | 0 ou 1 selon l'ordre sérialisé | éligibilité vérifiée au moment de l'affectation |
| 01-H | 1 offre `accepted`, les autres dans un état terminal cohérent | 1 par course affectée | cohérence `rides` / `dispatch_offers` / `ride_transitions` |
| 01-I2 | A : pas de `40P01` sur l'ordre réel ; B : `40P01` possible, avec récupération | selon l'issue | aucun état partiel après une erreur |

## Étape 2 : exécution

Règle appliquée : sans définitions réelles, exécuter 01-A…01-I2 reviendrait à tester une maquette que j'aurais
écrite moi-même. Ce serait contraire à « aucune modification d'architecture ni de code testé » et à « relever les
définitions réelles ». **Je n'ai donc exécuté aucun test NAATAL+.**

Un seul point est indépendant du code NAATAL+ et a pu être établi sur le moteur : **la sémantique des horloges
(01-F-C)**.

### 01-F-C : sémantique garantie des horloges (moteur seul)

- **Environnement :** PostgreSQL 16.14 local.
- **Objets créés :** uniquement un outil d'observation (`obs.lockrow`, 1 ligne à verrouiller), sans fonction, sans
  trigger, sans `pg_sleep`.
- **Attentes provoquées par des outils externes :** un sommeil côté client, et une seconde session qui tient
  `FOR UPDATE`.
- **Répétitions :** 30 par scénario.

**S1 — transaction explicite, 2 lectures séparées de 1,0 s :**

```
rep  1: lecture1 now=14:29:53.009468 stmt=14:29:53.009720 clock=14:29:53.010028
        lecture2 now=14:29:53.009468 stmt=14:29:54.011213 clock=14:29:54.011401
        Δnow=0.000000s Δstmt=1.001s Δclock=1.001s
compteurs d'écart sur 30 répétitions :
  now<>current_timestamp: 0   now<>transaction_timestamp: 0   now_changed_in_tx: 0
  statement_not_advanced: 0   clock_not_advanced: 0
```

**S2 — instruction unique en autocommit (forme d'un RPC), bloquée environ 1 s par une 2ᵉ session :**

```
requête : with l as (select id from obs.lockrow where id=1 for update)
          select now(), current_timestamp, transaction_timestamp(), statement_timestamp(), clock_timestamp() from l
rep 30: attente=0.991s now=14:30:52.929761 current_ts=14:30:52.929761 stmt=14:30:52.929761
        clock=14:30:53.919312 clock-now=0.990s
compteurs d'écart sur 30 répétitions :
  now<>current_timestamp: 0   now<>statement(diff>1ms): 0   clock-now<0.9s: 0
```

**Sémantique observée, identique sur les 60 répétitions (aucune variation, donc aucune anomalie) :**

- `now()` = `CURRENT_TIMESTAMP` = `transaction_timestamp()`. La valeur est **figée pendant toute la transaction**.
- `statement_timestamp()` avance à chaque instruction. Dans une instruction unique, il est égal à `now()` et
  **figé avant l'attente de verrou**.
- `clock_timestamp()` est l'heure murale au moment de l'évaluation. Lu après l'obtention du verrou, il reflète
  l'attente (≈ 0,99 s).

**Verdict 01-F (NAATAL+) : INCONCLUSIF.** La sémantique du moteur est établie, mais on ne sait pas quelle horloge le
contrôle d'expiration réel utilise : sa définition est introuvable.

Log complet : `01-F-C.log` ; script : `obs_01FC_clock_semantics.py`.

## Étape 3 : verdicts

| Test | Verdict | Justification factuelle |
|---|---|---|
| 01-A | INCONCLUSIF | RPC d'acceptation, `dispatch_offers`, `ride_transitions` introuvables : aucun état initial réel ne peut être relevé |
| 01-B | INCONCLUSIF | idem |
| 01-C | INCONCLUSIF | `request_dedup` réelle introuvable |
| 01-D | INCONCLUSIF | idem |
| 01-D2 | INCONCLUSIF | frontières de transaction indéterminables (ni Edge Function ni RPC) ; conformément à la règle, pas de résultat sans frontières certaines |
| 01-E | INCONCLUSIF | fonction de redispatch et colonne de génération introuvables |
| 01-F | INCONCLUSIF | sémantique du moteur établie (ci-dessus), contrôle d'expiration réel introuvable |
| 01-G2 | INCONCLUSIF | `is_driver_eligible()` introuvable |
| 01-H | INCONCLUSIF | les 3 tables (`rides`, `dispatch_offers`, `ride_transitions`) sont introuvables |
| 01-I2 | INCONCLUSIF | ordre réel des verrous indéterminable (I2-A impossible). I2-B a été observé lors de la session précédente, sur la maquette uniquement, et ne prouve rien sur I2-A |

### Écart constaté entre la spec et l'environnement (sans proposition de correction)

Le GO DÉFINITIF suppose l'existence de `is_driver_eligible()`, `dispatch_offers`, `ride_transitions`,
`request_dedup` et d'une RPC d'acceptation. Aucun de ces objets n'est présent dans l'environnement accessible à cette
session.

### Ce qui débloque l'exécution

L'un des trois suffit :
1. pousser les migrations SQL (`supabase/migrations/*.sql`) et les Edge Functions dans ce dépôt ;
2. ajouter à la session le dépôt qui les contient ;
3. fournir une chaîne de connexion vers une base de test Supabase (secret d'environnement).

Le harnais `../harness/` est déjà prêt : seuls les noms d'objets sont à remplacer.
