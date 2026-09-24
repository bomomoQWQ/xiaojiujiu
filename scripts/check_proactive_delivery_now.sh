#!/usr/bin/env bash
# Check whether proactive messages were actually delivered, across all active people.
set -u

echo "=== fleet ==="
curl -s http://127.0.0.1:8800/fleet/status -o /tmp/fleet-status.json
python3 - <<'PY'
import json
s=json.load(open('/tmp/fleet-status.json',encoding='utf-8'))
print('count',s['count'],'healthy',sum(p['health']=='ok' for p in s['people']))
for p in s['people']:
 print(p['person'],p['port'],p['health'],'events',p['raw_events'])
PY

echo
echo "=== proactive chain per person ==="
docker exec -i xxj-runtime-fleet python3 - <<'PY'
import glob,json,os,sqlite3
for path in sorted(glob.glob('/data/*/companion.sqlite3')):
 person=os.path.basename(os.path.dirname(path))
 con=sqlite3.connect(f'file:{path}?mode=ro',uri=True)
 con.row_factory=sqlite3.Row
 commits=con.execute("select created_at,event_type,content,metadata_json from raw_events where event_type='proactive_committed' order by created_at").fetchall()
 sent=con.execute("select created_at,event_type,content,metadata_json from raw_events where event_type='proactive_sent' order by created_at").fetchall()
 attempts=con.execute("select attempt_id,state,intent,created_at,updated_at from action_attempts order by created_at").fetchall()
 print(f'--- {person}: committed={len(commits)} sent={len(sent)} attempts={len(attempts)} ---')
 for r in commits[-5:]: print(' committed',r['created_at'],str(r['content'])[:100])
 for r in sent[-5:]: print(' sent     ',r['created_at'],str(r['content'])[:100])
 for a in attempts[-5:]:
  print(' attempt  ',a['attempt_id'],a['state'],a['created_at'],'->',a['updated_at'],str(a['intent'])[:90])
  ev=con.execute('select from_state,to_state,reason,created_at from attempt_events where attempt_id=? order by created_at',(a['attempt_id'],)).fetchall()
  for e in ev: print('    ',e['created_at'],e['from_state'],'->',e['to_state'],str(e['reason'])[:150])
 print(' latest dialogue:')
 for r in con.execute("select created_at,event_type,substr(coalesce(content,''),1,100) c from raw_events where event_type in ('user_message','assistant_message','proactive_sent') and content!='' order by created_at desc limit 8").fetchall()[::-1]:
  print('   ',r['created_at'],r['event_type'],r['c'])
PY

echo
echo "=== AstrBot send/delivery errors in last 6h ==="
docker logs astrbot-test --since 6h 2>&1 | sed -E 's/\x1b\[[0-9;]*m//g' | grep -iE 'ApiNotAvailable|send_message failed|proactive|transport unavailable|left to the Runtime|delivery' | tail -40 || true

echo
echo "=== adapter/current containers ==="
docker logs astrbot-test --since 30m 2>&1 | sed -E 's/\x1b\[[0-9;]*m//g' | grep -E '适配器已连接|aiocqhttp' | tail -8 || true
docker ps --format '{{.Names}}\t{{.Status}}' | grep -E 'astrbot-test|xxj-napcat-test|xxj-runtime-fleet'
