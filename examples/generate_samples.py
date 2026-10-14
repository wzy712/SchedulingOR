"""Generate the three seeded scenarios as runnable specs.

Deterministic (fixed seed) so the golden-instance tests stay stable.

    .venv/bin/python examples/generate_samples.py           # 写入 examples/
    .venv/bin/python examples/generate_samples.py --check   # 只校验，不写文件

Scenarios:
  1. exam-invigilation  期末考试监考   -- 一天两场、考场并行、按学科回避
  2. duty-roster        教师值班/值周  -- 长周期、周末均衡是核心诉求
  3. after-school       课后服务/晚自习 -- 按科目匹配辅导教师、课时均衡
"""

from __future__ import annotations

import argparse
import datetime as dt
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import yaml  # noqa: E402

from scheduling_or.schema.spec import Spec, validate_text  # noqa: E402

OUT = Path(__file__).resolve().parent

SURNAMES = list("赵钱孙李周吴郑王冯陈褚卫蒋沈韩杨朱秦尤许何吕施张孔曹严华金魏陶姜")
GIVEN = ["志远", "秀英", "建华", "晓明", "丽华", "国强", "文静", "海燕", "俊杰", "雪梅",
         "伟东", "婷婷", "鹏飞", "雅琴", "浩然", "梦琪", "宇轩", "思远", "嘉怡", "子涵"]


def _people(n: int, subjects: list[str], rng: random.Random, *, levels: bool = True):
    people = []
    for i in range(n):
        name = rng.choice(SURNAMES) + rng.choice(GIVEN)
        primary = subjects[i % len(subjects)]
        extra = rng.sample([s for s in subjects if s != primary], k=min(1, len(subjects) - 1))
        skills = sorted({primary, *extra})
        level = "senior" if i % 4 == 0 else ("junior" if i % 4 == 3 else "regular")
        person = {
            "id": f"T{i + 1:02d}",
            "name": name,
            "role": "teacher",
            "skills": skills,
            "attributes": {"subject": primary},
            "max_shifts": 14,
        }
        if levels:
            person["attributes"]["level"] = level
        people.append(person)
    return people


def _weekdays(start: dt.date, n: int) -> list[dt.date]:
    return [start + dt.timedelta(days=i) for i in range(n)]


# --------------------------------------------------------------------------
# 1. 期末考试监考
# --------------------------------------------------------------------------
def exam_invigilation() -> dict:
    rng = random.Random(20250601)
    subjects = ["chinese", "math", "english", "physics", "chemistry"]
    start = dt.date(2025, 6, 16)  # Monday
    days = _weekdays(start, 5)
    people = _people(24, subjects, rng)
    # 回避：老师不监考自己任教科目的场次 —— 用 unavailability 表达
    sessions = [("s1", "第一场", "09:00", "11:00"), ("s2", "第二场", "14:00", "16:00")]
    subject_of = {(d, s[0]): subjects[(i * 2 + j) % len(subjects)]
                  for i, d in enumerate(days) for j, s in enumerate(sessions)}
    unavail = []
    for person in people:
        own = person["attributes"]["subject"]
        for (d, sid), subj in subject_of.items():
            if sid == "s1" and subj == own:
                unavail.append(
                    {"person": person["id"], "dates": [d.isoformat()],
                     "shifts": [sid], "reason": f"回避本科目监考（{subj}）"}
                )
    demand = []
    for d in days:
        for sid, _n, _a, _b in sessions:
            demand.append({"date": d.isoformat(), "shift": sid, "min": 5, "target": 6, "max": 8})
    return {
        "schema_version": 1,
        "name": "期末考试监考（合成样例）",
        "instance": {
            "horizon": {"start": days[0].isoformat(), "end": days[-1].isoformat()},
            "shifts": [
                {"id": sid, "name": nm, "start": a, "end": b} for sid, nm, a, b in sessions
            ],
            "people": people,
            "groups": [
                {"id": "all_teachers", "all": True},
                *[{"id": f"{s}_teachers", "by_skill": s} for s in subjects],
                {"id": "seniors", "by_attribute": {"level": "senior"}},
            ],
            "demand": demand,
            "unavailability": unavail[:60],
            "preferences": [
                {"person": "T03", "shift": "s2", "kind": "avoid", "date": days[4].isoformat(),
                 "weight": 2, "note": "下午要接孩子"},
                {"person": "T07", "shift": "s1", "kind": "want", "date": days[0].isoformat(),
                 "weight": 1},
            ],
        },
        "constraints": {
            "hard": [
                # 一天可以监两场，但两场之间必须留出午休（≥1h，由 rest_between_shifts 保证）
                {"type": "one_shift_per_day", "max_per_day": 2},
                {"type": "rest_between_shifts", "min_hours": 1},
                {"type": "max_consecutive_work_days", "days": 5},
                {"type": "forbid_transition", "from": "s1", "to": "s2"},
                {"type": "requirement", "shift": "s1", "group": "seniors", "min": 1},
                {"type": "no_pair", "people": ["T01", "T05"], "note": "两人有亲属关系，不宜同场"},
            ],
            "soft": [
                {"type": "coverage_shortfall", "weight": 1000},
                {"type": "coverage_excess", "weight": 20},
                {"type": "fairness", "metric": "total_shifts", "aggregation": "spread", "weight": 60},
                {"type": "fairness", "metric": "shift:s1", "aggregation": "spread", "weight": 40},
                {"type": "preference", "weight": 30},
                {"type": "consecutive_work_preference", "preferred_max": 3, "weight": 15},
            ],
        },
        "solver": {"max_time_seconds": 30, "num_workers": 8, "strategy": "lexicographic"},
        "assumptions": [
            "每位教师一天最多监考两场，且两场之间至少间隔 1 小时（默认值，请确认）",
            "每场考试需要 5~8 名监考，其中至少 1 名资深教师",
            "已按「教师不监考自己任教科目的场次」生成回避记录",
        ],
        "unresolved": ["是否需要为行动不便的教师单独安排低楼层考场？"],
    }


