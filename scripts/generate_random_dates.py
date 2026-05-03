"""
지정 범위에서 영업일(평일) 중 랜덤 N일 추출하여 파일로 저장.

사용법:
    python scripts/generate_random_dates.py --from 2024-04-01 --to 2026-04-30 --n 100 --seed 42

저장: data/random_sample_dates.json
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config.settings import settings


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--from", dest="d_from", required=True)
    p.add_argument("--to", dest="d_to", required=True)
    p.add_argument("--n", type=int, default=100)
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--out", default=str(settings.DATA_DIR / "random_sample_dates.json"))
    args = p.parse_args()

    s = datetime.strptime(args.d_from, "%Y-%m-%d").date()
    e = datetime.strptime(args.d_to, "%Y-%m-%d").date()

    weekdays = []
    cur = s
    while cur <= e:
        if cur.weekday() < 5:  # Mon~Fri
            weekdays.append(cur)
        cur += timedelta(days=1)

    if args.n > len(weekdays):
        print(f"요청 N={args.n} > 영업일 {len(weekdays)}, 전체 사용")
        sample = weekdays
    else:
        random.seed(args.seed)
        sample = sorted(random.sample(weekdays, args.n))

    data = {
        "from": args.d_from,
        "to": args.d_to,
        "n": len(sample),
        "seed": args.seed,
        "dates": [d.strftime("%Y-%m-%d") for d in sample],
    }
    Path(args.out).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"저장: {args.out} ({len(sample)}일)")
    print(f"첫 5일: {data['dates'][:5]}")
    print(f"끝 5일: {data['dates'][-5:]}")


if __name__ == "__main__":
    main()
