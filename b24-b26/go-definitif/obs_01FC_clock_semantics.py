"""01-F-C : sémantique des horloges PostgreSQL. Moteur seul, aucune fonction testée.
Les seuls objets créés sont un outil d'observation (schéma obs, 1 ligne à verrouiller).
Les attentes viennent de l'extérieur : sommeil côté client, et une seconde session qui tient un verrou de ligne."""
import threading, time, json, datetime, os
import psycopg
DSN = os.environ.get("B2426_DSN", "host=localhost dbname=b2426 user=tester password=tester")
N = 30
out = open(os.path.join(os.path.dirname(__file__), "01-F-C.log"), "w")
def w(*a):
    s = " ".join(str(x) for x in a); print(s); out.write(s + "\n"); out.flush()
Q = "select now(), current_timestamp, transaction_timestamp(), statement_timestamp(), clock_timestamp()"
with psycopg.connect(DSN, autocommit=True) as c:
    w("#", c.execute("select version()").fetchone()[0], "| run", datetime.datetime.now().isoformat())
    c.execute("create schema if not exists obs; create table if not exists obs.lockrow(id int primary key); insert into obs.lockrow values (1) on conflict do nothing")

w(f"\n## S1 : transaction explicite, 2 lectures séparées de 1,0 s (sommeil client), {N} répétitions")
anom = {"now<>current_timestamp": 0, "now<>transaction_timestamp": 0, "now_changed_in_tx": 0,
        "statement_not_advanced": 0, "clock_not_advanced": 0}
for i in range(N):
    c = psycopg.connect(DSN, autocommit=False)
    a = c.execute(Q).fetchone(); time.sleep(1.0); b = c.execute(Q).fetchone(); c.commit(); c.close()
    for r in (a, b):
        anom["now<>current_timestamp"] += r[0] != r[1]
        anom["now<>transaction_timestamp"] += r[0] != r[2]
    anom["now_changed_in_tx"] += a[0] != b[0]
    anom["statement_not_advanced"] += (b[3] - a[3]).total_seconds() < 0.99
    anom["clock_not_advanced"] += (b[4] - a[4]).total_seconds() < 0.99
    w(f"  rep {i+1:2d}: lecture1 now={a[0].time()} stmt={a[3].time()} clock={a[4].time()} | "
      f"lecture2 now={b[0].time()} stmt={b[3].time()} clock={b[4].time()} | "
      f"Δnow={(b[0]-a[0]).total_seconds():.6f}s Δstmt={(b[3]-a[3]).total_seconds():.3f}s Δclock={(b[4]-a[4]).total_seconds():.3f}s")
w("  compteurs d'écart:", json.dumps(anom))

w(f"\n## S2 : instruction unique en autocommit, bloquée environ 1,0 s par une 2ᵉ session qui tient FOR UPDATE sur obs.lockrow, {N} répétitions")
w("   requête: with l as (select id from obs.lockrow where id=1 for update) select now(), current_timestamp, transaction_timestamp(), statement_timestamp(), clock_timestamp() from l")
anom2 = {"now<>current_timestamp": 0, "now<>statement(diff>1ms)": 0, "clock-now<0.9s": 0}
for i in range(N):
    held = threading.Event()
    def holder():
        h = psycopg.connect(DSN, autocommit=False)
        h.execute("select 1 from obs.lockrow where id=1 for update"); held.set()
        time.sleep(1.0); h.rollback(); h.close()
    t = threading.Thread(target=holder); t.start(); held.wait()
    c = psycopg.connect(DSN, autocommit=True); t0 = time.monotonic()
    r = c.execute("with l as (select id from obs.lockrow where id=1 for update) "
                  "select now(), current_timestamp, transaction_timestamp(), statement_timestamp(), clock_timestamp() from l").fetchone()
    wait = time.monotonic() - t0; c.close(); t.join()
    anom2["now<>current_timestamp"] += r[0] != r[1]
    anom2["now<>statement(diff>1ms)"] += abs((r[3]-r[0]).total_seconds()) > 0.001
    anom2["clock-now<0.9s"] += (r[4]-r[0]).total_seconds() < 0.9
    w(f"  rep {i+1:2d}: attente={wait:.3f}s now={r[0].time()} current_ts={r[1].time()} stmt={r[3].time()} clock={r[4].time()} "
      f"clock-now={(r[4]-r[0]).total_seconds():.3f}s")
w("  compteurs d'écart:", json.dumps(anom2))