# --------------------------------------------------------------------------
# 2. 教师值班 / 值周
# --------------------------------------------------------------------------
def duty_roster() -> dict:
    rng = random.Random(20250901)
    subjects = ["chinese", "math", "english"]
    start = dt.date(2025, 9, 1)
    days = _weekdays(start, 30)
    people = _people(18, subjects, rng, levels=False)
    shifts = [("day", "白班值班", "08:00", "18:00"), ("night", "夜班值班", "18:00", "22:00")]
    demand = []
    for d in days:
        demand.append({"date": d.isoformat(), "shift": "day", "min": 1, "target": 1, "max": 1})
        if d.weekday() >= 5:  # 周末需要夜班值守
            demand.append({"date": d.isoformat(), "shift": "night", "min": 1, "target": 1, "max": 1})
    return {
        "schema_version": 1,
        "name": "教师值班表（合成样例）",
        "instance": {
            "horizon": {"start": days[0].isoformat(), "end": days[-1].isoformat()},
            "shifts": [{"id": sid, "name": nm, "start": a, "end": b} for sid, nm, a, b in shifts],
            "people": people,
            "groups": [
                {"id": "all_teachers", "all": True},
                {"id": "weekend_capable", "by_skill": "chinese"},
            ],
            "demand": demand,
            "unavailability": [
                {"person": "T04", "dates": [d.isoformat() for d in days[:3]], "reason": "外出培训"},
                {"person": "T11", "dates": [d.isoformat() for d in days[10:12]], "reason": "病假"},
            ],
            "preferences": [
                {"person": "T02", "shift": "night", "kind": "avoid", "weekday": "sat", "weight": 2},
                {"person": "T09", "shift": "day", "kind": "want", "weekday": "mon", "weight": 1},
            ],
        },
        "constraints": {
            "hard": [
                {"type": "one_shift_per_day", "max_per_day": 1},
                {"type": "rest_between_shifts", "min_hours": 12},
                {"type": "max_consecutive_work_days", "days": 5},
                {"type": "weekly_rest_days", "min": 2},
                # 只设上限（劳动法规），不设下限：下限会强制每个人都上班，
                # 而值班岗位本身很少，反而会造成结构性不可行
                {"type": "hours_bounds", "period": "week", "max": 40},
            ],
            "soft": [
                {"type": "coverage_shortfall", "weight": 1000},
                {"type": "coverage_excess", "weight": 50},
                {"type": "fairness", "metric": "weekend_shifts", "aggregation": "spread", "weight": 200},
                {"type": "fairness", "metric": "total_shifts", "aggregation": "spread", "weight": 80},
                {"type": "fairness", "metric": "shift:night", "aggregation": "spread", "weight": 120},
                {"type": "preference", "weight": 30},
            ],
        },
        "solver": {"max_time_seconds": 60, "num_workers": 8, "strategy": "lexicographic"},
        "assumptions": [
            "工作日只需 1 人白班值班，周末额外需要 1 人夜班值守（默认值，请确认）",
            "每周至少休息 2 天，连续值班不超过 5 天",
            "班次间最小休息 12 小时",
            "只设每周工时上限 40 小时，不设下限（值班岗位有限，设下限会强制全员上班）",
        ],
        "unresolved": ["法定节假日是否按周末规则加倍值守？"],
    }


