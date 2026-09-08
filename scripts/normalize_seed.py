#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""normalize_seed.py —— 把运行库导出为"列与运行时完全一致"的规范 seed。

背景: 运行库 funds 是"老库+兼容补列"的 53 列(含遗留 max_dd/dd7), 而全新环境
运行时 funds 为 52 列(SCHEMA 44 + init_db 补 8 列, 用 max_daily_drop_7d/mdd)。
官方导入器若按 SELECT* 列序盲插会错位/失败。本脚本生成:
  - funds 列序 == 运行时 52 列(逐列名映射: max_daily_drop_7d←dd7, mdd 已有)
  - nav_history 与运行时一致
  - 并执行"两池清空": 仅当榜/自选/持仓基金保留展示指标, 其余置 NULL
用法: cd backend && python3 scripts/normalize_seed.py
"""
import os, sys, sqlite3
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import db as dbm
from modules.rank.ranker import _current_visible_codes, _CLEARABLE_COLS

def cols(c, t):
    return [r[1] for r in c.execute(f"PRAGMA table_info({t})").fetchall()]

def main():
    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    src_path = os.path.join(base, "fund.db")
    seed = os.path.join(base, "fund_core_data.db.new")
    for f in (seed, seed + "-wal", seed + "-shm"):
        if os.path.exists(f): os.remove(f)
    dst = sqlite3.connect(seed)
    dst.executescript(dbm.SCHEMA)
    for _c, _t in (("yindie","TEXT"),("d1","REAL"),("d2","REAL"),("reco","TEXT"),
                   ("reco_score","INTEGER"),("reco_days","INTEGER"),("prev_reco","TEXT"),
                   ("prev_reco_days","INTEGER")):
        dst.execute(f"ALTER TABLE funds ADD COLUMN {_c} {_t}")
    dst.commit()
    ALIAS = {"max_daily_drop_7d": ("dd7", "max_daily_drop_7d"), "mdd": ("mdd", "max_dd")}
    src = sqlite3.connect(f"file:{src_path}?mode=ro", uri=True)
    vis = _current_visible_codes(src)
    print("可见池:", len(vis))
    for t in ("funds", "nav_history"):
        sc, dc = cols(src, t), cols(dst, t)
        cm = []
        for x in dc:
            if x in sc: cm.append(sc.index(x)); continue
            a = None
            for c in ALIAS.get(x, ()):
                if c in sc: a = sc.index(c); break
            cm.append(a)
        rows = src.execute(f"SELECT * FROM {t}").fetchall()
        ins = f"INSERT INTO {t} ({','.join(dc)}) VALUES ({','.join('?'*len(dc))})"
        dst.executemany(ins, [tuple(r[i] if i is not None else None for i in cm) for r in rows])
        dst.commit()
        print(f"导入 {t}: {len(rows)} 行 ({len(sc)}→{len(dc)}列)")
    cc = [c for c in _CLEARABLE_COLS if c in cols(dst, "funds")]
    marks = ",".join("?" * len(vis))
    cur = dst.execute(
        f"UPDATE funds SET {','.join(f'{c}=?' for c in cc)} WHERE code NOT IN ({marks})",
        tuple([None] * len(cc)) + tuple(vis))
    dst.commit()
    print(f"非可见清空: {cur.rowcount} 只 / {len(cc)} 列")
    dst.close(); src.close()
    con = sqlite3.connect(f"file:{seed}?mode=ro", uri=True)
    print("funds 列数:", len(cols(con, "funds")),
          "| funds:", con.execute("SELECT COUNT(*) FROM funds").fetchone()[0],
          "| nav:", con.execute("SELECT COUNT(*) FROM nav_history").fetchone()[0],
          "| integrity:", con.execute("PRAGMA integrity_check").fetchone()[0])
    con.close()
    os.replace(seed, os.path.join(base, "fund_core_data.db"))
    for f in (seed + "-wal", seed + "-shm"):
        if os.path.exists(f): os.remove(f)
    print("OK ->", os.path.join(base, "fund_core_data.db"))

if __name__ == "__main__":
    main()
