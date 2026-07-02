import database as db
db.init_db()

doms = db.get_domains()
print("=== Domains ===")
for d in doms:
    print(f"  id={d['id']}, name={d['name']}, url={d['url']}")

cfg = db.get_config()
print(f"\n=== Config ===")
print(f"  Browsers: {cfg.get('concurrent_browsers', 10)}")
print(f"  Proxy mode: {cfg.get('proxy_mode', 'local_file')}")
print(f"  Proxy file: {cfg.get('proxy_file', '')}")

if doms:
    steps = db.get_steps(doms[0]['id'])
    print(f"\n=== Steps for '{doms[0]['name']}' ({len(steps)} steps) ===")
    for s in steps:
        sv = s.get('selector_value', '')[:50]
        iv = s.get('input_value', '')[:30]
        print(f"  {s['action']} | {s.get('selector_type','')}: {sv} | input: {iv}")
