"""D-21 이전 정규장별 커버리지 기준선. 오늘 밤 데이터는 아직 없다.

정의 (내일 그대로 쓴다):
  모집단 = 그 정규장에 TOSS_SECURITIES_TRADING_VOLUME 상위 N 위에 한 번이라도 든 종목
  커버   = 그 종목이 **같은 정규장 안에** trades_snap 행을 1건 이상 남겼는가
  정규장 = 13:30~20:00 UTC
"""
import sqlite3
import datetime

DB = 'C:/Users/dongh/orca/workspaces/toss_trade/w5-ops/data/tossmon.db'
con = sqlite3.connect('file:' + DB + '?mode=ro', uri=True)
cur = con.cursor()

RT = 'TOSS_SECURITIES_TRADING_VOLUME'
SESSIONS = ['2026-07-31', '2026-08-03', '2026-08-04', '2026-08-05', '2026-08-06',
            '2026-08-07', '2026-08-10', '2026-08-11', '2026-08-12', '2026-08-13']


def window(day):
    d = datetime.datetime.strptime(day, '%Y-%m-%d').replace(tzinfo=datetime.timezone.utc)
    lo = int((d + datetime.timedelta(hours=13, minutes=30)).timestamp() * 1000)
    hi = int((d + datetime.timedelta(hours=20)).timestamp() * 1000)
    return lo, hi


for topn in (10, 100):
    print('=== top-%d ===' % topn)
    print('%-12s %8s %8s %8s   %-8s %8s' %
          ('session', 'ranked', 'w/tape', 'cover%', 'capfill3', 'trades'))
    vals = []
    for day in SESSIONS:
        lo, hi = window(day)
        syms = [r[0] for r in cur.execute(
            'select distinct symbol from rankings_snap '
            'where ranking_type=? and rank<=? and snap_ms between ? and ?',
            (RT, topn, lo, hi)).fetchall()]
        if not syms:
            print('%-12s %8s (no ranking rows)' % (day, 0))
            continue
        ph = ','.join('?' * len(syms))
        tape = cur.execute(
            'select count(distinct symbol) from trades_snap '
            'where symbol in (%s) and ts_ms between ? and ?' % ph,
            syms + [lo, hi]).fetchone()[0]
        capfill = cur.execute(
            "select count(*) from promotions where reason='capacity_fill' "
            "and to_tier=3 and ts_ms between ? and ?", (lo, hi)).fetchone()[0]
        ntr = cur.execute(
            'select count(*) from trades_snap where ts_ms between ? and ?',
            (lo, hi)).fetchone()[0]
        pct = 100.0 * tape / len(syms)
        vals.append(pct)
        print('%-12s %8d %8d %7.1f%%   %-8d %8d' %
              (day, len(syms), tape, pct, capfill, ntr))
    if vals:
        vs = sorted(vals)
        print('  기준선 n=%d  min=%.1f%%  중앙=%.1f%%  max=%.1f%%' %
              (len(vs), vs[0], vs[len(vs) // 2], vs[-1]))
    print()
