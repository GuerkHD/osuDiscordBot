from __future__ import annotations
from math import sqrt
from dataclasses import dataclass

@dataclass
class PushInputs:
    pp: float
    SR: float
    TS: float
    accuracy_percent: float
    map_length_seconds: float
    top50_pp_threshold: float

def compute_TS(top10_avg_star_raw: float, top10_miss_sum: int) -> float:
    # TS = avg(SR_top10) - ( sqrt(sum(Misses_top10)) / 10 )
    return top10_avg_star_raw - (sqrt(top10_miss_sum) / 10.0)

def compute_push_value(inputs: PushInputs) -> float:
    pp = inputs.pp
    SR = inputs.SR
    TS = inputs.TS
    acc = inputs.accuracy_percent
    length = inputs.map_length_seconds
    Top50 = inputs.top50_pp_threshold

    # Cases in order of specification
    if (pp > Top50) and (SR < TS):
        return max(-10000.0, -10000.0 * (TS - SR))
    if (pp > Top50) and (SR >= TS):
        return 0.0
    if (pp <= Top50) and (acc > 95.0):
        return 0.0
    if (pp <= Top50) and (92.0 <= acc <= 95.0):
        return ((95 - acc) / 3) * length
    if (pp <= Top50) and (85.0 <= acc < 92.0):
        return length
    if (pp <= Top50) and (75.0 <= acc < 85.0):
        return (0.08 * acc - 5.8) * length
    if (pp <= Top50) and (acc < 75.0):
        return 0.2 * length
    return 0.0
