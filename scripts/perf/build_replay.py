"""Build a replay workload for profile.mjs from real bridge event logs.

    python3 build_replay.py --session <session-id> [--out out/replay.json]
        [--window-ms 30000] [--children 8] [--no-parent]
        [--density thinking_delta,text_delta,tool_use_start,tool_result]

Parent: the densest window of the session's own stream (by --density event
types), wrapped in a fresh turn with remapped ids so it appends instead of
colliding with history. Children: the densest window from each of the
session's largest sub-agent logs, kept under their real session ids — the
renderer folds those into sessionArchive exactly as it does during a live
swarm.

Output: [[offset_ms, event], ...] sorted by offset. Replays contain real
session content: keep them under out/ (git-ignored), never commit them.
"""
import argparse
import bisect
import json
import os
from collections import Counter

ap = argparse.ArgumentParser()
ap.add_argument('--session', required=True)
ap.add_argument('--out', default='out/replay.json')
ap.add_argument('--window-ms', type=int, default=30000)
ap.add_argument('--children', type=int, default=8)
ap.add_argument('--no-parent', action='store_true')
ap.add_argument('--density', default='thinking_delta,text_delta,tool_use_start,tool_result')
ap.add_argument('--home', default=os.environ.get('FREYJA_HOME', os.path.expanduser('~/.freyja')))
args = ap.parse_args()

SESS = os.path.join(args.home, 'sessions')
WINDOW_MS = args.window_ms
DENSITY = set(args.density.split(','))
STREAM_TYPES = {
    'text_delta', 'thinking_delta', 'tool_use_start', 'tool_input_delta',
    'tool_input_end', 'tool_result', 'usage', 'llm_call_metric', 'message_stop',
    'system_event', 'inbox_event', 'bus_message', 'memory_retrieved',
    'skill_retrieved', 'file_change_set', 'subagent_update', 'pressure_signal',
}


def load(path):
    rows = []
    with open(path) as f:
        for line in f:
            try:
                e = json.loads(line)
            except Exception:
                continue
            if '_t' in e:
                rows.append(e)
    return rows


def densest_window(rows, types):
    rows = [r for r in rows if r.get('type') in types]
    if not rows:
        return []
    ts = [r['_t'] for r in rows]
    pre = [0]
    for r in rows:
        pre.append(pre[-1] + (1 if r.get('type') in DENSITY else 0))
    best, best_i = 0, 0
    for i, t in enumerate(ts):
        j = bisect.bisect_right(ts, t + WINDOW_MS)
        if pre[j] - pre[i] > best:
            best, best_i = pre[j] - pre[i], i
    j = bisect.bisect_right(ts, ts[best_i] + WINDOW_MS)
    return rows[best_i:j]


def remap_tool_id(e):
    if e.get('type', '').startswith('tool_') and 'id' in e:
        e['id'] = e['id'] + '_r'


out = []
sid = args.session
turn = 'turn-replay-1'
if not args.no_parent:
    win = densest_window(load(os.path.join(SESS, f'{sid}.events.jsonl')), STREAM_TYPES - {'subagent_update'})
    t0 = win[0]['_t'] if win else 0
    out.append([0, {'type': 'turn_start', 'sessionId': sid, 'turnId': turn}])
    for e in win:
        e = dict(e)
        off = e.pop('_t') - t0 + 5
        if 'turnId' in e:
            e['turnId'] = turn
        remap_tool_id(e)
        if e['type'] != 'message_stop':
            out.append([off, e])

index = json.load(open(os.path.join(SESS, '_index.json')))
kids = [r['id'] for r in index['sessions'] if r.get('parentSessionId') == sid]
kids = [k for k in kids if os.path.exists(os.path.join(SESS, f'{k}.events.jsonl'))]
kids.sort(key=lambda k: -os.path.getsize(os.path.join(SESS, f'{k}.events.jsonl')))
for k in kids[: args.children]:
    w = densest_window(load(os.path.join(SESS, f'{k}.events.jsonl')), STREAM_TYPES)
    if not w:
        continue
    c0 = w[0]['_t']
    for e in w:
        e = dict(e)
        off = e.pop('_t') - c0 + 5
        remap_tool_id(e)
        if e['type'] not in ('message_stop', 'turn_complete'):
            out.append([off, e])

if not args.no_parent:
    out.append([WINDOW_MS + 50, {'type': 'message_stop', 'sessionId': sid, 'stopReason': 'end_turn'}])
    out.append([WINDOW_MS + 60, {'type': 'turn_complete', 'sessionId': sid, 'turnId': turn, 'success': True}])
out.sort(key=lambda r: r[0])
os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
json.dump(out, open(args.out, 'w'))
print(f'{len(out)} events over {WINDOW_MS}ms from {min(len(kids), args.children)} children'
      f'{"" if args.no_parent else " + parent"} -> {args.out}')
print(Counter(e['type'] for _, e in out).most_common(8))