# --------------------------------------------------------------------------
# 3. 课后服务 / 晚自习辅导
# --------------------------------------------------------------------------
def after_school() -> dict:
    rng = random.Random(20250301)
    subjects = ["math", "chinese", "english", "physics"]
    start = dt.date(2025, 3, 3)  # Monday
    days = _weekdays(start, 20)  # 4 weeks
    weekdays = [d for d in days if d.weekday() < 5]
    people = _people(14, subjects, rng)
    shifts = [("p7", "第七节辅导", "16:00", "17:00"), ("p8", "晚自习", "19:00", "20:30")]
    demand = []
    for d in weekdays:
        demand.append({"date": d.isoformat(), "shift": "p7", "min": 2, "target": 3, "max": 3})
        demand.append({"date": d.isoformat(), "shift": "p8", "min": 1, "target": 2, "max": 2})
    # 按科目匹配：奇数日需要数学老师，偶数日需要英语老师
    math_days = [d.isoformat() for i, d in enumerate(weekdays) if i % 2 == 0]
    eng_days = [d.isoformat() for i, d in enumerate(weekdays) if i % 2 == 1]
    return {
        "schema_version": 1,
        "name": "课后服务与晚自习辅导（合成样例）",
        "instance": {
            "horizon": {"start": days[0].isoformat(), "end": days[-1].isoformat()},
            "shifts": [{"id": sid, "name": nm, "start": a, "end": b} for sid, nm, a, b in shifts],
            "people": people,
            "groups": [
                {"id": "all_tutors", "all": True},
                {"id": "math_tutors", "by_skill": "math"},
                {"id": "english_tutors", "by_skill": "english"},
                {"id": "seniors", "by_attribute": {"level": "senior"}},
            ],
            "demand": demand,
            "unavailability": [
                {"person": "T06", "date_range": [days[5].isoformat(), days[6].isoformat()],
                 "reason": "教研活动"},
            ],
            "preferences": [
                {"person": "T02", "shift": "p8", "kind": "avoid", "weekday": "fri", "weight": 2},
                {"person": "T12", "shift": "p7", "kind": "want", "weekday": "tue", "weight": 1},
            ],
        },
        "constraints": {
            "hard": [
                {"type": "one_shift_per_day", "max_per_day": 2},
                {"type": "rest_between_shifts", "min_hours": 1},
                {"type": "max_consecutive_work_days", "days": 5},
                {"type": "weekly_rest_days", "min": 1},
                # 按科目匹配：数学日至少 2 名数学老师，英语日至少 2 名英语老师
                {"type": "requirement", "shift": "p7", "group": "math_tutors", "min": 2,
                 "dates": math_days, "note": "数学辅导日"},
                {"type": "requirement", "shift": "p7", "group": "english_tutors", "min": 2,
                 "dates": eng_days, "note": "英语辅导日"},
                {"type": "pairing", "junior_group": "all_tutors", "senior_group": "seniors"},
            ],
            "soft": [
                {"type": "coverage_shortfall", "weight": 1000},
                {"type": "coverage_excess", "weight": 20},
                {"type": "skill_shortfall", "shift": "p7", "group": "seniors", "min": 1,
                 "weight": 300, "note": "尽量每晚有一位资深教师带班"},
                {"type": "fairness", "metric": "total_shifts", "aggregation": "spread", "weight": 80},
                {"type": "fairness", "metric": "shift:p8", "aggregation": "spread", "weight": 60},
                {"type": "preference", "weight": 30},
            ],
        },
        "solver": {"max_time_seconds": 45, "num_workers": 8, "strategy": "lexicographic"},
        "assumptions": [
            "每周一至周五开设，每天第七节与晚自习各一轮（默认值，请确认）",
            "第七节辅导需要 2~3 名教师，晚自习需要 1~2 名",
            "数学日与英语日交替，由对应科目教师承担",
        ],
        "unresolved": ["同一教师能否连排第七节与晚自习？当前允许但间隔需 ≥1 小时"],
    }


SCENARIOS = {
    "exam-invigilation": exam_invigilation,
    "duty-roster": duty_roster,
    "after-school": after_school,
}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--check", action="store_true", help="只校验，不写文件")
    ap.add_argument("--out", type=Path, default=OUT)
    args = ap.parse_args()

    failures = 0
    for name, factory in SCENARIOS.items():
        data = factory()
        text = yaml.safe_dump(data, allow_unicode=True, sort_keys=False)
        result = validate_text(text)
        if not result["ok"]:
            print(f"❌ {name}: {result['stage']} -> {result['errors'][:3]}")
            failures += 1
            continue
        spec = Spec.loads(text)
        summary = result["summary"]
        print(
            f"✅ {name}: {summary['people']} 人 × {summary['dates']} 天 × {summary['shifts']} 班次，"
            f"硬 {summary['hard']} / 软 {summary['soft']}"
        )
        if not args.check:
            target = args.out / f"{name}.spec.yaml"
            target.write_text(text, encoding="utf-8")
            print(f"   已写入 {target.relative_to(ROOT)}")
        _ = spec
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
